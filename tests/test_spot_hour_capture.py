"""PR-29 (US5 / FR-008…FR-010): spot price captured at hour START — v2 engine.

The bug: the hourly tick at HH:00 priced the hour that just ENDED with a
price read AT the tick — exactly when Nordpool-style sensors flip to the
new hour's price. Whenever the price entity updated before the
integration's tick, the whole previous hour was billed at the wrong hour's
price.

The fix: capture (price, fallback) at each accounting hour's START
(session start, resume re-arm, and each hourly boundary after closing the
previous hour) and price every closing hour — hourly snapshot AND the
final partial hour at session end — with the captured pair.

All tests pin the frozen clock mid-hour (PR-28 lesson: freezegun otherwise
freezes at the real current time, and tests near a real UTC hour boundary
become nondeterministic).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ev_charging_manager.const import (
    CONF_CHARGING_IDLE_TIMEOUT_MIN,
    CONF_DISCONNECT_GRACE_MIN,
    DOMAIN,
    SessionEngineState,
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
MOCK_SPOT_PRICE_ENTITY = "sensor.nordpool_kwh"

MOCK_SPOT_V2_DATA = {
    **MOCK_CHARGER_DATA,
    "charger_profile": "goe_gemini",
    "pricing_mode": "spot",
    "spot_price_entity": MOCK_SPOT_PRICE_ENTITY,
    "spot_additional_cost_kwh": 0.85,
    "spot_vat_multiplier": 1.25,
    "spot_fallback_price_kwh": 2.50,
}

IDLE_TIMEOUT_MIN = 3


@pytest.fixture(autouse=True)
def _pin_clock_mid_hour(freezer) -> None:
    """Pin the frozen clock mid-hour (UTC) before engine setup (PR-28 lesson)."""
    freezer.move_to("2026-06-12T12:30:00+00:00")


async def _make_spot_entry(
    hass: HomeAssistant,
    *,
    spot_price: str = "1.00",
    data: dict | None = None,
) -> MockConfigEntry:
    """Create and set up a goe_gemini spot entry with idle charger states."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data or MOCK_SPOT_V2_DATA,
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
            CONF_CHARGING_IDLE_TIMEOUT_MIN: IDLE_TIMEOUT_MIN,
            CONF_DISCONNECT_GRACE_MIN: 10,
        },
        title="Test go-e Charger (spot capture)",
    )

    hass.states.async_set(MOCK_PLUG_ENTITY, "off")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
    hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, spot_price)

    entry.add_to_hass(hass)
    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _get_engine(hass: HomeAssistant, entry: MockConfigEntry) -> PlugAnchoredSessionEngine:
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert isinstance(engine, PlugAnchoredSessionEngine)
    return engine


async def _start_session(hass: HomeAssistant) -> None:
    """Start a session via the blip-first fast path and begin charging."""
    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
    await hass.async_block_till_done()


async def _fire_hour_boundary(hass: HomeAssistant, freezer, minutes: int) -> None:
    """Advance the frozen clock by `minutes` and fire the time-changed event."""
    freezer.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# (a) Boundary race: price flips BEFORE the hourly tick
# ---------------------------------------------------------------------------


async def test_boundary_race_closed_hour_uses_captured_price(hass: HomeAssistant, freezer) -> None:
    """FR-008/FR-009: the closing hour is billed at the price captured at its
    START even when the price entity flips to the new hour's price before the
    integration's hourly tick; the new price becomes the NEXT hour's capture."""
    entry = await _make_spot_entry(hass, spot_price="1.00")
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # 12:30 — session starts; hour-12 price captured = 1.00
        await _start_session(hass)
        session = engine.active_session
        assert session is not None

        # Charge 1.2 kWh during hour 12
        hass.states.async_set(MOCK_ENERGY_ENTITY, "1.2")
        await hass.async_block_till_done()

        # THE RACE: Nordpool flips to hour-13's price BEFORE our 13:00 tick
        hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, "2.00")
        await hass.async_block_till_done()

        # 13:00 — hourly tick
        await _fire_hour_boundary(hass, freezer, 30)

        assert session.price_details is not None
        assert len(session.price_details) == 1
        h12 = session.price_details[0]
        assert h12["spot_price_kr_kwh"] == 1.00, (
            f"FR-008: hour 12 must be billed at its CAPTURED price 1.00, "
            f"got {h12['spot_price_kr_kwh']!r} (read-at-tick bug)"
        )
        assert h12["fallback"] is False
        assert abs(h12["kwh"] - 1.2) < 0.001
        # 1.2 × (1.00 + 0.85) × 1.25 = 2.775
        assert abs(h12["cost_kr"] - 2.775) < 0.01

        # Hour 13: charge 2.0 kWh more; price flips to 3.00 before the 14:00 tick
        hass.states.async_set(MOCK_ENERGY_ENTITY, "3.2")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, "3.00")
        await hass.async_block_till_done()

        # 14:00 — hourly tick
        await _fire_hour_boundary(hass, freezer, 60)

        assert len(session.price_details) == 2
        h13 = session.price_details[1]
        assert h13["spot_price_kr_kwh"] == 2.00, (
            "FR-009: the price read at the 13:00 boundary (2.00) must price hour 13"
        )
        assert abs(h13["kwh"] - 2.0) < 0.001


# ---------------------------------------------------------------------------
# (b) Final partial hour priced with the captured value
# ---------------------------------------------------------------------------


async def test_final_partial_hour_uses_captured_price(hass: HomeAssistant, freezer) -> None:
    """FR-008: the final partial hour at session end is billed at the price
    captured at that hour's start — not a fresh read at completion time."""
    entry = await _make_spot_entry(hass, spot_price="1.00")
    engine = _get_engine(hass, entry)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _start_session(hass)
        assert engine.active_session is not None

        # Charge 1.5 kWh within the hour (5 min — past the micro-filter)
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        hass.states.async_set(MOCK_ENERGY_ENTITY, "1.5")
        await hass.async_block_till_done()

        # Price entity flips mid-hour (early flip / glitch) — must NOT affect
        # the in-progress hour's billing
        hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, "2.50")
        await hass.async_block_till_done()

        # Unplug → completion finalizes the partial hour
        hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    sessions = session_store.sessions
    assert len(sessions) == 1, f"Expected 1 completed session, got {len(sessions)}"
    details = sessions[0]["price_details"]
    assert details is not None and len(details) == 1
    final = details[0]
    assert final["spot_price_kr_kwh"] == 1.00, (
        f"FR-008: final partial hour must use the captured 1.00, got {final['spot_price_kr_kwh']!r}"
    )
    assert final["fallback"] is False
    # 1.5 × (1.00 + 0.85) × 1.25 = 3.46875
    assert abs(final["cost_kr"] - 3.46875) < 0.01


# ---------------------------------------------------------------------------
# (c) Resume mid-session: capture-at-resume prices the in-progress hour
# ---------------------------------------------------------------------------


async def test_resume_captures_price_for_in_progress_hour(hass: HomeAssistant, freezer) -> None:
    """FR-008 scenario 3: after an HA restart resume, the in-progress hour is
    priced with the price captured at RESUME time (the pre-restart capture is
    in-memory and gone)."""
    now = dt_util.utcnow()
    snapshot = {
        "id": str(uuid.uuid4()),
        "user_name": "Resume User",
        "user_type": "regular",
        "vehicle_name": None,
        "vehicle_battery_kwh": None,
        "efficiency_factor": None,
        "rfid_index": None,
        "rfid_uid": None,
        "charger_name": "Test Charger",
        "started_at": (now - timedelta(hours=1)).isoformat(),
        "connected_at": (now - timedelta(hours=1)).isoformat(),
        "energy_start_kwh": 0.0,
        "energy_kwh": 5.0,
        "cost_total_kr": 10.0,
        "cost_method": "spot",
        "price_details": [],
        "charger_total_before_kwh": None,
        "max_power_w": 7200.0,
        "charging_started_at": (now - timedelta(hours=1)).isoformat(),
        "charging_ended_at": None,
        "charging_duration_s": 3600,
        "charging_window_count": 1,
    }

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_SPOT_V2_DATA,
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
            CONF_CHARGING_IDLE_TIMEOUT_MIN: IDLE_TIMEOUT_MIN,
            CONF_DISCONNECT_GRACE_MIN: 10,
        },
        title="Test go-e Charger (spot resume)",
    )

    # Charger state at restart: plug in, power flowing, price entity = 1.80
    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
    hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
    hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, "1.80")

    raw_store_data = {
        "version": 1,
        "minor_version": 2,
        "key": "ev_charging_manager_sessions",
        "data": [snapshot],
    }
    with (
        patch(
            "homeassistant.helpers.storage.Store.async_load",
            new_callable=AsyncMock,
            return_value=raw_store_data,
        ),
        patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock),
    ):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    engine = _get_engine(hass, entry)
    assert engine.state == SessionEngineState.TRACKING
    session = engine.active_session
    assert session is not None and session.id == snapshot["id"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Post-restart charging: +1.0 kWh during the in-progress hour
        hass.states.async_set(MOCK_ENERGY_ENTITY, "6.0")
        await hass.async_block_till_done()

        # Price flips before the boundary tick — the in-progress hour must
        # still be billed at the 1.80 captured at resume
        hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, "9.99")
        await hass.async_block_till_done()

        # 13:00 — hourly tick closes the resume hour
        await _fire_hour_boundary(hass, freezer, 30)

    assert session.price_details, "Resume hour must have been captured"
    resume_hour = session.price_details[-1]
    assert resume_hour["spot_price_kr_kwh"] == 1.80, (
        f"FR-008: in-progress hour after resume must be billed at the resume-time "
        f"capture 1.80, got {resume_hour['spot_price_kr_kwh']!r}"
    )
    assert abs(resume_hour["kwh"] - 1.0) < 0.001


# ---------------------------------------------------------------------------
# (d) Fallback evaluated at CAPTURE time (FR-010)
# ---------------------------------------------------------------------------


async def test_fallback_provenance_is_capture_time(hass: HomeAssistant, freezer) -> None:
    """FR-010: price entity unavailable at an hour's START → that hour uses the
    fallback price with fallback=True, even if the entity recovers mid-hour;
    the next hour (captured while available) is not fallback."""
    entry = await _make_spot_entry(hass, spot_price=STATE_UNAVAILABLE)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # 12:30 — session starts with the price entity UNAVAILABLE → captured fallback
        await _start_session(hass)
        session = engine.active_session
        assert session is not None

        hass.states.async_set(MOCK_ENERGY_ENTITY, "2.0")
        await hass.async_block_till_done()

        # Entity recovers mid-hour — capture-time provenance must still govern
        hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, "1.00")
        await hass.async_block_till_done()

        # 13:00 tick
        await _fire_hour_boundary(hass, freezer, 30)

        assert len(session.price_details) == 1
        h12 = session.price_details[0]
        assert h12["fallback"] is True, (
            "FR-010: hour captured while unavailable must carry fallback=True"
        )
        assert h12["spot_price_kr_kwh"] is None
        assert h12["total_price_kr_kwh"] == 2.50
        # 2.0 × 2.50 = 5.00
        assert abs(h12["cost_kr"] - 5.00) < 0.01

        # Hour 13 captured at 1.00 (available at the boundary) → not fallback
        hass.states.async_set(MOCK_ENERGY_ENTITY, "3.0")
        await hass.async_block_till_done()
        await _fire_hour_boundary(hass, freezer, 60)

        assert len(session.price_details) == 2
        h13 = session.price_details[1]
        assert h13["fallback"] is False
        assert h13["spot_price_kr_kwh"] == 1.00


# ---------------------------------------------------------------------------
# (e) Static mode untouched
# ---------------------------------------------------------------------------


async def test_static_mode_untouched_by_capture(hass: HomeAssistant, freezer) -> None:
    """FR-008 scenario 5: static pricing is unaffected — no price_details, cost
    = energy × static price, hour boundaries are non-events."""
    static_data = {**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"}
    entry = await _make_spot_entry(hass, data=static_data)
    engine = _get_engine(hass, entry)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await _start_session(hass)
        session = engine.active_session
        assert session is not None

        hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
        await hass.async_block_till_done()

        # Cross an hour boundary — nothing spot-related may happen
        await _fire_hour_boundary(hass, freezer, 30)

    assert session.cost_method == "static"
    assert session.price_details is None
    # 5.0 × 2.50 (MOCK_CHARGER_DATA static price)
    assert abs(session.cost_total_kr - 12.50) < 0.01
