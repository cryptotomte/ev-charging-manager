"""PR-25 (021-cable-lock-race): cable_lock→Unlocked unplug confirmation tests.

On the goe_gemini profile the charger fires ``plug: on→off`` 0–3 s BEFORE
``cable_lock: Locked→Unlocked`` on a genuine unplug. ``_handle_plug_off`` reads
cable_lock synchronously, sees ``Locked``, and starts the transient-disconnect
grace timer. The lagging ``cable_lock→Unlocked`` must re-evaluate that decision
and confirm the unplug, completing the session immediately.

Covers FR-001..FR-012 and the User Story acceptance scenarios / quickstart.md.
All HA entities are mocked — no real hardware.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.ev_charging_manager.const import (
    CONF_CHARGING_IDLE_TIMEOUT_MIN,
    CONF_DISCONNECT_GRACE_MIN,
    DEBUG_CAT_SESSION_ENDED_BY_CABLE_UNLOCK,
    DEBUG_CAT_SESSION_FORCE_ENDED_BY_GRACE_TIMEOUT,
    DOMAIN,
    EVENT_SESSION_COMPLETED,
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

GRACE_TIMEOUT_MIN = 5  # short grace for fast test execution


# ---------------------------------------------------------------------------
# Setup helpers (mirror tests/test_session_engine_disconnect.py conventions)
# ---------------------------------------------------------------------------


async def _make_engine_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create and set up a config entry with PlugAnchoredSessionEngine active."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
            CONF_CHARGING_IDLE_TIMEOUT_MIN: 5,
            CONF_DISCONNECT_GRACE_MIN: GRACE_TIMEOUT_MIN,
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
    assert isinstance(engine, PlugAnchoredSessionEngine)
    return engine


class _CaptureLogger:
    """Minimal DebugLogger stand-in that records (category, message) pairs.

    Debug logging is disabled by default in tests, so ``engine._debug_logger`` is
    ``None``; tests that assert on log categories inject this capture object after
    setup. Signature mirrors ``DebugLogger.log(category, message)``.
    """

    enabled = True  # _handle_observation_change reads this before formatting

    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []

    def log(self, category: str, message: str) -> None:
        self.entries.append((category, message))

    @property
    def categories(self) -> list[str]:
        return [c for c, _ in self.entries]


async def _add_user(hass: HomeAssistant, entry_id: str, name: str) -> str:
    """Add a regular user via the subentry config flow. Returns the subentry_id."""
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "user"),
        context={"source": "user"},
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": name, "type": "regular"},
    )
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry_id)
    return [s for s in entry.subentries.values() if s.subentry_type == "user"][-1].subentry_id


async def _add_rfid(hass: HomeAssistant, entry_id: str, card_index: int, user_id: str) -> None:
    """Add an RFID mapping via the subentry config flow (card_index N → trx N+1)."""
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "rfid_mapping"),
        context={"source": "user"},
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"card_index": str(card_index), "user_id": user_id},
    )
    await hass.async_block_till_done()


async def _plug_in_and_charge(
    hass: HomeAssistant,
    trx_value: str = "2",
    energy_kwh: float = 5.0,
    start_energy: float = 0.0,
) -> None:
    """Plug in, blip RFID, and start charging to create an active session.

    Sequence mirrors the PR-24 event-driven model: plug-on enters RFID wait,
    trx non-null resolves the wait and starts the session, power > 0 opens a
    charging window.

    Args:
        trx_value: the RFID card slot reported (card_index N → trx N+1).
        energy_kwh: the lifetime energy-counter reading at the end of charging.
        start_energy: the lifetime energy-counter reading at session start. For a
            second session after a first one completed, pass a value ≥ the first
            session's final reading so the cumulative counter stays monotonic
            (avoids a spurious energy-counter-reset and a zero-energy micro-session).
    """
    # Clear any stale card from a previous session (the go-e drops trx→null on
    # unplug); plug-on with trx=null enters the PR-24 RFID wait, and the blip below
    # resolves it. Without this reset, a stale non-null trx would immediately start
    # a session attributed to the previous card.
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, str(start_energy))
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_TRX_ENTITY, trx_value)
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_ENERGY_ENTITY, str(start_energy + 0.5))
    hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_ENERGY_ENTITY, str(start_energy + energy_kwh))
    await hass.async_block_till_done()


async def _transient_plug_off(hass: HomeAssistant) -> None:
    """Simulate plug→off while cable_lock still reads Locked (the race)."""
    # cable_lock is still Locked at the plug-off instant (the 0–3 s race window).
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_PLUG_ENTITY, "off")
    await hass.async_block_till_done()


async def _cable_unlock(hass: HomeAssistant) -> None:
    """Fire cable_lock→Unlocked (the lagging confirmation signal)."""
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# T010 [US1] — confirmation completes the session
# ---------------------------------------------------------------------------


async def test_cable_unlock_confirms_unplug_completes_session(hass: HomeAssistant, freezer) -> None:
    """T010: cable_lock→Unlocked (plug off, grace pending) completes session A."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]
    capture = _CaptureLogger()
    engine._debug_logger = capture

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=8.0)

        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert engine.active_session is not None
        session_id = engine.active_session.id

        # plug→off while cable_lock still Locked → transient branch / grace timer
        await _transient_plug_off(hass)
        plug_off_at = dt_util.utcnow()
        assert engine.active_session is not None, "grace timer should hold session open"
        assert engine._disconnect_grace_cancel is not None, "grace timer must be pending"

        # cable_lock→Unlocked confirms the unplug ~3 s later (plug still off)
        freezer.tick(timedelta(seconds=3))
        await _cable_unlock(hass)

    assert engine.active_session is None, "session A must be completed on confirmation"
    assert engine.state == SessionEngineState.IDLE
    assert engine._disconnect_grace_cancel is None, "grace timer must be cancelled (FR-003)"
    assert len(session_store.sessions) == 1, "exactly one session persisted"
    assert session_store.sessions[0]["id"] == session_id

    # FR-009 / SC-005: completion logged under the dedicated category.
    assert DEBUG_CAT_SESSION_ENDED_BY_CABLE_UNLOCK in capture.categories, (
        f"confirmation completion must be logged; saw {capture.categories}"
    )
    # FR-009: the log body must carry enough context to audit the boundary decision —
    # the session id and the (original plug-off) disconnect timestamp.
    unlock_msg = next(
        msg for cat, msg in capture.entries if cat == DEBUG_CAT_SESSION_ENDED_BY_CABLE_UNLOCK
    )
    assert session_id in unlock_msg, (
        f"confirmation log must contain session id {session_id!r}; got {unlock_msg!r}"
    )
    assert plug_off_at.isoformat() in unlock_msg, (
        f"confirmation log must contain the plug-off timestamp "
        f"{plug_off_at.isoformat()!r}; got {unlock_msg!r}"
    )


# ---------------------------------------------------------------------------
# T011 [US1] — next plug-on starts a NEW session for a NEW user
# ---------------------------------------------------------------------------


async def test_after_confirm_new_plug_starts_new_session(hass: HomeAssistant, freezer) -> None:
    """T011: after confirmation, plug=on + trx-B starts a fresh session for B."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    # Two users, two cards: card 1 (trx=2) → Petra, card 2 (trx=3) → Paul.
    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        petra_id = await _add_user(hass, entry.entry_id, "Petra")
        paul_id = await _add_user(hass, entry.entry_id, "Paul")
        await _add_rfid(hass, entry.entry_id, card_index=1, user_id=petra_id)
        await _add_rfid(hass, entry.entry_id, card_index=2, user_id=paul_id)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Session A — Petra (trx=2)
        await _plug_in_and_charge(hass, trx_value="2", energy_kwh=8.0)
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert engine.active_session is not None
        session_a_id = engine.active_session.id
        assert engine.active_session.user_name == "Petra"

        # Genuine unplug with cable_lock lag → confirmation completes A
        await _transient_plug_off(hass)
        freezer.tick(timedelta(seconds=3))
        await _cable_unlock(hass)
        assert engine.active_session is None

        # Session B — Paul (trx=3) plugs in within the old grace window.
        # start_energy=8.0 keeps the lifetime counter monotonic after Petra's 8 kWh.
        freezer.tick(timedelta(seconds=30))
        await _plug_in_and_charge(hass, trx_value="3", energy_kwh=6.0, start_energy=8.0)
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert engine.active_session is not None, "Paul's plug-in must start a NEW session"
        session_b_id = engine.active_session.id
        assert session_b_id != session_a_id, "B must NOT be a window of A"
        assert engine.active_session.user_name == "Paul", (
            f"B must be attributed to Paul, got {engine.active_session.user_name!r}"
        )

        # Complete B cleanly to verify it persists as its own session.
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    assert len(session_store.sessions) == 2, (
        f"expected 2 distinct sessions (Petra, Paul), got {len(session_store.sessions)}"
    )
    users = {s["user_name"] for s in session_store.sessions}
    assert users == {"Petra", "Paul"}, f"sessions must be attributed separately, got {users}"


# ---------------------------------------------------------------------------
# T012 [US1] — 2026-05-29 incident replay (SC-002)
# ---------------------------------------------------------------------------


async def test_incident_replay_2026_05_29(hass: HomeAssistant, freezer) -> None:
    """T012/SC-002: Petra unplug @07:31:04, cable_lock→Unlocked @07:31:07,
    Paul plug-on @07:32:44 → two correctly-attributed sessions, Petra ends at plug-off.
    """
    freezer.move_to("2026-05-29T07:00:00+00:00")
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        petra_id = await _add_user(hass, entry.entry_id, "Petra")
        paul_id = await _add_user(hass, entry.entry_id, "Paul")
        await _add_rfid(hass, entry.entry_id, card_index=1, user_id=petra_id)
        await _add_rfid(hass, entry.entry_id, card_index=2, user_id=paul_id)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Petra charges for a while.
        freezer.move_to("2026-05-29T07:20:00+00:00")
        await _plug_in_and_charge(hass, trx_value="2", energy_kwh=9.0)

        # 07:31:04 — plug→off while cable_lock still Locked (the race).
        freezer.move_to("2026-05-29T07:31:04+00:00")
        plug_off_at = dt_util.utcnow()
        await _transient_plug_off(hass)

        # 07:31:07 — cable_lock→Unlocked confirms (plug still off).
        freezer.move_to("2026-05-29T07:31:07+00:00")
        await _cable_unlock(hass)

    assert engine.active_session is None, "Petra session must end at confirmation"
    assert len(session_store.sessions) == 1
    petra_session = session_store.sessions[0]
    assert petra_session["user_name"] == "Petra"
    # FR-011: disconnected_at == plug-off time (07:31:04), not 07:31:07.
    assert petra_session["disconnected_at"] == plug_off_at.isoformat(), (
        f"disconnect must be the plug-off moment, got {petra_session['disconnected_at']!r}"
    )

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # 07:32:44 — Paul plugs in (1 min 40 s after unplug, well inside old grace).
        # start_energy=9.0 keeps the lifetime counter monotonic after Petra's 9 kWh.
        freezer.move_to("2026-05-29T07:32:44+00:00")
        await _plug_in_and_charge(hass, trx_value="3", energy_kwh=5.0, start_energy=9.0)
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert engine.active_session is not None
        assert engine.active_session.user_name == "Paul", "07:32:44 plug-on must start Paul session"
        assert engine.active_session.id != petra_session["id"], "must not resume Petra's session"


# ---------------------------------------------------------------------------
# T013 [US2] — ends without waiting grace + no double-completion
# ---------------------------------------------------------------------------


async def test_cable_unlock_ends_without_waiting_grace(hass: HomeAssistant, freezer) -> None:
    """T013: confirmation completes immediately; grace timeout does not re-complete."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=7.0)
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        await _transient_plug_off(hass)
        freezer.tick(timedelta(seconds=2))
        await _cable_unlock(hass)

        assert engine.active_session is None, "session completes at the Unlocked moment"
        assert len(session_store.sessions) == 1

        # Advance well past the grace timeout — the cancelled timer must not fire
        # a second completion / second persist.
        freezer.tick(timedelta(minutes=GRACE_TIMEOUT_MIN + 2))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

    assert len(session_store.sessions) == 1, "no double-completion after grace window elapses"
    assert engine.state == SessionEngineState.IDLE


# ---------------------------------------------------------------------------
# T014 [US2] — disconnect timestamp == plug-off time (FR-011)
# ---------------------------------------------------------------------------


async def test_confirm_disconnect_timestamp_is_plug_off_time(hass: HomeAssistant, freezer) -> None:
    """T014/FR-011: connection_duration ends at plug-off, not the +3 s confirmation."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=8.0)
        connected_at = datetime.fromisoformat(engine.active_session.connected_at)

        freezer.tick(timedelta(minutes=10))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        await _transient_plug_off(hass)
        plug_off_at = dt_util.utcnow()

        # Confirmation arrives 3 s later — must NOT be counted in connection_duration.
        freezer.tick(timedelta(seconds=3))
        await _cable_unlock(hass)

    assert len(session_store.sessions) == 1
    session = session_store.sessions[0]
    assert session["disconnected_at"] == plug_off_at.isoformat()
    expected_conn = int((plug_off_at - connected_at).total_seconds())
    assert session["connection_duration_s"] == expected_conn, (
        f"connection_duration must end at plug-off ({expected_conn}s), "
        f"got {session['connection_duration_s']}s"
    )


# ---------------------------------------------------------------------------
# T015 [US3] — data_gap cleared on confirm (FR-012)
# ---------------------------------------------------------------------------


async def test_data_gap_cleared_on_confirm(hass: HomeAssistant, freezer) -> None:
    """T015/FR-012: no prior gap → confirmation clears the race-induced data_gap."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=6.0)
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        await _transient_plug_off(hass)
        # transient branch set data_gap=True
        assert engine.active_session.data_gap is True
        freezer.tick(timedelta(seconds=3))
        await _cable_unlock(hass)

    assert len(session_store.sessions) == 1
    assert session_store.sessions[0]["data_gap"] is False, (
        "race-induced data_gap must be cleared on confirmation (FR-012)"
    )


# ---------------------------------------------------------------------------
# T016 [US3] — data_gap preserved when a genuine gap existed (FR-012)
# ---------------------------------------------------------------------------


async def test_data_gap_preserved_when_genuine(hass: HomeAssistant, freezer) -> None:
    """T016/FR-012: a genuine mid-session gap survives the confirmation."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=6.0)
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Genuine mid-session gap: power sensor goes unavailable → data_gap=True.
        hass.states.async_set(MOCK_POWER_ENTITY, "unavailable")
        await hass.async_block_till_done()
        assert engine.active_session.data_gap is True, "genuine gap must set data_gap"

        # Power recovers, then a genuine unplug with cable_lock lag occurs.
        hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
        await hass.async_block_till_done()
        await _transient_plug_off(hass)
        # Pin the actual FR-012 snapshot mechanism, not just the end state: the
        # transient branch must have snapshotted the genuine (True) data_gap so the
        # confirmation can restore it rather than clear it.
        assert engine._data_gap_before_disconnect is True
        freezer.tick(timedelta(seconds=3))
        await _cable_unlock(hass)

    assert len(session_store.sessions) == 1
    assert session_store.sessions[0]["data_gap"] is True, (
        "an earlier genuine data_gap must be preserved through confirmation (FR-012)"
    )


# ---------------------------------------------------------------------------
# T017 [US3] — no-op while plug is on (FR-004)
# ---------------------------------------------------------------------------


async def test_cable_unlock_noop_while_plug_on(hass: HomeAssistant, freezer) -> None:
    """T017/FR-004: cable_lock→Unlocked while plug is on does not end the session."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=6.0)
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        session_id = engine.active_session.id

        # plug stays ON; cable_lock reports Unlocked anyway (spurious) → no boundary.
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()

        assert engine.active_session is not None, "session must continue while plug is on"
        assert engine.active_session.id == session_id
        assert engine.state == SessionEngineState.TRACKING

    assert len(session_store.sessions) == 0, "no completion while plug is on (FR-004)"


# ---------------------------------------------------------------------------
# T018 [US3] — no-op when idle or no grace pending (FR-005)
# ---------------------------------------------------------------------------


async def test_cable_unlock_noop_when_idle_or_no_grace(hass: HomeAssistant, freezer) -> None:
    """T018/FR-005: (a) idle engine no-op; (b) no double-completion after a
    synchronous-Unlocked completion when a late cable_lock toggle arrives.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # (a) Engine IDLE — a cable_lock→Unlocked is a safe no-op.
        assert engine.state == SessionEngineState.IDLE
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        assert engine.active_session is None
        assert len(session_store.sessions) == 0

        # (b) Synchronous-Unlocked completion (cable_lock already Unlocked at plug-off,
        #     so no grace timer is pending), then a late cable_lock toggle.
        await _plug_in_and_charge(hass, energy_kwh=6.0)
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()
        assert engine.active_session is None
        assert engine._disconnect_grace_cancel is None, "no grace pending after clean stop"
        assert len(session_store.sessions) == 1

        # Late spurious toggle Locked→Unlocked while idle → no second completion.
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()

    assert len(session_store.sessions) == 1, "no double-completion (FR-005)"


# ---------------------------------------------------------------------------
# T019 [US3] — no-op when charger offline (FR-028 of 018)
# ---------------------------------------------------------------------------


async def test_cable_unlock_noop_when_offline(hass: HomeAssistant, freezer) -> None:
    """T019: _charger_offline guard prevents confirmation."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=6.0)
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        await _transient_plug_off(hass)
        assert engine._disconnect_grace_cancel is not None

        # Force the offline guard, then fire cable_lock→Unlocked.
        engine._charger_offline = True
        freezer.tick(timedelta(seconds=3))
        await _cable_unlock(hass)

        assert engine.active_session is not None, "offline path is authoritative — no confirm"
        assert engine._disconnect_grace_cancel is not None, "grace timer must remain pending"

    assert len(session_store.sessions) == 0, "no completion while charger offline (FR-028)"


# ---------------------------------------------------------------------------
# T020 [US3] — transient resume + grace force-end unchanged (FR-007)
# ---------------------------------------------------------------------------


async def test_transient_resume_unchanged(hass: HomeAssistant, freezer) -> None:
    """T020/FR-007: plug-on within grace resumes; grace expiry force-ends with data_gap."""
    # --- part 1: resume on returning plug-on (no cable_lock→Unlocked) ---
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=6.0)
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        session_id = engine.active_session.id

        # Transient: plug off, cable_lock stays Locked (never Unlocked).
        await _transient_plug_off(hass)
        assert engine.active_session is not None
        assert engine._disconnect_grace_cancel is not None

        # Plug returns within grace → same session resumes.
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        await hass.async_block_till_done()
        assert engine.active_session is not None
        assert engine.active_session.id == session_id, "same session must resume"
        assert engine._disconnect_grace_cancel is None, "grace cancelled on resume"
        # PR-25 cleanup: plug-off context cleared on resolve.
        assert engine._plug_off_at is None
        assert engine._data_gap_before_disconnect is None

    assert len(session_store.sessions) == 0, "resumed session not yet persisted"

    # --- part 2: grace expiry force-ends (no recovery, no Unlocked) ---
    entry2 = await _make_engine_entry(hass)
    engine2 = _get_engine(hass, entry2)
    store2 = hass.data[DOMAIN][entry2.entry_id]["session_store"]
    capture = _CaptureLogger()
    engine2._debug_logger = capture

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=6.0)
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        await _transient_plug_off(hass)  # cable stays Locked
        freezer.tick(timedelta(minutes=GRACE_TIMEOUT_MIN + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

    assert len(store2.sessions) == 1, "grace expiry must force-end the session"
    assert store2.sessions[0]["data_gap"] is True, "force-end keeps data_gap True (FR-007)"
    assert DEBUG_CAT_SESSION_FORCE_ENDED_BY_GRACE_TIMEOUT in capture.categories, (
        f"grace expiry must log force-end category; saw {capture.categories}"
    )
    assert DEBUG_CAT_SESSION_ENDED_BY_CABLE_UNLOCK not in capture.categories, (
        "force-end must NOT be logged as a cable-unlock confirmation"
    )


# ---------------------------------------------------------------------------
# T021 [US3] — no session split on oscillation (FR-008 / FR-N01..N05)
# ---------------------------------------------------------------------------


async def test_no_session_split_on_oscillation(hass: HomeAssistant, freezer) -> None:
    """T021: power oscillation with the cable in produces exactly one session."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in + blip → session.
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
        hass.states.async_set(MOCK_POWER_ENTITY, "3456.0")
        await hass.async_block_till_done()

        session_id = engine.active_session.id

        # BMS pulses: power oscillates with the cable physically in (plug stays on,
        # cable_lock stays Locked). No cable_lock→Unlocked, no plug-off.
        for i in range(5):
            freezer.tick(timedelta(seconds=90))
            hass.states.async_set(MOCK_ENERGY_ENTITY, f"{(i + 1) * 0.3:.3f}")
            hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
            await hass.async_block_till_done()
            freezer.tick(timedelta(seconds=60))
            hass.states.async_set(MOCK_POWER_ENTITY, "3456.0")
            await hass.async_block_till_done()

        assert engine.active_session is not None
        assert engine.active_session.id == session_id, "no boundary fired on oscillation"

        # Real unplug at the end → exactly one session.
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    assert len(session_store.sessions) == 1, (
        f"oscillation must not split the session, got {len(session_store.sessions)}"
    )
    assert session_store.sessions[0]["id"] == session_id


# ---------------------------------------------------------------------------
# T022 [US3] — rapid Unlocked toggle while grace pending is idempotent (FR-003/005)
# ---------------------------------------------------------------------------


async def test_rapid_unlock_toggle_while_grace_pending(hass: HomeAssistant, freezer) -> None:
    """Two cable_lock→Unlocked transitions back-to-back (Locked in between) while a
    grace timer is pending and plug is off must complete exactly ONE session.

    This exercises the pre-task-execution re-entry window: the first Unlocked
    synchronously cancels the grace timer (``_disconnect_grace_cancel`` → None) and
    schedules the completion task. A second Unlocked arriving before that task runs
    must fall through the ``_disconnect_grace_cancel is None`` guard (and, after the
    task runs, the ``_active_session is None`` guard) and be a no-op — no double
    completion (FR-003 / FR-005).
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=6.0)
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        session_id = engine.active_session.id

        # plug→off while cable_lock still Locked → transient branch / grace pending.
        await _transient_plug_off(hass)
        assert engine._disconnect_grace_cancel is not None, "grace timer must be pending"

        freezer.tick(timedelta(seconds=3))
        # Fire Unlocked → Locked → Unlocked back-to-back WITHOUT settling the loop in
        # between, so the second Unlocked re-enters the handler before the completion
        # task scheduled by the first Unlocked has run.
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()

    assert engine.active_session is None, "session must be completed exactly once"
    assert engine.state == SessionEngineState.IDLE
    assert engine._disconnect_grace_cancel is None
    assert len(session_store.sessions) == 1, (
        f"rapid Unlocked toggle must not double-complete; got {len(session_store.sessions)}"
    )
    assert session_store.sessions[0]["id"] == session_id


# ---------------------------------------------------------------------------
# T023 [US3] — no-op during RFID wait (FR-004, spec 020 unchanged)
# ---------------------------------------------------------------------------


async def test_cable_unlock_noop_during_rfid_wait(hass: HomeAssistant, freezer) -> None:
    """cable_lock→Unlocked while an RFID wait is in flight (plug=on, no committed
    session, no grace pending) must be a no-op: plug is on, so it is not an unplug
    confirmation. Existing RFID-wait behavior (spec 020) is unchanged.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # plug-on with trx=null enters the PR-24 RFID wait (no session committed yet).
        hass.states.async_set(MOCK_TRX_ENTITY, "null")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        assert engine._rfid_wait is not None, "engine must be in the RFID wait state"
        assert engine.active_session is None, "no session committed during RFID wait"
        assert engine._disconnect_grace_cancel is None, "no grace pending during RFID wait"

        # cable_lock→Unlocked while plug is on → no-op (FR-004).
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()

        assert engine._rfid_wait is not None, "RFID wait must be unchanged"
        assert engine.active_session is None, "no session may be started by cable unlock"
        assert engine._disconnect_grace_cancel is None, "no grace timer may be started"

    assert len(session_store.sessions) == 0, "no completion during RFID wait"


# ===========================================================================
# PR-27 (023-recovery-hardening) US2: completion idempotency under trigger
# flapping (FR-008/FR-009/FR-010)
# ===========================================================================


def _make_add_session_with_injection(session_store, inject):
    """Wrap session_store.add_session: yield once, run `inject`, then persist.

    AsyncMock awaits complete synchronously and HA dispatches state-change
    listeners only when the loop settles, so flapping driven via
    ``hass.states.async_set`` can never land inside the completion's first
    await in tests. The injection models exactly that production window: the
    completion task is parked at the persist await and a trigger handler runs
    in the meantime. ``inject`` runs at most once.
    """
    real_add = session_store.add_session
    fired = {"done": False}

    async def _add(session_dict):
        await asyncio.sleep(0)
        if not fired["done"]:
            fired["done"] = True
            inject()
        return await real_add(session_dict)

    return _add


async def test_plug_flap_during_completion_completes_once(hass: HomeAssistant, freezer) -> None:
    """FR-008/FR-010: plug off→unavailable→off flapping landing during the
    completion's first await → exactly one stored session, one event."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]
    events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    def _flap() -> None:
        # The completion is parked at its persist await; the plug entity flaps
        # off→unavailable→off (cable_lock already reads Unlocked), re-entering
        # the plug-off handler mid-completion.
        engine._handle_plug_change("unavailable")
        engine._handle_plug_change("off")

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=6.0)
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        session_id = engine.active_session.id

        with patch.object(
            session_store,
            "add_session",
            side_effect=_make_add_session_with_injection(session_store, _flap),
        ):
            # Clean unplug → completion task starts and suspends at the persist.
            hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
            await hass.async_block_till_done()
            hass.states.async_set(MOCK_PLUG_ENTITY, "off")
            await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE
    assert engine.active_session is None
    assert len(session_store.sessions) == 1, (
        f"plug flapping during completion must store exactly one session, "
        f"got {len(session_store.sessions)}"
    )
    assert session_store.sessions[0]["id"] == session_id
    assert len(events) == 1, f"exactly one EVENT_SESSION_COMPLETED expected, got {len(events)}"


async def test_late_unlock_after_grace_expiry_no_second_completion(
    hass: HomeAssistant, freezer
) -> None:
    """FR-008/FR-009: cable_lock→Unlocked arriving after grace expiry (state
    COMPLETING, completion in flight) must not run a second completion.

    The injection also restores a stale (already-fired) grace handle before
    the confirmation runs — the exact FR-009 enabler: a stale handle makes the
    'grace pending' guard lie, so only the FR-008 state guard stands between
    the late Unlocked and a duplicate completion.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]
    events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    def _late_unlock() -> None:
        engine._disconnect_grace_cancel = lambda: None  # stale fired handle
        engine._handle_cable_lock_confirmation("Unlocked")

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=6.0)
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        session_id = engine.active_session.id

        await _transient_plug_off(hass)  # grace timer armed (cable still Locked)
        assert engine._disconnect_grace_cancel is not None

        with patch.object(
            session_store,
            "add_session",
            side_effect=_make_add_session_with_injection(session_store, _late_unlock),
        ):
            # Grace expires → force-end completion task starts (suspends at the
            # persist); the lagging Unlocked lands inside that window.
            freezer.tick(timedelta(minutes=GRACE_TIMEOUT_MIN + 1))
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE
    assert len(session_store.sessions) == 1, (
        f"late Unlocked after grace expiry must not double-complete, "
        f"got {len(session_store.sessions)} sessions"
    )
    assert session_store.sessions[0]["id"] == session_id
    assert len(events) == 1, f"exactly one completion event expected, got {len(events)}"


async def test_grace_expiry_and_plug_trigger_race_one_completion(
    hass: HomeAssistant, freezer
) -> None:
    """FR-008/FR-009/FR-010: grace-expiry and a plug-event completion trigger
    racing in the same window → exactly one completion."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]
    events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    def _plug_trigger() -> None:
        # While the grace-expiry completion is in flight, the charger's signals
        # settle: cable_lock now reads Unlocked and the plug bounces
        # unavailable→off — the synchronous plug-off path would see
        # cable_lock == Unlocked and fire a second completion.
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        engine._handle_plug_change("unavailable")
        engine._handle_plug_change("off")

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=6.0)
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        session_id = engine.active_session.id

        await _transient_plug_off(hass)  # grace timer armed

        with patch.object(
            session_store,
            "add_session",
            side_effect=_make_add_session_with_injection(session_store, _plug_trigger),
        ):
            # Grace expires → completion task in flight (suspended at persist);
            # the plug trigger lands inside that window.
            freezer.tick(timedelta(minutes=GRACE_TIMEOUT_MIN + 1))
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE
    assert len(session_store.sessions) == 1, (
        f"racing grace/plug completion triggers must collapse to one completion, "
        f"got {len(session_store.sessions)} sessions"
    )
    assert session_store.sessions[0]["id"] == session_id
    assert len(events) == 1, f"exactly one completion event expected, got {len(events)}"


async def test_fired_timers_clear_their_handles(hass: HomeAssistant, freezer) -> None:
    """FR-009: fired grace and idle timers null their cancel handles, so
    'timer pending' checks reflect reality."""
    # --- idle timer ---
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=6.0)
        # Power drops to 0 → idle timer armed.
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()
        assert engine._idle_timer_cancel is not None, "idle timer must be armed"

        # Idle timeout fires → window closes AND the handle must be cleared.
        freezer.tick(timedelta(minutes=6))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert not engine.window_tracker.is_open(), "idle expiry must close the window"
        assert engine._idle_timer_cancel is None, (
            "FR-009: a fired idle timer must clear its cancel handle"
        )

        # --- grace timer ---
        await _transient_plug_off(hass)
        assert engine._disconnect_grace_cancel is not None, "grace timer must be armed"

        freezer.tick(timedelta(minutes=GRACE_TIMEOUT_MIN + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        # Assert BEFORE settling the loop: the handle must be nulled by the
        # fired callback itself, not by the completion task's cleanup.
        assert engine._disconnect_grace_cancel is None, (
            "FR-009: a fired grace timer must clear its cancel handle immediately"
        )
        await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
