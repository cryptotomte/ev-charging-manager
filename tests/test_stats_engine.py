"""Tests for StatsEngine — accumulation, rollover, unknown user, guest (T006/T013/T017/T020)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ev_charging_manager.const import (
    DOMAIN,
    EVENT_SESSION_COMPLETED,
)
from custom_components.ev_charging_manager.stats_engine import (
    GuestLastSession,
    MonthStats,
    StatsEngine,
    UserStats,
    _month_key_from_iso,
)
from custom_components.ev_charging_manager.stats_store import StatsStore

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return a minimal MockConfigEntry and register it in hass."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"charger_name": "Test Charger"},
        title="Test Charger",
    )
    entry.add_to_hass(hass)
    return entry


def _make_completed_event(
    user_name: str = "Petra",
    user_type: str = "regular",
    energy_kwh: float = 12.4,
    cost_kr: float = 31.0,
    started_at: str = "2026-03-14T14:00:00+01:00",
    ended_at: str = "2026-03-14T14:22:00+01:00",
) -> dict:
    """Return a session_completed event data dict."""
    return {
        "user_name": user_name,
        "user_type": user_type,
        "energy_kwh": energy_kwh,
        "cost_kr": cost_kr,
        "started_at": started_at,
        "ended_at": ended_at,
    }


async def _setup_engine(hass: HomeAssistant) -> tuple[StatsEngine, StatsStore, MockConfigEntry]:
    """Create and set up a StatsEngine with a fully mocked StatsStore."""
    entry = _make_entry(hass)
    store = StatsStore(hass)
    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=None):
        with patch.object(store._store, "async_save", new_callable=AsyncMock):
            engine = StatsEngine(hass, entry, store)
            await engine.async_setup()
            # Register in hass.data so sensors can find it
            hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})["stats_engine"] = engine
            return engine, store, entry


# ---------------------------------------------------------------------------
# T006: Accumulation tests
# ---------------------------------------------------------------------------


async def test_single_session_updates_all_fields(hass: HomeAssistant) -> None:
    """Single session_completed event updates all UserStats fields correctly."""
    engine, store, entry = await _setup_engine(hass)

    with patch.object(store._store, "async_save", new_callable=AsyncMock):
        hass.bus.async_fire(EVENT_SESSION_COMPLETED, _make_completed_event())
        await hass.async_block_till_done()

    assert "Petra" in engine.user_stats
    stats = engine.user_stats["Petra"]
    assert stats.total_energy_kwh == 12.4
    assert stats.total_cost_kr == 31.0
    assert stats.session_count == 1
    assert stats.last_session_at == "2026-03-14T14:22:00+01:00"


async def test_two_sessions_accumulate_correctly(hass: HomeAssistant) -> None:
    """Two sessions for the same user accumulate totals correctly."""
    engine, store, entry = await _setup_engine(hass)

    with patch.object(store._store, "async_save", new_callable=AsyncMock):
        hass.bus.async_fire(
            EVENT_SESSION_COMPLETED,
            _make_completed_event(energy_kwh=12.4, cost_kr=31.0),
        )
        await hass.async_block_till_done()

        hass.bus.async_fire(
            EVENT_SESSION_COMPLETED,
            _make_completed_event(
                energy_kwh=8.0,
                cost_kr=20.0,
                started_at="2026-03-15T10:00:00+01:00",
                ended_at="2026-03-15T11:00:00+01:00",
            ),
        )
        await hass.async_block_till_done()

    stats = engine.user_stats["Petra"]
    assert stats.session_count == 2
    assert round(stats.total_energy_kwh, 1) == 20.4
    assert round(stats.total_cost_kr, 2) == 51.0


async def test_two_users_get_independent_stats(hass: HomeAssistant) -> None:
    """Sessions for different users accumulate independently."""
    engine, store, entry = await _setup_engine(hass)

    with patch.object(store._store, "async_save", new_callable=AsyncMock):
        hass.bus.async_fire(
            EVENT_SESSION_COMPLETED,
            _make_completed_event(user_name="Petra", energy_kwh=12.4),
        )
        await hass.async_block_till_done()

        hass.bus.async_fire(
            EVENT_SESSION_COMPLETED,
            _make_completed_event(user_name="Paul", user_type="regular", energy_kwh=5.0),
        )
        await hass.async_block_till_done()

    assert engine.user_stats["Petra"].total_energy_kwh == 12.4
    assert engine.user_stats["Paul"].total_energy_kwh == 5.0
    assert engine.user_stats["Petra"].session_count == 1
    assert engine.user_stats["Paul"].session_count == 1


async def test_load_restores_state_from_store(hass: HomeAssistant) -> None:
    """StatsEngine.async_setup() loads persisted data from StatsStore."""
    entry = _make_entry(hass)
    store = StatsStore(hass)

    persisted = {
        "user_stats": {
            "Petra": {
                "user_name": "Petra",
                "user_type": "regular",
                "total_energy_kwh": 100.0,
                "total_cost_kr": 250.0,
                "session_count": 8,
                "last_session_at": "2026-02-01T10:00:00+00:00",
                "current_month": {
                    "month": "2026-02",
                    "energy_kwh": 10.0,
                    "cost_kr": 25.0,
                    "sessions": 1,
                },
                "previous_month": {
                    "month": "2026-01",
                    "energy_kwh": 90.0,
                    "cost_kr": 225.0,
                    "sessions": 7,
                },
            }
        },
        "guest_last": None,
    }

    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=persisted):
        with patch.object(store._store, "async_save", new_callable=AsyncMock):
            engine = StatsEngine(hass, entry, store)
            await engine.async_setup()

    assert "Petra" in engine.user_stats
    assert engine.user_stats["Petra"].total_energy_kwh == 100.0
    assert engine.user_stats["Petra"].session_count == 8


# ---------------------------------------------------------------------------
# T013: Month rollover tests
# ---------------------------------------------------------------------------


async def test_session_updates_current_month(hass: HomeAssistant) -> None:
    """Session updates current_month fields using started_at month (FR-006)."""
    engine, store, entry = await _setup_engine(hass)

    with patch.object(store._store, "async_save", new_callable=AsyncMock):
        hass.bus.async_fire(
            EVENT_SESSION_COMPLETED,
            _make_completed_event(
                energy_kwh=12.4, cost_kr=31.0, started_at="2026-03-14T14:00:00+01:00"
            ),
        )
        await hass.async_block_till_done()

    stats = engine.user_stats["Petra"]
    assert stats.current_month.month == "2026-03"
    assert stats.current_month.energy_kwh == 12.4
    assert stats.current_month.cost_kr == 31.0
    assert stats.current_month.sessions == 1


async def test_midnight_callback_on_day_1_rolls_over(hass: HomeAssistant) -> None:
    """Midnight callback on 1st of month copies current → previous and resets current."""
    engine, store, entry = await _setup_engine(hass)

    # Set up existing March data
    engine._user_stats["Petra"] = UserStats(
        user_name="Petra",
        user_type="regular",
        total_energy_kwh=45.2,
        total_cost_kr=113.0,
        session_count=3,
        last_session_at="2026-03-20T10:00:00+00:00",
        current_month=MonthStats(month="2026-03", energy_kwh=45.2, cost_kr=113.0, sessions=3),
        previous_month=MonthStats(month="2026-02", energy_kwh=0.0, cost_kr=0.0, sessions=0),
    )

    with patch.object(store._store, "async_save", new_callable=AsyncMock):
        # Fire time change: April 1st at midnight UTC
        async_fire_time_changed(hass, datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc))
        await hass.async_block_till_done()

    stats = engine.user_stats["Petra"]
    # Current month reset to April
    assert stats.current_month.month == "2026-04"
    assert stats.current_month.energy_kwh == 0.0
    assert stats.current_month.sessions == 0
    # Previous month = old March data
    assert stats.previous_month.month == "2026-03"
    assert stats.previous_month.energy_kwh == 45.2
    assert stats.previous_month.sessions == 3
    # Lifetime total unchanged
    assert stats.total_energy_kwh == 45.2


async def test_midnight_callback_on_non_first_day_does_nothing(hass: HomeAssistant) -> None:
    """Midnight callback on day != 1 does not roll over."""
    engine, store, entry = await _setup_engine(hass)

    engine._user_stats["Petra"] = UserStats(
        user_name="Petra",
        user_type="regular",
        current_month=MonthStats(month="2026-03", energy_kwh=10.0, cost_kr=25.0, sessions=1),
        previous_month=MonthStats(month="2026-02", energy_kwh=0.0, cost_kr=0.0, sessions=0),
    )

    save_mock = AsyncMock()
    with patch.object(store._store, "async_save", save_mock):
        # Fire time change: April 15th — NOT 1st
        async_fire_time_changed(hass, datetime(2026, 4, 15, 0, 0, 0, tzinfo=timezone.utc))
        await hass.async_block_till_done()

    # No rollover: current month unchanged
    assert engine.user_stats["Petra"].current_month.month == "2026-03"
    # No save triggered
    save_mock.assert_not_called()


async def test_rollover_is_idempotent(hass: HomeAssistant) -> None:
    """Month rollover is no-op if current_month already matches new month (FR-013)."""
    engine, store, entry = await _setup_engine(hass)

    # All users already on April (Unknown is always present from _setup_engine)
    engine._user_stats["Unknown"].current_month = MonthStats(
        month="2026-04", energy_kwh=0.0, cost_kr=0.0, sessions=0
    )
    engine._user_stats["Petra"] = UserStats(
        user_name="Petra",
        user_type="regular",
        current_month=MonthStats(month="2026-04", energy_kwh=5.0, cost_kr=12.5, sessions=1),
        previous_month=MonthStats(month="2026-03", energy_kwh=45.2, cost_kr=113.0, sessions=3),
    )

    save_mock = AsyncMock()
    with patch.object(store._store, "async_save", save_mock):
        async_fire_time_changed(hass, datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc))
        await hass.async_block_till_done()

    # No rollover: current and previous month unchanged
    assert engine.user_stats["Petra"].current_month.month == "2026-04"
    assert engine.user_stats["Petra"].current_month.energy_kwh == 5.0
    assert engine.user_stats["Petra"].previous_month.month == "2026-03"
    # No save since nothing changed
    save_mock.assert_not_called()


async def test_session_month_assignment_uses_started_at(hass: HomeAssistant) -> None:
    """Month is determined from started_at, not ended_at (FR-006).

    Session started in March but ended in April (after midnight on April 1st).
    Statistics should be counted in March.
    """
    engine, store, entry = await _setup_engine(hass)

    with patch.object(store._store, "async_save", new_callable=AsyncMock):
        hass.bus.async_fire(
            EVENT_SESSION_COMPLETED,
            _make_completed_event(
                energy_kwh=5.0,
                started_at="2026-03-31T23:45:00+01:00",  # March
                ended_at="2026-04-01T00:10:00+02:00",  # April
            ),
        )
        await hass.async_block_till_done()

    stats = engine.user_stats["Petra"]
    assert stats.current_month.month == "2026-03"  # March from started_at
    assert stats.current_month.energy_kwh == 5.0


async def test_inline_rollover_when_session_in_new_month(hass: HomeAssistant) -> None:
    """If started_at is in a new month, old month rolls over before accumulating."""
    engine, store, entry = await _setup_engine(hass)

    # Existing March stats
    engine._user_stats["Petra"] = UserStats(
        user_name="Petra",
        user_type="regular",
        total_energy_kwh=30.0,
        total_cost_kr=75.0,
        session_count=2,
        last_session_at="2026-03-20T10:00:00+00:00",
        current_month=MonthStats(month="2026-03", energy_kwh=30.0, cost_kr=75.0, sessions=2),
        previous_month=MonthStats(month="2026-02", energy_kwh=0.0, cost_kr=0.0, sessions=0),
    )

    with patch.object(store._store, "async_save", new_callable=AsyncMock):
        # Session in April
        hass.bus.async_fire(
            EVENT_SESSION_COMPLETED,
            _make_completed_event(
                energy_kwh=10.0, cost_kr=25.0, started_at="2026-04-05T14:00:00+02:00"
            ),
        )
        await hass.async_block_till_done()

    stats = engine.user_stats["Petra"]
    # Inline rollover happened: March moved to previous, April is current
    assert stats.previous_month.month == "2026-03"
    assert stats.previous_month.energy_kwh == 30.0
    assert stats.current_month.month == "2026-04"
    assert stats.current_month.energy_kwh == 10.0
    # Lifetime total includes both months
    assert round(stats.total_energy_kwh, 1) == 40.0


# ---------------------------------------------------------------------------
# T017: Unknown user tests
# ---------------------------------------------------------------------------


async def test_unknown_user_always_exists_at_setup(hass: HomeAssistant) -> None:
    """StatsEngine always initializes an 'Unknown' entry on setup (FR-007)."""
    engine, store, entry = await _setup_engine(hass)

    assert "Unknown" in engine.user_stats
    assert engine.user_stats["Unknown"].user_type == "unknown"
    assert engine.user_stats["Unknown"].session_count == 0


async def test_unknown_user_accumulates_from_events(hass: HomeAssistant) -> None:
    """Sessions with user_name='Unknown' accumulate under the Unknown user."""
    engine, store, entry = await _setup_engine(hass)

    with patch.object(store._store, "async_save", new_callable=AsyncMock):
        hass.bus.async_fire(
            EVENT_SESSION_COMPLETED,
            _make_completed_event(user_name="Unknown", user_type="unknown", energy_kwh=5.0),
        )
        await hass.async_block_till_done()

    assert engine.user_stats["Unknown"].total_energy_kwh == 5.0
    assert engine.user_stats["Unknown"].session_count == 1


async def test_unknown_user_independent_from_named_users(hass: HomeAssistant) -> None:
    """Unknown and named user stats are independent."""
    engine, store, entry = await _setup_engine(hass)

    with patch.object(store._store, "async_save", new_callable=AsyncMock):
        hass.bus.async_fire(
            EVENT_SESSION_COMPLETED,
            _make_completed_event(user_name="Petra", energy_kwh=10.0),
        )
        await hass.async_block_till_done()

        hass.bus.async_fire(
            EVENT_SESSION_COMPLETED,
            _make_completed_event(user_name="Unknown", user_type="unknown", energy_kwh=3.0),
        )
        await hass.async_block_till_done()

    assert engine.user_stats["Petra"].total_energy_kwh == 10.0
    assert engine.user_stats["Unknown"].total_energy_kwh == 3.0


# ---------------------------------------------------------------------------
# T020 (engine part): Guest last-session tests
# ---------------------------------------------------------------------------


async def test_guest_session_updates_guest_last(hass: HomeAssistant) -> None:
    """Guest session sets GuestLastSession on the engine."""
    engine, store, entry = await _setup_engine(hass)

    with patch.object(store._store, "async_save", new_callable=AsyncMock):
        hass.bus.async_fire(
            EVENT_SESSION_COMPLETED,
            _make_completed_event(
                user_name="Guest",
                user_type="guest",
                energy_kwh=32.1,
                ended_at="2026-04-10T17:32:05+02:00",
            ),
        )
        await hass.async_block_till_done()

    assert engine.guest_last is not None
    assert engine.guest_last.energy_kwh == 32.1
    assert engine.guest_last.charge_price_kr is None
    assert engine.guest_last.session_at == "2026-04-10T17:32:05+02:00"


async def test_non_guest_session_does_not_update_guest_last(hass: HomeAssistant) -> None:
    """Regular user session does NOT overwrite GuestLastSession."""
    engine, store, entry = await _setup_engine(hass)

    # Set an initial guest_last value
    engine._guest_last = GuestLastSession(energy_kwh=32.1, session_at="2026-04-10T17:32:05+02:00")

    with patch.object(store._store, "async_save", new_callable=AsyncMock):
        # Regular session
        hass.bus.async_fire(
            EVENT_SESSION_COMPLETED,
            _make_completed_event(user_name="Petra", user_type="regular", energy_kwh=8.0),
        )
        await hass.async_block_till_done()

    # Guest last unchanged
    assert engine.guest_last is not None
    assert engine.guest_last.energy_kwh == 32.1


async def test_second_guest_session_overwrites_guest_last(hass: HomeAssistant) -> None:
    """Second guest session overwrites the first GuestLastSession (FR-009)."""
    engine, store, entry = await _setup_engine(hass)

    with patch.object(store._store, "async_save", new_callable=AsyncMock):
        # First guest session
        hass.bus.async_fire(
            EVENT_SESSION_COMPLETED,
            _make_completed_event(user_name="Guest", user_type="guest", energy_kwh=32.1),
        )
        await hass.async_block_till_done()

        # Second guest session
        hass.bus.async_fire(
            EVENT_SESSION_COMPLETED,
            _make_completed_event(
                user_name="Guest",
                user_type="guest",
                energy_kwh=15.0,
                ended_at="2026-05-01T12:00:00+02:00",
            ),
        )
        await hass.async_block_till_done()

    assert engine.guest_last is not None
    assert engine.guest_last.energy_kwh == 15.0


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


def test_month_key_from_iso_valid() -> None:
    """Valid ISO timestamp returns YYYY-MM string."""
    assert _month_key_from_iso("2026-03-14T14:22:00+01:00") == "2026-03"
    assert _month_key_from_iso("2026-12-01T00:00:00+00:00") == "2026-12"


def test_month_key_from_iso_empty_returns_empty() -> None:
    """Empty or falsy input returns empty string."""
    assert _month_key_from_iso("") == ""
    assert _month_key_from_iso(None) == ""  # type: ignore[arg-type]


def test_month_key_from_iso_invalid_returns_empty() -> None:
    """Invalid timestamp returns empty string without raising."""
    assert _month_key_from_iso("not-a-date") == ""
