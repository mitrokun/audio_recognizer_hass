"""
Custom integration to provide a service for recognizing audio files
using one of the system's Speech-to-Text (STT) providers.
"""
import asyncio
import logging
from collections.abc import AsyncIterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.components import stt
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import language as language_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def _async_stream_audio_from_file(file_path: str) -> AsyncIterable[bytes]:
    """Stream audio from a file, skipping the WAV header."""
    try:
        with open(file_path, "rb") as audio_file:
            # WAV files have a header (usually 44 bytes) that contains metadata.
            # STT engines expect a raw audio stream (raw PCM), so we skip the header.
            audio_file.seek(44)
            while chunk := audio_file.read(1024):
                yield chunk
                # Give the Python event loop a chance to breathe so we don't block it.
                await asyncio.sleep(0)
    except FileNotFoundError:
        # Use ServiceValidationError for errors that the user should see in the UI.
        raise ServiceValidationError(f"Audio file not found at path: {file_path}")
    except Exception as e:
        raise ServiceValidationError(f"Error reading audio file '{file_path}': {e}")


class ServiceCallData:
    """A class to store and validate data from the service call."""
    def __init__(self, data_call: ServiceCall):
        """Initialize the service call data."""
        self.stt_entity_id: str | None = data_call.data.get("entity_id")
        self.file_path: str | None = data_call.data.get("file_path")
        self.language: str | None = data_call.data.get("language")
        
        self.validate()

    def validate(self) -> None:
        """Validate the service call data."""
        if not self.stt_entity_id:
            raise ServiceValidationError("Service call failed: 'entity_id' is required.")
        if not self.file_path:
            raise ServiceValidationError("Service call failed: 'file_path' is required.")


async def async_recognize_and_get_response(hass: HomeAssistant, service_data: ServiceCallData) -> dict:
    """
    Performs the STT recognition and returns a dictionary with the result.
    This function contains the core logic.
    """
    stt_provider = stt.async_get_speech_to_text_entity(hass, service_data.stt_entity_id)
    if stt_provider is None:
        raise ServiceValidationError(f"STT provider with entity_id '{service_data.stt_entity_id}' not found.")

    # --- Language selection logic ---
    target_language = service_data.language
    supported_languages = stt_provider.supported_languages

    # 1. If user did not specify a language, try to use the system default.
    if not target_language:
        target_language = hass.config.language
        _LOGGER.debug("Language not specified, using system default: %s", target_language)

    # 2. Check if the provider supports the target language.
    # language_util.matches() is a smart function that understands variants (e.g., ru, ru-RU).
    if not language_util.matches(target_language, supported_languages):
        error_msg = (
            f"Language '{target_language}' is not supported by {service_data.stt_entity_id}. "
            f"Supported languages are: {', '.join(supported_languages)}"
        )
        
        # If the language was the system default, it might just not be supported.
        # In this case, fall back to the first available language from the provider.
        if service_data.language is None and supported_languages:
             _LOGGER.warning(error_msg)
             target_language = supported_languages[0]
             _LOGGER.warning("Falling back to the first supported language: %s", target_language)
        else:
             # If the user explicitly requested an unsupported language, it's a clear error.
             raise ServiceValidationError(error_msg)

    _LOGGER.info("Using language: %s for recognition.", target_language)
    # --- End of language selection logic ---
    
    metadata = stt.SpeechMetadata(
        language=target_language, # <-- Use the determined language
        format=stt.AudioFormats.WAV, 
        codec=stt.AudioCodecs.PCM,
        bit_rate=stt.AudioBitRates.BITRATE_16, 
        sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
        channel=stt.AudioChannels.CHANNEL_MONO
    )
    
    try:
        audio_stream = _async_stream_audio_from_file(service_data.file_path)
        _LOGGER.info("Starting recognition for '%s' with '%s'...", service_data.file_path, service_data.stt_entity_id)
        result = await stt_provider.internal_async_process_audio_stream(metadata, audio_stream)

        if result.result == stt.SpeechResultState.SUCCESS:
            _LOGGER.info("Recognition successful! Text: '%s'", result.text)
            return {"text": result.text}
        
        _LOGGER.warning("Recognition failed. Result state: %s", result.result)
        raise ServiceValidationError(f"Recognition failed. Result state: {result.result}")

    except ServiceValidationError as e:
        # Re-raise validation errors so they are visible in the UI.
        raise e
    except Exception as e:
        # Log other, unexpected errors and return a generic error message.
        _LOGGER.error("An unexpected error occurred during STT processing: %s", e, exc_info=True)
        raise ServiceValidationError("An unexpected error occurred during STT processing.")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Audio Recognizer from a config entry."""

    async def handle_recognize_file_service(call: ServiceCall) -> dict:
        """
        Handles the service call, validates input, and returns the result from the core function.
        Home Assistant will automatically handle the returned dictionary as a service response.
        """
        service_data = ServiceCallData(call)
        return await async_recognize_and_get_response(hass, service_data)

    hass.services.async_register(
        DOMAIN, 
        "recognize_file", 
        handle_recognize_file_service,
        supports_response=SupportsResponse.ONLY
    )
    
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.services.async_remove(DOMAIN, "recognize_file")
    return True