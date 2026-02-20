"""Tests for EV Charging Manager data models."""

from __future__ import annotations

from custom_components.ev_charging_manager.models import (
    GuestPricing,
    RfidMapping,
    User,
    Vehicle,
)

# ---------------------------------------------------------------------------
# Vehicle
# ---------------------------------------------------------------------------


def test_vehicle_from_subentry(mock_vehicle_subentry_data: dict) -> None:
    """Vehicle.from_subentry creates a Vehicle with correct field values."""
    vehicle = Vehicle.from_subentry("sub_001", mock_vehicle_subentry_data)

    assert vehicle.id == "sub_001"
    assert vehicle.name == "Peugeot 3008 PHEV"
    assert vehicle.battery_capacity_kwh == 14.4
    assert vehicle.usable_battery_kwh == 14.4
    assert vehicle.charging_phases == 1
    assert vehicle.max_charging_power_kw == 3.7
    assert vehicle.charging_efficiency == 0.88


def test_vehicle_usable_defaults_to_capacity() -> None:
    """usable_battery_kwh defaults to battery_capacity_kwh when not set."""
    data = {
        "name": "Tesla Model 3",
        "battery_capacity_kwh": 60.0,
        "charging_phases": 3,
    }
    vehicle = Vehicle.from_subentry("sub_002", data)

    assert vehicle.usable_battery_kwh == 60.0


def test_vehicle_to_dict(mock_vehicle_subentry_data: dict) -> None:
    """Vehicle.to_dict produces PRD v1.9 JSON format."""
    vehicle = Vehicle.from_subentry("sub_001", mock_vehicle_subentry_data)
    result = vehicle.to_dict()

    assert result == {
        "id": "sub_001",
        "name": "Peugeot 3008 PHEV",
        "battery_capacity_kwh": 14.4,
        "usable_battery_kwh": 14.4,
        "charging_phases": 1,
        "max_charging_power_kw": 3.7,
        "charging_efficiency": 0.88,
    }


def test_vehicle_default_efficiency() -> None:
    """Vehicle defaults to 0.90 charging efficiency when not specified."""
    data = {
        "name": "Nissan Leaf",
        "battery_capacity_kwh": 40.0,
        "charging_phases": 1,
    }
    vehicle = Vehicle.from_subentry("sub_003", data)

    assert vehicle.charging_efficiency == 0.90


# ---------------------------------------------------------------------------
# User — regular
# ---------------------------------------------------------------------------


def test_user_from_subentry_regular(mock_user_subentry_data: dict) -> None:
    """User.from_subentry creates a regular user with correct fields."""
    user = User.from_subentry("sub_100", mock_user_subentry_data)

    assert user.id == "sub_100"
    assert user.name == "Paul"
    assert user.type == "regular"
    assert user.active is True
    assert user.created_at == "2026-03-01T10:00:00+00:00"
    assert user.guest_pricing is None


def test_user_to_dict_regular(mock_user_subentry_data: dict) -> None:
    """Regular user to_dict matches PRD format (no guest_pricing)."""
    user = User.from_subentry("sub_100", mock_user_subentry_data)
    result = user.to_dict()

    assert result == {
        "id": "sub_100",
        "name": "Paul",
        "type": "regular",
        "active": True,
        "created_at": "2026-03-01T10:00:00+00:00",
    }
    assert "guest_pricing" not in result


# ---------------------------------------------------------------------------
# User — guest with pricing
# ---------------------------------------------------------------------------


def test_user_from_subentry_guest(mock_guest_user_subentry_data: dict) -> None:
    """User.from_subentry creates a guest user with pricing."""
    user = User.from_subentry("sub_200", mock_guest_user_subentry_data)

    assert user.id == "sub_200"
    assert user.name == "Guest"
    assert user.type == "guest"
    assert user.guest_pricing is not None
    assert user.guest_pricing.method == "fixed"
    assert user.guest_pricing.price_per_kwh == 4.50


def test_user_to_dict_guest(mock_guest_user_subentry_data: dict) -> None:
    """Guest user to_dict includes guest_pricing object."""
    user = User.from_subentry("sub_200", mock_guest_user_subentry_data)
    result = user.to_dict()

    assert result["type"] == "guest"
    assert result["guest_pricing"] == {"method": "fixed", "price_per_kwh": 4.50}


def test_user_guest_markup_pricing() -> None:
    """Guest user with markup pricing produces correct to_dict."""
    data = {
        "name": "Markup Guest",
        "type": "guest",
        "active": True,
        "created_at": "2026-03-01T12:00:00+00:00",
        "guest_pricing": {"method": "markup", "markup_factor": 1.8},
    }
    user = User.from_subentry("sub_201", data)
    result = user.to_dict()

    assert result["guest_pricing"] == {"method": "markup", "markup_factor": 1.8}


# ---------------------------------------------------------------------------
# User validation
# ---------------------------------------------------------------------------


def test_user_validate_guest_without_pricing() -> None:
    """Guest user without pricing returns 'pricing_required'."""
    user = User(
        id="sub_300",
        name="No Price Guest",
        type="guest",
        guest_pricing=None,
    )
    assert user.validate() == "pricing_required"


def test_user_validate_regular_ok() -> None:
    """Regular user always validates OK."""
    user = User(id="sub_301", name="Paul", type="regular")
    assert user.validate() is None


def test_user_validate_guest_fixed_without_price() -> None:
    """Guest with method=fixed but missing price_per_kwh returns 'price_required'."""
    user = User(
        id="sub_302",
        name="Bad Guest",
        type="guest",
        guest_pricing=GuestPricing(method="fixed", price_per_kwh=None),
    )
    assert user.validate() == "price_required"


def test_user_validate_guest_markup_without_factor() -> None:
    """Guest with method=markup but missing markup_factor returns 'markup_required'."""
    user = User(
        id="sub_303",
        name="Bad Markup Guest",
        type="guest",
        guest_pricing=GuestPricing(method="markup", markup_factor=None),
    )
    assert user.validate() == "markup_required"


def test_user_validate_guest_fixed_ok() -> None:
    """Guest with correct fixed pricing validates OK."""
    user = User(
        id="sub_304",
        name="Good Guest",
        type="guest",
        guest_pricing=GuestPricing(method="fixed", price_per_kwh=4.50),
    )
    assert user.validate() is None


# ---------------------------------------------------------------------------
# RfidMapping
# ---------------------------------------------------------------------------


def test_rfid_mapping_from_subentry(mock_rfid_subentry_data: dict) -> None:
    """RfidMapping.from_subentry creates a mapping with correct fields."""
    mapping = RfidMapping.from_subentry(mock_rfid_subentry_data)

    assert mapping.card_index == 0
    assert mapping.card_uid is None
    assert mapping.user_id == "mock_user_subentry_id"
    assert mapping.vehicle_id == "mock_vehicle_subentry_id"
    assert mapping.active is True
    assert mapping.deactivated_by is None


def test_rfid_mapping_to_dict(mock_rfid_subentry_data: dict) -> None:
    """RfidMapping.to_dict produces PRD v1.9 JSON format."""
    mapping = RfidMapping.from_subentry(mock_rfid_subentry_data)
    result = mapping.to_dict()

    assert result == {
        "card_index": 0,
        "card_uid": None,
        "user_id": "mock_user_subentry_id",
        "vehicle_id": "mock_vehicle_subentry_id",
        "active": True,
        "deactivated_by": None,
    }


def test_rfid_mapping_without_vehicle() -> None:
    """RFID mapping without vehicle has vehicle_id=None."""
    data = {
        "card_index": 5,
        "card_uid": None,
        "user_id": "user_abc",
        "active": True,
        "deactivated_by": None,
    }
    mapping = RfidMapping.from_subentry(data)

    assert mapping.vehicle_id is None
    assert mapping.to_dict()["vehicle_id"] is None


def test_rfid_mapping_cascade_deactivated() -> None:
    """RFID mapping with deactivated_by='user_cascade' preserves state."""
    data = {
        "card_index": 2,
        "card_uid": None,
        "user_id": "user_xyz",
        "vehicle_id": "veh_abc",
        "active": False,
        "deactivated_by": "user_cascade",
    }
    mapping = RfidMapping.from_subentry(data)

    assert mapping.active is False
    assert mapping.deactivated_by == "user_cascade"
    result = mapping.to_dict()
    assert result["active"] is False
    assert result["deactivated_by"] == "user_cascade"
