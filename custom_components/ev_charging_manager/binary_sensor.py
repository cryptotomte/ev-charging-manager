"""Binary sensor platform for EV Charging Manager — charging active indicator."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_SESSION_UPDATE, SessionEngineState


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EV Charging Manager binary sensor entities from a config entry."""
    async_add_entities([ChargingBinarySensor(hass, entry)])


class ChargingBinarySensor(BinarySensorEntity):
    """Binary sensor indicating whether EV charging is actively in progress."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the charging binary sensor."""
        self._hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_charging"
        self._attr_translation_key = "charging"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
        )

    @callback
    def _handle_update(self) -> None:
        """Handle dispatcher update from SessionEngine."""
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Subscribe to SessionEngine dispatcher signal."""
        signal = SIGNAL_SESSION_UPDATE.format(self._entry.entry_id)
        self.async_on_remove(async_dispatcher_connect(self._hass, signal, self._handle_update))

    @property
    def is_on(self) -> bool:
        """Return True when a session is actively being tracked."""
        engine = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("session_engine")
        if engine is None:
            return False
        return engine.state == SessionEngineState.TRACKING
