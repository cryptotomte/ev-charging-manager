"""Tests for ChargingBinarySensor — US3 (PR-23).

T030: Failing scenarios before is_on predicate is updated:
  (a) session active + window open → is_on == True
  (b) session active + last window closed (sub-state 'charged') → is_on == False
  (c) no session → is_on == False
  (d) engine without get_status_sub_state (legacy fallback) + state == TRACKING → is_on == True
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ev_charging_manager.const import (
    CONF_CHARGING_IDLE_TIMEOUT_MIN,
    CONF_DISCONNECT_GRACE_MIN,
    CONF_HEARTBEAT_LOG_INTERVAL_MIN,
    CONF_UI_DISPATCH_INTERVAL_S,
    DOMAIN,
    SIGNAL_SESSION_UPDATE,
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

# Short idle timeout so charged-state tests complete quickly.
IDLE_TIMEOUT_MIN = 3


async def _make_engine_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create and set up a goe_gemini config entry with PlugAnchoredSessionEngine."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
            CONF_CHARGING_IDLE_TIMEOUT_MIN: IDLE_TIMEOUT_MIN,
            CONF_DISCONNECT_GRACE_MIN: 10,
            CONF_HEARTBEAT_LOG_INTERVAL_MIN: 0,
            CONF_UI_DISPATCH_INTERVAL_S: 0,
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
    assert isinstance(engine, PlugAnchoredSessionEngine), (
        f"Expected PlugAnchoredSessionEngine, got {type(engine).__name__}"
    )
    return engine


def _get_binary_sensor_state(hass: HomeAssistant, entry: MockConfigEntry) -> bool | None:
    """Return the is_on state of the charging binary sensor for the given entry.

    Looks up the entity by its unique_id (<entry_id>_charging) via the entity registry
    so the test is independent of the entry title slug.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    unique_id = f"{entry.entry_id}_charging"
    entity_entry = registry.async_get_entity_id("binary_sensor", DOMAIN, unique_id)
    if entity_entry is None:
        return None
    state = hass.states.get(entity_entry)
    if state is None:
        return None
    return state.state == "on"


# ---------------------------------------------------------------------------
# (a) Session active + charging window open → is_on == True
# ---------------------------------------------------------------------------


async def test_binary_sensor_on_when_window_open(hass: HomeAssistant, freezer) -> None:
    """(a) is_on must be True when a session has an open charging window."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in cable (session starts, sub-state = waiting)
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        # Power rises — window opens, sub-state = charging
        hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.5")
        await hass.async_block_till_done()

    assert engine.get_status_sub_state() == "charging", (
        f"Precondition: expected sub-state 'charging', got {engine.get_status_sub_state()!r}"
    )
    assert _get_binary_sensor_state(hass, entry) is True, (
        "is_on must be True when charging window is open"
    )


# ---------------------------------------------------------------------------
# (b) Session active + last window closed (sub-state 'charged') → is_on == False
# ---------------------------------------------------------------------------


async def test_binary_sensor_off_when_window_closed(hass: HomeAssistant, freezer) -> None:
    """(b) is_on must be False when session exists but last window has closed."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in + power rises (open window)
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "1.0")
        await hass.async_block_till_done()

        # Power drops to 0 then idle timeout fires → window closes, sub-state = charged
        hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()

        freezer.tick(timedelta(minutes=IDLE_TIMEOUT_MIN + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

    assert engine.get_status_sub_state() == "charged", (
        f"Precondition: expected sub-state 'charged', got {engine.get_status_sub_state()!r}"
    )
    # Cable still in, session still active — but window is closed
    assert engine.active_session is not None, "Precondition: session should still be active"
    assert _get_binary_sensor_state(hass, entry) is False, (
        "is_on must be False when charging window is closed (sub-state='charged')"
    )


# ---------------------------------------------------------------------------
# (c) No session → is_on == False
# ---------------------------------------------------------------------------


async def test_binary_sensor_off_when_no_session(hass: HomeAssistant) -> None:
    """(c) is_on must be False when there is no active session."""
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)

    # Engine is idle (no plug)
    assert engine.active_session is None, "Precondition: no active session expected"
    assert engine.get_status_sub_state() == "idle"

    assert _get_binary_sensor_state(hass, entry) is False, (
        "is_on must be False when no session is active"
    )


# ---------------------------------------------------------------------------
# (d) Engine without get_status_sub_state (legacy fallback) + state == TRACKING → is_on == True
# ---------------------------------------------------------------------------


async def test_binary_sensor_legacy_fallback_tracking_is_on(hass: HomeAssistant) -> None:
    """(d) Legacy fallback: engine without get_status_sub_state uses engine.state == TRACKING.

    Simulates a generic-profile engine (legacy SessionEngine) by injecting a
    mock that has no get_status_sub_state method but exposes .state == TRACKING.
    The sensor's dispatcher signal is sent after injection to trigger async_write_ha_state().
    """
    entry = await _make_engine_entry(hass)

    # Build a mock engine without get_status_sub_state (mimics legacy SessionEngine)
    legacy_engine = MagicMock(spec=[])  # spec=[] means NO attributes are pre-declared
    legacy_engine.state = SessionEngineState.TRACKING
    # Confirm hasattr returns False — no get_status_sub_state on this mock
    assert not hasattr(legacy_engine, "get_status_sub_state"), (
        "Test setup error: legacy mock must NOT have get_status_sub_state"
    )

    # Inject the mock engine then dispatch a signal to refresh published state
    hass.data[DOMAIN][entry.entry_id]["session_engine"] = legacy_engine
    async_dispatcher_send(hass, SIGNAL_SESSION_UPDATE.format(entry.entry_id))
    await hass.async_block_till_done()

    assert _get_binary_sensor_state(hass, entry) is True, (
        "Legacy fallback: is_on must be True when engine.state == TRACKING and "
        "get_status_sub_state is absent"
    )
