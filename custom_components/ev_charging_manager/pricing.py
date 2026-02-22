"""Pricing engine for EV charging session cost calculation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SpotConfig:
    """Configuration for spot pricing mode."""

    price_entity: str
    additional_cost_kwh: float
    vat_multiplier: float
    fallback_price_kwh: float


class PricingEngine:
    """Calculate session cost from energy consumed.

    Supports static and spot pricing modes.
    """

    def __init__(
        self,
        mode: str,
        static_price: float,
        spot_config: SpotConfig | None = None,
    ) -> None:
        """Initialize with pricing mode and static price per kWh."""
        self._mode = mode
        self._static_price = static_price
        self._spot_config = spot_config

    @property
    def mode(self) -> str:
        """Return the pricing mode ('static' or 'spot')."""
        return self._mode

    @property
    def fallback_price(self) -> float | None:
        """Return the fallback price if in spot mode, else None."""
        if self._spot_config is None:
            return None
        return self._spot_config.fallback_price_kwh

    def calculate(self, energy_kwh: float) -> float:
        """Calculate total cost for the given energy consumption.

        For static mode: cost = energy_kwh × static_price_per_kwh.
        """
        return energy_kwh * self._static_price

    def calculate_spot_hour(self, kwh_this_hour: float, spot_price: float | None) -> dict:
        """Calculate cost for one clock-hour segment.

        Args:
            kwh_this_hour: Energy consumed during this hour segment.
            spot_price: Spot price from sensor (kr/kWh), or None if unavailable.

        Returns:
            dict with keys: spot_price_kr_kwh, total_price_kr_kwh, cost_kr, fallback

        """
        if self._spot_config is None:
            raise RuntimeError("calculate_spot_hour requires spot_config (mode must be 'spot')")
        cfg = self._spot_config

        if spot_price is None:
            total_price = cfg.fallback_price_kwh
            fallback = True
            effective_spot = None
        else:
            total_price = round((spot_price + cfg.additional_cost_kwh) * cfg.vat_multiplier, 4)
            fallback = False
            effective_spot = spot_price

        cost = round(kwh_this_hour * total_price, 4)

        return {
            "spot_price_kr_kwh": effective_spot,
            "total_price_kr_kwh": round(total_price, 4),
            "cost_kr": cost,
            "fallback": fallback,
        }

    def calculate_spot_total(self, price_details: list[dict]) -> float:
        """Sum all hourly costs from price_details list.

        Returns:
            Total session cost (kr).

        """
        return round(sum(d.get("cost_kr", 0.0) for d in price_details), 4)
