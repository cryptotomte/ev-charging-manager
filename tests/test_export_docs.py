"""Validation tests for export documentation (FR-009, FR-010).

T006: Verify the InfluxDB automation template is valid YAML with the correct
event trigger, and that all fields referenced in the template exist in the
actual session_completed event payload.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.ev_charging_manager.const import (
    DOMAIN,
    EVENT_SESSION_COMPLETED,
)
from tests.conftest import (
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    setup_session_engine,
    start_charging_session,
    stop_charging_session,
)

# Path to the automation template relative to repo root
AUTOMATION_FILE = (
    Path(__file__).parent.parent / "automations" / "ev_charging_manager_influxdb_export.yaml"
)

# Micro-session thresholds disabled so our test session completes
_NO_MICRO = {"min_session_duration_s": 0, "min_session_energy_wh": 0}


def test_influxdb_automation_yaml_valid():
    """FR-010: Automation template is valid YAML with correct event trigger."""
    assert AUTOMATION_FILE.exists(), f"Automation file not found: {AUTOMATION_FILE}"

    content = yaml.safe_load(AUTOMATION_FILE.read_text())
    assert content is not None, "YAML file is empty"

    # The file wraps the automation in an 'automation' key
    assert "automation" in content, "Missing top-level 'automation' key"
    automations = content["automation"]
    assert isinstance(automations, list), "'automation' should be a list"
    assert len(automations) >= 1, "No automations defined"

    automation = automations[0]

    # Verify trigger is event-based with correct event_type
    assert "trigger" in automation, "Missing 'trigger' key"
    triggers = automation["trigger"]
    assert isinstance(triggers, list), "'trigger' should be a list"

    event_trigger = triggers[0]
    assert event_trigger["platform"] == "event"
    assert event_trigger["event_type"] == "ev_charging_manager_session_completed"

    # Verify action uses influxdb.write
    assert "action" in automation, "Missing 'action' key"
    actions = automation["action"]
    assert isinstance(actions, list), "'action' should be a list"
    assert actions[0]["service"] == "influxdb.write"


async def test_session_completed_event_has_all_influxdb_fields(hass: HomeAssistant):
    """FR-009: All fields referenced in the automation template exist in the event payload."""
    # Parse the automation template to extract referenced field names
    content = yaml.safe_load(AUTOMATION_FILE.read_text())
    automation = content["automation"][0]
    influx_data = automation["action"][0]["data"]

    # Collect all event field names referenced in the template
    # Tags and fields reference trigger.event.data.<field_name>
    referenced_fields: set[str] = set()
    for tag_template in influx_data["tags"].values():
        # Extract field name from "{{ trigger.event.data.FIELD ... }}"
        field = _extract_event_field(tag_template)
        if field:
            referenced_fields.add(field)

    for field_template in influx_data["fields"].values():
        field = _extract_event_field(field_template)
        if field:
            referenced_fields.add(field)

    # Timestamp also references an event field
    ts_field = _extract_event_field(influx_data["timestamp"])
    if ts_field:
        referenced_fields.add(ts_field)

    assert len(referenced_fields) > 0, "No event fields extracted from template"

    # Run a real session through the engine to capture the event payload
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, options=_NO_MICRO, title="Test")
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)
    await setup_session_engine(hass, entry)

    # Start session with energy
    hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
    await start_charging_session(hass, trx_value="0")

    # Add some energy
    hass.states.async_set(MOCK_ENERGY_ENTITY, "11.5")
    await hass.async_block_till_done()

    # Complete session
    await stop_charging_session(hass)

    assert len(completed_events) == 1, "Expected exactly one session_completed event"
    event_data = completed_events[0].data

    # Verify every field referenced in the automation template exists in the event
    missing_fields = referenced_fields - set(event_data.keys())
    assert not missing_fields, (
        f"Fields referenced in automation template but missing from event payload: {missing_fields}"
    )

    # Also verify charger_name specifically (FR-013)
    assert "charger_name" in event_data
    assert event_data["charger_name"] == "My go-e Charger"

    # G1: rfid_index must always exist in completed event (even when None)
    assert "rfid_index" in event_data


def _extract_event_field(template_str: str) -> str | None:
    """Extract the event data field name from a Jinja2 template string.

    Parses patterns like '{{ trigger.event.data.field_name | ... }}'.
    """
    prefix = "trigger.event.data."
    idx = template_str.find(prefix)
    if idx == -1:
        return None
    start = idx + len(prefix)
    # Field name ends at space, pipe, or closing brace
    end = start
    while end < len(template_str) and template_str[end] not in (" ", "|", "}"):
        end += 1
    return template_str[start:end].strip()
