"""
Custom integration to provide a service for recognizing audio files
and a Telegram bot interface for STT providers.
"""
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .helpers import async_process_audio_data, async_transcode_from_path
from .telegram import TelegramBotManager

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Audio Recognizer integration."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Audio Recognizer from a config entry."""

    async def handle_recognize_file_service(call: ServiceCall) -> dict[str, Any]:
        """Handle the recognize_file service call."""
        file_path = call.data.get("file_path")
        stt_entity_id = call.data.get("entity_id")
        language = call.data.get("language")
        if not file_path:
            raise ServiceValidationError("'file_path' is required.")
        if not stt_entity_id:
            raise ServiceValidationError("'entity_id' is required.")

        audio_data = await async_transcode_from_path(file_path)
        return await async_process_audio_data(hass, stt_entity_id, language, audio_data)

    hass.services.async_register(
        DOMAIN, "recognize_file", handle_recognize_file_service,
        supports_response=SupportsResponse.ONLY
    )

    async def handle_send_reply_service(call: ServiceCall):
        """Handle the send_reply service call."""
        chat_id = call.data.get("chat_id")
        message = call.data.get("message")
        if not chat_id:
            raise ServiceValidationError("'chat_id' is required.")
        if not message:
            raise ServiceValidationError("'message' is required.")
        
        bot_manager: TelegramBotManager = hass.data[DOMAIN][entry.entry_id]
        await bot_manager.async_send_message(str(chat_id), str(message))

    hass.services.async_register(
        DOMAIN, "send_reply", handle_send_reply_service
    )

    # Setup and start the Telegram bot
    bot_manager = TelegramBotManager(hass, entry)
    hass.data[DOMAIN][entry.entry_id] = bot_manager
    await bot_manager.start_bot_if_enabled()

    entry.async_on_unload(entry.add_update_listener(update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.services.async_remove(DOMAIN, "recognize_file")
    hass.services.async_remove(DOMAIN, "send_reply")

    bot_manager: TelegramBotManager = hass.data[DOMAIN].pop(entry.entry_id)
    await bot_manager.stop_bot()

    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)