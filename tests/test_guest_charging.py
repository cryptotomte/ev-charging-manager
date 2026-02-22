"""Tests for guest charging: charge price calculation, real-time sensor, guest last sensor.

Covers:
- T007 (US1): Charge price calculation at session end
- T010 (US2): Real-time charge price sensor during guest sessions
- T013 (US3): Guest last charge price sensor persistence
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.ev_charging_manager.const import DOMAIN, EVENT_SESSION_COMPLETED
from custom_components.ev_charging_manager.stats_engine import GuestLastSession, StatsEngine
from custom_components.ev_charging_manager.stats_store import StatsStore
from tests.conftest import (
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    setup_session_engine,
    start_charging_session,
    stop_charging_session,
)

# Session options that bypass micro-session filter — ensures all sessions are persisted
_NO_MICRO = {"min_session_duration_s": 0, "min_session_energy_wh": 0}

# Spot pricing charger config
MOCK_SPOT_PRICE_ENTITY = "sensor.nordpool_kwh"
MOCK_SPOT_CHARGER_DATA = {
    **MOCK_CHARGER_DATA,
    "pricing_mode": "spot",
    "spot_price_entity": MOCK_SPOT_PRICE_ENTITY,
    "spot_additional_cost_kwh": 0.85,
    "spot_vat_multiplier": 1.25,
    "spot_fallback_price_kwh": 2.50,
}

# trx=3 → rfid_index=2 → card_index=2 (guest card)
GUEST_TRX = "3"
# trx=1 → rfid_index=0 → card_index=0 (regular user card)
REGULAR_TRX = "1"


# ---------------------------------------------------------------------------
# Helpers: inject config store data directly
# ---------------------------------------------------------------------------


def _inject_guest_fixed(hass: HomeAssistant, entry_id: str) -> None:
    """Inject fixed-price guest user + RFID mapping into ConfigStore.

    card_index=2, trx=3 → Gäst-Erik, 4.50 kr/kWh.
    """
    config_store = hass.data[DOMAIN][entry_id]["config_store"]
    config_store._data["users"] = [
        {
            "id": "guest-erik",
            "name": "Gäst-Erik",
            "type": "guest",
            "active": True,
            "guest_pricing": {"method": "fixed", "price_per_kwh": 4.50},
        }
    ]
    config_store._data["rfid_mappings"] = [
        {"card_index": 2, "user_id": "guest-erik", "active": True}
    ]


def _inject_guest_markup(hass: HomeAssistant, entry_id: str) -> None:
    """Inject markup guest user + RFID mapping into ConfigStore.

    card_index=2, trx=3 → Gäst-Anna, 1.8× markup.
    """
    config_store = hass.data[DOMAIN][entry_id]["config_store"]
    config_store._data["users"] = [
        {
            "id": "guest-anna",
            "name": "Gäst-Anna",
            "type": "guest",
            "active": True,
            "guest_pricing": {"method": "markup", "markup_factor": 1.8},
        }
    ]
    config_store._data["rfid_mappings"] = [
        {"card_index": 2, "user_id": "guest-anna", "active": True}
    ]


def _inject_regular_user(hass: HomeAssistant, entry_id: str) -> None:
    """Inject regular user + RFID mapping into ConfigStore.

    card_index=0, trx=1 → Paul.
    """
    config_store = hass.data[DOMAIN][entry_id]["config_store"]
    config_store._data["users"] = [
        {"id": "user-paul", "name": "Paul", "type": "regular", "active": True}
    ]
    config_store._data["rfid_mappings"] = [
        {"card_index": 0, "user_id": "user-paul", "active": True}
    ]


def _entity_state(hass: HomeAssistant, entity_id: str) -> str:
    """Return the current HA state string for an entity."""
    state = hass.states.get(entity_id)
    if state is None:
        return "MISSING"
    return state.state


# ---------------------------------------------------------------------------
# StatsEngine helper (for unit-level US3 sensor tests)
# ---------------------------------------------------------------------------


async def _setup_stats_engine(
    hass: HomeAssistant,
) -> tuple[StatsEngine, StatsStore, MockConfigEntry]:
    """Create and set up a StatsEngine with mocked storage."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"charger_name": "Test Charger"},
        title="Test Charger",
    )
    entry.add_to_hass(hass)
    store = StatsStore(hass)
    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=None):
        with patch.object(store._store, "async_save", new_callable=AsyncMock):
            engine = StatsEngine(hass, entry, store)
            await engine.async_setup()
            hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})["stats_engine"] = engine
            return engine, store, entry


# ---------------------------------------------------------------------------
# T007 (US1): Charge price calculation at session end
# ---------------------------------------------------------------------------


async def test_fixed_price_guest_charge_price_in_event(hass: HomeAssistant) -> None:
    """Fixed-price guest: 32.1 kWh × 4.50 kr/kWh = 144.45 kr in completed event.

    Also verifies cost_kr = 80.25 (static price 2.50 kr/kWh × 32.1 kWh).
    """
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=_NO_MICRO, title="My go-e Charger"
    )
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)
    await setup_session_engine(hass, entry)

    _inject_guest_fixed(hass, entry.entry_id)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value=GUEST_TRX)

    # Consume 32.1 kWh
    hass.states.async_set(MOCK_ENERGY_ENTITY, "32.1")
    await hass.async_block_till_done()

    await stop_charging_session(hass)
    await hass.async_block_till_done()

    assert len(completed_events) == 1
    data = completed_events[0].data
    assert abs(data["energy_kwh"] - 32.1) < 0.01
    assert abs(data["cost_kr"] - 80.25) < 0.01  # 32.1 × 2.50
    assert data["charge_price_kr"] is not None
    assert abs(data["charge_price_kr"] - 144.45) < 0.01  # 32.1 × 4.50


async def test_markup_guest_charge_price_in_event(hass: HomeAssistant) -> None:
    """Markup guest: 80.25 kr × 1.8 = 144.45 kr charge price in completed event."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=_NO_MICRO, title="My go-e Charger"
    )
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)
    await setup_session_engine(hass, entry)

    _inject_guest_markup(hass, entry.entry_id)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value=GUEST_TRX)

    # Consume 32.1 kWh → owner cost = 32.1 × 2.50 = 80.25 kr
    hass.states.async_set(MOCK_ENERGY_ENTITY, "32.1")
    await hass.async_block_till_done()

    await stop_charging_session(hass)
    await hass.async_block_till_done()

    assert len(completed_events) == 1
    data = completed_events[0].data
    assert abs(data["cost_kr"] - 80.25) < 0.01
    assert data["charge_price_kr"] is not None
    assert abs(data["charge_price_kr"] - 144.45) < 0.01  # 80.25 × 1.8


async def test_markup_guest_with_spot_pricing(hass: HomeAssistant) -> None:
    """Markup guest with spot pricing: charge_price = spot_cost × markup_factor."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_SPOT_CHARGER_DATA,
        options=_NO_MICRO,
        title="My go-e Charger",
    )
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)
    hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, "0.89")
    await setup_session_engine(hass, entry)

    _inject_guest_markup(hass, entry.entry_id)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
    await start_charging_session(hass, trx_value=GUEST_TRX)

    # Consume 2.0 kWh → partial hour cost: 2.0 × (0.89 + 0.85) × 1.25 = 4.35 kr
    hass.states.async_set(MOCK_ENERGY_ENTITY, "12.0")
    await hass.async_block_till_done()

    await stop_charging_session(hass)
    await hass.async_block_till_done()

    assert len(completed_events) == 1
    data = completed_events[0].data
    assert data["charge_price_kr"] is not None
    # charge_price = cost_kr × 1.8; cost_kr comes from spot calculation
    assert abs(data["charge_price_kr"] - round(data["cost_kr"] * 1.8, 2)) < 0.001
    assert data["cost_method"] == "spot"


async def test_regular_user_charge_price_is_none(hass: HomeAssistant) -> None:
    """Regular user session: charge_price_kr = None in completed event."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=_NO_MICRO, title="My go-e Charger"
    )
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)
    await setup_session_engine(hass, entry)

    _inject_regular_user(hass, entry.entry_id)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value=REGULAR_TRX)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
    await hass.async_block_till_done()

    await stop_charging_session(hass)
    await hass.async_block_till_done()

    assert len(completed_events) == 1
    assert completed_events[0].data["charge_price_kr"] is None


async def test_unknown_user_trx_zero_charge_price_is_none(hass: HomeAssistant) -> None:
    """Unknown user (trx=0, no RFID): charge_price_kr = None in completed event."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=_NO_MICRO, title="My go-e Charger"
    )
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    # trx=0 → unknown user
    await start_charging_session(hass, trx_value="0")

    hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
    await hass.async_block_till_done()

    await stop_charging_session(hass)
    await hass.async_block_till_done()

    assert len(completed_events) == 1
    assert completed_events[0].data["charge_price_kr"] is None


async def test_session_object_has_charge_price_fields(hass: HomeAssistant) -> None:
    """After a guest session, session object has charge_price_total_kr and charge_price_method."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=_NO_MICRO, title="My go-e Charger"
    )
    await setup_session_engine(hass, entry)

    _inject_guest_fixed(hass, entry.entry_id)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value=GUEST_TRX)

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session
    assert session is not None
    # charge_price_method is set at session start
    assert session.charge_price_method == "fixed"

    # Consume energy — charge_price_total_kr updates in real-time
    hass.states.async_set(MOCK_ENERGY_ENTITY, "32.1")
    await hass.async_block_till_done()

    assert session.charge_price_total_kr is not None
    assert abs(session.charge_price_total_kr - 144.45) < 0.01


async def test_markup_zero_energy_charge_price_is_zero(hass: HomeAssistant) -> None:
    """Edge case: markup guest with 0 kWh → charge_price = 0.00."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=_NO_MICRO, title="My go-e Charger"
    )
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)
    await setup_session_engine(hass, entry)

    _inject_guest_markup(hass, entry.entry_id)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value=GUEST_TRX)

    # No energy consumed — stay at 0
    await stop_charging_session(hass)
    await hass.async_block_till_done()

    assert len(completed_events) == 1
    data = completed_events[0].data
    # charge_price = 0.0 × 1.8 = 0.00
    assert data["charge_price_kr"] == 0.0


async def test_guest_without_pricing_config_charge_price_is_none(hass: HomeAssistant) -> None:
    """Guest user without guest_pricing in ConfigStore → charge_price_kr = None."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=_NO_MICRO, title="My go-e Charger"
    )
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)
    await setup_session_engine(hass, entry)

    # Inject a guest user WITHOUT guest_pricing key (legacy data or misconfigured)
    config_store = hass.data[DOMAIN][entry.entry_id]["config_store"]
    config_store._data["users"] = [
        {"id": "guest-legacy", "name": "Gäst-Legacy", "type": "guest", "active": True}
    ]
    config_store._data["rfid_mappings"] = [
        {"card_index": 2, "user_id": "guest-legacy", "active": True}
    ]

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value=GUEST_TRX)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
    await hass.async_block_till_done()

    await stop_charging_session(hass)
    await hass.async_block_till_done()

    assert len(completed_events) == 1
    assert completed_events[0].data["charge_price_kr"] is None
    assert completed_events[0].data["user_type"] == "guest"


async def test_guest_pricing_cleared_after_session(hass: HomeAssistant) -> None:
    """Guest pricing snapshot is cleared after session ends (no bleed-over)."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=_NO_MICRO, title="My go-e Charger"
    )
    await setup_session_engine(hass, entry)

    _inject_guest_fixed(hass, entry.entry_id)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value=GUEST_TRX)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
    await hass.async_block_till_done()

    await stop_charging_session(hass)
    await hass.async_block_till_done()

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine._guest_pricing is None


# ---------------------------------------------------------------------------
# T010 (US2): Real-time charge price sensor during guest sessions
# ---------------------------------------------------------------------------


async def test_charge_price_sensor_fixed_guest_mid_session(hass: HomeAssistant) -> None:
    """Fixed guest mid-session: 10.0 kWh → charge_price sensor = 45.00 kr."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="My go-e Charger")
    await setup_session_engine(hass, entry)

    _inject_guest_fixed(hass, entry.entry_id)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value=GUEST_TRX)

    # Consume 10 kWh → 10.0 × 4.50 = 45.00 kr
    hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
    await hass.async_block_till_done()

    state = hass.states.get("sensor.my_go_e_charger_session_charge_price")
    assert state is not None
    assert state.state != STATE_UNAVAILABLE
    assert abs(float(state.state) - 45.0) < 0.01


async def test_charge_price_sensor_markup_guest_mid_session(hass: HomeAssistant) -> None:
    """Markup guest mid-session: running owner cost 25.00 kr × 1.8 = 45.00 kr."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="My go-e Charger")
    await setup_session_engine(hass, entry)

    _inject_guest_markup(hass, entry.entry_id)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value=GUEST_TRX)

    # Consume 10 kWh → owner cost = 10 × 2.50 = 25.00 → charge_price = 25.00 × 1.8 = 45.00
    hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
    await hass.async_block_till_done()

    state = hass.states.get("sensor.my_go_e_charger_session_charge_price")
    assert state is not None
    assert state.state != STATE_UNAVAILABLE
    assert abs(float(state.state) - 45.0) < 0.01


async def test_charge_price_sensor_unavailable_regular_user(hass: HomeAssistant) -> None:
    """Regular user session: charge_price sensor is unavailable."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="My go-e Charger")
    await setup_session_engine(hass, entry)

    _inject_regular_user(hass, entry.entry_id)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value=REGULAR_TRX)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
    await hass.async_block_till_done()

    state = hass.states.get("sensor.my_go_e_charger_session_charge_price")
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


async def test_charge_price_sensor_unavailable_when_idle(hass: HomeAssistant) -> None:
    """No active session: charge_price sensor is unavailable."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="My go-e Charger")
    await setup_session_engine(hass, entry)

    state = hass.states.get("sensor.my_go_e_charger_session_charge_price")
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


# ---------------------------------------------------------------------------
# T013 (US3): Guest last charge price sensor persistence
# ---------------------------------------------------------------------------


async def test_guest_last_charge_price_after_guest_session(hass: HomeAssistant) -> None:
    """After a guest session completes, guest_last_charge_price = 144.45 kr."""
    engine, store, entry = await _setup_stats_engine(hass)

    with patch.object(store._store, "async_save", new_callable=AsyncMock):
        hass.bus.async_fire(
            EVENT_SESSION_COMPLETED,
            {
                "user_name": "Gäst-Erik",
                "user_type": "guest",
                "energy_kwh": 32.1,
                "cost_kr": 80.25,
                "charge_price_kr": 144.45,
                "started_at": "2026-04-10T14:00:00+02:00",
                "ended_at": "2026-04-10T15:00:00+02:00",
            },
        )
        await hass.async_block_till_done()

    assert engine.guest_last is not None
    assert abs(engine.guest_last.charge_price_kr - 144.45) < 0.001


async def test_regular_session_after_guest_does_not_overwrite(hass: HomeAssistant) -> None:
    """Regular user session does NOT overwrite guest_last_charge_price."""
    engine, store, entry = await _setup_stats_engine(hass)

    # Set initial guest_last with charge price
    engine._guest_last = GuestLastSession(
        energy_kwh=32.1,
        charge_price_kr=144.45,
        session_at="2026-04-10T15:00:00+02:00",
    )

    with patch.object(store._store, "async_save", new_callable=AsyncMock):
        hass.bus.async_fire(
            EVENT_SESSION_COMPLETED,
            {
                "user_name": "Paul",
                "user_type": "regular",
                "energy_kwh": 10.0,
                "cost_kr": 25.0,
                "charge_price_kr": None,
                "started_at": "2026-04-11T10:00:00+02:00",
                "ended_at": "2026-04-11T11:00:00+02:00",
            },
        )
        await hass.async_block_till_done()

    # Guest last unchanged
    assert engine.guest_last is not None
    assert abs(engine.guest_last.charge_price_kr - 144.45) < 0.001
    assert abs(engine.guest_last.energy_kwh - 32.1) < 0.001


async def test_new_guest_session_overwrites_charge_price(hass: HomeAssistant) -> None:
    """Second guest session overwrites guest_last_charge_price with new value."""
    engine, store, entry = await _setup_stats_engine(hass)

    with patch.object(store._store, "async_save", new_callable=AsyncMock):
        # First guest session
        hass.bus.async_fire(
            EVENT_SESSION_COMPLETED,
            {
                "user_name": "Gäst-Erik",
                "user_type": "guest",
                "energy_kwh": 32.1,
                "cost_kr": 80.25,
                "charge_price_kr": 144.45,
                "started_at": "2026-04-10T14:00:00+02:00",
                "ended_at": "2026-04-10T15:00:00+02:00",
            },
        )
        await hass.async_block_till_done()

        # Second guest session with different charge price
        hass.bus.async_fire(
            EVENT_SESSION_COMPLETED,
            {
                "user_name": "Gäst-Anna",
                "user_type": "guest",
                "energy_kwh": 44.4,
                "cost_kr": 111.0,
                "charge_price_kr": 200.0,
                "started_at": "2026-04-12T10:00:00+02:00",
                "ended_at": "2026-04-12T11:00:00+02:00",
            },
        )
        await hass.async_block_till_done()

    assert engine.guest_last is not None
    assert abs(engine.guest_last.charge_price_kr - 200.0) < 0.001


async def test_guest_last_charge_price_sensor_unavailable_without_guest_session(
    hass: HomeAssistant,
) -> None:
    """No prior guest session → guest_last_charge_price sensor is unavailable."""
    engine, store, entry = await _setup_stats_engine(hass)

    # No sessions yet — guest_last is None
    assert engine.guest_last is None

    # Sensor availability depends on engine.guest_last.charge_price_kr
    from custom_components.ev_charging_manager.stats_sensor import GuestLastChargePriceSensor

    sensor = GuestLastChargePriceSensor(hass, entry)
    assert sensor.available is False
    assert sensor.native_value is None


async def test_guest_last_charge_price_sensor_available_after_guest_session(
    hass: HomeAssistant,
) -> None:
    """After a guest session, guest_last_charge_price sensor shows the price."""
    engine, store, entry = await _setup_stats_engine(hass)

    engine._guest_last = GuestLastSession(
        energy_kwh=32.1,
        charge_price_kr=144.45,
        session_at="2026-04-10T15:00:00+02:00",
    )

    from custom_components.ev_charging_manager.stats_sensor import GuestLastChargePriceSensor

    sensor = GuestLastChargePriceSensor(hass, entry)
    assert sensor.available is True
    assert sensor.native_value == 144.45


async def test_guest_last_charge_price_sensor_unavailable_when_price_is_none(
    hass: HomeAssistant,
) -> None:
    """GuestLastSession exists but charge_price_kr is None → sensor unavailable."""
    engine, store, entry = await _setup_stats_engine(hass)

    # Guest session without pricing (legacy session before PR-06)
    engine._guest_last = GuestLastSession(
        energy_kwh=15.0,
        charge_price_kr=None,
        session_at="2026-03-01T12:00:00+01:00",
    )

    from custom_components.ev_charging_manager.stats_sensor import GuestLastChargePriceSensor

    sensor = GuestLastChargePriceSensor(hass, entry)
    assert sensor.available is False
    assert sensor.native_value is None
