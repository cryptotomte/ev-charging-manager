"""Lifecycle operations for cascade deactivation/reactivation/deletion."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .charger_profiles import CHARGER_PROFILES
from .const import (
    CONF_CABLE_LOCK_ENTITY,
    CONF_CHARGER_PROFILE,
    CONF_CHARGER_SERIAL,
    CONF_ERROR_ENTITY,
    CONF_MODEL_STATUS_ENTITY,
    CONF_PLUG_ENTITY,
)

_LOGGER = logging.getLogger(__name__)

# Mapping from options key to profile key name for observation slots
_OBSERVATION_SLOT_MAP = {
    CONF_PLUG_ENTITY: "plug_sensor",
    CONF_CABLE_LOCK_ENTITY: "cable_lock_sensor",
    CONF_MODEL_STATUS_ENTITY: "model_status_sensor",
    CONF_ERROR_ENTITY: "error_sensor",
}


async def async_migrate_observation_slots(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Populate missing observation-slot options from the charger profile.

    Runs at async_setup_entry time.  Only fills slots that are absent from
    entry.options — a slot that was deliberately cleared by the user (value
    None or empty string) is left untouched once it has been set.

    For profiles without observation-entity patterns (e.g. 'generic') this
    function is a no-op.
    """
    profile_key = entry.data.get(CONF_CHARGER_PROFILE, "")
    profile = CHARGER_PROFILES.get(profile_key, {})
    serial = entry.data.get(CONF_CHARGER_SERIAL, "")

    new_options: dict = {}
    for conf_key, profile_key_name in _OBSERVATION_SLOT_MAP.items():
        pattern = profile.get(profile_key_name)
        if pattern is None:
            # Profile has no value for this slot (e.g. generic) — skip
            continue
        if conf_key in entry.options:
            # Slot already set (including deliberate None/custom value) — skip
            continue
        if "{serial}" in pattern and not serial:
            # Serial is required to resolve this pattern but is missing — skip the
            # slot rather than persisting the literal placeholder string.
            _LOGGER.warning(
                "Observation slot %s skipped — charger serial missing",
                conf_key,
            )
            continue
        resolved = pattern.replace("{serial}", serial)
        new_options[conf_key] = resolved

    if new_options:
        merged = {**entry.options, **new_options}
        hass.config_entries.async_update_entry(entry, options=merged)
        _LOGGER.debug(
            "Migrated observation slots for entry %s: %s",
            entry.entry_id,
            list(new_options.keys()),
        )


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
