"""Sensor platform for EV Charging Manager — session metrics."""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CAR_STATUS_ENTITY, DOMAIN, SIGNAL_SESSION_UPDATE, SessionEngineState
from .stats_sensor import create_stats_sensors

_LOGGER = logging.getLogger(__name__)


class _WindowAttribute(TypedDict):
    """One entry in ChargingDurationSensor.extra_state_attributes['windows']."""

    index: int
    started_at: str  # ISO-8601 UTC
    ended_at: str | None
    duration_s: int
    energy_kwh: float


class _ChargingDurationAttributes(TypedDict):
    """Shape of ChargingDurationSensor.extra_state_attributes."""

    window_count: int
    current_window_open: bool
    windows: list[_WindowAttribute]


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
        # PR-22: last-session duration sensors (US2 / T034)
        LastSessionChargingDurationSensor(hass, entry),
        LastSessionConnectionDurationSensor(hass, entry),
        # PR-23: live charging duration sensor (US4 / T036)
        ChargingDurationSensor(hass, entry),
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
        """Return current engine state.

        For PlugAnchoredSessionEngine (goe_gemini profile), returns the fine-grained
        sub-state: idle / waiting / charging / charged (per FR-013–FR-017, T041).
        For the legacy SessionEngine, returns the raw state machine value.
        Falls back to 'idle' if engine is missing.
        """
        engine = self._engine()
        if engine is None:
            return SessionEngineState.IDLE
        # PlugAnchoredSessionEngine exposes get_status_sub_state() (PR-22 / T041)
        if hasattr(engine, "get_status_sub_state"):
            return engine.get_status_sub_state()
        return engine.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return diagnostic attributes: last unknown reason, timestamp, charger connectivity.

        PR-22 (T035): adds current_session_id, current_charging_window_count,
        current_charging_duration_s, current_connection_duration_s for active sessions.
        """
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

        # PR-22: live active session attributes (read from PlugAnchoredSessionEngine only)
        current_session_id: str | None = None
        current_charging_window_count: int = 0
        current_charging_duration_s: int = 0
        current_connection_duration_s: int = 0
        if engine is not None:
            active = engine.active_session
            if active is not None:
                current_session_id = active.id
                current_charging_window_count = active.charging_window_count or 0
                current_charging_duration_s = active.charging_duration_s or 0
                # Live connection duration from connected_at
                if active.connected_at or active.started_at:
                    from datetime import datetime

                    from homeassistant.util import dt as dt_util

                    ts_str = active.connected_at or active.started_at
                    try:
                        connected = datetime.fromisoformat(ts_str)
                        current_connection_duration_s = int(
                            (dt_util.utcnow() - connected).total_seconds()
                        )
                    except (ValueError, TypeError):
                        pass

        return {
            "last_unknown_reason": engine.last_unknown_reason if engine else None,
            "last_unknown_at": engine.last_unknown_at if engine else None,
            "charger_connected": charger_connected,
            "last_session_user": engine.last_session_user if engine else None,
            "last_session_rfid_index": engine.last_session_rfid_index if engine else None,
            # PR-22 live active-session attributes (T035)
            "current_session_id": current_session_id,
            "current_charging_window_count": current_charging_window_count,
            "current_charging_duration_s": current_charging_duration_s,
            "current_connection_duration_s": current_connection_duration_s,
        }


# ---------------------------------------------------------------------------
# PR-23 (T035/US4): Live charging duration sensor
# ---------------------------------------------------------------------------


class ChargingDurationSensor(_SessionSensorBase):
    """Live charging duration sensor showing accumulated window time as HH:MM:SS.

    Displays the sum of all closed window durations plus the elapsed time of
    the currently open window (if any). Freezes between windows when no window
    is open. Updates via the dispatcher signal from SessionEngine (FR-022).

    Mirrors SessionDurationSensor's HH:MM:SS format (see SessionDurationSensor.native_value).
    Exposes per-window detail in extra_state_attributes.
    """

    _attr_icon = "mdi:battery-clock"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_charging_duration"
        self._attr_translation_key = "charging_duration"

    @property
    def native_value(self) -> str | None:
        """Return the live charging duration as HH:MM:SS, or None when no session.

        Computation (IC-5):
          total_closed_s + (utcnow() - open_window.start_at).total_seconds() if open else 0
        Formatted as zero-padded HH:MM:SS for parity with SessionDurationSensor.
        """
        engine = self._engine()
        if engine is None or not self._is_tracking():
            return None

        from homeassistant.util import dt as dt_util

        tracker = getattr(engine, "_window_tracker", None)
        if tracker is None:
            return None

        now = dt_util.utcnow()
        total_s = tracker.total_charging_duration_s(now)

        hours = total_s // 3600
        minutes = (total_s % 3600) // 60
        seconds = total_s % 60
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    @property
    def extra_state_attributes(self) -> _ChargingDurationAttributes | dict[str, Any]:
        """Return per-window detail for the active session.

        Returns {} when no session is active (FR-021).

        Schema per contracts/sensor-attributes.md:
            window_count: int         — session.charging_window_count
            current_window_open: bool — tracker.is_open()
            windows: list             — index/started_at/ended_at/duration_s/energy_kwh
        """
        engine = self._engine()
        if engine is None or not self._is_tracking():
            return {}

        session = self._active_session()
        if session is None:
            return {}

        tracker = getattr(engine, "_window_tracker", None)
        if tracker is None:
            return {}

        from homeassistant.util import dt as dt_util

        now = dt_util.utcnow()

        windows_list: list[_WindowAttribute] = [
            _WindowAttribute(
                index=idx,
                started_at=w.start_at.isoformat(),
                # ended_at: ISO string for closed windows, None for the open window
                ended_at=w.end_at.isoformat() if w.end_at is not None else None,
                duration_s=w.duration_s(now),
                energy_kwh=round(w.energy_kwh(), 3),
            )
            for idx, w in enumerate(tracker.windows_for_attributes(), start=1)
        ]

        return _ChargingDurationAttributes(
            window_count=session.charging_window_count,
            current_window_open=tracker.is_open(),
            windows=windows_list,
        )


# ---------------------------------------------------------------------------
# PR-22 (T034): Last-session duration sensors
# ---------------------------------------------------------------------------


class _LastSessionDurationBase(_SessionSensorBase):
    """Base for last-session duration sensors.

    Reports the duration from the most-recently finalized session.
    Availability: unavailable until at least one session has been completed.
    Subscribes to SIGNAL_SESSION_UPDATE (same dispatcher as all other sensors).
    """

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def _session_store(self):
        """Return the SessionStore for this entry."""
        return self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("session_store")

    def _last_completed_session(self) -> dict | None:
        """Return the most recently finalized session dict, or None."""
        store = self._session_store()
        if store is None:
            return None
        sessions = store.sessions
        return sessions[-1] if sessions else None

    @property
    def available(self) -> bool:
        """Unavailable until at least one session has been completed."""
        return self._last_completed_session() is not None


class LastSessionChargingDurationSensor(_LastSessionDurationBase):
    """Reports the charging duration of the last completed session (seconds).

    Per contracts/ha-entities.md: state = charging_duration_s from last session.
    Attributes: started_at (= charging_started_at), ended_at (= charging_ended_at),
    window_count, user_name, session_id.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_last_session_charging_duration"
        self._attr_translation_key = "last_session_charging_duration"

    @property
    def native_value(self) -> int | None:
        """Return charging_duration_s of the last completed session."""
        session = self._last_completed_session()
        if session is None:
            return None
        return session.get("charging_duration_s") or 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return charging-window details from the last session."""
        session = self._last_completed_session()
        if session is None:
            return {}
        return {
            "started_at": session.get("charging_started_at"),
            "ended_at": session.get("charging_ended_at"),
            "window_count": session.get("charging_window_count", 0),
            "user_name": session.get("user_name"),
            "session_id": session.get("id"),
        }


class LastSessionConnectionDurationSensor(_LastSessionDurationBase):
    """Reports the total connection duration of the last completed session (seconds).

    Per contracts/ha-entities.md: state = connection_duration_s from last session.
    Attributes: connected_at, disconnected_at, user_name, session_id.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_last_session_connection_duration"
        self._attr_translation_key = "last_session_connection_duration"

    @property
    def native_value(self) -> int | None:
        """Return connection_duration_s of the last completed session."""
        session = self._last_completed_session()
        if session is None:
            return None
        return session.get("connection_duration_s") or 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return connection-window details from the last session."""
        session = self._last_completed_session()
        if session is None:
            return {}
        return {
            "connected_at": session.get("connected_at") or session.get("started_at"),
            "disconnected_at": (session.get("disconnected_at") or session.get("ended_at")),
            "user_name": session.get("user_name"),
            "session_id": session.get("id"),
        }
