"""ConfigStore — persistent JSON config wrapping helpers.storage.Store."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    STORE_KEY,
    STORE_VERSION,
    SUBENTRY_TYPE_RFID_MAPPING,
    SUBENTRY_TYPE_USER,
    SUBENTRY_TYPE_VEHICLE,
)
from .models import RfidMapping, User, Vehicle

_LOGGER = logging.getLogger(__name__)

EMPTY_CONFIG: dict[str, list] = {
    "vehicles": [],
    "users": [],
    "rfid_mappings": [],
}


class ConfigStore:
    """Wrap helpers.storage.Store for denormalized EV Charging Manager config."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the config store."""
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, STORE_KEY)
        self._data: dict[str, Any] = {**EMPTY_CONFIG}

    async def async_load(self) -> dict[str, Any]:
        """Load config from disk. Returns empty structure on missing file (FR-022)."""
        stored = await self._store.async_load()
        if stored is None:
            _LOGGER.debug("No existing config store found, starting with empty config")
            self._data = {
                "vehicles": [],
                "users": [],
                "rfid_mappings": [],
            }
        else:
            self._data = stored
        return self._data

    async def async_save(self) -> None:
        """Save current config to disk."""
        await self._store.async_save(self._data)

    @property
    def data(self) -> dict[str, Any]:
        """Return current in-memory config data."""
        return self._data

    async def async_sync_from_subentries(self, entry: ConfigEntry) -> None:
        """Rebuild JSON config from subentries (source of truth) and persist."""
        vehicles: list[dict[str, Any]] = []
        users: list[dict[str, Any]] = []
        rfid_mappings: list[dict[str, Any]] = []

        for subentry in entry.subentries.values():
            data = dict(subentry.data)
            if subentry.subentry_type == SUBENTRY_TYPE_VEHICLE:
                vehicle = Vehicle.from_subentry(subentry.subentry_id, data)
                vehicles.append(vehicle.to_dict())
            elif subentry.subentry_type == SUBENTRY_TYPE_USER:
                user = User.from_subentry(subentry.subentry_id, data)
                users.append(user.to_dict())
            elif subentry.subentry_type == SUBENTRY_TYPE_RFID_MAPPING:
                mapping = RfidMapping.from_subentry(data)
                rfid_mappings.append(mapping.to_dict())

        self._data = {
            "vehicles": vehicles,
            "users": users,
            "rfid_mappings": rfid_mappings,
        }
        await self.async_save()
