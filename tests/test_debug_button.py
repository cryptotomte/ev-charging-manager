"""Integration tests for ClearDebugLogButton entity (US2, PR-28 async clear path)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ev_charging_manager.const import DOMAIN
from tests.conftest import MOCK_CHARGER_DATA, setup_session_engine


async def _flush_by_time(hass: HomeAssistant) -> None:
    """Advance past the age-flush threshold and drain pending tasks (PR-28)."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# T019: Integration test — press button → log file truncated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_button_press_clears_log_file_with_logging_on(hass: HomeAssistant, tmp_path):
    """Pressing the button truncates the log file and writes DEBUG_CLEAR when enabled."""
    hass.config.config_dir = str(tmp_path)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={"debug_logging": True},
        title="My go-e Charger",
    )

    await setup_session_engine(hass, entry)

    debug_logger = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
    assert debug_logger is not None
    assert debug_logger.enabled

    # Write some additional content and flush it to disk
    debug_logger.log("CAR_STATE", "car_value changed: Idle → Charging")
    debug_logger.log("SESSION_START", "session_id=test123")
    await _flush_by_time(hass)

    content_before = open(debug_logger.file_path, encoding="utf-8").read()
    assert "SESSION_START" in content_before

    # Press via the service call
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.my_go_e_charger_clear_debug_log"},
        blocking=True,
    )
    await hass.async_block_till_done()
    # The DEBUG_CLEAR marker is buffered by async_clear — flush it to disk
    await _flush_by_time(hass)

    content_after = open(debug_logger.file_path, encoding="utf-8").read()
    # Old content should be gone
    assert "SESSION_START" not in content_after
    assert "DEBUG_CLEAR" in content_after
    assert "Log cleared by user" in content_after


@pytest.mark.asyncio
async def test_button_entity_registered(hass: HomeAssistant, tmp_path):
    """ClearDebugLogButton is registered as a button entity after setup."""
    hass.config.config_dir = str(tmp_path)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={"debug_logging": True},
        title="My go-e Charger",
    )

    await setup_session_engine(hass, entry)

    # Check entity registry for the button
    from homeassistant.helpers import entity_registry as er

    ent_registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(ent_registry, entry.entry_id)
    button_entries = [e for e in entries if e.domain == "button"]

    assert len(button_entries) == 1, f"Expected 1 button entity, got {len(button_entries)}"
    assert "clear_debug_log" in button_entries[0].unique_id


@pytest.mark.asyncio
async def test_button_clears_log_when_logging_off(hass: HomeAssistant, tmp_path):
    """async_clear() with logging OFF truncates but does not write DEBUG_CLEAR."""
    hass.config.config_dir = str(tmp_path)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={"debug_logging": True},
        title="My go-e Charger",
    )

    await setup_session_engine(hass, entry)

    debug_logger = hass.data[DOMAIN][entry.entry_id]["debug_logger"]

    # Write some content then disable (disable flushes the buffer to disk)
    debug_logger.log("CAR_STATE", "test event")
    await debug_logger.async_disable()

    content_before = open(debug_logger.file_path, encoding="utf-8").read()
    assert "CAR_STATE" in content_before

    # Manually call async_clear (simulating button press while disabled)
    await debug_logger.async_clear()
    await _flush_by_time(hass)

    content_after = open(debug_logger.file_path, encoding="utf-8").read()
    # Old content should be gone, no DEBUG_CLEAR because logging is off
    assert "CAR_STATE" not in content_after
    assert "DEBUG_CLEAR" not in content_after
    assert content_after == ""
