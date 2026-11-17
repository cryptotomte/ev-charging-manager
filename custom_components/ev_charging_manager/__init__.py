"""EV Charging Manager integration."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import slugify

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
    SUBENTRY_TYPE_USER,
    SUBENTRY_TYPE_VEHICLE,
)
from .debug_logger import DebugLogger, async_cleanup_legacy_file
from .lifecycle import (
    async_cascade_delete_user,
    async_cascade_delete_vehicle,
    async_migrate_observation_slots,
)
from .sensor import (
    async_add_user_stats_sensors,
    async_remove_user_stats_sensors,
    async_rename_user_stats_sensors,
)
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
    """Handle config entry updates (subentry add/edit/delete) per spec 026 § 6.2 and D-25.

    Never reloads the entry: a reload during a session loses money (spec 026 D-2).
    """
    if entry.state is not ConfigEntryState.LOADED:
        return

    domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    config_store: ConfigStore | None = domain_data.get("config_store")
    if not config_store:
        return

    # D-3: diff and replace the snapshot before the first await. Each subentry
    # change made below re-enters this listener eagerly, and every invocation
    # must handle only its own change.
    prev = domain_data.get("prev_subentries", {})
    current = _snapshot_subentries(entry)
    domain_data["prev_subentries"] = current
    prev_names = {
        sid: info["data"]["name"]
        for sid, info in prev.items()
        if info["subentry_type"] == SUBENTRY_TYPE_USER
    }
    names = {
        sid: info["data"]["name"]
        for sid, info in current.items()
        if info["subentry_type"] == SUBENTRY_TYPE_USER
    }
    removed = {sid: info for sid, info in prev.items() if sid not in current}
    renamed = {
        sid: (prev_names[sid], name)
        for sid, name in names.items()
        if sid in prev_names and prev_names[sid] != name
    }
    added = {sid: name for sid, name in names.items() if sid not in prev_names}

    # D-25: sessions starting from here on see the new users and mappings, even
    # if the statistics work below fails or waits for an earlier run.
    await config_store.async_sync_from_subentries(entry)

    # D-25: each run's statistics and entity work runs whole, one run at a time.
    # The runs started by the RFID cascade queue here as separate tasks; the
    # holder never awaits them, so they cannot deadlock.
    async with domain_data["listener_lock"]:
        if (
            entry.state is not ConfigEntryState.LOADED
            or hass.data.get(DOMAIN, {}).get(entry.entry_id) is not domain_data
        ):
            return
        await _async_update_user_statistics(
            hass, entry, domain_data["stats_engine"], names, removed, renamed, added
        )


async def _async_update_user_statistics(
    hass: HomeAssistant,
    entry: ConfigEntry,
    stats_engine: StatsEngine,
    names: dict[str, str],
    removed: dict[str, dict],
    renamed: dict[str, tuple[str, str]],
    added: dict[str, str],
) -> None:
    """Steps 4-6 of spec 026 § 6.2. A failure for one user does not stop the others (D-25)."""

    def others(sid: str) -> tuple[set[str], set[str]]:
        """Exact names and slugs of the other configured users.

        The Unknown bucket counts as one, so its bucket and sensors are never
        removed or moved (spec 026 § 4.4). The slug guest counts as taken: the
        guest totals hold two of its unique_ids (D-27).
        """
        other_names = {"Unknown", *(n for s, n in names.items() if s != sid)}
        return other_names, {"guest", *(slugify(n) for n in other_names)}

    def heir(sid: str, slug: str) -> tuple[str | None, str] | None:
        """The holder of a shared slug once ``sid`` is gone; the guest totals have none (D-28)."""
        if slug == "guest":
            return None
        for other_sid, name in names.items():
            if other_sid != sid and slugify(name) == slug:
                return other_sid, name
        return (None, "Unknown") if slug == "unknown" else None

    for sid, info in removed.items():
        try:
            if info["subentry_type"] == SUBENTRY_TYPE_USER:
                _LOGGER.debug("User subentry %s removed, cascading delete", sid)
                await async_cascade_delete_user(hass, entry, sid)
                name = info["data"]["name"]
                other_names, other_slugs = others(sid)
                slug = slugify(name)
                await async_remove_user_stats_sensors(
                    hass,
                    entry,
                    sid,
                    name,
                    remove_registry=slug not in other_slugs,
                    heir=heir(sid, slug),
                )
                if name not in other_names:
                    await stats_engine.async_remove_user(name)
            elif info["subentry_type"] == SUBENTRY_TYPE_VEHICLE:
                _LOGGER.debug("Vehicle subentry %s removed, cascading delete", sid)
                await async_cascade_delete_vehicle(hass, entry, sid)
            else:
                _LOGGER.debug("RFID mapping subentry %s removed (no cascade)", sid)
        except Exception:
            _LOGGER.exception("Updating statistics for removed subentry %s failed", sid)

    for sid, (old_name, new_name) in renamed.items():
        try:
            other_names, other_slugs = others(sid)
            # D-9: a bucket already under the new name is a leftover, not this user's.
            if new_name not in other_names:
                await stats_engine.async_remove_user(new_name)
            # D-26: a name another user (or Unknown) holds keeps its bucket.
            if old_name not in other_names and new_name not in other_names:
                await stats_engine.async_rename_user(old_name, new_name)
            old_slug, new_slug = slugify(old_name), slugify(new_name)
            target_free = new_slug not in other_slugs
            await async_rename_user_stats_sensors(
                hass,
                entry,
                sid,
                old_name,
                new_name,
                move_registry=old_slug != new_slug and target_free and old_slug not in other_slugs,
                clear_target=old_slug != new_slug and target_free,
                add_new=target_free,
                heir=heir(sid, old_slug) if old_slug in other_slugs else None,
            )
        except Exception:
            _LOGGER.exception("Updating statistics for renamed user %s failed", sid)

    for sid, name in added.items():
        try:
            other_names, other_slugs = others(sid)
            if name not in other_names:  # D-9
                await stats_engine.async_remove_user(name)
            # Another user's sensors already hold this slug's unique_ids (legacy data).
            if slugify(name) not in other_slugs:
                async_add_user_stats_sensors(hass, entry, sid, name)
        except Exception:
            _LOGGER.exception("Updating statistics for added user %s failed", sid)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EV Charging Manager from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}

    # PR-28 (FR-002): delete the legacy web-served www/ log file as the FIRST
    # await of setup (review F6) — the unauthenticated /local/ exposure must
    # end even when a later setup step (store load, migration, ...) raises and
    # fails the entry. Never fails setup itself.
    await async_cleanup_legacy_file(hass, hass.config.config_dir)

    # Initialize ConfigStore and sync from subentries
    config_store = ConfigStore(hass)
    await config_store.async_load()
    await config_store.async_sync_from_subentries(entry)

    hass.data[DOMAIN][entry.entry_id]["config_store"] = config_store
    hass.data[DOMAIN][entry.entry_id]["prev_subentries"] = _snapshot_subentries(entry)
    hass.data[DOMAIN][entry.entry_id]["listener_lock"] = asyncio.Lock()

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

    # Set up debug logger — instantiated before session engine. The legacy
    # www/ file cleanup (FR-002) already ran as the first await of setup.
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
        # mid-session, not at setup. Both PlugAnchoredSessionEngine and the legacy
        # SessionEngine now expose async_unload (F6); the hasattr guard remains for
        # safety / forward-compat with any future engine that omits it.
        domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        session_engine = domain_data.get("session_engine")
        if session_engine is not None and hasattr(session_engine, "async_unload"):
            try:
                await session_engine.async_unload()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("session_engine.async_unload failed: %s", err)
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
