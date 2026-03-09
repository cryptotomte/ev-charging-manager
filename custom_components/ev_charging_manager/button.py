"""Button platform for EV Charging Manager — diagnostic action buttons."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .debug_logger import DebugLogger

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EV Charging Manager button entities from a config entry."""
    debug_logger: DebugLogger = hass.data[DOMAIN][entry.entry_id]["debug_logger"]

    async_add_entities([ClearDebugLogButton(entry, debug_logger)])


class ClearDebugLogButton(ButtonEntity):
    """Button that truncates the debug log file on press.

    Appears under Diagnostics on the integration device page.
    The button is always available regardless of whether debug logging is enabled.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "clear_debug_log"

    def __init__(self, entry: ConfigEntry, debug_logger: DebugLogger) -> None:
        """Initialize the clear-debug-log button."""
        self._entry = entry
        self._debug_logger = debug_logger
        self._attr_unique_id = f"{entry.entry_id}_clear_debug_log"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
        )

    async def async_press(self) -> None:
        """Handle button press — truncate the debug log file."""
        _LOGGER.debug("Clear debug log button pressed for entry %s", self._entry.entry_id)
        await self.hass.async_add_executor_job(self._debug_logger.clear)
