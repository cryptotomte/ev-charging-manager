"""Tests for EV Charging Manager ConfigStore."""

from __future__ import annotations

from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.config_store import ConfigStore
from custom_components.ev_charging_manager.const import DOMAIN, STORE_KEY, STORE_VERSION

from .conftest import MOCK_CHARGER_DATA, setup_entry_with_subentries

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_subentry(
    subentry_id: str, subentry_type: str, data: dict, title: str = ""
) -> MagicMock:
    """Create a mock ConfigSubentry."""
    sub = MagicMock()
    sub.subentry_id = subentry_id
    sub.subentry_type = subentry_type
    sub.data = MappingProxyType(data)
    sub.title = title or data.get("name", f"Sub {subentry_id}")
    return sub


def _make_mock_entry(*subentries: MagicMock) -> MagicMock:
    """Create a mock ConfigEntry with given subentries."""
    entry = MagicMock()
    entry.subentries = {s.subentry_id: s for s in subentries}
    return entry


# ---------------------------------------------------------------------------
# async_load — empty / missing file (FR-022)
# ---------------------------------------------------------------------------


async def test_async_load_empty(hass: HomeAssistant) -> None:
    """Loading with no stored file returns empty structure."""
    store = ConfigStore(hass)
    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=None):
        result = await store.async_load()

    assert result == {"vehicles": [], "users": [], "rfid_mappings": []}


async def test_async_load_existing_data(hass: HomeAssistant) -> None:
    """Loading with existing stored data returns that data."""
    existing = {
        "vehicles": [{"id": "v1", "name": "Test"}],
        "users": [],
        "rfid_mappings": [],
    }
    store = ConfigStore(hass)
    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=existing):
        result = await store.async_load()

    assert result == existing
    assert result["vehicles"][0]["name"] == "Test"


# ---------------------------------------------------------------------------
# async_save / async_load roundtrip
# ---------------------------------------------------------------------------


async def test_async_save_load_roundtrip(hass: HomeAssistant) -> None:
    """Save then load returns the same data."""
    store = ConfigStore(hass)

    saved_data = {}

    async def mock_save(data):
        saved_data.update(data)

    async def mock_load():
        return saved_data if saved_data else None

    with (
        patch.object(store._store, "async_save", side_effect=mock_save),
        patch.object(store._store, "async_load", side_effect=mock_load),
    ):
        # Load empty
        await store.async_load()
        assert store.data == {"vehicles": [], "users": [], "rfid_mappings": []}

        # Modify and save
        store._data["vehicles"].append({"id": "v1", "name": "My Car"})
        await store.async_save()

        # Reload
        result = await store.async_load()
        assert len(result["vehicles"]) == 1
        assert result["vehicles"][0]["name"] == "My Car"


# ---------------------------------------------------------------------------
# async_sync_from_subentries
# ---------------------------------------------------------------------------


async def test_sync_from_subentries_builds_json(hass: HomeAssistant) -> None:
    """Syncing from subentries produces correct JSON structure."""
    vehicle_sub = _make_mock_subentry(
        "v_001",
        "vehicle",
        {
            "name": "Peugeot 3008",
            "battery_capacity_kwh": 14.4,
            "usable_battery_kwh": 14.4,
            "charging_phases": 1,
            "max_charging_power_kw": 3.7,
            "charging_efficiency": 0.88,
        },
    )
    user_sub = _make_mock_subentry(
        "u_001",
        "user",
        {
            "name": "Paul",
            "type": "regular",
            "active": True,
            "created_at": "2026-03-01T10:00:00+00:00",
        },
    )
    rfid_sub = _make_mock_subentry(
        "r_001",
        "rfid_mapping",
        {
            "card_index": 0,
            "card_uid": None,
            "user_id": "u_001",
            "vehicle_id": "v_001",
            "active": True,
            "deactivated_by": None,
        },
    )
    entry = _make_mock_entry(vehicle_sub, user_sub, rfid_sub)

    store = ConfigStore(hass)
    with patch.object(store._store, "async_save", new_callable=AsyncMock):
        await store.async_sync_from_subentries(entry)

    data = store.data
    assert len(data["vehicles"]) == 1
    assert len(data["users"]) == 1
    assert len(data["rfid_mappings"]) == 1

    assert data["vehicles"][0]["id"] == "v_001"
    assert data["vehicles"][0]["name"] == "Peugeot 3008"
    assert data["vehicles"][0]["battery_capacity_kwh"] == 14.4

    assert data["users"][0]["id"] == "u_001"
    assert data["users"][0]["name"] == "Paul"
    assert data["users"][0]["type"] == "regular"

    assert data["rfid_mappings"][0]["card_index"] == 0
    assert data["rfid_mappings"][0]["user_id"] == "u_001"
    assert data["rfid_mappings"][0]["vehicle_id"] == "v_001"


async def test_sync_from_empty_subentries(hass: HomeAssistant) -> None:
    """Syncing from entry with no subentries produces empty config."""
    entry = _make_mock_entry()

    store = ConfigStore(hass)
    with patch.object(store._store, "async_save", new_callable=AsyncMock):
        await store.async_sync_from_subentries(entry)

    assert store.data == {"vehicles": [], "users": [], "rfid_mappings": []}


# ---------------------------------------------------------------------------
# T058 / spec 026 D-20: stored data of the wrong shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        {"vehicles": [{"id": "v1"}]},
        {},
        {"vehicles": [], "users": [{"id": "u1", "name": "Paul"}], "rfid_mappings": []},
    ],
)
async def test_async_load_valid_data_passes_through(hass: HomeAssistant, stored, caplog) -> None:
    """T-49: dicts whose present keys hold lists of dicts load unchanged, without a warning."""
    store = ConfigStore(hass)
    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=stored):
        result = await store.async_load()

    assert result == stored
    assert store.data == stored
    assert "unexpected format" not in caplog.text


@pytest.mark.parametrize(
    "stored",
    [
        [{"id": "v1"}],
        "corrupt",
        42,
        {"vehicles": [], "users": [], "rfid_mappings": None},
        {"vehicles": [], "users": [], "rfid_mappings": {"card_index": 0}},
        {"vehicles": [], "users": [], "rfid_mappings": ["card0"]},
    ],
)
async def test_corrupt_config_store_rebuilt_at_setup(
    hass: HomeAssistant, hass_storage, caplog, stored
) -> None:
    """T-48: setup succeeds, one warning, config rebuilt from the subentries."""
    hass_storage[STORE_KEY] = {"version": STORE_VERSION, "key": STORE_KEY, "data": stored}
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        title="Test Charger",
        subentries_data=[
            {
                "data": {"name": "Paul", "type": "regular", "active": True},
                "subentry_type": "user",
                "title": "Paul",
                "unique_id": None,
            }
        ],
    )
    await setup_entry_with_subentries(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    warnings = [r for r in caplog.records if "unexpected format" in r.message]
    assert [r.levelname for r in warnings] == ["WARNING"]
    store = hass.data[DOMAIN][entry.entry_id]["config_store"]
    assert [u["name"] for u in store.data["users"]] == ["Paul"]


# ---------------------------------------------------------------------------
# async_sync_from_subentries — guest user
# ---------------------------------------------------------------------------


async def test_sync_from_subentries_guest_user(hass: HomeAssistant) -> None:
    """Syncing a guest user with pricing produces correct JSON."""
    guest_sub = _make_mock_subentry(
        "u_002",
        "user",
        {
            "name": "Guest",
            "type": "guest",
            "active": True,
            "created_at": "2026-03-01T10:05:00+00:00",
            "guest_pricing": {"method": "fixed", "price_per_kwh": 4.50},
        },
    )
    entry = _make_mock_entry(guest_sub)

    store = ConfigStore(hass)
    with patch.object(store._store, "async_save", new_callable=AsyncMock):
        await store.async_sync_from_subentries(entry)

    user = store.data["users"][0]
    assert user["type"] == "guest"
    assert user["guest_pricing"] == {"method": "fixed", "price_per_kwh": 4.50}
