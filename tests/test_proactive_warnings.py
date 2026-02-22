"""Tests for proactive unknown-session warnings via persistent_notification (US5, FR-012/015)."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.const import (
    DOMAIN,
    UNKNOWN_SESSION_THRESHOLD,
    UNKNOWN_SESSION_WINDOW_DAYS,
)
from custom_components.ev_charging_manager.stats_engine import _prune_old_unknown_times
from tests.conftest import (
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    setup_session_engine,
    start_charging_session,
    stop_charging_session,
)

NO_FILTER_OPTIONS = {"min_session_duration_s": 0, "min_session_energy_wh": 0}

# Patch target for persistent_notification.async_create
_PN_CREATE_PATCH = "custom_components.ev_charging_manager.stats_engine.pn_async_create"


async def _complete_unknown_session(hass: HomeAssistant) -> None:
    """Helper: start and stop a session attributed to Unknown (trx=0)."""
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="0")
    await stop_charging_session(hass)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# FR-012: <3 unknown sessions — no notification
# ---------------------------------------------------------------------------


async def test_less_than_threshold_no_notification(hass: HomeAssistant) -> None:
    """FR-012: 2 unknown sessions in 7 days should NOT trigger a notification."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=NO_FILTER_OPTIONS, title="go-e"
    )
    await setup_session_engine(hass, entry)

    with patch(_PN_CREATE_PATCH) as mock_create:
        # Complete THRESHOLD-1 unknown sessions
        for _ in range(UNKNOWN_SESSION_THRESHOLD - 1):
            await _complete_unknown_session(hass)

        assert not mock_create.called, "Notification must NOT fire below threshold"


# ---------------------------------------------------------------------------
# FR-012: 3 unknown sessions in window — notification fires
# ---------------------------------------------------------------------------


async def test_threshold_unknown_sessions_triggers_notification(hass: HomeAssistant) -> None:
    """FR-012: 3 unknown sessions in 7 days triggers a persistent notification."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=NO_FILTER_OPTIONS, title="go-e"
    )
    await setup_session_engine(hass, entry)

    with patch(_PN_CREATE_PATCH) as mock_create:
        for _ in range(UNKNOWN_SESSION_THRESHOLD):
            await _complete_unknown_session(hass)

        assert mock_create.called, "Notification must fire when threshold is reached"

        # Verify notification_id contains the entry_id (for deduplication)
        call_kwargs = mock_create.call_args
        if call_kwargs.kwargs:
            assert "notification_id" in call_kwargs.kwargs, "notification_id must be set"
            assert entry.entry_id in call_kwargs.kwargs["notification_id"]


# ---------------------------------------------------------------------------
# FR-013: 4th unknown session — no duplicate (same notification_id)
# ---------------------------------------------------------------------------


async def test_4th_unknown_no_duplicate_notification(hass: HomeAssistant) -> None:
    """FR-013: Subsequent unknown sessions do not create duplicate notifications."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=NO_FILTER_OPTIONS, title="go-e"
    )
    await setup_session_engine(hass, entry)

    with patch(_PN_CREATE_PATCH) as mock_create:
        # Complete THRESHOLD+1 unknown sessions
        for _ in range(UNKNOWN_SESSION_THRESHOLD + 1):
            await _complete_unknown_session(hass)

        # Called more than once (each triggers the create with same notification_id),
        # but HA deduplicates by notification_id. Verify calls used same notification_id.
        assert mock_create.call_count >= UNKNOWN_SESSION_THRESHOLD - 1, (
            "Notification must fire at threshold and subsequent unknown sessions"
        )

        # All calls use the same notification_id
        notification_ids = set()
        for call in mock_create.call_args_list:
            nid = call.kwargs.get("notification_id")
            if nid:
                notification_ids.add(nid)
        if notification_ids:
            assert len(notification_ids) == 1, "All calls must use the same notification_id"


# ---------------------------------------------------------------------------
# FR-014: Sessions outside 7-day window excluded from count
# ---------------------------------------------------------------------------


async def test_old_timestamps_pruned_from_window(hass: HomeAssistant) -> None:
    """FR-014: Timestamps >7 days old are pruned and excluded from threshold count."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=NO_FILTER_OPTIONS, title="go-e"
    )
    await setup_session_engine(hass, entry)
    stats_engine = hass.data[DOMAIN][entry.entry_id]["stats_engine"]

    # Seed engine with old timestamps (> 7 days ago) — just below threshold
    old_time = (dt_util.utcnow() - timedelta(days=UNKNOWN_SESSION_WINDOW_DAYS + 1)).isoformat()
    stats_engine._unknown_session_times = [old_time] * (UNKNOWN_SESSION_THRESHOLD - 1)

    with patch(_PN_CREATE_PATCH) as mock_create:
        # One new unknown session — total would be THRESHOLD but old ones should be pruned
        await _complete_unknown_session(hass)

        # After pruning, only 1 new timestamp remains — below threshold
        assert not mock_create.called, (
            "Notification must NOT fire when old timestamps are pruned below threshold"
        )
        assert len(stats_engine._unknown_session_times) == 1, (
            "Only the new timestamp should remain after pruning"
        )


# ---------------------------------------------------------------------------
# Unit test: _prune_old_unknown_times helper
# ---------------------------------------------------------------------------


async def test_prune_function_removes_old_timestamps() -> None:
    """Unit test: _prune_old_unknown_times removes entries older than WINDOW_DAYS."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    now = dt_util.utcnow()
    recent = now.isoformat()
    just_inside = (now - timedelta(days=UNKNOWN_SESSION_WINDOW_DAYS - 1)).isoformat()
    just_outside = (now - timedelta(days=UNKNOWN_SESSION_WINDOW_DAYS + 1)).isoformat()

    times = [recent, just_inside, just_outside]
    pruned = _prune_old_unknown_times(times)

    assert recent in pruned
    assert just_inside in pruned
    assert just_outside not in pruned, "Timestamps older than window must be pruned"


async def test_prune_function_ignores_malformed_timestamps() -> None:
    """Unit test: _prune_old_unknown_times discards malformed entries without crashing."""
    from homeassistant.util import dt as dt_util

    now = dt_util.utcnow().isoformat()
    times = [now, "not-a-date", "", "2026-invalid"]
    pruned = _prune_old_unknown_times(times)

    assert now in pruned
    assert "not-a-date" not in pruned
    assert "" not in pruned
