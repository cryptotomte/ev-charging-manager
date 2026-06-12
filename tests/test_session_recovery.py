"""Tests for session recovery after HA restart (PR-03 US1, FR-001 to FR-006, FR-020).

PR-27 (023-recovery-hardening) adds a v2-engine section at the bottom:
recovery energy guard (FR-001/FR-002) — unavailable energy is "no evidence",
counter-reset detection uses ENERGY_RESET_EPSILON_KWH.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.ev_charging_manager.const import (
    CONF_CHARGING_IDLE_TIMEOUT_MIN,
    CONF_DISCONNECT_GRACE_MIN,
    DEFERRED_RECOVERY_TIMEOUT_MIN,
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

    async_load call order during setup: ConfigStore, StatsStore, SessionStore —
    StatsStore loads before SessionStore so the StatsEngine listener exists
    before session recovery can fire EVENT_SESSION_COMPLETED:
    1. ConfigStore (expects dict or None)
    2. StatsStore (expects dict or None)
    3. SessionStore (expects list or None)
    """
    call_count = 0
    session_payload = session_data  # may be None

    async def side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None  # ConfigStore: no existing config
        if call_count == 2:
            return None  # StatsStore: no existing stats
        return session_payload  # SessionStore: our test data

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


# ===========================================================================
# PR-27 (023-recovery-hardening) US1: recovery energy guard on the
# plug-anchored engine (FR-001, FR-002)
# ===========================================================================

MOCK_PLUG_ENTITY = "binary_sensor.goe_abc123_car_0"
MOCK_CABLE_LOCK_ENTITY = "sensor.goe_abc123_cus_value"


async def _setup_v2_with_snapshot(
    hass: HomeAssistant,
    snapshot: dict,
    *,
    plug_state: str = "on",
    cable_lock_state: str = "Locked",
    energy_state: str = "13.5",
    power_state: str = "0.0",
) -> MockConfigEntry:
    """Set up the goe_gemini (plug-anchored) engine with a recovery snapshot."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
            CONF_CHARGING_IDLE_TIMEOUT_MIN: 5,
            CONF_DISCONNECT_GRACE_MIN: 10,
        },
        title="Test go-e Charger (v2 recovery)",
    )

    hass.states.async_set(MOCK_PLUG_ENTITY, plug_state)
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, cable_lock_state)
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, energy_state)
    hass.states.async_set(MOCK_POWER_ENTITY, power_state)

    raw_store_data = {
        "version": 1,
        "minor_version": 2,
        "key": "ev_charging_manager_sessions",
        "data": [snapshot],
    }
    with (
        patch(
            "homeassistant.helpers.storage.Store.async_load",
            new_callable=AsyncMock,
            return_value=raw_store_data,
        ),
        patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock),
    ):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    return entry


async def test_v2_recovery_energy_unavailable_resumes_with_data_gap(
    hass: HomeAssistant,
) -> None:
    """FR-001: unavailable energy at recovery is 'no evidence' — session RESUMES.

    Previously the unavailable reading collapsed to 0.0 (`or 0.0`), was misread
    as a counter reset, and the session was force-completed while the cable was
    still plugged in.
    """
    snapshot = make_active_snapshot(
        session_id="v2-resume-001",
        energy_start_kwh=10.0,
        energy_kwh=3.0,
    )
    events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    entry = await _setup_v2_with_snapshot(
        hass,
        snapshot,
        plug_state="on",
        energy_state=STATE_UNAVAILABLE,  # energy sensor not yet loaded at recovery
        power_state="0.0",
    )

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.TRACKING, (
        "session must RESUME when energy evidence is missing — not complete as a reset"
    )
    assert engine.active_session is not None
    assert engine.active_session.id == "v2-resume-001"
    assert engine.active_session.data_gap is True, "missing evidence must flag a data gap"
    # Snapshot energy is carried over while the sensor is away.
    assert engine.active_session.energy_kwh == pytest.approx(3.0, abs=0.001)
    assert len(events) == 0, "no completion event may fire on resume"

    # Energy tracking continues once the sensor returns.
    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        hass.states.async_set(MOCK_ENERGY_ENTITY, "13.5")
        await hass.async_block_till_done()
    assert engine.active_session.energy_kwh == pytest.approx(3.5, abs=0.001), (
        "energy accumulation must continue from the live counter after the gap"
    )


async def test_v2_recovery_genuine_counter_reset_still_completes(
    hass: HomeAssistant,
) -> None:
    """FR-002 control: a genuine reset (energy available, measurably lower)
    preserves the existing counter-reset completion behavior."""
    snapshot = make_active_snapshot(
        session_id="v2-reset-001",
        energy_start_kwh=100.0,
        energy_kwh=5.0,
    )
    events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    entry = await _setup_v2_with_snapshot(
        hass,
        snapshot,
        plug_state="on",
        energy_state="0.3",  # measurably below energy_start=100.0
        power_state="0.0",
    )

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.IDLE, (
        "a genuine counter reset must complete the old session (FR-027 preserved)"
    )
    assert len(events) == 1, "exactly one completion event for the reset session"
    assert events[0].data["session_id"] == "v2-reset-001"
    assert events[0].data["reconstructed"] is True


async def test_v2_recovery_energy_jitter_below_start_is_not_a_reset(
    hass: HomeAssistant,
) -> None:
    """FR-002: a reading lower than start by less than the epsilon is jitter."""
    snapshot = make_active_snapshot(
        session_id="v2-jitter-001",
        energy_start_kwh=10.0,
        energy_kwh=3.0,
    )
    events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    entry = await _setup_v2_with_snapshot(
        hass,
        snapshot,
        plug_state="on",
        energy_state="9.995",  # 5 Wh below start — within ENERGY_RESET_EPSILON_KWH
        power_state="0.0",
    )

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.TRACKING, (
        "float jitter below energy_start must not be treated as a counter reset"
    )
    assert engine.active_session is not None
    assert engine.active_session.id == "v2-jitter-001"
    assert len(events) == 0


async def test_v2_deferred_recovery_energy_unavailable_resumes(
    hass: HomeAssistant,
) -> None:
    """FR-001 (deferred path): plug deferred at boot, then plug valid while
    energy is STILL unavailable → the deferred recovery run must also resume."""
    snapshot = make_active_snapshot(
        session_id="v2-deferred-001",
        energy_start_kwh=10.0,
        energy_kwh=3.0,
    )
    events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    entry = await _setup_v2_with_snapshot(
        hass,
        snapshot,
        plug_state=STATE_UNAVAILABLE,  # defers recovery (BUG-3 path)
        energy_state=STATE_UNAVAILABLE,
        power_state="0.0",
    )
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.active_session is None, "recovery must still be deferred"

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        await hass.async_block_till_done()

    assert engine.state == SessionEngineState.TRACKING, (
        "deferred recovery with unavailable energy must resume, not complete"
    )
    assert engine.active_session is not None
    assert engine.active_session.id == "v2-deferred-001"
    assert engine.active_session.data_gap is True
    assert len(events) == 0


async def test_v2_recovery_timeout_completes_with_snapshot_energy(
    hass: HomeAssistant, freezer
) -> None:
    """Deferred-timeout path: plug never valid, energy unavailable → snapshot is
    force-completed using the snapshot's energy values (no crash, no zeroing)."""
    snapshot = make_active_snapshot(
        session_id="v2-timeout-001",
        energy_start_kwh=10.0,
        energy_kwh=3.0,
    )

    entry = await _setup_v2_with_snapshot(
        hass,
        snapshot,
        plug_state=STATE_UNAVAILABLE,
        energy_state=STATE_UNAVAILABLE,
        power_state="0.0",
    )
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]
    assert engine.active_session is None, "recovery must be deferred at boot"

    with (
        patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock),
        patch(
            "custom_components.ev_charging_manager.session_engine_v2."
            "persistent_notification.async_create"
        ),
    ):
        freezer.tick(timedelta(minutes=DEFERRED_RECOVERY_TIMEOUT_MIN + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE
    assert len(session_store.sessions) == 1, "snapshot must be force-completed on timeout"
    forced = session_store.sessions[0]
    assert forced["id"] == "v2-timeout-001"
    assert forced["energy_kwh"] == pytest.approx(3.0, abs=0.001), (
        "force-completed session must keep the snapshot's energy when no live reading exists"
    )
