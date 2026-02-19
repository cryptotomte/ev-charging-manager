"""Tests for EV Charging Manager setup / unload / device registration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.const import DOMAIN


async def test_setup_entry(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """Config entry loads successfully."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_unload_entry(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """Config entry unloads cleanly after setup."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED

    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED


async def test_device_created(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """Device is registered with correct name and manufacturer after setup."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(identifiers={(DOMAIN, mock_config_entry.entry_id)})

    assert device is not None
    assert device.name == "My go-e Charger"
    assert device.manufacturer == "EV Charging Manager"
    assert device.model == "go-e Charger (Gemini / Gemini flex)"


async def test_multiple_instances(hass: HomeAssistant) -> None:
    """Two separate config entries create two separate devices without conflict."""
    entry1 = MockConfigEntry(
        domain=DOMAIN,
        data={
            "charger_profile": "goe_gemini",
            "car_status_entity": "sensor.goe_aaa_car_0",
            "car_status_charging_value": 2,
            "energy_entity": "sensor.goe_aaa_wh",
            "energy_unit": "Wh",
            "power_entity": "sensor.goe_aaa_nrg_total_power",
            "rfid_entity": None,
            "total_energy_entity": None,
            "rfid_uid_entity": None,
            "charger_name": "Garage Charger",
            "charger_host": "192.168.1.100",
            "pricing_mode": "static",
            "static_price_kwh": 2.50,
        },
        title="Garage Charger",
    )
    entry2 = MockConfigEntry(
        domain=DOMAIN,
        data={
            "charger_profile": "easee_home",
            "car_status_entity": "sensor.easee_status",
            "car_status_charging_value": "charging",
            "energy_entity": "sensor.easee_session_energy",
            "energy_unit": "kWh",
            "power_entity": "sensor.easee_power",
            "rfid_entity": None,
            "total_energy_entity": None,
            "rfid_uid_entity": None,
            "charger_name": "Driveway Charger",
            "charger_host": None,
            "pricing_mode": "static",
            "static_price_kwh": 1.80,
        },
        title="Driveway Charger",
    )

    entry1.add_to_hass(hass)
    entry2.add_to_hass(hass)

    # HA auto-sets-up all domain entries when the domain is first loaded;
    # calling async_setup for entry1 is sufficient to trigger both.
    await hass.config_entries.async_setup(entry1.entry_id)
    await hass.async_block_till_done()

    assert entry1.state is ConfigEntryState.LOADED
    assert entry2.state is ConfigEntryState.LOADED

    device_registry = dr.async_get(hass)
    device1 = device_registry.async_get_device(identifiers={(DOMAIN, entry1.entry_id)})
    device2 = device_registry.async_get_device(identifiers={(DOMAIN, entry2.entry_id)})

    assert device1 is not None
    assert device2 is not None
    assert device1.id != device2.id
    assert device1.name == "Garage Charger"
    assert device2.name == "Driveway Charger"
