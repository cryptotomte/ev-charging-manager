"""Tests for HEARTBEAT log timer and UI dispatch tick on PlugAnchoredSessionEngine.

PR-23, US5 — FR-012..FR-016.

Scenarios:
  (a) TRACKING + open window + freezer.tick(5 min) = one new HEARTBEAT line in log
  (b) IDLE state + freezer.tick(15 min) = zero HEARTBEAT lines
  (c) heartbeat_log_interval_min=0 + freezer.tick(1 h) = zero HEARTBEAT lines
  (d) freezer.tick(60 s) with ui_dispatch_interval_s=60 = SIGNAL_SESSION_UPDATE dispatched
  (e) ui_dispatch_interval_s=0 + freezer.tick(5 min) = zero spontaneous dispatches
  (f) reload mid-cycle = listeners cleanly replaced (no double-fire)
  (g) HEARTBEAT line matches the regex from contracts/debug-log-format.md
"""

from __future__ import annotations

import re
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ev_charging_manager.const import (
    CONF_CHARGING_IDLE_TIMEOUT_MIN,
    CONF_DEBUG_LOGGING,
    CONF_DISCONNECT_GRACE_MIN,
    CONF_HEARTBEAT_LOG_INTERVAL_MIN,
    CONF_UI_DISPATCH_INTERVAL_S,
    DEFAULT_HEARTBEAT_LOG_INTERVAL_MIN,
    DEFAULT_UI_DISPATCH_INTERVAL_S,
    DOMAIN,
    SIGNAL_SESSION_UPDATE,
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

# Regex matching the HEARTBEAT line format from contracts/debug-log-format.md.
# Example line:
#   2026-05-23T14:32:17.123 | HEARTBEAT       | state=charging window=1
#     session_id=ae0afefb-... wh=4.512 power=3680 connection_s=4925 charging_s=4920
HEARTBEAT_RE = re.compile(
    r"^[\d\-T:.+]+ \| HEARTBEAT\s+\| state=(?:charging|charged|initializing) "
    r"window=\d+ session_id=[\w-]+ wh=\d+\.\d{3} power=\d+ "
    r"connection_s=\d+ charging_s=\d+$"
)


async def _make_engine_entry(
    hass: HomeAssistant,
    *,
    heartbeat_interval_min: int = DEFAULT_HEARTBEAT_LOG_INTERVAL_MIN,
    ui_dispatch_interval_s: int = DEFAULT_UI_DISPATCH_INTERVAL_S,
    enable_debug_logging: bool = True,
    tmp_path: object | None = None,
) -> MockConfigEntry:
    """Create a config entry with PlugAnchoredSessionEngine active.

    Configures heartbeat and UI dispatch intervals for testing. The RFID wait
    (PR-24 event-driven model) is not exercised by these tests.
    """
    if enable_debug_logging and tmp_path is not None:
        hass.config.config_dir = str(tmp_path)

    options: dict = {
        "plug_entity": MOCK_PLUG_ENTITY,
        "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
        CONF_CHARGING_IDLE_TIMEOUT_MIN: 5,
        CONF_DISCONNECT_GRACE_MIN: 10,
        CONF_HEARTBEAT_LOG_INTERVAL_MIN: heartbeat_interval_min,
        CONF_UI_DISPATCH_INTERVAL_S: ui_dispatch_interval_s,
    }
    if enable_debug_logging:
        options[CONF_DEBUG_LOGGING] = True

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options=options,
        title="Test go-e Charger (Heartbeat)",
    )

    # Pre-set charger entities to idle
    hass.states.async_set(MOCK_PLUG_ENTITY, "off")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    hass.states.async_set(MOCK_POWER_ENTITY, "0.0")

    entry.add_to_hass(hass)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    return entry


def _get_engine(hass: HomeAssistant, entry: MockConfigEntry) -> PlugAnchoredSessionEngine:
    """Return the PlugAnchoredSessionEngine from hass.data."""
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert isinstance(engine, PlugAnchoredSessionEngine), (
        f"Expected PlugAnchoredSessionEngine, got {type(engine).__name__}"
    )
    return engine


def _get_debug_logger(hass: HomeAssistant, entry: MockConfigEntry):
    """Return the DebugLogger for the entry."""
    return hass.data[DOMAIN][entry.entry_id]["debug_logger"]


def _read_log_lines(hass: HomeAssistant, entry: MockConfigEntry) -> list[str]:
    """Return all emitted non-empty log lines: on-disk plus still-buffered.

    PR-28: log lines are buffered in memory and flushed off-loop, so a line
    emitted just before this call may not have reached the file yet. These
    tests assert WHAT was logged, not flush timing — include the buffer.
    """
    debug_logger = _get_debug_logger(hass, entry)
    if debug_logger is None or not debug_logger.enabled:
        return []
    try:
        with open(debug_logger.file_path, encoding="utf-8") as fh:
            lines = [line.rstrip("\n") for line in fh if line.strip()]
    except FileNotFoundError:
        lines = []
    lines.extend(line.rstrip("\n") for line in debug_logger._buffer if line.strip())
    return lines


def _count_heartbeat_lines(lines: list[str]) -> int:
    """Count lines containing the HEARTBEAT category marker."""
    return sum(1 for line in lines if "| HEARTBEAT" in line)


# ---------------------------------------------------------------------------
# Helper: put the engine into TRACKING state with an open charging window
# ---------------------------------------------------------------------------


async def _setup_tracking_with_window(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """Drive the engine into TRACKING state with an open charging window.

    Sequence: plug in → RFID (trx="2") → power > 0.
    """
    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
    await hass.async_block_till_done()

    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    await hass.async_block_till_done()

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.5")
    hass.states.async_set(MOCK_POWER_ENTITY, "3680.0")
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Scenario (a): TRACKING + open window → one HEARTBEAT line after 5 min
# ---------------------------------------------------------------------------


async def test_heartbeat_fires_during_tracking_with_open_window(
    hass: HomeAssistant,
    freezer,
    tmp_path,
) -> None:
    """Scenario (a): steady-state TRACKING + open window → one HEARTBEAT line.

    Advance time by exactly the interval (5 min default). The debug log must
    contain exactly one new HEARTBEAT line with all eight fields parseable.
    FR-012, FR-014.
    """
    entry = await _make_engine_entry(
        hass,
        heartbeat_interval_min=5,
        ui_dispatch_interval_s=0,  # isolate heartbeat from UI dispatch
        tmp_path=tmp_path,
    )
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _setup_tracking_with_window(hass, entry)

        assert engine.state == SessionEngineState.TRACKING
        assert engine.active_session is not None
        assert engine._window_tracker.is_open()

        # Capture log lines before the tick
        lines_before = _read_log_lines(hass, entry)
        hb_before = _count_heartbeat_lines(lines_before)

        # Advance time past the 5-minute interval
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        lines_after = _read_log_lines(hass, entry)
        hb_after = _count_heartbeat_lines(lines_after)

    assert hb_after - hb_before == 1, (
        f"Expected exactly 1 new HEARTBEAT line, got {hb_after - hb_before}. "
        f"All lines: {lines_after}"
    )


# ---------------------------------------------------------------------------
# Scenario (b): IDLE state → zero HEARTBEAT lines
# ---------------------------------------------------------------------------


async def test_heartbeat_silent_in_idle_state(
    hass: HomeAssistant,
    freezer,
    tmp_path,
) -> None:
    """Scenario (b): IDLE state → advancing past 3 intervals produces no HEARTBEAT lines.

    The engine is idle (no plug-in). The HEARTBEAT callback guards on TRACKING state,
    so no lines must appear even after 15 minutes. FR-014.
    """
    entry = await _make_engine_entry(
        hass,
        heartbeat_interval_min=5,
        ui_dispatch_interval_s=0,
        tmp_path=tmp_path,
    )
    engine = _get_engine(hass, entry)

    assert engine.state == SessionEngineState.IDLE

    lines_before = _read_log_lines(hass, entry)
    hb_before = _count_heartbeat_lines(lines_before)

    # Advance far past the interval
    freezer.tick(timedelta(minutes=15))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    lines_after = _read_log_lines(hass, entry)
    hb_after = _count_heartbeat_lines(lines_after)

    assert hb_after - hb_before == 0, (
        f"Expected 0 new HEARTBEAT lines in IDLE state, got {hb_after - hb_before}. "
        f"Lines: {lines_after}"
    )


# ---------------------------------------------------------------------------
# Scenario (c): heartbeat_log_interval_min=0 → timer not registered, no lines
# ---------------------------------------------------------------------------


async def test_heartbeat_disabled_when_interval_zero(
    hass: HomeAssistant,
    freezer,
    tmp_path,
) -> None:
    """Scenario (c): heartbeat_log_interval_min=0 disables HEARTBEAT entirely.

    The engine is in TRACKING with an open window. After 1 hour, the debug log
    must contain zero HEARTBEAT lines. FR-016 (disabling one timer must not
    affect the other).
    """
    entry = await _make_engine_entry(
        hass,
        heartbeat_interval_min=0,  # disabled
        ui_dispatch_interval_s=0,  # also disabled — isolate
        tmp_path=tmp_path,
    )
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _setup_tracking_with_window(hass, entry)

        assert engine.state == SessionEngineState.TRACKING
        assert engine.active_session is not None

        lines_before = _read_log_lines(hass, entry)
        hb_before = _count_heartbeat_lines(lines_before)

        # Advance far past where a 5-min timer would fire
        freezer.tick(timedelta(hours=1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        lines_after = _read_log_lines(hass, entry)
        hb_after = _count_heartbeat_lines(lines_after)

    assert hb_after - hb_before == 0, (
        f"Expected 0 new HEARTBEAT lines when interval=0, got {hb_after - hb_before}. "
        f"Lines: {lines_after}"
    )
    # Structural guard: no timer must be registered when interval=0 (FR-016).
    assert engine._heartbeat_log_timer_unsub is None, (
        "with heartbeat_log_interval_min=0, no heartbeat timer should be registered"
    )


async def test_heartbeat_timer_registered_when_interval_nonzero(
    hass: HomeAssistant,
    freezer,
    tmp_path,
) -> None:
    """Positive control for scenario (c): interval=5 → timer is registered.

    Confirms that _heartbeat_log_timer_unsub is not None when a non-zero interval
    is configured, proving the scenario (c) guard is not trivially true.
    """
    entry = await _make_engine_entry(
        hass,
        heartbeat_interval_min=5,
        ui_dispatch_interval_s=0,
        tmp_path=tmp_path,
    )
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _setup_tracking_with_window(hass, entry)

    assert engine._heartbeat_log_timer_unsub is not None, (
        "with heartbeat_log_interval_min=5, a heartbeat timer must be registered"
    )


# ---------------------------------------------------------------------------
# Scenario (d): ui_dispatch_interval_s=60 → SIGNAL_SESSION_UPDATE dispatched
# ---------------------------------------------------------------------------


async def test_ui_dispatch_fires_during_tracking(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Scenario (d): ui_dispatch_interval_s=60 → SIGNAL_SESSION_UPDATE is sent.

    Advances 60 s while in TRACKING; verifies at least one dispatch occurred.
    FR-013, FR-014.
    """
    entry = await _make_engine_entry(
        hass,
        heartbeat_interval_min=0,  # isolate UI dispatch
        ui_dispatch_interval_s=60,
        enable_debug_logging=False,
    )
    engine = _get_engine(hass, entry)

    dispatch_count = 0

    def _on_update() -> None:
        nonlocal dispatch_count
        dispatch_count += 1

    signal = SIGNAL_SESSION_UPDATE.format(entry.entry_id)
    entry_unsub = async_dispatcher_connect(hass, signal, _on_update)

    try:
        with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
            await _setup_tracking_with_window(hass, entry)

            assert engine.state == SessionEngineState.TRACKING

            # Reset counter after the setup (which may fire dispatches on state changes)
            dispatch_count = 0

            # Advance past the dispatch interval
            freezer.tick(timedelta(seconds=60))
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()

        assert dispatch_count >= 1, (
            f"Expected at least 1 SIGNAL_SESSION_UPDATE dispatch after 60s, got {dispatch_count}"
        )
    finally:
        entry_unsub()


# ---------------------------------------------------------------------------
# Scenario (e): ui_dispatch_interval_s=0 → no spontaneous dispatches
# ---------------------------------------------------------------------------


async def test_ui_dispatch_disabled_when_interval_zero(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Scenario (e): ui_dispatch_interval_s=0 disables the dispatch timer.

    Engine in TRACKING, advance 5 minutes. No timer-triggered dispatches should
    occur (only state-change driven ones). FR-016.
    """
    entry = await _make_engine_entry(
        hass,
        heartbeat_interval_min=0,
        ui_dispatch_interval_s=0,  # disabled
        enable_debug_logging=False,
    )
    engine = _get_engine(hass, entry)

    dispatch_count = 0

    def _on_update() -> None:
        nonlocal dispatch_count
        dispatch_count += 1

    signal = SIGNAL_SESSION_UPDATE.format(entry.entry_id)
    entry_unsub = async_dispatcher_connect(hass, signal, _on_update)

    try:
        with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
            await _setup_tracking_with_window(hass, entry)

            assert engine.state == SessionEngineState.TRACKING

            # Reset counter after setup state changes
            dispatch_count = 0

            # Advance far past where a 60s timer would fire
            freezer.tick(timedelta(minutes=5))
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()

        # No timer-driven dispatches
        assert dispatch_count == 0, (
            f"Expected 0 timer-driven dispatches when ui_dispatch=0, got {dispatch_count}"
        )
        # Structural guard: no timer must be registered when interval=0 (FR-016).
        assert engine._ui_dispatch_timer_unsub is None, (
            "with ui_dispatch_interval_s=0, no UI dispatch timer should be registered"
        )
    finally:
        entry_unsub()


async def test_ui_dispatch_timer_registered_when_interval_nonzero(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Positive control for scenario (e): interval=60 → dispatch timer is registered.

    Confirms that _ui_dispatch_timer_unsub is not None when a non-zero interval
    is configured, proving the scenario (e) guard is not trivially true.
    """
    entry = await _make_engine_entry(
        hass,
        heartbeat_interval_min=0,
        ui_dispatch_interval_s=60,
        enable_debug_logging=False,
    )
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _setup_tracking_with_window(hass, entry)

    assert engine._ui_dispatch_timer_unsub is not None, (
        "with ui_dispatch_interval_s=60, a UI dispatch timer must be registered"
    )


# ---------------------------------------------------------------------------
# Scenario (f): reload mid-cycle → no double-fire
# ---------------------------------------------------------------------------


async def test_heartbeat_no_double_fire_after_reload(
    hass: HomeAssistant,
    freezer,
    tmp_path,
) -> None:
    """Scenario (f): after entry reload, the HEARTBEAT listener is replaced.

    Reloading the entry must cancel the old timer and register a new one.
    After reload, advancing time past one interval must produce exactly one
    HEARTBEAT line — not two (double-fire). FR-015.
    """
    entry = await _make_engine_entry(
        hass,
        heartbeat_interval_min=5,
        ui_dispatch_interval_s=0,
        tmp_path=tmp_path,
    )

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _setup_tracking_with_window(hass, entry)

        engine = _get_engine(hass, entry)
        assert engine.state == SessionEngineState.TRACKING

        # Advance just under the interval (no heartbeat yet)
        freezer.tick(timedelta(minutes=4))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Reload the entry — this should cancel the old timer
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    # After reload the entry is set up again; re-fetch to get the new reference
    entry = hass.config_entries.async_get_entry(entry.entry_id)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Reset charger to idle so the state changes actually fire on _setup
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        hass.states.async_set(MOCK_TRX_ENTITY, "null")
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()

        await _setup_tracking_with_window(hass, entry)

        engine = _get_engine(hass, entry)
        assert engine.state == SessionEngineState.TRACKING

        lines_before = _read_log_lines(hass, entry)
        hb_before = _count_heartbeat_lines(lines_before)

        # Advance past the interval — should fire exactly ONCE (not twice)
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        lines_after = _read_log_lines(hass, entry)
        hb_after = _count_heartbeat_lines(lines_after)

    assert hb_after - hb_before == 1, (
        f"Expected exactly 1 HEARTBEAT after reload (no double-fire), "
        f"got {hb_after - hb_before}. Lines: {lines_after}"
    )


# ---------------------------------------------------------------------------
# Scenario (g): HEARTBEAT line matches the regex from contracts/debug-log-format.md
# ---------------------------------------------------------------------------


async def test_heartbeat_line_matches_format_regex(
    hass: HomeAssistant,
    freezer,
    tmp_path,
) -> None:
    """Scenario (g): every emitted HEARTBEAT line passes the format regex.

    Advances past the interval and validates the actual log line format.
    FR-012.
    """
    entry = await _make_engine_entry(
        hass,
        heartbeat_interval_min=5,
        ui_dispatch_interval_s=0,
        tmp_path=tmp_path,
    )
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _setup_tracking_with_window(hass, entry)

        assert engine.state == SessionEngineState.TRACKING
        assert engine.active_session is not None

        # Advance past interval
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        lines_after = _read_log_lines(hass, entry)

    heartbeat_lines = [line for line in lines_after if "| HEARTBEAT" in line]
    assert len(heartbeat_lines) >= 1, (
        f"Expected at least one HEARTBEAT line in log, got none. Lines: {lines_after}"
    )
    for line in heartbeat_lines:
        assert HEARTBEAT_RE.match(line), (
            f"HEARTBEAT line does not match format regex:\n"
            f"  line:  {line!r}\n"
            f"  regex: {HEARTBEAT_RE.pattern}"
        )


# ---------------------------------------------------------------------------
# Scenario (h): HEARTBEAT fires in 'charged' sub-state with power=0 (A6 / Contract Test #4)
# ---------------------------------------------------------------------------


async def test_heartbeat_fires_in_charged_substate(
    hass: HomeAssistant,
    freezer,
    tmp_path,
) -> None:
    """Scenario (h): HEARTBEAT emits with state=charged and power=0 after window closes.

    Contract Test #4 from contracts/debug-log-format.md. Also exercises the A4
    truthy-or fix: genuine power=0 between charging windows must appear as 0, not
    as a stale cached value.

    Setup:
      1. Open window 1, advance past HEARTBEAT interval (verify 'charging').
      2. Drop power to 0, advance past charging_idle_timeout_min → window 1 closes
         (sub-state becomes 'charged').
      3. Advance another HEARTBEAT interval.
      4. Assert a HEARTBEAT line exists with state=charged and power=0.
    """
    idle_timeout_min = 5  # matches CONF_CHARGING_IDLE_TIMEOUT_MIN default

    entry = await _make_engine_entry(
        hass,
        heartbeat_interval_min=5,
        ui_dispatch_interval_s=0,
        tmp_path=tmp_path,
    )
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Step 1: plug in and open a charging window
        await _setup_tracking_with_window(hass, entry)
        assert engine.state == SessionEngineState.TRACKING
        assert engine._window_tracker.is_open(), "Precondition: window must be open"

        # Advance 5 min to fire one HEARTBEAT in 'charging' sub-state
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        lines_mid = _read_log_lines(hass, entry)
        charging_hb = [
            line for line in lines_mid if "| HEARTBEAT" in line and "state=charging" in line
        ]
        assert len(charging_hb) >= 1, (
            f"Expected at least one HEARTBEAT with state=charging before idle timeout; "
            f"lines: {lines_mid}"
        )

        # Step 2: drop power to 0 — idle timer starts
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()

        # Advance past idle timeout → window 1 closes, sub-state becomes 'charged'
        freezer.tick(timedelta(minutes=idle_timeout_min + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert not engine._window_tracker.is_open(), "Window must be closed after idle timeout"
        assert engine.get_status_sub_state() == "charged", (
            f"Expected 'charged' sub-state after window close, "
            f"got {engine.get_status_sub_state()!r}"
        )

        # Step 3: advance another HEARTBEAT interval in 'charged' sub-state
        lines_before_hb2 = _read_log_lines(hass, entry)
        hb_count_before = _count_heartbeat_lines(lines_before_hb2)

        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        lines_after_hb2 = _read_log_lines(hass, entry)

    # Step 4: assert HEARTBEAT was emitted with state=charged and power=0
    hb_count_after = _count_heartbeat_lines(lines_after_hb2)
    assert hb_count_after > hb_count_before, (
        f"Expected at least one new HEARTBEAT line in 'charged' sub-state; lines: {lines_after_hb2}"
    )

    charged_hb_lines = [
        line for line in lines_after_hb2 if "| HEARTBEAT" in line and "state=charged" in line
    ]
    assert len(charged_hb_lines) >= 1, (
        f"Expected HEARTBEAT with state=charged, but no such line found. "
        f"All HEARTBEAT lines: {[ln for ln in lines_after_hb2 if '| HEARTBEAT' in ln]}"
    )

    # The charged HEARTBEAT line must report power=0 (not a stale cached value)
    for line in charged_hb_lines:
        assert "power=0" in line, (
            f"HEARTBEAT in 'charged' sub-state must report power=0, got: {line!r}"
        )
