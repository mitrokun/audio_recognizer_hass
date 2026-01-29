"""Telegram bot functionality for the Audio Recognizer integration."""
import logging

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
)
from .exceptions import NoAudioStreamError
from .helpers import async_process_audio_data, async_transcode_from_bytes

_LOGGER = logging.getLogger(__name__)


class TelegramBotManager:
    """Manages the Telegram bot lifecycle and message handling."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        """Initialize the bot manager."""
        self.hass = hass
        self.entry = entry
        self.telegram_app: Application | None = None

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

        media_filters = filters.VOICE | filters.AUDIO | filters.Document.AUDIO
        self.telegram_app.add_handler(MessageHandler(media_filters, self.handle_audio_message))

        await self.telegram_app.initialize()
        await self.telegram_app.start()
        if self.telegram_app.updater:
            await self.telegram_app.updater.start_polling()
            _LOGGER.info("Telegram bot started and polling for updates.")

    async def stop_bot(self):
        """Stop the Telegram bot if it is running."""
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

    # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
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

            duration = getattr(media, 'duration', 0)
            max_duration = self.entry.options.get(CONF_TELEGRAM_MAX_DURATION, 180)
            if max_duration > 0 and duration > 0 and duration > max_duration:
                _LOGGER.warning(
                    "Media file from chat_id %s is too long (%s seconds), limit is %s seconds. Ignoring.",
                    chat_id_str, duration, max_duration
                )
                if should_send_reply:
                    await update.message.reply_text(
                        f"❌ Файл слишком длинный ({duration} сек.). "
                        f"Максимальная длительность: {max_duration} сек."
                    )
                return

            media_file = await media.get_file()
            media_data = await media_file.download_as_bytearray()
            audio_data = await async_transcode_from_bytes(bytes(media_data))
            result = await async_process_audio_data(self.hass, stt_entity_id, None, audio_data)
            text = result.get("text")

            if text:
                _LOGGER.info("Recognition successful. Text: '%s'. Firing event.", text)
                self.hass.bus.async_fire(
                    EVENT_TRANSCRIPTION_RECEIVED,
                    {"text": text, "chat_id": chat_id_str, "username": update.message.from_user.username}
                )
                if should_send_reply:
                    await update.message.reply_text(f"🗣️: {text}")
            else:
                if should_send_reply:
                    await update.message.reply_text("Не удалось распознать речь.")

        except NoAudioStreamError:
            _LOGGER.warning("Processing failed because the media file has no audio stream.")
            if should_send_reply:
                await update.message.reply_text("❌ Не удалось обработать: медиафайл не содержит звуковой дорожки.")
        except Exception as e:
            _LOGGER.error("Error processing media message: %s", e, exc_info=True)
            if should_send_reply:
                await update.message.reply_text(f"Произошла ошибка: {e}")