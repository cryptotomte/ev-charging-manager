"""TC-009, TC-010, TC-011: Transient disconnect handling tests (PR-22 Phase 7).

TC-009: plug=off + cable_lock=unknown → session continues, data_gap=true set.
TC-010: plug=off + cable_lock=Unlocked → session ends normally.
TC-011: plug=off persisting for disconnect_grace_min → session force-ended.

PR-27 (023-recovery-hardening) US5 adds: idle re-arm guard (FR-016), hourly
spot double-arm guard (FR-017), unload final snapshot (FR-019), and the
grace-vs-outage rule (FR-020).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.const import STATE_UNAVAILABLE
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


async def _plug_in_and_charge(hass: HomeAssistant, energy_kwh: float = 5.0) -> None:
    """Helper: plug in and start charging to create an active session."""
    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.5")
    hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_ENERGY_ENTITY, str(energy_kwh))
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# TC-009: Transient plug-off with non-Unlocked cable_lock → session continues
# ---------------------------------------------------------------------------


async def test_tc009_transient_plug_off_cable_locked(hass: HomeAssistant, freezer) -> None:
    """TC-009: plug=off + cable_lock=unknown/Locked → session survives, data_gap=True."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=3.0)

        # Advance some time so session passes micro-filter
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        session_id_before = engine.active_session.id
        assert engine.active_session is not None, "TC-009: session should be active"

        # --- Simulate transient disconnect: plug=off but cable_lock != Unlocked ---
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "unknown")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

        # Session must still be active (not completed)
        assert engine.active_session is not None, (
            "TC-009: session must survive transient plug-off when cable_lock != Unlocked"
        )
        assert engine.active_session.id == session_id_before, (
            "TC-009: must be the same session — not a new one"
        )
        # data_gap must be set
        assert engine.active_session.data_gap is True, (
            "TC-009: data_gap must be True after transient disconnect"
        )

        # No sessions committed yet
        assert len(session_store.sessions) == 0, (
            "TC-009: no sessions should be in store yet (session still active)"
        )

        # --- Plug returns → grace timer cancelled, session continues ---
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        await hass.async_block_till_done()

        assert engine.active_session is not None, "TC-009: session must survive after plug returns"

        # --- Normal unplug now ---
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    # Session should now be stored (1 session)
    assert len(session_store.sessions) == 1, (
        f"TC-009: Expected 1 session after normal unplug, got {len(session_store.sessions)}"
    )


# ---------------------------------------------------------------------------
# TC-010: Real unplug (cable_lock=Unlocked) → session ends normally
# ---------------------------------------------------------------------------


async def test_tc010_real_unplug_cable_unlocked(hass: HomeAssistant, freezer) -> None:
    """TC-010: plug=off + cable_lock=Unlocked → session ends immediately."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=8.0)

        # Advance time past micro-filter minimum
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert engine.active_session is not None, "TC-010: session should be active"

        # --- Real unplug: cable_lock becomes Unlocked first (normal sequence) ---
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    # Session must be stored
    assert len(session_store.sessions) == 1, (
        f"TC-010: Expected 1 session after real unplug, got {len(session_store.sessions)}"
    )
    assert engine.active_session is None, (
        "TC-010: active_session should be None after session completes"
    )
    assert engine.get_status_sub_state() == "idle", (
        f"TC-010: engine should be idle after unplug, got {engine.get_status_sub_state()!r}"
    )


# ---------------------------------------------------------------------------
# TC-011: Grace timeout force-ends session regardless of cable_lock
# ---------------------------------------------------------------------------


async def test_tc011_grace_timeout_force_ends_session(hass: HomeAssistant, freezer) -> None:
    """TC-011: plug=off persists for disconnect_grace_min → session force-ended."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=6.0)

        # Advance time to ensure session passes micro-filter
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert engine.active_session is not None, "TC-011: session should be active"

        # --- Transient disconnect: plug=off but cable NOT Unlocked ---
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

        # Session still active (grace timer started, not expired yet)
        assert engine.active_session is not None, (
            "TC-011: session should still be active immediately after transient plug-off"
        )
        assert len(session_store.sessions) == 0, (
            "TC-011: no session should be stored before grace timeout"
        )

        # --- Advance past grace timeout ---
        freezer.tick(timedelta(minutes=GRACE_TIMEOUT_MIN + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

    # Session must be force-ended and stored
    assert len(session_store.sessions) == 1, (
        f"TC-011: Expected 1 session after grace timeout, got {len(session_store.sessions)}"
    )
    assert engine.active_session is None, "TC-011: active_session should be None after force-end"
    # data_gap must remain True
    session = session_store.sessions[0]
    assert session["data_gap"] is True, "TC-011: data_gap must be True in the force-ended session"


# ===========================================================================
# PR-27 (023-recovery-hardening) US5: timer & lifecycle guards
# (FR-016, FR-017, FR-019, FR-020)
# ===========================================================================


async def test_repeated_zero_power_does_not_extend_idle_deadline(
    hass: HomeAssistant, freezer
) -> None:
    """FR-016: repeated zero-power events while a window is open must NOT
    re-arm the idle timer — it fires at its ORIGINAL deadline."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=5.0)
        assert engine.window_tracker.is_open(), "precondition: charging window open"

        # t0: power drops to zero → idle timer armed (idle timeout = 5 min).
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()
        assert engine._idle_timer_cancel is not None

        # t0+4min: another zero-power reading arrives (different state string,
        # same physical value). It must NOT push the deadline to t0+9min.
        freezer.tick(timedelta(minutes=4))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_POWER_ENTITY, "0")
        await hass.async_block_till_done()

        # t0+6min: the ORIGINAL deadline (t0+5min) has passed — the window
        # must be closed. With per-event re-arming it would still be open.
        freezer.tick(timedelta(minutes=2))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

    assert not engine.window_tracker.is_open(), (
        "FR-016: repeated zero-power events must not postpone the idle deadline — "
        "the window must close at the original t0+idle_timeout"
    )
    assert engine.active_session is not None, "session itself continues (charged sub-state)"


async def test_failed_session_start_does_not_double_arm_hourly(hass: HomeAssistant) -> None:
    """FR-017: a session start that fails AFTER arming the hourly spot callback
    cancels it; the next spot session has exactly ONE active hourly callback."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **MOCK_CHARGER_DATA,
            "charger_profile": "goe_gemini",
            "pricing_mode": "spot",
            "spot_price_entity": "sensor.fake_spot_price",
            "spot_additional_cost_kwh": 0.85,
            "spot_vat_multiplier": 1.25,
            "spot_fallback_price_kwh": 2.50,
        },
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
            CONF_CHARGING_IDLE_TIMEOUT_MIN: 5,
            CONF_DISCONNECT_GRACE_MIN: GRACE_TIMEOUT_MIN,
        },
        title="Test go-e Charger (spot)",
    )
    hass.states.async_set(MOCK_PLUG_ENTITY, "off")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
    hass.states.async_set("sensor.fake_spot_price", "1.50")

    # Count live hourly registrations: each fake registration returns a cancel
    # that removes itself — len(active) == callbacks currently armed.
    active_hourly: list[object] = []

    def _fake_track(*_args, **_kwargs):
        handle = object()
        active_hourly.append(handle)

        def _cancel() -> None:
            if handle in active_hourly:
                active_hourly.remove(handle)

        return _cancel

    with (
        patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock),
        patch(
            "custom_components.ev_charging_manager.session_engine_v2.async_track_utc_time_change",
            side_effect=_fake_track,
        ),
        patch(
            "custom_components.ev_charging_manager.session_engine_v2."
            "persistent_notification.async_create"
        ),
    ):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        engine = _get_engine(hass, entry)

        # First session start FAILS after the spot block armed the hourly
        # callback (the unknown-RFID handler runs after spot init and raises).
        with patch.object(
            engine,
            "_async_handle_unknown_rfid",
            side_effect=RuntimeError("boom after hourly arm"),
        ):
            hass.states.async_set(MOCK_TRX_ENTITY, "5")  # unmapped → handler runs
            await hass.async_block_till_done()
            hass.states.async_set(MOCK_PLUG_ENTITY, "on")
            hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
            await hass.async_block_till_done()

        assert engine.active_session is None, "precondition: the first start failed"
        assert engine.state == SessionEngineState.IDLE
        assert len(active_hourly) == 0, (
            "FR-017: a failed session start must cancel the hourly callback it armed, "
            f"got {len(active_hourly)} still active"
        )

        # Next spot session starts normally → exactly ONE active hourly callback.
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        await hass.async_block_till_done()

        assert engine.active_session is not None, "second session must start"
        assert len(active_hourly) == 1, (
            f"FR-017: exactly one hourly callback may be active, got {len(active_hourly)}"
        )


async def test_unload_with_active_session_writes_final_snapshot(hass: HomeAssistant) -> None:
    """FR-019: unload with an active session persists one final snapshot."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=5.0)
        session_id = engine.active_session.id

        save_spy = AsyncMock(wraps=session_store.async_save_active_session)
        with patch.object(session_store, "async_save_active_session", save_spy):
            await engine.async_unload()

    assert save_spy.await_count == 1, (
        "FR-019: unload with an active session must write exactly one final snapshot"
    )
    assert save_spy.await_args.args[0]["id"] == session_id, (
        "the final snapshot must be the active session"
    )


async def test_unload_after_completion_does_not_resurrect(hass: HomeAssistant, freezer) -> None:
    """FR-019 × FR-011: unload right after a completion must not write an
    incomplete snapshot of the just-completed session."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=5.0)
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Clean unplug → session completes.
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()
        assert engine.active_session is None
        assert len(session_store.sessions) == 1

        save_spy = AsyncMock(wraps=session_store.async_save_active_session)
        with patch.object(session_store, "async_save_active_session", save_spy):
            await engine.async_unload()

    assert save_spy.await_count == 0, (
        "unload with no active session must not write any snapshot "
        "(a write here would resurrect the completed session)"
    )
    assert len(session_store.sessions) == 1


async def test_grace_suppressed_during_outage_and_rearmed_on_resolution(
    hass: HomeAssistant, freezer
) -> None:
    """FR-020: a pending grace must not force-end the session during a full
    charger outage; at outage resolution a FRESH full grace is armed and the
    normal triggers (here: cable-lock unlock confirmation) decide."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _plug_in_and_charge(hass, energy_kwh=6.0)
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        session_id = engine.active_session.id

        # Transient disconnect → grace armed (cable stays Locked).
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()
        assert engine._disconnect_grace_cancel is not None, "grace must be pending"

        # Full charger outage: power, energy, then plug go unavailable.
        hass.states.async_set(MOCK_POWER_ENTITY, STATE_UNAVAILABLE)
        hass.states.async_set(MOCK_ENERGY_ENTITY, STATE_UNAVAILABLE)
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, STATE_UNAVAILABLE)
        await hass.async_block_till_done()
        assert engine._charger_offline is True, "precondition: charger detected offline"

        # Grace expires mid-outage → must NOT complete the session.
        freezer.tick(timedelta(minutes=GRACE_TIMEOUT_MIN + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert engine.active_session is not None, (
            "FR-020: grace expiry during a full outage must not force-end the session"
        )
        assert engine.active_session.id == session_id
        assert len(session_store.sessions) == 0, "no completion may land mid-outage"

        # Outage resolves: the plug entity returns with 'off' (charger boot
        # sequence) → a FRESH full grace is armed; the plug=off itself must not
        # complete anything.
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

        assert engine._charger_offline is False, "outage must be resolved"
        assert engine.active_session is not None, (
            "FR-020: no immediate completion on the boot-sequence plug=off"
        )
        assert engine._disconnect_grace_cancel is not None, (
            "FR-020: a FRESH full grace must be armed at outage resolution"
        )

        # Normal trigger decides: the cable-lock unlock confirmation completes.
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()

    assert engine.active_session is None, "unlock confirmation must complete the session"
    assert engine.state == SessionEngineState.IDLE
    assert len(session_store.sessions) == 1
    assert session_store.sessions[0]["id"] == session_id
