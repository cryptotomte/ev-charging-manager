"""Tests for micro-session filtering (T016).

PR-27 (023-recovery-hardening) adds a v2-engine section at the bottom:
micro discards clear the persisted active-session snapshot (FR-006/FR-007)
and the micro filter uses AND semantics on connection-timestamp parse
failure (FR-018).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.ev_charging_manager.const import (
    CONF_CHARGING_IDLE_TIMEOUT_MIN,
    CONF_DISCONNECT_GRACE_MIN,
    DOMAIN,
    EVENT_SESSION_COMPLETED,
    EVENT_SESSION_STARTED,
    SessionEngineState,
)
from custom_components.ev_charging_manager.session_store import SessionStore
from tests.conftest import (
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    MOCK_POWER_ENTITY,
    MOCK_TRX_ENTITY,
    setup_session_engine,
    start_charging_session,
    stop_charging_session,
)

# Module path for patching dt_util in the session engine
_ENGINE_MODULE = "custom_components.ev_charging_manager.session_engine"


async def test_micro_session_under_duration_discarded(hass: HomeAssistant):
    """Session < 60s (with >= 50 Wh) is discarded — no completed event."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)
    started_events = async_capture_events(hass, EVENT_SESSION_STARTED)

    await setup_session_engine(hass, entry)
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="0")

    # 100 Wh > 50 Wh — enough energy, but < 60s duration (no time advance)
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.1")
    await hass.async_block_till_done()

    # End session immediately (< 60s)
    await stop_charging_session(hass)

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.IDLE
    assert len(completed_events) == 0
    assert len(started_events) == 1  # started event still fired


async def test_micro_session_under_energy_discarded(hass: HomeAssistant):
    """Session > 60s but < 50 Wh is discarded."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    session_start = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)

    with patch(f"{_ENGINE_MODULE}.dt_util") as mock_dt:
        mock_dt.utcnow.return_value = session_start

        await setup_session_engine(hass, entry)
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
        await start_charging_session(hass, trx_value="0")

        # Advance time by 120s
        mock_dt.utcnow.return_value = session_start + timedelta(seconds=120)

        # Very little energy: 0.01 kWh = 10 Wh < 50 Wh threshold
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.01")
        await hass.async_block_till_done()

        await stop_charging_session(hass)

    assert len(completed_events) == 0


async def test_valid_session_above_thresholds_persisted(hass: HomeAssistant):
    """Session > 60s AND > 50 Wh is persisted and fires completed event."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    session_start = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)

    with patch(f"{_ENGINE_MODULE}.dt_util") as mock_dt:
        mock_dt.utcnow.return_value = session_start

        await setup_session_engine(hass, entry)
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
        await start_charging_session(hass, trx_value="0")

        # Advance time by 120s
        mock_dt.utcnow.return_value = session_start + timedelta(seconds=120)

        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.1")  # 100 Wh > 50 Wh
        await hass.async_block_till_done()

        await stop_charging_session(hass)

    assert len(completed_events) == 1
    event_data = completed_events[0].data
    assert event_data["energy_kwh"] > 0


async def test_session_at_exact_thresholds_is_valid(hass: HomeAssistant):
    """Session above min_duration and min_energy is NOT a micro-session."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    session_start = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)

    with patch(f"{_ENGINE_MODULE}.dt_util") as mock_dt:
        mock_dt.utcnow.return_value = session_start

        await setup_session_engine(hass, entry)
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
        await start_charging_session(hass, trx_value="0")

        # Advance time by 120s (> 60s threshold)
        mock_dt.utcnow.return_value = session_start + timedelta(seconds=120)

        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.1")  # 100 Wh > 50 Wh
        await hass.async_block_till_done()

        await stop_charging_session(hass)

    assert len(completed_events) == 1


async def test_started_event_always_fired_for_micro_sessions(hass: HomeAssistant):
    """session_started event fires even for sessions that become micro-sessions."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    started_events = async_capture_events(hass, EVENT_SESSION_STARTED)
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    await setup_session_engine(hass, entry)
    await start_charging_session(hass, trx_value="0")

    # Immediately end (micro-session)
    await stop_charging_session(hass)

    assert len(started_events) == 1
    assert len(completed_events) == 0


# ===========================================================================
# PR-27 (023-recovery-hardening) US2/US5: micro discards clear the persisted
# snapshot (FR-006/FR-007) and FR-018 parse-failure AND semantics —
# plug-anchored (goe_gemini) engine
# ===========================================================================

MOCK_PLUG_ENTITY = "binary_sensor.goe_abc123_car_0"
MOCK_CABLE_LOCK_ENTITY = "sensor.goe_abc123_cus_value"


async def _make_v2_entry(
    hass: HomeAssistant,
    *,
    snapshot: dict | None = None,
    plug_state: str = "off",
    cable_lock_state: str = "Unlocked",
    energy_state: str = "0.0",
    power_state: str = "0.0",
) -> MockConfigEntry:
    """Set up a goe_gemini (plug-anchored) entry, optionally with a recovery snapshot."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
            CONF_CHARGING_IDLE_TIMEOUT_MIN: 5,
            CONF_DISCONNECT_GRACE_MIN: 10,
        },
        title="Test go-e Charger (micro)",
    )

    hass.states.async_set(MOCK_PLUG_ENTITY, plug_state)
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, cable_lock_state)
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, energy_state)
    hass.states.async_set(MOCK_POWER_ENTITY, power_state)

    raw_store_data = None
    if snapshot is not None:
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


def _make_micro_snapshot(connected_at: str, energy_kwh: float = 0.001) -> dict:
    """Build an active-session snapshot that will classify as micro at recovery."""
    return {
        "id": "micro-snap-001",
        "user_name": "Micro User",
        "user_type": "regular",
        "rfid_index": None,
        "charger_name": "Test Charger",
        "started_at": connected_at,
        "connected_at": connected_at,
        "energy_start_kwh": 0.0,
        "energy_kwh": energy_kwh,
        "charging_started_at": None,
        "charging_ended_at": None,
        "charging_duration_s": 0,
        "charging_window_count": 0,
    }


async def test_v2_live_micro_discard_clears_snapshot(hass: HomeAssistant):
    """FR-006 (live path): a micro discard must remove the persisted snapshot.

    Fresh-install variant included: the completed-sessions list is empty, so
    the cleared envelope must simply contain no sessions.
    """
    entry = await _make_v2_entry(hass)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    clear_spy = AsyncMock(wraps=session_store.async_clear_active_session)
    save_mock = AsyncMock()
    with (
        patch.object(session_store, "async_clear_active_session", clear_spy),
        patch("homeassistant.helpers.storage.Store.async_save", save_mock),
    ):
        # Plug in with a known trx and a tiny bit of energy, then unplug
        # immediately — under the 60 s / 50 Wh thresholds → micro discard.
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.001")
        await hass.async_block_till_done()

        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE
    assert len(completed_events) == 0, "micro discard fires no completion event"
    assert len(session_store.sessions) == 0, "micro session is not stored"
    assert clear_spy.await_count == 1, (
        "FR-006: the live micro-discard path must clear the persisted snapshot"
    )
    # Fresh install: the cleared envelope contains no sessions at all.
    session_envelopes = [
        call.args[0]
        for call in save_mock.call_args_list
        if call.args
        and isinstance(call.args[0], dict)
        and call.args[0].get("key") == "ev_charging_manager_sessions"
    ]
    assert session_envelopes, "the clear must write the cleaned envelope to disk"
    assert session_envelopes[-1]["data"] == [], (
        "after a micro discard on a fresh install, the envelope must be empty"
    )


async def test_v2_recovery_as_micro_clears_snapshot_once(hass: HomeAssistant):
    """FR-006/FR-007 (recovery path): a snapshot finalized as micro at restart
    is cleared from disk — a second restart recovers nothing, no completion
    event fires, statistics are untouched."""
    # Snapshot connected 30 s ago with ~1 Wh — micro on both criteria.
    connected_at = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    snapshot = _make_micro_snapshot(connected_at, energy_kwh=0.001)
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    save_mock = AsyncMock()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
            CONF_CHARGING_IDLE_TIMEOUT_MIN: 5,
            CONF_DISCONNECT_GRACE_MIN: 10,
        },
        title="Test go-e Charger (micro recovery)",
    )
    # plug=off at restart → snapshot is finalized (and classified micro).
    hass.states.async_set(MOCK_PLUG_ENTITY, "off")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.001")
    hass.states.async_set(MOCK_POWER_ENTITY, "0.0")

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
        patch("homeassistant.helpers.storage.Store.async_save", save_mock),
    ):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    assert engine.state == SessionEngineState.IDLE
    assert engine.active_session is None
    assert len(completed_events) == 0, (
        "a recovery-as-micro discard must not fire a completion event (stats untouched)"
    )
    assert len(session_store.sessions) == 0

    # The last session-store envelope written must contain no incomplete entry —
    # i.e. a second restart finds nothing to recover.
    session_envelopes = [
        call.args[0]
        for call in save_mock.call_args_list
        if call.args
        and isinstance(call.args[0], dict)
        and call.args[0].get("key") == "ev_charging_manager_sessions"
    ]
    assert session_envelopes, "the micro discard must rewrite the envelope on disk"
    final_envelope = session_envelopes[-1]
    assert final_envelope["data"] == [], (
        f"snapshot must be gone from disk after micro discard, got {final_envelope['data']!r}"
    )

    # Simulate the second restart: a fresh store loading the final envelope
    # must find neither sessions nor a recovery snapshot.
    store2 = SessionStore(hass)
    with (
        patch.object(
            store2._store, "async_load", new_callable=AsyncMock, return_value=final_envelope
        ),
        patch.object(store2._store, "async_save", new_callable=AsyncMock),
    ):
        sessions2, snapshot2 = await store2.async_load()
    assert sessions2 == []
    assert snapshot2 is None, "second restart must not recover the discarded micro session"
