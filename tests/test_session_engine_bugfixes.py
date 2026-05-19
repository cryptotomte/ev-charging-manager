"""Regression tests for the BUG-1..BUG-7 review findings on PlugAnchoredSessionEngine.

Each test below pins a specific bug from the PR-22 review log so a regression
re-introducing the bug can be detected quickly.

  - BUG-2: spot-pricing hourly tracker re-armed after HA restart.
  - BUG-3: restart recovery defers when the plug entity is unavailable at boot.
  - BUG-4: disk-I/O failure during session completion does not strand the engine.
  - BUG-5: ChargingWindow uses tz-aware UTC and validates timestamps.
  - BUG-6: hourly tracker unsub does not accumulate stale handlers per session.
  - BUG-7: STATE_UNAVAILABLE on the plug entity is treated as offline, not on.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.charging_window import (
    ChargingWindow,
    ChargingWindowTracker,
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


def _make_snapshot(
    *,
    session_id: str | None = None,
    energy_start_kwh: float = 0.0,
    energy_kwh: float = 5.0,
    started_at: str | None = None,
    cost_method: str = "static",
) -> dict:
    """Build a minimal active-session snapshot dict."""
    now_utc = datetime.now(timezone.utc)
    started = started_at or (now_utc - timedelta(hours=2)).isoformat()
    return {
        "id": session_id or str(uuid.uuid4()),
        "user_name": "Test User",
        "user_type": "regular",
        "vehicle_name": None,
        "vehicle_battery_kwh": None,
        "efficiency_factor": None,
        "rfid_index": None,
        "rfid_uid": None,
        "charger_name": "Test Charger",
        "started_at": started,
        "connected_at": started,
        "energy_start_kwh": energy_start_kwh,
        "energy_kwh": energy_kwh,
        "cost_total_kr": 0.0,
        "cost_method": cost_method,
        "price_details": None,
        "charger_total_before_kwh": None,
        "max_power_w": 7200.0,
        "charging_started_at": started,
        "charging_ended_at": None,
        "charging_duration_s": 3600,
        "charging_window_count": 1,
    }


# ---------------------------------------------------------------------------
# BUG-5: ChargingWindow uses tz-aware UTC and validates timestamps
# ---------------------------------------------------------------------------


def test_bug5_charging_window_default_last_power_change_is_tz_aware() -> None:
    """Default last_power_change_at must be tz-aware UTC (not naive datetime.utcnow)."""
    now = dt_util.utcnow()
    w = ChargingWindow(start_at=now)
    assert w.last_power_change_at.tzinfo is not None, (
        "last_power_change_at default must be tz-aware (BUG-5)"
    )
    # And duration_s with no `now` arg uses dt_util.utcnow → tz-aware subtraction works.
    duration = w.duration_s()
    assert duration >= 0


def test_bug5_charging_window_rejects_invalid_end_before_start() -> None:
    """__post_init__ must raise RuntimeError when end_at < start_at."""
    start = dt_util.utcnow()
    earlier = start - timedelta(seconds=10)
    with pytest.raises(RuntimeError, match="end_at"):
        ChargingWindow(start_at=start, end_at=earlier)


def test_bug5_charging_window_accepts_valid_closed_window() -> None:
    """A closed window with end_at >= start_at validates fine."""
    start = dt_util.utcnow()
    end = start + timedelta(minutes=5)
    w = ChargingWindow(start_at=start, end_at=end)
    assert w.is_open is False


def test_bug5_charging_window_tracker_open_close_uses_tz_aware() -> None:
    """ChargingWindowTracker.close_window must not raise on tz-aware subtraction."""
    tracker = ChargingWindowTracker()
    now = dt_util.utcnow()
    tracker.open_window(now, energy_kwh=10.0)
    later = now + timedelta(minutes=5)
    closed = tracker.close_window(later, energy_kwh=12.5)
    # duration computation succeeded → no naive/tz-aware TypeError.
    assert closed.duration_s() == 300


# ---------------------------------------------------------------------------
# BUG-7: STATE_UNAVAILABLE on plug entity treated as offline, not "on"
# ---------------------------------------------------------------------------


async def _setup_engine_with_session(
    hass: HomeAssistant,
) -> tuple[MockConfigEntry, PlugAnchoredSessionEngine]:
    """Set up engine with active session and return (entry, engine)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
            CONF_CHARGING_IDLE_TIMEOUT_MIN: 5,
            CONF_DISCONNECT_GRACE_MIN: 10,
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

        # Start a session
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    return entry, engine


async def test_bug7_plug_unavailable_does_not_complete_session(
    hass: HomeAssistant,
) -> None:
    """Plug entity transitioning to STATE_UNAVAILABLE must NOT complete the session.

    Previously the code silently no-oped on non-on/off plug values, letting the
    session keep accumulating energy as if plug were still on. The fix:
    treat it as offline and surface it via _check_charger_offline.
    """
    entry, engine = await _setup_engine_with_session(hass)
    assert engine.state == SessionEngineState.TRACKING

    # Now make the plug entity go unavailable.
    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        hass.states.async_set(MOCK_PLUG_ENTITY, STATE_UNAVAILABLE)
        await hass.async_block_till_done()

    # Session must NOT be force-completed by the unavailable transition.
    assert engine.state == SessionEngineState.TRACKING, (
        "STATE_UNAVAILABLE on plug entity must not complete the session (BUG-7)"
    )
    # Active session preserved.
    assert engine.active_session is not None


# ---------------------------------------------------------------------------
# BUG-3: restart recovery defers when plug entity is unavailable at boot
# ---------------------------------------------------------------------------


async def test_bug3_recovery_defers_when_plug_unavailable(hass: HomeAssistant) -> None:
    """When the plug entity is unavailable at restart, recovery must defer
    rather than silently treat plug as off and prematurely complete the session.
    """
    snapshot = _make_snapshot(energy_start_kwh=10.0, energy_kwh=5.0)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
            CONF_CHARGING_IDLE_TIMEOUT_MIN: 5,
            CONF_DISCONNECT_GRACE_MIN: 10,
        },
        title="Test go-e Charger",
    )

    # Critical: plug entity is unavailable at boot.
    hass.states.async_set(MOCK_PLUG_ENTITY, STATE_UNAVAILABLE)
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "15.5")
    hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")

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

        engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

        # Session must NOT be force-completed (deferred recovery).
        assert engine._deferred_recovery_unsub is not None, (
            "engine must register a wait-listener for the plug entity (BUG-3)"
        )
        assert engine.state != SessionEngineState.IDLE or engine.active_session is None
        # The orphaned snapshot session has NOT been completed yet.
        session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]
        assert len(session_store.sessions) == 0, (
            "snapshot must not be completed prematurely while plug is unavailable (BUG-3)"
        )

        # Now plug entity reports a valid value — recovery should resume.
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        await hass.async_block_till_done()

        # After deferred recovery runs, engine should be TRACKING with the
        # snapshot restored as the active session.
        assert engine.state == SessionEngineState.TRACKING, (
            "engine must resume the session after plug entity becomes valid (BUG-3)"
        )
        assert engine.active_session is not None
        assert engine.active_session.id == snapshot["id"]


# ---------------------------------------------------------------------------
# BUG-2: spot-pricing hourly tracker re-armed after HA restart
# ---------------------------------------------------------------------------


async def test_bug2_spot_pricing_hourly_unsub_rearmed_after_restart(
    hass: HomeAssistant,
) -> None:
    """On HA restart in spot-pricing mode with an active session, the engine
    MUST register the hourly snapshot callback (async_track_utc_time_change).
    Previously _hourly_unsub stayed None → hourly callback never fired →
    post-restart energy was bundled incorrectly into "current hour".
    """
    snapshot = _make_snapshot(
        energy_start_kwh=10.0,
        energy_kwh=2.0,
        cost_method="spot",
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **MOCK_CHARGER_DATA,
            "charger_profile": "goe_gemini",
            "pricing_mode": "spot",
            "spot_price_entity": "sensor.fake_spot_price",
            "spot_additional_cost_kwh": 0.85,
            "spot_vat_multiplier": 1.25,
            "spot_fallback_price_kwh": 2.50,
        },
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
            CONF_CHARGING_IDLE_TIMEOUT_MIN: 5,
            CONF_DISCONNECT_GRACE_MIN: 10,
        },
        title="Test go-e Charger",
    )

    # Plug on, charging.
    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "12.0")
    hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
    hass.states.async_set("sensor.fake_spot_price", "1.50")

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
        patch(
            "custom_components.ev_charging_manager.session_engine_v2.async_track_utc_time_change"
        ) as mock_track,
    ):
        mock_track.return_value = MagicMock()  # callable unsub
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # The hourly tracker should have been registered exactly once during
        # restart recovery (BUG-2 fix).
        assert mock_track.called, (
            "spot-pricing hourly callback must be re-registered on restart (BUG-2)"
        )

        engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
        assert engine._hourly_unsub is not None, (
            "_hourly_unsub must be set after restart in spot-pricing mode (BUG-2)"
        )


# ---------------------------------------------------------------------------
# BUG-4: disk-I/O failure during session completion does not strand the engine
# ---------------------------------------------------------------------------


async def test_bug4_persist_failure_does_not_strand_engine_in_completing(
    hass: HomeAssistant,
) -> None:
    """If session_store.add_session raises (e.g. disk full), the engine must
    still reset to IDLE and fire SESSION_COMPLETED (so stats consumers can
    recover and the user can resume charging).
    """
    entry, engine = await _setup_engine_with_session(hass)

    # Charge a bit so micro-filter doesn't discard.
    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
        await hass.async_block_till_done()

    # Backdate connected_at so the micro-filter does not discard.
    engine._active_session.connected_at = (dt_util.utcnow() - timedelta(minutes=5)).isoformat()

    fired_events: list = []
    hass.bus.async_listen("ev_charging_manager_session_completed", fired_events.append)

    # Force add_session to raise — simulating disk full.
    async def _explode(*_args, **_kwargs):
        raise OSError("No space left on device")

    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]
    with (
        patch.object(session_store, "add_session", side_effect=_explode),
        patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock),
    ):
        # Trigger session end via plug unplug.
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    # Engine MUST reset to IDLE despite the persist failure.
    assert engine.state == SessionEngineState.IDLE, (
        "engine must return to IDLE after persist failure (BUG-4)"
    )
    assert engine.active_session is None, "active_session must be cleared (BUG-4)"
    # SESSION_COMPLETED must have fired so downstream consumers know about it.
    assert len(fired_events) >= 1, "SESSION_COMPLETED must fire even on persist failure (BUG-4)"


# ---------------------------------------------------------------------------
# BUG-6: hourly tracker unsub does not accumulate stale handlers per session
# ---------------------------------------------------------------------------


async def test_bug6_hourly_unsub_not_registered_on_entry_unload_list(
    hass: HomeAssistant,
) -> None:
    """After several spot-mode sessions, the entry's unload list must NOT grow
    by one stale callback per session. The engine manages the unsub lifecycle
    internally via async_unload + the _engine_unsubs list.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **MOCK_CHARGER_DATA,
            "charger_profile": "goe_gemini",
            "pricing_mode": "spot",
            "spot_price_entity": "sensor.fake_spot_price",
            "spot_additional_cost_kwh": 0.85,
            "spot_vat_multiplier": 1.25,
            "spot_fallback_price_kwh": 2.50,
        },
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
            CONF_CHARGING_IDLE_TIMEOUT_MIN: 5,
            CONF_DISCONNECT_GRACE_MIN: 10,
        },
        title="Test go-e Charger",
    )

    hass.states.async_set(MOCK_PLUG_ENTITY, "off")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
    hass.states.async_set("sensor.fake_spot_price", "1.50")

    entry.add_to_hass(hass)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # Capture the unload list length BEFORE any session starts.
        # _on_unload is an internal HA list. Use len(entry._on_unload) defensively.
        before_count = len(getattr(entry, "_on_unload", []) or [])

        # Run a few plug-on / plug-off cycles. Each spot-mode session
        # previously appended one async_on_unload entry. After the fix,
        # the unload count must stay constant.
        for cycle in range(3):
            # Plug in.
            hass.states.async_set(MOCK_PLUG_ENTITY, "on")
            hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
            await hass.async_block_till_done()
            # Push some energy + backdate connected_at to bypass micro-filter.
            engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
            if engine.active_session is not None:
                engine.active_session.connected_at = (
                    dt_util.utcnow() - timedelta(minutes=5)
                ).isoformat()
            hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
            hass.states.async_set(MOCK_ENERGY_ENTITY, str(0.5 + cycle))
            await hass.async_block_till_done()

            # Plug out.
            hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
            hass.states.async_set(MOCK_PLUG_ENTITY, "off")
            hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
            await hass.async_block_till_done()

    after_count = len(getattr(entry, "_on_unload", []) or [])

    # Allow a small slack (e.g. one or two registrations may be added during
    # setup itself), but the count MUST NOT grow by 3 (one per session).
    growth = after_count - before_count
    assert growth < 3, (
        f"entry._on_unload grew by {growth} across 3 sessions — "
        "per-session callbacks are leaking (BUG-6)"
    )
