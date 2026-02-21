"""StatsEngine — per-user statistics accumulation and monthly rollover.

Dataclasses (MonthStats, UserStats, GuestLastSession) and the StatsEngine
that subscribes to ev_charging_manager_session_completed events, updates
in-memory stats, persists via StatsStore, and dispatches SIGNAL_STATS_UPDATE.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_utc_time_change

from .const import EVENT_SESSION_COMPLETED, SIGNAL_STATS_UPDATE

if TYPE_CHECKING:
    from .stats_store import StatsStore

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model dataclasses (T002)
# ---------------------------------------------------------------------------


@dataclass
class MonthStats:
    """Monthly charging statistics for one user."""

    month: str  # YYYY-MM format
    energy_kwh: float = 0.0
    cost_kr: float = 0.0
    sessions: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "month": self.month,
            "energy_kwh": self.energy_kwh,
            "cost_kr": self.cost_kr,
            "sessions": self.sessions,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MonthStats:
        """Deserialize from a storage dict."""
        return cls(
            month=data.get("month", ""),
            energy_kwh=float(data.get("energy_kwh", 0.0)),
            cost_kr=float(data.get("cost_kr", 0.0)),
            sessions=int(data.get("sessions", 0)),
        )

    @classmethod
    def empty(cls, month: str) -> MonthStats:
        """Return a zeroed MonthStats for the given YYYY-MM month key."""
        return cls(month=month)


@dataclass
class UserStats:
    """Accumulated charging statistics for one named user on one charger."""

    user_name: str
    user_type: str  # "regular", "guest", or "unknown"
    total_energy_kwh: float = 0.0
    total_cost_kr: float = 0.0
    session_count: int = 0
    last_session_at: str | None = None
    current_month: MonthStats = field(default_factory=lambda: MonthStats.empty(""))
    previous_month: MonthStats = field(default_factory=lambda: MonthStats.empty(""))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "user_name": self.user_name,
            "user_type": self.user_type,
            "total_energy_kwh": self.total_energy_kwh,
            "total_cost_kr": self.total_cost_kr,
            "session_count": self.session_count,
            "last_session_at": self.last_session_at,
            "current_month": self.current_month.to_dict(),
            "previous_month": self.previous_month.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserStats:
        """Deserialize from a storage dict."""
        return cls(
            user_name=data["user_name"],
            user_type=data.get("user_type", "regular"),
            total_energy_kwh=float(data.get("total_energy_kwh", 0.0)),
            total_cost_kr=float(data.get("total_cost_kr", 0.0)),
            session_count=int(data.get("session_count", 0)),
            last_session_at=data.get("last_session_at"),
            current_month=MonthStats.from_dict(data.get("current_month", {"month": ""})),
            previous_month=MonthStats.from_dict(data.get("previous_month", {"month": ""})),
        )


@dataclass
class GuestLastSession:
    """Most recent guest charging session — retained until next guest session."""

    energy_kwh: float
    charge_price_kr: float | None = None  # Implemented in PR-06
    session_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "energy_kwh": self.energy_kwh,
            "charge_price_kr": self.charge_price_kr,
            "session_at": self.session_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GuestLastSession:
        """Deserialize from a storage dict."""
        return cls(
            energy_kwh=float(data.get("energy_kwh", 0.0)),
            charge_price_kr=data.get("charge_price_kr"),
            session_at=data.get("session_at"),
        )


# ---------------------------------------------------------------------------
# StatsEngine (T004 skeleton + T009 accumulation + T014-T015 rollover)
# ---------------------------------------------------------------------------


class StatsEngine:
    """Accumulate per-user charging statistics from session completion events.

    - Subscribes to EVENT_SESSION_COMPLETED on the HA event bus (R4).
    - Schedules a daily midnight callback for month rollover (R5, D4).
    - Persists via StatsStore after each update.
    - Dispatches SIGNAL_STATS_UPDATE so sensor entities can refresh.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        stats_store: StatsStore,
    ) -> None:
        """Initialize the StatsEngine."""
        self._hass = hass
        self._entry = entry
        self._stats_store = stats_store
        self._user_stats: dict[str, UserStats] = {}
        self._guest_last: GuestLastSession | None = None
        self._unsub_event: Callable[[], None] | None = None
        self._unsub_midnight: Callable[[], None] | None = None

    @property
    def user_stats(self) -> dict[str, UserStats]:
        """Return the in-memory user statistics dict."""
        return self._user_stats

    @property
    def guest_last(self) -> GuestLastSession | None:
        """Return the last guest session data, or None if none has occurred."""
        return self._guest_last

    async def async_setup(self) -> None:
        """Load persisted stats, ensure Unknown user exists, subscribe to events."""
        # Load persisted statistics (T003/T010)
        self._user_stats, self._guest_last = await self._stats_store.async_load()

        # Always ensure "Unknown" user entry exists (FR-007, T018)
        if "Unknown" not in self._user_stats:
            self._user_stats["Unknown"] = UserStats(
                user_name="Unknown",
                user_type="unknown",
            )

        # Subscribe to session completed event bus (D1, R4)
        self._unsub_event = self._hass.bus.async_listen(
            EVENT_SESSION_COMPLETED,
            self._async_handle_session_completed,
        )

        # Register daily UTC midnight callback for month rollover (D4, R5)
        self._unsub_midnight = async_track_utc_time_change(
            self._hass,
            self._async_midnight_callback,
            hour=0,
            minute=0,
            second=0,
        )

        # Register cleanup when entry is unloaded
        self._entry.async_on_unload(self.async_teardown)

        _LOGGER.debug("StatsEngine set up for entry %s", self._entry.entry_id)

    @callback
    def async_teardown(self) -> None:
        """Unsubscribe all event and time listeners."""
        if self._unsub_event:
            self._unsub_event()
            self._unsub_event = None
        if self._unsub_midnight:
            self._unsub_midnight()
            self._unsub_midnight = None
        _LOGGER.debug("StatsEngine torn down for entry %s", self._entry.entry_id)

    async def _async_handle_session_completed(self, event: Event) -> None:
        """Handle session_completed: accumulate stats and persist.

        Extracts user_name, user_type, energy_kwh, cost_kr, started_at,
        ended_at from the event. Updates or creates UserStats. Updates
        current_month using started_at (FR-006). Updates GuestLastSession
        if user_type == "guest" (T021).
        """
        data = event.data
        user_name: str = data.get("user_name") or "Unknown"
        user_type: str = data.get("user_type") or "unknown"
        energy_kwh: float = float(data.get("energy_kwh", 0.0))
        cost_kr: float = float(data.get("cost_kr", 0.0))
        started_at: str = data.get("started_at") or ""
        ended_at: str | None = data.get("ended_at")

        # Find or create UserStats for this user (T009)
        if user_name not in self._user_stats:
            self._user_stats[user_name] = UserStats(
                user_name=user_name,
                user_type=user_type,
            )
        stats = self._user_stats[user_name]

        # Accumulate lifetime totals (T009)
        stats.total_energy_kwh = round(stats.total_energy_kwh + energy_kwh, 3)
        stats.total_cost_kr = round(stats.total_cost_kr + cost_kr, 2)
        stats.session_count += 1
        stats.last_session_at = ended_at

        # Update current_month using started_at (FR-006, T014)
        month_key = _month_key_from_iso(started_at)
        if month_key:
            if stats.current_month.month != month_key:
                # started_at belongs to a new month — inline rollover
                stats.previous_month = stats.current_month
                stats.current_month = MonthStats.empty(month_key)
            stats.current_month.energy_kwh = round(stats.current_month.energy_kwh + energy_kwh, 3)
            stats.current_month.cost_kr = round(stats.current_month.cost_kr + cost_kr, 2)
            stats.current_month.sessions += 1

        # Guest last-session update (T021, FR-008/FR-009)
        if user_type == "guest":
            self._guest_last = GuestLastSession(
                energy_kwh=energy_kwh,
                charge_price_kr=None,  # Implemented in PR-06
                session_at=ended_at,
            )

        # Persist and notify sensors
        await self._stats_store.async_save(self._user_stats, self._guest_last)
        self._dispatch_update()

        _LOGGER.debug(
            "Stats updated for user '%s': total=%.3f kWh, sessions=%d",
            user_name,
            stats.total_energy_kwh,
            stats.session_count,
        )

    async def _async_midnight_callback(self, now: datetime) -> None:
        """Perform month rollover on 1st of each month at midnight (T015, FR-005/013).

        Idempotent: if current_month.month already equals the new month key,
        no rollover is performed for that user (FR-013).
        """
        if now.day != 1:
            return

        new_month = now.strftime("%Y-%m")
        rolled = 0
        for stats in self._user_stats.values():
            if stats.current_month.month == new_month:
                # Already on the correct month — idempotent (FR-013)
                continue
            stats.previous_month = stats.current_month
            stats.current_month = MonthStats.empty(new_month)
            rolled += 1

        if rolled:
            _LOGGER.info("Month rollover to %s completed for %d user(s)", new_month, rolled)
            await self._stats_store.async_save(self._user_stats, self._guest_last)
            self._dispatch_update()

    @callback
    def _dispatch_update(self) -> None:
        """Dispatch SIGNAL_STATS_UPDATE to notify sensor entities."""
        signal = SIGNAL_STATS_UPDATE.format(self._entry.entry_id)
        async_dispatcher_send(self._hass, signal)


def _month_key_from_iso(iso_timestamp: str) -> str:
    """Extract YYYY-MM month key from an ISO 8601 timestamp string.

    Returns empty string if parsing fails or input is empty.
    """
    if not iso_timestamp:
        return ""
    try:
        dt = datetime.fromisoformat(iso_timestamp)
        return dt.strftime("%Y-%m")
    except (ValueError, TypeError):
        _LOGGER.warning("Could not parse timestamp for month key: %s", iso_timestamp)
        return ""
