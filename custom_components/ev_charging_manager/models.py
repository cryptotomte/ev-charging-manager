"""Data model classes for EV Charging Manager subentries."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class GuestPricing:
    """Pricing configuration for guest users."""

    method: str  # "fixed" or "markup"
    price_per_kwh: float | None = None
    markup_factor: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict (PRD v1.9 format)."""
        result: dict[str, Any] = {"method": self.method}
        if self.method == "fixed":
            result["price_per_kwh"] = self.price_per_kwh
        elif self.method == "markup":
            result["markup_factor"] = self.markup_factor
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GuestPricing:
        """Create from a dict (subentry data or JSON)."""
        return cls(
            method=data["method"],
            price_per_kwh=data.get("price_per_kwh"),
            markup_factor=data.get("markup_factor"),
        )

    def validate(self) -> str | None:
        """Validate pricing fields. Returns error key or None."""
        if self.method == "fixed" and not self.price_per_kwh:
            return "price_required"
        if self.method == "markup" and not self.markup_factor:
            return "markup_required"
        return None


@dataclass
class Vehicle:
    """Vehicle entity with charging parameters."""

    id: str
    name: str
    battery_capacity_kwh: float
    charging_phases: int
    usable_battery_kwh: float | None = None
    max_charging_power_kw: float | None = None
    charging_efficiency: float = 0.90

    def __post_init__(self) -> None:
        """Set defaults after init."""
        if self.usable_battery_kwh is None:
            self.usable_battery_kwh = self.battery_capacity_kwh

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict (PRD v1.9 format)."""
        return {
            "id": self.id,
            "name": self.name,
            "battery_capacity_kwh": self.battery_capacity_kwh,
            "usable_battery_kwh": self.usable_battery_kwh,
            "charging_phases": self.charging_phases,
            "max_charging_power_kw": self.max_charging_power_kw,
            "charging_efficiency": self.charging_efficiency,
        }

    @classmethod
    def from_subentry(cls, subentry_id: str, data: dict[str, Any]) -> Vehicle:
        """Create from a HA ConfigSubentry's data dict."""
        return cls(
            id=subentry_id,
            name=data["name"],
            battery_capacity_kwh=data["battery_capacity_kwh"],
            usable_battery_kwh=data.get("usable_battery_kwh"),
            max_charging_power_kw=data.get("max_charging_power_kw"),
            charging_phases=data["charging_phases"],
            charging_efficiency=data.get("charging_efficiency", 0.90),
        )


@dataclass
class User:
    """User entity — regular or guest."""

    id: str
    name: str
    type: str  # "regular" or "guest"
    active: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    guest_pricing: GuestPricing | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict (PRD v1.9 format)."""
        result: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "active": self.active,
            "created_at": self.created_at,
        }
        if self.type == "guest" and self.guest_pricing:
            result["guest_pricing"] = self.guest_pricing.to_dict()
        return result

    @classmethod
    def from_subentry(cls, subentry_id: str, data: dict[str, Any]) -> User:
        """Create from a HA ConfigSubentry's data dict."""
        guest_pricing = None
        if data.get("guest_pricing"):
            guest_pricing = GuestPricing.from_dict(data["guest_pricing"])
        return cls(
            id=subentry_id,
            name=data["name"],
            type=data["type"],
            active=data.get("active", True),
            created_at=data.get("created_at", datetime.now(UTC).isoformat()),
            guest_pricing=guest_pricing,
        )

    def validate(self) -> str | None:
        """Validate user fields. Returns error key or None."""
        if self.type == "guest" and not self.guest_pricing:
            return "pricing_required"
        if self.guest_pricing:
            return self.guest_pricing.validate()
        return None


@dataclass
class RfidMapping:
    """RFID card mapping linking card_index to user and optional vehicle."""

    card_index: int
    user_id: str
    card_uid: str | None = None
    vehicle_id: str | None = None
    active: bool = True
    deactivated_by: str | None = None  # null, "user_cascade", "individual"

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict (PRD v1.9 format)."""
        return {
            "card_index": self.card_index,
            "card_uid": self.card_uid,
            "user_id": self.user_id,
            "vehicle_id": self.vehicle_id,
            "active": self.active,
            "deactivated_by": self.deactivated_by,
        }

    @classmethod
    def from_subentry(cls, data: dict[str, Any]) -> RfidMapping:
        """Create from a HA ConfigSubentry's data dict."""
        return cls(
            card_index=data["card_index"],
            card_uid=data.get("card_uid"),
            user_id=data["user_id"],
            vehicle_id=data.get("vehicle_id"),
            active=data.get("active", True),
            deactivated_by=data.get("deactivated_by"),
        )
