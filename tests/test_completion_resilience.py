"""Review F1 (023-recovery-hardening): completion must not strand sessions.

(a) Degradation (primary): a failure in any exception-prone pre-persist
    metrics step — spot final-hour finalize, guest charge price, ETO
    cross-validation — logs a WARNING, sets data_gap=True, leaves that metric
    at a safe value, and the completion ALWAYS proceeds to persist + event.

(b) Retry backstop: a truly unexpected pre-persist failure (e.g. inside
    _close_window) restores the engine's claim on the session, undoes the
    attempt's speculative mutations (disconnected_at/ended_at, speculative
    spot final-hour entry + hourly re-arm) and arms a one-shot 30 s retry.
    A new physical plug-in while the retry is pending brings the completion
    forward instead of being silently merged into the dead session
    (the Petra/Paul cross-user-merge incident class).
"""

from __future__ import annotations

from datetime import timedelta
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
    DOMAIN,
    EVENT_SESSION_COMPLETED,
    SessionEngineState,
)
from tests.conftest import (
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    MOCK_POWER_ENTITY,
    MOCK_TRX_ENTITY,
)

MOCK_PLUG_ENTITY = "binary_sensor.goe_abc123_car_0"
MOCK_CABLE_LOCK_ENTITY = "sensor.goe_abc123_cus_value"
MOCK_SPOT_ENTITY = "sensor.fake_spot_price"


@pytest.fixture(autouse=True)
def _pin_clock_mid_hour(freezer) -> None:
    """Pin the frozen clock mid-hour (UTC) before engine setup.

    Without this, freezegun freezes at the REAL current time, and the
    ticks below (5 min + 31 s) can cross a real UTC hour boundary —
    firing the engine's hourly spot callback (async_track_utc_time_change
    minute=0) and appending a legitimate hourly price_details entry that
    breaks the speculative-entry assertions. Mid-hour, no tick in this
    module can reach an hour boundary."""
    freezer.move_to("2026-06-12T12:30:00+00:00")


async def _make_entry(hass: HomeAssistant, *, spot: bool = False) -> MockConfigEntry:
    """Set up a goe_gemini (plug-anchored) entry, optionally in spot mode."""
    data = {**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"}
    if spot:
        data |= {
            "pricing_mode": "spot",
            "spot_price_entity": MOCK_SPOT_ENTITY,
            "spot_additional_cost_kwh": 0.85,
            "spot_vat_multiplier": 1.25,
            "spot_fallback_price_kwh": 2.50,
        }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data,
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
            CONF_CHARGING_IDLE_TIMEOUT_MIN: 5,
            CONF_DISCONNECT_GRACE_MIN: 10,
        },
        title="Test go-e Charger (completion resilience)",
    )

    hass.states.async_set(MOCK_PLUG_ENTITY, "off")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
    if spot:
        hass.states.async_set(MOCK_SPOT_ENTITY, "1.50")

    entry.add_to_hass(hass)
    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    return entry


async def _charge_session(hass: HomeAssistant, freezer, energy_kwh: str = "6.0") -> None:
    """Start a session (trx=2), charge energy_kwh, advance past the micro filter."""
    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_ENERGY_ENTITY, energy_kwh)
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=5))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def _unplug(hass: HomeAssistant) -> None:
    """Clean unplug: cable_lock=Unlocked first, then plug=off."""
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_PLUG_ENTITY, "off")
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# F1(a): degradation — completion always persists + fires its event
# ---------------------------------------------------------------------------


async def test_charge_price_failure_degrades_and_completes(hass: HomeAssistant, freezer) -> None:
    """F1(a)(i): _calculate_charge_price raising must NOT strand the session —
    persisted exactly once, one event, data_gap=True, engine IDLE."""
    entry = await _make_entry(hass)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _charge_session(hass, freezer)
        session_id = engine.active_session.id

        with patch.object(
            engine, "_calculate_charge_price", side_effect=RuntimeError("price boom")
        ):
            await _unplug(hass)

    assert engine.state == SessionEngineState.IDLE
    assert engine.active_session is None
    assert len(session_store.sessions) == 1, "session persisted exactly once"
    stored = session_store.sessions[0]
    assert stored["id"] == session_id
    assert stored["data_gap"] is True, "degraded metric must flag data_gap"
    assert len(completed_events) == 1, "exactly one EVENT_SESSION_COMPLETED"


async def test_spot_capture_failure_degrades_cost_and_completes(
    hass: HomeAssistant, freezer
) -> None:
    """F1(a)(ii): a spot price-read failure degrades the cost to the running
    estimate (not frozen-wrong / not zero) and the completion proceeds.

    PR-29 (FR-008) moved the spot read OUT of finalize: the final hour is now
    priced from the price CAPTURED at that hour's start, so a read failure can
    only surface at a CAPTURE point (session start / resume / hourly tick).
    This test simulates the failure at the session-start capture — the hour is
    priced from the fallback, data_gap is flagged, and because the running
    estimate is kept at that same captured (fallback) price, the persisted cost
    equals the running estimate rather than being replaced by a wrong value."""
    entry = await _make_entry(hass, spot=True)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # The spot read RAISES at the session-start capture point: _capture_hour_price
        # must degrade (fallback price + data_gap) instead of stranding the session.
        with patch.object(engine, "_read_spot_price", side_effect=RuntimeError("spot boom")):
            await _charge_session(hass, freezer)
            session_id = engine.active_session.id
            running_cost = engine.active_session.cost_total_kr
            assert running_cost > 0, "precondition: running spot cost accumulated (fallback)"
            assert engine.active_session.data_gap is True, (
                "capture-time read failure must flag data_gap immediately"
            )

            await _unplug(hass)

    assert engine.state == SessionEngineState.IDLE
    assert len(session_store.sessions) == 1, "session persisted exactly once"
    stored = session_store.sessions[0]
    assert stored["id"] == session_id
    assert stored["data_gap"] is True
    assert stored["cost_total_kr"] == pytest.approx(running_cost), (
        "cost must degrade to the running estimate, not be replaced by a wrong value"
    )
    assert len(completed_events) == 1


async def test_eto_read_failure_degrades_and_completes(hass: HomeAssistant, freezer) -> None:
    """F1(a)(iii): an ETO cross-validation read failure degrades (charger
    totals stay unset) and the completion proceeds."""
    entry = await _make_entry(hass)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _charge_session(hass, freezer)
        session_id = engine.active_session.id

        with patch.object(engine, "_get_eto", side_effect=RuntimeError("eto boom")):
            await _unplug(hass)

    assert engine.state == SessionEngineState.IDLE
    assert len(session_store.sessions) == 1
    stored = session_store.sessions[0]
    assert stored["id"] == session_id
    assert stored["data_gap"] is True
    assert stored["charger_total_after_kwh"] is None, "failed read leaves the metric unset"
    assert len(completed_events) == 1


# ---------------------------------------------------------------------------
# F1(b): retry backstop for truly unexpected pre-persist failures
# ---------------------------------------------------------------------------


def _flaky_close_window(engine):
    """Return a _close_window replacement that raises on the FIRST call only."""
    original = engine._close_window
    calls = {"n": 0}

    def flaky(now, session=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom in _close_window")
        return original(now, session)

    return flaky


async def test_close_window_failure_retries_and_completes_once(
    hass: HomeAssistant, freezer
) -> None:
    """F1(b)(iii): _close_window raising once → claim restored (undo applied),
    the 30 s retry fires, session persisted exactly once, no duplicate final
    price_details entry, engine ends IDLE."""
    entry = await _make_entry(hass, spot=True)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _charge_session(hass, freezer)
        session_id = engine.active_session.id

        with patch.object(engine, "_close_window", side_effect=_flaky_close_window(engine)):
            await _unplug(hass)

            # Restore happened: the engine still owns the session…
            assert engine.state == SessionEngineState.TRACKING, "claim restored → TRACKING"
            assert engine.active_session is not None
            assert engine.active_session.id == session_id
            # …with the speculative completion mutations undone…
            assert engine.active_session.disconnected_at is None, (
                "undo must clear disconnected_at so the restored session is not "
                "silently filed as completed on the next restart"
            )
            assert engine.active_session.ended_at is None
            # …and a real retry armed.
            assert engine._completion_retry_unsub is not None, "30 s retry must be armed"
            assert len(session_store.sessions) == 0
            assert len(completed_events) == 0

            # Retry fires after 30 s and completes the session.
            freezer.tick(timedelta(seconds=31))
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE
    assert engine.active_session is None
    assert len(session_store.sessions) == 1, "session persisted exactly once"
    stored = session_store.sessions[0]
    assert stored["id"] == session_id
    assert len(completed_events) == 1, "exactly one EVENT_SESSION_COMPLETED"
    # The failed attempt never reached the spot finalize; the successful retry
    # appends exactly one final-hour entry.
    assert len(stored["price_details"]) == 1, "no duplicate final price_details entry"


class _BoomOnSessionStopLogger:
    """Debug-logger stub that raises on the FIRST SESSION_STOP line only.

    The SESSION_STOP micro-filter log line is the last pre-persist step that
    runs AFTER the spot final-hour finalize — raising there exercises the
    undo path for the speculative spot entry (pop + hourly re-arm)."""

    def __init__(self) -> None:
        self.enabled = True
        self.boomed = False

    def log(self, category: str, message: str) -> None:
        if category == "SESSION_STOP" and not self.boomed:
            self.boomed = True
            raise RuntimeError("boom after spot finalize")


async def test_failure_after_spot_finalize_pops_speculative_entry(
    hass: HomeAssistant, freezer
) -> None:
    """F1(b): a failure AFTER the spot final-hour append must pop the
    speculative entry and re-arm the hourly callback so the retried
    completion finalizes spot pricing exactly once (no duplicate entry)."""
    entry = await _make_entry(hass, spot=True)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _charge_session(hass, freezer)
        session_id = engine.active_session.id

        engine._debug_logger = _BoomOnSessionStopLogger()
        await _unplug(hass)

        # Restore happened; the speculative final-hour entry was popped.
        assert engine.state == SessionEngineState.TRACKING
        assert engine.active_session is not None
        assert engine.active_session.price_details == [], (
            "the speculative spot final-hour entry must be popped on undo"
        )
        assert engine._hourly_unsub is not None, (
            "the hourly callback must be re-armed so the retry can finalize spot"
        )

        # Retry completes.
        freezer.tick(timedelta(seconds=31))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE
    assert len(session_store.sessions) == 1
    stored = session_store.sessions[0]
    assert stored["id"] == session_id
    assert len(stored["price_details"]) == 1, "exactly one final-hour entry after retry"
    assert len(completed_events) == 1


async def test_new_plug_on_during_pending_retry_is_not_merged(hass: HomeAssistant, freezer) -> None:
    """F1(b)(iv): a NEW physical plug-in while a failed completion awaits
    retry must NOT be absorbed into the dead session (cross-user merge).
    The completion is brought forward; the plug-in starts a FRESH session."""
    entry = await _make_entry(hass)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _charge_session(hass, freezer)
        old_id = engine.active_session.id

        with patch.object(engine, "_close_window", side_effect=_flaky_close_window(engine)):
            await _unplug(hass)
            assert engine.state == SessionEngineState.TRACKING, "claim restored"
            assert engine._completion_retry_unsub is not None, "retry pending"

            # A new car arrives BEFORE the retry fires (different RFID).
            hass.states.async_set(MOCK_TRX_ENTITY, "3")
            await hass.async_block_till_done()
            hass.states.async_set(MOCK_PLUG_ENTITY, "on")
            hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
            await hass.async_block_till_done()

        # Exactly one completed OLD session…
        assert len(session_store.sessions) == 1, "old session completed exactly once"
        assert session_store.sessions[0]["id"] == old_id
        assert len(completed_events) == 1
        assert completed_events[0].data["session_id"] == old_id

        # …and the new plug-in runs as a FRESH session, not merged into the old one.
        assert engine.state == SessionEngineState.TRACKING
        assert engine.active_session is not None, "new plug-in must start a fresh session"
        assert engine.active_session.id != old_id, "new session must not reuse the dead session"
        assert engine._completion_retry_unsub is None, (
            "retry settled by the brought-forward completion"
        )
