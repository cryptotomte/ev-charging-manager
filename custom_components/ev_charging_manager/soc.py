"""SoC estimation for EV charging sessions."""

from __future__ import annotations


def estimate_soc(
    energy_kwh: float,
    efficiency_factor: float | None,
    battery_capacity_kwh: float | None,
) -> float | None:
    """Estimate the state-of-charge percentage added during a session.

    Formula: (energy_kwh × efficiency_factor) / battery_capacity_kwh × 100

    Returns None if efficiency_factor or battery_capacity_kwh is None or zero
    (guards against division by zero and missing vehicle data).
    """
    if efficiency_factor is None or battery_capacity_kwh is None:
        return None
    if battery_capacity_kwh == 0:
        return None
    return (energy_kwh * efficiency_factor) / battery_capacity_kwh * 100
