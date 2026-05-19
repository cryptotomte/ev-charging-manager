"""Log replay helper for EV Charging Manager regression tests.

Reads a fixture log file (one of the captured production debug logs in
tests/fixtures/) and yields (timestamp, category, message) tuples. Also
provides a function to convert these tuples into a sequence of
(entity_id, new_value) state-change pairs suitable for replay against
PlugAnchoredSessionEngine in unit tests.

Log file format (one line per event):
  <ISO8601 timestamp> | <CATEGORY>      | <message>
  # comment lines are skipped

The replay helper does NOT fire actual HA events — callers are responsible
for translating the returned state-change pairs into hass.states.async_set()
calls and then await hass.async_block_till_done().
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Generator

# Pattern for a valid log line: timestamp | category | message
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)\s*\|\s*(?P<cat>[A-Z_]+)\s*\|\s*(?P<msg>.+)$"
)

# Category → (entity signal, value extractor) mapping.
# Only the categories that carry charger signal information are mapped.
# Other categories (SESSION_START, ENGINE_DECISION, CHARGING_WINDOW_*, etc.)
# are informational and do not produce state-change events.
#
# Value extraction uses a regex applied to the message text.
# Returns None if this category should not produce a state-change event.
_CATEGORY_TO_SIGNAL: dict[str, tuple[str, re.Pattern]] = {
    "PLUG_STATE": ("plug", re.compile(r"plug changed: \S+ → (\S+)")),
    "CABLE_LOCK": ("cable_lock", re.compile(r"cus changed: \S+ → (\S+)")),
    "CAR_STATE": ("car_status", re.compile(r"car_value changed: \S+ → (\S+)")),
    "TRX_STATE": ("trx", re.compile(r"trx changed: \S+ → (\S+)")),
    "POWER": ("power", re.compile(r"power changed: \S+ → (\S+)")),
}

# Extract energy from " | wh=<value>" suffix present on many log lines
_WH_RE = re.compile(r"\|\s*wh=(\S+)")


def parse_log_file(
    path: str | Path,
) -> Generator[tuple[datetime, str, str], None, None]:
    """Yield (timestamp, category, message) for each valid log line.

    Skips blank lines and comment lines (starting with #).
    Raises ValueError on malformed non-comment lines.
    """
    path = Path(path)
    with open(path, encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = _LINE_RE.match(line)
            if not m:
                # Non-comment, non-blank line that does not match the expected format.
                # Raise so test failures surface fixture corruption quickly.
                raise ValueError(f"{path.name}:{line_no}: malformed log line: {line!r}")
            ts = datetime.fromisoformat(m.group("ts"))
            yield ts, m.group("cat"), m.group("msg")


def extract_signal_events(
    path: str | Path,
) -> list[tuple[datetime, str, str]]:
    """Return a list of (timestamp, signal_name, value) triples for signal-carrying log lines.

    Only lines with categories in _CATEGORY_TO_SIGNAL produce entries.
    Lines where the value regex does not match are silently skipped.

    Returns a time-ordered list (same order as the log file).

    Signal names correspond to the entity roles used by PlugAnchoredSessionEngine:
      "plug"        → plug binary sensor (values: "on" / "off")
      "cable_lock"  → cable lock sensor (values: "Locked" / "Unlocked" / "unknown" / etc.)
      "car_status"  → car status sensor (values: "Idle" / "Wait for car" / "Charging" / "Complete")
      "trx"         → RFID/transaction selector (values: "null" / "1" / "2" / ...)
      "power"       → power sensor in W (string representation of float)
      "energy"      → derived from wh= suffix on any line (kWh string)
    """
    events: list[tuple[datetime, str, str]] = []

    for ts, cat, msg in parse_log_file(path):
        if cat in _CATEGORY_TO_SIGNAL:
            signal_name, value_re = _CATEGORY_TO_SIGNAL[cat]
            m = value_re.search(msg)
            if m:
                events.append((ts, signal_name, m.group(1)))

        # Also extract energy values from wh= suffix on any line that carries it
        wh_m = _WH_RE.search(msg)
        if wh_m and cat in _CATEGORY_TO_SIGNAL:
            # Attach energy reading at this timestamp if the wh= suffix is present.
            # We only emit the energy event if we also emitted a signal event (same timestamp),
            # to keep the ordering coherent for test replay.
            wh_val = wh_m.group(1)
            try:
                float(wh_val)  # validate it is numeric
                events.append((ts, "energy", wh_val))
            except ValueError:
                pass

    return events


def build_entity_state_sequence(
    log_path: str | Path,
    entity_map: dict[str, str],
) -> list[tuple[str, str]]:
    """Convert signal events from a log fixture into (entity_id, value) pairs.

    Args:
        log_path: Path to the fixture log file.
        entity_map: Maps signal names (e.g. "plug") to HA entity IDs
                    (e.g. "binary_sensor.goe_abc123_car_0").

    Returns:
        Ordered list of (entity_id, value) pairs for use with hass.states.async_set().
        Signals not present in entity_map are skipped.

    Usage in tests:
        states = build_entity_state_sequence(log_path, {
            "plug": "binary_sensor.goe_abc123_car_0",
            "cable_lock": "sensor.goe_abc123_cus_value",
            "car_status": "sensor.goe_abc123_car_value",
            "trx": "select.goe_abc123_trx",
            "power": "sensor.goe_abc123_nrg_11",
            "energy": "sensor.goe_abc123_wh",
        })
        for entity_id, value in states:
            hass.states.async_set(entity_id, value)
            await hass.async_block_till_done()
    """
    result = []
    for _ts, signal, value in extract_signal_events(log_path):
        entity_id = entity_map.get(signal)
        if entity_id is not None:
            result.append((entity_id, value))
    return result


def build_entity_state_sequence_with_timestamps(
    log_path: str | Path,
    entity_map: dict[str, str],
) -> list[tuple[datetime, str, str]]:
    """Convert signal events from a log fixture into (timestamp, entity_id, value) triples.

    Like build_entity_state_sequence but also returns the log timestamp for each event.
    Used by replay tests to advance simulated time between events via freezer.tick().

    Args:
        log_path: Path to the fixture log file.
        entity_map: Maps signal names to HA entity IDs.

    Returns:
        Ordered list of (timestamp, entity_id, value) triples.
        Signals not in entity_map are skipped.
    """
    result = []
    for ts, signal, value in extract_signal_events(log_path):
        entity_id = entity_map.get(signal)
        if entity_id is not None:
            result.append((ts, entity_id, value))
    return result
