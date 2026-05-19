"""TC-025, TC-026, TC-027: Micro-filter and edge case tests (PR-22 Phase 9).

TC-025: Plug in with no energy delivered (energy_kwh < 50 Wh) → session discarded.
TC-026: Fumble — two rapid plug-off/plug-on cycles → both sessions discarded by micro-filter.
TC-027: Energy sensor unavailable mid-session → data_gap=True, energy resumes on recovery.
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


async def _make_engine_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create and set up a config entry with PlugAnchoredSessionEngine."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
            CONF_CHARGING_IDLE_TIMEOUT_MIN: 5,
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
    assert isinstance(engine, PlugAnchoredSessionEngine)
    return engine


# ---------------------------------------------------------------------------
# TC-025: Micro-filter — no energy delivered → session discarded
# ---------------------------------------------------------------------------


async def test_tc025_micro_filter_no_energy(hass: HomeAssistant, freezer) -> None:
    """TC-025: Plug in, plug out immediately with < 50 Wh → session discarded."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        # Session in memory (not yet persisted)
        assert engine.active_session is not None, "TC-025: session should be created on plug-in"

        # A tiny amount of energy — below 50 Wh = 0.05 kWh threshold
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.01")
        await hass.async_block_till_done()

        # Advance enough time so connection_duration_s passes the duration threshold
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Unplug immediately
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    # Micro-filter should discard the session (< 50 Wh)
    assert len(session_store.sessions) == 0, (
        f"TC-025: micro-filter should discard session with < 50 Wh, "
        f"got {len(session_store.sessions)} sessions"
    )
    assert engine.active_session is None, "TC-025: no active session after micro-filter discard"


# ---------------------------------------------------------------------------
# TC-026: Fumble — two rapid plug cycles → both sessions discarded
# ---------------------------------------------------------------------------


async def test_tc026_fumble_two_short_sessions_discarded(hass: HomeAssistant, freezer) -> None:
    """TC-026: Rapid plug-off + plug-on → two micro-sessions both discarded.

    Also documents trx behavior: for very short plug-off durations, trx may
    not clear between cycles (hardware-dependent). Engine uses plug as primary
    boundary signal and does not depend on trx clearing.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # ---- First fumble: plug in for 3 seconds, then out ----
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        # Tiny energy (still below threshold)
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.005")
        await hass.async_block_till_done()

        # Short session (3 s — below minimum duration)
        freezer.tick(timedelta(seconds=3))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Unplug
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

        # First session discarded
        assert len(session_store.sessions) == 0, (
            "TC-026: first fumble session should be discarded by micro-filter"
        )

        # ---- Second fumble: immediately plug in again ----
        # Note: trx may not clear between fumbles (hardware behavior) — engine is agnostic
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.010")
        await hass.async_block_till_done()

        freezer.tick(timedelta(seconds=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    # Both fumble sessions discarded
    assert len(session_store.sessions) == 0, (
        f"TC-026: both fumble sessions should be discarded, "
        f"got {len(session_store.sessions)} sessions"
    )
    assert engine.active_session is None, "TC-026: no active session after fumble"


# ---------------------------------------------------------------------------
# TC-027: Energy sensor unavailable mid-session → data_gap, energy resumes
# ---------------------------------------------------------------------------


async def test_tc027_energy_sensor_unavailable_mid_session(hass: HomeAssistant, freezer) -> None:
    """TC-027: Energy sensor goes unavailable mid-session.

    Verifies:
    - Session continues (no force-end)
    - data_gap=True on the active session
    - Energy attribution resumes correctly when sensor comes back
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in and start charging
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
        hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
        await hass.async_block_till_done()

        assert engine.active_session is not None
        assert engine.active_session.data_gap is False, "TC-027: data_gap should start as False"

        # Advance time
        freezer.tick(timedelta(minutes=3))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # --- Energy sensor goes unavailable ---
        hass.states.async_set(MOCK_ENERGY_ENTITY, "unavailable")
        await hass.async_block_till_done()

        assert engine.active_session.data_gap is True, (
            "TC-027: data_gap must be True after energy sensor unavailability"
        )
        # Session must still be active
        assert engine.active_session is not None, (
            "TC-027: session must survive energy sensor going unavailable"
        )

        # --- Energy sensor recovers ---
        hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
        await hass.async_block_till_done()

        # Energy tracking resumes at the recovered value
        assert (
            abs(engine.active_session.energy_kwh - (10.0 - engine.active_session.energy_start_kwh))
            <= 0.01
        ), "TC-027: energy attribution should resume correctly after sensor recovery"
        # data_gap remains True (history gap cannot be undone)
        assert engine.active_session.data_gap is True, (
            "TC-027: data_gap must remain True after sensor recovery (gap cannot be undone)"
        )

        # Complete the session
        freezer.tick(timedelta(minutes=3))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    # Session should be stored with data_gap=True
    assert len(session_store.sessions) == 1, (
        f"TC-027: Expected 1 stored session, got {len(session_store.sessions)}"
    )
    assert session_store.sessions[0]["data_gap"] is True, (
        "TC-027: stored session must have data_gap=True"
    )
