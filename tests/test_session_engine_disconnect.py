"""TC-009, TC-010, TC-011: Transient disconnect handling tests (PR-22 Phase 7).

TC-009: plug=off + cable_lock=unknown → session continues, data_gap=true set.
TC-010: plug=off + cable_lock=Unlocked → session ends normally.
TC-011: plug=off persisting for disconnect_grace_min → session force-ended.
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
            CONF_RFID_GRACE_SECONDS: 0,  # opt out: disconnect behavior tests
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
