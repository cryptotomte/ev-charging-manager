"""User add, delete and rename on a loaded entry (spec 026 D-2…D-9, tests T-27…T-41).

Changes are driven through the real subentry flows and
hass.config_entries.async_remove_subentry; results are read from the entity
registry, the state machine and the stats store.
"""

from __future__ import annotations

from types import MappingProxyType
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.ev_charging_manager.const import (
    DOMAIN,
    EVENT_SESSION_COMPLETED,
    STATS_STORE_KEY,
)

from .conftest import (
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    setup_session_engine,
    start_charging_session,
    stop_charging_session,
)

_METRICS = ("total_energy", "total_cost", "session_count", "avg_session_energy", "last_session")
_NO_FILTER_OPTIONS = {"min_session_duration_s": 0, "min_session_energy_wh": 0}


def _user(name: str, user_type: str = "regular") -> dict:
    data: dict = {"name": name, "type": user_type, "active": True}
    if user_type == "guest":
        data["guest_pricing"] = {"method": "fixed", "price_per_kwh": 4.0}
    return {"data": data, "subentry_type": "user", "title": name, "unique_id": None}


async def _setup(hass: HomeAssistant, *users: dict) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options=_NO_FILTER_OPTIONS,
        title="My go-e Charger",
        subentries_data=list(users),
    )
    await setup_session_engine(hass, entry)
    return entry


def _entity_ids(hass: HomeAssistant, entry: MockConfigEntry, name: str) -> list[str | None]:
    registry = er.async_get(hass)
    return [
        registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{slugify(name)}_{m}")
        for m in _METRICS
    ]


def _sid(hass: HomeAssistant, entry: MockConfigEntry, name: str) -> str:
    return next(
        s.subentry_id
        for s in hass.config_entries.async_get_entry(entry.entry_id).subentries.values()
        if s.subentry_type == "user" and s.data["name"] == name
    )


def _engine(hass: HomeAssistant, entry: MockConfigEntry):
    return hass.data[DOMAIN][entry.entry_id]["stats_engine"]


def _stored(hass_storage: dict) -> dict:
    return hass_storage[STATS_STORE_KEY]["data"]


async def _completed(
    hass: HomeAssistant, name: str, energy: float, cost: float, user_type: str = "regular"
) -> None:
    hass.bus.async_fire(
        EVENT_SESSION_COMPLETED,
        {
            "user_name": name,
            "user_type": user_type,
            "energy_kwh": energy,
            "cost_kr": cost,
            "started_at": "2026-11-10T08:00:00+00:00",
            "ended_at": "2026-11-10T10:00:00+00:00",
        },
    )
    await hass.async_block_till_done()


async def _add_user(hass: HomeAssistant, entry: MockConfigEntry, name: str) -> None:
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "user"), context={"source": "user"}
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": name, "type": "regular"}
    )
    await hass.async_block_till_done()


async def _rename_user(hass: HomeAssistant, entry: MockConfigEntry, old: str, new: str) -> None:
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "user"),
        context={"source": "reconfigure", "subentry_id": _sid(hass, entry, old)},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": new, "active": True}
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()


async def _delete_user(hass: HomeAssistant, entry: MockConfigEntry, name: str) -> None:
    hass.config_entries.async_remove_subentry(entry, _sid(hass, entry, name))
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, entity_id: str) -> str:
    return hass.states.get(entity_id).state


# ---------------------------------------------------------------------------
# D-2, D-4: add
# ---------------------------------------------------------------------------


async def test_add_user_creates_sensors_without_reload(hass: HomeAssistant) -> None:
    """T-27: five registry entries and states at once; same engine; no reload."""
    entry = await _setup(hass)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        await _add_user(hass, entry, "Paul")

    reload.assert_not_called()
    assert hass.data[DOMAIN][entry.entry_id]["session_engine"] is engine
    entity_ids = _entity_ids(hass, entry, "Paul")
    assert None not in entity_ids
    for entity_id in entity_ids:
        assert _state(hass, entity_id) == STATE_UNAVAILABLE


async def test_add_user_during_session_keeps_session(hass: HomeAssistant) -> None:
    """T-28: the running session object, its energy and its cost continue unchanged."""
    entry = await _setup(hass, _user("Petra"))
    await _add_rfid(hass, entry, 1, _sid(hass, entry, "Petra"))
    await start_charging_session(hass, trx_value="2")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "1.0")
    await hass.async_block_till_done()
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session
    energy, cost = session.energy_kwh, session.cost_total_kr

    await _add_user(hass, entry, "Paul")

    assert engine.active_session is session
    assert (session.energy_kwh, session.cost_total_kr) == (energy, cost)
    hass.states.async_set(MOCK_ENERGY_ENTITY, "2.0")
    await hass.async_block_till_done()
    assert session.energy_kwh == pytest.approx(2.0)
    assert session.cost_total_kr == pytest.approx(5.0)


async def _add_rfid(hass: HomeAssistant, entry: MockConfigEntry, index: int, user_id: str) -> None:
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "rfid_mapping"), context={"source": "user"}
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"card_index": str(index), "user_id": user_id}
    )
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# D-3, D-5, D-6: delete
# ---------------------------------------------------------------------------


async def test_delete_user_removes_sensors_and_bucket(hass: HomeAssistant, hass_storage) -> None:
    """T-29: registry entries, states and the bucket (engine and store) are gone."""
    entry = await _setup(hass, _user("Paul"))
    await _completed(hass, "Paul", 4.0, 10.0)
    entity_ids = _entity_ids(hass, entry, "Paul")

    await _delete_user(hass, entry, "Paul")

    assert _entity_ids(hass, entry, "Paul") == [None] * 5
    for entity_id in entity_ids:
        assert hass.states.get(entity_id) is None
    assert "Paul" not in _engine(hass, entry).user_stats
    assert "Paul" not in _stored(hass_storage)["user_stats"]


async def test_delete_user_removes_disabled_sensor(hass: HomeAssistant) -> None:
    """T-30: a sensor the user disabled is removed from the registry too."""
    entry = await _setup(hass, _user("Paul"))
    registry = er.async_get(hass)
    disabled = _entity_ids(hass, entry, "Paul")[2]
    registry.async_update_entity(disabled, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()

    await _delete_user(hass, entry, "Paul")

    assert registry.async_get(disabled) is None


async def test_delete_user_with_mappings_removes_bucket_once(hass: HomeAssistant) -> None:
    """T-31: the cascade re-enters the listener; the bucket removal still runs once."""
    entry = await _setup(hass, _user("Paul"))
    await _add_rfid(hass, entry, 0, _sid(hass, entry, "Paul"))
    await _add_rfid(hass, entry, 1, _sid(hass, entry, "Paul"))
    await _completed(hass, "Paul", 4.0, 10.0)
    engine = _engine(hass, entry)

    with (
        patch.object(engine, "async_remove_user", wraps=engine.async_remove_user) as remove,
        patch.object(
            engine._stats_store, "async_save", wraps=engine._stats_store.async_save
        ) as save,
    ):
        await _delete_user(hass, entry, "Paul")

    subentries = hass.config_entries.async_get_entry(entry.entry_id).subentries.values()
    assert [s for s in subentries if s.subentry_type == "rfid_mapping"] == []
    remove.assert_called_once_with("Paul")
    assert save.call_count == 1


async def test_delete_guest_keeps_guest_totals(hass: HomeAssistant, hass_storage) -> None:
    """T-32: guest aggregates do not drop; the carry is stored and survives a reload."""
    entry = await _setup(hass, _user("Visitor", "guest"), _user("Other", "guest"))
    await _completed(hass, "Visitor", 10.0, 45.0, "guest")
    await _completed(hass, "Other", 5.0, 20.0, "guest")
    energy_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_guest_total_energy"
    )
    cost_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_guest_total_cost"
    )
    assert (_state(hass, energy_id), _state(hass, cost_id)) == ("15.0", "65.0")

    await _delete_user(hass, entry, "Visitor")

    assert (_state(hass, energy_id), _state(hass, cost_id)) == ("15.0", "65.0")
    assert _stored(hass_storage)["guest_retired"] == {"energy_kwh": 10.0, "cost_kr": 45.0}

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert (_state(hass, energy_id), _state(hass, cost_id)) == ("15.0", "65.0")


async def test_delete_user_sharing_exact_name_keeps_bucket(hass: HomeAssistant) -> None:
    """T-33: another user with the same exact name keeps the bucket and the sensors."""
    entry = await _setup(hass, _user("Paul"), _user("Paul"))
    await _completed(hass, "Paul", 4.0, 10.0)
    entity_ids = _entity_ids(hass, entry, "Paul")

    await _delete_user(hass, entry, "Paul")

    assert _engine(hass, entry).user_stats["Paul"].total_energy_kwh == 4.0
    assert _entity_ids(hass, entry, "Paul") == entity_ids


async def test_delete_user_sharing_slug_keeps_sensors(hass: HomeAssistant) -> None:
    """T-33: same slug, other name: registry entries kept, own bucket removed."""
    entry = await _setup(hass, _user("Paul"), _user("paul"))
    await _completed(hass, "Paul", 4.0, 10.0)
    await _completed(hass, "paul", 1.0, 2.0)
    entity_ids = _entity_ids(hass, entry, "Paul")

    await _delete_user(hass, entry, "paul")

    assert _entity_ids(hass, entry, "Paul") == entity_ids
    assert "paul" not in _engine(hass, entry).user_stats
    assert _engine(hass, entry).user_stats["Paul"].total_energy_kwh == 4.0


# ---------------------------------------------------------------------------
# D-7, D-9: rename
# ---------------------------------------------------------------------------


async def test_rename_moves_bucket_and_sensors(hass: HomeAssistant, hass_storage) -> None:
    """T-34: bucket and registry entries follow the new name; entity ids stay."""
    entry = await _setup(hass, _user("Paul"))
    await _completed(hass, "Paul", 4.0, 10.0)
    before = _engine(hass, entry).user_stats["Paul"].to_dict()
    entity_ids = _entity_ids(hass, entry, "Paul")

    await _rename_user(hass, entry, "Paul", "Pelle")

    stats = _engine(hass, entry).user_stats
    assert "Paul" not in stats
    assert stats["Pelle"].to_dict() == {**before, "user_name": "Pelle"}
    assert set(_stored(hass_storage)["user_stats"]) == {"Pelle", "Unknown"}
    assert _entity_ids(hass, entry, "Paul") == [None] * 5
    assert _entity_ids(hass, entry, "Pelle") == entity_ids
    assert _state(hass, entity_ids[0]) == "4.0"

    await _completed(hass, "Pelle", 2.0, 5.0)
    assert _state(hass, entity_ids[0]) == "6.0"


async def test_rename_case_only_keeps_unique_ids(hass: HomeAssistant) -> None:
    """T-35: the slug is unchanged, so unique_ids stay; the bucket key moves."""
    entry = await _setup(hass, _user("Paul"))
    await _completed(hass, "Paul", 4.0, 10.0)
    entity_ids = _entity_ids(hass, entry, "Paul")

    await _rename_user(hass, entry, "Paul", "PAUL")

    assert _entity_ids(hass, entry, "PAUL") == entity_ids
    assert set(_engine(hass, entry).user_stats) == {"PAUL", "Unknown"}
    assert _state(hass, entity_ids[0]) == "4.0"


async def test_rename_onto_leftover_bucket(hass: HomeAssistant) -> None:
    """T-36: a leftover bucket under the new name is removed (guest: into the carry)."""
    entry = await _setup(hass, _user("Paul"))
    await _completed(hass, "Paul", 4.0, 10.0)
    await _completed(hass, "Ghost", 7.0, 30.0, "guest")

    await _rename_user(hass, entry, "Paul", "Ghost")

    engine = _engine(hass, entry)
    assert engine.user_stats["Ghost"].total_energy_kwh == 4.0
    assert engine.user_stats["Ghost"].user_type == "regular"
    assert engine.guest_retired == {"energy_kwh": 7.0, "cost_kr": 30.0}


async def test_rename_removes_stale_registry_entry(hass: HomeAssistant) -> None:
    """T-37: a stale entry holding a target unique_id is removed; the move succeeds."""
    entry = await _setup(hass, _user("Paul"))
    registry = er.async_get(hass)
    stale = registry.async_get_or_create(
        "sensor", DOMAIN, f"{entry.entry_id}_pelle_total_energy", config_entry=entry
    )
    entity_ids = _entity_ids(hass, entry, "Paul")

    await _rename_user(hass, entry, "Paul", "Pelle")

    assert registry.async_get(stale.entity_id) is None
    assert _entity_ids(hass, entry, "Pelle") == entity_ids


async def test_add_user_onto_leftover_bucket(hass: HomeAssistant) -> None:
    """T-38: the new user starts without the leftover's totals; guest totals go to the carry."""
    entry = await _setup(hass)
    await _completed(hass, "Ghost", 7.0, 30.0, "guest")

    await _add_user(hass, entry, "Ghost")

    engine = _engine(hass, entry)
    assert "Ghost" not in engine.user_stats
    assert engine.guest_retired == {"energy_kwh": 7.0, "cost_kr": 30.0}
    assert _state(hass, _entity_ids(hass, entry, "Ghost")[0]) == STATE_UNAVAILABLE


async def test_add_user_with_existing_exact_name_keeps_bucket(hass: HomeAssistant) -> None:
    """T-39: legacy data — another configured user already has the exact name."""
    entry = await _setup(hass, _user("Paul"))
    await _completed(hass, "Paul", 4.0, 10.0)

    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=MappingProxyType({"name": "Paul", "type": "regular", "active": True}),
            subentry_type="user",
            title="Paul",
            unique_id=None,
        ),
    )
    await hass.async_block_till_done()

    assert _engine(hass, entry).user_stats["Paul"].total_energy_kwh == 4.0
    assert _state(hass, _entity_ids(hass, entry, "Paul")[0]) == "4.0"


async def test_delete_user_during_session(hass: HomeAssistant) -> None:
    """T-40: the session completes under the old name and re-creates its bucket."""
    entry = await _setup(hass, _user("Petra"))
    await _add_rfid(hass, entry, 1, _sid(hass, entry, "Petra"))
    completed = async_capture_events(hass, EVENT_SESSION_COMPLETED)
    await start_charging_session(hass, trx_value="2")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "2.0")
    await hass.async_block_till_done()

    await _delete_user(hass, entry, "Petra")
    await stop_charging_session(hass)
    await hass.async_block_till_done()

    assert [e.data["user_name"] for e in completed] == ["Petra"]
    assert _engine(hass, entry).user_stats["Petra"].total_energy_kwh == pytest.approx(2.0)


async def test_vehicle_and_mapping_changes_leave_stats_entities_alone(
    hass: HomeAssistant,
) -> None:
    """T-41: vehicle and RFID mapping add, edit, delete touch no statistics entity."""
    entry = await _setup(hass, _user("Paul"))
    registry = er.async_get(hass)
    stats_ids = {
        e.entity_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.domain == "sensor"
    }
    registry_events = async_capture_events(hass, er.EVENT_ENTITY_REGISTRY_UPDATED)
    state_events = async_capture_events(hass, EVENT_STATE_CHANGED)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "vehicle"), context={"source": "user"}
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Car", "battery_capacity_kwh": 60.0, "charging_phases": "3"}
    )
    vehicle = next(s for s in entry.subentries.values() if s.subentry_type == "vehicle").subentry_id
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "vehicle"), context={"source": "reconfigure", "subentry_id": vehicle}
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Car B", "battery_capacity_kwh": 60.0, "charging_phases": "3"}
    )
    await _add_rfid(hass, entry, 0, _sid(hass, entry, "Paul"))
    mapping = next(
        s for s in entry.subentries.values() if s.subentry_type == "rfid_mapping"
    ).subentry_id
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "rfid_mapping"),
        context={"source": "reconfigure", "subentry_id": mapping},
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"user_id": _sid(hass, entry, "Paul"), "active": False}
    )
    hass.config_entries.async_remove_subentry(entry, mapping)
    hass.config_entries.async_remove_subentry(entry, vehicle)
    await hass.async_block_till_done()

    assert registry_events == []
    assert [e for e in state_events if e.data["entity_id"] in stats_ids] == []


async def test_delete_legacy_user_named_unknown_keeps_unknown_bucket(hass: HomeAssistant) -> None:
    """Spec 026 § 4.4: the Unknown bucket and its sensors are never removed."""
    entry = await _setup(hass, _user("Unknown"))
    await _completed(hass, "Unknown", 3.0, 7.5, "unknown")
    entity_ids = _entity_ids(hass, entry, "Unknown")

    await _delete_user(hass, entry, "Unknown")

    assert _engine(hass, entry).user_stats["Unknown"].total_energy_kwh == 3.0
    assert _entity_ids(hass, entry, "Unknown") == entity_ids
