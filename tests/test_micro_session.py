"""Tests for micro-session filtering (T016)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.ev_charging_manager.const import (
    DOMAIN,
    EVENT_SESSION_COMPLETED,
    EVENT_SESSION_STARTED,
    SessionEngineState,
)
from tests.conftest import (
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    setup_session_engine,
    start_charging_session,
    stop_charging_session,
)

# Module path for patching dt_util in the session engine
_ENGINE_MODULE = "custom_components.ev_charging_manager.session_engine"


async def test_micro_session_under_duration_discarded(hass: HomeAssistant):
    """Session < 60s (with >= 50 Wh) is discarded — no completed event."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)
    started_events = async_capture_events(hass, EVENT_SESSION_STARTED)

    await setup_session_engine(hass, entry)
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="0")

    # 100 Wh > 50 Wh — enough energy, but < 60s duration (no time advance)
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.1")
    await hass.async_block_till_done()

    # End session immediately (< 60s)
    await stop_charging_session(hass)

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.IDLE
    assert len(completed_events) == 0
    assert len(started_events) == 1  # started event still fired


async def test_micro_session_under_energy_discarded(hass: HomeAssistant):
    """Session > 60s but < 50 Wh is discarded."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    session_start = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)

    with patch(f"{_ENGINE_MODULE}.dt_util") as mock_dt:
        mock_dt.utcnow.return_value = session_start

        await setup_session_engine(hass, entry)
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
        await start_charging_session(hass, trx_value="0")

        # Advance time by 120s
        mock_dt.utcnow.return_value = session_start + timedelta(seconds=120)

        # Very little energy: 0.01 kWh = 10 Wh < 50 Wh threshold
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.01")
        await hass.async_block_till_done()

        await stop_charging_session(hass)

    assert len(completed_events) == 0


async def test_valid_session_above_thresholds_persisted(hass: HomeAssistant):
    """Session > 60s AND > 50 Wh is persisted and fires completed event."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    session_start = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)

    with patch(f"{_ENGINE_MODULE}.dt_util") as mock_dt:
        mock_dt.utcnow.return_value = session_start

        await setup_session_engine(hass, entry)
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
        await start_charging_session(hass, trx_value="0")

        # Advance time by 120s
        mock_dt.utcnow.return_value = session_start + timedelta(seconds=120)

        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.1")  # 100 Wh > 50 Wh
        await hass.async_block_till_done()

        await stop_charging_session(hass)

    assert len(completed_events) == 1
    event_data = completed_events[0].data
    assert event_data["energy_kwh"] > 0


async def test_session_at_exact_thresholds_is_valid(hass: HomeAssistant):
    """Session above min_duration and min_energy is NOT a micro-session."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    session_start = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)

    with patch(f"{_ENGINE_MODULE}.dt_util") as mock_dt:
        mock_dt.utcnow.return_value = session_start

        await setup_session_engine(hass, entry)
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
        await start_charging_session(hass, trx_value="0")

        # Advance time by 120s (> 60s threshold)
        mock_dt.utcnow.return_value = session_start + timedelta(seconds=120)

        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.1")  # 100 Wh > 50 Wh
        await hass.async_block_till_done()

        await stop_charging_session(hass)

    assert len(completed_events) == 1


async def test_started_event_always_fired_for_micro_sessions(hass: HomeAssistant):
    """session_started event fires even for sessions that become micro-sessions."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    started_events = async_capture_events(hass, EVENT_SESSION_STARTED)
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    await setup_session_engine(hass, entry)
    await start_charging_session(hass, trx_value="0")

    # Immediately end (micro-session)
    await stop_charging_session(hass)

    assert len(started_events) == 1
    assert len(completed_events) == 0
