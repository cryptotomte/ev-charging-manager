"""Tests for PricingEngine (T007 + T004)."""

from __future__ import annotations

import pytest

from custom_components.ev_charging_manager.pricing import PricingEngine, SpotConfig

# ---------------------------------------------------------------------------
# Existing static pricing tests (must remain green)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# SpotConfig creation
# ---------------------------------------------------------------------------


def test_spot_config_creation():
    """SpotConfig is a frozen dataclass with correct fields."""
    cfg = SpotConfig(
        price_entity="sensor.nordpool",
        additional_cost_kwh=0.85,
        vat_multiplier=1.25,
        fallback_price_kwh=2.50,
    )
    assert cfg.price_entity == "sensor.nordpool"
    assert cfg.additional_cost_kwh == 0.85
    assert cfg.vat_multiplier == 1.25
    assert cfg.fallback_price_kwh == 2.50


def test_spot_config_is_frozen():
    """SpotConfig instances are immutable."""
    cfg = SpotConfig(
        price_entity="sensor.nordpool",
        additional_cost_kwh=0.85,
        vat_multiplier=1.25,
        fallback_price_kwh=2.50,
    )
    with pytest.raises(Exception):
        cfg.additional_cost_kwh = 1.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# calculate_spot_hour — normal prices
# ---------------------------------------------------------------------------


def _make_spot_engine(
    additional_cost: float = 0.85,
    vat: float = 1.25,
    fallback: float = 2.50,
) -> PricingEngine:
    cfg = SpotConfig(
        price_entity="sensor.nordpool",
        additional_cost_kwh=additional_cost,
        vat_multiplier=vat,
        fallback_price_kwh=fallback,
    )
    return PricingEngine(mode="spot", static_price=0.0, spot_config=cfg)


def test_calculate_spot_hour_single_hour_normal():
    """Scenario 2: 1.2 kWh × (0.89 + 0.85) × 1.25 = 2.61 kr."""
    engine = _make_spot_engine()
    detail = engine.calculate_spot_hour(kwh_this_hour=1.2, spot_price=0.89)

    assert detail["fallback"] is False
    assert detail["spot_price_kr_kwh"] == 0.89
    assert abs(detail["total_price_kr_kwh"] - 2.175) < 0.001
    assert abs(detail["cost_kr"] - 2.61) < 0.01


def test_calculate_spot_hour_multi_hour():
    """Scenario 3: second hour 3.6 kWh × (1.23 + 0.85) × 1.25 = 9.36 kr."""
    engine = _make_spot_engine()
    detail = engine.calculate_spot_hour(kwh_this_hour=3.6, spot_price=1.23)

    assert detail["fallback"] is False
    assert abs(detail["total_price_kr_kwh"] - 2.60) < 0.01
    assert abs(detail["cost_kr"] - 9.36) < 0.01


def test_calculate_spot_hour_negative_spot_price():
    """Negative spot prices are calculated correctly (common in Nord Pool)."""
    engine = _make_spot_engine()
    # (-0.10 + 0.85) × 1.25 = 0.9375 kr/kWh; 2.0 × 0.9375 = 1.875 kr
    detail = engine.calculate_spot_hour(kwh_this_hour=2.0, spot_price=-0.10)

    assert detail["fallback"] is False
    assert detail["spot_price_kr_kwh"] == -0.10
    assert abs(detail["cost_kr"] - 1.875) < 0.001


def test_calculate_spot_hour_zero_energy():
    """Zero energy → zero cost regardless of spot price."""
    engine = _make_spot_engine()
    detail = engine.calculate_spot_hour(kwh_this_hour=0.0, spot_price=1.23)

    assert detail["cost_kr"] == 0.0
    assert detail["fallback"] is False


# ---------------------------------------------------------------------------
# calculate_spot_hour — fallback when sensor unavailable
# ---------------------------------------------------------------------------


def test_calculate_spot_hour_single_hour_fallback():
    """Scenario 4 hour 15: sensor unavailable → fallback price used."""
    engine = _make_spot_engine(fallback=2.50)
    detail = engine.calculate_spot_hour(kwh_this_hour=3.6, spot_price=None)

    assert detail["fallback"] is True
    assert detail["spot_price_kr_kwh"] is None
    assert detail["total_price_kr_kwh"] == 2.50
    assert abs(detail["cost_kr"] - 9.00) < 0.001


def test_calculate_spot_hour_fallback_zero_energy():
    """Zero energy with unavailable sensor → zero cost, fallback=True."""
    engine = _make_spot_engine()
    detail = engine.calculate_spot_hour(kwh_this_hour=0.0, spot_price=None)

    assert detail["fallback"] is True
    assert detail["cost_kr"] == 0.0


# ---------------------------------------------------------------------------
# calculate_spot_total — multi-hour sum
# ---------------------------------------------------------------------------


def test_calculate_spot_total_multi_hour():
    """Sum of multiple hourly costs: 2.61 + 9.36 + 8.10 = 20.07."""
    engine = _make_spot_engine()
    price_details = [
        {"cost_kr": 2.61},
        {"cost_kr": 9.36},
        {"cost_kr": 8.10},
    ]
    total = engine.calculate_spot_total(price_details)
    assert abs(total - 20.07) < 0.001


def test_calculate_spot_total_empty_list():
    """Empty price_details → zero total."""
    engine = _make_spot_engine()
    assert engine.calculate_spot_total([]) == 0.0


def test_calculate_spot_total_single_entry():
    """Single entry → returns that entry's cost."""
    engine = _make_spot_engine()
    total = engine.calculate_spot_total([{"cost_kr": 2.61}])
    assert abs(total - 2.61) < 0.001


# ---------------------------------------------------------------------------
# PricingEngine.mode property
# ---------------------------------------------------------------------------


def test_pricing_engine_mode_static():
    """mode property returns 'static' for static engines."""
    engine = PricingEngine(mode="static", static_price=2.50)
    assert engine.mode == "static"


def test_pricing_engine_mode_spot():
    """mode property returns 'spot' for spot engines."""
    engine = _make_spot_engine()
    assert engine.mode == "spot"
