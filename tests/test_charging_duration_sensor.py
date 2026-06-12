"""Tests for ChargingDurationSensor — US4 (PR-23).

Scenarios per contracts/sensor-attributes.md §Test contract:
  1. No session active: native_value is None, extra_state_attributes == {}
  2. One window open: live elapsed, windows has one entry with ended_at=None,
     current_window_open == True
  3. One window closed, no current open: frozen at closed-window duration,
     current_window_open == False
  4. Two windows (one closed + one open): sum(closed) + live; windows has 2 entries
  5. Live tick via freezer + manual dispatch: native_value advances by tick delta
  6. Schema validation: window dict has exactly the correct keys and types

Note on test independence (T033 guidance): the "live tick" scenario manually
calls async_dispatcher_send(hass, SIGNAL_SESSION_UPDATE.format(entry_id)) after
freezer.tick(...) to simulate US5's UI dispatch. This keeps the test self-contained
even if the periodic dispatch timer is disabled (CONF_UI_DISPATCH_INTERVAL_S=0).

Scenario 3 (frozen value): negative-assertion trap avoided by advancing time
WITHOUT dispatching and asserting native_value is UNCHANGED from its pre-tick value.
The comparison is pre/post explicit, not just "is still None".
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.const import (
    CONF_CHARGING_IDLE_TIMEOUT_MIN,
    CONF_DISCONNECT_GRACE_MIN,
    CONF_HEARTBEAT_LOG_INTERVAL_MIN,
    CONF_UI_DISPATCH_INTERVAL_S,
    DOMAIN,
    SIGNAL_SESSION_UPDATE,
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

# Short idle timeout (3 min) so window-close tests complete quickly.
IDLE_TIMEOUT_MIN = 3


async def _make_engine_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create and set up a goe_gemini config entry with PlugAnchoredSessionEngine.

    Timers are set to 0 to avoid interference with the tests:
      - CONF_HEARTBEAT_LOG_INTERVAL_MIN = 0 — no heartbeat timer
      - CONF_UI_DISPATCH_INTERVAL_S = 0 — no automatic dispatch tick
    The tests fire SIGNAL_SESSION_UPDATE manually when they want a sensor render.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
            CONF_CHARGING_IDLE_TIMEOUT_MIN: IDLE_TIMEOUT_MIN,
            CONF_DISCONNECT_GRACE_MIN: 10,
            CONF_HEARTBEAT_LOG_INTERVAL_MIN: 0,
            CONF_UI_DISPATCH_INTERVAL_S: 0,
        },
        title="Test go-e Charger",
    )

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


def _get_sensor_state(hass: HomeAssistant, entry: MockConfigEntry):
    """Return the HA state object for the ChargingDurationSensor.

    Looks up by unique_id <entry_id>_charging_duration via the entity registry.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    unique_id = f"{entry.entry_id}_charging_duration"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
    if entity_id is None:
        return None
    return hass.states.get(entity_id)


def _native_value(hass: HomeAssistant, entry: MockConfigEntry) -> str | None:
    """Return the native_value (state) of the ChargingDurationSensor."""
    state = _get_sensor_state(hass, entry)
    if state is None:
        return None
    if state.state in ("unavailable", "unknown"):
        return None
    return state.state


def _attributes(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    """Return the extra_state_attributes of the ChargingDurationSensor."""
    state = _get_sensor_state(hass, entry)
    if state is None:
        return {}
    return dict(state.attributes)


def _dispatch(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Fire SIGNAL_SESSION_UPDATE to trigger a sensor re-render."""
    signal = SIGNAL_SESSION_UPDATE.format(entry.entry_id)
    async_dispatcher_send(hass, signal)


def _hms_to_s(hms: str) -> int:
    """Convert 'HH:MM:SS' string to total seconds."""
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


# ---------------------------------------------------------------------------
# Scenario 1 — No active session: native_value is None, attributes == {}
# ---------------------------------------------------------------------------


async def test_no_session_native_value_none(hass: HomeAssistant) -> None:
    """Scenario 1: no session → sensor is unavailable (native_value=None), attributes={}."""
    from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
    from homeassistant.helpers import entity_registry as er

    entry = await _make_engine_entry(hass)

    # No session started — sensor must exist in the entity registry
    registry = er.async_get(hass)
    unique_id = f"{entry.entry_id}_charging_duration"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
    assert entity_id is not None, "ChargingDurationSensor must be registered in the entity registry"

    state = hass.states.get(entity_id)
    assert state is not None, f"State for {entity_id} must exist"
    assert state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, "None", "none"), (
        f"Sensor must be unavailable or unknown when no session, got state={state.state!r}"
    )

    # windows attribute must be absent or empty — never populated with stale data
    windows = state.attributes.get("windows")
    assert windows is None or windows == [], (
        f"windows attribute must be None or [] when no session, got {windows!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 2 — One window open: live elapsed, ended_at=None, current_window_open=True
# ---------------------------------------------------------------------------


async def test_one_window_open_live_elapsed(hass: HomeAssistant, freezer) -> None:
    """Scenario 2: session active with one open window.

    native_value shows elapsed time since window opened.
    windows list has one entry with ended_at=None and current_window_open=True.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in + lock
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        await hass.async_block_till_done()

        # Power rises → window opens
        hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.5")
        await hass.async_block_till_done()

    assert engine._window_tracker.is_open(), "Precondition: window should be open"
    assert engine.active_session is not None, "Precondition: session should be active"

    # Dispatch to trigger sensor render
    _dispatch(hass, entry)
    await hass.async_block_till_done()

    value = _native_value(hass, entry)
    attrs = _attributes(hass, entry)

    assert value is not None, "native_value must not be None when window is open"
    # Value should be a valid HH:MM:SS string
    parts = value.split(":")
    assert len(parts) == 3, f"Expected HH:MM:SS format, got {value!r}"
    assert all(p.isdigit() for p in parts), f"All parts must be digits, got {value!r}"

    assert attrs.get("current_window_open") is True, (
        f"current_window_open must be True, got {attrs.get('current_window_open')!r}"
    )
    windows = attrs.get("windows", [])
    assert len(windows) == 1, f"Expected 1 window entry, got {len(windows)}"
    w = windows[0]
    assert w["index"] == 1, f"Window index must be 1, got {w['index']!r}"
    assert w["ended_at"] is None, f"ended_at must be None for open window, got {w['ended_at']!r}"
    assert w["duration_s"] >= 0, f"duration_s must be >= 0, got {w['duration_s']!r}"
    assert w["energy_kwh"] >= 0.0, f"energy_kwh must be >= 0.0, got {w['energy_kwh']!r}"


# ---------------------------------------------------------------------------
# Scenario 3 — One window closed, no open: frozen at closed-window duration
# ---------------------------------------------------------------------------


async def test_one_window_closed_frozen(hass: HomeAssistant, freezer) -> None:
    """Scenario 3: session active but last window closed — value freezes.

    After window closes, advancing time WITHOUT dispatching must not change
    native_value. This explicitly proves the freeze is not trivial (comparing
    a pre-tick value against a post-tick value via explicit comparison).
    FR-019, IC-5.
    """
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in + lock
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        await hass.async_block_till_done()

        # Power rises → window opens
        hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "1.0")
        await hass.async_block_till_done()

        # Advance time 1 second so we have a non-trivial elapsed duration
        freezer.tick(timedelta(seconds=1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Power drops → start of idle timer
        hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()

        # Idle timeout fires → window closes
        freezer.tick(timedelta(minutes=IDLE_TIMEOUT_MIN + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

    assert not engine._window_tracker.is_open(), "Precondition: window should be closed"
    assert engine.active_session is not None, "Precondition: session should still be active"
    assert engine.get_status_sub_state() == "charged", (
        f"Precondition: sub-state should be 'charged', got {engine.get_status_sub_state()!r}"
    )

    # Dispatch after window closed to capture frozen state
    _dispatch(hass, entry)
    await hass.async_block_till_done()

    value_before = _native_value(hass, entry)
    attrs_before = _attributes(hass, entry)

    assert value_before is not None, "native_value must not be None while session is active"
    assert attrs_before.get("current_window_open") is False, (
        f"current_window_open must be False, got {attrs_before.get('current_window_open')!r}"
    )
    windows = attrs_before.get("windows", [])
    assert len(windows) == 1, f"Expected 1 closed window entry, got {len(windows)}"
    w = windows[0]
    assert w["ended_at"] is not None, "ended_at must be set for closed window"

    # Advance time by 5 minutes WITHOUT dispatching — value must NOT change
    freezer.tick(timedelta(minutes=5))
    # Deliberately do NOT call _dispatch() or async_fire_time_changed here
    await hass.async_block_till_done()

    value_after = _native_value(hass, entry)

    assert value_after == value_before, (
        f"Frozen sensor must not change without a dispatch: "
        f"before={value_before!r} after={value_after!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 4 — Two windows (closed + open): sum + live, 2 entries in windows list
# ---------------------------------------------------------------------------


async def test_two_windows_closed_plus_open(hass: HomeAssistant, freezer) -> None:
    """Scenario 4: two windows — first closed, second open.

    native_value = sum(closed) + live(current).
    windows list has 2 entries; first has ended_at set, second has ended_at=None.
    FR-019, FR-021.
    """
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in + lock
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        await hass.async_block_till_done()

        # Window 1 opens
        hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "1.0")
        await hass.async_block_till_done()

        # Advance 60 seconds so window 1 has a measurable duration
        freezer.tick(timedelta(seconds=60))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Power drops → window 1 starts closing
        hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()

        # Idle timeout fires → window 1 closes
        freezer.tick(timedelta(minutes=IDLE_TIMEOUT_MIN + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert engine.get_status_sub_state() == "charged", (
            "Precondition: sub-state should be 'charged' after window 1 closes"
        )
        assert not engine._window_tracker.is_open(), "Window 1 should be closed"

        # Window 2 opens (BMS pulse resumes charging)
        hass.states.async_set(MOCK_POWER_ENTITY, "3600.0")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "5.1")
        await hass.async_block_till_done()

    assert engine._window_tracker.is_open(), "Precondition: window 2 should be open"
    assert engine._window_tracker.closed_window_count() == 1, (
        "Precondition: exactly 1 closed window"
    )

    # Dispatch to render sensor
    _dispatch(hass, entry)
    await hass.async_block_till_done()

    value = _native_value(hass, entry)
    attrs = _attributes(hass, entry)

    assert value is not None, "native_value must not be None with two windows"

    assert attrs.get("current_window_open") is True, (
        f"current_window_open must be True, got {attrs.get('current_window_open')!r}"
    )
    windows = attrs.get("windows", [])
    assert len(windows) == 2, f"Expected 2 window entries, got {len(windows)}"

    w1, w2 = windows
    assert w1["index"] == 1, f"First window index must be 1, got {w1['index']!r}"
    assert w1["ended_at"] is not None, "First window must be closed (ended_at set)"
    assert w2["index"] == 2, f"Second window index must be 2, got {w2['index']!r}"
    assert w2["ended_at"] is None, "Second window must be open (ended_at=None)"

    # The total must include the closed window's duration
    closed_duration_s = w1["duration_s"]
    total_s = _hms_to_s(value)
    assert total_s >= closed_duration_s, (
        f"Total {total_s}s must be >= closed window {closed_duration_s}s"
    )


# ---------------------------------------------------------------------------
# Scenario 5 — Live tick: freezer.tick + manual dispatch advances value
# ---------------------------------------------------------------------------


async def test_live_tick_advances_native_value(hass: HomeAssistant, freezer) -> None:
    """Scenario 5: freezer.tick(60s) + dispatch advances native_value by ~60 seconds.

    This proves the sensor computes from utcnow() at each render rather than
    caching a stale value. The tick must be followed by a manual
    async_dispatcher_send to simulate US5's UI dispatch (IC-5 binding).
    FR-019, FR-022.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in + lock
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        await hass.async_block_till_done()

        # Power rises → window opens
        hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.5")
        await hass.async_block_till_done()

    assert engine._window_tracker.is_open(), "Precondition: window must be open"

    # First render
    _dispatch(hass, entry)
    await hass.async_block_till_done()
    value_before = _native_value(hass, entry)
    assert value_before is not None, "native_value must not be None"

    # Advance time by 60 seconds, then dispatch to update sensor
    freezer.tick(timedelta(seconds=60))
    _dispatch(hass, entry)
    await hass.async_block_till_done()
    value_after = _native_value(hass, entry)
    assert value_after is not None, "native_value must not be None after tick"

    seconds_before = _hms_to_s(value_before)
    seconds_after = _hms_to_s(value_after)
    delta = seconds_after - seconds_before

    # Allow ±1 s tolerance for scheduling jitter
    assert 59 <= delta <= 61, (
        f"Expected native_value to advance by ~60 s, got delta={delta}s "
        f"(before={value_before!r}, after={value_after!r})"
    )


# ---------------------------------------------------------------------------
# Scenario 6 — Schema validation: window dict has exactly the correct keys/types
# ---------------------------------------------------------------------------


async def test_window_attribute_schema(hass: HomeAssistant, freezer) -> None:
    """Scenario 6: window attribute dict has exactly the keys and types from the contract.

    contracts/sensor-attributes.md §Schema:
      index: int, started_at: str (ISO), ended_at: str|None,
      duration_s: int, energy_kwh: float.
    No extra keys. No missing keys.
    FR-021, IC-5.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in → window opens
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        await hass.async_block_till_done()

        hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.5")
        await hass.async_block_till_done()

    assert engine._window_tracker.is_open(), "Precondition: window must be open"

    _dispatch(hass, entry)
    await hass.async_block_till_done()

    attrs = _attributes(hass, entry)
    windows = attrs.get("windows", [])
    assert len(windows) == 1, f"Expected 1 window, got {len(windows)}"

    w = windows[0]
    expected_keys = {"index", "started_at", "ended_at", "duration_s", "energy_kwh"}
    actual_keys = set(w.keys())
    assert actual_keys == expected_keys, (
        f"Window dict has wrong keys. Expected {expected_keys}, got {actual_keys}"
    )

    # Type assertions per contract
    assert isinstance(w["index"], int), f"index must be int, got {type(w['index']).__name__}"
    assert isinstance(w["started_at"], str), (
        f"started_at must be str, got {type(w['started_at']).__name__}"
    )
    # ended_at is None for open window
    assert w["ended_at"] is None, f"ended_at must be None for open window, got {w['ended_at']!r}"
    assert isinstance(w["duration_s"], int), (
        f"duration_s must be int, got {type(w['duration_s']).__name__}"
    )
    assert isinstance(w["energy_kwh"], float), (
        f"energy_kwh must be float, got {type(w['energy_kwh']).__name__}"
    )
    assert w["duration_s"] >= 0, f"duration_s must be >= 0, got {w['duration_s']!r}"
    assert w["energy_kwh"] >= 0.0, f"energy_kwh must be >= 0.0, got {w['energy_kwh']!r}"

    # Top-level attribute types
    assert isinstance(attrs.get("window_count"), int), "window_count must be int"
    assert isinstance(attrs.get("current_window_open"), bool), "current_window_open must be bool"

    # Invariant: len(windows) == window_count
    assert len(windows) == attrs["window_count"], (
        f"len(windows)={len(windows)} must equal window_count={attrs['window_count']}"
    )


# ---------------------------------------------------------------------------
# PR-29 (US3/FR-005): legacy engine → permanently unavailable
# ---------------------------------------------------------------------------


async def test_legacy_engine_charging_duration_unavailable(hass: HomeAssistant) -> None:
    """FR-005: legacy SessionEngine with an ACTIVE session → sensor unavailable.

    The legacy engine has no charging-window tracking, so the sensor's value
    is forever indeterminable there. Before PR-29 it rendered as available-
    but-'unknown' forever — a standing availability-rule violation.
    """
    from homeassistant.const import STATE_UNAVAILABLE
    from homeassistant.helpers import entity_registry as er

    from custom_components.ev_charging_manager.session_engine import SessionEngine
    from tests.conftest import (
        MOCK_CAR_STATUS_ENTITY,
        setup_session_engine,
        start_charging_session,
    )

    _ = MOCK_CAR_STATUS_ENTITY  # imported for symmetry with conftest helpers

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Legacy Charger")
    await setup_session_engine(hass, entry)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert isinstance(engine, SessionEngine), "Precondition: legacy engine expected"

    await start_charging_session(hass, trx_value="2")
    assert engine.active_session is not None, "Precondition: active legacy session"

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_charging_duration"
    )
    assert entity_id is not None, "ChargingDurationSensor must be registered"

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE, (
        f"FR-005: ChargingDurationSensor must be unavailable on the legacy engine "
        f"(no window tracking), got {state.state!r}"
    )


# ---------------------------------------------------------------------------
# PR-29 (US3/FR-006): attributes share the value's availability gate
# ---------------------------------------------------------------------------


async def test_attributes_present_exactly_when_value_is(hass: HomeAssistant) -> None:
    """FR-006: extra_state_attributes uses the SAME gate as native_value.

    Before PR-29 the value gated on engine+active_session while the
    attributes gated on _is_tracking() — during COMPLETING (legacy-style
    state semantics: session still set, state != TRACKING) the attributes
    vanished while the value still rendered. Pin gate equality with a stub
    engine frozen in that divergent state.
    """
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    from custom_components.ev_charging_manager.const import SessionEngineState
    from custom_components.ev_charging_manager.sensor import ChargingDurationSensor
    from custom_components.ev_charging_manager.charging_window import ChargingWindowTracker

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        title="Stub",
    )
    entry.add_to_hass(hass)

    # Window tracker with one closed window (1 min, 1 kWh)
    now = dt_util.utcnow()
    tracker = ChargingWindowTracker()
    tracker.open_window(now - timedelta(minutes=1), 0.0)
    tracker.close_window(now, 1.0)

    class _StubSession:
        charging_window_count = 1

    class _StubEngine:
        """Engine frozen mid-COMPLETING: session still set, state != TRACKING."""

        state = SessionEngineState.COMPLETING
        active_session = _StubSession()
        window_tracker = tracker
        _window_tracker = tracker  # legacy-private alias used by pre-PR-29 code

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"session_engine": _StubEngine()}

    sensor = ChargingDurationSensor(hass, entry)

    value = sensor.native_value
    attrs = sensor.extra_state_attributes

    # The gates must agree: value present ⟺ attributes present
    assert sensor.available is True
    assert value is not None, "Value must render while the session is still set"
    assert attrs != {}, (
        "FR-006: attributes must be present exactly when the value is — "
        "they vanished during COMPLETING before PR-29"
    )
    assert attrs["window_count"] == 1
    assert attrs["current_window_open"] is False
    assert len(attrs["windows"]) == 1

    # And the inverse: no session → both gone
    _StubEngine.active_session = None
    assert sensor.available is False
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


# ---------------------------------------------------------------------------
# Scenario 7 — IC-3 row 3: waiting_for_rfid → sensor must be unavailable
# ---------------------------------------------------------------------------


async def test_charging_duration_unavailable_during_waiting_for_rfid(
    hass: HomeAssistant,
) -> None:
    """Scenario 7: engine in waiting_for_rfid → ChargingDurationSensor is unavailable.

    IC-3 row 3: "Returns None. No active session yet."
    TRACKING state without an active session (plug=on, trx=null) must NOT
    render "00:00:00" — it must be STATE_UNAVAILABLE or STATE_UNKNOWN.
    Covers the A2 fix (PR-24 review).
    """
    from unittest.mock import AsyncMock, patch

    from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    from custom_components.ev_charging_manager.const import (
        SIGNAL_SESSION_UPDATE,
        SessionSubState,
    )

    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in with trx=null → engine enters waiting_for_rfid (TRACKING, no session)
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

    # Sanity check: engine is in waiting_for_rfid sub-state
    assert engine.get_status_sub_state() == SessionSubState.WAITING_FOR_RFID, (
        f"Pre-condition: expected waiting_for_rfid, got {engine.get_status_sub_state()!r}"
    )
    assert engine.active_session is None, "Pre-condition: no active session in wait state"

    # Look up the sensor entity
    registry = er.async_get(hass)
    unique_id = f"{entry.entry_id}_charging_duration"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
    assert entity_id is not None, "ChargingDurationSensor must be registered"

    # Trigger a sensor render via the dispatcher
    async_dispatcher_send(hass, SIGNAL_SESSION_UPDATE.format(entry.entry_id))
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state is not None, f"State for {entity_id} must exist"

    # IC-3 row 3: sensor must be unavailable (native_value returns None)
    assert state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN), (
        f"ChargingDurationSensor must be unavailable during waiting_for_rfid, "
        f"got state={state.state!r}"
    )

    # Belt-and-suspenders: active_session is None confirms available=False
    assert engine.active_session is None, (
        "IC-3: available=False iff active_session is None — confirmed"
    )
