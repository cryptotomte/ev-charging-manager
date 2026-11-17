"""Tests for EV Charging Manager lifecycle operations (deactivate/reactivate/delete)."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.const import (
    CONF_CABLE_LOCK_ENTITY,
    CONF_ERROR_ENTITY,
    CONF_ETO_ENTITY,
    CONF_MODEL_STATUS_ENTITY,
    CONF_PLUG_ENTITY,
    CONF_TOTAL_ENERGY_ENTITY,
    DOMAIN,
)

from .conftest import (
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    MOCK_PLUG_ENTITY,
    setup_entry_with_subentries,
    setup_session_engine,
    start_profile_session,
    stop_profile_session,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")


def _get_subentries_by_type(entry, subentry_type: str) -> list:
    return [s for s in entry.subentries.values() if s.subentry_type == subentry_type]


async def _add_vehicle(hass, entry_id, name="Car A"):
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "vehicle"),
        context={"source": "user"},
    )
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": name,
            "battery_capacity_kwh": 60.0,
            "charging_phases": "3",
            "charging_efficiency": 0.90,
        },
    )


async def _add_user(hass, entry_id, name="Paul"):
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "user"),
        context={"source": "user"},
    )
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": name, "type": "regular"},
    )


async def _add_rfid(hass, entry_id, card_index, user_id, vehicle_id=None):
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "rfid_mapping"),
        context={"source": "user"},
    )
    user_input = {"card_index": str(card_index), "user_id": user_id}
    if vehicle_id:
        user_input["vehicle_id"] = vehicle_id
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input,
    )


async def _reconfigure_user(hass, entry_id, subentry_id, name, active):
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "user"),
        context={"source": "reconfigure", "subentry_id": subentry_id},
    )
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": name, "active": active},
    )


async def _reconfigure_rfid(hass, entry_id, subentry_id, user_id, active, vehicle_id=None):
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "rfid_mapping"),
        context={"source": "reconfigure", "subentry_id": subentry_id},
    )
    user_input = {"user_id": user_id, "active": active}
    if vehicle_id:
        user_input["vehicle_id"] = vehicle_id
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input,
    )


# ---------------------------------------------------------------------------
# T037: User deactivation cascade
# ---------------------------------------------------------------------------


async def test_user_deactivation_cascade(hass: HomeAssistant) -> None:
    """Deactivating user cascades to RFID mappings: active=false, deactivated_by=user_cascade."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    await _add_vehicle(hass, entry.entry_id)
    await _add_user(hass, entry.entry_id, "Paul")
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    user = _get_subentries_by_type(loaded, "user")[0]
    vehicle = _get_subentries_by_type(loaded, "vehicle")[0]

    # Add 2 RFID mappings for this user
    await _add_rfid(hass, entry.entry_id, 0, user.subentry_id, vehicle.subentry_id)
    await _add_rfid(hass, entry.entry_id, 1, user.subentry_id, vehicle.subentry_id)
    await hass.async_block_till_done()

    # Deactivate user
    result = await _reconfigure_user(hass, entry.entry_id, user.subentry_id, "Paul", False)
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    # Verify cascade
    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    user_sub = loaded.subentries[user.subentry_id]
    assert user_sub.data["active"] is False
    assert user_sub.title == "Paul (inactive)"

    mappings = _get_subentries_by_type(loaded, "rfid_mapping")
    assert len(mappings) == 2
    for m in mappings:
        assert m.data["active"] is False
        assert m.data["deactivated_by"] == "user_cascade"


# ---------------------------------------------------------------------------
# T038: User reactivation cascade
# ---------------------------------------------------------------------------


async def test_user_reactivation_cascade(hass: HomeAssistant) -> None:
    """Reactivating user restores cascade-deactivated mappings."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    await _add_vehicle(hass, entry.entry_id)
    await _add_user(hass, entry.entry_id, "Paul")
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    user = _get_subentries_by_type(loaded, "user")[0]
    vehicle = _get_subentries_by_type(loaded, "vehicle")[0]

    await _add_rfid(hass, entry.entry_id, 0, user.subentry_id, vehicle.subentry_id)
    await hass.async_block_till_done()

    # Deactivate then reactivate
    await _reconfigure_user(hass, entry.entry_id, user.subentry_id, "Paul", False)
    await hass.async_block_till_done()
    await _reconfigure_user(hass, entry.entry_id, user.subentry_id, "Paul", True)
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    user_sub = loaded.subentries[user.subentry_id]
    assert user_sub.data["active"] is True
    assert user_sub.title == "Paul"

    m = _get_subentries_by_type(loaded, "rfid_mapping")[0]
    assert m.data["active"] is True
    assert m.data["deactivated_by"] is None


# ---------------------------------------------------------------------------
# T039: Selective reactivation (FR-009)
# ---------------------------------------------------------------------------


async def test_selective_reactivation_fr009(hass: HomeAssistant) -> None:
    """Individually deactivated mapping stays inactive after user reactivation (FR-009)."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    await _add_vehicle(hass, entry.entry_id)
    await _add_user(hass, entry.entry_id, "Paul")
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    user = _get_subentries_by_type(loaded, "user")[0]
    vehicle = _get_subentries_by_type(loaded, "vehicle")[0]

    await _add_rfid(hass, entry.entry_id, 0, user.subentry_id, vehicle.subentry_id)
    await _add_rfid(hass, entry.entry_id, 1, user.subentry_id, vehicle.subentry_id)
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    mappings = _get_subentries_by_type(loaded, "rfid_mapping")
    mapping_0 = next(m for m in mappings if m.data["card_index"] == 0)
    mapping_1 = next(m for m in mappings if m.data["card_index"] == 1)

    # Individually deactivate mapping_1
    await _reconfigure_rfid(
        hass,
        entry.entry_id,
        mapping_1.subentry_id,
        user.subentry_id,
        active=False,
        vehicle_id=vehicle.subentry_id,
    )
    await hass.async_block_till_done()

    # Deactivate user (cascade)
    await _reconfigure_user(hass, entry.entry_id, user.subentry_id, "Paul", False)
    await hass.async_block_till_done()

    # Reactivate user
    await _reconfigure_user(hass, entry.entry_id, user.subentry_id, "Paul", True)
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    m0 = loaded.subentries[mapping_0.subentry_id]
    m1 = loaded.subentries[mapping_1.subentry_id]

    # mapping_0: was cascade-deactivated → should be restored
    assert m0.data["active"] is True
    assert m0.data["deactivated_by"] is None

    # mapping_1: was individually deactivated BEFORE cascade → stays inactive
    assert m1.data["active"] is False
    assert m1.data["deactivated_by"] == "individual"


# ---------------------------------------------------------------------------
# Spec 026 D-11: guest deactivation cascades like a regular user's
# ---------------------------------------------------------------------------

_GUEST_PRICING = {"guest_pricing_method": "fixed", "price_per_kwh": 4.5}


async def _add_guest(hass, entry_id, name="Visitor"):
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "user"),
        context={"source": "user"},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": name, "type": "guest"}
    )
    return await hass.config_entries.subentries.async_configure(result["flow_id"], _GUEST_PRICING)


async def _reconfigure_guest(hass, entry_id, subentry_id, active, pricing=_GUEST_PRICING):
    result = await _reconfigure_user(hass, entry_id, subentry_id, "Visitor", active)
    assert result["step_id"] == "reconfigure_guest_pricing"
    return await hass.config_entries.subentries.async_configure(result["flow_id"], pricing)


async def _guest_with_two_mappings(hass):
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)
    await _add_guest(hass, entry.entry_id)
    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    guest = _get_subentries_by_type(loaded, "user")[0]
    await _add_rfid(hass, entry.entry_id, 0, guest.subentry_id)
    await _add_rfid(hass, entry.entry_id, 1, guest.subentry_id)
    await hass.async_block_till_done()
    return entry, guest


async def test_guest_deactivation_cascade(hass: HomeAssistant) -> None:
    """T-09: deactivating a guest deactivates its mappings with user_cascade."""
    entry, guest = await _guest_with_two_mappings(hass)

    result = await _reconfigure_guest(hass, entry.entry_id, guest.subentry_id, False)
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT
    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    assert loaded.subentries[guest.subentry_id].data["active"] is False
    mappings = _get_subentries_by_type(loaded, "rfid_mapping")
    assert len(mappings) == 2
    for m in mappings:
        assert m.data["active"] is False
        assert m.data["deactivated_by"] == "user_cascade"


async def test_guest_reactivation_restores_only_cascaded_mappings(hass: HomeAssistant) -> None:
    """T-10: reactivating a guest restores user_cascade mappings, not individual ones."""
    entry, guest = await _guest_with_two_mappings(hass)
    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    mappings = _get_subentries_by_type(loaded, "rfid_mapping")
    mapping_0 = next(m for m in mappings if m.data["card_index"] == 0)
    mapping_1 = next(m for m in mappings if m.data["card_index"] == 1)
    await _reconfigure_rfid(
        hass, entry.entry_id, mapping_1.subentry_id, guest.subentry_id, active=False
    )

    await _reconfigure_guest(hass, entry.entry_id, guest.subentry_id, False)
    await _reconfigure_guest(hass, entry.entry_id, guest.subentry_id, True)
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    assert loaded.subentries[mapping_0.subentry_id].data["active"] is True
    assert loaded.subentries[mapping_0.subentry_id].data["deactivated_by"] is None
    assert loaded.subentries[mapping_1.subentry_id].data["active"] is False
    assert loaded.subentries[mapping_1.subentry_id].data["deactivated_by"] == "individual"


async def test_guest_pricing_error_changes_no_mapping(hass: HomeAssistant) -> None:
    """T-11: a pricing validation error leaves every mapping as it was."""
    entry, guest = await _guest_with_two_mappings(hass)

    result = await _reconfigure_guest(
        hass, entry.entry_id, guest.subentry_id, False, {"guest_pricing_method": "fixed"}
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "price_required"}
    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    assert loaded.subentries[guest.subentry_id].data["active"] is True
    for m in _get_subentries_by_type(loaded, "rfid_mapping"):
        assert m.data["active"] is True
        assert m.data["deactivated_by"] is None


# ---------------------------------------------------------------------------
# T040: Individual RFID mapping deactivation
# ---------------------------------------------------------------------------


async def test_individual_rfid_deactivation(hass: HomeAssistant) -> None:
    """Individually deactivating RFID mapping sets deactivated_by=individual."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    await _add_vehicle(hass, entry.entry_id)
    await _add_user(hass, entry.entry_id, "Paul")
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    user = _get_subentries_by_type(loaded, "user")[0]
    vehicle = _get_subentries_by_type(loaded, "vehicle")[0]

    await _add_rfid(hass, entry.entry_id, 0, user.subentry_id, vehicle.subentry_id)
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    mapping = _get_subentries_by_type(loaded, "rfid_mapping")[0]

    result = await _reconfigure_rfid(
        hass,
        entry.entry_id,
        mapping.subentry_id,
        user.subentry_id,
        active=False,
        vehicle_id=vehicle.subentry_id,
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    m = loaded.subentries[mapping.subentry_id]
    assert m.data["active"] is False
    assert m.data["deactivated_by"] == "individual"

    # User should be unaffected
    u = loaded.subentries[user.subentry_id]
    assert u.data["active"] is True


# ---------------------------------------------------------------------------
# T041: Individual RFID mapping reactivation
# ---------------------------------------------------------------------------


async def test_individual_rfid_reactivation(hass: HomeAssistant) -> None:
    """Reactivating individually deactivated mapping clears deactivated_by."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    await _add_vehicle(hass, entry.entry_id)
    await _add_user(hass, entry.entry_id, "Paul")
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    user = _get_subentries_by_type(loaded, "user")[0]
    vehicle = _get_subentries_by_type(loaded, "vehicle")[0]

    await _add_rfid(hass, entry.entry_id, 0, user.subentry_id, vehicle.subentry_id)
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    mapping = _get_subentries_by_type(loaded, "rfid_mapping")[0]

    # Deactivate
    await _reconfigure_rfid(
        hass,
        entry.entry_id,
        mapping.subentry_id,
        user.subentry_id,
        active=False,
        vehicle_id=vehicle.subentry_id,
    )
    await hass.async_block_till_done()

    # Reactivate
    result = await _reconfigure_rfid(
        hass,
        entry.entry_id,
        mapping.subentry_id,
        user.subentry_id,
        active=True,
        vehicle_id=vehicle.subentry_id,
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    m = loaded.subentries[mapping.subentry_id]
    assert m.data["active"] is True
    assert m.data["deactivated_by"] is None


# ---------------------------------------------------------------------------
# T042: ConfigStore sync after deactivation/reactivation
# ---------------------------------------------------------------------------


async def test_config_store_sync_after_deactivation(hass: HomeAssistant) -> None:
    """ConfigStore JSON updated after deactivation and reactivation."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    await _add_vehicle(hass, entry.entry_id)
    await _add_user(hass, entry.entry_id, "Paul")
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    user = _get_subentries_by_type(loaded, "user")[0]
    vehicle = _get_subentries_by_type(loaded, "vehicle")[0]

    await _add_rfid(hass, entry.entry_id, 0, user.subentry_id, vehicle.subentry_id)
    await hass.async_block_till_done()

    store = hass.data[DOMAIN][entry.entry_id]["config_store"]

    # Deactivate user
    await _reconfigure_user(hass, entry.entry_id, user.subentry_id, "Paul", False)
    await hass.async_block_till_done()

    data = store.data
    assert data["users"][0]["active"] is False
    assert data["rfid_mappings"][0]["active"] is False
    assert data["rfid_mappings"][0]["deactivated_by"] == "user_cascade"

    # Reactivate user
    await _reconfigure_user(hass, entry.entry_id, user.subentry_id, "Paul", True)
    await hass.async_block_till_done()

    data = store.data
    assert data["users"][0]["active"] is True
    assert data["rfid_mappings"][0]["active"] is True
    assert data["rfid_mappings"][0]["deactivated_by"] is None


# ===========================================================================
# US4: Permanent Delete Tests (T047–T051)
# ===========================================================================


# ---------------------------------------------------------------------------
# T047: Permanent user deletion cascade
# ---------------------------------------------------------------------------


async def test_permanent_user_deletion_cascade(hass: HomeAssistant) -> None:
    """Deleting user removes user + all associated RFID mappings."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    await _add_vehicle(hass, entry.entry_id)
    await _add_user(hass, entry.entry_id, "Paul")
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    user = _get_subentries_by_type(loaded, "user")[0]
    vehicle = _get_subentries_by_type(loaded, "vehicle")[0]

    await _add_rfid(hass, entry.entry_id, 0, user.subentry_id, vehicle.subentry_id)
    await _add_rfid(hass, entry.entry_id, 1, user.subentry_id, vehicle.subentry_id)
    await hass.async_block_till_done()

    # Remove user subentry via HA API
    hass.config_entries.async_remove_subentry(entry, user.subentry_id)
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    users = _get_subentries_by_type(loaded, "user")
    mappings = _get_subentries_by_type(loaded, "rfid_mapping")
    vehicles = _get_subentries_by_type(loaded, "vehicle")

    assert len(users) == 0
    assert len(mappings) == 0  # Both mappings removed by cascade
    assert len(vehicles) == 1  # Vehicle unaffected


# ---------------------------------------------------------------------------
# T048: Permanent vehicle deletion cascade
# ---------------------------------------------------------------------------


async def test_permanent_vehicle_deletion_cascade(hass: HomeAssistant) -> None:
    """Deleting vehicle nullifies vehicle_id on associated RFID mappings."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    await _add_vehicle(hass, entry.entry_id, "Shared Car")
    await _add_user(hass, entry.entry_id, "Paul")
    await _add_user(hass, entry.entry_id, "Anna")
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    vehicle = _get_subentries_by_type(loaded, "vehicle")[0]
    users = _get_subentries_by_type(loaded, "user")
    paul = next(u for u in users if u.data["name"] == "Paul")
    anna = next(u for u in users if u.data["name"] == "Anna")

    await _add_rfid(hass, entry.entry_id, 0, paul.subentry_id, vehicle.subentry_id)
    await _add_rfid(hass, entry.entry_id, 1, anna.subentry_id, vehicle.subentry_id)
    await hass.async_block_till_done()

    # Remove vehicle
    hass.config_entries.async_remove_subentry(entry, vehicle.subentry_id)
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    vehicles = _get_subentries_by_type(loaded, "vehicle")
    mappings = _get_subentries_by_type(loaded, "rfid_mapping")

    assert len(vehicles) == 0
    assert len(mappings) == 2  # Mappings still exist
    for m in mappings:
        assert m.data["vehicle_id"] is None  # vehicle_id nullified


# ---------------------------------------------------------------------------
# T049: Permanent RFID mapping deletion (no cascade)
# ---------------------------------------------------------------------------


async def test_permanent_rfid_deletion_no_cascade(hass: HomeAssistant) -> None:
    """Deleting RFID mapping leaves user and vehicle unaffected."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    await _add_vehicle(hass, entry.entry_id)
    await _add_user(hass, entry.entry_id, "Paul")
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    user = _get_subentries_by_type(loaded, "user")[0]
    vehicle = _get_subentries_by_type(loaded, "vehicle")[0]

    await _add_rfid(hass, entry.entry_id, 0, user.subentry_id, vehicle.subentry_id)
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    mapping = _get_subentries_by_type(loaded, "rfid_mapping")[0]

    hass.config_entries.async_remove_subentry(entry, mapping.subentry_id)
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    assert len(_get_subentries_by_type(loaded, "rfid_mapping")) == 0
    assert len(_get_subentries_by_type(loaded, "user")) == 1
    assert len(_get_subentries_by_type(loaded, "vehicle")) == 1

    # ConfigStore synced
    store = hass.data[DOMAIN][entry.entry_id]["config_store"]
    assert len(store.data["rfid_mappings"]) == 0


# ---------------------------------------------------------------------------
# T050: ConfigStore sync after deletion cascade
# ---------------------------------------------------------------------------


async def test_config_store_sync_after_deletion(hass: HomeAssistant) -> None:
    """ConfigStore JSON updated after user and vehicle delete cascades."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    await _add_vehicle(hass, entry.entry_id)
    await _add_user(hass, entry.entry_id, "Paul")
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    user = _get_subentries_by_type(loaded, "user")[0]
    vehicle = _get_subentries_by_type(loaded, "vehicle")[0]

    await _add_rfid(hass, entry.entry_id, 0, user.subentry_id, vehicle.subentry_id)
    await hass.async_block_till_done()

    store = hass.data[DOMAIN][entry.entry_id]["config_store"]

    # Delete user → cascade removes mapping
    hass.config_entries.async_remove_subentry(entry, user.subentry_id)
    await hass.async_block_till_done()

    assert len(store.data["users"]) == 0
    assert len(store.data["rfid_mappings"]) == 0
    assert len(store.data["vehicles"]) == 1


# ---------------------------------------------------------------------------
# T051: Vehicle deletion with mappings from different users
# ---------------------------------------------------------------------------


async def test_vehicle_deletion_multi_user_mappings(hass: HomeAssistant) -> None:
    """Deleting shared vehicle nullifies vehicle_id on mappings from different users."""
    entry = _make_entry()
    await setup_entry_with_subentries(hass, entry)

    await _add_vehicle(hass, entry.entry_id, "Shared Car")
    await _add_user(hass, entry.entry_id, "Paul")
    await _add_user(hass, entry.entry_id, "Anna")
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    vehicle = _get_subentries_by_type(loaded, "vehicle")[0]
    users = _get_subentries_by_type(loaded, "user")
    paul = next(u for u in users if u.data["name"] == "Paul")
    anna = next(u for u in users if u.data["name"] == "Anna")

    await _add_rfid(hass, entry.entry_id, 0, paul.subentry_id, vehicle.subentry_id)
    await _add_rfid(hass, entry.entry_id, 1, anna.subentry_id, vehicle.subentry_id)
    await hass.async_block_till_done()

    # Delete vehicle
    hass.config_entries.async_remove_subentry(entry, vehicle.subentry_id)
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    mappings = _get_subentries_by_type(loaded, "rfid_mapping")

    assert len(mappings) == 2
    paul_mapping = next(m for m in mappings if m.data["user_id"] == paul.subentry_id)
    anna_mapping = next(m for m in mappings if m.data["user_id"] == anna.subentry_id)

    assert paul_mapping.data["vehicle_id"] is None
    assert anna_mapping.data["vehicle_id"] is None


# ---------------------------------------------------------------------------
# T-MIG-01 through T-MIG-04 (PR-20): Observation-slot migration
# ---------------------------------------------------------------------------


async def test_mig_01_fresh_goe_install_populates_observation_slots(
    hass: HomeAssistant,
) -> None:
    """T-MIG-01: Fresh go-e install has observation slots in entry.options after setup."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini", "charger_serial": "S"},
        options={},
        title="Test Charger",
    )
    await setup_entry_with_subentries(hass, entry)
    loaded = hass.config_entries.async_get_entry(entry.entry_id)

    # goe_gemini profile with serial "S" should yield these entity IDs
    assert loaded.options.get(CONF_PLUG_ENTITY) == "binary_sensor.goe_S_car_0"
    assert loaded.options.get(CONF_CABLE_LOCK_ENTITY) == "sensor.goe_S_cus_value"
    assert loaded.options.get(CONF_MODEL_STATUS_ENTITY) == "sensor.goe_S_modelstatus_value"
    assert loaded.options.get(CONF_ERROR_ENTITY) == "sensor.goe_S_err_value"


async def test_mig_02_pre_pr20_entry_gets_observation_slots_on_setup(
    hass: HomeAssistant,
) -> None:
    """T-MIG-02: Pre-PR-20 entry with go-e profile + serial migrates on async_setup_entry."""
    # Simulate a pre-PR-20 entry: options has no observation keys
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini", "charger_serial": "abc123"},
        options={"min_session_duration_s": 60},
        title="Test Charger",
    )
    await setup_entry_with_subentries(hass, entry)
    loaded = hass.config_entries.async_get_entry(entry.entry_id)

    # Migration must have populated all four slots
    assert loaded.options.get(CONF_PLUG_ENTITY) == "binary_sensor.goe_abc123_car_0"
    assert loaded.options.get(CONF_CABLE_LOCK_ENTITY) == "sensor.goe_abc123_cus_value"
    assert loaded.options.get(CONF_MODEL_STATUS_ENTITY) == "sensor.goe_abc123_modelstatus_value"
    assert loaded.options.get(CONF_ERROR_ENTITY) == "sensor.goe_abc123_err_value"

    # Pre-existing options must be preserved
    assert loaded.options.get("min_session_duration_s") == 60


async def test_mig_03_pre_existing_observation_slot_not_overwritten(
    hass: HomeAssistant,
) -> None:
    """T-MIG-03: A custom value set before migration must NOT be overwritten."""
    custom_plug = "binary_sensor.my_custom_plug"
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini", "charger_serial": "abc123"},
        options={CONF_PLUG_ENTITY: custom_plug},
        title="Test Charger",
    )
    await setup_entry_with_subentries(hass, entry)
    loaded = hass.config_entries.async_get_entry(entry.entry_id)

    # Custom plug value must be preserved
    assert loaded.options.get(CONF_PLUG_ENTITY) == custom_plug

    # Other three must be populated from profile
    assert loaded.options.get(CONF_CABLE_LOCK_ENTITY) == "sensor.goe_abc123_cus_value"
    assert loaded.options.get(CONF_MODEL_STATUS_ENTITY) == "sensor.goe_abc123_modelstatus_value"
    assert loaded.options.get(CONF_ERROR_ENTITY) == "sensor.goe_abc123_err_value"


async def test_mig_04_generic_profile_no_observation_migration(
    hass: HomeAssistant,
) -> None:
    """T-MIG-04: Generic profile entry must not have observation slots injected."""
    generic_data = {**MOCK_CHARGER_DATA, "charger_profile": "generic"}
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=generic_data,
        options={},
        title="Generic Charger",
    )
    await setup_entry_with_subentries(hass, entry)
    loaded = hass.config_entries.async_get_entry(entry.entry_id)

    # Generic profile has no observation entity patterns — options must stay empty
    assert loaded.options.get(CONF_PLUG_ENTITY) is None
    assert loaded.options.get(CONF_CABLE_LOCK_ENTITY) is None
    assert loaded.options.get(CONF_MODEL_STATUS_ENTITY) is None
    assert loaded.options.get(CONF_ERROR_ENTITY) is None


async def test_mig_05_migration_idempotent_on_second_setup(
    hass: HomeAssistant,
) -> None:
    """T-MIG-05: Running migration twice (setup → unload → setup) leaves options unchanged."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_serial": "xyz99"},
        options={"min_session_duration_s": 60},
        title="Idempotency Test",
    )
    await setup_entry_with_subentries(hass, entry)
    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    options_after_first_setup = dict(loaded.options)

    # Unload then reload (second migration run)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    options_after_second_setup = dict(loaded.options)

    assert options_after_second_setup == options_after_first_setup, (
        "Migration must be idempotent: options must not change on second setup. "
        f"First: {options_after_first_setup}, Second: {options_after_second_setup}"
    )


async def test_mig_06_missing_serial_slots_unset(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T-MIG-06: go-e entry missing charger_serial skips observation slots and warns."""
    # go-e gemini entry without serial
    data_no_serial = {k: v for k, v in MOCK_CHARGER_DATA.items() if k != "charger_serial"}
    data_no_serial["charger_profile"] = "goe_gemini"

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data_no_serial,
        options={},
        title="No Serial Charger",
    )

    with caplog.at_level(logging.WARNING, logger="custom_components.ev_charging_manager"):
        await setup_entry_with_subentries(hass, entry)

    loaded = hass.config_entries.async_get_entry(entry.entry_id)

    # Slots must NOT be populated with placeholder strings
    for key in (
        CONF_PLUG_ENTITY,
        CONF_CABLE_LOCK_ENTITY,
        CONF_MODEL_STATUS_ENTITY,
        CONF_ERROR_ENTITY,
    ):
        val = loaded.options.get(key)
        assert val is None or "{serial}" not in str(val), (
            f"Slot {key} must not contain literal '{{serial}}', got: {val!r}"
        )

    # Warning must have been emitted
    assert any(
        "charger serial missing" in r.message or "skipped" in r.message for r in caplog.records
    ), "Expected warning about missing charger serial"


# ---------------------------------------------------------------------------
# Spec 026 D-18: eto_entity seeded from the total-energy entity of first setup
# ---------------------------------------------------------------------------

_ETO = "sensor.charger_total_energy"


def _eto_entry(profile: str, total_energy_entity: str | None, options: dict) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            **MOCK_CHARGER_DATA,
            "charger_profile": profile,
            CONF_TOTAL_ENERGY_ENTITY: total_energy_entity,
        },
        options=options,
        title="Test Charger",
    )


async def test_eto_seeded_from_total_energy_entity(hass: HomeAssistant) -> None:
    """T-43: on goe_gemini the option is written with the slot migration; a session uses it."""
    entry = _eto_entry(
        "goe_gemini", _ETO, {"min_session_duration_s": 0, "min_session_energy_wh": 0}
    )
    hass.states.async_set(_ETO, "100.0")
    with patch.object(
        hass.config_entries, "async_update_entry", wraps=hass.config_entries.async_update_entry
    ) as update:
        await setup_session_engine(hass, entry)

    update.assert_called_once()
    written = update.call_args.kwargs["options"]
    assert (written[CONF_ETO_ENTITY], written[CONF_PLUG_ENTITY]) == (_ETO, MOCK_PLUG_ENTITY)

    await _add_user(hass, entry.entry_id, "Petra")
    petra = _get_subentries_by_type(hass.config_entries.async_get_entry(entry.entry_id), "user")[0]
    with patch(
        "custom_components.ev_charging_manager.config_flow.get_discovery_provider",
        return_value=None,
    ):
        await _add_rfid(hass, entry.entry_id, 1, petra.subentry_id)
    await hass.async_block_till_done()
    await start_profile_session(hass, "goe_gemini")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "2.0")
    hass.states.async_set(_ETO, "102.0")
    await hass.async_block_till_done()
    await stop_profile_session(hass, "goe_gemini")

    session = hass.data[DOMAIN][entry.entry_id]["session_store"].sessions[-1]
    assert session["user_name"] == "Petra"
    assert (session["charger_total_before_kwh"], session["charger_total_after_kwh"]) == (
        100.0,
        102.0,
    )


@pytest.mark.parametrize(
    ("profile", "total_energy_entity", "options"),
    [
        ("goe_gemini", _ETO, {CONF_ETO_ENTITY: None}),
        ("goe_gemini", None, {}),
        ("generic", _ETO, {}),
    ],
)
async def test_eto_not_seeded(
    hass: HomeAssistant, profile: str, total_energy_entity, options: dict
) -> None:
    """T-44: a present option (even None) is kept; no total-energy entity adds no key;
    a profile without a total-energy sensor (generic) is never seeded (D-31)."""
    entry = _eto_entry(profile, total_energy_entity, options)
    await setup_entry_with_subentries(hass, entry)

    assert entry.options.get(CONF_ETO_ENTITY, "absent") == options.get(CONF_ETO_ENTITY, "absent")


async def test_eto_seed_runs_once(hass: HomeAssistant) -> None:
    """T-45: the second setup writes nothing."""
    entry = _eto_entry("goe_gemini", _ETO, {})
    await setup_entry_with_subentries(hass, entry)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    with patch.object(
        hass.config_entries, "async_update_entry", wraps=hass.config_entries.async_update_entry
    ) as update:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    update.assert_not_called()
    assert entry.options[CONF_ETO_ENTITY] == _ETO
