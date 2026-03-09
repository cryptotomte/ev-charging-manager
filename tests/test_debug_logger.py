"""Tests for DebugLogger, options flow debug_logging toggle, and SessionEngine hooks."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.const import DOMAIN
from custom_components.ev_charging_manager.debug_logger import DebugLogger
from tests.conftest import (
    MOCK_CAR_STATUS_ENTITY,
    MOCK_CHARGER_DATA,
    MOCK_TRX_ENTITY,
    setup_session_engine,
)

# ---------------------------------------------------------------------------
# T004: Unit tests for DebugLogger.enable()
# ---------------------------------------------------------------------------


def test_enable_creates_www_dir(tmp_path):
    """enable() creates www/ directory if it does not exist."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    logger = DebugLogger(config_dir)

    logger.enable()

    www_dir = os.path.join(config_dir, "www")
    assert os.path.isdir(www_dir), "www/ directory should be created by enable()"


def test_enable_writes_debug_on_line(tmp_path):
    """enable() writes a DEBUG_ON line to the log file."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    logger = DebugLogger(config_dir)

    logger.enable()

    content = open(logger.file_path, encoding="utf-8").read()
    assert "DEBUG_ON" in content
    assert "Debug logging enabled" in content


def test_enable_sets_enabled_flag(tmp_path):
    """enable() sets _enabled = True."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    logger = DebugLogger(config_dir)

    assert not logger.enabled
    logger.enable()
    assert logger.enabled


def test_log_appends_after_enable(tmp_path):
    """log() appends lines to the file after enable() is called."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    logger = DebugLogger(config_dir)

    logger.enable()
    logger.log("CAR_STATE", "car_value changed: Idle → Charging")
    logger.log("SESSION_START", "session_id=abc123 user=Petra charger=goe_409787")

    content = open(logger.file_path, encoding="utf-8").read()
    lines = content.strip().splitlines()

    # DEBUG_ON + 2 log lines
    assert len(lines) == 3
    assert "CAR_STATE" in lines[1]
    assert "car_value changed: Idle → Charging" in lines[1]
    assert "SESSION_START" in lines[2]


def test_log_noop_before_enable(tmp_path):
    """log() is a no-op when enable() has not been called."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    logger = DebugLogger(config_dir)

    logger.log("CAR_STATE", "car_value changed: Idle → Charging")

    assert not os.path.exists(logger.file_path)


def test_log_line_format(tmp_path):
    """log() writes lines with the correct format: timestamp | category | message."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    logger = DebugLogger(config_dir)

    logger.enable()
    logger.log("CAR_STATE", "test message")

    lines = open(logger.file_path, encoding="utf-8").read().strip().splitlines()
    # Find a CAR_STATE line
    car_line = next(ln for ln in lines if "CAR_STATE" in ln)

    # Should match: YYYY-MM-DDTHH:MM:SS.mmm | CATEGORY        | message
    parts = car_line.split(" | ")
    assert len(parts) == 3
    assert len(parts[0]) == 23  # timestamp with milliseconds
    assert parts[1] == "CAR_STATE      "  # padded to 15 chars (left-aligned)
    assert parts[2] == "test message"


def test_enable_idempotent_www_dir(tmp_path):
    """enable() does not fail if www/ already exists (exist_ok=True)."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    www_dir = os.path.join(config_dir, "www")
    os.makedirs(www_dir)  # pre-create
    logger = DebugLogger(config_dir)

    # Should not raise
    logger.enable()
    assert logger.enabled


# ---------------------------------------------------------------------------
# T005: Unit tests for DebugLogger.log() fail-counter
# ---------------------------------------------------------------------------


def test_log_silent_on_oserror(tmp_path):
    """log() does not raise on OSError — failure is silent (count < 5)."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    logger = DebugLogger(config_dir)
    logger.enable()

    # Patch open to raise OSError
    with patch("builtins.open", side_effect=OSError("disk full")):
        # Should not raise
        for _ in range(4):
            logger.log("CAR_STATE", "test")

    assert logger._fail_count == 4


def test_log_warns_every_5th_failure(tmp_path, caplog):
    """log() emits a warning exactly on every 5th consecutive failure."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    logger = DebugLogger(config_dir)
    logger.enable()

    import logging

    with patch("builtins.open", side_effect=OSError("disk full")):
        with caplog.at_level(logging.WARNING):
            for _ in range(10):
                logger.log("CAR_STATE", "test")

    # Warnings should appear at failures 5 and 10
    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_messages) == 2
    assert "5" in warning_messages[0] or "consecutive" in warning_messages[0]


def test_log_resets_fail_count_on_success(tmp_path, caplog):
    """_fail_count resets to 0 after a successful write."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    logger = DebugLogger(config_dir)
    logger.enable()

    # Force 3 failures
    with patch("builtins.open", side_effect=OSError("disk full")):
        for _ in range(3):
            logger.log("CAR_STATE", "test")

    assert logger._fail_count == 3

    # Successful write should reset counter
    logger.log("CAR_STATE", "success")
    assert logger._fail_count == 0


# ---------------------------------------------------------------------------
# T015: Unit tests for DebugLogger.clear()
# ---------------------------------------------------------------------------


def test_clear_noop_when_file_missing(tmp_path):
    """clear() is a no-op when the file does not exist — no error raised."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    logger = DebugLogger(config_dir)

    # Should not raise, even though file and www/ dir don't exist
    logger.clear()


def test_clear_truncates_file_with_logging_on(tmp_path):
    """clear() truncates file and writes DEBUG_CLEAR when enabled."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    logger = DebugLogger(config_dir)

    logger.enable()
    logger.log("CAR_STATE", "some event")
    logger.log("SESSION_START", "session started")

    # Verify content before clear
    content_before = open(logger.file_path, encoding="utf-8").read()
    assert "CAR_STATE" in content_before

    logger.clear()

    content_after = open(logger.file_path, encoding="utf-8").read()
    assert "CAR_STATE" not in content_after
    assert "DEBUG_CLEAR" in content_after
    assert "Log cleared by user" in content_after


def test_clear_truncates_file_with_logging_off(tmp_path):
    """clear() truncates file but does NOT write DEBUG_CLEAR when disabled."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    logger = DebugLogger(config_dir)

    # Enable, write content, then disable
    logger.enable()
    logger.log("SESSION_START", "started")
    logger.disable()

    # File exists at this point (has DEBUG_ON, SESSION_START, DEBUG_OFF)
    content_before = open(logger.file_path, encoding="utf-8").read()
    assert "SESSION_START" in content_before

    logger.clear()

    content_after = open(logger.file_path, encoding="utf-8").read()
    assert "SESSION_START" not in content_after
    assert "DEBUG_CLEAR" not in content_after
    # File should be empty after clear with logging off
    assert content_after == ""


# ---------------------------------------------------------------------------
# T020: Unit tests for DebugLogger.disable()
# ---------------------------------------------------------------------------


def test_disable_writes_debug_off_when_enabled(tmp_path):
    """disable() writes a DEBUG_OFF line when logging is active."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    logger = DebugLogger(config_dir)

    logger.enable()
    logger.disable()

    content = open(logger.file_path, encoding="utf-8").read()
    assert "DEBUG_OFF" in content
    assert "Debug logging disabled" in content


def test_disable_clears_enabled_flag(tmp_path):
    """disable() sets _enabled = False."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    logger = DebugLogger(config_dir)

    logger.enable()
    assert logger.enabled

    logger.disable()
    assert not logger.enabled


def test_log_noop_after_disable(tmp_path):
    """log() is a no-op after disable() — no new lines added."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    logger = DebugLogger(config_dir)

    logger.enable()
    logger.disable()

    lines_before = open(logger.file_path, encoding="utf-8").read().strip().splitlines()
    count_before = len(lines_before)

    logger.log("CAR_STATE", "should not be written")

    lines_after = open(logger.file_path, encoding="utf-8").read().strip().splitlines()
    assert len(lines_after) == count_before


def test_disable_preserves_file_content(tmp_path):
    """disable() does not truncate or delete existing file content."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    logger = DebugLogger(config_dir)

    logger.enable()
    logger.log("SESSION_START", "session_id=abc123 user=Petra charger=goe")
    logger.log("ENGINE_DECISION", "IDLE → TRACKING (trigger: car_state=Charging)")

    content_before_disable = open(logger.file_path, encoding="utf-8").read()

    logger.disable()

    content_after_disable = open(logger.file_path, encoding="utf-8").read()

    # All original lines must still be present
    assert "SESSION_START" in content_after_disable
    assert "ENGINE_DECISION" in content_after_disable
    # DEBUG_ON line preserved
    assert "DEBUG_ON" in content_after_disable
    # Content only grew (DEBUG_OFF appended)
    assert len(content_after_disable) >= len(content_before_disable)


def test_disable_noop_when_already_disabled(tmp_path):
    """disable() is a no-op when already disabled — no error, no file created."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    logger = DebugLogger(config_dir)

    # Never enabled — file should not be created
    logger.disable()

    assert not os.path.exists(logger.file_path)


# ---------------------------------------------------------------------------
# T014: Integration test — debug_logging=True → log file contains CAR_STATE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_car_state_logged(hass, tmp_path):
    """With debug_logging=True, a car_value state change produces a CAR_STATE log line."""
    # Point HA config_dir to tmp_path so the log file lands there
    hass.config.config_dir = str(tmp_path)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={"debug_logging": True},
        title="My go-e Charger",
    )

    await setup_session_engine(hass, entry)

    # Simulate car state change to Charging (with a valid trx to start a session)
    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    # Retrieve the debug_logger from hass.data
    debug_logger = hass.data[DOMAIN][entry.entry_id].get("debug_logger")
    assert debug_logger is not None, "debug_logger should be stored in hass.data"

    log_path = debug_logger.file_path
    assert os.path.exists(log_path), "Log file should be created"

    content = open(log_path, encoding="utf-8").read()
    assert "CAR_STATE" in content, f"Expected CAR_STATE in log:\n{content}"
    assert "DEBUG_ON" in content


# ---------------------------------------------------------------------------
# T021: Integration test — debug_logging=False → no new lines added after disable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_no_logging_when_disabled(hass, tmp_path):
    """With debug_logging=False (default), no log file is created."""
    hass.config.config_dir = str(tmp_path)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        # options not set → debug_logging defaults to False
        title="My go-e Charger",
    )

    await setup_session_engine(hass, entry)

    # Simulate state changes
    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    debug_logger = hass.data[DOMAIN][entry.entry_id].get("debug_logger")
    assert debug_logger is not None

    # File should not exist since logging is disabled
    assert not os.path.exists(debug_logger.file_path), (
        "Log file should NOT be created when debug_logging=False"
    )


# ---------------------------------------------------------------------------
# T022: Integration test — enable → log → disable → enable → log → append
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_reenable_appends_not_overwrites(hass, tmp_path):
    """Re-enabling after disable appends new events; old content is preserved."""
    config_dir = str(tmp_path)
    logger = DebugLogger(config_dir)

    # First enable/log/disable cycle
    logger.enable()
    logger.log("SESSION_START", "session_id=first")
    logger.disable()

    content_after_first = open(logger.file_path, encoding="utf-8").read()
    assert "SESSION_START" in content_after_first
    assert "session_id=first" in content_after_first

    # Second enable/log cycle
    logger.enable()
    logger.log("SESSION_START", "session_id=second")

    content_after_second = open(logger.file_path, encoding="utf-8").read()
    # Both batches should be present
    assert "session_id=first" in content_after_second, (
        "First batch must be preserved after re-enable"
    )
    assert "session_id=second" in content_after_second, (
        "Second batch must be present after re-enable"
    )
    # Second appears after first in the file
    assert content_after_second.index("session_id=first") < content_after_second.index(
        "session_id=second"
    )


# ---------------------------------------------------------------------------
# T023: Integration test — reload preserves logging across options update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_reload_continues_logging(hass, tmp_path):
    """After entry reload with debug_logging=True, DEBUG_ON is present and logging works."""
    hass.config.config_dir = str(tmp_path)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={"debug_logging": True},
        title="My go-e Charger",
    )

    await setup_session_engine(hass, entry)

    # Reload the entry (simulates options update)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    debug_logger = hass.data[DOMAIN][entry.entry_id].get("debug_logger")
    assert debug_logger is not None
    assert debug_logger.enabled, "Logger should be enabled after reload with debug_logging=True"

    log_path = debug_logger.file_path
    assert os.path.exists(log_path), "Log file should exist after reload"

    content = open(log_path, encoding="utf-8").read()
    assert "DEBUG_ON" in content
