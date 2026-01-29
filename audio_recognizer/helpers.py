"""Helper functions for the Audio Recognizer integration."""
import asyncio
import io
import logging
from collections.abc import AsyncIterable
from typing import Any

from homeassistant.components import stt
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import language as language_util

from .exceptions import NoAudioStreamError

_LOGGER = logging.getLogger(__name__)


async def async_transcode_from_bytes(source_data: bytes) -> bytes:
    """Transcodes an audio file FROM A BYTE ARRAY to raw PCM bytes."""
    command = ["ffmpeg", "-i", "-", "-vn", "-f", "wav", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", "-"]
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
    
    _LOGGER.warning("Transcoding from bytes resulted in empty audio data. The source likely has no audio stream.")
    raise NoAudioStreamError("The source media does not contain an audio stream.")


async def _async_stream_from_bytes(data: bytes, chunk_size: int = 4096) -> AsyncIterable[bytes]:
    buffer = io.BytesIO(data)
    while chunk := buffer.read(chunk_size):
        yield chunk
        await asyncio.sleep(0)


async def async_transcode_from_path(source_path: str) -> bytes:
    """Transcodes an audio file FROM A DISK PATH to raw PCM bytes."""
    command = ["ffmpeg", "-i", str(source_path), "-vn", "-f", "wav", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", "-"]
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
    
    _LOGGER.warning("Transcoding from path resulted in empty audio data. The source likely has no audio stream.")
    raise NoAudioStreamError("The source file does not contain an audio stream.")


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