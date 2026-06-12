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
    # But the save should include the active session in the data envelope
    saved_envelope = mock_save.call_args[0][0]
    saved_list = saved_envelope["data"] if isinstance(saved_envelope, dict) else saved_envelope
    assert len(saved_list) == 1
    assert saved_list[0]["id"] == "active_session"


# ---------------------------------------------------------------------------
# PR-27 (023-recovery-hardening): snapshot lifecycle — clear, FR-011 guard,
# FR-007 load-time cleanup (T003/T012)
# ---------------------------------------------------------------------------


def make_active_snapshot(session_id: str = "active", energy: float = 1.0) -> dict:
    """Create a minimal in-progress session snapshot (no ended_at/disconnected_at)."""
    return {
        "id": session_id,
        "energy_kwh": energy,
        "user_name": "Petra",
    }


def _envelope(sessions: list[dict]) -> dict:
    """Wrap a session list in a v1.2 store envelope (skips migration on load)."""
    return {
        "version": 1,
        "minor_version": 2,
        "key": "ev_charging_manager_sessions",
        "data": sessions,
    }


async def test_clear_active_session_with_empty_completed_list(session_store_with_save):
    """FR-006 edge: clearing the snapshot works on a fresh install (no sessions)."""
    store, mock_save = session_store_with_save
    await store.async_clear_active_session()

    mock_save.assert_called_once()
    saved_envelope = mock_save.call_args[0][0]
    assert saved_envelope["data"] == [], "cleared envelope must contain no sessions"
    assert store.sessions == []


async def test_clear_active_session_preserves_completed_sessions(session_store_with_save):
    """FR-006: clearing the snapshot rewrites the envelope from completed sessions only."""
    store, mock_save = session_store_with_save
    await store.add_session(make_session("s1"))
    await store.add_session(make_session("s2"))
    mock_save.reset_mock()

    await store.async_clear_active_session()

    mock_save.assert_called_once()
    saved_envelope = mock_save.call_args[0][0]
    saved_ids = [s["id"] for s in saved_envelope["data"]]
    assert saved_ids == ["s1", "s2"], "completed sessions must survive the clear"
    # No incomplete entries in the written envelope
    assert all(
        s.get("ended_at") is not None or s.get("disconnected_at") is not None
        for s in saved_envelope["data"]
    )


async def test_save_active_session_skips_already_completed_id(session_store_with_save):
    """FR-011: a snapshot whose session_id is already completed is NOT written."""
    store, mock_save = session_store_with_save
    await store.add_session(make_session("s1"))
    mock_save.reset_mock()

    # Periodic writer captured the active dict before completion landed —
    # the late write must be skipped, not resurrect "s1" as incomplete.
    await store.async_save_active_session(make_active_snapshot("s1"))

    mock_save.assert_not_called()


async def test_save_active_session_writes_unknown_id(session_store_with_save):
    """FR-011 control: a snapshot for a NOT-yet-completed id is written normally."""
    store, mock_save = session_store_with_save
    await store.add_session(make_session("s1"))
    mock_save.reset_mock()

    await store.async_save_active_session(make_active_snapshot("s2"))

    mock_save.assert_called_once()
    saved_envelope = mock_save.call_args[0][0]
    assert [s["id"] for s in saved_envelope["data"]] == ["s1", "s2"]


async def test_load_persists_cleaned_envelope_after_extraction(hass: HomeAssistant):
    """FR-007: load extracts the snapshot AND persists the cleaned envelope."""
    stored = _envelope([make_session("done"), make_active_snapshot("snap")])
    store = SessionStore(hass)
    with (
        patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=stored),
        patch.object(store._store, "async_save", new_callable=AsyncMock) as mock_save,
    ):
        sessions, snapshot = await store.async_load()

    assert snapshot is not None and snapshot["id"] == "snap"
    assert [s["id"] for s in sessions] == ["done"]
    # The cleaned envelope (snapshot removed) must have been written back to disk.
    mock_save.assert_called_once()
    cleaned = mock_save.call_args[0][0]
    assert [s["id"] for s in cleaned["data"]] == ["done"], (
        "load-time cleanup must persist the envelope WITHOUT the extracted snapshot"
    )


async def test_double_load_consumes_snapshot_once(hass: HomeAssistant):
    """FR-007: two consecutive restarts cannot consume the same snapshot."""
    stored = _envelope([make_active_snapshot("snap")])
    store1 = SessionStore(hass)
    with (
        patch.object(store1._store, "async_load", new_callable=AsyncMock, return_value=stored),
        patch.object(store1._store, "async_save", new_callable=AsyncMock) as mock_save,
    ):
        _sessions, snapshot1 = await store1.async_load()
    assert snapshot1 is not None and snapshot1["id"] == "snap"
    mock_save.assert_called_once()
    cleaned = mock_save.call_args[0][0]

    # Second restart loads what the first one wrote — no snapshot may remain.
    store2 = SessionStore(hass)
    with (
        patch.object(store2._store, "async_load", new_callable=AsyncMock, return_value=cleaned),
        patch.object(store2._store, "async_save", new_callable=AsyncMock),
    ):
        sessions2, snapshot2 = await store2.async_load()
    assert snapshot2 is None, "second restart must not re-consume the snapshot"
    assert sessions2 == []


async def test_periodic_save_does_not_resurrect_completed_session(hass: HomeAssistant):
    """FR-011 end-to-end (T012): the periodic writer captures the active dict,
    completion lands, THEN the periodic write fires → no incomplete snapshot of
    the completed id reaches disk."""
    store = SessionStore(hass, max_sessions=1000)
    mock_save = AsyncMock()

    mock_entry = MagicMock()
    mock_entry.async_on_unload = lambda cb: cb

    # The "engine": returns the active session dict until completion clears it.
    active_holder: dict = {"session": make_active_snapshot("s1")}

    def get_active() -> dict | None:
        return active_holder["session"]

    captured_interval_cb: dict = {}

    with patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=None):
        with patch.object(store._store, "async_save", mock_save):
            await store.async_load()
            with patch(
                "custom_components.ev_charging_manager.session_store.async_track_time_interval"
            ) as mock_track:
                mock_track.return_value = MagicMock()
                store.schedule_periodic_save(hass, mock_entry, 300, get_active)
                captured_interval_cb["save"] = mock_track.call_args[0][1]

            # The periodic _save captured `get_active` — but before the interval
            # fires, the session COMPLETES (race window): it lands in the store…
            completed = make_session("s1")
            await store.add_session(completed)
            mock_save.reset_mock()
            # …while the engine-side dict is still momentarily the stale active one
            # (modelled by get_active still returning it for this tick).

            # The periodic write fires with the stale capture.
            await captured_interval_cb["save"](None)

    # FR-011: the guard must skip the write — nothing on disk may contain an
    # incomplete copy of the already-completed session.
    for call in mock_save.call_args_list:
        envelope = call.args[0]
        for entry in envelope["data"]:
            if entry["id"] == "s1":
                assert (
                    entry.get("ended_at") is not None or entry.get("disconnected_at") is not None
                ), "an incomplete snapshot of the completed session reached disk"
    mock_save.assert_not_called()


async def test_load_without_snapshot_does_not_rewrite(hass: HomeAssistant):
    """FR-007 control: a clean store (no snapshot) triggers no cleanup write."""
    stored = _envelope([make_session("done")])
    store = SessionStore(hass)
    with (
        patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=stored),
        patch.object(store._store, "async_save", new_callable=AsyncMock) as mock_save,
    ):
        _sessions, snapshot = await store.async_load()
    assert snapshot is None
    mock_save.assert_not_called()
