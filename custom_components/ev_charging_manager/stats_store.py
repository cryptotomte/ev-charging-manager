"""StatsStore — persist per-user statistics via HA helpers.storage.Store.

Store key:     ev_charging_manager_stats
Store version: 1
Format:        {"user_stats": {...}, "guest_last": {...} | null, "unknown_session_times": [...]}
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STATS_STORE_KEY, STATS_STORE_VERSION
from .stats_engine import GuestLastSession, UserStats

_LOGGER = logging.getLogger(__name__)


class StatsStore:
    """Wrap helpers.storage.Store for per-user charging statistics.

    Serializes/deserializes UserStats and GuestLastSession to/from JSON.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the stats store."""
        self._store: Store[dict[str, Any]] = Store(hass, STATS_STORE_VERSION, STATS_STORE_KEY)

    async def async_load(
        self,
    ) -> tuple[dict[str, UserStats], GuestLastSession | None, list[str]]:
        """Load statistics from disk.

        Returns (empty dict, None, []) if no file exists yet.
        Malformed entries are skipped with a warning.
        """
        stored = await self._store.async_load()
        if stored is None:
            _LOGGER.debug("No existing stats store found, starting fresh")
            return {}, None, []

        # Deserialize per-user stats
        user_stats: dict[str, UserStats] = {}
        for name, data in stored.get("user_stats", {}).items():
            try:
                user_stats[name] = UserStats.from_dict(data)
            except (KeyError, ValueError, TypeError) as err:
                _LOGGER.warning("Skipping malformed stats entry for user '%s': %s", name, err)

        # Deserialize guest last-session
        guest_last: GuestLastSession | None = None
        guest_data = stored.get("guest_last")
        if guest_data:
            try:
                guest_last = GuestLastSession.from_dict(guest_data)
            except (KeyError, ValueError, TypeError) as err:
                _LOGGER.warning("Skipping malformed guest_last data: %s", err)

        # PR-07: Deserialize unknown session timestamps (list of ISO strings)
        unknown_session_times: list[str] = stored.get("unknown_session_times", [])
        if not isinstance(unknown_session_times, list):
            unknown_session_times = []

        _LOGGER.debug("Loaded stats for %d user(s) from storage", len(user_stats))
        return user_stats, guest_last, unknown_session_times

    async def async_save(
        self,
        user_stats: dict[str, UserStats],
        guest_last: GuestLastSession | None,
        unknown_session_times: list[str] | None = None,
    ) -> None:
        """Persist current statistics to disk."""
        data: dict[str, Any] = {
            "user_stats": {name: stats.to_dict() for name, stats in user_stats.items()},
            "guest_last": guest_last.to_dict() if guest_last is not None else None,
            "unknown_session_times": unknown_session_times
            if unknown_session_times is not None
            else [],
        }
        await self._store.async_save(data)
