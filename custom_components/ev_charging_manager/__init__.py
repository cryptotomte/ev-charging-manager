"""EV Charging Manager integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .charger_profiles import CHARGER_PROFILES
from .config_store import ConfigStore
from .const import CONF_CHARGER_NAME, CONF_CHARGER_PROFILE, DEFAULT_CHARGER_NAME, DOMAIN

_LOGGER = logging.getLogger(__name__)


def _snapshot_subentries(entry: ConfigEntry) -> dict[str, dict]:
    """Take a snapshot of current subentries for change detection."""
    return {
        sid: {"subentry_type": s.subentry_type, "data": dict(s.data)}
        for sid, s in entry.subentries.items()
    }


async def _on_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry updates (subentry add/edit/delete)."""
    if entry.state is not ConfigEntryState.LOADED:
        return

    domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    config_store: ConfigStore | None = domain_data.get("config_store")
    if not config_store:
        return

    prev = domain_data.get("prev_subentries", {})
    current_ids = set(entry.subentries.keys())
    prev_ids = set(prev.keys())

    # Detect deleted subentries and run cascade
    removed_ids = prev_ids - current_ids
    if removed_ids:
        from .lifecycle import async_cascade_delete_user, async_cascade_delete_vehicle

        for removed_id in removed_ids:
            removed_info = prev[removed_id]
            if removed_info["subentry_type"] == "user":
                _LOGGER.debug("User subentry %s removed, cascading delete", removed_id)
                await async_cascade_delete_user(hass, entry, removed_id)
            elif removed_info["subentry_type"] == "vehicle":
                _LOGGER.debug("Vehicle subentry %s removed, cascading delete", removed_id)
                await async_cascade_delete_vehicle(hass, entry, removed_id)
            else:
                _LOGGER.debug("RFID mapping subentry %s removed (no cascade)", removed_id)

    # Sync ConfigStore from subentries (source of truth)
    await config_store.async_sync_from_subentries(entry)

    # Update snapshot for next comparison
    domain_data["prev_subentries"] = _snapshot_subentries(entry)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EV Charging Manager from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}

    # Initialize ConfigStore and sync from subentries
    config_store = ConfigStore(hass)
    await config_store.async_load()
    await config_store.async_sync_from_subentries(entry)

    hass.data[DOMAIN][entry.entry_id]["config_store"] = config_store
    hass.data[DOMAIN][entry.entry_id]["prev_subentries"] = _snapshot_subentries(entry)

    # Register update listener for subentry changes
    entry.async_on_unload(entry.add_update_listener(_on_entry_updated))

    profile_key = entry.data.get(CONF_CHARGER_PROFILE, "")
    profile = CHARGER_PROFILES.get(profile_key, {})
    model = profile.get("name", "Manual")

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.data.get(CONF_CHARGER_NAME, DEFAULT_CHARGER_NAME),
        manufacturer="EV Charging Manager",
        model=model,
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.data[DOMAIN].pop(entry.entry_id, None)
    return True
