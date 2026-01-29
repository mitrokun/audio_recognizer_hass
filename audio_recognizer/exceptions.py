"""Custom exceptions for the Audio Recognizer integration."""

from homeassistant.exceptions import HomeAssistantError


class NoAudioStreamError(HomeAssistantError):
    """Exception to indicate that no audio stream was found in the media file."""
    pass