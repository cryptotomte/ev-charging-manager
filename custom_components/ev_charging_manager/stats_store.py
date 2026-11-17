"""StatsStore — persist per-user statistics via HA helpers.storage.Store.

Store key:     ev_charging_manager_stats
Store version: 1
Format:        {"user_stats": {...}, "guest_last": {...} | null, "unknown_session_times": [...],
                "guest_retired": {"energy_kwh": float, "cost_kr": float}}
"""

from __future__ import annotations

import logging
import math
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STATS_STORE_KEY, STATS_STORE_VERSION
from .stats_engine import GuestLastSession, UserStats

_LOGGER = logging.getLogger(__name__)

EMPTY_GUEST_RETIRED: dict[str, float] = {"energy_kwh": 0.0, "cost_kr": 0.0}


class StatsStore:
    """Wrap helpers.storage.Store for per-user charging statistics.

    Serializes/deserializes UserStats and GuestLastSession to/from JSON.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the stats store."""
        self._store: Store[dict[str, Any]] = Store(hass, STATS_STORE_VERSION, STATS_STORE_KEY)

    async def async_load(
        self,
    ) -> tuple[dict[str, UserStats], GuestLastSession | None, list[str], dict[str, float]]:
        """Load statistics from disk.

        Returns (empty dict, None, [], zero carry) if no file exists yet.
        Malformed entries are skipped with a warning.
        """
        stored = await self._store.async_load()
        if stored is None:
            _LOGGER.debug("No existing stats store found, starting fresh")
            return {}, None, [], dict(EMPTY_GUEST_RETIRED)

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

        # Spec 026 D-6: optional key; absent reads as zeros.
        guest_retired = dict(EMPTY_GUEST_RETIRED)
        raw_retired = stored.get("guest_retired")
        if raw_retired is not None:
            malformed = not isinstance(raw_retired, dict)
            if not malformed:
                for field, allow_negative in (("energy_kwh", False), ("cost_kr", True)):
                    try:
                        guest_retired[field] = _carry_value(raw_retired[field], allow_negative)
                    except (KeyError, TypeError, ValueError):
                        malformed = True
            if malformed:
                _LOGGER.warning(
                    "Malformed guest_retired in stats store, invalid fields read as 0: %r",
                    raw_retired,
                )

        _LOGGER.debug("Loaded stats for %d user(s) from storage", len(user_stats))
        return user_stats, guest_last, unknown_session_times, guest_retired

    async def async_save(
        self,
        user_stats: dict[str, UserStats],
        guest_last: GuestLastSession | None,
        unknown_session_times: list[str] | None,
        guest_retired: dict[str, float],
    ) -> None:
        """Persist current statistics to disk."""
        data: dict[str, Any] = {
            "user_stats": {name: stats.to_dict() for name, stats in user_stats.items()},
            "guest_last": guest_last.to_dict() if guest_last is not None else None,
            "unknown_session_times": unknown_session_times
            if unknown_session_times is not None
            else [],
            "guest_retired": guest_retired,
        }
        await self._store.async_save(data)


def _carry_value(raw: Any, allow_negative: bool) -> float:
    """Return one guest_retired total; raise ValueError or TypeError if it is not one (D-29).

    Cost may be negative because spot prices can be (V-1); energy may not.
    """
    if isinstance(raw, bool):
        raise TypeError("boolean")
    value = float(raw)
    if not math.isfinite(value) or (value < 0 and not allow_negative):
        raise ValueError(value)
    return value
