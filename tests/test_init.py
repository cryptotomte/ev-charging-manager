"""Tests for EV Charging Manager setup / unload / device registration."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.const import (
    DOMAIN,
    SESSION_STORE_KEY,
)
from tests.conftest import (
    MOCK_CAR_STATUS_ENTITY,
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    MOCK_POWER_ENTITY,
    MOCK_TRX_ENTITY,
)


async def test_setup_entry(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """Config entry loads successfully."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_unload_entry(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """Config entry unloads cleanly after setup."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED

    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED


async def test_device_created(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """Device is registered with correct name and manufacturer after setup."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(identifiers={(DOMAIN, mock_config_entry.entry_id)})

    assert device is not None
    assert device.name == "My go-e Charger"
    assert device.manufacturer == "EV Charging Manager"
    # Model name is derived from charger profile; mock_config_entry uses "generic"
    assert device.model == "Other / Manual configuration"


async def test_multiple_instances(hass: HomeAssistant) -> None:
    """Two separate config entries create two separate devices without conflict."""
    entry1 = MockConfigEntry(
        domain=DOMAIN,
        data={
            "charger_profile": "goe_gemini",
            "car_status_entity": "sensor.goe_aaa_car_value",
            "car_status_charging_value": "Charging",
            "energy_entity": "sensor.goe_aaa_wh",
            "energy_unit": "kWh",
            "power_entity": "sensor.goe_aaa_nrg_11",
            "rfid_entity": None,
            "total_energy_entity": None,
            "rfid_uid_entity": None,
            "charger_name": "Garage Charger",
            "charger_host": "192.168.1.100",
            "pricing_mode": "static",
            "static_price_kwh": 2.50,
        },
        title="Garage Charger",
    )
    entry2 = MockConfigEntry(
        domain=DOMAIN,
        data={
            "charger_profile": "generic",
            "car_status_entity": "sensor.driveway_car_status",
            "car_status_charging_value": "charging",
            "energy_entity": "sensor.driveway_energy",
            "energy_unit": "kWh",
            "power_entity": "sensor.driveway_power",
            "rfid_entity": None,
            "total_energy_entity": None,
            "rfid_uid_entity": None,
            "charger_name": "Driveway Charger",
            "charger_host": None,
            "pricing_mode": "static",
            "static_price_kwh": 1.80,
        },
        title="Driveway Charger",
    )

    entry1.add_to_hass(hass)
    entry2.add_to_hass(hass)

    # HA auto-sets-up all domain entries when the domain is first loaded;
    # calling async_setup for entry1 is sufficient to trigger both.
    await hass.config_entries.async_setup(entry1.entry_id)
    await hass.async_block_till_done()

    assert entry1.state is ConfigEntryState.LOADED
    assert entry2.state is ConfigEntryState.LOADED

    device_registry = dr.async_get(hass)
    device1 = device_registry.async_get_device(identifiers={(DOMAIN, entry1.entry_id)})
    device2 = device_registry.async_get_device(identifiers={(DOMAIN, entry2.entry_id)})

    assert device1 is not None
    assert device2 is not None
    assert device1.id != device2.id
    assert device1.name == "Garage Charger"
    assert device2.name == "Driveway Charger"


# ---------------------------------------------------------------------------
# T007 (PR-26 US3): StatsEngine subscribed before session recovery runs
# (FR-011, FR-012) — recovery-finalized sessions must reach user statistics
# ---------------------------------------------------------------------------


def _make_recovery_snapshot(
    user_name: str = "Petra",
    user_type: str = "regular",
    energy_start_kwh: float = 10.0,
    energy_kwh: float = 3.0,
) -> dict:
    """Return a minimal in-progress session snapshot (ended_at=None)."""
    return {
        "id": "pr26-recovery-session-001",
        "user_name": user_name,
        "user_type": user_type,
        "vehicle_name": None,
        "vehicle_battery_kwh": None,
        "efficiency_factor": None,
        "rfid_index": 1,
        "rfid_uid": None,
        "started_at": "2026-02-22T08:00:00+00:00",
        "ended_at": None,  # in progress — triggers recovery
        "duration_seconds": 0,
        "energy_kwh": energy_kwh,
        "energy_start_kwh": energy_start_kwh,
        "avg_power_w": 0.0,
        "max_power_w": 0.0,
        "cost_total_kr": 7.5,
        "cost_method": "static",
        "price_details": None,
        "charger_name": "My go-e Charger",
        "data_gap": False,
        "reconstructed": False,
    }


def _store_load_by_key(snapshot: dict):
    """Return an autospec Store.async_load side_effect keyed on the store key.

    Order-independent: serves the session snapshot to the session store only,
    regardless of the order in which the stores are created during setup.
    """

    async def side_effect(self):
        if self.key == SESSION_STORE_KEY:
            return [snapshot]
        return None

    return side_effect


async def _setup_with_recovery_snapshot(hass: HomeAssistant, snapshot: dict) -> MockConfigEntry:
    """Set up the integration with a stored in-progress snapshot.

    Charger entities report Idle / no card, so recovery finalizes the
    snapshot via _complete_snapshot_as_session (reason: not charging).
    """
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Idle")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "13.5")  # 3.5 kWh since energy_start
    hass.states.async_set(MOCK_POWER_ENTITY, "0.0")

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="My go-e Charger")
    with (
        patch(
            "homeassistant.helpers.storage.Store.async_load",
            autospec=True,
            side_effect=_store_load_by_key(snapshot),
        ),
        patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock),
    ):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_recovery_finalized_session_reaches_user_stats(hass: HomeAssistant) -> None:
    """FR-011/FR-012: a session finalized by restart recovery appears in the
    owning user's statistics (energy, cost, count) like a live session."""
    snapshot = _make_recovery_snapshot(user_name="Petra", user_type="regular")
    entry = await _setup_with_recovery_snapshot(hass, snapshot)

    assert entry.state is ConfigEntryState.LOADED
    stats_engine = hass.data[DOMAIN][entry.entry_id]["stats_engine"]

    assert "Petra" in stats_engine.user_stats, (
        "Recovery-finalized session must reach user statistics — StatsEngine "
        "must be subscribed to EVENT_SESSION_COMPLETED before recovery runs"
    )
    stats = stats_engine.user_stats["Petra"]
    assert stats.session_count == 1
    assert stats.total_energy_kwh == 3.5  # 13.5 current − 10.0 energy_start
    assert stats.total_cost_kr == 7.5


async def test_recovery_finalized_unknown_session_tracked(hass: HomeAssistant) -> None:
    """FR-012: a recovery-finalized session with no mapped user lands in
    unknown-session tracking exactly like a live unknown session."""
    snapshot = _make_recovery_snapshot(user_name="Unknown", user_type="unknown")
    entry = await _setup_with_recovery_snapshot(hass, snapshot)

    stats_engine = hass.data[DOMAIN][entry.entry_id]["stats_engine"]

    assert stats_engine.user_stats["Unknown"].session_count == 1
    assert stats_engine.user_stats["Unknown"].total_energy_kwh == 3.5
    # The unknown-session 7-day warning window records the session
    assert len(stats_engine._unknown_session_times) == 1


async def test_recovery_micro_session_does_not_reach_stats(hass: HomeAssistant) -> None:
    """C2 pin: a recovery-finalized session below the micro thresholds is
    discarded and never emits EVENT_SESSION_COMPLETED — it must NOT appear
    in user statistics."""
    # energy_start 13.49 → current 13.5 = 0.01 kWh (10 Wh < 50 Wh threshold)
    snapshot = _make_recovery_snapshot(
        user_name="Petra", user_type="regular", energy_start_kwh=13.49, energy_kwh=0.01
    )
    entry = await _setup_with_recovery_snapshot(hass, snapshot)

    stats_engine = hass.data[DOMAIN][entry.entry_id]["stats_engine"]

    assert "Petra" not in stats_engine.user_stats, (
        "Micro sessions discarded by recovery must not appear in statistics"
    )
    assert stats_engine.user_stats["Unknown"].session_count == 0
