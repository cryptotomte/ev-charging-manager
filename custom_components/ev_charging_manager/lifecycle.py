"""Lifecycle operations for cascade deactivation/reactivation/deletion."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def async_cascade_deactivate_user(
    hass: HomeAssistant,
    entry: ConfigEntry,
    user_subentry_id: str,
) -> None:
    """Cascade deactivation: set all active RFID mappings for this user to inactive."""
    for sub in list(entry.subentries.values()):
        if (
            sub.subentry_type == "rfid_mapping"
            and sub.data["user_id"] == user_subentry_id
            and sub.data.get("active", True)
        ):
            new_data = dict(sub.data)
            new_data["active"] = False
            new_data["deactivated_by"] = "user_cascade"
            hass.config_entries.async_update_subentry(entry, sub, data=new_data)
            _LOGGER.debug(
                "Cascade deactivated RFID mapping %s (card #%s)",
                sub.subentry_id,
                sub.data["card_index"],
            )


async def async_cascade_reactivate_user(
    hass: HomeAssistant,
    entry: ConfigEntry,
    user_subentry_id: str,
) -> None:
    """Cascade reactivation: restore only cascade-deactivated mappings (not individual)."""
    for sub in list(entry.subentries.values()):
        if (
            sub.subentry_type == "rfid_mapping"
            and sub.data["user_id"] == user_subentry_id
            and sub.data.get("deactivated_by") == "user_cascade"
        ):
            new_data = dict(sub.data)
            new_data["active"] = True
            new_data["deactivated_by"] = None
            hass.config_entries.async_update_subentry(entry, sub, data=new_data)
            _LOGGER.debug(
                "Cascade reactivated RFID mapping %s (card #%s)",
                sub.subentry_id,
                sub.data["card_index"],
            )


async def async_cascade_delete_user(
    hass: HomeAssistant,
    entry: ConfigEntry,
    removed_user_subentry_id: str,
) -> None:
    """Cascade deletion: remove all RFID mappings for this deleted user."""
    # Collect IDs first, then remove — avoids issues with dict mutation during iteration
    to_remove = [
        sub.subentry_id
        for sub in entry.subentries.values()
        if sub.subentry_type == "rfid_mapping" and sub.data["user_id"] == removed_user_subentry_id
    ]
    for sid in to_remove:
        if sid in entry.subentries:  # Guard against re-entrant listener
            hass.config_entries.async_remove_subentry(entry, sid)
            _LOGGER.debug(
                "Cascade deleted RFID mapping %s for removed user %s",
                sid,
                removed_user_subentry_id,
            )


async def async_cascade_delete_vehicle(
    hass: HomeAssistant,
    entry: ConfigEntry,
    removed_vehicle_subentry_id: str,
) -> None:
    """Cascade deletion: nullify vehicle_id on all RFID mappings referencing this vehicle."""
    for sub in list(entry.subentries.values()):
        if (
            sub.subentry_type == "rfid_mapping"
            and sub.data.get("vehicle_id") == removed_vehicle_subentry_id
        ):
            new_data = dict(sub.data)
            new_data["vehicle_id"] = None
            hass.config_entries.async_update_subentry(entry, sub, data=new_data)
            _LOGGER.debug(
                "Nullified vehicle_id on RFID mapping %s (card #%s) for removed vehicle %s",
                sub.subentry_id,
                sub.data["card_index"],
                removed_vehicle_subentry_id,
            )
