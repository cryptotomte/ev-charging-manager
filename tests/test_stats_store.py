"""Tests for StatsStore — persistence layer for per-user statistics (T007)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant

from custom_components.ev_charging_manager.stats_engine import (
    GuestLastSession,
    MonthStats,
    UserStats,
)
from custom_components.ev_charging_manager.stats_store import StatsStore


def _make_user_stats(
    user_name: str = "Petra",
    user_type: str = "regular",
    total_energy: float = 12.4,
    total_cost: float = 31.0,
    sessions: int = 1,
) -> UserStats:
    """Create a UserStats instance for testing."""
    return UserStats(
        user_name=user_name,
        user_type=user_type,
        total_energy_kwh=total_energy,
        total_cost_kr=total_cost,
        session_count=sessions,
        last_session_at="2026-03-14T14:22:00+01:00",
        current_month=MonthStats(month="2026-03", energy_kwh=12.4, cost_kr=31.0, sessions=1),
        previous_month=MonthStats(month="2026-02", energy_kwh=0.0, cost_kr=0.0, sessions=0),
    )


async def test_load_empty_store_returns_defaults(hass: HomeAssistant) -> None:
    """Loading a missing store returns empty dict and None guest_last."""
    store = StatsStore(hass)
    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=None):
        user_stats, guest_last = await store.async_load()

    assert user_stats == {}
    assert guest_last is None


async def test_save_and_load_roundtrip_preserves_userstats(hass: HomeAssistant) -> None:
    """Saving then loading preserves UserStats data faithfully."""
    store = StatsStore(hass)
    petra = _make_user_stats()
    user_stats_in = {"Petra": petra}

    saved_data: dict = {}

    async def fake_save(data: dict) -> None:
        saved_data.update(data)

    async def fake_load() -> dict:
        return saved_data if saved_data else None  # type: ignore[return-value]

    with patch.object(store._store, "async_save", side_effect=fake_save):
        await store.async_save(user_stats_in, None)

    with patch.object(store._store, "async_load", side_effect=fake_load):
        user_stats_out, guest_last_out = await store.async_load()

    assert "Petra" in user_stats_out
    out = user_stats_out["Petra"]
    assert out.user_name == "Petra"
    assert out.user_type == "regular"
    assert out.total_energy_kwh == 12.4
    assert out.total_cost_kr == 31.0
    assert out.session_count == 1
    assert out.last_session_at == "2026-03-14T14:22:00+01:00"
    assert out.current_month.month == "2026-03"
    assert out.current_month.energy_kwh == 12.4
    assert out.previous_month.month == "2026-02"
    assert guest_last_out is None


async def test_save_and_load_roundtrip_preserves_guest_last(hass: HomeAssistant) -> None:
    """Saving then loading preserves GuestLastSession data."""
    store = StatsStore(hass)
    guest = GuestLastSession(
        energy_kwh=32.1, charge_price_kr=None, session_at="2026-04-10T17:32:05+02:00"
    )

    saved_data: dict = {}

    async def fake_save(data: dict) -> None:
        saved_data.update(data)

    async def fake_load() -> dict:
        return saved_data if saved_data else None  # type: ignore[return-value]

    with patch.object(store._store, "async_save", side_effect=fake_save):
        await store.async_save({}, guest)

    with patch.object(store._store, "async_load", side_effect=fake_load):
        _, guest_out = await store.async_load()

    assert guest_out is not None
    assert guest_out.energy_kwh == 32.1
    assert guest_out.charge_price_kr is None
    assert guest_out.session_at == "2026-04-10T17:32:05+02:00"


async def test_load_skips_malformed_user_entry(hass: HomeAssistant) -> None:
    """Malformed user stats entries (missing user_name) are skipped with a warning."""
    store = StatsStore(hass)
    bad_data = {
        "user_stats": {
            "Petra": {"user_name": "Petra", "user_type": "regular"},  # valid minimal
            "Broken": {"total_energy_kwh": "not-a-number"},  # malformed: no user_name
        },
        "guest_last": None,
    }

    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=bad_data):
        user_stats, _ = await store.async_load()

    # Only the valid entry survives
    assert "Petra" in user_stats
    assert "Broken" not in user_stats


async def test_load_handles_missing_guest_last(hass: HomeAssistant) -> None:
    """If guest_last is absent (None), load returns None for guest_last."""
    store = StatsStore(hass)
    data = {"user_stats": {}, "guest_last": None}

    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=data):
        _, guest_last = await store.async_load()

    assert guest_last is None


async def test_save_multiple_users(hass: HomeAssistant) -> None:
    """Saving multiple users and loading them back preserves all entries."""
    store = StatsStore(hass)
    petra = _make_user_stats("Petra")
    paul = _make_user_stats("Paul", total_energy=8.0, total_cost=20.0, sessions=2)
    unknown = UserStats(user_name="Unknown", user_type="unknown")

    saved_data: dict = {}

    async def fake_save(data: dict) -> None:
        saved_data.update(data)

    async def fake_load() -> dict:
        return saved_data if saved_data else None  # type: ignore[return-value]

    with patch.object(store._store, "async_save", side_effect=fake_save):
        await store.async_save({"Petra": petra, "Paul": paul, "Unknown": unknown}, None)

    with patch.object(store._store, "async_load", side_effect=fake_load):
        user_stats, _ = await store.async_load()

    assert set(user_stats.keys()) == {"Petra", "Paul", "Unknown"}
    assert user_stats["Paul"].session_count == 2
    assert user_stats["Unknown"].total_energy_kwh == 0.0
