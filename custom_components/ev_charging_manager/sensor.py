"""Sensor platform for EV Charging Manager — session metrics."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CAR_STATUS_ENTITY, DOMAIN, SIGNAL_SESSION_UPDATE, SessionEngineState
from .stats_sensor import create_stats_sensors

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EV Charging Manager sensor entities from a config entry."""
    # Session metric sensors (PR-03)
    entities: list[Any] = [
        CurrentUserSensor(hass, entry),
        CurrentVehicleSensor(hass, entry),
        SessionEnergySensor(hass, entry),
        SessionDurationSensor(hass, entry),
        SessionCostSensor(hass, entry),
        SessionChargePriceSensor(hass, entry),
        SessionPowerSensor(hass, entry),
        SessionSocAddedSensor(hass, entry),
        StatusSensor(hass, entry),
    ]

    # Per-user statistics sensors (PR-04, T011)
    config_store = hass.data[DOMAIN][entry.entry_id]["config_store"]
    user_names: list[str] = [u["name"] for u in config_store.data.get("users", [])]
    # "Unknown" user always exists in stats (FR-007)
    if "Unknown" not in user_names:
        user_names.append("Unknown")
    entities.extend(create_stats_sensors(hass, entry, user_names))

    async_add_entities(entities)


class _SessionSensorBase(SensorEntity):
    """Base class for all session sensor entities.

    All sensors are push-based (no polling) and subscribe to the dispatcher
    signal from SessionEngine. Device is tied to the config entry device.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        self._hass = hass
        self._entry = entry
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

    def _engine(self):
        """Return the SessionEngine for this entry."""
        return self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("session_engine")

    def _is_tracking(self) -> bool:
        """Return True if engine is in TRACKING state."""
        engine = self._engine()
        return engine is not None and engine.state == SessionEngineState.TRACKING

    def _active_session(self):
        """Return the active session or None."""
        engine = self._engine()
        if engine is None:
            return None
        return engine.active_session

    @property
    def available(self) -> bool:
        """Return True only when a session is active (TRACKING state)."""
        return self._is_tracking()


class CurrentUserSensor(_SessionSensorBase):
    """Shows the name of the user currently charging."""

    _attr_icon = "mdi:account"
    _attr_unique_id: str

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_current_user"
        self._attr_translation_key = "current_user"

    @property
    def native_value(self) -> str | None:
        session = self._active_session()
        if session is None:
            return None
        return session.user_name


class CurrentVehicleSensor(_SessionSensorBase):
    """Shows the vehicle being charged."""

    _attr_icon = "mdi:car-electric"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_current_vehicle"
        self._attr_translation_key = "current_vehicle"

    @property
    def native_value(self) -> str | None:
        session = self._active_session()
        if session is None:
            return None
        return session.vehicle_name  # None → unavailable for unknown users


class SessionEnergySensor(_SessionSensorBase):
    """Shows the energy consumed in the current session (kWh)."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "kWh"
    _attr_suggested_display_precision = 2

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_session_energy"
        self._attr_translation_key = "session_energy"

    @property
    def native_value(self) -> float | None:
        session = self._active_session()
        if session is None:
            return None
        return round(session.energy_kwh, 2)


class SessionDurationSensor(_SessionSensorBase):
    """Shows the duration of the current session as HH:MM:SS string.

    Uses string format (no device class) since HA DURATION requires a numeric unit.
    """

    _attr_icon = "mdi:timer-outline"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_session_duration"
        self._attr_translation_key = "session_duration"

    @property
    def native_value(self) -> str | None:
        session = self._active_session()
        if session is None:
            return None
        from datetime import datetime

        from homeassistant.util import dt as dt_util

        try:
            started = datetime.fromisoformat(session.started_at)
            elapsed = int((dt_util.utcnow() - started).total_seconds())
        except (ValueError, TypeError):
            elapsed = 0
        hours = elapsed // 3600
        minutes = (elapsed % 3600) // 60
        seconds = elapsed % 60
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class SessionCostSensor(_SessionSensorBase):
    """Shows the accumulated cost of the current session (kr)."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "kr"
    _attr_suggested_display_precision = 2

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_session_cost"
        self._attr_translation_key = "session_cost"

    @property
    def native_value(self) -> float | None:
        session = self._active_session()
        if session is None:
            return None
        return round(session.cost_total_kr, 2)


class SessionChargePriceSensor(_SessionSensorBase):
    """Shows the guest charge price during an active guest session (kr)."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "kr"
    _attr_suggested_display_precision = 2

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_session_charge_price"
        self._attr_translation_key = "session_charge_price"

    @property
    def available(self) -> bool:
        """Available only when TRACKING an active guest session."""
        if not self._is_tracking():
            return False
        session = self._active_session()
        return session is not None and session.charge_price_method is not None

    @property
    def native_value(self) -> float | None:
        """Return the current guest charge price, or None if unavailable."""
        session = self._active_session()
        if session is None or session.charge_price_total_kr is None:
            return None
        return round(session.charge_price_total_kr, 2)


class SessionPowerSensor(_SessionSensorBase):
    """Shows the current charging power (W)."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_session_power"
        self._attr_translation_key = "session_power"

    @property
    def native_value(self) -> float | None:
        engine = self._engine()
        if engine is None or not self._is_tracking():
            return None
        return round(engine._last_power_w, 0)


class SessionSocAddedSensor(_SessionSensorBase):
    """Shows the estimated SoC percentage added during this session."""

    _attr_native_unit_of_measurement = "%"
    _attr_suggested_display_precision = 1

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_session_soc_added"
        self._attr_translation_key = "session_soc_added"

    @property
    def available(self) -> bool:
        """Available only when tracking AND vehicle battery capacity is known."""
        session = self._active_session()
        if session is None:
            return False
        return session.vehicle_battery_kwh is not None

    @property
    def native_value(self) -> float | None:
        session = self._active_session()
        if session is None:
            return None
        if session.estimated_soc_added_pct is None:
            return None
        return round(session.estimated_soc_added_pct, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes including the is_estimate flag."""
        return {"is_estimate": True}


class StatusSensor(_SessionSensorBase):
    """Diagnostic sensor showing the engine state (idle/tracking/error).

    This sensor is NEVER unavailable — it always reports the current state.
    """

    _attr_icon = "mdi:state-machine"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_translation_key = "status"

    @property
    def available(self) -> bool:
        """Always available — status sensor never goes unavailable."""
        return True

    @property
    def native_value(self) -> str:
        """Return current engine state. Falls back to 'idle' if engine missing."""
        engine = self._engine()
        if engine is None:
            return SessionEngineState.IDLE
        return engine.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return diagnostic attributes: last unknown reason, timestamp, charger connectivity."""
        engine = self._engine()
        # charger_connected: True when car_status entity is reachable (not unavailable/unknown)
        car_entity = self._entry.data.get(CONF_CAR_STATUS_ENTITY)
        charger_connected: bool = False
        if car_entity:
            car_state = self._hass.states.get(car_entity)
            charger_connected = car_state is not None and car_state.state not in (
                "unavailable",
                "unknown",
            )
        return {
            "last_unknown_reason": engine.last_unknown_reason if engine else None,
            "last_unknown_at": engine.last_unknown_at if engine else None,
            "charger_connected": charger_connected,
        }
