"""Session and RfidResolution dataclasses for EV Charging Manager."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RfidResolution:
    """Immutable result of resolving a trx value to a user and optional vehicle."""

    user_name: str
    user_type: str  # "regular", "guest", "unknown"
    vehicle_name: str | None
    vehicle_battery_kwh: float | None
    efficiency_factor: float | None
    rfid_index: int | None  # trx-1, None for trx=0
    reason: str  # "matched", "unmapped", "rfid_inactive", "no_rfid"


@dataclass
class Session:
    """A single EV charging event.

    Created at session start with snapshot data, updated during charging,
    finalized at session end. All user/vehicle/pricing data is snapshotted
    at creation — never reference IDs that could become stale.
    """

    # Identity
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    # Snapshot fields (set at session start, never change)
    user_name: str = "Unknown"
    user_type: str = "unknown"
    vehicle_name: str | None = None
    vehicle_battery_kwh: float | None = None
    efficiency_factor: float | None = None
    rfid_index: int | None = None
    rfid_uid: str | None = None
    charger_name: str = ""

    # Timing
    started_at: str = ""
    ended_at: str | None = None
    duration_seconds: int = 0

    # Energy tracking
    energy_kwh: float = 0.0
    energy_start_kwh: float = 0.0

    # Power tracking
    avg_power_w: float = 0.0
    max_power_w: float = 0.0

    # Placeholder fields (deferred to later PRs)
    phases_used: int | None = None
    max_current_a: float | None = None

    # Cost (static pricing in this PR)
    cost_total_kr: float = 0.0
    cost_method: str = "static"
    price_details: None = None

    # Charge price (PR-06)
    charge_price_total_kr: None = None
    charge_price_method: None = None

    # SoC estimate
    estimated_soc_added_pct: float | None = None

    # Charger meter (PR-07)
    charger_total_before_kwh: None = None
    charger_total_after_kwh: None = None

    # Data quality flags (PR-07)
    data_gap: bool = False
    reconstructed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict matching PRD v1.9 session YAML schema."""
        return {
            "id": self.id,
            "user_name": self.user_name,
            "user_type": self.user_type,
            "vehicle_name": self.vehicle_name,
            "vehicle_battery_kwh": self.vehicle_battery_kwh,
            "efficiency_factor": self.efficiency_factor,
            "rfid_index": self.rfid_index,
            "rfid_uid": self.rfid_uid,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "energy_kwh": self.energy_kwh,
            "energy_start_kwh": self.energy_start_kwh,
            "avg_power_w": self.avg_power_w,
            "max_power_w": self.max_power_w,
            "phases_used": self.phases_used,
            "max_current_a": self.max_current_a,
            "cost_total_kr": self.cost_total_kr,
            "cost_method": self.cost_method,
            "price_details": self.price_details,
            "charge_price_total_kr": self.charge_price_total_kr,
            "charge_price_method": self.charge_price_method,
            "estimated_soc_added_pct": self.estimated_soc_added_pct,
            "charger_name": self.charger_name,
            "charger_total_before_kwh": self.charger_total_before_kwh,
            "charger_total_after_kwh": self.charger_total_after_kwh,
            "data_gap": self.data_gap,
            "reconstructed": self.reconstructed,
        }
