"""Pricing engine for EV charging session cost calculation."""

from __future__ import annotations


class PricingEngine:
    """Calculate session cost from energy consumed.

    Supports static pricing mode in this PR. Spot pricing added in PR-05.
    """

    def __init__(self, mode: str, static_price: float) -> None:
        """Initialize with pricing mode and static price per kWh."""
        self._mode = mode
        self._static_price = static_price

    def calculate(self, energy_kwh: float) -> float:
        """Calculate total cost for the given energy consumption.

        For static mode: cost = energy_kwh × static_price_per_kwh.
        """
        return energy_kwh * self._static_price
