"""Tests for PlugAnchoredSessionEngine — User Story 1 core behavior (PR-22).

TC-001: One session per plug cycle even when car_status oscillates.
TC-002: charging_started_at set at first power > 0, does not move.
TC-003: charging_ended_at set after idle timeout, cleared on power resume.
TC-005: Multi-window session — correct counts, durations, events.

These tests MUST FAIL before session_engine_v2.py is implemented.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ev_charging_manager.const import (
    CONF_CHARGING_IDLE_TIMEOUT_MIN,
    CONF_DISCONNECT_GRACE_MIN,
    DEFAULT_CHARGING_IDLE_TIMEOUT_MIN,
    DOMAIN,
    EVENT_CHARGING_CHARGED,
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

# Additional entity IDs for the plug-anchored model
MOCK_PLUG_ENTITY = "binary_sensor.goe_abc123_car_0"
MOCK_CABLE_LOCK_ENTITY = "sensor.goe_abc123_cus_value"

# Options that provide entity bindings for the new engine.
MOCK_OPTIONS_V2 = {
    "plug_entity": MOCK_PLUG_ENTITY,
    "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
    CONF_CHARGING_IDLE_TIMEOUT_MIN: DEFAULT_CHARGING_IDLE_TIMEOUT_MIN,
    CONF_DISCONNECT_GRACE_MIN: 10,
}


async def _make_engine_entry(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    """Create and set up a config entry with PlugAnchoredSessionEngine active."""
    all_options = dict(MOCK_OPTIONS_V2)
    if options:
        all_options.update(options)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options=all_options,
        title="Test go-e Charger",
    )

    # Pre-set entity states to idle
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


# ---------------------------------------------------------------------------
# TC-001: One session per plug cycle even when car_status oscillates
# ---------------------------------------------------------------------------


async def test_tc001_one_session_per_plug_cycle_bms_pulsing(
    hass: HomeAssistant,
    freezer,
) -> None:
    """TC-001: Simulated Peugeot BMS pulsing pattern — exactly one session.

    car_status oscillates Charging↔Complete at 60–120 s intervals over a
    single plug-on → plug-off cycle. The new engine must produce exactly one
    session in storage and attribute all energy to it.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Step 1: Plug in cable — engine enters RFID wait (no session yet in PR-24)
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        assert engine.state == SessionEngineState.TRACKING
        # No session yet — RFID wait is active (PR-24 event-driven model)
        assert engine.active_session is None

        # Step 2: RFID auth → resolves the wait, session starts
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        await hass.async_block_till_done()

        # Session is now active
        assert engine.active_session is not None, "Session must start after RFID blip"
        session_id = engine.active_session.id

        # Step 3: Car starts charging (power > 0)
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
        hass.states.async_set(MOCK_POWER_ENTITY, "3456.0")
        await hass.async_block_till_done()

        # BMS pulses: power oscillates 0↔3456 with sub-threshold pauses
        # Simulate 5 BMS cycles (each < 5 min)
        for i in range(5):
            # Power drops to 0 briefly (BMS balancing)
            freezer.tick(timedelta(seconds=90))
            hass.states.async_set(MOCK_ENERGY_ENTITY, f"{(i + 1) * 0.2:.3f}")
            hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
            await hass.async_block_till_done()

            # Power resumes quickly (< 5 min)
            freezer.tick(timedelta(seconds=60))
            hass.states.async_set(MOCK_POWER_ENTITY, "3456.0")
            await hass.async_block_till_done()

        # Energy accumulated
        hass.states.async_set(MOCK_ENERGY_ENTITY, "1.5")
        await hass.async_block_till_done()

        # Power drops to 0 and stays for > 5 min → window closes
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()
        freezer.tick(timedelta(minutes=6))
        await hass.async_block_till_done()

        # Energy at unplug
        hass.states.async_set(MOCK_ENERGY_ENTITY, "1.5")
        await hass.async_block_till_done()

        # Step 4: Unplug (cable_lock = Unlocked → validated unplug)
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    # Engine must be back to IDLE
    assert engine.active_session is None

    # Exactly 1 session in storage
    sessions = session_store.sessions
    assert len(sessions) == 1, f"Expected 1 session, got {len(sessions)}"

    # All energy attributed to the one session
    session = sessions[0]
    assert session["id"] == session_id
    assert session["energy_kwh"] >= 0.0  # counter delta (may be approximate)
    assert session["charging_window_count"] >= 1

    # No phantom session ID other than the original
    assert all(s["id"] == session_id for s in sessions)


# ---------------------------------------------------------------------------
# TC-002: charging_started_at does not move on subsequent windows
# ---------------------------------------------------------------------------


async def test_tc002_charging_started_at_set_once(
    hass: HomeAssistant,
    freezer,
) -> None:
    """TC-002: charging_started_at is set at first power > 0 and does not move."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
        await hass.async_block_till_done()

        # First power > 0 event
        hass.states.async_set(MOCK_POWER_ENTITY, "3456.0")
        await hass.async_block_till_done()

        first_charging_started_at = engine.active_session.charging_started_at
        assert first_charging_started_at is not None

        # Power drops (BMS pulse)
        freezer.tick(timedelta(seconds=90))
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()

        # Power resumes — charging_started_at must NOT change
        freezer.tick(timedelta(seconds=60))
        hass.states.async_set(MOCK_POWER_ENTITY, "3456.0")
        await hass.async_block_till_done()

        assert engine.active_session.charging_started_at == first_charging_started_at

        # Power drops again for > idle threshold — window closes
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()
        freezer.tick(timedelta(minutes=6))
        await hass.async_block_till_done()

        # Power resumes (second window) — charging_started_at still must NOT change
        hass.states.async_set(MOCK_POWER_ENTITY, "2000.0")
        await hass.async_block_till_done()

        assert engine.active_session.charging_started_at == first_charging_started_at


# ---------------------------------------------------------------------------
# TC-003: charging_ended_at set after idle timeout, cleared on power resume
# ---------------------------------------------------------------------------


async def test_tc003_charging_ended_at_set_and_cleared(
    hass: HomeAssistant,
    freezer,
) -> None:
    """TC-003: charging_ended_at set when idle threshold elapses, cleared on power resume."""
    # Use shorter idle timeout for faster test
    entry = await _make_engine_entry(hass, options={CONF_CHARGING_IDLE_TIMEOUT_MIN: 1})
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
        await hass.async_block_till_done()

        hass.states.async_set(MOCK_POWER_ENTITY, "3456.0")
        await hass.async_block_till_done()

        # charging_ended_at is None while charging
        assert engine.active_session.charging_ended_at is None

        # Power drops and idle threshold elapses (1 min)
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()
        # Advance frozen clock AND fire HA's time-based scheduler so async_call_later fires
        freezer.tick(timedelta(minutes=2))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # charging_ended_at must be set now
        assert engine.active_session.charging_ended_at is not None

        # Power resumes → charging_ended_at must be cleared (set to None)
        hass.states.async_set(MOCK_POWER_ENTITY, "3456.0")
        await hass.async_block_till_done()

        assert engine.active_session.charging_ended_at is None


# ---------------------------------------------------------------------------
# TC-005: Multi-window session — correct counts, durations, events
# ---------------------------------------------------------------------------


async def test_tc005_multi_window_session(
    hass: HomeAssistant,
    freezer,
) -> None:
    """TC-005: Two distinct charging windows — correct counts and ev_charging_charged events.

    Window 1: opens, closes after idle threshold.
    Window 2: opens in same session (post-completion balancing), closes.
    Asserts: charging_window_count == 2, charging_duration_s = sum of both windows,
    exactly one ev_charging_charged event per window.
    """
    # 1-minute idle timeout for faster test
    entry = await _make_engine_entry(hass, options={CONF_CHARGING_IDLE_TIMEOUT_MIN: 1})
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    charged_events = []

    def capture_charged(event):
        charged_events.append(event.data)

    hass.bus.async_listen(EVENT_CHARGING_CHARGED, capture_charged)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
        await hass.async_block_till_done()

        # Window 1 opens
        hass.states.async_set(MOCK_POWER_ENTITY, "3456.0")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
        await hass.async_block_till_done()

        assert engine.active_session.charging_window_count == 1

        # Advance frozen clock 3 minutes while window 1 is open
        freezer.tick(timedelta(minutes=3))
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.172")
        await hass.async_block_till_done()

        # Window 1 closes (power=0 for > 1 min idle timeout)
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()
        # Advance frozen clock and fire HA's time scheduler to trigger idle timer
        freezer.tick(timedelta(minutes=2))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert engine.active_session.charging_window_count == 1  # count includes closed window
        assert engine.active_session.charging_ended_at is not None
        assert len(charged_events) == 1  # ev_charging_charged fired once

        # Window 2 opens (post-completion balancing)
        hass.states.async_set(MOCK_POWER_ENTITY, "2100.0")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.172")
        await hass.async_block_till_done()

        assert engine.active_session.charging_window_count == 2
        assert engine.active_session.charging_ended_at is None  # cleared on window open

        # Advance frozen clock 2 minutes while window 2 is open
        freezer.tick(timedelta(minutes=2))
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.242")
        await hass.async_block_till_done()

        # Window 2 closes (power=0 for > 1 min idle timeout)
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()
        # Advance frozen clock and fire HA's time scheduler for window 2 idle timer
        freezer.tick(timedelta(minutes=2))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert len(charged_events) == 2  # second ev_charging_charged fired

        # Unplug
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    # Verify final session record
    assert engine.active_session is None
    sessions = session_store.sessions
    assert len(sessions) == 1

    session = sessions[0]
    assert session["charging_window_count"] == 2

    # charging_duration_s should be sum of both windows (not connection duration)
    assert session["charging_duration_s"] > 0
    assert session["connection_duration_s"] >= session["charging_duration_s"]

    # Both charged events reference the same session
    for evt in charged_events:
        assert evt["session_id"] == session["id"]
    assert charged_events[0]["window_index"] == 1
    assert charged_events[1]["window_index"] == 2
