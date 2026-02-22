"""Tests for stats sensor entities — per-user statistics sensors (T008, T020)."""

from __future__ import annotations

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.const import DOMAIN, EVENT_SESSION_COMPLETED
from tests.conftest import MOCK_CHARGER_DATA

# ---------------------------------------------------------------------------
# Full-integration fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def stats_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Set up the full integration and return the config entry.

    Sets up charger entity states to idle before loading, so all listeners
    register with a clean baseline. No user subentries added by default.
    """
    hass.states.async_set("sensor.goe_abc123_car_value", "Idle")
    hass.states.async_set("select.goe_abc123_trx", "null")
    hass.states.async_set("sensor.goe_abc123_wh", "0.0")
    hass.states.async_set("sensor.goe_abc123_nrg_11", "0.0")

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        title="My go-e Charger",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def stats_entry_with_user(hass: HomeAssistant) -> MockConfigEntry:
    """Set up the full integration with one user subentry (Petra, regular).

    Uses subentries_data parameter on MockConfigEntry to pre-populate
    the Petra user subentry before setup.
    """
    hass.states.async_set("sensor.goe_abc123_car_value", "Idle")
    hass.states.async_set("select.goe_abc123_trx", "null")
    hass.states.async_set("sensor.goe_abc123_wh", "0.0")
    hass.states.async_set("sensor.goe_abc123_nrg_11", "0.0")

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        title="My go-e Charger",
        subentries_data=[
            {
                "data": {"name": "Petra", "type": "regular", "active": True},
                "subentry_type": "user",
                "title": "Petra",
                "unique_id": None,
            }
        ],
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# ---------------------------------------------------------------------------
# T008: Sensor creation tests
# ---------------------------------------------------------------------------


async def test_unknown_user_sensors_created_without_subentries(
    hass: HomeAssistant, stats_entry: MockConfigEntry
) -> None:
    """Even with no user subentries, 5 sensors for 'Unknown' and 2 guest sensors exist."""
    entry = stats_entry
    registry = er.async_get(hass)

    # Verify Unknown user sensors
    for metric in [
        "total_energy",
        "total_cost",
        "session_count",
        "avg_session_energy",
        "last_session",
    ]:
        unique_id = f"{entry.entry_id}_unknown_{metric}"
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        assert entity_id is not None, f"Missing sensor unique_id: {unique_id}"

    # Verify guest sensors
    for unique_id_suffix in ["guest_last_energy", "guest_last_charge_price"]:
        unique_id = f"{entry.entry_id}_{unique_id_suffix}"
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        assert entity_id is not None, f"Missing sensor unique_id: {unique_id}"


async def test_user_subentry_creates_5_sensors(
    hass: HomeAssistant, stats_entry_with_user: MockConfigEntry
) -> None:
    """One user subentry (Petra) creates 5 stats sensors with correct unique_ids."""
    entry = stats_entry_with_user
    registry = er.async_get(hass)

    from homeassistant.util import slugify

    user_slug = slugify("Petra")  # → "petra"

    for metric in [
        "total_energy",
        "total_cost",
        "session_count",
        "avg_session_energy",
        "last_session",
    ]:
        unique_id = f"{entry.entry_id}_{user_slug}_{metric}"
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        assert entity_id is not None, f"Missing sensor: {unique_id}"


async def test_sensor_unique_ids_follow_entry_slug_metric_pattern(
    hass: HomeAssistant, stats_entry_with_user: MockConfigEntry
) -> None:
    """Sensor unique_ids follow '{entry_id}_{user_slug}_{metric}' pattern (R2, D5)."""
    entry = stats_entry_with_user
    registry = er.async_get(hass)

    from homeassistant.util import slugify

    slug = slugify("Petra")

    for metric in [
        "total_energy",
        "total_cost",
        "session_count",
        "avg_session_energy",
        "last_session",
    ]:
        expected_uid = f"{entry.entry_id}_{slug}_{metric}"
        entity = registry.async_get_entity_id("sensor", DOMAIN, expected_uid)
        assert entity is not None


async def test_sensor_values_zero_before_any_session(
    hass: HomeAssistant, stats_entry_with_user: MockConfigEntry
) -> None:
    """Stats sensors show 0 for totals before any session (not unavailable)."""
    entry = stats_entry_with_user
    registry = er.async_get(hass)

    from homeassistant.util import slugify

    slug = slugify("Petra")

    for metric in ["total_energy", "total_cost", "session_count"]:
        uid = f"{entry.entry_id}_{slug}_{metric}"
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid)
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert float(state.state) == 0.0


async def test_avg_session_energy_unavailable_before_sessions(
    hass: HomeAssistant, stats_entry_with_user: MockConfigEntry
) -> None:
    """avg_session_energy is unavailable when session_count == 0 (sensor-contracts.md)."""
    entry = stats_entry_with_user
    registry = er.async_get(hass)

    from homeassistant.util import slugify

    slug = slugify("Petra")

    uid = f"{entry.entry_id}_{slug}_avg_session_energy"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid)
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


async def test_last_session_unavailable_before_sessions(
    hass: HomeAssistant, stats_entry_with_user: MockConfigEntry
) -> None:
    """last_session is unavailable when session_count == 0 (sensor-contracts.md)."""
    entry = stats_entry_with_user
    registry = er.async_get(hass)

    from homeassistant.util import slugify

    slug = slugify("Petra")

    uid = f"{entry.entry_id}_{slug}_last_session"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid)
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


async def test_sensor_values_update_after_session_event(
    hass: HomeAssistant, stats_entry_with_user: MockConfigEntry
) -> None:
    """Firing session_completed updates total_energy, total_cost, session_count sensors."""
    entry = stats_entry_with_user
    registry = er.async_get(hass)

    from homeassistant.util import slugify

    slug = slugify("Petra")

    hass.bus.async_fire(
        EVENT_SESSION_COMPLETED,
        {
            "user_name": "Petra",
            "user_type": "regular",
            "energy_kwh": 12.4,
            "cost_kr": 31.0,
            "started_at": "2026-03-14T14:00:00+01:00",
            "ended_at": "2026-03-14T14:22:00+01:00",
        },
    )
    await hass.async_block_till_done()

    energy_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{slug}_total_energy"
    )
    cost_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{slug}_total_cost")
    count_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{slug}_session_count"
    )

    assert float(hass.states.get(energy_id).state) == 12.4
    assert float(hass.states.get(cost_id).state) == 31.0
    assert float(hass.states.get(count_id).state) == 1.0


async def test_avg_session_energy_available_after_session(
    hass: HomeAssistant, stats_entry_with_user: MockConfigEntry
) -> None:
    """avg_session_energy becomes available after the first session."""
    entry = stats_entry_with_user
    registry = er.async_get(hass)

    from homeassistant.util import slugify

    slug = slugify("Petra")

    hass.bus.async_fire(
        EVENT_SESSION_COMPLETED,
        {
            "user_name": "Petra",
            "user_type": "regular",
            "energy_kwh": 12.4,
            "cost_kr": 31.0,
            "started_at": "2026-03-14T14:00:00+01:00",
            "ended_at": "2026-03-14T14:22:00+01:00",
        },
    )
    await hass.async_block_till_done()

    uid = f"{entry.entry_id}_{slug}_avg_session_energy"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid)
    state = hass.states.get(entity_id)
    assert state.state != STATE_UNAVAILABLE
    assert float(state.state) == 12.4


async def test_monthly_attributes_on_total_energy_sensor(
    hass: HomeAssistant, stats_entry_with_user: MockConfigEntry
) -> None:
    """total_energy sensor has monthly breakdown attributes after a session (FR-004)."""
    entry = stats_entry_with_user
    registry = er.async_get(hass)

    from homeassistant.util import slugify

    slug = slugify("Petra")

    hass.bus.async_fire(
        EVENT_SESSION_COMPLETED,
        {
            "user_name": "Petra",
            "user_type": "regular",
            "energy_kwh": 12.4,
            "cost_kr": 31.0,
            "started_at": "2026-03-14T14:00:00+01:00",
            "ended_at": "2026-03-14T14:22:00+01:00",
        },
    )
    await hass.async_block_till_done()

    uid = f"{entry.entry_id}_{slug}_total_energy"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid)
    state = hass.states.get(entity_id)

    attrs = state.attributes
    assert "current_month_kwh" in attrs
    assert "current_month_cost" in attrs
    assert "current_month_sessions" in attrs
    assert "previous_month_kwh" in attrs
    assert "previous_month_cost" in attrs
    assert "previous_month_sessions" in attrs
    assert attrs["current_month_kwh"] == 12.4
    assert attrs["current_month_sessions"] == 1


# ---------------------------------------------------------------------------
# T020 (sensor part): Guest sensors
# ---------------------------------------------------------------------------


async def test_guest_last_energy_unavailable_before_guest_session(
    hass: HomeAssistant, stats_entry: MockConfigEntry
) -> None:
    """guest_last_energy is unavailable when no guest session has occurred."""
    entry = stats_entry
    registry = er.async_get(hass)

    uid = f"{entry.entry_id}_guest_last_energy"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid)
    assert entity_id is not None

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


async def test_guest_last_charge_price_unavailable_without_price_in_event(
    hass: HomeAssistant, stats_entry: MockConfigEntry
) -> None:
    """guest_last_charge_price is unavailable when event has no charge_price_kr."""
    entry = stats_entry
    registry = er.async_get(hass)

    uid = f"{entry.entry_id}_guest_last_charge_price"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid)
    assert entity_id is not None

    # Guest session without charge_price_kr in event data
    hass.bus.async_fire(
        EVENT_SESSION_COMPLETED,
        {
            "user_name": "Guest",
            "user_type": "guest",
            "energy_kwh": 32.1,
            "cost_kr": 0.0,
            "started_at": "2026-04-10T17:00:00+02:00",
            "ended_at": "2026-04-10T17:32:05+02:00",
        },
    )
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.state == STATE_UNAVAILABLE


async def test_guest_last_charge_price_shows_value_after_guest_session(
    hass: HomeAssistant, stats_entry: MockConfigEntry
) -> None:
    """guest_last_charge_price shows 144.45 after a guest session with charge_price_kr (PR-06)."""
    entry = stats_entry
    registry = er.async_get(hass)

    uid = f"{entry.entry_id}_guest_last_charge_price"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid)
    assert entity_id is not None

    # Before any session — unavailable
    state = hass.states.get(entity_id)
    assert state.state == STATE_UNAVAILABLE

    # Fire guest session with charge_price_kr
    hass.bus.async_fire(
        EVENT_SESSION_COMPLETED,
        {
            "user_name": "Gäst-Erik",
            "user_type": "guest",
            "energy_kwh": 32.1,
            "cost_kr": 80.25,
            "charge_price_kr": 144.45,
            "started_at": "2026-04-10T14:00:00+02:00",
            "ended_at": "2026-04-10T15:00:00+02:00",
        },
    )
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.state != STATE_UNAVAILABLE
    assert abs(float(state.state) - 144.45) < 0.01


async def test_guest_last_energy_updates_after_guest_session(
    hass: HomeAssistant, stats_entry: MockConfigEntry
) -> None:
    """guest_last_energy shows energy value after a guest session."""
    entry = stats_entry
    registry = er.async_get(hass)

    hass.bus.async_fire(
        EVENT_SESSION_COMPLETED,
        {
            "user_name": "Guest",
            "user_type": "guest",
            "energy_kwh": 32.1,
            "cost_kr": 0.0,
            "started_at": "2026-04-10T17:00:00+02:00",
            "ended_at": "2026-04-10T17:32:05+02:00",
        },
    )
    await hass.async_block_till_done()

    uid = f"{entry.entry_id}_guest_last_energy"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid)
    state = hass.states.get(entity_id)
    assert state.state != STATE_UNAVAILABLE
    assert float(state.state) == 32.1


async def test_guest_last_energy_unchanged_after_regular_session(
    hass: HomeAssistant, stats_entry: MockConfigEntry
) -> None:
    """guest_last_energy is not overwritten by non-guest sessions (FR-009)."""
    entry = stats_entry
    registry = er.async_get(hass)

    # Guest session first
    hass.bus.async_fire(
        EVENT_SESSION_COMPLETED,
        {
            "user_name": "Guest",
            "user_type": "guest",
            "energy_kwh": 32.1,
            "cost_kr": 0.0,
            "started_at": "2026-04-10T17:00:00+02:00",
            "ended_at": "2026-04-10T17:32:05+02:00",
        },
    )
    await hass.async_block_till_done()

    # Regular user session
    hass.bus.async_fire(
        EVENT_SESSION_COMPLETED,
        {
            "user_name": "Petra",
            "user_type": "regular",
            "energy_kwh": 8.0,
            "cost_kr": 20.0,
            "started_at": "2026-04-11T09:00:00+02:00",
            "ended_at": "2026-04-11T10:00:00+02:00",
        },
    )
    await hass.async_block_till_done()

    uid = f"{entry.entry_id}_guest_last_energy"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid)
    state = hass.states.get(entity_id)
    # Still shows the guest's value
    assert float(state.state) == 32.1


# ---------------------------------------------------------------------------
# Guest total sensors (G3)
# ---------------------------------------------------------------------------


async def test_guest_total_energy_sensor_created(
    hass: HomeAssistant, stats_entry: MockConfigEntry
) -> None:
    """guest_total_energy sensor is created and shows 0 before any sessions."""
    entry = stats_entry
    registry = er.async_get(hass)

    uid = f"{entry.entry_id}_guest_total_energy"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid)
    assert entity_id is not None

    state = hass.states.get(entity_id)
    assert state is not None
    assert float(state.state) == 0.0


async def test_guest_total_cost_sensor_created(
    hass: HomeAssistant, stats_entry: MockConfigEntry
) -> None:
    """guest_total_cost sensor is created and shows 0 before any sessions."""
    entry = stats_entry
    registry = er.async_get(hass)

    uid = f"{entry.entry_id}_guest_total_cost"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid)
    assert entity_id is not None

    state = hass.states.get(entity_id)
    assert state is not None
    assert float(state.state) == 0.0


async def test_guest_total_sensors_sum_across_guests(
    hass: HomeAssistant, stats_entry: MockConfigEntry
) -> None:
    """guest_total_energy and guest_total_cost sum across multiple guest users."""
    entry = stats_entry
    registry = er.async_get(hass)

    # Fire two guest sessions from different guests
    hass.bus.async_fire(
        EVENT_SESSION_COMPLETED,
        {
            "user_name": "Gäst-Erik",
            "user_type": "guest",
            "energy_kwh": 20.0,
            "cost_kr": 50.0,
            "started_at": "2026-04-10T14:00:00+02:00",
            "ended_at": "2026-04-10T15:00:00+02:00",
        },
    )
    await hass.async_block_till_done()

    hass.bus.async_fire(
        EVENT_SESSION_COMPLETED,
        {
            "user_name": "Gäst-Anna",
            "user_type": "guest",
            "energy_kwh": 12.5,
            "cost_kr": 31.25,
            "started_at": "2026-04-11T10:00:00+02:00",
            "ended_at": "2026-04-11T11:00:00+02:00",
        },
    )
    await hass.async_block_till_done()

    energy_uid = f"{entry.entry_id}_guest_total_energy"
    energy_id = registry.async_get_entity_id("sensor", DOMAIN, energy_uid)
    state = hass.states.get(energy_id)
    assert float(state.state) == pytest.approx(32.5, abs=0.01)

    cost_uid = f"{entry.entry_id}_guest_total_cost"
    cost_id = registry.async_get_entity_id("sensor", DOMAIN, cost_uid)
    state = hass.states.get(cost_id)
    assert float(state.state) == pytest.approx(81.25, abs=0.01)


async def test_guest_total_sensors_ignore_regular_users(
    hass: HomeAssistant, stats_entry: MockConfigEntry
) -> None:
    """guest_total sensors only include guest-type users, not regular or unknown."""
    entry = stats_entry
    registry = er.async_get(hass)

    # Guest session
    hass.bus.async_fire(
        EVENT_SESSION_COMPLETED,
        {
            "user_name": "Gäst-Erik",
            "user_type": "guest",
            "energy_kwh": 10.0,
            "cost_kr": 25.0,
            "started_at": "2026-04-10T14:00:00+02:00",
            "ended_at": "2026-04-10T15:00:00+02:00",
        },
    )
    await hass.async_block_till_done()

    # Regular user session — should NOT be included
    hass.bus.async_fire(
        EVENT_SESSION_COMPLETED,
        {
            "user_name": "Petra",
            "user_type": "regular",
            "energy_kwh": 8.0,
            "cost_kr": 20.0,
            "started_at": "2026-04-11T09:00:00+02:00",
            "ended_at": "2026-04-11T10:00:00+02:00",
        },
    )
    await hass.async_block_till_done()

    # Unknown user session — should NOT be included
    hass.bus.async_fire(
        EVENT_SESSION_COMPLETED,
        {
            "user_name": "Unknown",
            "user_type": "unknown",
            "energy_kwh": 5.0,
            "cost_kr": 12.5,
            "started_at": "2026-04-12T09:00:00+02:00",
            "ended_at": "2026-04-12T10:00:00+02:00",
        },
    )
    await hass.async_block_till_done()

    energy_uid = f"{entry.entry_id}_guest_total_energy"
    energy_id = registry.async_get_entity_id("sensor", DOMAIN, energy_uid)
    state = hass.states.get(energy_id)
    # Only guest energy: 10.0
    assert float(state.state) == pytest.approx(10.0, abs=0.01)

    cost_uid = f"{entry.entry_id}_guest_total_cost"
    cost_id = registry.async_get_entity_id("sensor", DOMAIN, cost_uid)
    state = hass.states.get(cost_id)
    # Only guest cost: 25.0
    assert float(state.state) == pytest.approx(25.0, abs=0.01)
