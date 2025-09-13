"""
Custom integration to provide a service for recognizing audio files
and a Telegram bot interface for STT providers.
This version uses in-memory transcoding via FFmpeg and runs the bot
correctly within the Home Assistant event loop.
"""
import asyncio
import logging
import os
import io
from pathlib import Path
from collections.abc import AsyncIterable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.components import stt
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import language as language_util

# Telegram-related imports
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, CallbackContext

from .const import (
    DOMAIN,
    CONF_TELEGRAM_ENABLED,
    CONF_TELEGRAM_BOT_TOKEN,
    CONF_TELEGRAM_CHAT_IDS,
    CONF_TELEGRAM_STT_ENTITY_ID,
    EVENT_TRANSCRIPTION_RECEIVED,
    CONF_TELEGRAM_SEND_REPLY
)

_LOGGER = logging.getLogger(__name__)


async def async_transcode_from_path(source_path: str) -> bytes:
    """Transcodes an audio file FROM A DISK PATH to raw PCM bytes."""
    _LOGGER.debug("Transcoding %s to standardized in-memory WAV format", source_path)
    command = ["ffmpeg", "-i", str(source_path), "-f", "wav", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", "-"]
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout_data, stderr_data = await process.communicate()
    if process.returncode != 0:
        error_msg = stderr_data.decode(errors='ignore')
        _LOGGER.error("FFmpeg (from path) failed with code %s: %s", process.returncode, error_msg)
        raise ServiceValidationError(f"Failed to transcode audio file: {source_path}. Error: {error_msg}")
    wav_header_size = 44
    if len(stdout_data) > wav_header_size:
        return stdout_data[wav_header_size:]
    raise ServiceValidationError("Transcoding resulted in empty audio data.")

async def async_transcode_from_bytes(source_data: bytes) -> bytes:
    """Transcodes an audio file FROM A BYTE ARRAY to raw PCM bytes."""
    _LOGGER.debug("Transcoding in-memory audio data to standardized WAV format")
    command = ["ffmpeg", "-i", "-", "-f", "wav", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", "-"]
    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout_data, stderr_data = await process.communicate(input=source_data)
    if process.returncode != 0:
        error_msg = stderr_data.decode(errors='ignore')
        _LOGGER.error("FFmpeg (from bytes) failed with code %s: %s", process.returncode, error_msg)
        raise ServiceValidationError(f"Failed to transcode in-memory audio. Error: {error_msg}")
    wav_header_size = 44
    if len(stdout_data) > wav_header_size:
        return stdout_data[wav_header_size:]
    raise ServiceValidationError("Transcoding resulted in empty audio data.")

async def _async_stream_from_bytes(data: bytes, chunk_size: int = 4096) -> AsyncIterable[bytes]:
    buffer = io.BytesIO(data)
    while chunk := buffer.read(chunk_size):
        yield chunk
        await asyncio.sleep(0)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:

    async def handle_recognize_file_service(call: ServiceCall) -> dict[str, Any]:
        file_path = call.data.get("file_path")
        stt_entity_id = call.data.get("entity_id")
        language = call.data.get("language")
        if not file_path: raise ServiceValidationError("'file_path' is required.")
        if not stt_entity_id: raise ServiceValidationError("'entity_id' is required.")
        
        # 1. Транскодируем из файла
        audio_data = await async_transcode_from_path(file_path)
        # 2. Распознаем
        return await async_process_audio_data(hass, stt_entity_id, language, audio_data)

    hass.services.async_register(
        DOMAIN, "recognize_file", handle_recognize_file_service,
        supports_response=SupportsResponse.ONLY
    )
    
    # Action send_reply
    async def handle_send_reply_service(call: ServiceCall):
        chat_id = call.data.get("chat_id")
        message = call.data.get("message")
        if not chat_id: raise ServiceValidationError("'chat_id' is required.")
        if not message: raise ServiceValidationError("'message' is required.")
        bot_manager = hass.data[DOMAIN][entry.entry_id]
        await bot_manager.async_send_message(str(chat_id), str(message))

    hass.services.async_register(
        DOMAIN, "send_reply", handle_send_reply_service
    )
    
    bot_manager = TelegramBotManager(hass, entry)
    hass.data[DOMAIN][entry.entry_id] = bot_manager
    await bot_manager.start_bot_if_enabled()
    
    entry.async_on_unload(entry.add_update_listener(update_listener))
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.services.async_remove(DOMAIN, "recognize_file")
    hass.services.async_remove(DOMAIN, "send_reply")
    bot_manager = hass.data[DOMAIN].pop(entry.entry_id)
    await bot_manager.stop_bot()
    return True

async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    await hass.config_entries.async_reload(entry.entry_id)


class TelegramBotManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self.telegram_app: Application | None = None

    async def start_bot_if_enabled(self):
        if not self.entry.options.get(CONF_TELEGRAM_ENABLED): return
        token = self.entry.options.get(CONF_TELEGRAM_BOT_TOKEN)
        if not token: _LOGGER.error("Telegram bot enabled, but Bot Token is not configured."); return
        _LOGGER.info("Starting Telegram bot...")
        def build_app(): return Application.builder().token(token).build()
        self.telegram_app = await self.hass.async_add_executor_job(build_app)
        self.telegram_app.add_handler(MessageHandler(filters.VOICE, self.handle_voice_message))
        await self.telegram_app.initialize()
        await self.telegram_app.start()
        if self.telegram_app.updater:
            await self.telegram_app.updater.start_polling()
            _LOGGER.info("Telegram bot started and polling for updates.")

    async def stop_bot(self):
        if not self.telegram_app: return
        _LOGGER.info("Stopping Telegram bot...")
        try:
            if self.telegram_app.updater and self.telegram_app.updater.running: await self.telegram_app.updater.stop()
            if self.telegram_app.running: await self.telegram_app.stop()
            await self.telegram_app.shutdown()
            _LOGGER.info("Telegram bot stopped successfully.")
        except Exception as e: _LOGGER.error("Error while stopping telegram bot: %s", e)
        finally: self.telegram_app = None

    async def async_send_message(self, chat_id: str, text: str):
        if not self.telegram_app or not self.telegram_app.bot:
            _LOGGER.error("Telegram bot is not available to send a message.")
            return
        try:
            _LOGGER.debug("Sending message to chat_id %s: %s", chat_id, text)
            await self.telegram_app.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            _LOGGER.error("Failed to send Telegram message to chat_id %s: %s", chat_id, e)


    async def handle_voice_message(self, update: Update, context: CallbackContext):
        """Handles incoming voice messages from Telegram without disk operations."""
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
            voice_file = await update.message.voice.get_file()
            # 1. Скачиваем файл в память
            ogg_data = await voice_file.download_as_bytearray()
            
            # 2. Транскодируем его из памяти в память
            audio_data = await async_transcode_from_bytes(bytes(ogg_data))

            # 3. Распознаем
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

        except Exception as e:
            _LOGGER.error("Error processing voice message: %s", e, exc_info=True)
            if should_send_reply:
                await update.message.reply_text(f"Произошла ошибка: {e}")        

async def async_process_audio_data(
    hass: HomeAssistant, stt_entity_id: str, language: str | None, audio_data: bytes
) -> dict[str, Any]:
    """Processes raw PCM audio data using an STT provider."""
    stt_provider = stt.async_get_speech_to_text_entity(hass, stt_entity_id)
    if stt_provider is None: raise ServiceValidationError(f"STT provider '{stt_entity_id}' not found.")
    
    target_language = language or hass.config.language
    supported_languages = stt_provider.supported_languages
    if not language_util.matches(target_language, supported_languages):
         if language is None and supported_languages:
             _LOGGER.warning("Language '%s' not supported, falling back to first available: %s", target_language, supported_languages[0])
             target_language = supported_languages[0]
         else: raise ServiceValidationError(f"Language '{target_language}' is not supported by {stt_entity_id}.")

    metadata = stt.SpeechMetadata(
        language=target_language, format=stt.AudioFormats.WAV, codec=stt.AudioCodecs.PCM,
        bit_rate=stt.AudioBitRates.BITRATE_16, sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
        channel=stt.AudioChannels.CHANNEL_MONO
    )
    
    try:
        audio_stream = _async_stream_from_bytes(audio_data)
        _LOGGER.info("Starting recognition with '%s'...", stt_entity_id)
        result = await stt_provider.internal_async_process_audio_stream(metadata, audio_stream)
        
        if result.result == stt.SpeechResultState.SUCCESS:
            _LOGGER.info("Recognition successful! Text: '%s'", result.text)
            return {"text": result.text}
        
        raise ServiceValidationError(f"Recognition failed. Result state: {result.result}")

    except Exception as e:
        _LOGGER.error("An unexpected error during STT processing: %s", e, exc_info=True)
        raise ServiceValidationError("An unexpected error occurred during STT processing.")