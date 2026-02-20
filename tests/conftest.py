"""Shared fixtures for EV Charging Manager tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.config_store import ConfigStore

# Standard mock data for charger config entry
MOCK_CHARGER_DATA = {
    "charger_profile": "goe_gemini",
    "charger_serial": "abc123",
    "car_status_entity": "sensor.goe_abc123_car_value",
    "car_status_charging_value": "Charging",
    "energy_entity": "sensor.goe_abc123_wh",
    "energy_unit": "kWh",
    "power_entity": "sensor.goe_abc123_nrg_11",
    "rfid_entity": "select.goe_abc123_trx",
    "total_energy_entity": None,
    "rfid_uid_entity": None,
    "charger_name": "My go-e Charger",
    "charger_host": "192.168.1.100",
    "pricing_mode": "static",
    "static_price_kwh": 2.50,
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations for all tests."""


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a mock config entry with sample go-e data."""
    return MockConfigEntry(
        domain="ev_charging_manager",
        data=MOCK_CHARGER_DATA,
        title="My go-e Charger",
    )


@pytest.fixture
def mock_vehicle_subentry_data() -> dict:
    """Return sample vehicle subentry data."""
    return {
        "name": "Peugeot 3008 PHEV",
        "battery_capacity_kwh": 14.4,
        "usable_battery_kwh": 14.4,
        "charging_phases": 1,
        "max_charging_power_kw": 3.7,
        "charging_efficiency": 0.88,
    }


@pytest.fixture
def mock_user_subentry_data() -> dict:
    """Return sample regular user subentry data."""
    return {
        "name": "Paul",
        "type": "regular",
        "active": True,
        "created_at": "2026-03-01T10:00:00+00:00",
    }


@pytest.fixture
def mock_guest_user_subentry_data() -> dict:
    """Return sample guest user subentry data with fixed pricing."""
    return {
        "name": "Guest",
        "type": "guest",
        "active": True,
        "created_at": "2026-03-01T10:05:00+00:00",
        "guest_pricing": {
            "method": "fixed",
            "price_per_kwh": 4.50,
        },
    }


@pytest.fixture
def mock_rfid_subentry_data() -> dict:
    """Return sample RFID mapping subentry data."""
    return {
        "card_index": 0,
        "card_uid": None,
        "user_id": "mock_user_subentry_id",
        "vehicle_id": "mock_vehicle_subentry_id",
        "active": True,
        "deactivated_by": None,
    }


@pytest.fixture
async def mock_config_store(hass: HomeAssistant) -> ConfigStore:
    """Return a ConfigStore instance with mocked storage."""
    store = ConfigStore(hass)
    with patch.object(store._store, "async_save", new_callable=AsyncMock):
        await store.async_load()
        yield store


async def setup_entry_with_subentries(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> ConfigEntry:
    """Set up a config entry and return it after setup completes."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.config_entries.async_get_entry(entry.entry_id)
