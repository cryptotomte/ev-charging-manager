"""Tests for SessionStore (T011)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ev_charging_manager.session_store import SessionStore


def make_session(session_id: str = "abc", energy: float = 5.0) -> dict:
    """Create a minimal completed session dict for testing."""
    return {
        "id": session_id,
        "energy_kwh": energy,
        "user_name": "Petra",
        "ended_at": "2026-03-01T12:00:00+00:00",
    }


@pytest.fixture
async def session_store(hass: HomeAssistant) -> SessionStore:
    """Return a SessionStore with mocked internal Store."""
    store = SessionStore(hass, max_sessions=1000)
    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=None):
        with patch.object(store._store, "async_save", new_callable=AsyncMock):
            await store.async_load()
            yield store


@pytest.fixture
async def session_store_with_save(hass: HomeAssistant):
    """Return a SessionStore with accessible mock for async_save."""
    store = SessionStore(hass, max_sessions=1000)
    mock_save = AsyncMock()
    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=None):
        with patch.object(store._store, "async_save", mock_save):
            await store.async_load()
            yield store, mock_save


async def test_load_empty_store_returns_empty(hass: HomeAssistant):
    """Loading a missing store returns ([], None) tuple."""
    store = SessionStore(hass)
    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=None):
        sessions, snapshot = await store.async_load()
    assert sessions == []
    assert snapshot is None
    assert store.sessions == []


async def test_add_session_persisted(session_store_with_save):
    """Adding a session persists it to storage."""
    store, mock_save = session_store_with_save
    session = make_session("s1")
    await store.add_session(session)
    assert len(store.sessions) == 1
    assert store.sessions[0]["id"] == "s1"
    mock_save.assert_called_once()


async def test_add_1001_sessions_prunes_oldest(hass: HomeAssistant):
    """Adding 1001 sessions with max=1000 removes the oldest."""
    store = SessionStore(hass, max_sessions=1000)
    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=None):
        with patch.object(store._store, "async_save", new_callable=AsyncMock):
            await store.async_load()
            # Add 1000 sessions
            for i in range(1000):
                store._sessions.append(make_session(f"s{i}"))
            # Add one more — should trigger pruning
            await store.add_session(make_session("s_new"))
    assert len(store.sessions) == 1000
    # Oldest (s0) should be removed
    assert store.sessions[0]["id"] == "s1"
    assert store.sessions[-1]["id"] == "s_new"


async def test_load_persisted_sessions_survives_reload(hass: HomeAssistant):
    """Sessions loaded from persistent storage are available."""
    stored_data = [make_session("s_persisted")]
    store = SessionStore(hass)
    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=stored_data):
        await store.async_load()
    assert len(store.sessions) == 1
    assert store.sessions[0]["id"] == "s_persisted"


async def test_periodic_save_is_scheduled(hass: HomeAssistant):
    """schedule_periodic_save registers an async_track_time_interval."""
    store = SessionStore(hass)

    mock_entry = MagicMock()
    unload_callbacks = []
    mock_entry.async_on_unload = lambda cb: unload_callbacks.append(cb)

    with patch(
        "custom_components.ev_charging_manager.session_store.async_track_time_interval"
    ) as mock_track:
        mock_track.return_value = MagicMock()
        store.schedule_periodic_save(hass, mock_entry, 300, lambda: None)

    mock_track.assert_called_once()
    # Interval should be 300 seconds
    call_args = mock_track.call_args
    from datetime import timedelta

    assert call_args[0][2] == timedelta(seconds=300)
    # Unload callback should be registered
    assert len(unload_callbacks) == 1


async def test_load_separates_snapshot_from_completed(hass: HomeAssistant):
    """Incomplete sessions (ended_at=None) are returned as active snapshot, not in sessions list."""
    stored_data = [
        make_session("completed_1"),
        {"id": "incomplete", "energy_kwh": 1.0, "user_name": "Test"},  # no ended_at
    ]
    store = SessionStore(hass)
    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=stored_data):
        sessions, snapshot = await store.async_load()
    assert len(sessions) == 1
    assert sessions[0]["id"] == "completed_1"
    assert store.sessions == sessions
    # Active snapshot returned separately (not in sessions list)
    assert snapshot is not None
    assert snapshot["id"] == "incomplete"


async def test_load_returns_most_recent_snapshot(hass: HomeAssistant):
    """When multiple incomplete entries exist, the last one is returned as snapshot."""
    stored_data = [
        {"id": "snap_old", "energy_kwh": 1.0},  # no ended_at, older
        {"id": "snap_new", "energy_kwh": 2.0},  # no ended_at, newer
    ]
    store = SessionStore(hass)
    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=stored_data):
        sessions, snapshot = await store.async_load()
    assert sessions == []
    assert snapshot is not None
    assert snapshot["id"] == "snap_new"


async def test_load_no_snapshot_when_all_complete(hass: HomeAssistant):
    """When all stored sessions are complete, snapshot is None."""
    stored_data = [make_session("s1"), make_session("s2")]
    store = SessionStore(hass)
    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=stored_data):
        sessions, snapshot = await store.async_load()
    assert len(sessions) == 2
    assert snapshot is None


async def test_save_active_session(hass: HomeAssistant):
    """async_save_active_session writes session without adding to completed list."""
    store = SessionStore(hass)
    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=None):
        with patch.object(store._store, "async_save", new_callable=AsyncMock) as mock_save:
            await store.async_load()
            active = make_session("active_session")
            await store.async_save_active_session(active)

    mock_save.assert_called_once()
    # Completed sessions list should still be empty
    assert store.sessions == []
    # But the save should include the active session
    saved_data = mock_save.call_args[0][0]
    assert len(saved_data) == 1
    assert saved_data[0]["id"] == "active_session"
