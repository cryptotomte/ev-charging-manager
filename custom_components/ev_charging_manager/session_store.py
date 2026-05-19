"""SessionStore — persist completed charging sessions via HA storage.

PR-22: Schema bumped from minor_version 1 → 2. Migration algorithm converts
legacy field names (started_at, ended_at, duration_seconds) to canonical PR-22
names (connected_at, disconnected_at, charging_duration_s) and populates new
nullable fields with defaults. Migration is idempotent.
"""

from __future__ import annotations

import logging
from datetime import datetime
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

# Current minor version — bump when the session schema changes.
_SESSION_STORE_MINOR_VERSION = 2


def _migrate_sessions_v1_1_to_v1_2(data: dict[str, Any]) -> dict[str, Any]:
    """Run the v1.1 → v1.2 migration on a raw store dict.

    Idempotent: if minor_version is already >= 2, returns data unchanged.
    Modifies and returns the dict in-place for efficiency.

    Migration algorithm (per data-model.md):
    - Rename started_at → connected_at (if connected_at absent)
    - Rename ended_at → disconnected_at (if disconnected_at absent)
    - Compute connection_duration_s from timestamps when both available
    - Map duration_seconds → charging_duration_s
    - Set new nullable fields (charging_started_at, charging_ended_at) to None
    - Set new count default (charging_window_count=0)
    - Bump minor_version to 2

    Note (PR-22 revision 2026-05-19): the originally-planned `blocked` field has
    been removed (FR-032 REVISED). Story 07 no longer force-stops sessions, so
    migrated records do not gain a `blocked` field.
    """
    if data.get("minor_version", 0) >= 2:
        return data  # already migrated — idempotent no-op

    sessions = data.get("data", [])
    for session in sessions:
        # Field renames — copy only if destination key is absent (idempotent)
        if "started_at" in session and "connected_at" not in session:
            session["connected_at"] = session["started_at"]
        if "ended_at" in session and "disconnected_at" not in session:
            session["disconnected_at"] = session["ended_at"]

        # Compute connection_duration_s
        connected = session.get("connected_at")
        disconnected = session.get("disconnected_at")
        if connected and disconnected and "connection_duration_s" not in session:
            try:
                t_connected = datetime.fromisoformat(connected)
                t_disconnected = datetime.fromisoformat(disconnected)
                session["connection_duration_s"] = int(
                    (t_disconnected - t_connected).total_seconds()
                )
            except (ValueError, TypeError):
                session["connection_duration_s"] = 0
        else:
            session.setdefault("connection_duration_s", 0)

        # Map existing duration → charging_duration_s
        if "charging_duration_s" not in session:
            session["charging_duration_s"] = session.get("duration_seconds", 0)

        # New nullable fields
        session.setdefault("charging_started_at", None)
        session.setdefault("charging_ended_at", None)

        # New count default
        # PR-22 revision 2026-05-19: do NOT add a `blocked` field (FR-032 REVISED).
        session.setdefault("charging_window_count", 0)

    data["minor_version"] = 2
    return data


class SessionStore:
    """Persist completed charging sessions as JSON via HA's helpers.storage.Store.

    Store key: ev_charging_manager_sessions
    Store version: 1
    Format: list of session dicts (see data-model.md for schema)

    Retention: when sessions exceed max_sessions, oldest (index 0) are pruned.
    """

    def __init__(
        self, hass: HomeAssistant, max_sessions: int = DEFAULT_MAX_STORED_SESSIONS
    ) -> None:
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

    async def async_load(self) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Load sessions from disk, running schema migration if needed.

        Returns a tuple of (completed_sessions, active_snapshot_or_None).
        The active_snapshot is the last periodic save of an in-progress session
        (ended_at / disconnected_at is None/missing). Only the most recent
        incomplete entry is treated as a recovery snapshot; any extras are discarded.

        PR-22: runs _migrate_sessions_v1_1_to_v1_2 on load if minor_version < 2.
        Migration is idempotent and persisted immediately after load.
        """
        raw = await self._store.async_load()
        if raw is None:
            _LOGGER.debug("No existing session store found, starting fresh")
            self._sessions = []
            return self._sessions, None

        # Wrap bare list (older store format) in expected envelope
        if isinstance(raw, list):
            raw = {"version": 1, "minor_version": 1, "data": raw}

        # Run schema migration (idempotent)
        if raw.get("minor_version", 0) < 2:
            _LOGGER.info(
                "Session store minor_version=%d — running v1.2 migration",
                raw.get("minor_version", 0),
            )
            raw = _migrate_sessions_v1_1_to_v1_2(raw)
            # Persist immediately so a crash after migration still saves the upgrade
            await self._store.async_save(raw)

        stored: list[dict[str, Any]] = raw.get("data", [])

        # Separate completed sessions from active snapshot(s).
        # A session is active (incomplete) when disconnected_at (preferred) or
        # ended_at (legacy) is None/missing.
        def _is_complete(s: dict[str, Any]) -> bool:
            return s.get("disconnected_at") is not None or s.get("ended_at") is not None

        complete = [s for s in stored if _is_complete(s)]
        incomplete = [s for s in stored if not _is_complete(s)]

        self._sessions = complete
        _LOGGER.debug("Loaded %d completed sessions from storage", len(self._sessions))

        active_snapshot: dict[str, Any] | None = None
        if incomplete:
            active_snapshot = incomplete[-1]  # most recent snapshot
            _LOGGER.info(
                "Found active session snapshot for recovery: id=%s",
                active_snapshot.get("id", "?"),
            )

        return self._sessions, active_snapshot

    def _make_envelope(self, sessions: list[dict[str, Any]]) -> dict[str, Any]:
        """Wrap a session list in the versioned store envelope."""
        return {
            "version": SESSION_STORE_VERSION,
            "minor_version": _SESSION_STORE_MINOR_VERSION,
            "key": SESSION_STORE_KEY,
            "data": sessions,
        }

    async def async_save(self) -> None:
        """Persist in-memory sessions to disk."""
        await self._store.async_save(self._make_envelope(self._sessions))

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
        # Write all completed sessions + the active snapshot in the versioned envelope
        await self._store.async_save(self._make_envelope([*self._sessions, session_dict]))

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
