"""Tests for PricingEngine (T007)."""

from __future__ import annotations

from custom_components.ev_charging_manager.pricing import PricingEngine


def test_static_pricing_basic():
    """12.4 kWh × 2.50 kr/kWh = 31.00 kr."""
    engine = PricingEngine(mode="static", static_price=2.50)
    result = engine.calculate(12.4)
    assert abs(result - 31.00) < 0.001


def test_zero_energy_gives_zero_cost():
    """0 kWh → 0 kr cost."""
    engine = PricingEngine(mode="static", static_price=2.50)
    assert engine.calculate(0.0) == 0.0


def test_large_energy_value():
    """Large energy (100 kWh) × high price (5 kr) = 500 kr."""
    engine = PricingEngine(mode="static", static_price=5.0)
    assert engine.calculate(100.0) == 500.0


def test_negative_price_allowed():
    """Negative price is calculated as-is (domain allows it)."""
    engine = PricingEngine(mode="static", static_price=-1.0)
    result = engine.calculate(10.0)
    assert result == -10.0


def test_zero_price_gives_zero_cost():
    """Zero price per kWh → zero cost regardless of energy."""
    engine = PricingEngine(mode="static", static_price=0.0)
    assert engine.calculate(50.0) == 0.0


def test_small_energy_precision():
    """Small energy amounts are calculated with float precision."""
    engine = PricingEngine(mode="static", static_price=2.50)
    result = engine.calculate(0.5)
    assert abs(result - 1.25) < 0.0001
