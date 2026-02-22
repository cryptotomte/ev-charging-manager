"""Tests for session recovery after HA restart (US1, FR-001 to FR-006, FR-020)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.ev_charging_manager.const import (
    DOMAIN,
    EVENT_SESSION_COMPLETED,
    SessionEngineState,
)
from tests.conftest import (
    MOCK_CAR_STATUS_ENTITY,
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    MOCK_POWER_ENTITY,
    MOCK_TRX_ENTITY,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_active_snapshot(
    session_id: str = "saved-session-001",
    user_name: str = "Petra",
    user_type: str = "regular",
    energy_start_kwh: float = 10.0,
    energy_kwh: float = 3.0,
    rfid_index: int | None = 1,  # card index (trx-1)
    started_at: str = "2026-02-22T08:00:00+00:00",
) -> dict:
    """Create a minimal active session snapshot (ended_at=None)."""
    return {
        "id": session_id,
        "user_name": user_name,
        "user_type": user_type,
        "vehicle_name": None,
        "vehicle_battery_kwh": None,
        "efficiency_factor": None,
        "rfid_index": rfid_index,
        "rfid_uid": None,
        "started_at": started_at,
        "ended_at": None,  # Mark as active snapshot
        "duration_seconds": 0,
        "energy_kwh": energy_kwh,
        "energy_start_kwh": energy_start_kwh,
        "avg_power_w": 0.0,
        "max_power_w": 0.0,
        "phases_used": None,
        "max_current_a": None,
        "cost_total_kr": 0.0,
        "cost_method": "static",
        "price_details": None,
        "charge_price_total_kr": None,
        "charge_price_method": None,
        "estimated_soc_added_pct": None,
        "charger_name": "My go-e Charger",
        "charger_total_before_kwh": None,
        "charger_total_after_kwh": None,
        "data_gap": False,
        "reconstructed": False,
    }


def _make_store_load_side_effect(session_data: list[dict] | None):
    """Return a side_effect function that serves correct data per-store type.

    HA setup creates three Store instances in order:
    1. ConfigStore (expects dict or None)
    2. SessionStore (expects list or None)
    3. StatsStore (expects dict or None)
    """
    call_count = 0
    session_payload = session_data  # may be None

    async def side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None  # ConfigStore: no existing config
        if call_count == 2:
            return session_payload  # SessionStore: our test data
        return None  # StatsStore: no existing stats

    return side_effect


async def _setup_with_snapshot(
    hass: HomeAssistant,
    snapshot: dict | None,
    *,
    car_status: str = "Charging",
    trx_value: str = "2",
    energy_value: str = "13.5",
    power_value: str = "3700.0",
) -> MockConfigEntry:
    """Set up the integration with a pre-existing session snapshot and entity states."""
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, car_status)
    hass.states.async_set(MOCK_TRX_ENTITY, trx_value)
    hass.states.async_set(MOCK_ENERGY_ENTITY, energy_value)
    hass.states.async_set(MOCK_POWER_ENTITY, power_value)

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="My go-e Charger")
    stored_data = [snapshot] if snapshot is not None else None

    with (
        patch(
            "homeassistant.helpers.storage.Store.async_load",
            side_effect=_make_store_load_side_effect(stored_data),
        ),
        patch(
            "homeassistant.helpers.storage.Store.async_save",
            new_callable=AsyncMock,
        ),
    ):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    return entry


# ---------------------------------------------------------------------------
# FR-001 / FR-002: Resume same session (still charging)
# ---------------------------------------------------------------------------


async def test_recovery_same_session_still_charging(hass: HomeAssistant) -> None:
    """FR-002: Session resumes in TRACKING state when charger still charging with same card."""
    snapshot = make_active_snapshot(
        session_id="saved-001",
        rfid_index=1,  # trx=2 → card index 1
        energy_start_kwh=10.0,
        energy_kwh=3.0,
    )
    events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    with (
        patch(
            "homeassistant.helpers.storage.Store.async_load",
            side_effect=_make_store_load_side_effect([snapshot]),
        ),
        patch(
            "homeassistant.helpers.storage.Store.async_save",
            new_callable=AsyncMock,
        ),
    ):
        hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
        hass.states.async_set(MOCK_TRX_ENTITY, "2")  # trx=2 → rfid_index=1 (same)
        hass.states.async_set(MOCK_ENERGY_ENTITY, "13.5")  # 10.0 start + 3.5 new
        hass.states.async_set(MOCK_POWER_ENTITY, "3700.0")

        entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="go-e")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.TRACKING, "Engine should be in TRACKING state"
    assert engine.active_session is not None
    assert engine.active_session.id == "saved-001", "Same session ID should be preserved"
    assert engine.active_session.reconstructed is True, "Session must be flagged as reconstructed"
    assert engine.active_session.energy_kwh == pytest.approx(3.5, abs=0.01)
    # No completion event should fire — session is still active
    assert len(events) == 0


# ---------------------------------------------------------------------------
# FR-003 / FR-004: Charging ended during restart
# ---------------------------------------------------------------------------


async def test_recovery_charging_ended_during_restart(hass: HomeAssistant) -> None:
    """FR-003/FR-004: Session completed with best data when charging ended during downtime."""
    snapshot = make_active_snapshot(
        session_id="saved-002",
        rfid_index=1,
        energy_start_kwh=5.0,
        energy_kwh=4.0,  # last known = 4.0 kWh
    )
    events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    with (
        patch(
            "homeassistant.helpers.storage.Store.async_load",
            side_effect=_make_store_load_side_effect([snapshot]),
        ),
        patch(
            "homeassistant.helpers.storage.Store.async_save",
            new_callable=AsyncMock,
        ),
    ):
        # Charger is no longer charging
        hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Complete")
        hass.states.async_set(MOCK_TRX_ENTITY, "null")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "9.0")  # 5.0 + 4.0 = still available
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")

        entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="go-e")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.IDLE, "Engine should be IDLE after completing"

    assert len(events) == 1, "One session_completed event should fire"
    event_data = events[0].data
    assert event_data["session_id"] == "saved-002"
    assert event_data["reconstructed"] is True, "Recovered session must be flagged reconstructed"
    assert event_data["user_name"] == "Petra"


# ---------------------------------------------------------------------------
# FR-020: Different user's session active on restart
# ---------------------------------------------------------------------------


async def test_recovery_different_user_session(hass: HomeAssistant) -> None:
    """FR-020: Old session completed (reconstructed), new session starts tracking."""
    # Petra (rfid_index=1, trx=2) was charging when HA shut down
    snapshot = make_active_snapshot(
        session_id="petras-session",
        user_name="Petra",
        rfid_index=1,  # trx=2
        energy_start_kwh=10.0,
        energy_kwh=2.0,
    )
    events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    with (
        patch(
            "homeassistant.helpers.storage.Store.async_load",
            side_effect=_make_store_load_side_effect([snapshot]),
        ),
        patch(
            "homeassistant.helpers.storage.Store.async_save",
            new_callable=AsyncMock,
        ),
    ):
        # Now a different card (trx=3 → rfid_index=2) is charging
        hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
        hass.states.async_set(MOCK_TRX_ENTITY, "3")  # different trx
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.5")  # new session energy
        hass.states.async_set(MOCK_POWER_ENTITY, "3700.0")

        entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="go-e")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # Old session must be completed as reconstructed
    assert len(events) >= 1, "Petra's session should be completed"
    old_event = next((e for e in events if e.data.get("session_id") == "petras-session"), None)
    assert old_event is not None, "Old session completion event must be fired"
    assert old_event.data["reconstructed"] is True

    # New session should be tracking (card trx=3, unmapped → Unknown user)
    assert engine.state == SessionEngineState.TRACKING, "New session should start tracking"
    assert engine.active_session is not None
    assert engine.active_session.id != "petras-session", "New session has a new ID"


# ---------------------------------------------------------------------------
# FR-006: Corrupt or unreadable saved state → idle, no crash
# ---------------------------------------------------------------------------


async def test_recovery_corrupt_state_starts_idle(hass: HomeAssistant) -> None:
    """FR-006: Corrupt state file causes graceful idle start without errors."""
    # Simulate a corrupt/unusual snapshot that would fail parsing
    corrupt_snapshot = {"id": None, "ended_at": None, "user_name": 12345}  # invalid types

    with (
        patch(
            "homeassistant.helpers.storage.Store.async_load",
            side_effect=_make_store_load_side_effect([corrupt_snapshot]),
        ),
        patch(
            "homeassistant.helpers.storage.Store.async_save",
            new_callable=AsyncMock,
        ),
    ):
        hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Idle")
        hass.states.async_set(MOCK_TRX_ENTITY, "null")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")

        entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="go-e")
        entry.add_to_hass(hass)
        # Should not raise any exception
        result = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert result is True, "Integration must set up successfully even with corrupt state"
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.IDLE


# ---------------------------------------------------------------------------
# FR-005: Energy counter reset fallback (wh has reset)
# ---------------------------------------------------------------------------


async def test_recovery_energy_counter_reset_fallback(hass: HomeAssistant) -> None:
    """FR-005: When session energy is lower than energy_start, detect counter reset."""
    # Saved: energy_start=100.0, energy_kwh=5.0 (so charger was at ~105.0)
    snapshot = make_active_snapshot(
        session_id="saved-003",
        rfid_index=1,  # trx=2
        energy_start_kwh=100.0,
        energy_kwh=5.0,
    )
    events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    with (
        patch(
            "homeassistant.helpers.storage.Store.async_load",
            side_effect=_make_store_load_side_effect([snapshot]),
        ),
        patch(
            "homeassistant.helpers.storage.Store.async_save",
            new_callable=AsyncMock,
        ),
    ):
        # Charger still charging with same card but wh counter has reset to 0.3
        hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
        hass.states.async_set(MOCK_TRX_ENTITY, "2")  # same trx
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.3")  # reset! (0.3 < 100.0 = energy_start)
        hass.states.async_set(MOCK_POWER_ENTITY, "3700.0")

        entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="go-e")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Counter reset = treat as new/different session context
    # Old session should be completed, engine should continue tracking with reset counter
    # The important thing is old session is completed as reconstructed
    assert len(events) >= 1, "Old session should be completed on energy counter reset"
    old_event = events[0]
    assert old_event.data["reconstructed"] is True
    assert old_event.data["session_id"] == "saved-003"


# ---------------------------------------------------------------------------
# No snapshot → normal startup
# ---------------------------------------------------------------------------


async def test_no_snapshot_normal_startup(hass: HomeAssistant) -> None:
    """No active snapshot: engine starts in IDLE without recovery."""
    with (
        patch(
            "homeassistant.helpers.storage.Store.async_load",
            side_effect=_make_store_load_side_effect(None),
        ),
        patch(
            "homeassistant.helpers.storage.Store.async_save",
            new_callable=AsyncMock,
        ),
    ):
        hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Idle")
        hass.states.async_set(MOCK_TRX_ENTITY, "null")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")

        entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="go-e")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.IDLE
    assert engine.active_session is None
