"""EV Charging Manager integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr

from .charger_profiles import CHARGER_PROFILES
from .config_store import ConfigStore
from .const import (
    CONF_CHARGER_NAME,
    CONF_CHARGER_PROFILE,
    CONF_DEBUG_LOGGING,
    CONF_MAX_STORED_SESSIONS,
    CONF_PERSISTENCE_INTERVAL_S,
    DEFAULT_CHARGER_NAME,
    DEFAULT_MAX_STORED_SESSIONS,
    DEFAULT_PERSISTENCE_INTERVAL_S,
    DOMAIN,
    PLATFORMS,
)
from .debug_logger import DebugLogger, async_cleanup_legacy_file
from .lifecycle import async_migrate_observation_slots
from .session_engine import SessionEngine
from .session_engine_v2 import PlugAnchoredSessionEngine
from .session_store import SessionStore
from .stats_engine import StatsEngine
from .stats_store import StatsStore

_LOGGER = logging.getLogger(__name__)


def _snapshot_subentries(entry: ConfigEntry) -> dict[str, dict]:
    """Take a snapshot of current subentries for change detection."""
    return {
        sid: {"subentry_type": s.subentry_type, "data": dict(s.data)}
        for sid, s in entry.subentries.items()
    }


async def _on_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry updates (subentry add/edit/delete)."""
    if entry.state is not ConfigEntryState.LOADED:
        return

    domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    config_store: ConfigStore | None = domain_data.get("config_store")
    if not config_store:
        return

    prev = domain_data.get("prev_subentries", {})
    current_ids = set(entry.subentries.keys())
    prev_ids = set(prev.keys())

    # Detect deleted subentries and run cascade
    removed_ids = prev_ids - current_ids
    if removed_ids:
        from .lifecycle import async_cascade_delete_user, async_cascade_delete_vehicle

        for removed_id in removed_ids:
            removed_info = prev[removed_id]
            if removed_info["subentry_type"] == "user":
                _LOGGER.debug("User subentry %s removed, cascading delete", removed_id)
                await async_cascade_delete_user(hass, entry, removed_id)
            elif removed_info["subentry_type"] == "vehicle":
                _LOGGER.debug("Vehicle subentry %s removed, cascading delete", removed_id)
                await async_cascade_delete_vehicle(hass, entry, removed_id)
            else:
                _LOGGER.debug("RFID mapping subentry %s removed (no cascade)", removed_id)

    # Sync ConfigStore from subentries (source of truth)
    await config_store.async_sync_from_subentries(entry)

    # Update snapshot for next comparison
    domain_data["prev_subentries"] = _snapshot_subentries(entry)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EV Charging Manager from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}

    # Initialize ConfigStore and sync from subentries
    config_store = ConfigStore(hass)
    await config_store.async_load()
    await config_store.async_sync_from_subentries(entry)

    hass.data[DOMAIN][entry.entry_id]["config_store"] = config_store
    hass.data[DOMAIN][entry.entry_id]["prev_subentries"] = _snapshot_subentries(entry)

    # Register update listener for subentry changes
    entry.async_on_unload(entry.add_update_listener(_on_entry_updated))

    profile_key = entry.data.get(CONF_CHARGER_PROFILE, "")
    profile = CHARGER_PROFILES.get(profile_key, {})
    model = profile.get("name", "Manual")

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.data.get(CONF_CHARGER_NAME, DEFAULT_CHARGER_NAME),
        manufacturer="EV Charging Manager",
        model=model,
    )

    # Populate missing observation-slot options from charger profile (one-time migration).
    # Runs before SessionEngine so it can read the resolved entity IDs from entry.options.
    await async_migrate_observation_slots(hass, entry)

    # PR-28 (FR-002): delete the legacy web-served www/ log file BEFORE logger
    # creation, unconditionally — the unauthenticated /local/ exposure must end
    # even when debug logging is disabled. Never fails setup.
    await async_cleanup_legacy_file(hass, hass.config.config_dir)

    # Set up debug logger — instantiated before session engine
    debug_logging_enabled = entry.options.get(CONF_DEBUG_LOGGING, False)
    debug_logger = DebugLogger(hass, hass.config.config_dir)
    if debug_logging_enabled:
        await debug_logger.async_enable()
    hass.data[DOMAIN][entry.entry_id]["debug_logger"] = debug_logger
    # Disable on unload: flushes the buffer with DEBUG_OFF as the final line
    # (coroutine callbacks are awaited by entry.async_on_unload).
    entry.async_on_unload(debug_logger.async_disable)

    # Review F1: HA does NOT unload config entries at an orderly stop —
    # async_on_unload fires only on reload/remove, so without this listener
    # up to 5 s / 500 buffered lines and the DEBUG_OFF marker would silently
    # vanish at every clean HA restart. async_disable() is idempotent, so the
    # unload path and this listener can both call it. Registered via
    # entry.async_on_unload so a reload does not leak one listener per setup.
    async def _flush_debug_log_on_stop(_event: Event) -> None:
        await debug_logger.async_disable()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _flush_debug_log_on_stop)
    )

    # Set up stats store and engine BEFORE the session engine (PR-26 Fix 5,
    # FR-011): session recovery below can fire EVENT_SESSION_COMPLETED, and the
    # event bus has no replay — the StatsEngine listener must already exist or
    # recovery-finalized sessions never reach user statistics.
    stats_store = StatsStore(hass)
    stats_engine = StatsEngine(hass, entry, stats_store)
    await stats_engine.async_setup()
    hass.data[DOMAIN][entry.entry_id]["stats_engine"] = stats_engine

    # Set up session store and load persisted sessions
    max_sessions = entry.options.get(CONF_MAX_STORED_SESSIONS, DEFAULT_MAX_STORED_SESSIONS)
    session_store = SessionStore(hass, max_sessions=max_sessions)
    _sessions, active_snapshot = await session_store.async_load()

    # Select the session engine based on the charger profile (T013 / FR-036, FR-037).
    # goe_gemini → PlugAnchoredSessionEngine (PR-22 plug-anchored model).
    # All other profiles → legacy SessionEngine (unchanged behaviour).
    # Both engines expose the same coordinator-callable interface so all downstream
    # platforms (sensor.py, binary_sensor.py, button.py, stats_sensor.py) remain profile-blind.
    uses_plug_anchored = profile.get("supports_plug_anchored_model", False)
    _dl = debug_logger if debug_logging_enabled else None
    if uses_plug_anchored:
        session_engine: SessionEngine | PlugAnchoredSessionEngine = PlugAnchoredSessionEngine(
            hass, entry, config_store, session_store, _dl
        )
    else:
        session_engine = SessionEngine(hass, entry, config_store, session_store, _dl)
    if active_snapshot is not None:
        await session_engine.async_recover(active_snapshot)
    session_engine.async_setup()

    # Schedule periodic persistence of active session
    persistence_interval = entry.options.get(
        CONF_PERSISTENCE_INTERVAL_S, DEFAULT_PERSISTENCE_INTERVAL_S
    )
    session_store.schedule_periodic_save(
        hass,
        entry,
        persistence_interval,
        session_engine.get_active_session_dict,
    )

    hass.data[DOMAIN][entry.entry_id]["session_store"] = session_store
    hass.data[DOMAIN][entry.entry_id]["session_engine"] = session_engine

    # Forward setup to sensor and binary_sensor platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # BUG-6: tear down engine-managed listeners (per-session unsubs) that we
        # cannot register via entry.async_on_unload because they were created
        # mid-session, not at setup. PlugAnchoredSessionEngine exposes async_unload;
        # the legacy SessionEngine does not, so we duck-type the call.
        domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        session_engine = domain_data.get("session_engine")
        if session_engine is not None and hasattr(session_engine, "async_unload"):
            try:
                await session_engine.async_unload()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("session_engine.async_unload failed: %s", err)
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
