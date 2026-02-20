"""Tests for RfidLookup (T005)."""

from __future__ import annotations

from custom_components.ev_charging_manager.rfid_lookup import RfidLookup

# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------

VEHICLE_PEUGEOT = {
    "id": "v_peugeot",
    "name": "Peugeot 3008 PHEV",
    "battery_capacity_kwh": 14.4,
    "charging_efficiency": 0.88,
}

USER_PETRA = {
    "id": "u_petra",
    "name": "Petra",
    "type": "regular",
    "active": True,
}

MAPPING_PETRA_CARD1 = {
    "card_index": 1,  # trx=2 → index 1
    "user_id": "u_petra",
    "vehicle_id": "v_peugeot",
    "active": True,
}

MAPPING_INACTIVE = {
    "card_index": 2,  # trx=3 → index 2
    "user_id": "u_petra",
    "vehicle_id": "v_peugeot",
    "active": False,
}


def make_lookup(mappings=None, users=None, vehicles=None) -> RfidLookup:
    """Build a RfidLookup with optional test data."""
    return RfidLookup(
        {
            "rfid_mappings": mappings or [],
            "users": users or [],
            "vehicles": vehicles or [],
        }
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_trx_none_returns_none():
    """trx=None means no session — return None."""
    lookup = make_lookup()
    assert lookup.resolve(None) is None


def test_trx_zero_returns_unknown_no_rfid():
    """trx=0 means no card used — return Unknown/no_rfid."""
    lookup = make_lookup()
    result = lookup.resolve(0)
    assert result is not None
    assert result.user_name == "Unknown"
    assert result.user_type == "unknown"
    assert result.reason == "no_rfid"
    assert result.rfid_index is None
    assert result.vehicle_name is None


def test_trx_zero_as_string():
    """trx='0' (string) same as trx=0."""
    lookup = make_lookup()
    result = lookup.resolve("0")
    assert result is not None
    assert result.reason == "no_rfid"


def test_normal_user_resolution():
    """trx=2 resolves to index 1 → Petra with Peugeot."""
    lookup = make_lookup(
        mappings=[MAPPING_PETRA_CARD1],
        users=[USER_PETRA],
        vehicles=[VEHICLE_PEUGEOT],
    )
    result = lookup.resolve(2)
    assert result is not None
    assert result.user_name == "Petra"
    assert result.user_type == "regular"
    assert result.vehicle_name == "Peugeot 3008 PHEV"
    assert result.vehicle_battery_kwh == 14.4
    assert result.efficiency_factor == 0.88
    assert result.rfid_index == 1
    assert result.reason == "matched"


def test_type_agnostic_int_vs_string():
    """trx=2 (int) and trx='2' (str) produce the same result."""
    lookup = make_lookup(
        mappings=[MAPPING_PETRA_CARD1],
        users=[USER_PETRA],
        vehicles=[VEHICLE_PEUGEOT],
    )
    result_int = lookup.resolve(2)
    result_str = lookup.resolve("2")
    assert result_int is not None
    assert result_str is not None
    assert result_int.user_name == result_str.user_name
    assert result_int.rfid_index == result_str.rfid_index
    assert result_int.reason == result_str.reason


def test_unmapped_index_returns_unknown(caplog):
    """trx=5 with no mapping for index 4 → Unknown/unmapped + warning."""
    import logging

    lookup = make_lookup()
    with caplog.at_level(logging.WARNING):
        result = lookup.resolve(5)
    assert result is not None
    assert result.user_name == "Unknown"
    assert result.reason == "unmapped"
    assert result.rfid_index == 4
    assert "No RFID mapping found" in caplog.text


def test_inactive_mapping_returns_unknown(caplog):
    """Inactive RFID card → Unknown/rfid_inactive + warning."""
    import logging

    lookup = make_lookup(
        mappings=[MAPPING_INACTIVE],
        users=[USER_PETRA],
        vehicles=[VEHICLE_PEUGEOT],
    )
    with caplog.at_level(logging.WARNING):
        result = lookup.resolve(3)  # index=2 = MAPPING_INACTIVE
    assert result is not None
    assert result.user_name == "Unknown"
    assert result.reason == "rfid_inactive"
    assert "inactive" in caplog.text


def test_unexpected_format_returns_unknown(caplog):
    """trx='abc' is unexpected format → Unknown + warning."""
    import logging

    lookup = make_lookup()
    with caplog.at_level(logging.WARNING):
        result = lookup.resolve("abc")
    assert result is not None
    assert result.user_name == "Unknown"
    assert result.reason == "unmapped"
    assert "Unexpected trx value format" in caplog.text


def test_user_with_no_vehicle():
    """Mapping with no vehicle_id → user resolved, vehicle=None."""
    mapping_no_vehicle = {
        "card_index": 0,
        "user_id": "u_petra",
        "vehicle_id": None,
        "active": True,
    }
    lookup = make_lookup(
        mappings=[mapping_no_vehicle],
        users=[USER_PETRA],
        vehicles=[],
    )
    result = lookup.resolve(1)  # index=0
    assert result is not None
    assert result.user_name == "Petra"
    assert result.vehicle_name is None
    assert result.vehicle_battery_kwh is None
    assert result.reason == "matched"


def test_all_valid_indices_1_to_10():
    """All valid RFID card indices (trx=1..10, index=0..9) resolve correctly."""
    mappings = [
        {"card_index": i, "user_id": "u_petra", "vehicle_id": None, "active": True}
        for i in range(10)
    ]
    lookup = make_lookup(mappings=mappings, users=[USER_PETRA])
    for trx in range(1, 11):
        result = lookup.resolve(trx)
        assert result is not None
        assert result.rfid_index == trx - 1
        assert result.reason == "matched"
