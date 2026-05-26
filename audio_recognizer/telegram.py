"""Telegram bot functionality for the Audio Recognizer integration."""
import asyncio
import contextvars
import logging
import random
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from telegram import Update
from telegram.ext import Application, CallbackContext, MessageHandler, filters

from .const import (
    CONF_TELEGRAM_BOT_TOKEN,
    CONF_TELEGRAM_CHAT_IDS,
    CONF_TELEGRAM_ENABLED,
    CONF_TELEGRAM_MAX_DURATION,
    CONF_TELEGRAM_SEND_REPLY,
    CONF_TELEGRAM_STT_ENTITY_ID,
    EVENT_TRANSCRIPTION_RECEIVED,
    EVENT_TEXT_RECEIVED,
)
from .exceptions import NoAudioStreamError
from .helpers import async_process_audio_data, async_transcode_from_bytes

_LOGGER = logging.getLogger(__name__)


def get_stt_stream_callback_var(hass: HomeAssistant) -> contextvars.ContextVar:
    """Get or create the global STT streaming context variable.
    
    This shared context variable allows any calling application to register 
    a generic callback to receive real-time text chunks.
    """
    if "stt_stream_callback_var" not in hass.data:
        hass.data["stt_stream_callback_var"] = contextvars.ContextVar(
            "stt_stream_callback", default=None
        )
    return hass.data["stt_stream_callback_var"]


class TelegramBotManager:
    """Manages the Telegram bot lifecycle and message handling."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        """Initialize the bot manager."""
        self.hass = hass
        self.entry = entry
        self.telegram_app: Application | None = None
        self._remove_chunk_listener = None
        
        # Track active draft sessions, accumulated texts, and rate limiters
        self._active_drafts = set()
        self._accumulated_texts = {}
        self._last_sent_times = {}

        # Semaphore to serialize incoming ASR requests and protect the ASR engine
        self._stt_semaphore = asyncio.Semaphore(1)

        # Expose context variables to the global HA registry
        self.hass.data["telegram_bot_ctx"] = {
            "chat_id": contextvars.ContextVar("tg_chat_id", default=None),
            "draft_id": contextvars.ContextVar("tg_draft_id", default=None),
        }

    async def start_bot_if_enabled(self):
        """Start the Telegram bot if it's enabled in the config."""
        if not self.entry.options.get(CONF_TELEGRAM_ENABLED):
            return
        token = self.entry.options.get(CONF_TELEGRAM_BOT_TOKEN)
        if not token:
            _LOGGER.error("Telegram bot enabled, but Bot Token is not configured.")
            return

        _LOGGER.info("Starting Telegram bot...")

        def build_app():
            return Application.builder().token(token).build()

        self.telegram_app = await self.hass.async_add_executor_job(build_app)

        # Audio media handler
        media_filters = filters.VOICE | filters.AUDIO | filters.Document.AUDIO
        self.telegram_app.add_handler(MessageHandler(media_filters, self.handle_audio_message))

        # Text handler
        text_filters = (filters.TEXT | filters.FORWARDED) & ~filters.COMMAND
        self.telegram_app.add_handler(MessageHandler(text_filters, self.handle_text_message))

        await self.telegram_app.initialize()
        await self.telegram_app.start()

        # Subscribe to streaming chunks from the HA event bus (local to audio_recognizer)
        self._remove_chunk_listener = self.hass.bus.async_listen(
            "telegram_stream_chunk",
            self._handle_stream_chunk_event
        )

        if self.telegram_app.updater:
            await self.telegram_app.updater.start_polling()
            _LOGGER.info("Telegram bot started and polling for updates.")

    async def stop_bot(self):
        """Stop the Telegram bot if it is running."""
        # Unsubscribe from the event bus
        if self._remove_chunk_listener:
            self._remove_chunk_listener()
            self._remove_chunk_listener = None

        if not self.telegram_app:
            return
            
        _LOGGER.info("Stopping Telegram bot...")
        try:
            if self.telegram_app.updater and self.telegram_app.updater.running:
                await self.telegram_app.updater.stop()
            if self.telegram_app.running:
                await self.telegram_app.stop()
            await self.telegram_app.shutdown()
            _LOGGER.info("Telegram bot stopped successfully.")
        except Exception as e:
            _LOGGER.error("Error while stopping telegram bot: %s", e)
        finally:
            self.telegram_app = None

    async def async_send_message(self, chat_id: str, text: str):
        """Send a message to a Telegram chat."""
        if not self.telegram_app or not self.telegram_app.bot:
            _LOGGER.error("Telegram bot is not available to send a message.")
            return
        try:
            await self.telegram_app.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            _LOGGER.error("Failed to send Telegram message to chat_id %s: %s", chat_id, e)

    async def _handle_stream_chunk_event(self, event):
        """Handle incoming streaming chunks from the HA event bus to update the draft."""
        chat_id = event.data.get("chat_id")
        draft_id = event.data.get("draft_id")
        raw_text = event.data.get("text")

        if not self.telegram_app or not self.telegram_app.bot:
            return

        # If the recognition session has already finalized, ignore late chunks
        if draft_id not in self._active_drafts:
            _LOGGER.debug("Ignoring late chunk for finalized draft_id: %s", draft_id)
            return

        # Clean the raw chunk from STT technical ellipses and extra spaces
        clean_chunk = raw_text.replace("...", "").strip()
        if not clean_chunk:
            return

        # Retrieve and update accumulated text for this draft session
        current_text = self._accumulated_texts.get(draft_id, "")
        if current_text:
            # Smart join: avoid adding a space before punctuation marks (e.g., "word, word")
            if clean_chunk.startswith((".", ",", "!", "?", ":", ";")):
                current_text += clean_chunk
            else:
                current_text += " " + clean_chunk
        else:
            current_text = clean_chunk

        self._accumulated_texts[draft_id] = current_text

        # Rate-limiting: do not send updates to Telegram more than once per 1.0 second
        # to strictly adhere to Telegram's Flood Control policy on message edits.
        current_time = time.time()
        last_sent = self._last_sent_times.get(draft_id, 0.0)
        if current_time - last_sent < 0.3:
            return

        self._last_sent_times[draft_id] = current_time
        formatted_text = f"🗣️: {current_text}"

        try:
            # Update the ephemeral draft message
            await self.telegram_app.bot.send_message_draft(
                chat_id=chat_id,
                draft_id=draft_id,
                text=formatted_text
            )
        except Exception as e:
            _LOGGER.debug("Failed to update Telegram send_message_draft: %s", e)

    async def handle_text_message(self, update: Update, context: CallbackContext):
        """Handle incoming text and forwarded messages from Telegram."""
        chat_id_str = str(update.message.chat_id)
        allowed_ids_str = self.entry.options.get(CONF_TELEGRAM_CHAT_IDS, "")
        allowed_ids = [s.strip() for s in allowed_ids_str.split(',') if s.strip()]

        if allowed_ids and chat_id_str not in allowed_ids:
            _LOGGER.warning("Ignoring text message from unauthorized chat_id: %s", chat_id_str)
            return

        text = update.message.text or update.message.caption
        username = update.message.from_user.username

        if not text:
            return

        _LOGGER.info("Received text: '%s' from chat_id: %s. Firing event.", text, chat_id_str)
        self.hass.bus.async_fire(
            EVENT_TEXT_RECEIVED,
            {"text": text, "chat_id": chat_id_str, "username": username}
        )

    async def handle_audio_message(self, update: Update, context: CallbackContext):
        """Handle incoming voice, audio, and audio-document messages from Telegram."""
        chat_id_str = str(update.message.chat_id)
        allowed_ids_str = self.entry.options.get(CONF_TELEGRAM_CHAT_IDS, "")
        allowed_ids = [s.strip() for s in allowed_ids_str.split(',') if s.strip()]
        
        if allowed_ids and chat_id_str not in allowed_ids:
            _LOGGER.warning("Ignoring message from unauthorized chat_id: %s", chat_id_str)
            return

        stt_entity_id = self.entry.options.get(CONF_TELEGRAM_STT_ENTITY_ID)
        if not stt_entity_id:
            _LOGGER.error("Telegram received a message, but no STT provider is configured.")
            return

        should_send_reply = self.entry.options.get(CONF_TELEGRAM_SEND_REPLY, True)

        try:
            media = update.message.voice or update.message.audio or update.message.document
            if not media:
                return

            # Perform the maximum duration check before acquiring the semaphore
            # to reject invalid files instantly without blocking the queue.
            duration = getattr(media, 'duration', 0)
            max_duration = self.entry.options.get(CONF_TELEGRAM_MAX_DURATION, 180)
            if max_duration > 0 and duration > 0 and duration > max_duration:
                _LOGGER.warning(
                    "Media file from chat_id %s is too long (%s seconds), limit is %s seconds. Ignoring.",
                    chat_id_str, duration, max_duration
                )
                if should_send_reply:
                    await update.message.reply_text(f"❌ File too long ({duration}s). Max: {max_duration}s.")
                return

            # Queue processing sequentially using the STT semaphore to prevent server overload
            async with self._stt_semaphore:
                # Initialize drafting mode and session-specific accumulator once our turn starts
                draft_id = random.randint(100000, 999999)
                self._active_drafts.add(draft_id)
                self._accumulated_texts[draft_id] = ""
                self._last_sent_times[draft_id] = 0.0

                if should_send_reply:
                    # Dispatching an empty text triggers the "Thinking..." indicator
                    await self.telegram_app.bot.send_message_draft(
                        chat_id=update.message.chat_id,
                        draft_id=draft_id,
                        text=""
                    )

                media_file = await media.get_file()
                media_data = await media_file.download_as_bytearray()
                audio_data = await async_transcode_from_bytes(bytes(media_data))

                # Define a generic callback closure that captures session variables
                def telegram_callback(raw_chunk_text: str) -> None:
                    """Closure callback that forwards chunks to Telegram listener."""
                    self.hass.bus.async_fire(
                        "telegram_stream_chunk",
                        {
                            "chat_id": chat_id_str,
                            "draft_id": draft_id,
                            "text": raw_chunk_text
                        }
                    )

                # Retrieve the global ContextVar and bind our callback to this async context
                callback_var = get_stt_stream_callback_var(self.hass)
                callback_token = callback_var.set(telegram_callback)

                try:
                    # Process audio; downstream STT will inherit the set callback ContextVar
                    result = await async_process_audio_data(self.hass, stt_entity_id, None, audio_data)
                    text = result.get("text")
                finally:
                    # Reset the callback context variable
                    callback_var.reset(callback_token)

                    # Clean up the state tracking of this draft session
                    self._active_drafts.discard(draft_id)
                    self._accumulated_texts.pop(draft_id, None)
                    self._last_sent_times.pop(draft_id, None)

                if text:
                    _LOGGER.info("Recognition successful. Text: '%s'. Firing event.", text)
                    self.hass.bus.async_fire(
                        EVENT_TRANSCRIPTION_RECEIVED,
                        {"text": text, "chat_id": chat_id_str, "username": update.message.from_user.username}
                    )
                    if should_send_reply:
                        # Standard reply automatically finalizes/clears the draft on Telegram clients
                        await update.message.reply_text(f"🗣️: {text}")
                else:
                    if should_send_reply:
                        await update.message.reply_text("❌ Recognition failed.")

        except NoAudioStreamError:
            _LOGGER.warning("Processing failed because the media file has no audio stream.")
            if should_send_reply:
                await update.message.reply_text("❌ No audio track found.")
        except Exception as e:
            _LOGGER.error("Error processing media message: %s", e, exc_info=True)
            if should_send_reply:
                await update.message.reply_text(f"❌ Error: {e}")
