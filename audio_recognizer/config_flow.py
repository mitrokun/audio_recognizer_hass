"""Config flow for Audio Recognizer."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithConfigEntry,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    CONF_TELEGRAM_ENABLED,
    CONF_TELEGRAM_BOT_TOKEN,
    CONF_TELEGRAM_CHAT_IDS,
    CONF_TELEGRAM_STT_ENTITY_ID
)


class AudioRecognizerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Audio Recognizer."""
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(title="Audio Recognizer", data={})

        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlowWithConfigEntry:
        """Get the options flow for this handler."""
        return AudioRecognizerOptionsFlow(config_entry)


class AudioRecognizerOptionsFlow(OptionsFlowWithConfigEntry):
    """Handle an options flow for Audio Recognizer."""


    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_TELEGRAM_ENABLED,
                    default=self.options.get(CONF_TELEGRAM_ENABLED, False),
                ): bool,
                vol.Optional(
                    CONF_TELEGRAM_BOT_TOKEN,
                    description={"suggested_value": self.options.get(CONF_TELEGRAM_BOT_TOKEN)},
                ): str,
                vol.Optional(
                    CONF_TELEGRAM_CHAT_IDS,
                    description={"suggested_value": self.options.get(CONF_TELEGRAM_CHAT_IDS)},
                ): str,
                vol.Optional(
                    CONF_TELEGRAM_STT_ENTITY_ID,
                    description={"suggested_value": self.options.get(CONF_TELEGRAM_STT_ENTITY_ID)},
                ): selector.selector({
                    "entity": {
                        "domain": "stt"
                    }
                }),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
