"""Tests for event-driven RFID wait model on PlugAnchoredSessionEngine (PR-24, US1).

Replaces test_session_engine_rfid_grace.py. Scenarios:

  1. test_rfid_wait_trx_resolves_starts_session
     plug-on with trx=null, then trx→2 after arbitrary delay → user=Mapped (FR-001, FR-002)

  2. test_rfid_wait_power_flow_triggers_open_access_session
     plug-on with trx=null, then power→>0 → user=Unknown reason=open_access_inferred (FR-003)

  3. test_rfid_wait_plug_off_cancels
     plug-on with trx=null, then plug-off → no SESSION_START (FR-004)

  4. test_rfid_wait_plug_invalid_cancels
     plug-on with trx=null, then plug→"unavailable" → no SESSION_START (FR-004)

  5. test_rfid_wait_no_timeout_long_delay
     plug-on with trx=null, tick 5 minutes → still waiting, no SESSION_START (FR-005)

  6. test_rfid_wait_trx_zero_immediate_start
     plug-on with trx="0" → SESSION_START immediately (FR-006, existing FR-008 carry-over)

  7. test_rfid_wait_blip_then_plug_fast_path
     trx="2" set with plug=off, then plug→on → SESSION_START immediately with mapped user (FR-006)

  8. test_rfid_wait_connected_at_records_plug_on_time
     plug-on at T0, trx→"2" at T0+90s → session.connected_at == T0 (FR-007)

All tests use the event-driven model. No CONF_RFID_GRACE_SECONDS is configured
because the option has been removed entirely (FR-014).
"""

from __future__ import annotations

from datetime import datetime, timedelta
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
    EVENT_SESSION_STARTED,
    SessionEngineState,
    UnknownReason,
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


async def _make_engine_entry(
    hass: HomeAssistant,
    extra_options: dict | None = None,
) -> MockConfigEntry:
    """Create and set up a config entry with PlugAnchoredSessionEngine active.

    No CONF_RFID_GRACE_SECONDS — the option has been removed in PR-24 (FR-014).
    Includes a mapped user (Petra, RFID card_index=1, trx="2") so tests can
    assert attribution.
    """
    options: dict = {
        "plug_entity": MOCK_PLUG_ENTITY,
        "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
        CONF_CHARGING_IDLE_TIMEOUT_MIN: 5,
        CONF_DISCONNECT_GRACE_MIN: 10,
    }
    if extra_options:
        options.update(extra_options)

    data = {**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"}

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data,
        options=options,
        title="Test go-e Charger (RFID wait)",
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
    # Add user
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "user"),
        context={"source": "user"},
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Petra", "type": "regular"},
    )
    await hass.async_block_till_done()

    # Find the user subentry ID
    entry = hass.config_entries.async_get_entry(entry_id)
    user_subs = [s for s in entry.subentries.values() if s.subentry_type == "user"]
    user_id = user_subs[-1].subentry_id

    # Add RFID mapping: card_index=1 → trx="2"
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
# Test 1: plug-on trx=null, trx resolves → session with mapped user (FR-001, FR-002)
# ---------------------------------------------------------------------------


async def test_rfid_wait_trx_resolves_starts_session(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Plug-on with trx=null; trx→"2" after arbitrary delay → user=Petra (FR-001, FR-002).

    Key assertion: no SESSION_START before trx resolves, one SESSION_START after.
    No timer is involved — the wait resolves purely on trx state change.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    # Add Petra user + RFID mapping so trx=2 resolves to "Petra"
    await _add_petra_user_and_rfid(hass, entry.entry_id)

    session_started_events: list[dict] = []

    def _capture(event) -> None:
        session_started_events.append(dict(event.data))

    hass.bus.async_listen(EVENT_SESSION_STARTED, _capture)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in — trx is null → engine enters RFID wait state
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        assert engine.state == SessionEngineState.TRACKING, "Engine must be TRACKING after plug-on"
        assert engine.active_session is None, "No session must exist while waiting for RFID"
        assert len(session_started_events) == 0, "SESSION_START must not fire before trx resolves"

        # Wait 30 seconds — event-driven model must NOT time out (FR-005)
        freezer.tick(timedelta(seconds=30))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Still no session (no timer, no timeout)
        assert engine.active_session is None, (
            "No session must exist after 30s (no timeout in event-driven model)"
        )
        assert len(session_started_events) == 0

        # Blip RFID tag → trx=2 → session resolves to Petra
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        await hass.async_block_till_done()

        assert engine.active_session is not None, (
            "Session must start when trx becomes non-null during RFID wait"
        )
        assert engine.active_session.user_name == "Petra", (
            f"Expected user=Petra, got {engine.active_session.user_name!r}"
        )
        assert len(session_started_events) == 1, (
            f"Exactly one SESSION_START must fire, got {len(session_started_events)}"
        )


# ---------------------------------------------------------------------------
# Test 2: power flow during RFID wait → open-access session (FR-003)
# ---------------------------------------------------------------------------


async def test_rfid_wait_power_flow_triggers_open_access_session(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Plug-on trx=null, power→>0 → session with user=Unknown, reason=open_access_inferred (FR-003).

    Covers the open-access charger path: energy flows without an RFID blip.
    Session must be attributed to Unknown, not left uncommitted.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    plug_on_time = dt_util.utcnow()

    session_started_events: list[dict] = []

    def _capture(event) -> None:
        session_started_events.append(dict(event.data))

    hass.bus.async_listen(EVENT_SESSION_STARTED, _capture)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in — trx is null → RFID wait starts
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        assert engine.active_session is None
        assert engine._rfid_wait is not None, (  # noqa: SLF001
            "Engine must have _rfid_wait state after plug-on with trx=null"
        )

        # Power begins flowing (open-access charger — no RFID required)
        hass.states.async_set(MOCK_POWER_ENTITY, "3680.0")
        await hass.async_block_till_done()

        # Session must have started with Unknown user (open-access inference)
        assert engine.active_session is not None, (
            "Session must start when power flows during RFID wait (FR-003)"
        )
        assert engine.active_session.user_name == "Unknown", (
            f"Open-access session must have user=Unknown, got {engine.active_session.user_name!r}"
        )
        assert len(session_started_events) == 1

        # connected_at must be the plug-on time, not the power-on time (FR-007)
        session_connected_at = engine.active_session.connected_at
        cat_dt = datetime.fromisoformat(session_connected_at)
        if cat_dt.tzinfo is not None:
            cat_ts = cat_dt.timestamp()
        else:
            cat_ts = cat_dt.replace(tzinfo=dt_util.UTC).timestamp()
        plug_ts = plug_on_time.timestamp()
        diff = abs(cat_ts - plug_ts)
        assert diff < 2.0, (
            f"connected_at ({session_connected_at}) must be the plug-on time "
            f"(diff={diff:.1f}s) — FR-007"
        )

        # Reason must indicate open-access inference
        assert engine.last_unknown_reason == "open_access_inferred", (
            f"Expected reason=open_access_inferred, got {engine.last_unknown_reason!r}"
        )

        # T019 (US4): first charging window must open with the power-on time (not after).
        # _async_start_session detects power > 0 and calls _open_window(now) immediately
        # (see session_engine_v2.py lines 1703-1705: "if power > 0: _open_window(now)").
        assert engine.window_tracker.is_open(), (
            "T019/US4: a charging window must be open because power > 0 at session start"
        )
        active_window = engine.window_tracker.active_window()
        assert active_window is not None
        # Window started_at must be close to when power-on was set (same dispatch cycle)
        assert active_window.start_at is not None, "T019/US4: active_window.start_at must be set"
        # Note: 'reason' field does not exist on Session in this project —
        # the diagnostic reason is tracked separately via engine.last_unknown_reason,
        # which is already asserted above. No 'session.reason' attribute to check.


# ---------------------------------------------------------------------------
# Test 3: plug-on then plug-off → no session (FR-004)
# ---------------------------------------------------------------------------


async def test_rfid_wait_plug_off_cancels(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Plug-on trx=null, then plug→off → wait cancelled, no SESSION_START (FR-004).

    Validates that unplugging before a blip or power flow produces no session.
    Advances time well past any would-be timer to confirm no late fire.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    session_started_events: list[dict] = []

    def _capture(event) -> None:
        session_started_events.append(dict(event.data))

    hass.bus.async_listen(EVENT_SESSION_STARTED, _capture)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in → RFID wait starts
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        assert engine.active_session is None
        assert engine.state == SessionEngineState.TRACKING

        # Advance 10 seconds, still no blip
        freezer.tick(timedelta(seconds=10))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Unplug (cable_lock=Unlocked validates it as a real unplug)
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

        # Advance further — confirm no late fire occurs
        freezer.tick(timedelta(minutes=10))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert len(session_started_events) == 0, (
            f"No SESSION_START must fire after plug-off cancels wait; "
            f"got {len(session_started_events)} events"
        )
        assert engine.active_session is None
        assert len(session_store.sessions) == 0, (
            f"No sessions must be persisted; got {len(session_store.sessions)}"
        )
        # SF9: verify engine returned cleanly to IDLE with no stale wait state
        assert engine.state == SessionEngineState.IDLE, (
            f"SF9: engine must be IDLE after plug-off cancel, got {engine.state!r}"
        )
        assert engine._rfid_wait is None, (  # noqa: SLF001
            "SF9: _rfid_wait must be None after plug-off cancels the wait"
        )


# ---------------------------------------------------------------------------
# Test 4: plug enters invalid state during wait → wait cancelled (FR-004)
# ---------------------------------------------------------------------------


async def test_rfid_wait_plug_invalid_cancels(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Plug-on trx=null, then plug→"unavailable" → wait cancelled, no SESSION_START (FR-004).

    Validates that an invalid plug state (charger temporarily offline)
    cancels the RFID wait without starting a session.
    Advances time past any would-be window to confirm no late fire.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    session_started_events: list[dict] = []

    def _capture(event) -> None:
        session_started_events.append(dict(event.data))

    hass.bus.async_listen(EVENT_SESSION_STARTED, _capture)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in → RFID wait starts
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        assert engine.state == SessionEngineState.TRACKING
        assert engine.active_session is None

        # Plug entity goes unavailable (charger offline / data loss)
        hass.states.async_set(MOCK_PLUG_ENTITY, "unavailable")
        await hass.async_block_till_done()

        # Advance time — confirm no late fire
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert len(session_started_events) == 0, (
            f"No SESSION_START must fire when plug goes unavailable during wait; "
            f"got {len(session_started_events)} events"
        )
        assert engine.active_session is None
        # SF9: verify engine returned cleanly to IDLE with no stale wait state
        assert engine.state == SessionEngineState.IDLE, (
            f"SF9: engine must be IDLE after plug-invalid cancel, got {engine.state!r}"
        )
        assert engine._rfid_wait is None, (  # noqa: SLF001
            "SF9: _rfid_wait must be None after plug-invalid cancels the wait"
        )


# ---------------------------------------------------------------------------
# Test 5: no timeout — wait continues indefinitely (FR-005)
# ---------------------------------------------------------------------------


async def test_rfid_wait_no_timeout_long_delay(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Plug-on trx=null, advance 5 minutes → still waiting, no SESSION_START (FR-005).

    The event-driven model has NO fallback timer. After 5 minutes without
    a trx change, power flow, or plug-off, the engine stays in RFID wait.
    SESSION_START must NOT fire spontaneously.

    After verifying the continued wait, a trx change resolves the session.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    await _add_petra_user_and_rfid(hass, entry.entry_id)

    session_started_events: list[dict] = []

    def _capture(event) -> None:
        session_started_events.append(dict(event.data))

    hass.bus.async_listen(EVENT_SESSION_STARTED, _capture)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in → RFID wait starts
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        assert engine.active_session is None
        assert engine.state == SessionEngineState.TRACKING

        # Advance 5 minutes — no timer should fire (FR-005)
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert len(session_started_events) == 0, (
            f"No SESSION_START must fire after 5 min (no timer in event-driven model); "
            f"got {len(session_started_events)} events"
        )
        assert engine.active_session is None, (
            "Engine must still be in RFID wait after 5 min (FR-005)"
        )

        # Now resolve via trx blip — must still work
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        await hass.async_block_till_done()

        assert engine.active_session is not None
        assert engine.active_session.user_name == "Petra"
        assert len(session_started_events) == 1


# ---------------------------------------------------------------------------
# Test 6: trx="0" at plug-on → immediate session start (FR-006, carry-over FR-008)
# ---------------------------------------------------------------------------


async def test_rfid_wait_trx_zero_immediate_start(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Plug-on with trx="0" (open-access) → SESSION_START immediately, no wait (FR-006).

    trx="0" is NOT in _INVALID_STATES — it is the open-access sentinel at go-e.
    The engine must start a session immediately without entering RFID wait state.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    session_started_events: list[dict] = []

    def _capture(event) -> None:
        session_started_events.append(dict(event.data))

    hass.bus.async_listen(EVENT_SESSION_STARTED, _capture)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Set trx="0" BEFORE plug-on (already authorized, open-access)
        hass.states.async_set(MOCK_TRX_ENTITY, "0")
        await hass.async_block_till_done()

        # Plug in with trx="0" already set → immediate session start
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        # Session must have started immediately (no wait entered)
        assert engine.active_session is not None, (
            "Session must start immediately when trx='0' at plug-on (FR-008 carry-over)"
        )
        assert len(session_started_events) == 1, (
            f"SESSION_START must fire immediately for trx='0', "
            f"got {len(session_started_events)} events"
        )
        # Engine must NOT be in RFID wait state
        assert engine._rfid_wait is None, (  # noqa: SLF001
            "_rfid_wait must be None when session started immediately"
        )


# ---------------------------------------------------------------------------
# Test 7: blip before plug → plug-on → SESSION_START immediately (FR-006, FR-007)
# ---------------------------------------------------------------------------


async def test_rfid_wait_blip_then_plug_fast_path(
    hass: HomeAssistant,
    freezer,
) -> None:
    """trx="2" set with plug=off, then plug→on → SESSION_START immediately (FR-006).

    The fast path: user blips RFID first, then inserts cable. trx is already
    non-null at plug-on time, so the engine skips the RFID wait and starts
    the session immediately with the blipped user's attribution.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    await _add_petra_user_and_rfid(hass, entry.entry_id)

    session_started_events: list[dict] = []

    def _capture(event) -> None:
        session_started_events.append(dict(event.data))

    hass.bus.async_listen(EVENT_SESSION_STARTED, _capture)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Blip RFID with plug still off
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        await hass.async_block_till_done()

        # Engine is still IDLE (plug is off)
        assert engine.state == SessionEngineState.IDLE
        assert engine.active_session is None
        assert len(session_started_events) == 0

        # Record plug-on time
        plug_on_time = dt_util.utcnow()

        # Insert cable — trx is already non-null → immediate session start
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        assert engine.active_session is not None, (
            "Session must start immediately when plug-on with trx already set (FR-006)"
        )
        assert engine.active_session.user_name == "Petra", (
            f"Expected user=Petra (blipped before plug), got {engine.active_session.user_name!r}"
        )
        assert len(session_started_events) == 1

        # No RFID wait state should be active
        assert engine._rfid_wait is None  # noqa: SLF001

        # connected_at should be the plug-on time (FR-007)
        session_connected_at = engine.active_session.connected_at
        cat_dt = datetime.fromisoformat(session_connected_at)
        if cat_dt.tzinfo is not None:
            cat_ts = cat_dt.timestamp()
        else:
            cat_ts = cat_dt.replace(tzinfo=dt_util.UTC).timestamp()
        plug_ts = plug_on_time.timestamp()
        diff = abs(cat_ts - plug_ts)
        assert diff < 2.0, f"connected_at must be the plug-on time (diff={diff:.1f}s) — FR-007"


# ---------------------------------------------------------------------------
# Test 8: connected_at is plug-on time, not trx-resolution time (FR-007)
# ---------------------------------------------------------------------------


async def test_rfid_wait_connected_at_records_plug_on_time(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Plug-on at T0, trx→"2" at T0+90s → session.connected_at == T0 (FR-007).

    Verifies that even with a 90-second wait between plug-on and RFID blip,
    the session's connected_at reflects the actual plug-on time, not the
    moment the trx resolved.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    await _add_petra_user_and_rfid(hass, entry.entry_id)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Record plug-on time (T0)
        plug_on_time = dt_util.utcnow()

        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        assert engine.active_session is None
        assert engine.state == SessionEngineState.TRACKING

        # Advance 90 seconds — user takes a while before blipping
        freezer.tick(timedelta(seconds=90))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Still no session (event-driven — no timer fires)
        assert engine.active_session is None

        # Blip RFID tag at T0+90s
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        await hass.async_block_till_done()

        assert engine.active_session is not None
        assert engine.active_session.user_name == "Petra"

        # connected_at must be T0 (plug-on), not T0+90s (trx resolution) — FR-007
        session_connected_at = engine.active_session.connected_at
        cat_dt = datetime.fromisoformat(session_connected_at)
        if cat_dt.tzinfo is not None:
            cat_ts = cat_dt.timestamp()
        else:
            cat_ts = cat_dt.replace(tzinfo=dt_util.UTC).timestamp()
        plug_ts = plug_on_time.timestamp()
        trx_resolve_ts = plug_ts + 90.0

        diff_from_plug = abs(cat_ts - plug_ts)
        diff_from_resolve = abs(cat_ts - trx_resolve_ts)

        assert diff_from_plug < 2.0, (
            f"connected_at must be close to plug-on time T0 (diff={diff_from_plug:.1f}s), "
            f"not the trx-resolution time (diff_from_resolve={diff_from_resolve:.1f}s) — FR-007"
        )


# ---------------------------------------------------------------------------
# SF9: cancel tests — engine.state + _rfid_wait cleanup assertions
# ---------------------------------------------------------------------------
# (These are additive assertions in the bodies of Test 3 and Test 4 above.
# The new standalone tests below are B3, SF5, SF6.)


# ---------------------------------------------------------------------------
# B3: trx and power both fire during wait → exactly one session starts
# ---------------------------------------------------------------------------


async def test_rfid_wait_trx_and_power_race_no_op(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Trx and power both transition during waiting_for_rfid in one tick → one session.

    Validates the race-condition guard documented in engine-state-machine.md
    §'Race-condition analysis': the first handler to fire starts the session;
    the second sees active_session is not None and is a no-op.  Exactly one
    SESSION_START event must be fired and no duplicate session must exist.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    # Add Petra so trx="2" resolves to a mapped user
    await _add_petra_user_and_rfid(hass, entry.entry_id)

    session_started_events: list[dict] = []

    def _capture(event) -> None:
        session_started_events.append(dict(event.data))

    hass.bus.async_listen(EVENT_SESSION_STARTED, _capture)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in — trx is null → engine enters RFID wait state
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        assert engine.active_session is None, "Pre-condition: no session before race"
        assert engine._rfid_wait is not None, (  # noqa: SLF001
            "Pre-condition: RFID wait must be active after plug-on with trx=null"
        )

        # Both events in the same HA tick — no freezer.tick between them
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        hass.states.async_set(MOCK_POWER_ENTITY, "3680")
        # Single dispatch round to let both listeners run
        await hass.async_block_till_done()

        # Exactly one session must exist
        assert engine.active_session is not None, (
            "B3: at least one handler must have started a session"
        )
        # Exactly one SESSION_START event (the second handler is a no-op)
        assert len(session_started_events) == 1, (
            f"B3: exactly one SESSION_START must fire (got {len(session_started_events)})"
        )
        # User must be either Petra (trx won) or Unknown (power won) — both valid
        assert engine.active_session.user_name in ("Petra", "Unknown"), (
            f"B3: user must be Petra or Unknown, got {engine.active_session.user_name!r}"
        )
        # Wait state must be cleared
        assert engine._rfid_wait is None, (  # noqa: SLF001
            "B3: _rfid_wait must be cleared after session started"
        )


# ---------------------------------------------------------------------------
# SF5: wait → trx unmapped → Unknown session with rfid_unmapped reason
# ---------------------------------------------------------------------------


async def test_rfid_wait_trx_unmapped_starts_unknown_session(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Plug-on trx=null, then unmapped trx arrives → session with user=Unknown.

    Tests the wait-path unmapped branch.  The fast-path (trx set before plug-on)
    is already covered by test_unknown_rfid_notification.py TC-012.  This test
    covers the case where the unmapped blip arrives DURING the wait.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    # Deliberately add NO user/RFID mappings — trx="99" will be unmapped.

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in — trx is null → RFID wait starts
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        assert engine.active_session is None
        assert engine._rfid_wait is not None, (  # noqa: SLF001
            "SF5: RFID wait must be active after plug-on with trx=null"
        )

        # Set unmapped trx value — no mapping exists for index 98 (trx="99")
        hass.states.async_set(MOCK_TRX_ENTITY, "99")
        await hass.async_block_till_done()

        # Session must have started with Unknown attribution
        assert engine.active_session is not None, (
            "SF5: session must start when unmapped trx arrives during RFID wait"
        )
        assert engine.active_session.user_name == "Unknown", (
            f"SF5: unmapped RFID must produce user=Unknown, got {engine.active_session.user_name!r}"
        )
        # Reason must indicate an unmapped RFID
        assert engine.last_unknown_reason == UnknownReason.RFID_UNMAPPED, (
            f"SF5: expected reason={UnknownReason.RFID_UNMAPPED!r}, "
            f"got {engine.last_unknown_reason!r}"
        )
        # Wait state must be cleared
        assert engine._rfid_wait is None, (  # noqa: SLF001
            "SF5: _rfid_wait must be cleared after session starts"
        )


# ---------------------------------------------------------------------------
# SF6: wait → trx="0" arriving during wait → Unknown session (open-access)
# ---------------------------------------------------------------------------


async def test_rfid_wait_trx_changes_to_zero_during_wait(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Plug-on trx=null, then trx→'0' during wait → Unknown session (trx_was_zero).

    Complements test_rfid_wait_trx_zero_immediate_start which covers trx='0'
    SET BEFORE plug-on.  This test covers trx changing FROM null TO '0' DURING
    an active RFID wait.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in — trx is null → RFID wait starts
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        assert engine.active_session is None
        assert engine._rfid_wait is not None, (  # noqa: SLF001
            "SF6: RFID wait must be active after plug-on with trx=null"
        )

        # trx changes to "0" (open-access sentinel) during the wait
        hass.states.async_set(MOCK_TRX_ENTITY, "0")
        await hass.async_block_till_done()

        # Session must have started (trx="0" is not in _INVALID_STATES)
        assert engine.active_session is not None, (
            "SF6: session must start when trx='0' arrives during RFID wait"
        )
        assert engine.active_session.user_name == "Unknown", (
            f"SF6: trx='0' session must have user=Unknown, got {engine.active_session.user_name!r}"
        )
        # Reason must indicate trx was zero
        assert engine.last_unknown_reason == UnknownReason.TRX_ZERO, (
            f"SF6: expected reason={UnknownReason.TRX_ZERO!r}, got {engine.last_unknown_reason!r}"
        )
        # Wait state must be cleared
        assert engine._rfid_wait is None, (  # noqa: SLF001
            "SF6: _rfid_wait must be cleared after session starts"
        )


# ---------------------------------------------------------------------------
# PR-28 (024-debug-logger-overhaul) US2: RFID tag redaction in debug log
# ---------------------------------------------------------------------------


async def test_rfid_tag_redacted_in_all_debug_log_lines(hass: HomeAssistant, tmp_path) -> None:
    """FR-004: an RFID blip with a long tag value never puts the full value in
    the debug log — RFID_READ / TRX_STATE / TRX_MIDSESSION carry ***{last2}."""
    from custom_components.ev_charging_manager.const import CONF_DEBUG_LOGGING

    hass.config.config_dir = str(tmp_path)
    entry = await _make_engine_entry(hass, extra_options={CONF_DEBUG_LOGGING: True})
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug on with trx=null → RFID wait; then blip with a long tag value
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        hass.states.async_set(MOCK_TRX_ENTITY, "abc123f4")
        await hass.async_block_till_done()

        assert engine.active_session is not None

        # Mid-session trx change (numeric → TRX_MIDSESSION path)
        hass.states.async_set(MOCK_TRX_ENTITY, "7")
        await hass.async_block_till_done()

        debug_logger = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
        await debug_logger.async_disable()  # flush everything emitted so far

    content = open(debug_logger.file_path, encoding="utf-8").read()
    lines = content.splitlines()

    # The full tag value must never appear in any line of the log file
    assert "abc123f4" not in content, f"Full tag leaked into the debug log:\n{content}"

    rfid_read = [ln for ln in lines if "RFID_READ" in ln]
    assert rfid_read, "Expected an RFID_READ line"
    assert "tag=***f4" in rfid_read[0]

    trx_state = [ln for ln in lines if "TRX_STATE" in ln and "***f4" in ln]
    assert trx_state, f"Expected a TRX_STATE line with the masked tag, got:\n{content}"

    midsession = [ln for ln in lines if "TRX_MIDSESSION" in ln]
    assert midsession, "Expected a TRX_MIDSESSION line for the mid-session change"
    assert "now trx=***" in midsession[0]
    assert "now trx=7" not in midsession[0]
