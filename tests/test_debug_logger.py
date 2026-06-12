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


def _read_text(logger: DebugLogger) -> str:
    with open(logger.file_path, encoding="utf-8") as fh:
        return fh.read()


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
    content = _read_text(logger)
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

    content = _read_text(logger)
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


async def test_double_disable_writes_single_debug_off(hass: HomeAssistant, tmp_path) -> None:
    """Review F1: async_disable() is idempotent — the EVENT_HOMEASSISTANT_STOP
    listener and the unload callback may both call it; exactly one DEBUG_OFF."""
    logger = await _make_enabled_logger(hass, tmp_path)
    logger.log("CAR_STATE", "before double disable")

    await logger.async_disable()
    await logger.async_disable()

    lines = _read_lines(logger)
    assert sum("DEBUG_OFF" in ln for ln in lines) == 1
    assert "DEBUG_OFF" in lines[-1]


async def test_failed_final_flush_abandons_lines_terminally(
    hass: HomeAssistant, tmp_path, caplog
) -> None:
    """Review F3: when the final flush in async_disable() fails, the instance
    closes terminally — buffer abandoned with ONE unconditional WARNING naming
    the line count, retry timer cancelled and never re-armed (no zombie 5 s
    loop on the abandoned instance)."""
    logger = await _make_enabled_logger(hass, tmp_path)
    await _flush_by_time(hass)  # DEBUG_ON to disk

    logger.log("CAR_STATE", "doomed-line")
    with (
        patch("builtins.open", side_effect=OSError("disk full")),
        caplog.at_level(logging.WARNING, logger=debug_logger_module.__name__),
    ):
        await logger.async_disable()

    abandoned = [r for r in caplog.records if "abandoned" in r.message]
    assert len(abandoned) == 1, "Exactly one unconditional abandonment warning"
    assert "2" in abandoned[0].message  # doomed-line + DEBUG_OFF marker

    # Terminal state: buffer cleared, no timer armed, no flush pending
    assert logger._buffer == []
    assert logger._cancel_flush_timer is None
    assert not logger._flush_scheduled

    # Time passing produces no zombie writes (disk is healthy again here)
    count_before = len(_read_lines(logger))
    await _flush_by_time(hass)
    assert len(_read_lines(logger)) == count_before
    assert "doomed-line" not in _read_text(logger)


async def test_debug_off_final_under_concurrent_log(hass: HomeAssistant, tmp_path, caplog) -> None:
    """Review F8a: a log() call arriving mid-flush while async_disable() is
    writing the final lines is discarded AT THE SOURCE by the _closed flag
    (review F3) — that line never reaches the file or the buffer (so it is
    not even "abandoned"), and DEBUG_OFF stays the final line (FR-008)."""
    logger = await _make_enabled_logger(hass, tmp_path)
    logger.log("CAR_STATE", "before disable")

    original_write = logger._write_lines

    def write_with_concurrent_log(lines: list[str]) -> None:
        # Fires inside the executor job of the FINAL flush — simulates a
        # callback racing the shutdown. _closed is already True here.
        logger.log("CAR_STATE", "late line during final flush")
        original_write(lines)

    with (
        patch.object(logger, "_write_lines", side_effect=write_with_concurrent_log),
        caplog.at_level(logging.WARNING, logger=debug_logger_module.__name__),
    ):
        await logger.async_disable()

    lines = _read_lines(logger)
    assert not any("late line during final flush" in ln for ln in lines), (
        "A log() call during the final flush must never reach the file"
    )
    assert "DEBUG_OFF" in lines[-1]
    assert logger._buffer == [], "The racing line must never enter the buffer"
    # Discarded at log(), not abandoned by async_disable's post-flush sweep
    assert not any("abandoned" in r.message for r in caplog.records)

    # Nothing pending that could write after close
    await _flush_by_time(hass)
    assert "DEBUG_OFF" in _read_lines(logger)[-1]


async def test_log_after_disable_no_buffer_growth(hass: HomeAssistant, tmp_path) -> None:
    """Review F3: log() on a closed instance never grows the buffer."""
    logger = await _make_enabled_logger(hass, tmp_path)
    await logger.async_disable()

    logger.log("CAR_STATE", "after close")

    assert logger._buffer == []
    assert logger._cancel_flush_timer is None


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

    content = _read_text(logger)
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

    content = _read_text(logger)
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
    assert _read_text(logger) == ""
    assert logger._buffer != []  # DEBUG_CLEAR marker buffered

    await _flush_by_time(hass)
    content = _read_text(logger)
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

    assert _read_text(logger) == ""


async def test_clear_failure_rebuffers_pending_lines(hass: HomeAssistant, tmp_path) -> None:
    """Review F2: when the clear's executor write fails, the drained lines go
    back into the buffer (the truncation failed anyway) and reach the file on
    a later successful flush instead of being silently lost."""
    logger = await _make_enabled_logger(hass, tmp_path)
    await _flush_by_time(hass)
    logger.log("CAR_STATE", "pending-at-clear")

    with patch("builtins.open", side_effect=OSError("disk full")):
        await logger.async_clear()

    assert any("pending-at-clear" in ln for ln in logger._buffer), (
        "Drained lines must be re-buffered when the clear write fails"
    )

    await _flush_by_time(hass)

    content = _read_text(logger)
    assert "pending-at-clear" in content
    assert logger._buffer == []


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

    content = _read_text(logger)
    assert "retained-1" in content
    assert "retained-2" in content
    assert content.index("retained-1") < content.index("retained-2")


async def test_first_flush_failure_warns_immediately(hass: HomeAssistant, tmp_path, caplog) -> None:
    """Review F4: the FIRST failure of a failure streak emits a warning right
    away — a single failed flush must never be silent."""
    logger = await _make_enabled_logger(hass, tmp_path)
    await _flush_by_time(hass)

    with (
        patch("builtins.open", side_effect=OSError("disk full")),
        caplog.at_level(logging.WARNING, logger=debug_logger_module.__name__),
    ):
        logger.log("CAR_STATE", "single failure")
        await _flush_by_time(hass)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "Exactly one warning on the first failure"
    assert "flush" in warnings[0].message
    assert logger.file_path in warnings[0].message


async def test_flush_failure_warning_throttled(hass: HomeAssistant, tmp_path, caplog) -> None:
    """Repeat failures are throttled: warn on the 1st (review F4) and every
    5th consecutive failure — not on each one."""
    logger = await _make_enabled_logger(hass, tmp_path)
    await _flush_by_time(hass)

    with patch("builtins.open", side_effect=OSError("disk full")):
        with caplog.at_level(logging.WARNING, logger=debug_logger_module.__name__):
            for i in range(5):
                logger.log("CAR_STATE", f"fail {i}")
                await _flush_by_time(hass)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2  # failure #1 (immediate) + failure #5 (throttled)
    assert "consecutive" in warnings[1].message
    assert "5" in warnings[1].message


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

    content = _read_text(logger)
    assert "5 lines dropped" in content
    assert "cap-line-04" not in content
    assert "cap-line-05" in content
    assert "cap-line-14" in content
    # The note precedes the retained lines (chronological position of the gap)
    assert content.index("lines dropped") < content.index("cap-line-05")
    assert logger._buffer == []


async def test_dropped_count_sums_across_multiple_capped_cycles(
    hass: HomeAssistant, tmp_path, monkeypatch
) -> None:
    """Review F8c: two capped failure cycles before recovery — the single
    DEBUG_DROPPED note on the next successful flush counts the SUM of all
    lines dropped across both cycles (FR-010)."""
    monkeypatch.setattr(debug_logger_module, "DEBUG_LOG_BUFFER_CAP", 10)
    logger = await _make_enabled_logger(hass, tmp_path)
    await _flush_by_time(hass)  # DEBUG_ON to disk

    with patch("builtins.open", side_effect=OSError("disk full")):
        # Cycle 1: 15 lines, failed flush drops the 5 oldest
        for i in range(15):
            logger.log("CAR_STATE", f"c1-line-{i:02d}")
        await _flush_by_time(hass)
        assert logger._dropped_count == 5

        # Cycle 2: 5 more lines (buffer back at 15), failed flush drops 5 more
        for i in range(5):
            logger.log("CAR_STATE", f"c2-line-{i:02d}")
        await _flush_by_time(hass)
        assert logger._dropped_count == 10

    # Recovery: one note carrying the summed count
    await _flush_by_time(hass)

    content = _read_text(logger)
    assert "10 lines dropped" in content
    assert content.count("DEBUG_DROPPED") == 1
    # Survivors: c1-10..c1-14 + all of cycle 2; c1-09 and older are gone
    assert "c1-line-09" not in content
    assert "c1-line-10" in content
    assert "c2-line-04" in content
    assert logger._dropped_count == 0


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
    """Short NON-numeric values are fully masked — last-2 would echo the
    whole value."""
    assert redact_tag("") == "***"
    assert redact_tag("a") == "***"
    assert redact_tag("ab") == "***"
    assert redact_tag("3a") == "***"


def test_redact_tag_single_digit_slot_index_unmasked() -> None:
    """Review F7: pure single-digit values are go-e card SLOT INDICES (0-9,
    '0' = auth required) — not the persistent personal identifier FR-004
    protects. They render literally so TRX_STATE transitions stay readable."""
    assert redact_tag("0") == "0"
    assert redact_tag("9") == "9"
    assert redact_tag(2) == "2"
    # Two-digit numeric values are NOT exempted — only ^\d$ passes through
    assert redact_tag("10") == "***"


def test_redact_tag_sentinel_boundary() -> None:
    """Review F7 boundary pin: this function masks unconditionally — sentinel
    values like 'null' are exempted by CALLERS via _INVALID_STATES, never
    here. A near-sentinel like 'nullx' must stay masked."""
    assert redact_tag("nullx") == "***lx"
    assert redact_tag("null") == "***ll"


def test_redact_tag_long_value_keeps_last_two() -> None:
    """Longer values show *** plus the last two characters."""
    assert redact_tag("abc123f4") == "***f4"
    assert redact_tag("04:B7:C8:D2:E1:F3:A2") == "***A2"


async def test_legacy_engine_rfid_read_redacted(hass: HomeAssistant, tmp_path, caplog) -> None:
    """Legacy SessionEngine: RFID_READ log lines carry the masked tag,
    never the full value (FR-004 crosses the engine freeze). Review F8b: the
    session-start _LOGGER.info line in the HA core log is masked too."""
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

    with caplog.at_level(logging.INFO):
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

    # Review F8b: the session-start info line reaches the HA core log with
    # the masked tag only
    assert "Session started" in caplog.text
    assert "trx=***f4" in caplog.text
    assert "abc123f4" not in caplog.text, "Full RFID tag must never reach the HA core log"


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


async def test_rotation_failure_falls_back_to_append(hass: HomeAssistant, tmp_path, caplog) -> None:
    """Review F5: a failing os.replace must not halt logging — lines are
    appended to the oversized active file (bounded unrotated growth beats
    infinite drop), ONE rotation warning names the .1 path per streak, and
    rotation is retried on subsequent flushes."""
    logger = await _make_enabled_logger(hass, tmp_path)
    await _flush_by_time(hass)  # DEBUG_ON to disk

    with open(logger.file_path, "a", encoding="utf-8") as fh:
        fh.write("x" * (debug_logger_module.DEBUG_LOG_MAX_BYTES + 1))

    with (
        patch(
            "custom_components.ev_charging_manager.debug_logger.os.replace",
            side_effect=OSError("permission denied"),
        ),
        caplog.at_level(logging.WARNING, logger=debug_logger_module.__name__),
    ):
        logger.log("CAR_STATE", "after rotation failure 1")
        await _flush_by_time(hass)
        logger.log("CAR_STATE", "after rotation failure 2")
        await _flush_by_time(hass)

    # Lines still landed in the (oversized) active file — no drop mode
    content = _read_text(logger)
    assert "after rotation failure 1" in content
    assert "after rotation failure 2" in content

    # ONE warning per streak, naming the rotated (.1) path — not the active file
    warnings = [r for r in caplog.records if "rotate" in r.message]
    assert len(warnings) == 1
    assert logger.rotated_file_path in warnings[0].message

    # Rotation is retried: with os.replace healthy again the next flush rotates
    logger.log("CAR_STATE", "after rotation recovery")
    await _flush_by_time(hass)
    assert os.path.exists(logger.rotated_file_path)
    active_lines = _read_lines(logger)
    assert len(active_lines) == 1
    assert "after rotation recovery" in active_lines[0]


async def test_clear_truncates_active_only_dot1_remains(hass: HomeAssistant, tmp_path) -> None:
    """async_clear() truncates the active file only — the .1 generation remains
    (FR-012 / US4 scenario 3)."""
    logger = await _make_enabled_logger(hass, tmp_path)
    logger.log("CAR_STATE", "active content")
    await _flush_by_time(hass)

    with open(logger.rotated_file_path, "w", encoding="utf-8") as fh:
        fh.write("ROTATED GENERATION\n")

    await logger.async_clear()

    assert _read_text(logger) == ""
    assert open(logger.rotated_file_path, encoding="utf-8").read() == "ROTATED GENERATION\n"
