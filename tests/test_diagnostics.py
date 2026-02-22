"""Tests for unknown session diagnostic reason codes (US3, FR-010, FR-011)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.const import (
    DOMAIN,
    UNKNOWN_REASON_RFID_INACTIVE,
    UNKNOWN_REASON_RFID_TYPE_ERROR,
    UNKNOWN_REASON_RFID_UNMAPPED,
    UNKNOWN_REASON_TRX_NULL,
    UNKNOWN_REASON_TRX_ZERO,
    SessionEngineState,
)
from tests.conftest import (
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    setup_session_engine,
    start_charging_session,
    stop_charging_session,
)

NO_FILTER_OPTIONS = {"min_session_duration_s": 0, "min_session_energy_wh": 0}


async def _add_user(hass, entry_id, name="Petra") -> str:
    """Add a user subentry and return its subentry_id."""
    from tests.test_session_engine import _add_user as _au

    return await _au(hass, entry_id, name=name)


async def _add_rfid(hass, entry_id, card_index, user_id, active=True) -> None:
    """Add an RFID mapping subentry."""
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "rfid_mapping"),
        context={"source": "user"},
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"card_index": str(card_index), "user_id": user_id},
    )
    await hass.async_block_till_done()
    if not active:
        # Reconfigure to set inactive
        entry = hass.config_entries.async_get_entry(entry_id)
        for sub in entry.subentries.values():
            if sub.subentry_type == "rfid_mapping" and sub.data.get("card_index") == card_index:
                result = await hass.config_entries.subentries.async_init(
                    (entry_id, "rfid_mapping"),
                    context={"source": "reconfigure", "subentry_id": sub.subentry_id},
                )
                await hass.config_entries.subentries.async_configure(
                    result["flow_id"],
                    {"user_id": user_id, "active": False},
                )
                await hass.async_block_till_done()
                break


# ---------------------------------------------------------------------------
# FR-011: trx_was_null — charger charging with no RFID signal
# ---------------------------------------------------------------------------


async def test_reason_trx_was_null(hass: HomeAssistant) -> None:
    """FR-010/011: trx_was_null when _async_start_session runs with trx=null (race condition).

    In normal flow, _handle_idle_state gates on _is_trx_active(). But between the gate
    check and _async_start_session executing (scheduled as a task), the trx entity can
    change to null. This defensive path sets reason=trx_was_null via resolve(None)=None.
    """
    from tests.conftest import MOCK_CAR_STATUS_ENTITY, MOCK_TRX_ENTITY

    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=NO_FILTER_OPTIONS, title="go-e"
    )
    await setup_session_engine(hass, entry)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.IDLE

    # Set car to Charging but trx to "null" — simulates race where trx changed
    # between _handle_idle_state check and _async_start_session execution
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    await hass.async_block_till_done()

    # Directly invoke _async_start_session (bypassing the _is_trx_active gate)
    await engine._async_start_session()

    # resolve(None) returns None → reason = trx_was_null
    assert engine.active_session is not None
    assert engine.active_session.user_name == "Unknown"
    assert engine.active_session.user_type == "unknown"
    assert engine._last_unknown_reason == UNKNOWN_REASON_TRX_NULL
    assert engine._last_unknown_at is not None


async def test_reason_trx_was_zero(hass: HomeAssistant) -> None:
    """FR-011: trx=0 (open access mode) → reason=trx_was_zero."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=NO_FILTER_OPTIONS, title="go-e"
    )
    await setup_session_engine(hass, entry)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # trx=0 means open access — session starts with unknown user
    await start_charging_session(hass, trx_value="0")

    assert engine.state == SessionEngineState.TRACKING
    assert engine.active_session is not None
    assert engine.active_session.user_type == "unknown"

    # Complete session to trigger reason code
    await stop_charging_session(hass)
    await hass.async_block_till_done()

    assert engine._last_unknown_reason == UNKNOWN_REASON_TRX_ZERO
    assert engine._last_unknown_at is not None


# ---------------------------------------------------------------------------
# FR-011: rfid_unmapped — unmapped card slot
# ---------------------------------------------------------------------------


async def test_reason_rfid_unmapped(hass: HomeAssistant) -> None:
    """FR-011: Unmapped card → reason=rfid_unmapped."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=NO_FILTER_OPTIONS, title="go-e"
    )
    await setup_session_engine(hass, entry)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # No RFID mapping exists for trx=2 (card index 1)
    await start_charging_session(hass, trx_value="2")
    assert engine.active_session is not None
    assert engine.active_session.user_type == "unknown"

    await stop_charging_session(hass)
    await hass.async_block_till_done()

    assert engine._last_unknown_reason == UNKNOWN_REASON_RFID_UNMAPPED


# ---------------------------------------------------------------------------
# FR-011: rfid_inactive — mapping exists but disabled
# ---------------------------------------------------------------------------


async def test_reason_rfid_inactive(hass: HomeAssistant) -> None:
    """FR-011: Inactive RFID mapping → reason=rfid_inactive."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=NO_FILTER_OPTIONS, title="go-e"
    )
    await setup_session_engine(hass, entry)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # Add an inactive mapping for card_index=1 (trx=2)
    user_id = await _add_user(hass, entry.entry_id, name="Petra")
    await _add_rfid(hass, entry.entry_id, card_index=1, user_id=user_id, active=False)

    await start_charging_session(hass, trx_value="2")
    assert engine.active_session is not None
    assert engine.active_session.user_type == "unknown"

    await stop_charging_session(hass)
    await hass.async_block_till_done()

    assert engine._last_unknown_reason == UNKNOWN_REASON_RFID_INACTIVE


# ---------------------------------------------------------------------------
# FR-011: rfid_type_error — non-numeric trx
# ---------------------------------------------------------------------------


async def test_reason_rfid_type_error(hass: HomeAssistant) -> None:
    """FR-011: Non-numeric trx → reason=rfid_type_error."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=NO_FILTER_OPTIONS, title="go-e"
    )
    await setup_session_engine(hass, entry)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # Non-numeric trx triggers type error
    await start_charging_session(hass, trx_value="abc_invalid")
    assert engine.active_session is not None
    assert engine.active_session.user_type == "unknown"

    await stop_charging_session(hass)
    await hass.async_block_till_done()

    assert engine._last_unknown_reason == UNKNOWN_REASON_RFID_TYPE_ERROR


# ---------------------------------------------------------------------------
# FR-010: Reason persists through normal (identified) sessions
# ---------------------------------------------------------------------------


async def test_reason_persists_through_normal_session(hass: HomeAssistant) -> None:
    """FR-010: last_unknown_reason is NOT cleared by a normal identified session."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=NO_FILTER_OPTIONS, title="go-e"
    )
    await setup_session_engine(hass, entry)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # First: unknown session (trx=0)
    await start_charging_session(hass, trx_value="0")
    await stop_charging_session(hass)
    await hass.async_block_till_done()
    assert engine._last_unknown_reason == UNKNOWN_REASON_TRX_ZERO
    saved_reason = engine._last_unknown_reason
    saved_at = engine._last_unknown_at

    # Now add a user and RFID mapping so next session is identified
    user_id = await _add_user(hass, entry.entry_id, name="Petra")
    await _add_rfid(hass, entry.entry_id, card_index=1, user_id=user_id)

    # Second session: identified (trx=2 → Petra)
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="2")
    assert engine.active_session is not None
    assert engine.active_session.user_name == "Petra", "Should be identified as Petra"
    await stop_charging_session(hass)
    await hass.async_block_till_done()

    # Reason must still be from the previous unknown session (not cleared)
    assert engine._last_unknown_reason == saved_reason, "Reason must persist through normal session"
    assert engine._last_unknown_at == saved_at, "Timestamp must persist through normal session"


# ---------------------------------------------------------------------------
# FR-010: Reason overwritten by next unknown session
# ---------------------------------------------------------------------------


async def test_reason_overwritten_by_next_unknown_session(hass: HomeAssistant) -> None:
    """FR-010: Next unknown session overwrites the last unknown reason."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=NO_FILTER_OPTIONS, title="go-e"
    )
    await setup_session_engine(hass, entry)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # First unknown: trx=0 → trx_was_zero
    await start_charging_session(hass, trx_value="0")
    await stop_charging_session(hass)
    await hass.async_block_till_done()
    assert engine._last_unknown_reason == UNKNOWN_REASON_TRX_ZERO

    # Second unknown: trx=2 (unmapped) → rfid_unmapped
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="2")
    await stop_charging_session(hass)
    await hass.async_block_till_done()

    # Reason should now reflect the latest unknown session
    assert engine._last_unknown_reason == UNKNOWN_REASON_RFID_UNMAPPED


# ---------------------------------------------------------------------------
# StatusSensor exposes last_unknown_reason and last_unknown_at (T015)
# ---------------------------------------------------------------------------


async def test_status_sensor_exposes_diagnostic_attributes(hass: HomeAssistant) -> None:
    """T015: StatusSensor extra_state_attributes include last_unknown_reason and last_unknown_at."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=NO_FILTER_OPTIONS, title="go-e"
    )
    await setup_session_engine(hass, entry)

    # Complete an unknown session to populate reason
    await start_charging_session(hass, trx_value="0")
    await stop_charging_session(hass)
    await hass.async_block_till_done()

    # Check StatusSensor attributes
    status_entity = hass.states.get("sensor.my_go_e_charger_status")
    assert status_entity is not None, "Status sensor must exist"
    attrs = status_entity.attributes

    assert "last_unknown_reason" in attrs, "StatusSensor must have last_unknown_reason attribute"
    assert "last_unknown_at" in attrs, "StatusSensor must have last_unknown_at attribute"
    assert attrs["last_unknown_reason"] == UNKNOWN_REASON_TRX_ZERO


async def test_status_sensor_reason_none_before_first_unknown(hass: HomeAssistant) -> None:
    """StatusSensor shows None for last_unknown_reason before any unknown session."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="go-e")
    await setup_session_engine(hass, entry)

    status_entity = hass.states.get("sensor.my_go_e_charger_status")
    assert status_entity is not None
    attrs = status_entity.attributes

    assert attrs.get("last_unknown_reason") is None
    assert attrs.get("last_unknown_at") is None
