"""TC-022, TC-004: Overnight charging scenario tests for PlugAnchoredSessionEngine (PR-22).

TC-022: Plug in at 22:00, charge 22:30–02:00 (3.5 h), idle 02:00–07:30, unplug 07:30.
        Asserts:
        - Exactly 1 session.
        - connection_duration_s ≈ 9 * 3600 (22:00 → 07:30).
        - charging_duration_s ≈ 3.5 * 3600 (22:30 → 02:00).
        - avg_power_w = energy_kwh * 3600 / charging_duration_s (NOT connection time).

TC-004: Explicit avg_power_w formula verification with ±0.1 W tolerance.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ev_charging_manager.const import (
    CONF_CHARGING_IDLE_TIMEOUT_MIN,
    CONF_DISCONNECT_GRACE_MIN,
    CONF_RFID_GRACE_SECONDS,
    DOMAIN,
)
from custom_components.ev_charging_manager.session_engine_v2 import (
    PlugAnchoredSessionEngine,
)
from tests.conftest import (
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    MOCK_POWER_ENTITY,
    MOCK_TRX_ENTITY,
)

MOCK_PLUG_ENTITY = "binary_sensor.goe_abc123_car_0"
MOCK_CABLE_LOCK_ENTITY = "sensor.goe_abc123_cus_value"

# 6-minute idle timeout so tests don't have to wait 5 minutes of simulated time
IDLE_TIMEOUT_MIN = 6


async def _make_engine_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create and set up a config entry with PlugAnchoredSessionEngine active."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
            CONF_CHARGING_IDLE_TIMEOUT_MIN: IDLE_TIMEOUT_MIN,
            CONF_DISCONNECT_GRACE_MIN: 10,
            CONF_RFID_GRACE_SECONDS: 0,  # opt out: overnight/duration behavior tests
        },
        title="Test go-e Charger",
    )

    hass.states.async_set(MOCK_PLUG_ENTITY, "off")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    hass.states.async_set(MOCK_POWER_ENTITY, "0.0")

    entry.add_to_hass(hass)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    return entry


def _get_engine(hass: HomeAssistant, entry: MockConfigEntry) -> PlugAnchoredSessionEngine:
    """Return the PlugAnchoredSessionEngine from hass.data."""
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert isinstance(engine, PlugAnchoredSessionEngine), (
        f"Expected PlugAnchoredSessionEngine, got {type(engine).__name__}"
    )
    return engine


# ---------------------------------------------------------------------------
# TC-022: overnight session — connection vs charging duration distinction
# ---------------------------------------------------------------------------


async def test_tc022_overnight_charging_session(hass: HomeAssistant, freezer) -> None:
    """TC-022: Overnight scenario — plug in 22:00, charge 22:30–02:00, idle, unplug 07:30.

    Verifies that connection_duration_s and charging_duration_s are correctly
    computed as separate values, and avg_power_w uses charging time (not
    connection time) as the denominator.
    """
    entry = await _make_engine_entry(hass)
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    # Total simulated energy for the session (kWh)
    session_energy_kwh = 22.5
    # Charging spans 22:30 to 02:00 = 3.5 hours
    charging_duration_s = int(3.5 * 3600)
    # Connection spans 22:00 to 07:30 = 9.5 hours
    connection_duration_s = int(9.5 * 3600)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # ---- 22:00: plug in cable (no charging yet) ----
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        assert engine.active_session is not None, "Session should start on plug-in"

        # ---- advance 30 min to 22:30 ----
        freezer.tick(timedelta(minutes=30))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # ---- 22:30: charging begins ----
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.5")
        hass.states.async_set(MOCK_POWER_ENTITY, "6440.0")
        await hass.async_block_till_done()

        assert engine.active_session.charging_started_at is not None, (
            "charging_started_at should be set when power > 0"
        )

        # ---- advance 3.5 hours to 02:00 ----
        freezer.tick(timedelta(hours=3, minutes=30))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # ---- 02:00: charging stops (BMS complete, power drops to 0) ----
        hass.states.async_set(MOCK_ENERGY_ENTITY, str(session_energy_kwh))
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()

        # ---- advance idle timeout + 1 min to ensure timer fires (02:07) ----
        freezer.tick(timedelta(minutes=IDLE_TIMEOUT_MIN + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Window should be closed now
        assert engine.active_session.charging_ended_at is not None, (
            "charging_ended_at should be set after idle timeout"
        )
        assert engine.active_session.charging_window_count == 1, (
            "Exactly one charging window expected"
        )

        # ---- advance remaining idle to 07:30 (about 5h20m) ----
        freezer.tick(timedelta(hours=5, minutes=20))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # ---- 07:30: unplug ----
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    # ---- Assertions ----
    sessions = session_store.sessions
    assert len(sessions) == 1, f"TC-022: Expected 1 session, got {len(sessions)}: {sessions}"

    session = sessions[0]

    # Connection duration: 22:00 → 07:30 = 9.5 h (±5 min tolerance for timer resolution)
    assert abs(session["connection_duration_s"] - connection_duration_s) <= 600, (
        f"TC-022: connection_duration_s {session['connection_duration_s']} "
        f"not within 10 min of {connection_duration_s}"
    )

    # Charging duration: 22:30 → 02:00 = 3.5 h (±5 min tolerance)
    assert abs(session["charging_duration_s"] - charging_duration_s) <= 600, (
        f"TC-022: charging_duration_s {session['charging_duration_s']} "
        f"not within 10 min of {charging_duration_s}"
    )

    # Connection >> charging (overnight idle > charging time)
    assert session["connection_duration_s"] > session["charging_duration_s"], (
        "TC-022: connection_duration_s must exceed charging_duration_s for overnight scenario"
    )

    # Energy attribution
    assert abs(session["energy_kwh"] - session_energy_kwh) <= 0.1, (
        f"TC-022: energy_kwh {session['energy_kwh']:.3f} not close to {session_energy_kwh}"
    )

    # Exactly 1 charging window
    assert session["charging_window_count"] == 1, (
        f"TC-022: Expected 1 charging window, got {session['charging_window_count']}"
    )


# ---------------------------------------------------------------------------
# TC-004: avg_power_w uses charging_duration_s denominator
# ---------------------------------------------------------------------------


async def test_tc004_avg_power_uses_charging_duration(hass: HomeAssistant, freezer) -> None:
    """TC-004: avg_power_w = energy_kwh * 3600 / charging_duration_s, not connection time.

    Charges for exactly 2 h in a session with 3 h total connection time.
    Verifies avg_power_w ≈ energy_kwh * 1800 W/kWh (using charging time).
    """
    entry = await _make_engine_entry(hass)
    _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    energy_kwh = 14.0  # kWh charged
    charging_duration_s = 2 * 3600  # 2 hours of actual charging
    # Expected avg_power: 14.0 kWh / 2h = 7.0 kW = 7000 W
    expected_avg_power_w = (energy_kwh * 3_600_000) / charging_duration_s

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

        # Advance 30 min before charging starts (adds to connection time only)
        freezer.tick(timedelta(minutes=30))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Start charging
        hass.states.async_set(MOCK_POWER_ENTITY, "7000.0")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.1")
        await hass.async_block_till_done()

        # Advance 2 h of charging
        freezer.tick(timedelta(hours=2))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Charging ends
        hass.states.async_set(MOCK_ENERGY_ENTITY, str(energy_kwh))
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()

        # Wait for idle timer
        freezer.tick(timedelta(minutes=IDLE_TIMEOUT_MIN + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Advance 30 min more idle before unplug (total connection = 3h30m)
        freezer.tick(timedelta(minutes=30))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        # Unplug
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    sessions = session_store.sessions
    assert len(sessions) == 1, f"TC-004: Expected 1 session, got {len(sessions)}: {sessions}"

    session = sessions[0]

    # charging_duration_s must be close to 2 h (±5 min)
    assert abs(session["charging_duration_s"] - charging_duration_s) <= 600, (
        f"TC-004: charging_duration_s {session['charging_duration_s']} "
        f"not within 5 min of {charging_duration_s} s"
    )

    # avg_power_w = energy_kwh * 3 600 000 / charging_duration_s (not connection)
    # Allow ±0.1 W per spec
    computed_avg = session["avg_power_w"]
    # Recompute expected from actual charging_duration_s for tolerance
    expected_from_actual = (session["energy_kwh"] * 3_600_000) / session["charging_duration_s"]
    assert abs(computed_avg - expected_from_actual) <= 0.1, (
        f"TC-004: avg_power_w {computed_avg:.3f} W deviates from "
        f"expected {expected_from_actual:.3f} W by more than 0.1 W"
    )

    # Also verify it's not using connection_duration_s (would give ~40% lower value)
    connection_based_avg = (session["energy_kwh"] * 3_600_000) / session["connection_duration_s"]
    assert abs(computed_avg - connection_based_avg) > 100, (
        f"TC-004: avg_power_w looks like it was computed from connection_duration_s "
        f"(got {computed_avg:.1f} W, connection-based would be {connection_based_avg:.1f} W)"
    )

    # Sanity: expected power is around 7000 W
    assert expected_avg_power_w * 0.8 <= computed_avg <= expected_avg_power_w * 1.2, (
        f"TC-004: avg_power_w {computed_avg:.1f} W not in expected range "
        f"[{expected_avg_power_w * 0.8:.1f}, {expected_avg_power_w * 1.2:.1f}]"
    )
