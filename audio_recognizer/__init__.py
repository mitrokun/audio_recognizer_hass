"""
Custom integration to provide a service for recognizing audio files
using one of the system's Speech-to-Text (STT) providers.
This version uses in-memory transcoding and non-blocking file reads.
"""
import asyncio
import logging
import io
from collections.abc import AsyncIterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.components import stt
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import language as language_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _read_wav_file_sync(source_path: str) -> bytes:
    """
    Reads a WAV file in a blocking manner. To be run in executor.
    Returns raw PCM data.
    """
    try:
        with open(source_path, "rb") as f:
            f.seek(44)  # Skip WAV header
            return f.read()
    except FileNotFoundError:
        raise
    except Exception as e:
        _LOGGER.error("Error reading WAV file '%s': %s", source_path, e)
        raise


async def async_transcode_to_bytes(source_path: str) -> bytes:
    _LOGGER.debug("Transcoding %s to in-memory WAV format", source_path)
    command = [
        "ffmpeg", "-i", source_path, "-f", "wav", "-acodec", "pcm_s16le",
        "-ar", "16000", "-ac", "1", "-",
    ]
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout_data, stderr_data = await process.communicate()
    if process.returncode != 0:
        _LOGGER.error("FFmpeg failed with code %s: %s", process.returncode, stderr_data.decode(errors='ignore'))
        raise ServiceValidationError(f"Failed to transcode audio file: {source_path}")
    wav_header_size = 44
    if len(stdout_data) > wav_header_size:
        return stdout_data[wav_header_size:]
    raise ServiceValidationError("Transcoding resulted in empty audio data.")


async def _async_stream_from_bytes(data: bytes, chunk_size: int = 4096) -> AsyncIterable[bytes]:
    buffer = io.BytesIO(data)
    while chunk := buffer.read(chunk_size):
        yield chunk
        await asyncio.sleep(0)


class ServiceCallData:
    def __init__(self, data_call: ServiceCall):
        self.stt_entity_id: str | None = data_call.data.get("entity_id")
        self.file_path: str | None = data_call.data.get("file_path")
        self.language: str | None = data_call.data.get("language")
        self.validate()

    def validate(self) -> None:
        if not self.stt_entity_id: raise ServiceValidationError("'entity_id' is required.")
        if not self.file_path: raise ServiceValidationError("'file_path' is required.")


async def async_recognize_and_get_response(hass: HomeAssistant, service_data: ServiceCallData) -> dict:
    source_path = service_data.file_path
    
    try:
        if not source_path.lower().endswith(".wav"):
            audio_data = await async_transcode_to_bytes(source_path)
        else:
            _LOGGER.debug("Reading WAV file %s in executor", source_path)
            audio_data = await hass.async_add_executor_job(
                _read_wav_file_sync, source_path
            )

    except FileNotFoundError:
        raise ServiceValidationError(f"Audio file not found at path: {source_path}")
    except Exception as e:
        raise ServiceValidationError(f"Error processing audio file '{source_path}': {e}")


    stt_provider = stt.async_get_speech_to_text_entity(hass, service_data.stt_entity_id)
    if stt_provider is None:
        raise ServiceValidationError(f"STT provider '{service_data.stt_entity_id}' not found.")
    
    target_language = service_data.language or hass.config.language
    supported_languages = stt_provider.supported_languages
    if not language_util.matches(target_language, supported_languages):
         if service_data.language is None and supported_languages:
             target_language = supported_languages[0]
         else:
             raise ServiceValidationError(f"Language '{target_language}' is not supported.")
    
    _LOGGER.info("Using language: %s for recognition.", target_language)
    
    metadata = stt.SpeechMetadata(
        language=target_language, format=stt.AudioFormats.WAV, codec=stt.AudioCodecs.PCM,
        bit_rate=stt.AudioBitRates.BITRATE_16, sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
        channel=stt.AudioChannels.CHANNEL_MONO
    )
    
    try:
        audio_stream = _async_stream_from_bytes(audio_data)
        _LOGGER.info("Starting recognition for file '%s'...", source_path)
        result = await stt_provider.internal_async_process_audio_stream(metadata, audio_stream)

        if result.result == stt.SpeechResultState.SUCCESS:
            _LOGGER.info("Recognition successful! Text: '%s'", result.text)
            return {"text": result.text}
        
        raise ServiceValidationError(f"Recognition failed. Result state: {result.result}")

    except ServiceValidationError as e:
        raise e
    except Exception as e:
        _LOGGER.error("An unexpected error occurred during STT processing: %s", e, exc_info=True)
        raise ServiceValidationError("An unexpected error occurred during STT processing.")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    async def handle_recognize_file_service(call: ServiceCall) -> dict:
        service_data = ServiceCallData(call)
        return await async_recognize_and_get_response(hass, service_data)

    hass.services.async_register(
        DOMAIN, "recognize_file", handle_recognize_file_service,
        supports_response=SupportsResponse.ONLY
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.services.async_remove(DOMAIN, "recognize_file")
    return True
