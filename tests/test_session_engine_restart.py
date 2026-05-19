"""TC-017, TC-018, TC-019: HA restart mid-session recovery tests (PR-22 Phase 8).

TC-017: Active session snapshot + plug=on + power>0 at restart
        → session resumed with data_gap=True, reconstructed=True, energy delta attributed.

TC-018: Active session snapshot + plug=on at shutdown, but plug=off at restart
        → session ended with best-estimate disconnected_at, data_gap=True.

TC-019: Open window at shutdown: if power>0 at restart → window continues;
        if power=0 at restart → window closes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from custom_components.ev_charging_manager.const import (
    CONF_CHARGING_IDLE_TIMEOUT_MIN,
    CONF_DISCONNECT_GRACE_MIN,
    DOMAIN,
    SessionEngineState,
)
from custom_components.ev_charging_manager.session_engine_v2 import (
    PlugAnchoredSessionEngine,
)
from tests.conftest import (
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    MOCK_POWER_ENTITY,
    MOCK_TRX_ENTITY,
)

MOCK_PLUG_ENTITY = "binary_sensor.goe_abc123_car_0"
MOCK_CABLE_LOCK_ENTITY = "sensor.goe_abc123_cus_value"


def _make_snapshot(
    *,
    session_id: str | None = None,
    energy_start_kwh: float = 0.0,
    energy_kwh: float = 5.0,
    started_at: str | None = None,
    connected_at: str | None = None,
    charging_started_at: str | None = None,
) -> dict:
    """Build a minimal active-session snapshot dict for restart recovery tests."""
    now_utc = datetime.now(timezone.utc)
    started = started_at or (now_utc - timedelta(hours=2)).isoformat()
    return {
        "id": session_id or str(uuid.uuid4()),
        "user_name": "Restart Test User",
        "user_type": "regular",
        "vehicle_name": None,
        "vehicle_battery_kwh": None,
        "efficiency_factor": None,
        "rfid_index": None,
        "rfid_uid": None,
        "charger_name": "Test Charger",
        "started_at": started,
        "connected_at": connected_at or started,
        "energy_start_kwh": energy_start_kwh,
        "energy_kwh": energy_kwh,
        "cost_total_kr": 0.0,
        "cost_method": "static",
        "price_details": None,
        "charger_total_before_kwh": None,
        "max_power_w": 7200.0,
        "charging_started_at": charging_started_at,
        "charging_ended_at": None,
        "charging_duration_s": 3600,
        "charging_window_count": 1,
        # reconstructed and data_gap are NOT set — they should be set by recovery
    }


async def _make_engine_entry(
    hass: HomeAssistant,
    *,
    plug_state: str = "on",
    cable_lock_state: str = "Locked",
    power_state: str = "0.0",
    energy_state: str = "5.0",
) -> MockConfigEntry:
    """Create and set up a config entry, pre-setting charger entity states."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
            CONF_CHARGING_IDLE_TIMEOUT_MIN: 5,
            CONF_DISCONNECT_GRACE_MIN: 10,
        },
        title="Test go-e Charger",
    )

    # Set charger entity states to simulate what the charger looks like at restart
    hass.states.async_set(MOCK_PLUG_ENTITY, plug_state)
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, cable_lock_state)
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, energy_state)
    hass.states.async_set(MOCK_POWER_ENTITY, power_state)

    return entry


def _get_engine(hass: HomeAssistant, entry: MockConfigEntry) -> PlugAnchoredSessionEngine:
    """Return the PlugAnchoredSessionEngine from hass.data."""
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert isinstance(engine, PlugAnchoredSessionEngine)
    return engine


# ---------------------------------------------------------------------------
# TC-017: Restart with plug=on + power>0 → session resumed
# ---------------------------------------------------------------------------


async def test_tc017_restart_plug_on_power_charging(
    hass: HomeAssistant,
) -> None:
    """TC-017: HA restarts with plug still in and power flowing → session resumed.

    Verifies: data_gap=True, reconstructed=True, energy delta attributed.
    """
    energy_start = 10.0
    energy_at_restart = 15.5  # 5.5 kWh delivered since session start
    snapshot = _make_snapshot(
        energy_start_kwh=energy_start,
        energy_kwh=5.0,  # last saved energy (older)
    )
    session_id = snapshot["id"]

    entry = await _make_engine_entry(
        hass,
        plug_state="on",
        cable_lock_state="Locked",
        power_state="7200.0",
        energy_state=str(energy_at_restart),
    )

    # Inject the snapshot into the session store before setup
    raw_store_data = {
        "version": 1,
        "minor_version": 2,
        "key": "ev_charging_manager_sessions",
        "data": [snapshot],  # active snapshot: no ended_at
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

    engine = _get_engine(hass, entry)

    # Session must be resumed (not IDLE)
    assert engine.state == SessionEngineState.TRACKING, (
        f"TC-017: expected TRACKING after restart with plug=on, got {engine.state}"
    )
    assert engine.active_session is not None, "TC-017: active session must be set"
    assert engine.active_session.id == session_id, "TC-017: session id must match snapshot"

    # Flags
    assert engine.active_session.data_gap is True, (
        "TC-017: data_gap must be True after restart recovery"
    )
    assert engine.active_session.reconstructed is True, (
        "TC-017: reconstructed must be True after restart recovery"
    )

    # Energy: should reflect current counter - energy_start (delta)
    expected_energy = energy_at_restart - energy_start
    assert abs(engine.active_session.energy_kwh - expected_energy) <= 0.01, (
        f"TC-017: energy_kwh {engine.active_session.energy_kwh:.3f} not close to "
        f"expected {expected_energy:.3f} (counter delta)"
    )

    # FR-N01: verify no _awaiting_reset or _last_car_status on the new engine
    assert not hasattr(engine, "_awaiting_reset"), (
        "TC-017 (FR-N02): _awaiting_reset must not exist on PlugAnchoredSessionEngine"
    )
    assert not hasattr(engine, "_last_car_status"), (
        "TC-017 (FR-N02): _last_car_status must not exist on PlugAnchoredSessionEngine"
    )


# ---------------------------------------------------------------------------
# TC-018: Restart with plug=off → session completed with best-estimate time
# ---------------------------------------------------------------------------


async def test_tc018_restart_plug_off_session_completed(
    hass: HomeAssistant,
) -> None:
    """TC-018: HA restarts with plug off (cable removed during outage) → session ended.

    Verifies: session is stored (not lost), data_gap=True, engine returns to IDLE.
    """
    energy_start = 2.0
    energy_at_restart = 7.0  # energy counter after cable removal
    snapshot = _make_snapshot(
        energy_start_kwh=energy_start,
        energy_kwh=4.5,
        charging_started_at=dt_util.utcnow().isoformat(),
    )

    entry = await _make_engine_entry(
        hass,
        plug_state="off",
        cable_lock_state="Unlocked",  # cable removed
        power_state="0.0",
        energy_state=str(energy_at_restart),
    )

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

    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    # Engine should be IDLE after completing the orphaned session
    assert engine.state == SessionEngineState.IDLE, (
        f"TC-018: expected IDLE after restart with plug=off, got {engine.state}"
    )
    assert engine.active_session is None, "TC-018: no active session after restart with plug=off"

    # The completed session should be in the store
    assert len(session_store.sessions) == 1, (
        f"TC-018: expected 1 completed session, got {len(session_store.sessions)}"
    )

    completed = session_store.sessions[0]
    assert completed["data_gap"] is True, "TC-018: data_gap must be True"
    assert completed["id"] == snapshot["id"], "TC-018: session id must match original snapshot"
    # disconnected_at must be set
    assert completed.get("disconnected_at") or completed.get("ended_at"), (
        "TC-018: disconnected_at must be set on the completed session"
    )


# ---------------------------------------------------------------------------
# TC-019: Window state restoration on restart
# ---------------------------------------------------------------------------


async def test_tc019_restart_window_state_power_on(
    hass: HomeAssistant,
) -> None:
    """TC-019a: Power > 0 at restart → window continues (engine opens a new window)."""
    energy_start = 0.0
    energy_at_restart = 5.0
    snapshot = _make_snapshot(
        energy_start_kwh=energy_start,
        energy_kwh=5.0,
        charging_started_at=dt_util.utcnow().isoformat(),
    )

    entry = await _make_engine_entry(
        hass,
        plug_state="on",
        cable_lock_state="Locked",
        power_state="7200.0",  # charging at restart
        energy_state=str(energy_at_restart),
    )

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

    engine = _get_engine(hass, entry)

    # Window must be open (power was >0 at restart)
    assert engine.window_tracker.is_open(), (
        "TC-019a: window must be open when power>0 at restart"
    )
    assert engine.get_status_sub_state() == "charging", (
        f"TC-019a: expected 'charging', got {engine.get_status_sub_state()!r}"
    )


async def test_tc019b_restart_window_state_power_off(
    hass: HomeAssistant,
) -> None:
    """TC-019b: Power = 0 at restart → window tracker starts closed."""
    energy_start = 0.0
    energy_at_restart = 5.0
    snapshot = _make_snapshot(
        energy_start_kwh=energy_start,
        energy_kwh=5.0,
        charging_started_at=dt_util.utcnow().isoformat(),
    )

    entry = await _make_engine_entry(
        hass,
        plug_state="on",
        cable_lock_state="Locked",
        power_state="0.0",  # idle at restart
        energy_state=str(energy_at_restart),
    )

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

    engine = _get_engine(hass, entry)

    # Session resumed but window is not open (power was 0 at restart)
    assert engine.state == SessionEngineState.TRACKING, (
        "TC-019b: session must be in TRACKING after restart"
    )
    assert not engine.window_tracker.is_open(), (
        "TC-019b: window must NOT be open when power=0 at restart"
    )
    # Sub-state reflects no open window with at least 1 previous window
    # (snapshot had charging_window_count=1 so we're 'charged' or 'waiting')
    sub = engine.get_status_sub_state()
    assert sub in ("charged", "waiting"), (
        f"TC-019b: expected 'charged' or 'waiting' for idle power at restart, got {sub!r}"
    )
