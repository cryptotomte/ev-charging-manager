"""Tests for EV Charging Manager ConfigStore."""

from __future__ import annotations

from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.ev_charging_manager.config_store import ConfigStore

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
# T058: Edge case — corrupted/invalid stored data
# ---------------------------------------------------------------------------


async def test_async_load_corrupted_returns_stored_data(hass: HomeAssistant) -> None:
    """Loading corrupted (partial) data returns whatever was stored (no crash)."""
    # Simulate stored data that's missing expected keys
    corrupted = {"vehicles": [{"id": "v1"}]}
    store = ConfigStore(hass)
    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=corrupted):
        result = await store.async_load()

    # ConfigStore should load the data as-is (not crash)
    assert result == corrupted
    assert store.data == corrupted


async def test_async_load_empty_dict_returns_empty_dict(hass: HomeAssistant) -> None:
    """Loading an empty dict (not None) returns that empty dict."""
    store = ConfigStore(hass)
    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value={}):
        result = await store.async_load()

    assert result == {}


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
