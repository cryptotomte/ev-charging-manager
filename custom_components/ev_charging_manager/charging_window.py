"""ChargingWindow and ChargingWindowTracker for PR-22 plug-anchored session model.

A ChargingWindow represents one continuous period of energy flow within a session.
Windows are bounded by:
  - Open:  first power > 0 event (or first power > 0 after idle threshold elapses)
  - Close: power == 0 sustained for >= charging_idle_timeout_min

Multiple windows can occur within one session (BMS balancing, post-completion cell
balancing, user app pause/resume). Only aggregate counts and durations are persisted;
individual window detail goes to the debug log only (per data-model.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ChargingWindow:
    """One continuous period of energy flow within a charging session.

    Fields:
        start_at:            Timestamp when this window opened (first power > 0).
        end_at:              Timestamp when window closed (idle timeout elapsed). None if open.
        energy_start_kwh:    Counter reading at window open.
        energy_end_kwh:      Counter reading at window close. None if open.
        last_power_change_at: Updated when power transitions between 0 and > 0.
                              Used to detect the idle threshold expiry.
        last_power_value:    Most recent power reading. Used to detect 0 → > 0 transitions.
    """

    start_at: datetime
    end_at: datetime | None = None
    energy_start_kwh: float = 0.0
    energy_end_kwh: float | None = None
    last_power_change_at: datetime = field(default_factory=datetime.utcnow)
    last_power_value: float = 0.0

    @property
    def is_open(self) -> bool:
        """Return True if this window has not yet been closed."""
        return self.end_at is None

    def duration_s(self, now: datetime | None = None) -> int:
        """Return duration of this window in seconds.

        For closed windows returns the fixed duration. For an open window
        returns the elapsed time up to `now` (or utcnow() if not provided).
        """
        if self.end_at is not None:
            return int((self.end_at - self.start_at).total_seconds())
        reference = now if now is not None else datetime.utcnow()
        return int((reference - self.start_at).total_seconds())

    def energy_kwh(self) -> float:
        """Return energy delivered in this window.

        For closed windows returns the fixed delta. For an open window returns
        the energy delivered so far (which may be 0 if counter not yet read).
        """
        if self.energy_end_kwh is not None:
            return max(0.0, self.energy_end_kwh - self.energy_start_kwh)
        return 0.0


class ChargingWindowTracker:
    """Track the sequence of charging windows within a session.

    Maintains an ordered list of closed windows and at most one open window.
    The caller is responsible for persisting snapshot state; this class is
    in-memory only.
    """

    def __init__(self) -> None:
        """Initialize with empty window lists."""
        self._closed_windows: list[ChargingWindow] = []
        self._active_window: ChargingWindow | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def open_window(self, now: datetime, energy_kwh: float) -> ChargingWindow:
        """Open a new charging window at the given time and counter reading.

        Raises RuntimeError if a window is already open (caller must close first).
        """
        if self._active_window is not None and self._active_window.is_open:
            raise RuntimeError(
                "Cannot open a new window while the previous window is still open. "
                "Call close_window() first."
            )
        self._active_window = ChargingWindow(
            start_at=now,
            energy_start_kwh=energy_kwh,
            last_power_change_at=now,
            last_power_value=0.0,  # will be updated immediately by on_power_change
        )
        return self._active_window

    def close_window(self, now: datetime, energy_kwh: float) -> ChargingWindow:
        """Close the active window and move it to the closed list.

        Returns the closed window. Raises RuntimeError if no window is open.
        """
        if self._active_window is None or not self._active_window.is_open:
            raise RuntimeError("Cannot close window — no open window exists.")
        self._active_window.end_at = now
        self._active_window.energy_end_kwh = energy_kwh
        closed = self._active_window
        self._closed_windows.append(closed)
        self._active_window = None
        return closed

    def on_power_change(self, now: datetime, new_power: float) -> None:
        """Record a power transition on the active window.

        Updates last_power_change_at and last_power_value on the active window.
        No-op if no window is currently open.
        """
        if self._active_window is not None and self._active_window.is_open:
            self._active_window.last_power_change_at = now
            self._active_window.last_power_value = new_power

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def is_open(self) -> bool:
        """Return True if a charging window is currently open."""
        return self._active_window is not None and self._active_window.is_open

    def active_window(self) -> ChargingWindow | None:
        """Return the currently open window, or None."""
        if self._active_window is not None and self._active_window.is_open:
            return self._active_window
        return None

    def window_count(self) -> int:
        """Return total number of windows opened (closed + potentially open)."""
        open_count = 1 if self.is_open() else 0
        return len(self._closed_windows) + open_count

    def closed_window_count(self) -> int:
        """Return number of fully closed windows."""
        return len(self._closed_windows)

    def total_charging_duration_s(self, now: datetime | None = None) -> int:
        """Return total charging duration in seconds across all windows.

        For the open window (if any), uses `now` as the current time.
        """
        total = sum(w.duration_s() for w in self._closed_windows)
        if self._active_window is not None and self._active_window.is_open:
            total += self._active_window.duration_s(now)
        return total

    def current_window_duration_s(self, now: datetime | None = None) -> int:
        """Return the duration of the currently open window in seconds, or 0."""
        if not self.is_open():
            return 0
        return self._active_window.duration_s(now)  # type: ignore[union-attr]

    def last_closed_window(self) -> ChargingWindow | None:
        """Return the most recently closed window, or None."""
        return self._closed_windows[-1] if self._closed_windows else None

    def all_closed_windows(self) -> list[ChargingWindow]:
        """Return a copy of the closed windows list."""
        return list(self._closed_windows)
