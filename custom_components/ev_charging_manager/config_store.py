"""ConfigStore — persistent JSON config wrapping helpers.storage.Store."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import (
    SIGNAL_RFID_MAPPING_ADDED,
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
        self._hass = hass
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
        """Rebuild JSON config from subentries (source of truth) and persist.

        Emits SIGNAL_RFID_MAPPING_ADDED for each newly-added RFID mapping
        (compared to the previous in-memory snapshot) so that
        PlugAnchoredSessionEngine can auto-dismiss any pending unmapped-RFID
        persistent notification (FR-022, PR-22 revision 2026-05-19).
        """
        # Capture previous RFID-mapping set for diffing before we overwrite _data.
        previous_indices = {m.get("card_index") for m in self._data.get("rfid_mappings", []) if m}

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

        # Diff and emit dispatcher signal for any newly-added mapping.
        # `card_index` is the integer slot 0–9; matches Session.rfid_index used by
        # the notification dismisser. We deliberately fire one signal per newly-
        # added index so the dismisser receives the exact value(s) it should
        # match against active notifications.
        new_indices = {m.get("card_index") for m in rfid_mappings if m} - previous_indices
        if new_indices:
            signal = SIGNAL_RFID_MAPPING_ADDED.format(entry.entry_id)
            for idx in new_indices:
                # idx may be None if subentry data is malformed; pass through so
                # the dismisser can also clear "trx-null" notifications when
                # the user adds the first-ever mapping in response to one.
                async_dispatcher_send(self._hass_for_signal(entry), signal, idx)

    def _hass_for_signal(self, entry: ConfigEntry) -> HomeAssistant:
        """Return the HomeAssistant instance used to dispatch signals.

        ConfigStore stores `hass` as an attribute on the underlying Store; for
        clarity we keep a direct reference set at init time.
        """
        # _hass is set in __init__; cast for the type checker.
        return self._hass
