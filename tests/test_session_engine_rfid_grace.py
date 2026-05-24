"""Tests for RFID grace timer on PlugAnchoredSessionEngine (PR-23, US1).

Scenarios:
  (a) plug-on with trx=null, then trx becomes mapped-RFID within grace → user=Mapped
  (b) plug-on with trx=null, grace expires without RFID → user=Unknown
  (c) plug-on then plug-off before grace expires → no SESSION_START
  (d) plug-on then plug enters invalid state during grace → no SESSION_START
  (e) plug-on with trx="0" (open-access) → immediate session (no defer)
  (f) connected_at = plug-on timestamp, not timer-fire timestamp (FR-006)

FR-001..FR-008 and IC-1.
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
    CONF_RFID_GRACE_SECONDS,
    DOMAIN,
    EVENT_SESSION_STARTED,
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

# Grace timer value used in most tests (seconds)
GRACE_SECONDS = 5


async def _make_engine_entry(
    hass: HomeAssistant,
    rfid_grace_seconds: int = GRACE_SECONDS,
    extra_options: dict | None = None,
) -> MockConfigEntry:
    """Create and set up a config entry with PlugAnchoredSessionEngine active.

    Configures the entry with the given rfid_grace_seconds option and a mapped
    user (Petra, RFID card_index=1, trx="2") so tests can assert attribution.
    """
    options: dict = {
        "plug_entity": MOCK_PLUG_ENTITY,
        "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
        CONF_CHARGING_IDLE_TIMEOUT_MIN: 5,
        CONF_DISCONNECT_GRACE_MIN: 10,
        CONF_RFID_GRACE_SECONDS: rfid_grace_seconds,
    }
    if extra_options:
        options.update(extra_options)

    # Build config with a known RFID mapping: trx="2" → Petra (card_index=1)
    data = {**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"}

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data,
        options=options,
        title="Test go-e Charger (RFID grace)",
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


# ---------------------------------------------------------------------------
# Scenario (a): plug-on with trx=null, trx resolves within grace → user=Mapped
# ---------------------------------------------------------------------------


async def _add_petra_user_and_rfid(hass: HomeAssistant, entry_id: str) -> None:
    """Add Petra user (trx=2 → card_index=1) to the config entry via subentry flows."""
    # Add user
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "user"),
        context={"source": "user"},
    )
    result = await hass.config_entries.subentries.async_configure(
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


async def test_rfid_grace_trx_resolves_within_grace(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Scenario (a): plug-on trx=null, RFID blip arrives within grace window.

    Expected: exactly one SESSION_START with user attribution from the blipped tag.
    connected_at is the plug-on time, not the trx-resolution time (FR-006).
    """
    entry = await _make_engine_entry(hass, rfid_grace_seconds=GRACE_SECONDS)
    engine = _get_engine(hass, entry)

    # Add Petra user + RFID mapping so trx=2 resolves to "Petra"
    await _add_petra_user_and_rfid(hass, entry.entry_id)

    session_started_events: list[dict] = []

    def _capture_session_started(event) -> None:
        session_started_events.append(dict(event.data))

    hass.bus.async_listen(EVENT_SESSION_STARTED, _capture_session_started)

    plug_on_time = dt_util.utcnow()

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Step 1: plug in cable, trx still null → grace timer should start
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        # Engine must be TRACKING (state machine entered), but session should be
        # deferred — no SESSION_START event yet.
        assert engine.state == SessionEngineState.TRACKING, (
            "Engine should be TRACKING after plug-on"
        )
        # No session started yet (waiting for RFID grace)
        assert len(session_started_events) == 0, (
            "SESSION_START must not fire immediately when trx=null and grace>0"
        )
        # active_session should be None (session is not committed yet)
        assert engine.active_session is None, "active_session must be None during RFID grace window"

        # Step 2: RFID blip arrives within grace window (< GRACE_SECONDS elapsed)
        freezer.tick(timedelta(seconds=3))
        async_fire_time_changed(hass, dt_util.utcnow())
        hass.states.async_set(MOCK_TRX_ENTITY, "2")  # trx=2 → Petra
        await hass.async_block_till_done()

        # Session must now be active and attributed to Petra
        assert engine.active_session is not None, (
            "Session must start immediately when trx becomes non-null during grace"
        )
        assert engine.active_session.user_name == "Petra", (
            f"Expected user=Petra, got {engine.active_session.user_name!r}"
        )
        assert len(session_started_events) == 1, (
            f"Exactly one SESSION_START must fire, got {len(session_started_events)}"
        )

        # connected_at must be the plug-on time, not the trx-resolution time (FR-006)
        session_connected_at = engine.active_session.connected_at
        plug_on_iso = plug_on_time.isoformat()
        # connected_at should be within 1 second of plug_on_time (not 3+ seconds later)
        from datetime import datetime

        cat_dt = datetime.fromisoformat(session_connected_at)
        plug_on_naive = plug_on_time.replace(tzinfo=None) if plug_on_time.tzinfo else plug_on_time
        diff = abs((cat_dt.replace(tzinfo=None) - plug_on_naive).total_seconds())
        assert diff < 2.0, (
            f"connected_at ({session_connected_at}) must be the plug-on time "
            f"({plug_on_iso}), diff={diff:.1f}s (FR-006)"
        )


# ---------------------------------------------------------------------------
# Scenario (b): plug-on with trx=null, grace expires → user=Unknown
# ---------------------------------------------------------------------------


async def test_rfid_grace_expires_without_rfid_user_unknown(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Scenario (b): grace window expires without RFID blip → session with user=Unknown.

    Verifies FR-003: fallback to Unknown attribution when grace expires.
    """
    entry = await _make_engine_entry(hass, rfid_grace_seconds=GRACE_SECONDS)
    engine = _get_engine(hass, entry)

    session_started_events: list[dict] = []

    def _capture(event) -> None:
        session_started_events.append(dict(event.data))

    hass.bus.async_listen(EVENT_SESSION_STARTED, _capture)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in, trx stays null
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        # No session yet
        assert engine.active_session is None, "No session during grace window"
        assert len(session_started_events) == 0

        # Advance past the grace window to trigger expiry
        freezer.tick(timedelta(seconds=GRACE_SECONDS + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Session must now have fired with user=Unknown
        assert engine.active_session is not None, "Session must start after grace expires (FR-003)"
        assert engine.active_session.user_name == "Unknown", (
            f"Expected user=Unknown after grace expiry, got {engine.active_session.user_name!r}"
        )
        assert len(session_started_events) == 1, (
            f"Exactly one SESSION_START must fire after grace expiry, "
            f"got {len(session_started_events)}"
        )


# ---------------------------------------------------------------------------
# Scenario (c): plug-on then plug-off before grace expires → no SESSION_START
# ---------------------------------------------------------------------------


async def test_rfid_grace_plug_off_before_expiry_no_session(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Scenario (c): plug-off during grace window → no session committed (FR-004).

    This tests the negative-assertion pattern correctly: we advance time past
    the grace period AFTER the plug-off to confirm no delayed fire occurs.
    The trigger (plug-off) fires, then we advance time to confirm no late start.
    """
    entry = await _make_engine_entry(hass, rfid_grace_seconds=GRACE_SECONDS)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    session_started_events: list[dict] = []

    def _capture(event) -> None:
        session_started_events.append(dict(event.data))

    hass.bus.async_listen(EVENT_SESSION_STARTED, _capture)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in, trx stays null → grace starts
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        # Advance 2 s (within grace)
        freezer.tick(timedelta(seconds=2))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Plug off during grace — cable lock is Unlocked → real unplug
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

        # Engine should return to IDLE (or COMPLETING briefly then IDLE)
        # Most importantly: no session_started event, no active session
        # Now advance well past grace to confirm no timer fires late
        freezer.tick(timedelta(seconds=GRACE_SECONDS + 5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert len(session_started_events) == 0, (
            f"No SESSION_START must fire when plug-off during grace; "
            f"got {len(session_started_events)} events: {session_started_events}"
        )
        assert engine.active_session is None, (
            "active_session must be None after plug-off during grace"
        )

        # Verify session store was not written (no spurious Unknown session)
        sessions_in_store = session_store.sessions
        assert len(sessions_in_store) == 0, (
            f"No sessions must be persisted after plug-off during grace, "
            f"got {len(sessions_in_store)}"
        )


# ---------------------------------------------------------------------------
# Scenario (d): plug enters invalid state during grace → no SESSION_START
# ---------------------------------------------------------------------------


async def test_rfid_grace_plug_invalid_during_grace_no_session(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Scenario (d): plug goes unavailable during grace → timer cancelled, no session.

    Tests FR-005: cancel grace timer when plug enters _INVALID_STATES.
    This is the negative-assertion trap scenario: we advance time past grace
    AFTER the invalid-state trigger so the assertion is not trivially true.
    """
    entry = await _make_engine_entry(hass, rfid_grace_seconds=GRACE_SECONDS)
    engine = _get_engine(hass, entry)

    session_started_events: list[dict] = []

    def _capture(event) -> None:
        session_started_events.append(dict(event.data))

    hass.bus.async_listen(EVENT_SESSION_STARTED, _capture)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in, trx stays null → grace starts
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        # Advance 2 s (within grace)
        freezer.tick(timedelta(seconds=2))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Plug entity goes unavailable during grace (FR-005)
        hass.states.async_set(MOCK_PLUG_ENTITY, "unavailable")
        await hass.async_block_till_done()

        # Advance past the original grace deadline — grace timer must have been cancelled
        freezer.tick(timedelta(seconds=GRACE_SECONDS + 5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert len(session_started_events) == 0, (
            f"No SESSION_START must fire when plug goes invalid during grace; "
            f"got {len(session_started_events)} events"
        )
        assert engine.active_session is None, (
            "active_session must be None after plug goes invalid during grace"
        )


# ---------------------------------------------------------------------------
# Scenario (e): plug-on with trx="0" (open-access) → immediate session (no defer)
# ---------------------------------------------------------------------------


async def test_rfid_grace_trx_zero_immediate_start(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Scenario (e): trx='0' at plug-on → immediate session start, no grace deferral.

    FR-008: trx="0" is NOT in _INVALID_STATES; it is the open-access path.
    Session must start immediately, not after GRACE_SECONDS.
    """
    entry = await _make_engine_entry(hass, rfid_grace_seconds=GRACE_SECONDS)
    engine = _get_engine(hass, entry)

    session_started_events: list[dict] = []

    def _capture(event) -> None:
        session_started_events.append(dict(event.data))

    hass.bus.async_listen(EVENT_SESSION_STARTED, _capture)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Set trx="0" BEFORE plug-on (already authorized, open-access)
        hass.states.async_set(MOCK_TRX_ENTITY, "0")
        await hass.async_block_till_done()

        # Plug in with trx="0" already set → immediate start
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        # Session must have started immediately (no delay)
        assert engine.active_session is not None, (
            "Session must start immediately when trx='0' at plug-on (FR-008)"
        )
        assert len(session_started_events) == 1, (
            f"SESSION_START must fire immediately for trx='0', "
            f"got {len(session_started_events)} events"
        )

        # No time has elapsed — this confirms it was immediate, not deferred
        # (We have not called freezer.tick at all)


# ---------------------------------------------------------------------------
# Scenario (f): connected_at = plug-on timestamp, not fire-time (FR-006)
# ---------------------------------------------------------------------------


async def test_rfid_grace_connected_at_is_plug_on_time(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Scenario (f): connected_at records plug-on time even when session fires after grace.

    FR-006: connected_at must be the plug-on timestamp regardless of how the
    session start was triggered (timer expiry or trx resolution during grace).

    This test uses the grace-expiry path (no RFID) and verifies that
    connected_at == plug-on time, not the grace-expiry time.
    """
    entry = await _make_engine_entry(hass, rfid_grace_seconds=GRACE_SECONDS)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Record time at plug-on
        plug_on_time = dt_util.utcnow()

        # Plug in, trx stays null
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        # Advance exactly GRACE_SECONDS + 1 to fire the expiry callback
        freezer.tick(timedelta(seconds=GRACE_SECONDS + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert engine.active_session is not None, "Session must exist after grace expiry"

        # connected_at must match plug-on time, not (plug_on + GRACE_SECONDS + 1)
        from datetime import datetime

        session_connected_at = engine.active_session.connected_at
        cat_dt = datetime.fromisoformat(session_connected_at)
        # Make both timezone-naive for comparison
        if cat_dt.tzinfo is not None:
            cat_ts = cat_dt.timestamp()
        else:
            cat_ts = cat_dt.replace(tzinfo=dt_util.UTC).timestamp()
        plug_ts = plug_on_time.timestamp()
        grace_expiry_ts = plug_ts + GRACE_SECONDS + 1

        diff_from_plug = abs(cat_ts - plug_ts)
        diff_from_expiry = abs(cat_ts - grace_expiry_ts)

        assert diff_from_plug < 2.0, (
            f"connected_at must be close to plug-on time (diff={diff_from_plug:.1f}s), "
            f"not the grace-expiry time (diff_from_expiry={diff_from_expiry:.1f}s) — FR-006"
        )


# ---------------------------------------------------------------------------
# Scenario (g): rfid_grace_seconds=0 disables the grace timer → immediate start
# ---------------------------------------------------------------------------


async def test_rfid_grace_disabled_immediate_start(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Scenario (g): rfid_grace_seconds=0 reverts to legacy immediate-start behavior.

    FR-007: setting rfid_grace_seconds=0 disables the grace timer entirely.
    Even with trx=null at plug-on, the session starts immediately (user=Unknown).
    """
    entry = await _make_engine_entry(hass, rfid_grace_seconds=0)
    engine = _get_engine(hass, entry)

    session_started_events: list[dict] = []

    def _capture(event) -> None:
        session_started_events.append(dict(event.data))

    hass.bus.async_listen(EVENT_SESSION_STARTED, _capture)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in with trx=null and grace=0 → immediate start
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        # Session must have started immediately (legacy behavior)
        assert engine.active_session is not None, (
            "Session must start immediately when rfid_grace_seconds=0 (FR-007)"
        )
        assert len(session_started_events) == 1, (
            f"SESSION_START must fire immediately when grace disabled, "
            f"got {len(session_started_events)} events"
        )
        assert engine.active_session.user_name == "Unknown", (
            f"User must be Unknown for trx=null, got {engine.active_session.user_name!r}"
        )


# ---------------------------------------------------------------------------
# A2: late session-start task aborts after plug-off (idempotency guard)
# ---------------------------------------------------------------------------


async def test_rfid_grace_late_task_aborts_after_plug_off(
    hass: HomeAssistant,
    freezer,
) -> None:
    """A2: grace-expiry task that fires after plug-off must abort, not create a session.

    Scenario: grace timer is scheduled → plug-off arrives → grace fires after.
    Without the idempotency guard, _async_start_session would create an orphaned
    active session even though the engine is already IDLE / COMPLETING (FR-004).

    This test exercises the guard by:
      1. Plugging in (trx=null, grace=5s) → grace task is scheduled.
      2. Unplugging BEFORE the grace fires (within the grace window).
      3. Advancing time PAST the grace deadline → the grace callback fires.
      4. Asserting: no session started, engine.active_session is None.
    """
    entry = await _make_engine_entry(hass, rfid_grace_seconds=GRACE_SECONDS)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    session_started_events: list[dict] = []

    def _capture(event) -> None:
        session_started_events.append(dict(event.data))

    hass.bus.async_listen(EVENT_SESSION_STARTED, _capture)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Step 1: plug in → grace timer scheduled, no session yet
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        assert engine.active_session is None, "No session must exist during grace window"

        # Step 2: unplug before grace fires (1s elapsed of 5s grace)
        freezer.tick(timedelta(seconds=1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

        # Step 3: advance past the original grace deadline (the scheduled task fires)
        freezer.tick(timedelta(seconds=GRACE_SECONDS + 5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Step 4: assert no session was created
        assert len(session_started_events) == 0, (
            f"No SESSION_START must fire when plug-off precedes grace expiry; "
            f"got {len(session_started_events)} events: {session_started_events}"
        )
        assert engine.active_session is None, (
            "active_session must be None — late grace task must abort via idempotency guard"
        )
        assert len(session_store.sessions) == 0, (
            f"No sessions must be persisted; got {len(session_store.sessions)}"
        )
