"""Tests for session store schema migration v1.1 → v1.2 (PR-22).

Covers FR-033: migration of legacy session records to the new canonical field names,
and idempotence guarantee (re-running migration is a no-op).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant

from custom_components.ev_charging_manager.session_store import (
    SessionStore,
    _migrate_sessions_v1_1_to_v1_2,
)

# ---------------------------------------------------------------------------
# Unit tests for the migration function itself
# ---------------------------------------------------------------------------


def load_fixture_data() -> dict:
    """Load the v1.1 fixture from tests/fixtures/storage_sessions_v1_1.json."""
    fixture_path = Path(__file__).parent / "fixtures" / "storage_sessions_v1_1.json"
    with open(fixture_path) as fh:
        return json.load(fh)


def test_migration_adds_required_fields() -> None:
    """All required v1.2 fields must be present after migration."""
    data = load_fixture_data()
    assert data["minor_version"] == 1  # fixture is v1.1

    result = _migrate_sessions_v1_1_to_v1_2(data)

    assert result["minor_version"] == 2

    required_new_fields = [
        "connected_at",
        "disconnected_at",
        "connection_duration_s",
        "charging_started_at",
        "charging_ended_at",
        "charging_duration_s",
        "charging_window_count",
        "blocked",
    ]

    for session in result["data"]:
        for field_name in required_new_fields:
            assert field_name in session, (
                f"Session {session.get('id', '?')} is missing field {field_name!r} "
                f"after migration"
            )


def test_migration_maps_timestamps_correctly() -> None:
    """connected_at must equal started_at; disconnected_at must equal ended_at."""
    data = load_fixture_data()
    result = _migrate_sessions_v1_1_to_v1_2(data)

    for session in result["data"]:
        original_started_at = session.get("started_at")
        original_ended_at = session.get("ended_at")

        if original_started_at:
            assert session["connected_at"] == original_started_at, (
                f"connected_at should equal started_at for session {session['id']}"
            )
        if original_ended_at:
            assert session["disconnected_at"] == original_ended_at, (
                f"disconnected_at should equal ended_at for session {session['id']}"
            )


def test_migration_computes_connection_duration() -> None:
    """connection_duration_s must be (disconnected_at - connected_at) in seconds."""
    from datetime import datetime

    data = load_fixture_data()
    result = _migrate_sessions_v1_1_to_v1_2(data)

    for session in result["data"]:
        if session.get("connected_at") and session.get("disconnected_at"):
            connected = datetime.fromisoformat(session["connected_at"])
            disconnected = datetime.fromisoformat(session["disconnected_at"])
            expected_duration = int((disconnected - connected).total_seconds())
            assert session["connection_duration_s"] == expected_duration, (
                f"connection_duration_s wrong for session {session['id']}"
            )


def test_migration_maps_duration_seconds_to_charging_duration() -> None:
    """charging_duration_s must equal the original duration_seconds."""
    data = load_fixture_data()

    # Capture original duration_seconds before migration
    original_durations = {
        s["id"]: s.get("duration_seconds", 0) for s in data["data"]
    }

    result = _migrate_sessions_v1_1_to_v1_2(data)

    for session in result["data"]:
        sid = session["id"]
        assert session["charging_duration_s"] == original_durations[sid], (
            f"charging_duration_s should equal original duration_seconds for {sid}"
        )


def test_migration_sets_nullable_fields_to_none() -> None:
    """charging_started_at and charging_ended_at must be None for migrated records."""
    data = load_fixture_data()
    result = _migrate_sessions_v1_1_to_v1_2(data)

    for session in result["data"]:
        assert session["charging_started_at"] is None, (
            f"charging_started_at should be None for migrated session {session['id']}"
        )
        assert session["charging_ended_at"] is None, (
            f"charging_ended_at should be None for migrated session {session['id']}"
        )


def test_migration_sets_count_and_flag_defaults() -> None:
    """charging_window_count must be 0 and blocked must be False for migrated records."""
    data = load_fixture_data()
    result = _migrate_sessions_v1_1_to_v1_2(data)

    for session in result["data"]:
        assert session["charging_window_count"] == 0, (
            f"charging_window_count should be 0 for migrated session {session['id']}"
        )
        assert session["blocked"] is False, (
            f"blocked should be False for migrated session {session['id']}"
        )


def test_migration_preserves_existing_fields() -> None:
    """All pre-existing fields (user_name, energy_kwh, etc.) must survive migration."""
    data = load_fixture_data()

    # Snapshot original field values
    originals = {
        s["id"]: {
            "user_name": s.get("user_name"),
            "user_type": s.get("user_type"),
            "energy_kwh": s.get("energy_kwh"),
            "avg_power_w": s.get("avg_power_w"),
            "cost_total_kr": s.get("cost_total_kr"),
            "data_gap": s.get("data_gap"),
            "reconstructed": s.get("reconstructed"),
        }
        for s in data["data"]
    }

    result = _migrate_sessions_v1_1_to_v1_2(data)

    for session in result["data"]:
        sid = session["id"]
        for field_name, original_value in originals[sid].items():
            assert session[field_name] == original_value, (
                f"Field {field_name!r} changed during migration for session {sid}: "
                f"was {original_value!r}, now {session[field_name]!r}"
            )


def test_migration_is_idempotent() -> None:
    """Running migration twice must produce identical results to running it once."""
    data = load_fixture_data()

    # Run once
    result_once = _migrate_sessions_v1_1_to_v1_2(data)
    assert result_once["minor_version"] == 2

    # Deep-copy via JSON round-trip
    import copy
    data_copy = copy.deepcopy(result_once)

    # Run again — must be a no-op
    result_twice = _migrate_sessions_v1_1_to_v1_2(data_copy)

    assert result_twice["minor_version"] == 2
    assert result_twice["data"] == result_once["data"]


# ---------------------------------------------------------------------------
# Integration test: run migration through SessionStore.async_load()
# ---------------------------------------------------------------------------


async def test_session_store_runs_migration_on_load(hass: HomeAssistant) -> None:
    """SessionStore.async_load() must migrate v1.1 → v1.2 and persist the result."""
    fixture_data = load_fixture_data()
    assert fixture_data["minor_version"] == 1

    store = SessionStore(hass)

    # Mock the underlying Store to return the v1.1 fixture data
    saved_data: list[dict] = []

    async def mock_async_save(data: dict) -> None:
        saved_data.append(data)

    with (
        patch.object(store._store, "async_load", new_callable=AsyncMock, return_value=fixture_data),
        patch.object(store._store, "async_save", side_effect=mock_async_save),
    ):
        sessions, active_snapshot = await store.async_load()

    # Migration should have been persisted
    assert len(saved_data) >= 1, "Migration result was not saved"
    migrated_saved = saved_data[-1]
    assert migrated_saved["minor_version"] == 2

    # All loaded sessions must have v1.2 fields
    for s in sessions:
        assert "connected_at" in s
        assert "charging_duration_s" in s
        assert "blocked" in s
