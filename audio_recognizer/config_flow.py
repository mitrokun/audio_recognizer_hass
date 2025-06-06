"""Config flow for Audio Recognizer."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigFlow
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class AudioRecognizerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Audio Recognizer."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        # Убедимся, что можно добавить только один экземпляр этой интеграции
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            # Пользователь нажал "Отправить" на форме.
            # Так как у нас нет полей, просто создаем запись.
            return self.async_create_entry(title="Audio Recognizer", data={})

        # Показываем пользователю форму. В нашем случае она будет пустой,
        # так как нам не нужна никакая конфигурация от пользователя.
        # Home Assistant покажет кнопку "Отправить".
        return self.async_show_form(step_id="user")