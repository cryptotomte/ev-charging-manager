"""TC-024: Guest user charging test (PR-22 Phase 9).

Verifies that a session with a mapped guest user has:
- user_type="guest"
- charge_price_total_kr set (PR-06 guest pricing applied)
- charge_price_method matching the user's configured method
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
)

MOCK_PLUG_ENTITY = "binary_sensor.goe_abc123_car_0"
MOCK_CABLE_LOCK_ENTITY = "sensor.goe_abc123_cus_value"
MOCK_TRX_ENTITY = "select.goe_abc123_trx"

GUEST_PRICE_PER_KWH = 4.50


async def _add_guest_user(hass, entry_id) -> str:
    """Add a guest user via the subentry config flow. Returns the subentry_id."""
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "user"),
        context={"source": "user"},
    )
    # Step 1: basic info
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Guest Friend", "type": "guest"},
    )
    # Step 2: guest pricing
    await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"guest_pricing_method": "fixed", "price_per_kwh": GUEST_PRICE_PER_KWH},
    )
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry_id)
    return [s for s in entry.subentries.values() if s.subentry_type == "user"][-1].subentry_id


async def _add_rfid(hass, entry_id, card_index: int, user_id: str) -> None:
    """Add an RFID mapping via the subentry config flow."""
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "rfid_mapping"),
        context={"source": "user"},
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"card_index": str(card_index), "user_id": user_id},
    )
    await hass.async_block_till_done()


async def test_tc024_guest_charging_price_applied(hass: HomeAssistant, freezer) -> None:
    """TC-024: Guest user charges → user_type=guest, charge_price_total_kr set."""
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

        # Add guest user and RFID mapping via config flows
        guest_user_id = await _add_guest_user(hass, entry.entry_id)
        await _add_rfid(hass, entry.entry_id, card_index=1, user_id=guest_user_id)
        # card_index=1 → trx=2 (card slot 2 in go-e app)

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert isinstance(engine, PlugAnchoredSessionEngine)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    energy_kwh = 6.0  # 6 kWh → charge price = 6.0 * 4.50 = 27.00 kr

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # RFID tap then plug-in (trx=2 → rfid_index=1 → maps to Guest Friend)
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        await hass.async_block_till_done()

        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        # Start charging
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.5")
        hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
        await hass.async_block_till_done()

        # Advance time past micro-filter
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert engine.active_session is not None
        assert engine.active_session.user_type == "guest", (
            f"TC-024: expected user_type='guest', got {engine.active_session.user_type!r}"
        )
        assert engine.active_session.user_name == "Guest Friend", (
            f"TC-024: expected user_name='Guest Friend', got {engine.active_session.user_name!r}"
        )

        # Finish charging
        hass.states.async_set(MOCK_ENERGY_ENTITY, str(energy_kwh))
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()

        # Unplug
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    # Verify stored session
    assert len(session_store.sessions) == 1, (
        f"TC-024: Expected 1 session, got {len(session_store.sessions)}"
    )
    session = session_store.sessions[0]

    assert session["user_type"] == "guest", (
        f"TC-024: stored session user_type must be 'guest', got {session['user_type']!r}"
    )
    assert session["user_name"] == "Guest Friend"

    # Guest charge price should be set
    assert session.get("charge_price_total_kr") is not None, (
        "TC-024: charge_price_total_kr must be set for guest sessions"
    )
    assert session.get("charge_price_method") == "fixed", (
        f"TC-024: charge_price_method must be 'fixed', got {session.get('charge_price_method')!r}"
    )

    # Price check: energy * price_per_kwh (±10 Wh tolerance for energy rounding)
    expected_price = energy_kwh * GUEST_PRICE_PER_KWH
    assert abs(session["charge_price_total_kr"] - expected_price) <= 0.10, (
        f"TC-024: charge_price_total_kr {session['charge_price_total_kr']:.2f} kr "
        f"not close to expected {expected_price:.2f} kr"
    )
