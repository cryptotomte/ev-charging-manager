"""TC-017, TC-018, TC-019: HA restart mid-session recovery tests (PR-22 Phase 8).

TC-017: Active session snapshot + plug=on + power>0 at restart
        → session resumed with data_gap=True, reconstructed=True, energy delta attributed.

TC-018: Active session snapshot + plug=on at shutdown, but plug=off at restart
        → session ended with best-estimate disconnected_at, data_gap=True.

TC-019: Open window at shutdown: if power>0 at restart → window continues;
        if power=0 at restart → window closes.

TC-020 (US2): HA-restart charging_duration_s recovery — five scenarios (FR-024..029):
        (a) charging_duration_s ≥ pre + post restart durations
        (b) windows[] has two entries, first with both started_at and ended_at
        (c) CHARGING_WINDOW_CLOSE log uses session.charging_window_count
        (d) avg_power_w plausible (Petras-bil reproducer)
        (e) FR-029 schema smoke test: version stays at 1.2
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ev_charging_manager.const import (
    CONF_CHARGING_IDLE_TIMEOUT_MIN,
    CONF_DEBUG_LOGGING,
    CONF_DISCONNECT_GRACE_MIN,
    DEBUG_CAT_CHARGING_WINDOW_CLOSE,
    DOMAIN,
    SESSION_STORE_VERSION,
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
    charging_duration_s: int = 3600,
    charging_window_count: int = 1,
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
        "charging_duration_s": charging_duration_s,
        "charging_window_count": charging_window_count,
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
    assert engine.window_tracker.is_open(), "TC-019a: window must be open when power>0 at restart"
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
    # (snapshot had charging_window_count=1 so we're 'charged' or 'initializing')
    sub = engine.get_status_sub_state()
    assert sub in ("charged", "initializing"), (
        f"TC-019b: expected 'charged' or 'initializing' for idle power at restart, got {sub!r}"
    )


# ---------------------------------------------------------------------------
# TC-020 (US2): HA-restart charging_duration_s recovery — five scenarios
# FR-024..FR-029 (T017 in tasks.md)
# ---------------------------------------------------------------------------


async def _make_engine_entry_with_restart(
    hass: HomeAssistant,
    snapshot: dict,
    *,
    plug_state: str = "on",
    cable_lock_state: str = "Locked",
    power_state: str = "7200.0",
    energy_state: str = "5.0",
    debug: bool = False,
    tmp_path: object | None = None,
) -> tuple[MockConfigEntry, AsyncMock]:
    """Set up a config entry simulating a restart with a persisted session snapshot.

    Returns (entry, save_mock) so callers can inspect what was saved.
    """
    if debug and tmp_path is not None:
        hass.config.config_dir = str(tmp_path)

    options: dict = {
        "plug_entity": MOCK_PLUG_ENTITY,
        "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
        CONF_CHARGING_IDLE_TIMEOUT_MIN: 5,
        CONF_DISCONNECT_GRACE_MIN: 10,
    }
    if debug:
        options[CONF_DEBUG_LOGGING] = True

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options=options,
        title="Test go-e Charger (US2)",
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
    save_mock = AsyncMock()
    with (
        patch(
            "homeassistant.helpers.storage.Store.async_load",
            new_callable=AsyncMock,
            return_value=raw_store_data,
        ),
        patch(
            "homeassistant.helpers.storage.Store.async_save",
            save_mock,
        ),
    ):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    return entry, save_mock


async def test_tc020a_restart_charging_duration_includes_pre_restart(
    hass: HomeAssistant,
    freezer,
) -> None:
    """TC-020(a): charging_duration_s after restart includes pre-restart window duration.

    Scenario: session was active with charging_started_at set (N seconds of charging
    happened before the restart). After restart, M more seconds of charging happen,
    then the cable is unplugged. The final charging_duration_s must be >= N + M.

    FR-024, FR-025, FR-026, FR-028.
    """
    pre_restart_charge_s = 2700  # 45 minutes of charging before restart
    post_restart_charge_s = 300  # 5 minutes after restart

    # charging_started_at is 45 min before now (the pre-restart open window start)
    now_utc = dt_util.utcnow()
    charging_started = (now_utc - timedelta(seconds=pre_restart_charge_s)).isoformat()
    energy_start = 2.0
    energy_at_restart = 10.157  # some energy has flowed already

    snapshot = _make_snapshot(
        energy_start_kwh=energy_start,
        energy_kwh=energy_at_restart - energy_start,
        charging_started_at=charging_started,
        charging_duration_s=0,  # persisted duration was lost (bug scenario)
        charging_window_count=1,
    )

    entry, _save = await _make_engine_entry_with_restart(
        hass,
        snapshot,
        power_state="7200.0",
        energy_state=str(energy_at_restart),
    )
    engine = _get_engine(hass, entry)
    assert engine.state == SessionEngineState.TRACKING

    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Advance time so M seconds of post-restart charging accumulates
        freezer.tick(timedelta(seconds=post_restart_charge_s))
        await hass.async_block_till_done()

        # Power drops to zero → idle timer starts
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()

        # Wait for idle timeout (5 min) → window closes
        freezer.tick(timedelta(minutes=6))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Unplug → session finalises
        hass.states.async_set(MOCK_ENERGY_ENTITY, "18.157")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    # Session must have ended
    assert engine.state == SessionEngineState.IDLE, (
        f"TC-020(a): expected IDLE after unplug, got {engine.state}"
    )
    assert len(session_store.sessions) == 1, (
        f"TC-020(a): expected 1 completed session, got {len(session_store.sessions)}"
    )
    completed = session_store.sessions[0]
    actual_duration = completed["charging_duration_s"]
    # Must include both pre-restart (synthetic window) and post-restart durations
    assert actual_duration >= pre_restart_charge_s + post_restart_charge_s, (
        f"TC-020(a): charging_duration_s={actual_duration} must be >= "
        f"{pre_restart_charge_s + post_restart_charge_s} (pre + post restart)"
    )


async def test_tc020b_restart_windows_has_two_entries_first_with_end(
    hass: HomeAssistant,
    freezer,
) -> None:
    """TC-020(b): After restart, window tracker has two closed windows.

    The first window (synthetic, from pre-restart open window) has both start_at
    and end_at set. The second window (post-restart) is opened then closed normally.

    FR-024, FR-026, IC-6.
    """
    pre_restart_charge_s = 1800  # 30 minutes before restart
    now_utc = dt_util.utcnow()
    charging_started = (now_utc - timedelta(seconds=pre_restart_charge_s)).isoformat()
    energy_start = 0.0
    energy_at_restart = 5.0

    snapshot = _make_snapshot(
        energy_start_kwh=energy_start,
        energy_kwh=energy_at_restart - energy_start,
        charging_started_at=charging_started,
        charging_duration_s=0,
        charging_window_count=1,
    )

    entry, _save = await _make_engine_entry_with_restart(
        hass,
        snapshot,
        power_state="7200.0",
        energy_state=str(energy_at_restart),
    )
    engine = _get_engine(hass, entry)
    assert engine.state == SessionEngineState.TRACKING

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Advance time so post-restart window accumulates some duration
        freezer.tick(timedelta(seconds=300))
        await hass.async_block_till_done()

        # Power drops → idle timer starts
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()

        # Wait for idle timeout → post-restart window closes
        freezer.tick(timedelta(minutes=6))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

    # WindowTracker must have exactly 2 closed windows
    tracker = engine.window_tracker
    closed = tracker.all_closed_windows()
    assert len(closed) == 2, (
        f"TC-020(b): expected 2 closed windows after restart-spanning close, got {len(closed)}"
    )
    # First window (synthetic) must have both start_at and end_at set
    synthetic = closed[0]
    assert synthetic.start_at is not None, "TC-020(b): synthetic window must have start_at"
    assert synthetic.end_at is not None, "TC-020(b): synthetic window must have end_at (closed)"
    assert synthetic.end_at >= synthetic.start_at, (
        "TC-020(b): synthetic window end_at must be >= start_at"
    )
    # Second window (post-restart) also closed
    post = closed[1]
    assert post.start_at is not None, "TC-020(b): post-restart window must have start_at"
    assert post.end_at is not None, "TC-020(b): post-restart window must be closed"


async def test_tc020c_charging_window_close_log_uses_session_window_count(
    hass: HomeAssistant,
    freezer,
    tmp_path,
) -> None:
    """TC-020(c): CHARGING_WINDOW_CLOSE log line uses session.charging_window_count.

    After restart, the post-restart window's close log must use the
    session-wide counter (2), not the tracker's internal closed_window_count()
    which would reset to 1 after recovery (the off-by-one / mismatch bug).

    FR-027, IC-6.
    """
    pre_restart_charge_s = 1800
    now_utc = dt_util.utcnow()
    charging_started = (now_utc - timedelta(seconds=pre_restart_charge_s)).isoformat()
    energy_at_restart = 5.0

    snapshot = _make_snapshot(
        energy_start_kwh=0.0,
        energy_kwh=energy_at_restart,
        charging_started_at=charging_started,
        charging_duration_s=0,
        charging_window_count=1,
    )

    # Enable debug logging so we can capture the CHARGING_WINDOW_CLOSE log line
    entry, _save = await _make_engine_entry_with_restart(
        hass,
        snapshot,
        power_state="7200.0",
        energy_state=str(energy_at_restart),
        debug=True,
        tmp_path=tmp_path,
    )
    engine = _get_engine(hass, entry)
    assert engine.state == SessionEngineState.TRACKING

    # Attach spy to debug_logger so we can intercept log calls without file I/O
    debug_logger = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
    assert debug_logger is not None
    logged_calls: list[tuple[str, str]] = []
    original_log = debug_logger.log

    def spy_log(category: str, message: str) -> None:
        logged_calls.append((category, message))
        original_log(category, message)

    debug_logger.log = spy_log  # type: ignore[method-assign]
    if engine._debug_logger is not None:
        engine._debug_logger.log = spy_log  # type: ignore[method-assign]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Advance time so post-restart window accumulates duration
        freezer.tick(timedelta(seconds=300))
        await hass.async_block_till_done()

        # Power drops → idle timer starts
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()

        # Wait for idle timeout → CHARGING_WINDOW_CLOSE fires
        freezer.tick(timedelta(minutes=6))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

    # Find the CHARGING_WINDOW_CLOSE log call
    close_calls = [
        (cat, msg) for cat, msg in logged_calls if cat == DEBUG_CAT_CHARGING_WINDOW_CLOSE
    ]
    assert len(close_calls) >= 1, (
        f"TC-020(c): no {DEBUG_CAT_CHARGING_WINDOW_CLOSE} log call found; "
        f"all calls: {[c for c, _ in logged_calls]}"
    )
    # The post-restart window is window #2 in the session (session.charging_window_count=2).
    # Before the fix, closed_window_count() returns 1 (only the tracker's count, not
    # the session-wide counter). After the fix it must say window=2.
    close_msg = close_calls[-1][1]
    assert "window=2" in close_msg, (
        f"TC-020(c): CHARGING_WINDOW_CLOSE log must use session.charging_window_count (2), "
        f"but got: {close_msg!r}"
    )
    assert "window=1" not in close_msg, (
        f"TC-020(c): CHARGING_WINDOW_CLOSE log must NOT use tracker closed_count (1), "
        f"but got: {close_msg!r}"
    )


async def test_tc020d_avg_power_plausible_after_restart(
    hass: HomeAssistant,
    freezer,
) -> None:
    """TC-020(d): avg_power_w is physically plausible after restart-spanning session.

    Reproducer: Petras-bil case — 8.157 kWh over ~9500s ≈ 3.1 kW.
    Before the fix, only the post-restart charging time counted, making
    avg_power_w appear impossibly high (e.g. 45 kW for a 3.7 kW charger).

    FR-028.
    """
    # Simulate: 2h37min of pre-restart charging (9500s), then restart, then 300s more
    pre_restart_charge_s = 9500
    post_restart_charge_s = 300
    total_energy_kwh = 8.157
    energy_start = 0.0
    energy_at_restart = total_energy_kwh  # all energy was before restart in this scenario

    now_utc = dt_util.utcnow()
    charging_started = (now_utc - timedelta(seconds=pre_restart_charge_s)).isoformat()

    snapshot = _make_snapshot(
        energy_start_kwh=energy_start,
        energy_kwh=energy_at_restart,
        charging_started_at=charging_started,
        charging_duration_s=0,  # lost on restart — the bug
        charging_window_count=1,
    )

    session_store_saved: list[dict] = []

    async def capture_save(data: dict) -> None:
        session_store_saved.clear()
        session_store_saved.append(data)

    entry, _save = await _make_engine_entry_with_restart(
        hass,
        snapshot,
        power_state="7200.0",
        energy_state=str(energy_at_restart),
    )
    engine = _get_engine(hass, entry)
    assert engine.state == SessionEngineState.TRACKING

    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Advance time for post-restart charge
        freezer.tick(timedelta(seconds=post_restart_charge_s))
        await hass.async_block_till_done()

        # Power drops → idle timer
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()

        # Idle timeout → window closes
        freezer.tick(timedelta(minutes=6))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Set final energy and unplug
        hass.states.async_set(MOCK_ENERGY_ENTITY, str(energy_at_restart))
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE
    assert len(session_store.sessions) == 1

    completed = session_store.sessions[0]
    avg_power = completed.get("avg_power_w", 0.0)

    # For a typical home charger (3.7–7.4 kW), avg power must be within plausible range.
    # The bug produced ~45 kW (8.157 kWh / 300s), the fix should give ~3.1 kW (/ ~9800s).
    assert avg_power <= 7500, (
        f"TC-020(d): avg_power_w={avg_power:.1f}W is impossibly high for a home charger — "
        f"pre-restart duration was probably lost (charging_duration_s="
        f"{completed.get('charging_duration_s')})"
    )
    # Also verify total duration is plausible (not just the 300s post-restart slice)
    assert completed["charging_duration_s"] >= pre_restart_charge_s, (
        f"TC-020(d): charging_duration_s={completed['charging_duration_s']}s should be "
        f">= pre_restart_charge_s={pre_restart_charge_s}s"
    )


async def test_tc020e_schema_version_unchanged_after_restart_session(
    hass: HomeAssistant,
    freezer,
) -> None:
    """TC-020(e): FR-029 smoke test — schema version remains 1 / minor 2 after restart.

    The synthetic-window injection must NOT bump the store schema version.

    FR-029.
    """
    pre_restart_charge_s = 600
    now_utc = dt_util.utcnow()
    charging_started = (now_utc - timedelta(seconds=pre_restart_charge_s)).isoformat()

    snapshot = _make_snapshot(
        energy_start_kwh=0.0,
        energy_kwh=2.0,
        charging_started_at=charging_started,
        charging_duration_s=0,
        charging_window_count=1,
    )

    raw_store_data = {
        "version": 1,
        "minor_version": 2,
        "key": "ev_charging_manager_sessions",
        "data": [snapshot],
    }

    options: dict = {
        "plug_entity": MOCK_PLUG_ENTITY,
        "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
        CONF_CHARGING_IDLE_TIMEOUT_MIN: 5,
        CONF_DISCONNECT_GRACE_MIN: 10,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options=options,
        title="Test go-e Charger (US2 schema)",
    )

    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "2.0")
    hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")

    # Use AsyncMock and capture calls via call_args_list — each call's first positional
    # argument is the envelope dict passed to Store.async_save(data).
    save_mock = AsyncMock()
    with (
        patch(
            "homeassistant.helpers.storage.Store.async_load",
            new_callable=AsyncMock,
            return_value=raw_store_data,
        ),
        patch(
            "homeassistant.helpers.storage.Store.async_save",
            save_mock,
        ),
    ):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        engine = _get_engine(hass, entry)
        assert engine.state == SessionEngineState.TRACKING

        # Advance time, drop power, wait for idle timeout
        freezer.tick(timedelta(seconds=300))
        await hass.async_block_till_done()

        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()

        freezer.tick(timedelta(minutes=6))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Unplug → session finalized → final save happens
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    # At least one save must have happened (session completion)
    assert save_mock.call_count >= 1, (
        "TC-020(e): expected at least one Store.async_save call after session end"
    )
    # Filter to only session-store saves (identified by the store key in the envelope).
    # patch("...Store.async_save") intercepts ALL Store instances (session, stats, config),
    # so we must filter to the one that uses the session store key.
    session_store_saves = [
        call.args[0]
        for call in save_mock.call_args_list
        if call.args
        and isinstance(call.args[0], dict)
        and call.args[0].get("key") == "ev_charging_manager_sessions"
    ]
    assert len(session_store_saves) >= 1, (
        f"TC-020(e): no session-store saves found; all save calls: "
        f"{[c.args[0] if c.args else c.kwargs for c in save_mock.call_args_list]}"
    )
    # Verify all session-store saves use version=1, minor_version=2 (no schema bump)
    for envelope in session_store_saves:
        assert envelope.get("version") == SESSION_STORE_VERSION, (
            f"TC-020(e): expected version={SESSION_STORE_VERSION}, got {envelope.get('version')!r}"
        )
        assert envelope.get("minor_version") == 2, (
            f"TC-020(e): expected minor_version=2, got {envelope.get('minor_version')!r}"
        )


# ---------------------------------------------------------------------------
# SF7: HA restart mid-wait → engine re-enters waiting_for_rfid on startup
# ---------------------------------------------------------------------------


async def test_ha_restart_during_rfid_wait_re_enters_wait_state(
    hass: HomeAssistant,
) -> None:
    """HA restarts while a cable is plugged in but no RFID blip has occurred yet.

    Verifies that after restart the engine re-derives the sub-state from current
    entity values and correctly enters waiting_for_rfid — exactly as it would in a
    live session.  No active session snapshot exists (the wait was pre-session).

    The test simulates the post-restart scenario: after HA loads the integration
    (engine listener is now registered), the charger entities fire their restored
    state-change events.  This is how HA communicates "plug is on at restart" to
    the engine — via a state_changed event, not via a separate boot hook.

    Per spec.md Edge Cases (A4 / restart handling) and contracts/engine-state-machine.md:
    on the first plug=on event with trx=null (and no active snapshot), the engine
    must enter waiting_for_rfid rather than starting a session.
    """
    # Create the entry with plug=off (idle). _make_engine_entry only sets states and
    # returns the entry — it does NOT call add_to_hass or async_setup.
    entry = await _make_engine_entry(
        hass,
        plug_state="off",
        cable_lock_state="Unlocked",
        power_state="0.0",
        energy_state="0.0",
    )
    # Complete setup (no snapshot → active_snapshot is None → no async_recover).
    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    engine = _get_engine(hass, entry)
    assert engine.state == SessionEngineState.IDLE, (
        "SF7 pre-condition: engine must be IDLE before simulating restart-plug-on"
    )

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Simulate HA restoring the plug entity state after restart — the charger
        # has been plugged in since before the restart (cable_lock=Locked confirms it).
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        hass.states.async_set(MOCK_TRX_ENTITY, "null")  # no RFID blip before restart
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        await hass.async_block_till_done()

    # The engine must be TRACKING with no active session (RFID wait re-entered)
    assert engine.state == SessionEngineState.TRACKING, (
        f"SF7: expected TRACKING after post-restart plug=on, got {engine.state!r}"
    )
    assert engine.active_session is None, (
        "SF7: no session must exist — restart happened before any RFID blip"
    )

    # Sub-state must be waiting_for_rfid (cable in, no blip)
    sub = engine.get_status_sub_state()
    assert sub == "waiting_for_rfid", (
        f"SF7: expected sub-state 'waiting_for_rfid' after post-restart plug=on/trx=null, "
        f"got {sub!r}"
    )

    # _rfid_wait must be initialised (the wait state was re-derived from entity values)
    assert engine._rfid_wait is not None, (  # noqa: SLF001
        "SF7: _rfid_wait must be set after post-restart plug=on with trx=null"
    )

    # Sending a trx event must resolve the wait and start a session normally
    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Unmapped blip arrives (no user configured) → session starts as Unknown
        hass.states.async_set(MOCK_TRX_ENTITY, "5")
        await hass.async_block_till_done()

    assert engine.active_session is not None, (
        "SF7: session must start normally when trx resolves after post-restart wait"
    )
    assert engine._rfid_wait is None, (  # noqa: SLF001
        "SF7: _rfid_wait must be cleared after session starts"
    )


async def test_tc020f_no_synthetic_window_when_pre_restart_was_charged(
    hass: HomeAssistant,
) -> None:
    """TC-020(f): no synthetic window injected when pre-restart state was 'charged'.

    If the snapshot has BOTH charging_started_at AND charging_ended_at set (i.e.
    the session was in 'charged' sub-state when HA shut down — the charging window
    was already closed), the engine must NOT inject a synthetic window. Doing so
    would inflate charging_duration_s for the Elvis 9.5 h Färdigladdad scenario.

    FR-024 edge-case L132: synthetic injection only applies to currently-open
    pre-restart windows.
    """
    pre_restart_duration_s = 3600  # 1 hour recorded before HA shutdown
    now_utc = dt_util.utcnow()
    charging_started = (now_utc - timedelta(hours=2)).isoformat()
    charging_ended = (now_utc - timedelta(hours=1)).isoformat()  # window was closed
    energy_start = 0.0
    energy_at_restart = 7.0

    snapshot = _make_snapshot(
        energy_start_kwh=energy_start,
        energy_kwh=energy_at_restart,
        charging_started_at=charging_started,
        charging_duration_s=pre_restart_duration_s,
        charging_window_count=1,
    )
    # Inject charging_ended_at to simulate 'charged' sub-state at shutdown
    snapshot["charging_ended_at"] = charging_ended

    entry, _save = await _make_engine_entry_with_restart(
        hass,
        snapshot,
        plug_state="on",
        cable_lock_state="Locked",
        power_state="0.0",  # idle at restart (charged state, BMS finished)
        energy_state=str(energy_at_restart),
    )
    engine = _get_engine(hass, entry)
    assert engine.state == SessionEngineState.TRACKING

    # No synthetic window must have been injected — tracker must be empty
    tracker = engine.window_tracker
    assert tracker.closed_window_count() == 0, (
        f"TC-020(f): expected 0 synthetic windows when charging_ended_at is set, "
        f"got {tracker.closed_window_count()}"
    )

    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Unplug immediately — session finalises
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE, (
        f"TC-020(f): expected IDLE after unplug, got {engine.state}"
    )
    assert len(session_store.sessions) == 1

    completed = session_store.sessions[0]
    actual_duration = completed["charging_duration_s"]

    # The key invariant of A1: when charging_ended_at is set (window was already
    # closed at shutdown), no synthetic window must be injected. A synthetic
    # window covering charging_started_at→recovery_time would add ~1 hour to
    # the duration (charging_started_at is 2 hours ago, charging_ended_at is
    # 1 hour ago). Without the guard the inflated value would be >> 3600s.
    # With the guard, no synthetic window is present, so the window tracker
    # contributes 0s (no post-restart windows opened either). The final value
    # must NOT exceed the pre-restart recorded duration.
    assert actual_duration <= pre_restart_duration_s, (
        f"TC-020(f): charging_duration_s={actual_duration}s should not exceed the "
        f"pre-restart recorded duration ({pre_restart_duration_s}s). "
        f"A larger value indicates a synthetic window was incorrectly injected."
    )
    # Confirm window tracker received no synthetic windows (structural guard)
    assert completed.get("charging_window_count", 0) <= 1, (
        "TC-020(f): session window count should not be inflated by a synthetic window"
    )
