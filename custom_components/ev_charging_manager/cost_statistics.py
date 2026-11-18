"""The long-term statistics of the total-cost sensors carry the entry's unit (spec 028 D-74)."""

from __future__ import annotations

import asyncio
import logging
from functools import partial

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import DATA_INSTANCE

from .const import entry_currency

_LOGGER = logging.getLogger(__name__)


async def async_reconcile_cost_statistics(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Relabel each total-cost statistic of the entry whose unit is not the entry's unit,
    when the sensor's state shows the entry's unit.

    Relabels, converts nothing. Never raises (F-7): a failure leaves Home Assistant's own
    "unit has changed" repair issue for the user.
    """
    if DATA_INSTANCE not in hass.data:
        return
    unit = entry_currency(entry)
    try:
        # Imported here because the integration must work without the recorder (D-68).
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            async_update_statistics_metadata,
            get_metadata,
        )

        instance = get_instance(hass)
        # Shielded: cancelling this task at unload must not cancel the recorder's own future.
        if not await asyncio.shield(instance.async_db_ready):
            return
        statistic_ids = {
            reg.entity_id
            for reg in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
            if reg.original_device_class == SensorDeviceClass.MONETARY
            and (reg.capabilities or {}).get("state_class") == SensorStateClass.TOTAL
        }
        if not statistic_ids:
            return
        metadata = await instance.async_add_executor_job(
            partial(get_metadata, hass, statistic_ids=statistic_ids)
        )
        for statistic_id, (_, meta) in sorted(metadata.items()):
            if meta["unit_of_measurement"] == unit:
                continue
            # A unit the sensor does not show is one the user set (customize); relabelling
            # against it would stop the statistic at every start.
            state = hass.states.get(statistic_id)
            if state is None or state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) != unit:
                continue
            _LOGGER.info(
                "Changing the unit of the long-term statistics of %s from %s to %s",
                statistic_id,
                meta["unit_of_measurement"],
                unit,
            )
            async_update_statistics_metadata(
                hass, statistic_id, new_unit_class=None, new_unit_of_measurement=unit
            )
    except Exception as err:
        _LOGGER.warning(
            "Could not put the long-term statistics of the cost sensors in %s (%s). "
            "Fix them in Developer tools → Statistics",
            unit,
            err,
        )
