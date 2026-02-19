"""Shared fixtures for EV Charging Manager tests."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations for all tests."""


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a mock config entry with sample go-e data."""
    return MockConfigEntry(
        domain="ev_charging_manager",
        data={
            "charger_profile": "goe_gemini",
            "car_status_entity": "sensor.goe_abc123_car_0",
            "car_status_charging_value": 2,
            "energy_entity": "sensor.goe_abc123_wh",
            "energy_unit": "Wh",
            "power_entity": "sensor.goe_abc123_nrg_total_power",
            "rfid_entity": "sensor.goe_abc123_trx",
            "total_energy_entity": None,
            "rfid_uid_entity": "sensor.goe_abc123_lri",
            "charger_name": "My go-e Charger",
            "charger_host": "192.168.1.100",
            "pricing_mode": "static",
            "static_price_kwh": 2.50,
        },
        title="My go-e Charger",
    )
