"""TC-020, TC-021: Charger outage detection tests (PR-22 Phase 9 / FR-028).

TC-020: All charger entities go unavailable for longer than disconnect_grace_min
        → session NOT force-ended (no positive plug-off signal exists).

TC-021: Recovery from all-unavailable:
  TC-021a: cable still plugged (plug=on) at recovery → session continues.
  TC-021b: cable removed (plug=off + cable_lock=Unlocked) at recovery → session ends.
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

GRACE_MIN = 5  # short grace for test speed


async def _make_engine_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create and set up a config entry with PlugAnchoredSessionEngine."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
            CONF_CHARGING_IDLE_TIMEOUT_MIN: 5,
            CONF_DISCONNECT_GRACE_MIN: GRACE_MIN,
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


# ---------------------------------------------------------------------------
# TC-020: All entities unavailable → session NOT force-ended
# ---------------------------------------------------------------------------


async def test_tc020_charger_outage_no_grace_timer(hass: HomeAssistant, freezer) -> None:
    """TC-020: Charger goes fully offline → session survives beyond disconnect_grace_min.

    FR-028: simultaneous unavailability of all entities is NOT a positive plug-off
    signal. The grace timer must NOT start. Session must remain active.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in and start charging
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_ENERGY_ENTITY, "2.0")
        hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
        await hass.async_block_till_done()

        # Advance time so session passes micro-filter
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        session_id = engine.active_session.id

        # --- Charger goes offline: all entities → unavailable ---
        hass.states.async_set(MOCK_PLUG_ENTITY, "unavailable")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_POWER_ENTITY, "unavailable")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_ENERGY_ENTITY, "unavailable")
        await hass.async_block_till_done()

        # Advance well past disconnect_grace_min (FR-028 asserts NO force-end)
        freezer.tick(timedelta(minutes=GRACE_MIN * 3))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Session must still be active — no grace-timeout force-end
        assert engine.state == SessionEngineState.TRACKING, (
            f"TC-020: session must survive charger outage > grace_min, got state={engine.state}"
        )
        assert engine.active_session is not None, "TC-020: active_session must not be None"
        assert engine.active_session.id == session_id, "TC-020: same session must persist"

        # No sessions stored yet (session still active)
        assert len(session_store.sessions) == 0, (
            "TC-020: no session should be committed during charger outage"
        )


# ---------------------------------------------------------------------------
# TC-021a: Recovery from all-unavailable — cable still in
# ---------------------------------------------------------------------------


async def test_tc021a_outage_recovery_cable_in(hass: HomeAssistant, freezer) -> None:
    """TC-021a: Charger recovers from offline with cable still plugged → session continues."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in and charge
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_ENERGY_ENTITY, "3.0")
        hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
        await hass.async_block_till_done()

        freezer.tick(timedelta(minutes=3))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        session_id = engine.active_session.id

        # Charger goes offline
        hass.states.async_set(MOCK_PLUG_ENTITY, "unavailable")
        hass.states.async_set(MOCK_POWER_ENTITY, "unavailable")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "unavailable")
        await hass.async_block_till_done()

        freezer.tick(timedelta(minutes=GRACE_MIN + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Charger comes back online — cable still in
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
        hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
        await hass.async_block_till_done()

    # Session should still be active (same session)
    assert engine.state == SessionEngineState.TRACKING, (
        f"TC-021a: expected TRACKING after outage recovery, got {engine.state}"
    )
    assert engine.active_session is not None
    assert engine.active_session.id == session_id, "TC-021a: same session must continue"


# ---------------------------------------------------------------------------
# TC-021b: Recovery from all-unavailable — cable removed during outage
# ---------------------------------------------------------------------------


async def test_tc021b_outage_recovery_cable_out(hass: HomeAssistant, freezer) -> None:
    """TC-021b: Charger recovers from offline with cable removed → session ends."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in and charge
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_ENERGY_ENTITY, "4.0")
        hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
        await hass.async_block_till_done()

        freezer.tick(timedelta(minutes=3))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Charger goes offline
        hass.states.async_set(MOCK_PLUG_ENTITY, "unavailable")
        hass.states.async_set(MOCK_POWER_ENTITY, "unavailable")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "unavailable")
        await hass.async_block_till_done()

        freezer.tick(timedelta(minutes=GRACE_MIN + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Charger comes back — cable has been removed
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "4.0")
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()

    # Session should end when plug=off + cable_lock=Unlocked
    assert engine.state == SessionEngineState.IDLE or (len(session_store.sessions) == 1), (
        f"TC-021b: expected session to complete after outage recovery + unplug, "
        f"state={engine.state} sessions={len(session_store.sessions)}"
    )
