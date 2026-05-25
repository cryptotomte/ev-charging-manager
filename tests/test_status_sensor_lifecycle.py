"""TC-023, TC-005b: Status sensor lifecycle tests for PlugAnchoredSessionEngine (PR-22).

TC-023: Full lifecycle state transitions — StatusSensor value at each step:
        idle → initializing → charging → charged → idle.

TC-005b: Multi-window scenario — window 1 fires ev_charging_charged once, sensor
         returns 'charging' again for window 2, then 'charged', window count == 2.
         (TC-005 core assertions are in test_session_engine_plug_anchored.py; these
         tests focus on the StatusSensor value specifically.)
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
    DOMAIN,
    EVENT_CHARGING_CHARGED,
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

# Short idle timeout so timer-based tests complete quickly
IDLE_TIMEOUT_MIN = 3


async def _make_engine_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create and set up a config entry with PlugAnchoredSessionEngine active."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
            CONF_CHARGING_IDLE_TIMEOUT_MIN: IDLE_TIMEOUT_MIN,
            CONF_DISCONNECT_GRACE_MIN: 10,
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


def _get_status_sensor_state(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Return the current state of the status sensor for this entry."""
    # Status sensor unique_id is "{entry_id}_status"
    entity_id = "sensor.ev_charging_manager_status"
    state = hass.states.get(entity_id)
    if state is None:
        # Fall back to engine sub-state directly
        engine = _get_engine(hass, entry)
        return engine.get_status_sub_state()
    return state.state


# ---------------------------------------------------------------------------
# TC-023: Full lifecycle state machine transitions
# ---------------------------------------------------------------------------


async def test_tc023_status_sensor_lifecycle(hass: HomeAssistant, freezer) -> None:
    """TC-023: StatusSensor transitions through idle→initializing→charging→charged→idle.

    Verifies that:
    - Before plug: 'idle'
    - After plug-in (no power, RFID resolved): 'initializing'
    - After power > 0: 'charging'
    - After power = 0 + idle timeout fires: 'charged'
    - After unplug: 'idle'
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # ---- Pre-plug: idle ----
        assert engine.get_status_sub_state() == "idle", (
            "TC-023: engine should be 'idle' before plug-in"
        )

        # ---- Plug in cable (no power yet): initializing ----
        # Set trx to a valid card index before plug-on so the event-driven RFID
        # wait resolves immediately (PR-24: no timer fallback). The session enters
        # INITIALIZING (formerly 'waiting') until power > 0.
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        assert engine.get_status_sub_state() == "initializing", (
            f"TC-023: expected 'initializing' after plug-in, got {engine.get_status_sub_state()!r}"
        )

        # ---- Power rises: charging ----
        hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.5")
        await hass.async_block_till_done()

        assert engine.get_status_sub_state() == "charging", (
            f"TC-023: expected 'charging' after power > 0, got {engine.get_status_sub_state()!r}"
        )

        # ---- Power drops to 0 — brief pause, still 'charging' (window still open) ----
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()

        # Window is still open until idle timeout fires — should still be 'charging'
        assert engine.get_status_sub_state() == "charging", (
            f"TC-023: expected 'charging' during brief power=0 (window open), "
            f"got {engine.get_status_sub_state()!r}"
        )

        # ---- Advance past idle timeout → window closes → 'charged' ----
        hass.states.async_set(MOCK_ENERGY_ENTITY, "5.2")
        await hass.async_block_till_done()

        freezer.tick(timedelta(minutes=IDLE_TIMEOUT_MIN + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert engine.get_status_sub_state() == "charged", (
            f"TC-023: expected 'charged' after idle timeout, got {engine.get_status_sub_state()!r}"
        )

        # ---- Unplug (cable_lock=Unlocked first, then plug=off) → 'idle' ----
        # Also clear trx to null (go-e firmware auto-clears trx after session ends;
        # without this, the idle engine would show 'waiting_for_plug' because trx="2"
        # is still set from the session that just completed).
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_TRX_ENTITY, "null")
        await hass.async_block_till_done()

        assert engine.get_status_sub_state() == "idle", (
            f"TC-023: expected 'idle' after unplug, got {engine.get_status_sub_state()!r}"
        )


# ---------------------------------------------------------------------------
# TC-005b: Multi-window status sensor transitions + ev_charging_charged events
# ---------------------------------------------------------------------------


async def test_tc005b_multi_window_status_sensor(hass: HomeAssistant, freezer) -> None:
    """TC-005b: Two windows — sensor goes 'charging' → 'charged' → 'charging' → 'charged'.

    Also verifies ev_charging_charged fires once per window close and
    charging_window_count is 2 at end.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    charged_events: list[dict] = []

    def _capture_charged_event(event) -> None:
        """Capture ev_charging_charged events."""
        charged_events.append(dict(event.data))

    hass.bus.async_listen(EVENT_CHARGING_CHARGED, _capture_charged_event)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # ---- Plug in ----
        # Set trx to a valid card index before plug-on so the event-driven RFID
        # wait resolves immediately (PR-24: no timer fallback). The session enters
        # INITIALIZING (formerly 'waiting') until power > 0.
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        assert engine.get_status_sub_state() == "initializing"

        # ---- Window 1: start charging ----
        hass.states.async_set(MOCK_POWER_ENTITY, "11000.0")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "1.0")
        await hass.async_block_till_done()

        assert engine.get_status_sub_state() == "charging"

        # ---- Window 1: power drops, advance past timeout → charged ----
        hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()

        freezer.tick(timedelta(minutes=IDLE_TIMEOUT_MIN + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert engine.get_status_sub_state() == "charged", (
            f"TC-005b: after window 1 close, expected 'charged', "
            f"got {engine.get_status_sub_state()!r}"
        )

        # ev_charging_charged fired once for window 1
        assert len(charged_events) == 1, (
            f"TC-005b: Expected 1 ev_charging_charged event after window 1, "
            f"got {len(charged_events)}"
        )
        assert charged_events[0]["window_index"] == 1

        # ---- Window 2: power resumes (BMS post-completion balancing) ----
        hass.states.async_set(MOCK_POWER_ENTITY, "3000.0")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "10.5")
        await hass.async_block_till_done()

        assert engine.get_status_sub_state() == "charging", (
            f"TC-005b: after power resumes for window 2, expected 'charging', "
            f"got {engine.get_status_sub_state()!r}"
        )
        assert engine.active_session.charging_started_at is not None, (
            "TC-005b: charging_started_at must remain set (from window 1, not overwritten)"
        )

        # ---- Window 2: power drops, advance past timeout → charged ----
        hass.states.async_set(MOCK_ENERGY_ENTITY, "11.0")
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()

        freezer.tick(timedelta(minutes=IDLE_TIMEOUT_MIN + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert engine.get_status_sub_state() == "charged", (
            f"TC-005b: after window 2 close, expected 'charged', "
            f"got {engine.get_status_sub_state()!r}"
        )

        # ev_charging_charged fired once more for window 2
        assert len(charged_events) == 2, (
            f"TC-005b: Expected 2 ev_charging_charged events total, got {len(charged_events)}"
        )
        assert charged_events[1]["window_index"] == 2

        # Window count on active session
        assert engine.active_session.charging_window_count == 2, (
            f"TC-005b: Expected charging_window_count == 2, "
            f"got {engine.active_session.charging_window_count}"
        )

        # ---- Unplug → session completes → idle ----
        # Also clear trx to null (go-e firmware auto-clears trx after session ends;
        # without this, the idle engine would show 'waiting_for_plug' because trx="2"
        # is still set from the session that just completed).
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_TRX_ENTITY, "null")
        await hass.async_block_till_done()

        assert engine.get_status_sub_state() == "idle", (
            f"TC-005b: after unplug, expected 'idle', got {engine.get_status_sub_state()!r}"
        )
