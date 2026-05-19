"""Switch platform for EV Charging Manager (PR-22).

Phase 6 (US4, T055-T059) implements the unknown-RFID block switch.
This stub registers the platform so the integration can load; the full
SwitchEntity implementation is added in that phase.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EV Charging Manager switch entities from a config entry.

    Full implementation added in Phase 6 (T055). For now this is a no-op
    stub that satisfies the platform registration requirement.
    """
    # Phase 6 (T055): instantiate UnknownRfidBlockSwitch and add it here.
    pass
