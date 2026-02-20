"""Tests for estimate_soc (T009)."""

from __future__ import annotations

from custom_components.ev_charging_manager.soc import estimate_soc


def test_normal_soc_calculation():
    """(12.4 × 0.88) / 14.4 × 100 ≈ 75.8%."""
    result = estimate_soc(energy_kwh=12.4, efficiency_factor=0.88, battery_capacity_kwh=14.4)
    assert result is not None
    assert abs(result - 75.78) < 0.1


def test_none_efficiency_returns_none():
    """No efficiency factor → return None (no vehicle data)."""
    result = estimate_soc(energy_kwh=12.4, efficiency_factor=None, battery_capacity_kwh=14.4)
    assert result is None


def test_none_battery_returns_none():
    """No battery capacity → return None."""
    result = estimate_soc(energy_kwh=12.4, efficiency_factor=0.88, battery_capacity_kwh=None)
    assert result is None


def test_zero_battery_returns_none():
    """Zero battery capacity → return None (division guard)."""
    result = estimate_soc(energy_kwh=12.4, efficiency_factor=0.88, battery_capacity_kwh=0)
    assert result is None


def test_zero_energy_gives_zero_soc():
    """Zero energy added → 0% SoC added."""
    result = estimate_soc(energy_kwh=0.0, efficiency_factor=0.88, battery_capacity_kwh=14.4)
    assert result == 0.0


def test_large_battery_100kwh():
    """100 kWh battery EV — 50 kWh charged at 90% efficiency → 45%."""
    result = estimate_soc(energy_kwh=50.0, efficiency_factor=0.90, battery_capacity_kwh=100.0)
    assert result is not None
    assert abs(result - 45.0) < 0.01


def test_full_charge_calculation():
    """14.4 kWh at 100% efficiency into 14.4 kWh battery → 100% SoC."""
    result = estimate_soc(energy_kwh=14.4, efficiency_factor=1.0, battery_capacity_kwh=14.4)
    assert result is not None
    assert abs(result - 100.0) < 0.01
