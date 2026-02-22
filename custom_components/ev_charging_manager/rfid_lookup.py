"""RFID lookup — resolve trx values to user/vehicle via ConfigStore data."""

from __future__ import annotations

import logging
from typing import Any

from .session import RfidResolution

_LOGGER = logging.getLogger(__name__)


class RfidLookup:
    """Resolve trx sensor values to user and vehicle information.

    Reads rfid_mappings, users, and vehicles from ConfigStore data.
    All lookups are pure in-memory operations (no HA calls).
    """

    def __init__(self, config_data: dict[str, Any]) -> None:
        """Initialize with ConfigStore data snapshot."""
        self._mappings: list[dict[str, Any]] = config_data.get("rfid_mappings", [])
        self._users: list[dict[str, Any]] = config_data.get("users", [])
        self._vehicles: list[dict[str, Any]] = config_data.get("vehicles", [])

    def resolve(self, trx_value: int | str | None) -> RfidResolution | None:
        """Resolve a trx sensor value to a user/vehicle.

        Returns None if trx_value is None (no session should start).
        Returns RfidResolution with user_type="unknown" for trx=0 or unmapped/inactive cards.
        Accepts both int and str for trx_value (type-agnostic per FR-005).
        """
        if trx_value is None:
            return None

        # Normalize to int
        try:
            trx_int = int(trx_value)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "Unexpected trx value format: %r — treating as type error",
                trx_value,
            )
            return RfidResolution(
                user_name="Unknown",
                user_type="unknown",
                vehicle_name=None,
                vehicle_battery_kwh=None,
                efficiency_factor=None,
                rfid_index=None,
                reason="type_error",
            )

        # trx=0 means no RFID card used
        if trx_int == 0:
            return RfidResolution(
                user_name="Unknown",
                user_type="unknown",
                vehicle_name=None,
                vehicle_battery_kwh=None,
                efficiency_factor=None,
                rfid_index=None,
                reason="no_rfid",
            )

        rfid_index = trx_int - 1

        # Find mapping by card_index
        mapping = next(
            (m for m in self._mappings if m.get("card_index") == rfid_index),
            None,
        )

        if mapping is None:
            _LOGGER.warning(
                "No RFID mapping found for trx=%r (index %d)",
                trx_value,
                rfid_index,
            )
            return RfidResolution(
                user_name="Unknown",
                user_type="unknown",
                vehicle_name=None,
                vehicle_battery_kwh=None,
                efficiency_factor=None,
                rfid_index=rfid_index,
                reason="unmapped",
            )

        if not mapping.get("active", True):
            _LOGGER.warning(
                "RFID card at index %d is inactive (trx=%r)",
                rfid_index,
                trx_value,
            )
            return RfidResolution(
                user_name="Unknown",
                user_type="unknown",
                vehicle_name=None,
                vehicle_battery_kwh=None,
                efficiency_factor=None,
                rfid_index=rfid_index,
                reason="rfid_inactive",
            )

        # Resolve user
        user_id = mapping.get("user_id")
        user = next((u for u in self._users if u.get("id") == user_id), None)
        if user is None:
            _LOGGER.warning("User %r referenced by RFID mapping not found", user_id)
            return RfidResolution(
                user_name="Unknown",
                user_type="unknown",
                vehicle_name=None,
                vehicle_battery_kwh=None,
                efficiency_factor=None,
                rfid_index=rfid_index,
                reason="unmapped",
            )

        user_name = user.get("name", "Unknown")
        user_type = user.get("type", "regular")

        # Resolve optional vehicle
        vehicle_id = mapping.get("vehicle_id")
        vehicle_name: str | None = None
        vehicle_battery_kwh: float | None = None
        efficiency_factor: float | None = None

        if vehicle_id:
            vehicle = next(
                (v for v in self._vehicles if v.get("id") == vehicle_id),
                None,
            )
            if vehicle:
                vehicle_name = vehicle.get("name")
                vehicle_battery_kwh = vehicle.get("battery_capacity_kwh")
                efficiency_factor = vehicle.get("charging_efficiency")

        # Extract guest pricing snapshot if user is a guest (PR-06)
        guest_pricing: dict | None = None
        if user_type == "guest":
            guest_pricing = user.get("guest_pricing")

        return RfidResolution(
            user_name=user_name,
            user_type=user_type,
            vehicle_name=vehicle_name,
            vehicle_battery_kwh=vehicle_battery_kwh,
            efficiency_factor=efficiency_factor,
            rfid_index=rfid_index,
            reason="matched",
            guest_pricing=guest_pricing,
        )
