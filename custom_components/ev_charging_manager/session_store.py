"""SessionStore — persist completed charging sessions via HA storage."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    DEFAULT_MAX_STORED_SESSIONS,
    SESSION_STORE_KEY,
    SESSION_STORE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


class SessionStore:
    """Persist completed charging sessions as JSON via HA's helpers.storage.Store.

    Store key: ev_charging_manager_sessions
    Store version: 1
    Format: list of session dicts (see data-model.md for schema)

    Retention: when sessions exceed max_sessions, oldest (index 0) are pruned.
    """

    def __init__(self, hass: HomeAssistant, max_sessions: int = DEFAULT_MAX_STORED_SESSIONS) -> None:
        """Initialize the session store."""
        self._store: Store[list[dict[str, Any]]] = Store(
            hass, SESSION_STORE_VERSION, SESSION_STORE_KEY
        )
        self._sessions: list[dict[str, Any]] = []
        self._max_sessions = max_sessions

    @property
    def sessions(self) -> list[dict[str, Any]]:
        """Return the in-memory session list."""
        return self._sessions

    async def async_load(self) -> list[dict[str, Any]]:
        """Load sessions from disk. Returns empty list if no file exists."""
        stored = await self._store.async_load()
        if stored is None:
            _LOGGER.debug("No existing session store found, starting fresh")
            self._sessions = []
        else:
            self._sessions = stored
            _LOGGER.debug("Loaded %d sessions from storage", len(self._sessions))
        return self._sessions

    async def async_save(self) -> None:
        """Persist in-memory sessions to disk."""
        await self._store.async_save(self._sessions)

    async def add_session(self, session_dict: dict[str, Any]) -> None:
        """Add a completed session and enforce retention limit.

        If the session count would exceed max_sessions, the oldest session
        (first in list) is removed before adding the new one.
        """
        self._sessions.append(session_dict)
        if len(self._sessions) > self._max_sessions:
            removed = self._sessions.pop(0)
            _LOGGER.debug(
                "Session retention limit reached (%d), removed oldest session %s",
                self._max_sessions,
                removed.get("id", "?"),
            )
        await self.async_save()

    async def async_save_active_session(self, session_dict: dict[str, Any]) -> None:
        """Persist an in-progress session snapshot for crash recovery.

        Saves the active session as the last entry in the store temporarily.
        This is NOT the same as add_session — it writes a transient snapshot.
        Used by periodic save to survive HA restarts mid-session.
        """
        # Write only the active session snapshot (not mixed with completed sessions)
        await self._store.async_save([*self._sessions, session_dict])

    @callback
    def schedule_periodic_save(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        interval_seconds: int,
        get_active_session_dict: Any,
    ) -> None:
        """Schedule periodic persistence of the active session state.

        Uses async_track_time_interval, unsubscribed on entry unload via
        entry.async_on_unload(). get_active_session_dict is a callable that
        returns the current active session as a dict, or None if no session.
        """
        from datetime import timedelta

        async def _save(_now: Any) -> None:
            active = get_active_session_dict()
            if active is not None:
                _LOGGER.debug("Periodic save: persisting active session snapshot")
                await self.async_save_active_session(active)

        cancel = async_track_time_interval(
            hass,
            _save,
            timedelta(seconds=interval_seconds),
        )
        entry.async_on_unload(cancel)
