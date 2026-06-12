"""DebugLogger — buffered, off-loop diagnostic file writer for EV Charging Manager.

The log file lives at ``{config_dir}/ev_charging_manager_debug.log`` (next to
``home-assistant.log``). It is reachable via file share/SSH only — it is
deliberately NOT under the web-served ``www/`` directory. The pre-v0.4.5
location ``{config_dir}/www/`` (served unauthenticated at ``/local/``) is the
removed legacy location; :func:`async_cleanup_legacy_file` deletes any file
left there.

I/O model (PR-28)
-----------------
``log()`` is synchronous and MUST only be called from the Home Assistant
event loop (all call sites are ``@callback`` handlers, setup code, or entity
methods). It never touches the file system — it appends a pre-formatted,
pre-timestamped line to an in-memory buffer. Buffered lines are written by a
background flush when the buffer reaches ``DEBUG_LOG_FLUSH_LINES`` lines or
``DEBUG_LOG_FLUSH_SECONDS`` seconds after the first buffered line, whichever
comes first. All file operations (rotation check, append, truncate, legacy
delete) run in the executor; flushes are serialized by an ``asyncio.Lock``.

When the active file exceeds ``DEBUG_LOG_MAX_BYTES`` at flush time it is
rotated to a single ``.1`` generation (``os.replace``) before the flush's
lines are appended to a fresh active file. On flush failure (``OSError``)
the lines stay buffered for retry, capped at ``DEBUG_LOG_BUFFER_CAP``
(drop-oldest); a dropped-count note is written on the next successful flush.

Lifecycle
---------
- Instantiated in ``async_setup_entry`` with ``hass`` and
  ``hass.config.config_dir``.
- ``async_enable()`` is awaited when the ``debug_logging`` option is True.
- ``async_disable()`` is registered via ``entry.async_on_unload`` — it
  buffers the DEBUG_OFF marker (always the final line) and flushes.
- ``async_clear()`` (clear-log button) flushes pending lines, truncates the
  active file off-loop, then buffers the DEBUG_CLEAR marker. The ``.1``
  rotation generation is left untouched.
- A new instance is created on every reload (options change triggers full
  reload).

Categories
----------
The DebugLogger accepts arbitrary category strings; the recognized category
constants live in const.py (``DEBUG_CAT_*``). Current set:

  Markers:      DEBUG_ON, DEBUG_OFF, DEBUG_CLEAR, DEBUG_DROPPED
  Sessions:     SESSION_START, SESSION_STOP, ENGINE_DECISION, RFID_READ
  Observation:  CAR_STATE, CAR_STATE_UNAVAIL, PLUG_STATE, CABLE_LOCK,
                MODEL_STATUS, ERR_STATE, TRX_STATE
  PR-22 model:  CHARGING_WINDOW_OPEN, CHARGING_WINDOW_CLOSE,
                DISCONNECT_DETECTED, DISCONNECT_RESOLVED,
                HA_RESTART_DETECTED, SESSION_RESUMED,
                SESSION_FORCE_ENDED_BY_RESTART,
                SESSION_FORCE_ENDED_BY_GRACE_TIMEOUT,
                SESSION_ENDED_BY_CABLE_UNLOCK,
                CHARGER_OFFLINE, CHARGER_BACK_ONLINE, TRX_MIDSESSION,
                RFID_UNMAPPED_NOTIFIED, RFID_UNMAPPED_NOTIFY_FAILED,
                RECOVERY_TIMEOUT
  PR-23/24/27:  HEARTBEAT, RFID_WAIT, DATA_GAP
  Legacy engine only: GATE_ENGAGED, GATE_PROMOTE, GATE_CLEAR, BALANCING_SKIP
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .const import (
    DEBUG_LOG_BUFFER_CAP,
    DEBUG_LOG_FLUSH_LINES,
    DEBUG_LOG_FLUSH_SECONDS,
    DEBUG_LOG_MAX_BYTES,
)

_LOGGER = logging.getLogger(__name__)

# Log file name written at the config root (FR-001). The same name under
# {config_dir}/www/ is the legacy location cleaned up at setup (FR-002).
_FILE_NAME = "ev_charging_manager_debug.log"

# Emit an HA logger warning every N consecutive flush failures
_WARN_EVERY_N_FAILURES = 5


def redact_tag(value: str | int | None) -> str:
    """Return the privacy-masked form of an RFID tag/trx value (FR-004).

    RFID tags are persistent personal identifiers — log lines may only carry
    a masked form: ``***`` plus the last two characters.

    Args:
        value: Raw tag/trx value as read from the charger entity.

    Returns:
        ``"None"`` for absent values, ``"***"`` for values of length <= 2,
        ``"***{last2}"`` otherwise (e.g. ``"abc123f4"`` -> ``"***f4"``).
    """
    if value is None:
        return "None"
    text = str(value)
    if len(text) <= 2:
        return "***"
    return f"***{text[-2:]}"


async def async_cleanup_legacy_file(hass: HomeAssistant, config_dir: str) -> None:
    """Delete the legacy web-served log file under www/ if present (FR-002).

    Runs unconditionally at setup — even when debug logging is disabled — so
    the unauthenticated ``/local/`` exposure ends regardless of the toggle
    (US1 scenario 4). Never raises: a deletion failure logs a WARNING naming
    the path so the user can remove the file manually.
    """
    legacy_path = os.path.join(config_dir, "www", _FILE_NAME)

    def _remove() -> bool:
        if not os.path.exists(legacy_path):
            return False
        os.remove(legacy_path)
        return True

    try:
        removed = await hass.async_add_executor_job(_remove)
    except Exception as err:  # noqa: BLE001 — cleanup must never fail setup
        _LOGGER.warning(
            "Could not delete legacy debug log file %s: %s — it is served "
            "unauthenticated at /local/ev_charging_manager_debug.log; "
            "please remove it manually",
            legacy_path,
            err,
        )
        return
    if removed:
        _LOGGER.info(
            "Deleted legacy debug log file %s — the debug log now lives at "
            "the config root and is no longer web-served",
            legacy_path,
        )


class DebugLogger:
    """Buffered diagnostic logger writing to a plain-text file off the event loop.

    See the module docstring for the I/O model, lifecycle, and category list.
    """

    def __init__(self, hass: HomeAssistant, config_dir: str) -> None:
        """Initialize with the hass instance and HA configuration directory path."""
        self._hass = hass
        self._config_dir = config_dir
        self._enabled: bool = False
        self._buffer: list[str] = []
        self._flush_lock = asyncio.Lock()
        self._cancel_flush_timer: CALLBACK_TYPE | None = None
        self._flush_scheduled: bool = False
        self._closed: bool = False
        self._fail_count: int = 0
        self._dropped_count: int = 0

    @property
    def file_path(self) -> str:
        """Return the absolute path to the active log file (config root)."""
        return os.path.join(self._config_dir, _FILE_NAME)

    @property
    def rotated_file_path(self) -> str:
        """Return the absolute path to the single .1 rotation generation."""
        return self.file_path + ".1"

    @property
    def enabled(self) -> bool:
        """Return True if debug logging is currently active."""
        return self._enabled

    async def async_enable(self) -> None:
        """Enable debug logging and buffer the DEBUG_ON marker line.

        No file system access happens here — the marker reaches disk with the
        first flush. Re-opens a previously closed (disabled) instance; in
        production a fresh instance is created per reload, so this only
        matters for in-place re-enable (tests).
        """
        self._closed = False
        self._enabled = True
        self.log("DEBUG_ON", "Debug logging enabled")

    async def async_disable(self) -> None:
        """Disable debug logging: buffer DEBUG_OFF, flush, close the instance.

        Called from BOTH ``entry.async_on_unload`` (reload/remove) and the
        EVENT_HOMEASSISTANT_STOP listener (orderly stop) — idempotent: the
        second call is a no-op (one DEBUG_OFF, one final flush). The DEBUG_OFF
        marker is buffered BEFORE the flag is cleared, so it is always the
        final line in the file (FR-008). No-op when logging was never enabled.
        """
        if self._closed:
            return
        if self._enabled:
            self.log("DEBUG_OFF", "Debug logging disabled")
            self._enabled = False
        self._closed = True
        await self._async_flush()

    def log(self, category: str, message: str) -> None:
        """Buffer one timestamped line if enabled. Sync, EVENT-LOOP ONLY.

        Must only be called from the event loop (callback handlers, setup,
        entity methods) — it schedules flush tasks/timers on the running loop
        without any cross-thread machinery. The line timestamp is taken here
        (buffer time), not at flush time. Performs NO file I/O (FR-005/006).

        Args:
            category: Short event category, padded to 15 chars in the output line.
            message:  Free-form human-readable event description.
        """
        if not self._enabled:
            return
        self._buffer.append(self._format_line(category, message))
        self._schedule_flush()

    async def async_clear(self) -> None:
        """Flush pending lines, truncate the active file, buffer DEBUG_CLEAR (FR-012).

        Serialized with in-flight flushes via the flush lock; the append +
        truncate run in a single executor job — never on the event loop.
        A no-op when neither the file nor buffered lines exist. The ``.1``
        rotation generation is left untouched. The DEBUG_CLEAR marker (only
        when logging is enabled) is buffered and reaches disk with the next
        flush.
        """
        async with self._flush_lock:
            self._cancel_timer()
            lines = self._buffer
            self._buffer = []

            def _flush_and_truncate() -> bool:
                path = self.file_path
                if not os.path.exists(path) and not lines:
                    return False
                # FR-008: pending lines hit the file before the truncation
                if lines:
                    with open(path, "a", encoding="utf-8") as fh:
                        fh.writelines(lines)
                with open(path, "w", encoding="utf-8"):
                    pass
                return True

            try:
                truncated = await self._hass.async_add_executor_job(_flush_and_truncate)
            except OSError as err:
                _LOGGER.warning("DebugLogger: could not clear log file: %s", err)
                return

        if truncated and self._enabled:
            self.log("DEBUG_CLEAR", "Log cleared by user")

    # ------------------------------------------------------------------
    # Internal: buffer flushing
    # ------------------------------------------------------------------

    @staticmethod
    def _format_line(category: str, message: str) -> str:
        """Format one log line — byte-identical to the pre-PR-28 format (SC-006)."""
        timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        return f"{timestamp} | {category:<15} | {message}\n"

    def _schedule_flush(self) -> None:
        """Arm the count or age flush trigger for the just-buffered line (FR-007)."""
        if self._flush_scheduled:
            return
        if len(self._buffer) >= DEBUG_LOG_FLUSH_LINES:
            # Count trigger: flush now (the timer, if armed, is superseded)
            self._flush_scheduled = True
            self._cancel_timer()
            self._hass.async_create_task(self._async_flush())
        elif self._cancel_flush_timer is None:
            # Age trigger: first line in an empty buffer arms the 5 s timer
            self._arm_timer()

    def _arm_timer(self) -> None:
        """Arm the age-flush timer.

        Deliberately NOT ``cancel_on_shutdown`` (review F1): the
        EVENT_HOMEASSISTANT_STOP listener owns shutdown flushing — a timer
        firing during shutdown is harmless (the flush lock serializes), while
        auto-cancellation would actively drop a pending flush.
        """
        self._cancel_flush_timer = async_call_later(
            self._hass,
            DEBUG_LOG_FLUSH_SECONDS,
            HassJob(self._on_flush_timer),
        )

    def _cancel_timer(self) -> None:
        """Cancel a pending age-flush timer, if any."""
        if self._cancel_flush_timer is not None:
            self._cancel_flush_timer()
            self._cancel_flush_timer = None

    @callback
    def _on_flush_timer(self, _now: datetime) -> None:
        """Age trigger fired — schedule a flush task."""
        self._cancel_flush_timer = None
        if self._flush_scheduled:
            return
        self._flush_scheduled = True
        self._hass.async_create_task(self._async_flush())

    async def _async_flush(self) -> None:
        """Write all buffered lines to disk in one serialized executor job.

        Rotation check + append happen in the SAME executor job under the
        flush lock, so no line can land between rotate and append (FR-011).
        On OSError the failed lines are re-prepended (order preserved) ahead
        of any lines that arrived during the attempt, the buffer is capped at
        DEBUG_LOG_BUFFER_CAP drop-oldest, and a throttled warning is emitted
        (FR-010).
        """
        async with self._flush_lock:
            self._flush_scheduled = False
            self._cancel_timer()
            if not self._buffer:
                return
            lines = self._buffer
            self._buffer = []

            write_lines = lines
            if self._dropped_count:
                # Recovery note for lines dropped while the disk was unwritable.
                # Built directly (not via log()) — no recursive flush scheduling.
                note = self._format_line(
                    "DEBUG_DROPPED",
                    f"{self._dropped_count} lines dropped while the log file was unwritable",
                )
                write_lines = [note, *lines]

            try:
                await self._hass.async_add_executor_job(self._write_lines, write_lines)
            except OSError as err:
                self._fail_count += 1
                # Failed lines go back FIRST — lines buffered during the
                # attempt keep their position after them (order preserved).
                self._buffer[:0] = lines
                overflow = len(self._buffer) - DEBUG_LOG_BUFFER_CAP
                if overflow > 0:
                    del self._buffer[:overflow]
                    self._dropped_count += overflow
                if self._fail_count % _WARN_EVERY_N_FAILURES == 0:
                    _LOGGER.warning(
                        "DebugLogger: %d consecutive flush failures (latest: %s) — "
                        "check file permissions for %s",
                        self._fail_count,
                        err,
                        self.file_path,
                    )
                # Retry via the age timer only — never an immediate task, to
                # avoid a hot retry loop while the disk stays broken.
                if self._cancel_flush_timer is None:
                    self._arm_timer()
            else:
                self._fail_count = 0
                self._dropped_count = 0

    def _write_lines(self, lines: list[str]) -> None:
        """Executor job: rotate the active file if oversized, then append.

        Runs OFF the event loop. ``os.replace`` is atomic for same-filesystem
        renames; any previous ``.1`` generation is replaced (FR-011).
        Raises OSError to the caller on failure.
        """
        path = self.file_path
        if os.path.exists(path) and os.path.getsize(path) > DEBUG_LOG_MAX_BYTES:
            os.replace(path, self.rotated_file_path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.writelines(lines)
