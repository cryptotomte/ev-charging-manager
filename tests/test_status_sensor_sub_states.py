"""TC-024: Status sensor sub-state visibility tests (PR-24, US2, US3, FR-008/FR-009/FR-010).

Tests for the six visible sub-states of the Status sensor:

  1. test_substate_waiting_for_rfid_when_plug_on_trx_null
     engine=TRACKING, no session, plug=on, trx=null → "waiting_for_rfid" (FR-008)

  2. test_substate_waiting_for_plug_when_idle_trx_set
     engine=IDLE, plug=off, trx="2" (non-null, non-zero) → "waiting_for_plug" (FR-009)

  3. test_substate_waiting_for_plug_returns_to_idle_on_trx_clear
     from previous state, trx→null → "idle" (FR-009 edge case — go-e auto-clear)

  4. test_substate_initializing_renamed_from_waiting
     active session, no windows open, zero closed windows → "initializing" (FR-010)

  5. test_substate_full_lifecycle_via_plug_first
     plug-on (no trx) → trx → power-on → idle-timeout
     transitions: idle → waiting_for_rfid → initializing → charging → charged (FR-008, FR-010)

  6. test_substate_full_lifecycle_via_blip_first
     trx (no plug) → plug-on → power-on → idle-timeout
     transitions: idle → waiting_for_plug → initializing → charging → charged (FR-009, FR-010)

All tests use the event-driven wait model (PR-24). No CONF_RFID_GRACE_SECONDS.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ev_charging_manager.const import (
    CONF_CHARGING_IDLE_TIMEOUT_MIN,
    CONF_DISCONNECT_GRACE_MIN,
    DOMAIN,
    SessionEngineState,
    SessionSubState,
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

# Short idle timeout so timer-based lifecycle tests complete quickly
IDLE_TIMEOUT_MIN = 3


async def _make_engine_entry(
    hass: HomeAssistant,
    extra_options: dict | None = None,
) -> MockConfigEntry:
    """Create and set up a config entry with PlugAnchoredSessionEngine active.

    No CONF_RFID_GRACE_SECONDS — option removed in PR-24 (FR-014).
    Includes a mapped user (Petra, card_index=1, trx="2") so lifecycle tests
    can assert attribution via trx→"2" → resolved user.
    """
    options: dict = {
        "plug_entity": MOCK_PLUG_ENTITY,
        "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
        CONF_CHARGING_IDLE_TIMEOUT_MIN: IDLE_TIMEOUT_MIN,
        CONF_DISCONNECT_GRACE_MIN: 10,
    }
    if extra_options:
        options.update(extra_options)

    data = {**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"}

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data,
        options=options,
        title="Test go-e Charger (sub-state tests)",
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


async def _add_petra_user_and_rfid(hass: HomeAssistant, entry_id: str) -> None:
    """Add Petra user (trx=2 → card_index=1) to the config entry via subentry flows."""
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "user"),
        context={"source": "user"},
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Petra", "type": "regular"},
    )
    await hass.async_block_till_done()

    entry = hass.config_entries.async_get_entry(entry_id)
    user_subs = [s for s in entry.subentries.values() if s.subentry_type == "user"]
    user_id = user_subs[-1].subentry_id

    result = await hass.config_entries.subentries.async_init(
        (entry_id, "rfid_mapping"),
        context={"source": "user"},
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"card_index": "1", "user_id": user_id},
    )
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Test 1: plug=on, trx=null, no active session → waiting_for_rfid (FR-008)
# ---------------------------------------------------------------------------


async def test_substate_waiting_for_rfid_when_plug_on_trx_null(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Engine TRACKING + no session + plug=on + trx=null → sub-state == waiting_for_rfid.

    This is the US2 scenario: cable plugged in, user has not blipped yet.
    The status sensor must display the waiting_for_rfid sub-state (FR-008)
    rather than the confusing 'idle' it showed before PR-24.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in with trx=null → engine enters RFID wait
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        # Engine must be TRACKING with no active session (pre-session RFID wait)
        assert engine.state == SessionEngineState.TRACKING
        assert engine.active_session is None

        sub = engine.get_status_sub_state()
        assert sub == SessionSubState.WAITING_FOR_RFID, (
            f"TC-024: expected 'waiting_for_rfid' while plug=on and trx=null, got {sub!r}"
        )
        # Also verify the bare string equality holds (StrEnum guarantee)
        assert sub == "waiting_for_rfid", (
            "StrEnum: get_status_sub_state() == 'waiting_for_rfid' must be True"
        )
        # SF8: Status sensor HA state must reflect the sub-state (A1 wiring)
        registry = er.async_get(hass)
        status_entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_status"
        )
        assert status_entity_id is not None, "SF8: Status sensor entity must be registered"
        ha_state = hass.states.get(status_entity_id)
        assert ha_state is not None, f"SF8: HA state for {status_entity_id} must exist"
        assert ha_state.state == "waiting_for_rfid", (
            f"SF8: Status sensor HA state must be 'waiting_for_rfid', got {ha_state.state!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: engine=IDLE, plug=off, trx="2" → waiting_for_plug (FR-009)
# ---------------------------------------------------------------------------


async def test_substate_waiting_for_plug_when_idle_trx_set(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Engine IDLE + plug=off + trx='2' (non-null, non-zero) → sub-state == waiting_for_plug.

    This is the US3 scenario: user blips RFID before inserting the cable.
    The engine stays IDLE (no session, plug is off), but the sub-state must
    display 'waiting_for_plug' to confirm the blip was received (FR-009).
    Also verifies the Status sensor HA state is updated (A1 wiring fix).
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Blip RFID with plug=off — engine remains IDLE
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        await hass.async_block_till_done()

        # Engine must remain IDLE (no cable inserted)
        assert engine.state == SessionEngineState.IDLE, (
            f"TC-024: engine must be IDLE when trx set without plug, got {engine.state!r}"
        )
        assert engine.active_session is None

        sub = engine.get_status_sub_state()
        assert sub == SessionSubState.WAITING_FOR_PLUG, (
            f"TC-024: expected 'waiting_for_plug' while idle with trx set, got {sub!r}"
        )
        assert sub == "waiting_for_plug"

        # A1 wiring check: HA state must reflect waiting_for_plug without an extra event.
        # _dispatch_update() must have been called during the trx state change handler.
        registry = er.async_get(hass)
        status_entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_status"
        )
        assert status_entity_id is not None, "Status sensor entity must be registered"
        ha_state = hass.states.get(status_entity_id)
        assert ha_state is not None, f"HA state for {status_entity_id} must exist"
        assert ha_state.state == "waiting_for_plug", (
            f"TC-024 A1: Status sensor HA state must be 'waiting_for_plug' after trx set, "
            f"got {ha_state.state!r}"
        )


# ---------------------------------------------------------------------------
# Test 3: from waiting_for_plug, trx→null → back to idle (FR-009 edge case)
# ---------------------------------------------------------------------------


async def test_substate_waiting_for_plug_returns_to_idle_on_trx_clear(
    hass: HomeAssistant,
    freezer,
) -> None:
    """From waiting_for_plug state, trx→null (go-e auto-clear) → sub-state returns to idle.

    Covers the edge case from US3 acceptance scenario 3: if the charger's RFID
    transaction clears (typical go-e behavior ~120 s after an unanswered blip),
    the sub-state reverts to idle without creating a session (FR-009).
    Also verifies the Status sensor HA state is updated (A1 wiring fix).
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Blip RFID with plug=off → waiting_for_plug
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        await hass.async_block_till_done()

        assert engine.get_status_sub_state() == "waiting_for_plug", (
            "TC-024: pre-condition must be waiting_for_plug"
        )

        # go-e auto-clears trx to null (charger RFID auth timeout)
        hass.states.async_set(MOCK_TRX_ENTITY, "null")
        await hass.async_block_till_done()

        sub = engine.get_status_sub_state()
        assert sub == SessionSubState.IDLE, (
            f"TC-024: expected 'idle' after trx clears from waiting_for_plug, got {sub!r}"
        )
        assert engine.active_session is None, "No session must be created on trx clear"

        # A1 wiring check: HA state must reflect 'idle' after trx clears.
        registry = er.async_get(hass)
        status_entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_status"
        )
        assert status_entity_id is not None, "Status sensor entity must be registered"
        ha_state = hass.states.get(status_entity_id)
        assert ha_state is not None, f"HA state for {status_entity_id} must exist"
        assert ha_state.state == "idle", (
            f"TC-024 A1: Status sensor HA state must be 'idle' after trx clears, "
            f"got {ha_state.state!r}"
        )


# ---------------------------------------------------------------------------
# Test 4: active session, no windows open, zero closed windows → initializing (FR-010)
# ---------------------------------------------------------------------------


async def test_substate_initializing_renamed_from_waiting(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Active session + no open window + zero closed windows → sub-state == 'initializing'.

    Verifies the FR-010 rename: the short transient state between SESSION_START and
    CHARGING_WINDOW_OPEN is now 'initializing', not 'waiting'. The bare string
    'waiting' must NOT appear in production return values.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Blip first, then plug in (fast path — immediate session start)
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        # Session must have started (trx non-null at plug-on → fast path)
        assert engine.active_session is not None, (
            "TC-024: session must start immediately with trx set at plug-on"
        )
        # Window tracker has no open window yet (power=0) and no closed windows
        assert not engine.window_tracker.is_open(), (
            "TC-024: window must not be open before power > 0"
        )
        assert engine.window_tracker.window_count() == 0

        sub = engine.get_status_sub_state()
        assert sub == SessionSubState.INITIALIZING, (
            f"TC-024: expected 'initializing' before first window opens, got {sub!r}"
        )
        assert sub == "initializing", "TC-024: StrEnum equality must hold for 'initializing'"
        # Explicitly confirm the old 'waiting' string is NOT returned
        assert sub != "waiting", (
            "TC-024: FR-010 rename — 'waiting' must never be returned, use 'initializing'"
        )
        # SF8: Status sensor HA state must reflect 'initializing' (A1 wiring)
        registry = er.async_get(hass)
        status_entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_status"
        )
        assert status_entity_id is not None, "SF8: Status sensor entity must be registered"
        ha_state = hass.states.get(status_entity_id)
        assert ha_state is not None, f"SF8: HA state for {status_entity_id} must exist"
        assert ha_state.state == "initializing", (
            f"SF8: Status sensor HA state must be 'initializing', got {ha_state.state!r}"
        )


# ---------------------------------------------------------------------------
# Test 5: full lifecycle via plug-first path (FR-008, FR-010)
# ---------------------------------------------------------------------------


async def test_substate_full_lifecycle_via_plug_first(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Full lifecycle: plug-on (no trx) → blip → power-on → idle-timeout.

    Asserts ordered transitions:
      idle → waiting_for_rfid → initializing → charging → charged

    Covers FR-008 (waiting_for_rfid) and FR-010 (initializing rename).
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    transitions: list[str] = []

    # SF8: helper to read the Status sensor HA state
    def _sensor_state() -> str | None:
        registry = er.async_get(hass)
        eid = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_status")
        if eid is None:
            return None
        state = hass.states.get(eid)
        return state.state if state else None

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # ---- idle ----
        sub = engine.get_status_sub_state()
        transitions.append(sub)
        assert sub == "idle", f"Expected idle at start, got {sub!r}"

        # ---- plug-on with trx=null → waiting_for_rfid ----
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        sub = engine.get_status_sub_state()
        transitions.append(sub)
        assert sub == "waiting_for_rfid", (
            f"TC-024: expected 'waiting_for_rfid' after plug-on with trx=null, got {sub!r}"
        )
        # SF8: verify sensor HA state matches
        assert _sensor_state() == "waiting_for_rfid", (
            f"SF8 (plug-first): sensor HA state must be 'waiting_for_rfid', got {_sensor_state()!r}"
        )

        # ---- RFID blip → session starts → initializing ----
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        await hass.async_block_till_done()

        assert engine.active_session is not None, "Session must start after trx resolves"
        sub = engine.get_status_sub_state()
        transitions.append(sub)
        assert sub == "initializing", (
            f"TC-024: expected 'initializing' after session start (pre-power), got {sub!r}"
        )
        # SF8: verify sensor HA state matches
        assert _sensor_state() == "initializing", (
            f"SF8 (plug-first): sensor HA state must be 'initializing', got {_sensor_state()!r}"
        )

        # ---- power-on → charging ----
        hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "1.0")
        await hass.async_block_till_done()

        sub = engine.get_status_sub_state()
        transitions.append(sub)
        assert sub == "charging", f"TC-024: expected 'charging' after power > 0, got {sub!r}"
        # SF8: verify sensor HA state matches
        assert _sensor_state() == "charging", (
            f"SF8 (plug-first): sensor HA state must be 'charging', got {_sensor_state()!r}"
        )

        # ---- idle timeout → charged ----
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()

        freezer.tick(timedelta(minutes=IDLE_TIMEOUT_MIN + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        sub = engine.get_status_sub_state()
        transitions.append(sub)
        assert sub == "charged", f"TC-024: expected 'charged' after idle timeout, got {sub!r}"
        # SF8: verify sensor HA state matches
        assert _sensor_state() == "charged", (
            f"SF8 (plug-first): sensor HA state must be 'charged', got {_sensor_state()!r}"
        )

    # Assert the full ordered sequence
    assert transitions == ["idle", "waiting_for_rfid", "initializing", "charging", "charged"], (
        f"TC-024 (plug-first): unexpected lifecycle sequence: {transitions}"
    )


# ---------------------------------------------------------------------------
# Test 6: full lifecycle via blip-first path (FR-009, FR-010)
# ---------------------------------------------------------------------------


async def test_substate_full_lifecycle_via_blip_first(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Full lifecycle: trx-set (no plug) → plug-on → power-on → idle-timeout.

    Asserts ordered transitions:
      idle → waiting_for_plug → initializing → charging → charged

    Covers FR-009 (waiting_for_plug) and FR-010 (initializing rename).
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    transitions: list[str] = []

    # SF8: helper to read the Status sensor HA state
    def _sensor_state() -> str | None:
        registry = er.async_get(hass)
        eid = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_status")
        if eid is None:
            return None
        state = hass.states.get(eid)
        return state.state if state else None

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # ---- idle ----
        sub = engine.get_status_sub_state()
        transitions.append(sub)
        assert sub == "idle", f"Expected idle at start, got {sub!r}"

        # ---- RFID blip with plug=off → waiting_for_plug ----
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        await hass.async_block_till_done()

        assert engine.state == SessionEngineState.IDLE, (
            "Engine must remain IDLE after blip-only (no cable)"
        )
        sub = engine.get_status_sub_state()
        transitions.append(sub)
        assert sub == "waiting_for_plug", (
            f"TC-024: expected 'waiting_for_plug' after blip with plug=off, got {sub!r}"
        )
        # SF8: verify sensor HA state matches
        assert _sensor_state() == "waiting_for_plug", (
            f"SF8 (blip-first): sensor HA state must be 'waiting_for_plug', got {_sensor_state()!r}"
        )

        # ---- plug-on with trx already set → immediate session start → initializing ----
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        assert engine.active_session is not None, (
            "Session must start immediately (fast path: trx non-null at plug-on)"
        )
        sub = engine.get_status_sub_state()
        transitions.append(sub)
        assert sub == "initializing", (
            f"TC-024: expected 'initializing' after fast-path session start, got {sub!r}"
        )
        # SF8: verify sensor HA state matches
        assert _sensor_state() == "initializing", (
            f"SF8 (blip-first): sensor HA state must be 'initializing', got {_sensor_state()!r}"
        )

        # ---- power-on → charging ----
        hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "1.0")
        await hass.async_block_till_done()

        sub = engine.get_status_sub_state()
        transitions.append(sub)
        assert sub == "charging", f"TC-024: expected 'charging' after power > 0, got {sub!r}"
        # SF8: verify sensor HA state matches
        assert _sensor_state() == "charging", (
            f"SF8 (blip-first): sensor HA state must be 'charging', got {_sensor_state()!r}"
        )

        # ---- idle timeout → charged ----
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()

        freezer.tick(timedelta(minutes=IDLE_TIMEOUT_MIN + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        sub = engine.get_status_sub_state()
        transitions.append(sub)
        assert sub == "charged", f"TC-024: expected 'charged' after idle timeout, got {sub!r}"
        # SF8: verify sensor HA state matches
        assert _sensor_state() == "charged", (
            f"SF8 (blip-first): sensor HA state must be 'charged', got {_sensor_state()!r}"
        )

    # Assert the full ordered sequence
    assert transitions == ["idle", "waiting_for_plug", "initializing", "charging", "charged"], (
        f"TC-024 (blip-first): unexpected lifecycle sequence: {transitions}"
    )
