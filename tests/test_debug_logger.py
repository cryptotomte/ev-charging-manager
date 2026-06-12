"""Tests for the buffered off-loop DebugLogger (PR-28, 024-debug-logger-overhaul).

Covers the rewritten async I/O model:
- sync log() buffers only (no event-loop file I/O)
- count (50 lines) and age (5 s) flush triggers, emission order preserved
- async_disable flushes with DEBUG_OFF as the final line
- async_clear: flush -> truncate -> DEBUG_CLEAR, off-loop
- flush OSError: lines retained, throttled warning, buffer cap drop-oldest,
  dropped-count note on recovery
- line format byte-identical to the pre-PR-28 logger (SC-006)

Line-format and category pins are carried over from the old test suite so
content compatibility is proven, not assumed.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.ev_charging_manager import debug_logger as debug_logger_module
from custom_components.ev_charging_manager.const import DEBUG_LOG_FLUSH_LINES
from custom_components.ev_charging_manager.debug_logger import (
    DebugLogger,
    async_cleanup_legacy_file,
    redact_tag,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_enabled_logger(hass: HomeAssistant, tmp_path) -> DebugLogger:
    """Return a DebugLogger at tmp_path with logging enabled (DEBUG_ON buffered)."""
    logger = DebugLogger(hass, str(tmp_path))
    await logger.async_enable()
    return logger


async def _flush_by_time(hass: HomeAssistant) -> None:
    """Advance past the age-flush threshold and drain pending tasks."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()


def _read_lines(logger: DebugLogger) -> list[str]:
    with open(logger.file_path, encoding="utf-8") as fh:
        return fh.read().splitlines()


# ---------------------------------------------------------------------------
# T003 (h) + carried-over pins: file location and line format (SC-006)
# ---------------------------------------------------------------------------


async def test_file_path_at_config_root(hass: HomeAssistant, tmp_path) -> None:
    """The log file lives in the config root — never under www/ (FR-001)."""
    logger = DebugLogger(hass, str(tmp_path))

    assert logger.file_path == os.path.join(str(tmp_path), "ev_charging_manager_debug.log")
    assert f"{os.sep}www{os.sep}" not in logger.file_path


async def test_log_line_format_byte_identical(hass: HomeAssistant, tmp_path) -> None:
    """Line format is unchanged: 'YYYY-MM-DDTHH:MM:SS.mmm | CAT<15 | msg' (SC-006)."""
    logger = await _make_enabled_logger(hass, tmp_path)

    logger.log("CAR_STATE", "test message")
    await logger.async_disable()

    lines = _read_lines(logger)
    car_line = next(ln for ln in lines if "CAR_STATE" in ln)

    parts = car_line.split(" | ")
    assert len(parts) == 3
    assert len(parts[0]) == 23  # ISO timestamp with milliseconds
    assert parts[1] == "CAR_STATE      "  # padded to 15 chars (left-aligned)
    assert parts[2] == "test message"


async def test_enable_buffers_debug_on_marker(hass: HomeAssistant, tmp_path) -> None:
    """async_enable() sets the flag and buffers the DEBUG_ON marker line."""
    logger = DebugLogger(hass, str(tmp_path))

    assert not logger.enabled
    await logger.async_enable()
    assert logger.enabled

    await _flush_by_time(hass)
    content = open(logger.file_path, encoding="utf-8").read()
    assert "DEBUG_ON" in content
    assert "Debug logging enabled" in content


async def test_log_noop_before_enable(hass: HomeAssistant, tmp_path) -> None:
    """log() is a no-op when async_enable() has not been called."""
    logger = DebugLogger(hass, str(tmp_path))

    logger.log("CAR_STATE", "should not be buffered")
    await _flush_by_time(hass)

    assert logger._buffer == []
    assert not os.path.exists(logger.file_path)


# ---------------------------------------------------------------------------
# T003 (a): log() from sync context performs NO file I/O on the loop
# ---------------------------------------------------------------------------


async def test_log_performs_no_file_io(hass: HomeAssistant, tmp_path) -> None:
    """log() only appends to the in-memory buffer — no open/stat/rename (FR-005/006)."""
    logger = await _make_enabled_logger(hass, tmp_path)

    with (
        patch("builtins.open") as mock_open,
        patch("os.replace") as mock_replace,
        patch("os.path.getsize") as mock_getsize,
    ):
        logger.log("CAR_STATE", "buffered only")

        mock_open.assert_not_called()
        mock_replace.assert_not_called()
        mock_getsize.assert_not_called()

    assert any("buffered only" in ln for ln in logger._buffer)
    assert not os.path.exists(logger.file_path)


# ---------------------------------------------------------------------------
# T003 (b): 50-line threshold triggers an immediate flush
# ---------------------------------------------------------------------------


async def test_count_threshold_triggers_flush(hass: HomeAssistant, tmp_path) -> None:
    """Reaching DEBUG_LOG_FLUSH_LINES buffered lines flushes immediately (FR-007)."""
    logger = await _make_enabled_logger(hass, tmp_path)
    await _flush_by_time(hass)  # flush the DEBUG_ON marker out of the way

    for i in range(DEBUG_LOG_FLUSH_LINES - 1):
        logger.log("CAR_STATE", f"line {i}")
    await hass.async_block_till_done()

    # 49 buffered lines: below threshold, timer not fired — nothing new on disk
    assert len(_read_lines(logger)) == 1  # DEBUG_ON only

    logger.log("CAR_STATE", f"line {DEBUG_LOG_FLUSH_LINES - 1}")
    await hass.async_block_till_done()

    assert len(_read_lines(logger)) == 1 + DEBUG_LOG_FLUSH_LINES
    assert logger._buffer == []


# ---------------------------------------------------------------------------
# T003 (c): 5 s age trigger flushes a partial buffer
# ---------------------------------------------------------------------------


async def test_age_threshold_triggers_flush(hass: HomeAssistant, tmp_path) -> None:
    """Buffered lines reach disk 5 s after the first buffered line (FR-007)."""
    logger = await _make_enabled_logger(hass, tmp_path)
    await _flush_by_time(hass)

    logger.log("CAR_STATE", "first")
    logger.log("PLUG_STATE", "second")
    await hass.async_block_till_done()

    # No count trigger, timer not fired yet — still buffered
    assert len(_read_lines(logger)) == 1
    assert len(logger._buffer) == 2

    await _flush_by_time(hass)

    lines = _read_lines(logger)
    assert any("first" in ln for ln in lines)
    assert any("second" in ln for ln in lines)
    assert logger._buffer == []


async def test_timestamp_taken_at_buffer_time(hass: HomeAssistant, tmp_path, freezer) -> None:
    """Line timestamps reflect log() call time, not flush time."""
    freezer.move_to("2026-06-12T10:00:00+00:00")
    logger = await _make_enabled_logger(hass, tmp_path)

    logger.log("CAR_STATE", "buffered at ten")
    freezer.tick(timedelta(seconds=30))
    await _flush_by_time(hass)

    line = next(ln for ln in _read_lines(logger) if "buffered at ten" in ln)
    assert line.startswith("2026-06-12T10:00:00"), line


# ---------------------------------------------------------------------------
# T003 (d): emission order preserved across flushes
# ---------------------------------------------------------------------------


async def test_emission_order_preserved_across_flushes(hass: HomeAssistant, tmp_path) -> None:
    """Lines land on disk in emission order across multiple flush cycles (FR-007)."""
    logger = await _make_enabled_logger(hass, tmp_path)

    logger.log("CAR_STATE", "order-1")
    logger.log("CAR_STATE", "order-2")
    await _flush_by_time(hass)
    logger.log("CAR_STATE", "order-3")
    logger.log("CAR_STATE", "order-4")
    await _flush_by_time(hass)

    content = open(logger.file_path, encoding="utf-8").read()
    positions = [content.index(f"order-{i}") for i in (1, 2, 3, 4)]
    assert positions == sorted(positions)


# ---------------------------------------------------------------------------
# T003 (e): async_disable flushes with DEBUG_OFF as the final line
# ---------------------------------------------------------------------------


async def test_disable_flushes_with_debug_off_final(hass: HomeAssistant, tmp_path) -> None:
    """async_disable() flushes the buffer; the DEBUG_OFF marker is the last line (FR-008)."""
    logger = await _make_enabled_logger(hass, tmp_path)

    logger.log("SESSION_START", "session_id=abc123")
    await logger.async_disable()

    lines = _read_lines(logger)
    assert any("SESSION_START" in ln for ln in lines)
    assert "DEBUG_OFF" in lines[-1]
    assert "Debug logging disabled" in lines[-1]
    assert not logger.enabled
    assert logger._buffer == []


async def test_log_noop_after_disable(hass: HomeAssistant, tmp_path) -> None:
    """log() after async_disable() adds nothing — even after a flush window."""
    logger = await _make_enabled_logger(hass, tmp_path)
    await logger.async_disable()

    count_before = len(_read_lines(logger))
    logger.log("CAR_STATE", "should not be written")
    await _flush_by_time(hass)

    assert len(_read_lines(logger)) == count_before


async def test_disable_noop_when_never_enabled(hass: HomeAssistant, tmp_path) -> None:
    """async_disable() without prior enable creates no file and does not raise."""
    logger = DebugLogger(hass, str(tmp_path))

    await logger.async_disable()

    assert not os.path.exists(logger.file_path)


async def test_disable_preserves_file_content(hass: HomeAssistant, tmp_path) -> None:
    """async_disable() appends — it never truncates existing content."""
    logger = await _make_enabled_logger(hass, tmp_path)
    logger.log("SESSION_START", "session_id=abc123")
    logger.log("ENGINE_DECISION", "IDLE → TRACKING")

    await logger.async_disable()

    content = open(logger.file_path, encoding="utf-8").read()
    assert "DEBUG_ON" in content
    assert "SESSION_START" in content
    assert "ENGINE_DECISION" in content
    assert "DEBUG_OFF" in content


async def test_reenable_appends_not_overwrites(hass: HomeAssistant, tmp_path) -> None:
    """Re-enabling after disable appends new events; old content is preserved."""
    logger = await _make_enabled_logger(hass, tmp_path)
    logger.log("SESSION_START", "session_id=first")
    await logger.async_disable()

    await logger.async_enable()
    logger.log("SESSION_START", "session_id=second")
    await logger.async_disable()

    content = open(logger.file_path, encoding="utf-8").read()
    assert "session_id=first" in content
    assert "session_id=second" in content
    assert content.index("session_id=first") < content.index("session_id=second")


# ---------------------------------------------------------------------------
# T003 (f): async_clear — flush, truncate, DEBUG_CLEAR marker, off-loop
# ---------------------------------------------------------------------------


async def test_clear_flushes_then_truncates(hass: HomeAssistant, tmp_path) -> None:
    """async_clear() flushes pending lines, truncates, then buffers DEBUG_CLEAR (FR-012)."""
    logger = await _make_enabled_logger(hass, tmp_path)
    logger.log("CAR_STATE", "on-disk event")
    await _flush_by_time(hass)
    logger.log("PLUG_STATE", "still-buffered event")

    await logger.async_clear()

    # Active file truncated; pre-clear content gone (flushed first, then truncated)
    assert open(logger.file_path, encoding="utf-8").read() == ""
    assert logger._buffer != []  # DEBUG_CLEAR marker buffered

    await _flush_by_time(hass)
    content = open(logger.file_path, encoding="utf-8").read()
    assert "DEBUG_CLEAR" in content
    assert "Log cleared by user" in content
    assert "on-disk event" not in content
    assert "still-buffered event" not in content


async def test_clear_no_marker_when_disabled(hass: HomeAssistant, tmp_path) -> None:
    """async_clear() with logging off truncates but writes no DEBUG_CLEAR marker."""
    logger = await _make_enabled_logger(hass, tmp_path)
    logger.log("CAR_STATE", "some event")
    await logger.async_disable()

    await logger.async_clear()
    await _flush_by_time(hass)

    assert open(logger.file_path, encoding="utf-8").read() == ""


async def test_clear_noop_when_file_missing(hass: HomeAssistant, tmp_path) -> None:
    """async_clear() with no file and nothing buffered is a silent no-op."""
    logger = DebugLogger(hass, str(tmp_path))

    await logger.async_clear()
    await _flush_by_time(hass)

    assert not os.path.exists(logger.file_path)


# ---------------------------------------------------------------------------
# T003 (g): flush OSError — retry, throttled warning, buffer cap, recovery note
# ---------------------------------------------------------------------------


async def test_flush_failure_retains_lines_for_retry(hass: HomeAssistant, tmp_path) -> None:
    """On flush OSError the lines stay buffered and land on the next attempt (FR-010)."""
    logger = await _make_enabled_logger(hass, tmp_path)
    await _flush_by_time(hass)

    with patch("builtins.open", side_effect=OSError("disk full")):
        logger.log("CAR_STATE", "retained-1")
        logger.log("CAR_STATE", "retained-2")
        await _flush_by_time(hass)
        # Flush failed — both lines still buffered, in order
        assert len(logger._buffer) == 2
        assert "retained-1" in logger._buffer[0]
        assert "retained-2" in logger._buffer[1]

    await _flush_by_time(hass)

    content = open(logger.file_path, encoding="utf-8").read()
    assert "retained-1" in content
    assert "retained-2" in content
    assert content.index("retained-1") < content.index("retained-2")


async def test_flush_failure_warning_throttled(hass: HomeAssistant, tmp_path, caplog) -> None:
    """A warning is emitted every 5th consecutive flush failure — not on each one."""
    logger = await _make_enabled_logger(hass, tmp_path)
    await _flush_by_time(hass)

    with patch("builtins.open", side_effect=OSError("disk full")):
        with caplog.at_level(logging.WARNING, logger=debug_logger_module.__name__):
            for i in range(5):
                logger.log("CAR_STATE", f"fail {i}")
                await _flush_by_time(hass)

    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "consecutive" in r.message
    ]
    assert len(warnings) == 1
    assert "5" in warnings[0].message


async def test_failure_count_resets_on_successful_flush(hass: HomeAssistant, tmp_path) -> None:
    """The consecutive-failure counter resets after a successful flush."""
    logger = await _make_enabled_logger(hass, tmp_path)
    await _flush_by_time(hass)

    with patch("builtins.open", side_effect=OSError("disk full")):
        for i in range(3):
            logger.log("CAR_STATE", f"fail {i}")
            await _flush_by_time(hass)
    assert logger._fail_count == 3

    await _flush_by_time(hass)
    assert logger._fail_count == 0


async def test_buffer_capped_drop_oldest_with_recovery_note(
    hass: HomeAssistant, tmp_path, monkeypatch
) -> None:
    """Persistent flush failure caps the buffer drop-oldest; a dropped-count note
    is written on the next successful flush (FR-010)."""
    monkeypatch.setattr(debug_logger_module, "DEBUG_LOG_BUFFER_CAP", 10)
    logger = await _make_enabled_logger(hass, tmp_path)
    await _flush_by_time(hass)

    with patch("builtins.open", side_effect=OSError("disk full")):
        for i in range(15):
            logger.log("CAR_STATE", f"cap-line-{i:02d}")
        await _flush_by_time(hass)

    # Capped at 10: the 5 oldest dropped, the 10 newest retained in order
    assert len(logger._buffer) == 10
    assert "cap-line-05" in logger._buffer[0]
    assert "cap-line-14" in logger._buffer[-1]

    await _flush_by_time(hass)

    content = open(logger.file_path, encoding="utf-8").read()
    assert "5 lines dropped" in content
    assert "cap-line-04" not in content
    assert "cap-line-05" in content
    assert "cap-line-14" in content
    # The note precedes the retained lines (chronological position of the gap)
    assert content.index("lines dropped") < content.index("cap-line-05")
    assert logger._buffer == []


# ---------------------------------------------------------------------------
# T005 (US1): async_cleanup_legacy_file unit tests (FR-002)
# ---------------------------------------------------------------------------


async def test_cleanup_deletes_legacy_file(hass: HomeAssistant, tmp_path, caplog) -> None:
    """The legacy www/ file is deleted and the deletion logged at INFO."""
    legacy = tmp_path / "www" / "ev_charging_manager_debug.log"
    legacy.parent.mkdir()
    legacy.write_text("exposed content\n")

    with caplog.at_level(logging.INFO, logger=debug_logger_module.__name__):
        await async_cleanup_legacy_file(hass, str(tmp_path))

    assert not legacy.exists()
    assert any(str(legacy) in r.message for r in caplog.records)


async def test_cleanup_noop_when_legacy_missing(hass: HomeAssistant, tmp_path, caplog) -> None:
    """No legacy file: cleanup is a silent no-op — no log records, no error."""
    with caplog.at_level(logging.INFO, logger=debug_logger_module.__name__):
        await async_cleanup_legacy_file(hass, str(tmp_path))

    assert caplog.records == []


async def test_cleanup_failure_never_raises(hass: HomeAssistant, tmp_path, caplog) -> None:
    """Deletion failure emits a WARNING naming the path and never raises."""
    legacy = tmp_path / "www" / "ev_charging_manager_debug.log"
    legacy.parent.mkdir()
    legacy.write_text("exposed content\n")

    with (
        patch(
            "custom_components.ev_charging_manager.debug_logger.os.remove",
            side_effect=OSError("permission denied"),
        ),
        caplog.at_level(logging.WARNING, logger=debug_logger_module.__name__),
    ):
        await async_cleanup_legacy_file(hass, str(tmp_path))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert str(legacy) in warnings[0].message


# ---------------------------------------------------------------------------
# T007 (US2): redact_tag — RFID tag values masked in all log lines (FR-004)
# ---------------------------------------------------------------------------


def test_redact_tag_none() -> None:
    """None renders as 'None' — no crash, no spurious mask."""
    assert redact_tag(None) == "None"


def test_redact_tag_short_values_fully_masked() -> None:
    """Values of length <= 2 are fully masked."""
    assert redact_tag("") == "***"
    assert redact_tag("a") == "***"
    assert redact_tag("ab") == "***"
    assert redact_tag(2) == "***"


def test_redact_tag_long_value_keeps_last_two() -> None:
    """Longer values show *** plus the last two characters."""
    assert redact_tag("abc123f4") == "***f4"
    assert redact_tag("04:B7:C8:D2:E1:F3:A2") == "***A2"


async def test_legacy_engine_rfid_read_redacted(hass: HomeAssistant, tmp_path) -> None:
    """Legacy SessionEngine: RFID_READ log lines carry the masked tag,
    never the full value (FR-004 crosses the engine freeze)."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.ev_charging_manager.const import DOMAIN
    from tests.conftest import (
        MOCK_CAR_STATUS_ENTITY,
        MOCK_CHARGER_DATA,
        MOCK_TRX_ENTITY,
        setup_session_engine,
    )

    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={"debug_logging": True},
        title="My go-e Charger",
    )
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_TRX_ENTITY, "abc123f4")
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    debug_logger = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
    await debug_logger.async_disable()  # flush everything emitted so far

    content = open(debug_logger.file_path, encoding="utf-8").read()
    assert "abc123f4" not in content, "Full RFID tag value must never reach the log file"
    rfid_lines = [ln for ln in content.splitlines() if "RFID_READ" in ln]
    assert rfid_lines, "Expected an RFID_READ line"
    assert "tag=***f4" in rfid_lines[0]


# ---------------------------------------------------------------------------
# T009 (US4): rotation at DEBUG_LOG_MAX_BYTES — single .1 generation (FR-011)
# ---------------------------------------------------------------------------


async def test_rotation_at_size_cap_replaces_previous_generation(
    hass: HomeAssistant, tmp_path
) -> None:
    """An oversized active file is rotated to .1 (replacing any previous .1)
    at flush time; the flush's lines land in a fresh active file — no lines
    lost across the rotation (FR-011)."""
    logger = await _make_enabled_logger(hass, tmp_path)
    await _flush_by_time(hass)  # DEBUG_ON marker to disk

    # Pre-existing .1 generation that must be REPLACED (single generation)
    with open(logger.rotated_file_path, "w", encoding="utf-8") as fh:
        fh.write("OLD GENERATION CONTENT\n")

    # Grow the active file past the cap
    with open(logger.file_path, "a", encoding="utf-8") as fh:
        fh.write("x" * (debug_logger_module.DEBUG_LOG_MAX_BYTES + 1))

    logger.log("CAR_STATE", "first line after rotation")
    await _flush_by_time(hass)

    # Active file restarted: contains ONLY the flushed line
    active_lines = _read_lines(logger)
    assert len(active_lines) == 1
    assert "first line after rotation" in active_lines[0]

    # Previous content rolled into .1; the old .1 was replaced
    rotated = open(logger.rotated_file_path, encoding="utf-8").read()
    assert "DEBUG_ON" in rotated
    assert rotated.rstrip("\n").endswith("x" * 10)
    assert "OLD GENERATION CONTENT" not in rotated


async def test_rotation_bounds_disk_usage_to_two_generations(
    hass: HomeAssistant, tmp_path, monkeypatch
) -> None:
    """Repeated flushes past the cap keep exactly two files: active + .1."""
    monkeypatch.setattr(debug_logger_module, "DEBUG_LOG_MAX_BYTES", 200)
    logger = await _make_enabled_logger(hass, tmp_path)

    for i in range(20):
        logger.log("CAR_STATE", f"filler line number {i} with some padding text")
        await _flush_by_time(hass)

    log_files = sorted(p.name for p in tmp_path.iterdir() if "debug.log" in p.name)
    assert log_files == [
        "ev_charging_manager_debug.log",
        "ev_charging_manager_debug.log.1",
    ]
    # Both generations stay bounded: cap + one flush worth of lines
    assert os.path.getsize(logger.file_path) < 400
    assert os.path.getsize(logger.rotated_file_path) < 400


async def test_clear_truncates_active_only_dot1_remains(hass: HomeAssistant, tmp_path) -> None:
    """async_clear() truncates the active file only — the .1 generation remains
    (FR-012 / US4 scenario 3)."""
    logger = await _make_enabled_logger(hass, tmp_path)
    logger.log("CAR_STATE", "active content")
    await _flush_by_time(hass)

    with open(logger.rotated_file_path, "w", encoding="utf-8") as fh:
        fh.write("ROTATED GENERATION\n")

    await logger.async_clear()

    assert open(logger.file_path, encoding="utf-8").read() == ""
    assert open(logger.rotated_file_path, encoding="utf-8").read() == "ROTATED GENERATION\n"
