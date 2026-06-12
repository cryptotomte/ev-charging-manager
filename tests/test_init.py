"""Tests for EV Charging Manager setup / unload / device registration."""

from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

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
    setup_session_engine,
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
    """Device-registry isolation across entries (single_config_entry blocks a
    second entry via the UI flow; this test bypasses the flow deliberately via
    add_to_hass)."""
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


# ---------------------------------------------------------------------------
# PR-28 (024-debug-logger-overhaul) US1: new log location + legacy cleanup
# ---------------------------------------------------------------------------

_LEGACY_LOG_NAME = "ev_charging_manager_debug.log"


def _make_debug_entry(debug_logging: bool) -> MockConfigEntry:
    """Return a config entry with the debug_logging option set."""
    return MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={"debug_logging": debug_logging},
        title="My go-e Charger",
    )


async def test_setup_creates_log_at_config_root_not_www(hass: HomeAssistant, tmp_path) -> None:
    """US1 scenario 1: with debug logging enabled the log is created in the
    config root and nothing exists under web-served www/ (FR-001)."""
    hass.config.config_dir = str(tmp_path)
    entry = _make_debug_entry(debug_logging=True)

    await setup_session_engine(hass, entry)
    # Drain the buffered DEBUG_ON marker to disk (age trigger)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()

    root_log = tmp_path / _LEGACY_LOG_NAME
    assert root_log.exists(), "Log file must be created at the config root"
    assert "DEBUG_ON" in root_log.read_text()
    assert not (tmp_path / "www" / _LEGACY_LOG_NAME).exists(), (
        "No log file may exist under the web-served www/ directory"
    )


async def test_legacy_www_file_deleted_at_setup(hass: HomeAssistant, tmp_path, caplog) -> None:
    """US1 scenario 2: a pre-existing legacy www/ log file is deleted at setup
    and the deletion is logged (FR-002)."""
    hass.config.config_dir = str(tmp_path)
    legacy = tmp_path / "www" / _LEGACY_LOG_NAME
    legacy.parent.mkdir()
    legacy.write_text("old exposed content\n")
    entry = _make_debug_entry(debug_logging=True)

    with caplog.at_level(logging.INFO):
        await setup_session_engine(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert not legacy.exists(), "Legacy www/ log file must be deleted at setup"
    assert any(
        "legacy debug log" in r.message.lower() and str(legacy) in r.message for r in caplog.records
    ), "The deletion must be logged with the legacy path"


async def test_legacy_www_file_deleted_when_logging_disabled(hass: HomeAssistant, tmp_path) -> None:
    """US1 scenario 4: the legacy file is deleted even when debug logging is
    DISABLED — the exposure must end regardless of the toggle (FR-002)."""
    hass.config.config_dir = str(tmp_path)
    legacy = tmp_path / "www" / _LEGACY_LOG_NAME
    legacy.parent.mkdir()
    legacy.write_text("old exposed content\n")
    entry = _make_debug_entry(debug_logging=False)

    await setup_session_engine(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert not legacy.exists(), "Legacy cleanup must run even with debug_logging=False"
    # And no new log file is created while logging is disabled
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    assert not (tmp_path / _LEGACY_LOG_NAME).exists()


async def test_legacy_cleanup_runs_even_when_setup_fails_early(
    hass: HomeAssistant, tmp_path
) -> None:
    """Review F6: the legacy-file cleanup is the FIRST await in setup — an
    exception in any later setup step (here: ConfigStore load) must not leave
    the unauthenticated /local/ exposure in place."""
    hass.config.config_dir = str(tmp_path)
    legacy = tmp_path / "www" / _LEGACY_LOG_NAME
    legacy.parent.mkdir()
    legacy.write_text("old exposed content\n")
    entry = _make_debug_entry(debug_logging=True)

    with patch(
        "custom_components.ev_charging_manager.ConfigStore.async_load",
        side_effect=RuntimeError("storage corrupted"),
    ):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is not ConfigEntryState.LOADED, "Setup must fail in this scenario"
    assert not legacy.exists(), "Legacy www/ cleanup must run before anything that can fail setup"


async def test_legacy_file_missing_is_noop(hass: HomeAssistant, tmp_path) -> None:
    """US1 scenario 3: setup proceeds normally when no legacy file exists."""
    hass.config.config_dir = str(tmp_path)
    entry = _make_debug_entry(debug_logging=True)

    await setup_session_engine(hass, entry)

    assert entry.state is ConfigEntryState.LOADED


async def test_legacy_deletion_failure_warns_and_setup_succeeds(
    hass: HomeAssistant, tmp_path, caplog
) -> None:
    """FR-002 edge: deletion failure logs a WARNING naming the path and never
    fails setup."""
    hass.config.config_dir = str(tmp_path)
    legacy = tmp_path / "www" / _LEGACY_LOG_NAME
    legacy.parent.mkdir()
    legacy.write_text("old exposed content\n")
    entry = _make_debug_entry(debug_logging=True)

    with (
        patch(
            "custom_components.ev_charging_manager.debug_logger.os.remove",
            side_effect=OSError("permission denied"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        await setup_session_engine(hass, entry)

    assert entry.state is ConfigEntryState.LOADED, "Cleanup failure must never fail setup"
    assert any(r.levelno == logging.WARNING and str(legacy) in r.message for r in caplog.records), (
        "A WARNING naming the legacy path must be emitted"
    )


async def test_stop_event_flushes_buffer_with_debug_off(hass: HomeAssistant, tmp_path) -> None:
    """Review F1: HA does NOT unload config entries at an orderly stop — the
    EVENT_HOMEASSISTANT_STOP listener must flush buffered lines with DEBUG_OFF
    as the final line, or up to 5 s / 500 lines vanish at every clean restart."""
    hass.config.config_dir = str(tmp_path)
    entry = _make_debug_entry(debug_logging=True)

    await setup_session_engine(hass, entry)
    debug_logger = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
    debug_logger.log("CAR_STATE", "pre-stop event")

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    lines = (tmp_path / _LEGACY_LOG_NAME).read_text().splitlines()
    assert any("pre-stop event" in ln for ln in lines), "Buffer must be flushed at HA stop"
    assert "DEBUG_OFF" in lines[-1], "DEBUG_OFF must be the final line at HA stop"


async def test_stop_listener_removed_on_unload(hass: HomeAssistant, tmp_path) -> None:
    """Review F1: the stop listener is registered via entry.async_on_unload so
    a reload does not leak one listener per setup."""
    hass.config.config_dir = str(tmp_path)
    entry = _make_debug_entry(debug_logging=True)

    # Platform components (sensor/button/...) add their own stop listeners
    # that persist after entry unload — measure relative deltas only.
    await setup_session_engine(hass, entry)
    after_setup = hass.bus.async_listeners().get(EVENT_HOMEASSISTANT_STOP, 0)

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    after_unload = hass.bus.async_listeners().get(EVENT_HOMEASSISTANT_STOP, 0)
    assert after_unload == after_setup - 1, (
        "The stop listener must be unsubscribed on unload — no leak per reload"
    )

    # A reload cycle ends with the same listener count as the first setup
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.bus.async_listeners().get(EVENT_HOMEASSISTANT_STOP, 0) == after_setup


async def test_unload_writes_debug_off_as_final_line(hass: HomeAssistant, tmp_path) -> None:
    """FR-008: integration unload flushes the buffer; DEBUG_OFF is the final line."""
    hass.config.config_dir = str(tmp_path)
    entry = _make_debug_entry(debug_logging=True)

    await setup_session_engine(hass, entry)
    debug_logger = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
    debug_logger.log("CAR_STATE", "pre-unload event")

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    lines = (tmp_path / _LEGACY_LOG_NAME).read_text().splitlines()
    assert any("pre-unload event" in ln for ln in lines), "Buffer must be flushed on unload"
    assert "DEBUG_OFF" in lines[-1], "DEBUG_OFF must be the final line"
