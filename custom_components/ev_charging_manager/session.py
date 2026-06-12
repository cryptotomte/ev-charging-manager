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
    guest_pricing: dict | None = None  # Guest pricing config snapshot (PR-06)


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

    # Timing (legacy fields — preserved for backward compatibility)
    started_at: str = ""  # alias for connected_at (backward compat)
    ended_at: str | None = None  # alias for disconnected_at (backward compat)
    duration_seconds: int = 0  # alias for charging_duration_s (backward compat)

    # PR-22: Plug-anchored timing fields
    # connected_at replaces started_at as the canonical session-start timestamp
    connected_at: str = ""
    disconnected_at: str | None = None
    connection_duration_s: int = 0

    # PR-22: Charging-window timing fields
    charging_started_at: str | None = None  # first power > 0 after session start; does NOT move
    charging_ended_at: str | None = None  # end of most-recently-closed window; None while open
    charging_duration_s: int = 0  # sum of all closed window durations
    charging_window_count: int = 0  # number of windows opened during the session

    # PR-22 revision 2026-05-19: the originally-planned `blocked` field has been
    # removed. Story 07 no longer force-stops sessions (passive notification model),
    # so unmapped-RFID sessions are signalled solely via user_name="Unknown" /
    # user_type="unknown" (the existing attribution). See FR-032 (REVISED).

    # Energy tracking
    energy_kwh: float = 0.0
    energy_start_kwh: float = 0.0

    # Power tracking
    avg_power_w: float = 0.0
    max_power_w: float = 0.0

    # Placeholder fields (deferred to later PRs)
    phases_used: int | None = None
    max_current_a: float | None = None

    # Cost
    cost_total_kr: float = 0.0
    cost_method: str = "static"
    price_details: list[dict] | None = None

    # Charge price (PR-06)
    charge_price_total_kr: float | None = None
    charge_price_method: str | None = None

    # SoC estimate
    estimated_soc_added_pct: float | None = None

    # Charger meter (PR-07)
    charger_total_before_kwh: float | None = None
    charger_total_after_kwh: float | None = None

    # Data quality flags (PR-07).
    #
    # data_gap is the CENTRAL "this record may be incomplete or imprecise"
    # flag (this list is the canonical definition — setters reference it):
    #   - full charger outage detected during tracking (PR-22 FR-028),
    #   - HA-restart resume (PR-22 FR-026 — every recovered session),
    #   - mid-session meter reset rebase (PR-27 FR-015),
    #   - transient disconnect in flight (plug=off with cable_lock not
    #     Unlocked; restored to its pre-disconnect value when a lagging
    #     cable_lock→Unlocked confirms a clean unplug, PR-25 FR-012),
    #   - energy/power entity unavailable mid-session,
    #   - session kept despite an unparseable connection timestamp
    #     (PR-27 FR-018, live and recovery paths),
    #   - degraded completion metric (spot finalize / ETO read / guest
    #     charge price, review F1a).
    # The engine mirrors the flag in PlugAnchoredSessionEngine._data_gap and
    # transfers it onto the session at completion.
    data_gap: bool = False
    reconstructed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict — schema v1.2 (PR-22).

        Preserves legacy field names (started_at, ended_at, duration_seconds) as
        aliases for one release to ease downstream automation transitions.
        New canonical names (connected_at, disconnected_at, charging_duration_s) take
        precedence; old names are kept for backward compatibility only.
        """
        return {
            "id": self.id,
            "user_name": self.user_name,
            "user_type": self.user_type,
            "vehicle_name": self.vehicle_name,
            "vehicle_battery_kwh": self.vehicle_battery_kwh,
            "efficiency_factor": self.efficiency_factor,
            "rfid_index": self.rfid_index,
            "rfid_uid": self.rfid_uid,
            # Legacy aliases (kept for one release per data-model.md backward-compat plan)
            "started_at": self.connected_at or self.started_at,
            "ended_at": self.disconnected_at or self.ended_at,
            "duration_seconds": self.charging_duration_s or self.duration_seconds,
            # PR-22 canonical fields
            "connected_at": self.connected_at or self.started_at,
            "disconnected_at": self.disconnected_at or self.ended_at,
            "connection_duration_s": self.connection_duration_s,
            "charging_started_at": self.charging_started_at,
            "charging_ended_at": self.charging_ended_at,
            "charging_duration_s": self.charging_duration_s or self.duration_seconds,
            "charging_window_count": self.charging_window_count,
            # PR-22 revision 2026-05-19: `blocked` field removed (FR-032 REVISED).
            # Energy
            "energy_kwh": self.energy_kwh,
            "energy_start_kwh": self.energy_start_kwh,
            "avg_power_w": self.avg_power_w,
            "max_power_w": self.max_power_w,
            "phases_used": self.phases_used,
            "max_current_a": self.max_current_a,
            # Cost
            "cost_total_kr": self.cost_total_kr,
            "cost_method": self.cost_method,
            "price_details": self.price_details,
            "charge_price_total_kr": self.charge_price_total_kr,
            "charge_price_method": self.charge_price_method,
            "estimated_soc_added_pct": self.estimated_soc_added_pct,
            # Charger metadata
            "charger_name": self.charger_name,
            "charger_total_before_kwh": self.charger_total_before_kwh,
            "charger_total_after_kwh": self.charger_total_after_kwh,
            # Quality flags
            "data_gap": self.data_gap,
            "reconstructed": self.reconstructed,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Session":
        """Reconstruct a Session from a stored dict (schema v1.1 or v1.2).

        Supports both legacy field names (started_at, ended_at, duration_seconds)
        and new canonical names (connected_at, disconnected_at, charging_duration_s).
        Old-only records are up-converted transparently.
        """
        # Resolve connected_at from canonical or legacy key
        connected_at = d.get("connected_at") or d.get("started_at", "")
        disconnected_at = d.get("disconnected_at") or d.get("ended_at")
        charging_duration_s = d.get("charging_duration_s") or d.get("duration_seconds", 0)

        return cls(
            id=d.get("id", ""),
            user_name=d.get("user_name", "Unknown"),
            user_type=d.get("user_type", "unknown"),
            vehicle_name=d.get("vehicle_name"),
            vehicle_battery_kwh=d.get("vehicle_battery_kwh"),
            efficiency_factor=d.get("efficiency_factor"),
            rfid_index=d.get("rfid_index"),
            rfid_uid=d.get("rfid_uid"),
            charger_name=d.get("charger_name", ""),
            # Legacy aliases
            started_at=d.get("started_at", connected_at),
            ended_at=d.get("ended_at", disconnected_at),
            duration_seconds=int(d.get("duration_seconds", charging_duration_s)),
            # PR-22 canonical fields
            connected_at=connected_at,
            disconnected_at=disconnected_at,
            connection_duration_s=int(d.get("connection_duration_s", 0)),
            charging_started_at=d.get("charging_started_at"),
            charging_ended_at=d.get("charging_ended_at"),
            charging_duration_s=int(charging_duration_s),
            charging_window_count=int(d.get("charging_window_count", 0)),
            # PR-22 revision 2026-05-19: `blocked` field removed (FR-032 REVISED).
            # Any legacy stored `blocked: true` value is intentionally ignored.
            # Energy
            energy_kwh=float(d.get("energy_kwh", 0.0)),
            energy_start_kwh=float(d.get("energy_start_kwh", 0.0)),
            avg_power_w=float(d.get("avg_power_w", 0.0)),
            max_power_w=float(d.get("max_power_w", 0.0)),
            phases_used=d.get("phases_used"),
            max_current_a=d.get("max_current_a"),
            # Cost
            cost_total_kr=float(d.get("cost_total_kr", 0.0)),
            cost_method=d.get("cost_method", "static"),
            price_details=d.get("price_details"),
            charge_price_total_kr=d.get("charge_price_total_kr"),
            charge_price_method=d.get("charge_price_method"),
            estimated_soc_added_pct=d.get("estimated_soc_added_pct"),
            # Charger metadata
            charger_total_before_kwh=d.get("charger_total_before_kwh"),
            charger_total_after_kwh=d.get("charger_total_after_kwh"),
            # Quality flags
            data_gap=bool(d.get("data_gap", False)),
            reconstructed=bool(d.get("reconstructed", False)),
        )
