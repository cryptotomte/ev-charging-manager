"""Sensor entity classes for per-user charging statistics (PR-04).

Entity classes: StatsBaseSensor, UserTotalEnergySensor, UserTotalCostSensor,
UserSessionCountSensor, UserAvgSessionEnergySensor, UserLastSessionSensor,
GuestLastEnergySensor, GuestLastChargePriceSensor, GuestTotalEnergySensor,
GuestTotalCostSensor.

All sensors are registered through the existing sensor.py platform's
async_setup_entry (plan.md D3) — this file contains entity class definitions only.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.util import slugify

from .const import DOMAIN, SIGNAL_STATS_UPDATE
from .stats_engine import GuestLastSession, StatsEngine, UserStats

_LOGGER = logging.getLogger(__name__)


class StatsBaseSensor(SensorEntity):
    """Base class for all per-user statistics sensor entities.

    Push-based (no polling). Subscribes to SIGNAL_STATS_UPDATE dispatcher.
    Device is tied to the config entry's charger device.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the stats sensor."""
        self._hass = hass
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
        )

    @callback
    def _handle_update(self) -> None:
        """Handle dispatcher update from StatsEngine."""
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Subscribe to StatsEngine dispatcher signal."""
        signal = SIGNAL_STATS_UPDATE.format(self._entry.entry_id)
        self.async_on_remove(async_dispatcher_connect(self._hass, signal, self._handle_update))

    def _stats_engine(self) -> StatsEngine | None:
        """Return the StatsEngine for this entry, or None if not loaded."""
        return self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("stats_engine")

    def _get_user_stats(self, user_name: str) -> UserStats | None:
        """Return UserStats for the given user, or None if engine not loaded."""
        engine = self._stats_engine()
        if engine is None:
            return None
        return engine.user_stats.get(user_name)

    def _get_guest_last(self) -> GuestLastSession | None:
        """Return the last guest session data, or None if engine not loaded."""
        engine = self._stats_engine()
        if engine is None:
            return None
        return engine.guest_last


# ---------------------------------------------------------------------------
# Per-user sensors (T010 + T016 monthly attributes)
# ---------------------------------------------------------------------------


class UserTotalEnergySensor(StatsBaseSensor):
    """Lifetime accumulated energy for one user (kWh).

    Also exposes current_month and previous_month statistics as attributes (T016).
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "kWh"
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        user_name: str,
        user_slug: str,
    ) -> None:
        """Initialize."""
        super().__init__(hass, entry)
        self._user_name = user_name
        self._attr_unique_id = f"{entry.entry_id}_{user_slug}_total_energy"
        self._attr_translation_key = "user_total_energy"
        self._attr_translation_placeholders = {"user": user_name}

    @property
    def native_value(self) -> float:
        """Return lifetime total energy (0.0 when no sessions)."""
        stats = self._get_user_stats(self._user_name)
        if stats is None:
            return 0.0
        return round(stats.total_energy_kwh, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return monthly breakdown attributes (T016, FR-004)."""
        stats = self._get_user_stats(self._user_name)
        if stats is None:
            return {}
        return {
            "current_month_kwh": round(stats.current_month.energy_kwh, 3),
            "current_month_cost": round(stats.current_month.cost_kr, 2),
            "current_month_sessions": stats.current_month.sessions,
            "previous_month_kwh": round(stats.previous_month.energy_kwh, 3),
            "previous_month_cost": round(stats.previous_month.cost_kr, 2),
            "previous_month_sessions": stats.previous_month.sessions,
        }


class UserTotalCostSensor(StatsBaseSensor):
    """Lifetime accumulated cost for one user (kr)."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "kr"
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        user_name: str,
        user_slug: str,
    ) -> None:
        """Initialize."""
        super().__init__(hass, entry)
        self._user_name = user_name
        self._attr_unique_id = f"{entry.entry_id}_{user_slug}_total_cost"
        self._attr_translation_key = "user_total_cost"
        self._attr_translation_placeholders = {"user": user_name}

    @property
    def native_value(self) -> float:
        """Return lifetime total cost (0.0 when no sessions)."""
        stats = self._get_user_stats(self._user_name)
        if stats is None:
            return 0.0
        return round(stats.total_cost_kr, 2)


class UserSessionCountSensor(StatsBaseSensor):
    """Total number of completed sessions for one user."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:counter"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        user_name: str,
        user_slug: str,
    ) -> None:
        """Initialize."""
        super().__init__(hass, entry)
        self._user_name = user_name
        self._attr_unique_id = f"{entry.entry_id}_{user_slug}_session_count"
        self._attr_translation_key = "user_session_count"
        self._attr_translation_placeholders = {"user": user_name}

    @property
    def native_value(self) -> int:
        """Return lifetime session count (0 when no sessions)."""
        stats = self._get_user_stats(self._user_name)
        if stats is None:
            return 0
        return stats.session_count


class UserAvgSessionEnergySensor(StatsBaseSensor):
    """Average energy per session for one user (kWh).

    Unavailable when no sessions have been recorded (sensor-contracts.md).
    """

    _attr_native_unit_of_measurement = "kWh"
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        user_name: str,
        user_slug: str,
    ) -> None:
        """Initialize."""
        super().__init__(hass, entry)
        self._user_name = user_name
        self._attr_unique_id = f"{entry.entry_id}_{user_slug}_avg_session_energy"
        self._attr_translation_key = "user_avg_session_energy"
        self._attr_translation_placeholders = {"user": user_name}

    @property
    def available(self) -> bool:
        """Available only after at least one session."""
        stats = self._get_user_stats(self._user_name)
        return stats is not None and stats.session_count > 0

    @property
    def native_value(self) -> float | None:
        """Return avg energy; None if no sessions."""
        stats = self._get_user_stats(self._user_name)
        if stats is None or stats.session_count == 0:
            return None
        return round(stats.total_energy_kwh / stats.session_count, 2)


class UserLastSessionSensor(StatsBaseSensor):
    """Timestamp of the user's most recent completed session.

    Unavailable when no sessions have been recorded (sensor-contracts.md).
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        user_name: str,
        user_slug: str,
    ) -> None:
        """Initialize."""
        super().__init__(hass, entry)
        self._user_name = user_name
        self._attr_unique_id = f"{entry.entry_id}_{user_slug}_last_session"
        self._attr_translation_key = "user_last_session"
        self._attr_translation_placeholders = {"user": user_name}

    @property
    def available(self) -> bool:
        """Available only after at least one session."""
        stats = self._get_user_stats(self._user_name)
        return stats is not None and stats.session_count > 0

    @property
    def native_value(self) -> datetime | None:
        """Return last session timestamp as datetime; None if no sessions."""
        stats = self._get_user_stats(self._user_name)
        if stats is None or not stats.last_session_at:
            return None
        try:
            return datetime.fromisoformat(stats.last_session_at)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "Could not parse last_session_at for user '%s': %s",
                self._user_name,
                stats.last_session_at,
            )
            return None


# ---------------------------------------------------------------------------
# Guest-specific sensors (T022)
# ---------------------------------------------------------------------------


class GuestLastEnergySensor(StatsBaseSensor):
    """Energy from the most recent guest charging session (kWh).

    Retains its value until overwritten by the next guest session (FR-009).
    Unavailable when no guest session has ever completed.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = "kWh"
    _attr_suggested_display_precision = 2

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_guest_last_energy"
        self._attr_translation_key = "guest_last_energy"

    @property
    def available(self) -> bool:
        """Available only after at least one guest session has completed."""
        return self._get_guest_last() is not None

    @property
    def native_value(self) -> float | None:
        """Return last guest energy, or None if no guest session."""
        guest = self._get_guest_last()
        if guest is None:
            return None
        return round(guest.energy_kwh, 2)


class GuestLastChargePriceSensor(StatsBaseSensor):
    """Charge price from the most recent guest session (kr).

    Retains its value until overwritten by the next guest session.
    Unavailable when no guest session has completed or guest had no pricing configured.
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "kr"
    _attr_suggested_display_precision = 2

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_guest_last_charge_price"
        self._attr_translation_key = "guest_last_charge_price"

    @property
    def available(self) -> bool:
        """Available only after a guest session with charge price has completed."""
        guest = self._get_guest_last()
        return guest is not None and guest.charge_price_kr is not None

    @property
    def native_value(self) -> float | None:
        """Return last guest charge price, or None if unavailable."""
        guest = self._get_guest_last()
        if guest is None or guest.charge_price_kr is None:
            return None
        return round(guest.charge_price_kr, 2)


class GuestTotalEnergySensor(StatsBaseSensor):
    """Lifetime accumulated energy across all guest users (kWh).

    Sums total_energy_kwh for all UserStats where user_type == "guest".
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "kWh"
    _attr_suggested_display_precision = 2

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_guest_total_energy"
        self._attr_translation_key = "guest_total_energy"

    @property
    def native_value(self) -> float:
        """Return lifetime total energy across all guest users."""
        engine = self._stats_engine()
        if engine is None:
            return 0.0
        total = sum(
            s.total_energy_kwh for s in engine.user_stats.values() if s.user_type == "guest"
        )
        return round(total, 2)


class GuestTotalCostSensor(StatsBaseSensor):
    """Lifetime accumulated cost across all guest users (kr).

    Sums total_cost_kr for all UserStats where user_type == "guest".
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "kr"
    _attr_suggested_display_precision = 2

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_guest_total_cost"
        self._attr_translation_key = "guest_total_cost"

    @property
    def native_value(self) -> float:
        """Return lifetime total cost across all guest users."""
        engine = self._stats_engine()
        if engine is None:
            return 0.0
        total = sum(s.total_cost_kr for s in engine.user_stats.values() if s.user_type == "guest")
        return round(total, 2)


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


def create_stats_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry,
    user_names: list[str],
) -> list[StatsBaseSensor]:
    """Create all statistics sensor entities for the given users.

    Creates 5 sensors per user (total_energy, total_cost, session_count,
    avg_session_energy, last_session) plus 4 guest sensors.

    user_names should include "Unknown" — callers are responsible for ensuring it.
    """
    entities: list[StatsBaseSensor] = []

    for user_name in user_names:
        user_slug = slugify(user_name)
        entities.extend(
            [
                UserTotalEnergySensor(hass, entry, user_name, user_slug),
                UserTotalCostSensor(hass, entry, user_name, user_slug),
                UserSessionCountSensor(hass, entry, user_name, user_slug),
                UserAvgSessionEnergySensor(hass, entry, user_name, user_slug),
                UserLastSessionSensor(hass, entry, user_name, user_slug),
            ]
        )

    # Guest sensors are always created (not per-user)
    entities.extend(
        [
            GuestLastEnergySensor(hass, entry),
            GuestLastChargePriceSensor(hass, entry),
            GuestTotalEnergySensor(hass, entry),
            GuestTotalCostSensor(hass, entry),
        ]
    )

    return entities
