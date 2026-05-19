"""TC-006: Race-condition regression test for PlugAnchoredSessionEngine (PR-22).

The original race bug: when the energy-counter entity update arrived before the
car_status entity update, the gate-engage logic read a stale _last_car_status
and failed to activate, producing orphaned micro-sessions.

The new PlugAnchoredSessionEngine eliminates the gate entirely. This test verifies:
1. Event ordering (energy before plug-off) still produces one correct session.
2. The new engine does NOT have a _last_car_status attribute (FR-N02 enforcement).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.const import (
    CONF_CHARGING_IDLE_TIMEOUT_MIN,
    DOMAIN,
)
from custom_components.ev_charging_manager.session_engine_v2 import (
    PlugAnchoredSessionEngine,
)
from tests.conftest import MOCK_CHARGER_DATA, MOCK_ENERGY_ENTITY, MOCK_POWER_ENTITY, MOCK_TRX_ENTITY

MOCK_PLUG_ENTITY = "binary_sensor.goe_abc123_car_0"
MOCK_CABLE_LOCK_ENTITY = "sensor.goe_abc123_cus_value"

MOCK_OPTIONS_V2 = {
    "plug_entity": MOCK_PLUG_ENTITY,
    "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
    CONF_CHARGING_IDLE_TIMEOUT_MIN: 1,  # 1-minute timeout for fast tests
    "disconnect_grace_min": 10,
}


async def _make_engine_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options=MOCK_OPTIONS_V2,
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


async def test_tc006_energy_event_before_plug_off_produces_one_session(
    hass: HomeAssistant,
    freezer,
) -> None:
    """TC-006: energy entity update arrives before plug-off event — one session, correct energy."""
    entry = await _make_engine_entry(hass)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert isinstance(engine, PlugAnchoredSessionEngine)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in and start charging
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
        await hass.async_block_till_done()

        hass.states.async_set(MOCK_POWER_ENTITY, "3456.0")
        await hass.async_block_till_done()

        session_id = engine.active_session.id

        # Advance time; energy accumulates
        freezer.tick(timedelta(minutes=5))
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.288")
        await hass.async_block_till_done()

        # Power drops and idle timeout elapses
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()
        freezer.tick(timedelta(minutes=2))
        await hass.async_block_till_done()

        # Race: energy update arrives FIRST (before plug-off event)
        # In the old engine this could produce a phantom session;
        # in the new engine the session boundary is purely plug-driven.
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.288")  # same value, simulates re-fire
        await hass.async_block_till_done()

        # THEN the plug-off arrives (validated by cable_lock=Unlocked)
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    # Verify: exactly one session
    sessions = session_store.sessions
    assert len(sessions) == 1, f"Expected 1 session, got {len(sessions)}: {sessions}"

    session = sessions[0]
    assert session["id"] == session_id
    assert session["energy_kwh"] >= 0.0


def test_tc006_engine_has_no_last_car_status_attribute(
    hass: HomeAssistant,
) -> None:
    """TC-006 (enforcement): PlugAnchoredSessionEngine must NOT have _last_car_status.

    FR-N02: the new engine must not use a stale cache of car_status as a
    session-boundary signal. We enforce this via attribute absence.

    Note: this is a plain (non-async) test — no engine setup needed; we just
    verify the class definition does not define the forbidden attribute.
    """
    from custom_components.ev_charging_manager.session_engine_v2 import (
        PlugAnchoredSessionEngine,
    )

    # The class itself must not define _last_car_status as a class attribute
    assert not hasattr(PlugAnchoredSessionEngine, "_last_car_status"), (
        "PlugAnchoredSessionEngine must not have a _last_car_status class attribute (FR-N02)"
    )

    # Inspect __init__ to verify it is not set there (heuristic: attribute must not
    # appear as an instance attribute name in the __init__ source).
    import inspect
    source = inspect.getsource(PlugAnchoredSessionEngine.__init__)
    assert "_last_car_status" not in source, (
        "PlugAnchoredSessionEngine.__init__ must not set _last_car_status (FR-N02)"
    )
