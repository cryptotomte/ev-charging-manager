"""Tests for energy cross-validation against charger ETO counter (US4, FR-016/017/018)."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.const import (
    CONF_ETO_ENTITY,
    DOMAIN,
    SessionEngineState,
)
from tests.conftest import (
    MOCK_CAR_STATUS_ENTITY,
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    setup_session_engine,
    start_charging_session,
    stop_charging_session,
)

NO_FILTER_OPTIONS = {"min_session_duration_s": 0, "min_session_energy_wh": 0}

MOCK_ETO_ENTITY = "sensor.charger_total_energy"


def _eto_options() -> dict:
    """Return options dict with ETO entity configured and no micro-session filter."""
    return {
        "min_session_duration_s": 0,
        "min_session_energy_wh": 0,
        CONF_ETO_ENTITY: MOCK_ETO_ENTITY,
    }


# ---------------------------------------------------------------------------
# FR-016: No ETO configured — cross-validation silently skipped
# ---------------------------------------------------------------------------


async def test_no_eto_configured_skips_silently(hass: HomeAssistant) -> None:
    """FR-016: When eto_entity is not configured, no cross-validation is attempted."""
    # Use default MOCK_CHARGER_DATA without ETO entity
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=NO_FILTER_OPTIONS, title="go-e"
    )
    await setup_session_engine(hass, entry)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    await start_charging_session(hass, trx_value="2")
    assert engine.state == SessionEngineState.TRACKING

    hass.states.async_set(MOCK_ENERGY_ENTITY, "2.0")
    await stop_charging_session(hass)
    await hass.async_block_till_done()

    # Session should complete without error — eto fields remain None
    assert engine.state == SessionEngineState.IDLE


# ---------------------------------------------------------------------------
# FR-017: ETO unavailable at session start — validation skipped
# ---------------------------------------------------------------------------


async def test_eto_unavailable_at_start_skips_gracefully(hass: HomeAssistant) -> None:
    """FR-017: ETO sensor unavailable at session start — validation is skipped gracefully."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=_eto_options(), title="go-e"
    )
    await setup_session_engine(hass, entry)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # ETO entity not set (unavailable) at session start
    await start_charging_session(hass, trx_value="2")
    assert engine.state == SessionEngineState.TRACKING
    assert engine._eto_start is None, "ETO start must be None when sensor unavailable"

    hass.states.async_set(MOCK_ENERGY_ENTITY, "1.5")
    hass.states.async_set(MOCK_ETO_ENTITY, "500.0")  # ETO available at end
    await stop_charging_session(hass)
    await hass.async_block_till_done()

    # Session completes — cross-validation skipped (no eto_start to compare)
    assert engine.state == SessionEngineState.IDLE


# ---------------------------------------------------------------------------
# FR-017: ETO unavailable at session end — validation skipped
# ---------------------------------------------------------------------------


async def test_eto_unavailable_at_end_skips_gracefully(hass: HomeAssistant) -> None:
    """FR-017: ETO sensor unavailable at session end — validation is skipped gracefully."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=_eto_options(), title="go-e"
    )
    await setup_session_engine(hass, entry)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # ETO entity available at session start
    hass.states.async_set(MOCK_ETO_ENTITY, "100.0")
    await start_charging_session(hass, trx_value="2")
    assert engine._eto_start == 100.0, "ETO start must be captured when sensor available"

    # ETO entity becomes unavailable before session end
    from homeassistant.const import STATE_UNAVAILABLE

    hass.states.async_set(MOCK_ETO_ENTITY, STATE_UNAVAILABLE)
    hass.states.async_set(MOCK_ENERGY_ENTITY, "2.0")
    await stop_charging_session(hass)
    await hass.async_block_till_done()

    # Session completes — cross-validation skipped (eto_end unavailable)
    assert engine.state == SessionEngineState.IDLE


# ---------------------------------------------------------------------------
# FR-018: Normal match — no warning logged
# ---------------------------------------------------------------------------


async def test_energy_match_within_5pct_no_warning(hass: HomeAssistant, caplog) -> None:
    """FR-018: When tracked energy matches ETO diff within 5%, no warning is logged."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=_eto_options(), title="go-e"
    )
    await setup_session_engine(hass, entry)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # ETO = 100.0 kWh at session start
    hass.states.async_set(MOCK_ETO_ENTITY, "100.0")
    await start_charging_session(hass, trx_value="2")

    # Session tracked 3.0 kWh (energy sensor goes from 0 to 3.0)
    hass.states.async_set(MOCK_ENERGY_ENTITY, "3.0")
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    # ETO = 103.0 at session end → diff = 3.0 kWh — perfect match
    hass.states.async_set(MOCK_ETO_ENTITY, "103.0")
    with caplog.at_level(logging.WARNING, logger="custom_components.ev_charging_manager"):
        await stop_charging_session(hass)
        await hass.async_block_till_done()

    # No cross-validation WARNING should be logged
    cross_val_warnings = [r for r in caplog.records if "cross-validation" in r.message.lower()]
    assert not cross_val_warnings, (
        "No cross-validation warning should be logged for matching energy"
    )
    assert engine._eto_start is None, "eto_start must be reset after session"


# ---------------------------------------------------------------------------
# FR-018: >5% deviation — WARNING logged
# ---------------------------------------------------------------------------


async def test_energy_deviation_over_5pct_logs_warning(hass: HomeAssistant, caplog) -> None:
    """FR-018: When tracked energy deviates >5% from ETO diff, a WARNING is logged."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=_eto_options(), title="go-e"
    )
    await setup_session_engine(hass, entry)

    # ETO = 200.0 kWh at session start
    hass.states.async_set(MOCK_ETO_ENTITY, "200.0")
    await start_charging_session(hass, trx_value="2")

    # Session tracked 5.0 kWh
    hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    # ETO = 210.0 at session end → diff = 10.0 kWh
    # Tracked = 5.0 kWh → deviation = (10.0 - 5.0) / 10.0 = 50% >> 5%
    hass.states.async_set(MOCK_ETO_ENTITY, "210.0")
    with caplog.at_level(logging.WARNING, logger="custom_components.ev_charging_manager"):
        await stop_charging_session(hass)
        await hass.async_block_till_done()

    cross_val_warnings = [r for r in caplog.records if "cross-validation" in r.message.lower()]
    assert cross_val_warnings, "Cross-validation WARNING must be logged when deviation >5%"
    warning_text = cross_val_warnings[0].message
    assert "5.0" in warning_text or "10.0" in warning_text, "Warning must include energy values"
