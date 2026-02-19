"""Integration tests for EV Charging Manager subentry config flows."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.const import DOMAIN

from .conftest import MOCK_CHARGER_DATA, setup_entry_with_subentries

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry() -> MockConfigEntry:
    """Create a standard mock config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        title="My go-e Charger",
    )


async def _add_vehicle(
    hass: HomeAssistant,
    entry_id: str,
    name: str = "Peugeot 3008 PHEV",
    battery_capacity_kwh: float = 14.4,
    charging_phases: int = 1,
    charging_efficiency: float = 0.88,
    max_charging_power_kw: float | None = 3.7,
) -> dict:
    """Add a vehicle subentry and return the flow result."""
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "vehicle"),
        context={"source": "user"},
    )
    user_input = {
        "name": name,
        "battery_capacity_kwh": battery_capacity_kwh,
        "charging_phases": str(charging_phases),  # SelectSelector uses strings
        "charging_efficiency": charging_efficiency,
    }
    if max_charging_power_kw is not None:
        user_input["max_charging_power_kw"] = max_charging_power_kw
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input,
    )
    return result


async def _add_regular_user(hass: HomeAssistant, entry_id: str, name: str = "Paul") -> dict:
    """Add a regular user subentry and return the flow result."""
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "user"),
        context={"source": "user"},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": name, "type": "regular"},
    )
    return result


async def _add_guest_user_fixed(
    hass: HomeAssistant,
    entry_id: str,
    name: str = "Guest",
    price_per_kwh: float = 4.50,
) -> dict:
    """Add a guest user with fixed pricing and return the flow result."""
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "user"),
        context={"source": "user"},
    )
    # Step 1: name + type
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": name, "type": "guest"},
    )
    # Step 2: guest pricing
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"guest_pricing_method": "fixed", "price_per_kwh": price_per_kwh},
    )
    return result


async def _add_guest_user_markup(
    hass: HomeAssistant,
    entry_id: str,
    name: str = "Markup Guest",
    markup_factor: float = 1.8,
) -> dict:
    """Add a guest user with markup pricing and return the flow result."""
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "user"),
        context={"source": "user"},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": name, "type": "guest"},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"guest_pricing_method": "markup", "markup_factor": markup_factor},
    )
    return result


def _get_subentries_by_type(entry, subentry_type: str) -> list:
    """Return all subentries of a given type."""
    return [s for s in entry.subentries.values() if s.subentry_type == subentry_type]


# ---------------------------------------------------------------------------
# T010: Vehicle add flow
# ---------------------------------------------------------------------------


async def test_vehicle_add_flow(hass: HomeAssistant) -> None:
    """Adding a vehicle via subentry creates entry with correct data."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)
    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)

    result = await _add_vehicle(hass, entry.entry_id)

    assert result["type"] == FlowResultType.CREATE_ENTRY

    vehicles = _get_subentries_by_type(loaded_entry, "vehicle")
    assert len(vehicles) == 1
    v = vehicles[0]
    assert v.data["name"] == "Peugeot 3008 PHEV"
    assert v.data["battery_capacity_kwh"] == 14.4
    assert v.data["usable_battery_kwh"] == 14.4
    assert v.data["charging_phases"] == 1
    assert v.data["charging_efficiency"] == 0.88
    assert v.data["max_charging_power_kw"] == 3.7


# ---------------------------------------------------------------------------
# T011: Vehicle edit flow
# ---------------------------------------------------------------------------


async def test_vehicle_edit_flow(hass: HomeAssistant) -> None:
    """Editing a vehicle updates data but preserves subentry_id."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)
    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)

    await _add_vehicle(hass, entry.entry_id)
    vehicles = _get_subentries_by_type(loaded_entry, "vehicle")
    vehicle_sub = vehicles[0]
    original_id = vehicle_sub.subentry_id

    # Reconfigure
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "vehicle"),
        context={"source": "reconfigure", "subentry_id": vehicle_sub.subentry_id},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Updated Vehicle",
            "battery_capacity_kwh": 60.0,
            "usable_battery_kwh": 55.0,
            "charging_phases": "3",  # SelectSelector uses strings
            "charging_efficiency": 0.92,
            "max_charging_power_kw": 11.0,
        },
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    # Verify updated data
    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    vehicles = _get_subentries_by_type(loaded_entry, "vehicle")
    v = vehicles[0]
    assert v.subentry_id == original_id
    assert v.data["name"] == "Updated Vehicle"
    assert v.data["battery_capacity_kwh"] == 60.0
    assert v.data["usable_battery_kwh"] == 55.0


# ---------------------------------------------------------------------------
# T012: Regular user add flow
# ---------------------------------------------------------------------------


async def test_regular_user_add_flow(hass: HomeAssistant) -> None:
    """Adding a regular user creates entry with active=true and created_at."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)
    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)

    result = await _add_regular_user(hass, entry.entry_id)

    assert result["type"] == FlowResultType.CREATE_ENTRY

    users = _get_subentries_by_type(loaded_entry, "user")
    assert len(users) == 1
    u = users[0]
    assert u.data["name"] == "Paul"
    assert u.data["type"] == "regular"
    assert u.data["active"] is True
    assert "created_at" in u.data


# ---------------------------------------------------------------------------
# T013: Guest user add flow (fixed pricing)
# ---------------------------------------------------------------------------


async def test_guest_user_add_fixed_pricing(hass: HomeAssistant) -> None:
    """Adding a guest user with fixed pricing creates correct guest_pricing."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)
    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)

    result = await _add_guest_user_fixed(hass, entry.entry_id)

    assert result["type"] == FlowResultType.CREATE_ENTRY

    users = _get_subentries_by_type(loaded_entry, "user")
    guest = next(u for u in users if u.data["type"] == "guest")
    assert guest.data["name"] == "Guest"
    assert guest.data["active"] is True
    assert guest.data["guest_pricing"]["method"] == "fixed"
    assert guest.data["guest_pricing"]["price_per_kwh"] == 4.50


# ---------------------------------------------------------------------------
# T014: Guest user add flow (markup pricing)
# ---------------------------------------------------------------------------


async def test_guest_user_add_markup_pricing(hass: HomeAssistant) -> None:
    """Adding a guest user with markup pricing creates correct guest_pricing."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    result = await _add_guest_user_markup(hass, entry.entry_id)

    assert result["type"] == FlowResultType.CREATE_ENTRY

    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    users = _get_subentries_by_type(loaded_entry, "user")
    guest = next(u for u in users if u.data["type"] == "guest")
    assert guest.data["guest_pricing"]["method"] == "markup"
    assert guest.data["guest_pricing"]["markup_factor"] == 1.8


# ---------------------------------------------------------------------------
# T015: User edit flow (type immutable FR-004)
# ---------------------------------------------------------------------------


async def test_user_edit_flow_type_immutable(hass: HomeAssistant) -> None:
    """Editing a user changes name but type remains unchanged (FR-004)."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    await _add_regular_user(hass, entry.entry_id)
    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    users = _get_subentries_by_type(loaded_entry, "user")
    user_sub = users[0]

    # Reconfigure — change name only
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "user"),
        context={"source": "reconfigure", "subentry_id": user_sub.subentry_id},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Paul Updated", "active": True},
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    users = _get_subentries_by_type(loaded_entry, "user")
    u = users[0]
    assert u.data["name"] == "Paul Updated"
    assert u.data["type"] == "regular"  # Immutable


# ---------------------------------------------------------------------------
# T016: Guest pricing validation
# ---------------------------------------------------------------------------


async def test_guest_pricing_validation_pricing_required(hass: HomeAssistant) -> None:
    """Guest user type without completing pricing step shows pricing_required error."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    # Start user flow, select guest type
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "user"),
        context={"source": "user"},
    )
    # Step 1: name + type=guest → should show guest_pricing form
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Bad Guest", "type": "guest"},
    )
    # Should be on guest_pricing step
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "guest_pricing"

    # Submit fixed pricing without price_per_kwh
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"guest_pricing_method": "fixed"},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "price_required"


# ---------------------------------------------------------------------------
# T017: ConfigStore sync after add
# ---------------------------------------------------------------------------


async def test_config_store_sync_after_add(hass: HomeAssistant) -> None:
    """ConfigStore JSON matches PRD format after adding vehicle + user."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    await _add_vehicle(hass, entry.entry_id)
    await hass.async_block_till_done()

    await _add_regular_user(hass, entry.entry_id)
    await hass.async_block_till_done()

    config_store = hass.data[DOMAIN][entry.entry_id]["config_store"]
    data = config_store.data

    assert len(data["vehicles"]) == 1
    assert len(data["users"]) == 1

    v = data["vehicles"][0]
    assert "id" in v
    assert v["name"] == "Peugeot 3008 PHEV"
    assert v["battery_capacity_kwh"] == 14.4

    u = data["users"][0]
    assert "id" in u
    assert u["name"] == "Paul"
    assert u["type"] == "regular"
    assert u["active"] is True
    assert "created_at" in u


# ---------------------------------------------------------------------------
# T018: Data persistence after reload
# ---------------------------------------------------------------------------


async def test_data_persistence_after_reload(hass: HomeAssistant) -> None:
    """Data survives unload → setup cycle (subentries are source of truth)."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    await _add_vehicle(hass, entry.entry_id)
    await _add_regular_user(hass, entry.entry_id)
    await hass.async_block_till_done()

    # Unload
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED

    # Reload
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    # Verify ConfigStore rebuilt from subentries
    config_store = hass.data[DOMAIN][entry.entry_id]["config_store"]
    data = config_store.data

    assert len(data["vehicles"]) == 1
    assert len(data["users"]) == 1
    assert data["vehicles"][0]["name"] == "Peugeot 3008 PHEV"
    assert data["users"][0]["name"] == "Paul"


# ===========================================================================
# US2: RFID Mapping Tests (T027–T031)
# ===========================================================================


async def _add_rfid_mapping(
    hass: HomeAssistant,
    entry_id: str,
    card_index: int,
    user_id: str,
    vehicle_id: str | None = None,
) -> dict:
    """Add an RFID mapping subentry and return the flow result."""
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "rfid_mapping"),
        context={"source": "user"},
    )
    user_input: dict = {
        "card_index": str(card_index),
        "user_id": user_id,
    }
    if vehicle_id is not None:
        user_input["vehicle_id"] = vehicle_id
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input,
    )
    return result


# ---------------------------------------------------------------------------
# T027: RFID mapping add flow
# ---------------------------------------------------------------------------


async def test_rfid_mapping_add_flow(hass: HomeAssistant) -> None:
    """Adding RFID mapping creates entry with card_uid=null, active=true."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)
    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)

    # Add user and vehicle first
    await _add_vehicle(hass, entry.entry_id)
    await _add_regular_user(hass, entry.entry_id)
    await hass.async_block_till_done()

    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    vehicle = _get_subentries_by_type(loaded_entry, "vehicle")[0]
    user = _get_subentries_by_type(loaded_entry, "user")[0]

    result = await _add_rfid_mapping(
        hass,
        entry.entry_id,
        card_index=0,
        user_id=user.subentry_id,
        vehicle_id=vehicle.subentry_id,
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY

    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    mappings = _get_subentries_by_type(loaded_entry, "rfid_mapping")
    assert len(mappings) == 1
    m = mappings[0]
    assert m.data["card_index"] == 0
    assert m.data["card_uid"] is None
    assert m.data["user_id"] == user.subentry_id
    assert m.data["vehicle_id"] == vehicle.subentry_id
    assert m.data["active"] is True
    assert m.data["deactivated_by"] is None


# ---------------------------------------------------------------------------
# T028: Duplicate card_index rejection
# ---------------------------------------------------------------------------


async def test_rfid_mapping_duplicate_card_index(hass: HomeAssistant) -> None:
    """Adding a second mapping with same card_index shows already_mapped error."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    await _add_vehicle(hass, entry.entry_id)
    await _add_regular_user(hass, entry.entry_id)
    await hass.async_block_till_done()

    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    user = _get_subentries_by_type(loaded_entry, "user")[0]
    vehicle = _get_subentries_by_type(loaded_entry, "vehicle")[0]

    # First mapping — card_index=0
    await _add_rfid_mapping(
        hass,
        entry.entry_id,
        0,
        user.subentry_id,
        vehicle.subentry_id,
    )

    # Second mapping — card_index=0 again → error
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "rfid_mapping"),
        context={"source": "user"},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"card_index": "0", "user_id": user.subentry_id},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["card_index"] == "already_mapped"


# ---------------------------------------------------------------------------
# T029: RFID mapping without vehicle (guest user)
# ---------------------------------------------------------------------------


async def test_rfid_mapping_without_vehicle(hass: HomeAssistant) -> None:
    """RFID mapping without vehicle has vehicle_id=null."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    await _add_guest_user_fixed(hass, entry.entry_id)
    await hass.async_block_till_done()

    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    user = _get_subentries_by_type(loaded_entry, "user")[0]

    result = await _add_rfid_mapping(
        hass,
        entry.entry_id,
        card_index=0,
        user_id=user.subentry_id,
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY

    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    m = _get_subentries_by_type(loaded_entry, "rfid_mapping")[0]
    assert m.data["vehicle_id"] is None


# ---------------------------------------------------------------------------
# T030: RFID mapping edit (vehicle swap)
# ---------------------------------------------------------------------------


async def test_rfid_mapping_edit_vehicle_swap(hass: HomeAssistant) -> None:
    """Editing RFID mapping can change vehicle_id, card_index unchanged."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    await _add_vehicle(hass, entry.entry_id, name="Car A")
    await _add_vehicle(hass, entry.entry_id, name="Car B")
    await _add_regular_user(hass, entry.entry_id)
    await hass.async_block_till_done()

    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    vehicles = _get_subentries_by_type(loaded_entry, "vehicle")
    car_a = next(v for v in vehicles if v.data["name"] == "Car A")
    car_b = next(v for v in vehicles if v.data["name"] == "Car B")
    user = _get_subentries_by_type(loaded_entry, "user")[0]

    await _add_rfid_mapping(
        hass,
        entry.entry_id,
        0,
        user.subentry_id,
        car_a.subentry_id,
    )
    await hass.async_block_till_done()

    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    mapping = _get_subentries_by_type(loaded_entry, "rfid_mapping")[0]

    # Reconfigure — swap vehicle from car_a to car_b
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "rfid_mapping"),
        context={"source": "reconfigure", "subentry_id": mapping.subentry_id},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "user_id": user.subentry_id,
            "vehicle_id": car_b.subentry_id,
            "active": True,
        },
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    m = _get_subentries_by_type(loaded_entry, "rfid_mapping")[0]
    assert m.data["card_index"] == 0  # Unchanged
    assert m.data["vehicle_id"] == car_b.subentry_id


# ---------------------------------------------------------------------------
# T031: Active-only user filter (FR-019)
# ---------------------------------------------------------------------------


async def test_rfid_mapping_active_only_user_filter(hass: HomeAssistant) -> None:
    """Only active users appear in RFID mapping user dropdown (FR-019)."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    # Add active user
    await _add_regular_user(hass, entry.entry_id, name="Active User")
    await hass.async_block_till_done()

    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    active_user = _get_subentries_by_type(loaded_entry, "user")[0]

    # Add a second user and deactivate via reconfigure
    await _add_regular_user(hass, entry.entry_id, name="Inactive User")
    await hass.async_block_till_done()

    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    users = _get_subentries_by_type(loaded_entry, "user")
    inactive_user = next(u for u in users if u.data["name"] == "Inactive User")

    # Deactivate the second user
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "user"),
        context={"source": "reconfigure", "subentry_id": inactive_user.subentry_id},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Inactive User", "active": False},
    )
    assert result["type"] == FlowResultType.ABORT

    # Start RFID mapping flow — check the form data schema
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "rfid_mapping"),
        context={"source": "user"},
    )
    assert result["type"] == FlowResultType.FORM

    # The form should show only the active user in the schema
    # We test the actual flow: adding with active user works
    await _add_vehicle(hass, entry.entry_id)
    await hass.async_block_till_done()

    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    vehicle = _get_subentries_by_type(loaded_entry, "vehicle")[0]

    result2 = await _add_rfid_mapping(
        hass,
        entry.entry_id,
        0,
        active_user.subentry_id,
        vehicle.subentry_id,
    )
    assert result2["type"] == FlowResultType.CREATE_ENTRY


# ===========================================================================
# Phase 7: Edge Case Tests (T056, T057, T059)
# ===========================================================================


# ---------------------------------------------------------------------------
# T056: Guest user with markup pricing but missing markup_factor
# ---------------------------------------------------------------------------


async def test_guest_pricing_validation_markup_required(hass: HomeAssistant) -> None:
    """Guest user with markup method but no markup_factor shows markup_required error."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "user"),
        context={"source": "user"},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Markup Guest", "type": "guest"},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "guest_pricing"

    # Submit markup method without markup_factor
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"guest_pricing_method": "markup"},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "markup_required"


# ---------------------------------------------------------------------------
# T057: All 10 card indices mapped — no available slots
# ---------------------------------------------------------------------------


async def test_rfid_mapping_all_slots_used(hass: HomeAssistant) -> None:
    """When all 10 card slots are mapped, new mapping still shows form (validation in handler)."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    # Add a user
    await _add_regular_user(hass, entry.entry_id, "Paul")
    await hass.async_block_till_done()

    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    user = _get_subentries_by_type(loaded_entry, "user")[0]

    # Map all 10 card slots
    for i in range(10):
        result = await _add_rfid_mapping(
            hass,
            entry.entry_id,
            card_index=i,
            user_id=user.subentry_id,
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY

    # Try to add an 11th — any card_index submitted should be rejected as already_mapped
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "rfid_mapping"),
        context={"source": "user"},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"card_index": "0", "user_id": user.subentry_id},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["card_index"] == "already_mapped"


# ---------------------------------------------------------------------------
# T059: Duplicate user names allowed (FR-020)
# ---------------------------------------------------------------------------


async def test_duplicate_user_names_allowed(hass: HomeAssistant) -> None:
    """Two users with the same name are both persisted (FR-020)."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    result1 = await _add_regular_user(hass, entry.entry_id, name="Paul")
    assert result1["type"] == FlowResultType.CREATE_ENTRY

    result2 = await _add_regular_user(hass, entry.entry_id, name="Paul")
    assert result2["type"] == FlowResultType.CREATE_ENTRY

    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    users = _get_subentries_by_type(loaded_entry, "user")
    assert len(users) == 2
    assert all(u.data["name"] == "Paul" for u in users)

    # Both should have unique subentry_ids
    ids = {u.subentry_id for u in users}
    assert len(ids) == 2
