"""DebugLogger — optional diagnostic file writer for EV Charging Manager."""

from __future__ import annotations

import logging
import os
from datetime import datetime

_LOGGER = logging.getLogger(__name__)

# Log file name written under {config_dir}/www/
_FILE_NAME = "ev_charging_manager_debug.log"

# Emit an HA logger warning every N consecutive write failures
_WARN_EVERY_N_FAILURES = 5


class DebugLogger:
    """Write timestamped diagnostic events to a plain-text file in www/.

    The file is accessible via the HA web server at:
        http://<ha-host>:8123/local/ev_charging_manager_debug.log

    Lifecycle
    ---------
    - Instantiated in async_setup_entry with hass.config.config_dir.
    - enable() is called when the debug_logging option is True.
    - disable() is called on integration unload (entry.async_on_unload).
    - A new instance is created on every reload (options change triggers full reload).
    """

    def __init__(self, config_dir: str) -> None:
        """Initialize with the HA configuration directory path."""
        self._config_dir = config_dir
        self._enabled: bool = False
        self._fail_count: int = 0

    @property
    def file_path(self) -> str:
        """Return the absolute path to the log file."""
        return os.path.join(self._config_dir, "www", _FILE_NAME)

    @property
    def enabled(self) -> bool:
        """Return True if debug logging is currently active."""
        return self._enabled

    def enable(self) -> None:
        """Enable debug logging.

        Creates the www/ directory if it does not exist, then writes a DEBUG_ON
        marker line and sets the enabled flag.
        """
        www_dir = os.path.join(self._config_dir, "www")
        os.makedirs(www_dir, exist_ok=True)
        self._enabled = True
        self.log("DEBUG_ON", "Debug logging enabled")

    def disable(self) -> None:
        """Disable debug logging.

        Writes a DEBUG_OFF marker line if currently enabled, then clears the flag.
        Subsequent log() calls become no-ops until enable() is called again.
        """
        if self._enabled:
            # Write the marker before clearing the flag so _write() will execute
            self.log("DEBUG_OFF", "Debug logging disabled")
        self._enabled = False

    def log(self, category: str, message: str) -> None:
        """Append one timestamped line to the log file if enabled.

        On OSError: increments _fail_count and emits an HA logger warning every
        _WARN_EVERY_N_FAILURES consecutive failures. Resets _fail_count on success.

        Args:
            category: Short event category, padded to 15 chars in the output line.
            message:  Free-form human-readable event description.
        """
        if not self._enabled:
            return
        self._write(category, message)

    def clear(self) -> None:
        """Truncate the log file.

        No-op if the file does not exist. After truncation writes a DEBUG_CLEAR
        marker line if logging is currently enabled.
        """
        if not os.path.exists(self.file_path):
            return

        try:
            # Truncate by opening in write mode
            with open(self.file_path, "w", encoding="utf-8"):
                pass
        except OSError as err:
            _LOGGER.warning("DebugLogger: could not truncate log file: %s", err)
            return

        if self._enabled:
            self._write("DEBUG_CLEAR", "Log cleared by user")

    def _write(self, category: str, message: str) -> None:
        """Write a single formatted line to the log file.

        Called internally by log() and clear(). Handles OSError with retry-count
        based warning throttling.
        """
        timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        line = f"{timestamp} | {category:<15} | {message}\n"

        try:
            with open(self.file_path, "a", encoding="utf-8") as fh:
                fh.write(line)
            # Reset consecutive failure counter on success
            self._fail_count = 0
        except OSError as err:
            self._fail_count += 1
            if self._fail_count % _WARN_EVERY_N_FAILURES == 0:
                _LOGGER.warning(
                    "DebugLogger: %d consecutive write failures (latest: %s) — "
                    "check file permissions for %s",
                    self._fail_count,
                    err,
                    self.file_path,
                )
