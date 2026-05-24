"""PlugAnchoredSessionEngine — plug-anchored multi-window session model (PR-22).

# Negative requirements verified per Phase 10 of tasks.md:
# - No GATE_PROMOTE, BALANCING_SKIP, _awaiting_reset, _gate_*, _evaluate_promotion (FR-N01)
# - No _last_car_status stale-cache mechanism (FR-N02)
# - No session-splitting on car_status oscillation or BMS pulses (FR-N03)
# - No time-based session-merge for fumble case (FR-N04)
# - No modelstatus-reason-based behavior (FR-N05)

This module coexists with the legacy session_engine.py. The legacy engine continues
to serve the "generic" charger profile unchanged. async_setup_entry() branches on
charger profile (T013 / FR-036).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.components import persistent_notification
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
    async_track_utc_time_change,
)
from homeassistant.util import dt as dt_util

from .charging_window import ChargingWindow, ChargingWindowTracker
from .const import (
    CONF_CABLE_LOCK_ENTITY,
    CONF_CHARGER_NAME,
    CONF_CHARGING_IDLE_TIMEOUT_MIN,
    CONF_DISCONNECT_GRACE_MIN,
    CONF_ENERGY_ENTITY,
    CONF_ETO_ENTITY,
    CONF_HEARTBEAT_LOG_INTERVAL_MIN,
    CONF_MIN_SESSION_DURATION_S,
    CONF_MIN_SESSION_ENERGY_WH,
    CONF_NOTIFY_UNMAPPED_RFID,
    CONF_PLUG_ENTITY,
    CONF_POWER_ENTITY,
    CONF_RFID_ENTITY,
    CONF_RFID_GRACE_SECONDS,
    CONF_RFID_UID_ENTITY,
    CONF_SPOT_ADDITIONAL_COST_KWH,
    CONF_SPOT_FALLBACK_PRICE_KWH,
    CONF_SPOT_PRICE_ENTITY,
    CONF_SPOT_VAT_MULTIPLIER,
    CONF_STATIC_PRICE_KWH,
    CONF_UI_DISPATCH_INTERVAL_S,
    DEBUG_CAT_CHARGER_BACK_ONLINE,
    DEBUG_CAT_CHARGER_OFFLINE,
    DEBUG_CAT_CHARGING_WINDOW_CLOSE,
    DEBUG_CAT_CHARGING_WINDOW_OPEN,
    DEBUG_CAT_DISCONNECT_DETECTED,
    DEBUG_CAT_DISCONNECT_RESOLVED,
    DEBUG_CAT_HA_RESTART_DETECTED,
    DEBUG_CAT_HEARTBEAT,
    DEBUG_CAT_RECOVERY_TIMEOUT,
    DEBUG_CAT_RFID_GRACE,
    DEBUG_CAT_RFID_UNMAPPED_NOTIFIED,
    DEBUG_CAT_RFID_UNMAPPED_NOTIFY_FAILED,
    DEBUG_CAT_SESSION_FORCE_ENDED_BY_GRACE_TIMEOUT,
    DEBUG_CAT_SESSION_FORCE_ENDED_BY_RESTART,
    DEBUG_CAT_SESSION_RESUMED,
    DEBUG_CAT_TRX_MIDSESSION,
    DEFAULT_CHARGING_IDLE_TIMEOUT_MIN,
    DEFAULT_DISCONNECT_GRACE_MIN,
    DEFAULT_HEARTBEAT_LOG_INTERVAL_MIN,
    DEFAULT_MIN_SESSION_DURATION_S,
    DEFAULT_MIN_SESSION_ENERGY_WH,
    DEFAULT_NOTIFY_UNMAPPED_RFID,
    DEFAULT_RFID_GRACE_SECONDS,
    DEFAULT_SPOT_ADDITIONAL_COST_KWH,
    DEFAULT_SPOT_FALLBACK_PRICE_KWH,
    DEFAULT_SPOT_VAT_MULTIPLIER,
    DEFAULT_STATIC_PRICE_KWH,
    DEFAULT_UI_DISPATCH_INTERVAL_S,
    DEFERRED_RECOVERY_TIMEOUT_MIN,
    EVENT_CHARGING_CHARGED,
    EVENT_SESSION_COMPLETED,
    EVENT_SESSION_STARTED,
    EVENT_UNKNOWN_RFID_DETECTED,
    NOTIFICATION_ID_UNKNOWN_RFID,
    SIGNAL_RFID_MAPPING_ADDED,
    SIGNAL_SESSION_UPDATE,
    UNKNOWN_REASON_RFID_INACTIVE,
    UNKNOWN_REASON_RFID_TYPE_ERROR,
    UNKNOWN_REASON_RFID_UNMAPPED,
    UNKNOWN_REASON_TRX_NULL,
    UNKNOWN_REASON_TRX_ZERO,
    SessionEngineState,
    SessionSubState,
)
from .debug_logger import DebugLogger
from .models import GuestPricing
from .pricing import PricingEngine, SpotConfig
from .rfid_lookup import RfidLookup
from .session import Session
from .session_store import SessionStore
from .soc import estimate_soc

_LOGGER = logging.getLogger(__name__)

# Type alias for cancellation/unsubscribe handles returned by HA helpers.
_Unsub = CALLBACK_TYPE | None

# States that indicate an entity has no valid value
_INVALID_STATES = {STATE_UNAVAILABLE, STATE_UNKNOWN, None, "null", ""}

# Map RfidResolution.reason → diagnostic reason constant
_RFID_REASON_MAP: dict[str, str] = {
    "no_rfid": UNKNOWN_REASON_TRX_ZERO,
    "unmapped": UNKNOWN_REASON_RFID_UNMAPPED,
    "rfid_inactive": UNKNOWN_REASON_RFID_INACTIVE,
    "type_error": UNKNOWN_REASON_RFID_TYPE_ERROR,
}


@dataclass(slots=True)
class _RfidGraceState:
    """In-memory state for an active RFID grace window.

    See data-model.md §E1. The grace state is either fully present
    (all three fields set) or ``None`` (no grace window in flight) —
    there is no partial state.
    """

    plug_on_at: datetime
    cancel: CALLBACK_TYPE  # async_call_later handle
    trx_listener_unsub: Callable[[], None] | None  # may be None if listener never attached


class PlugAnchoredSessionEngine:
    """Plug-anchored multi-window session engine for goe_gemini profile.

    Session lifecycle:
      IDLE → (plug=on) → TRACKING/WAITING → (power>0) → TRACKING/CHARGING
           → (idle timeout) → TRACKING/CHARGED → (plug=off + cable_lock=Unlocked)
           → COMPLETING → IDLE

    One session per physical cable insertion. Session splitting on car_status
    oscillation, BMS pulses, or any internal signal is explicitly forbidden
    (FR-N01 through FR-N05).

    Internal sub-state within TRACKING is tracked via:
      _window_tracker.is_open() → CHARGING sub-state
      not _window_tracker.is_open() and active_session → CHARGED/WAITING sub-state
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: Any,  # ConfigEntry
        config_store: Any,  # ConfigStore
        session_store: SessionStore,
        debug_logger: DebugLogger | None = None,
    ) -> None:
        """Initialize the plug-anchored session engine."""
        self._hass = hass
        self._entry = entry
        self._config_store = config_store
        self._session_store = session_store
        self._debug_logger = debug_logger

        # Engine state
        self._state = SessionEngineState.IDLE
        self._active_session: Session | None = None

        # Charging window tracker — in-memory, one per active session
        self._window_tracker: ChargingWindowTracker = ChargingWindowTracker()

        # Cached last valid readings (survives transient unavailability)
        self._last_energy_kwh: float | None = None
        self._last_power_w: float | None = None
        self._last_trx: str | int | None = None

        # Observation signal caches (for debug log "before → after" formatting)
        self._last_plug: str | None = None
        self._last_cable_lock: str | None = None
        self._last_model_status: str | None = None
        self._last_err: str | None = None

        # Transient-disconnect grace timer handle (FR-004 / Story 06)
        # NOT persisted across restarts (FR-030)
        self._disconnect_grace_cancel: _Unsub = None

        # RFID grace state (PR-23, US1, FR-001).
        # When plug transitions off→on with trx in _INVALID_STATES and
        # rfid_grace_seconds > 0, we defer session start for up to
        # rfid_grace_seconds seconds to allow a real RFID blip to propagate.
        # None when no grace window is active; fully set otherwise (no partial state).
        self._rfid_grace: _RfidGraceState | None = None

        # Idle timer handle — cancels when power resumes (T026)
        self._idle_timer_cancel: _Unsub = None

        # PR-23 US5: periodic HEARTBEAT log timer and UI dispatch timer.
        # Each is an async_track_time_interval unsub handle (or None when not registered).
        # A value of None means the timer is not active (either disabled via option=0
        # or not yet registered by async_setup).
        self._heartbeat_log_timer_unsub: _Unsub = None
        self._ui_dispatch_timer_unsub: _Unsub = None

        # Data quality flags
        self._data_gap: bool = False
        self._eto_start: float | None = None

        # Last unknown session diagnostics (persists until next unknown session)
        self._last_unknown_reason: str | None = None
        self._last_unknown_at: str | None = None

        # Last completed session info (for StatusSensor attributes)
        self._last_session_user: str | None = None
        self._last_session_rfid_index: int | None = None

        # Offline state tracking (FR-028: all-entities-unavailable must NOT trigger grace timer)
        self._charger_offline: bool = False
        self._offline_entities_count: int = 0

        # Guest pricing snapshot (PR-06 — snapshotted at SESSION_START)
        self._guest_pricing: GuestPricing | None = None

        # Spot pricing state
        self._hour_energy_snapshot: float = 0.0
        self._hour_start_time: str = ""
        self._hourly_unsub: Any = None

        # Story 07 (passive notification): set of notification IDs currently visible,
        # keyed for auto-dismiss when the corresponding mapping is added (FR-022).
        # Each entry pairs notification_id with the rfid_index (or None for trx-null).
        self._active_unmapped_notifications: dict[str, int | None] = {}

        # Engine-managed unsubs for listeners we register from inside the engine.
        # Cleared on async_unload (FR-029 spirit — leaks were the root cause of
        # BUG-6 where per-session async_on_unload accumulated stale handlers).
        self._engine_unsubs: list[Any] = []

        # Pending defer of restart recovery while plug entity is not yet available
        # (BUG-3): unsub for the wait listener if scheduled.
        self._deferred_recovery_unsub: Any = None
        # HIGH-1: timeout cancel handle for the deferred-recovery wait. If the
        # plug entity never reports a valid state within DEFERRED_RECOVERY_TIMEOUT_MIN,
        # the engine fires a persistent notification and force-completes the snapshot.
        self._deferred_recovery_timeout_unsub: Any = None

        # Build pricing engine from entry data (immutable at runtime)
        pricing_mode = entry.data.get("pricing_mode", "static")
        spot_config: SpotConfig | None = None
        if pricing_mode == "spot":
            spot_config = SpotConfig(
                price_entity=entry.data.get(CONF_SPOT_PRICE_ENTITY, ""),
                additional_cost_kwh=entry.data.get(
                    CONF_SPOT_ADDITIONAL_COST_KWH, DEFAULT_SPOT_ADDITIONAL_COST_KWH
                ),
                vat_multiplier=entry.data.get(
                    CONF_SPOT_VAT_MULTIPLIER, DEFAULT_SPOT_VAT_MULTIPLIER
                ),
                fallback_price_kwh=entry.data.get(
                    CONF_SPOT_FALLBACK_PRICE_KWH, DEFAULT_SPOT_FALLBACK_PRICE_KWH
                ),
            )
        self._pricing = PricingEngine(
            mode=pricing_mode,
            static_price=entry.data.get(CONF_STATIC_PRICE_KWH, DEFAULT_STATIC_PRICE_KWH),
            spot_config=spot_config,
        )

    # -----------------------------------------------------------------------
    # Public interface (coordinator-callable, same as legacy SessionEngine)
    # -----------------------------------------------------------------------

    @property
    def state(self) -> SessionEngineState:
        """Return the current engine state machine state."""
        return self._state

    @property
    def active_session(self) -> Session | None:
        """Return the active session, or None if idle."""
        return self._active_session

    @property
    def last_unknown_reason(self) -> str | None:
        """Return the last diagnostic reason for an unknown session."""
        return self._last_unknown_reason

    @property
    def last_unknown_at(self) -> str | None:
        """Return the ISO timestamp when the last unknown reason was set."""
        return self._last_unknown_at

    @property
    def last_session_user(self) -> str | None:
        """Return the user name from the last completed session."""
        return self._last_session_user

    @property
    def last_session_rfid_index(self) -> int | None:
        """Return the RFID index from the last completed session."""
        return self._last_session_rfid_index

    @property
    def window_tracker(self) -> ChargingWindowTracker:
        """Return the window tracker for the active session (for sensor reads)."""
        return self._window_tracker

    def get_status_sub_state(self) -> SessionSubState:
        """Return the status sensor sub-state for the current engine state.

        Returns:
            SessionSubState.IDLE     — no active session
            SessionSubState.WAITING  — active session but no charging window has opened
            SessionSubState.CHARGING — a charging window is currently open
            SessionSubState.CHARGED  — all windows closed, cable still in
        """
        if self._state == SessionEngineState.IDLE or self._active_session is None:
            return SessionSubState.IDLE
        if self._window_tracker.is_open():
            return SessionSubState.CHARGING
        if self._window_tracker.window_count() > 0:
            return SessionSubState.CHARGED
        return SessionSubState.WAITING

    def get_active_session_dict(self) -> dict | None:
        """Return the active session as a dict for periodic persistence, or None."""
        if self._active_session is None:
            return None
        return self._active_session.to_dict()

    async def async_unload(self) -> None:
        """Tear down engine-managed listeners (BUG-6 fix).

        Cancels any per-session callbacks (e.g. spot-pricing hourly tracker,
        deferred-recovery wait listener) that we register from inside the
        engine. Idempotent — safe to call multiple times.

        Called from __init__.async_unload_entry. Listeners registered via
        entry.async_on_unload are torn down by HA itself.
        """
        # Cancel idle/grace timers if still pending.
        self._cancel_idle_timer()
        self._cancel_grace_timer()
        self._cancel_rfid_grace_timer()

        # PR-23 US5: cancel periodic HEARTBEAT and UI dispatch timers (FR-015).
        # These are also registered with entry.async_on_unload, but explicit
        # cancel here ensures idempotent teardown on direct async_unload() calls.
        self._cancel_heartbeat_timer()
        self._cancel_ui_dispatch_timer()

        # Cancel deferred recovery wait if still in flight.
        if self._deferred_recovery_unsub is not None:
            try:
                self._deferred_recovery_unsub()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("deferred-recovery unsub failed: %s", err)
            self._deferred_recovery_unsub = None

        # HIGH-1: cancel the deferred-recovery timeout if still in flight.
        if self._deferred_recovery_timeout_unsub is not None:
            try:
                self._deferred_recovery_timeout_unsub()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("deferred-recovery-timeout unsub failed: %s", err)
            self._deferred_recovery_timeout_unsub = None

        # Cancel and clear all engine-managed unsubs.
        for unsub in list(self._engine_unsubs):
            try:
                unsub()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("engine unsub failed: %s", err)
        self._engine_unsubs.clear()

        if self._hourly_unsub is not None:
            try:
                self._hourly_unsub()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("hourly_unsub failed: %s", err)
            self._hourly_unsub = None

    # -----------------------------------------------------------------------
    # Recovery (called before async_setup by async_setup_entry)
    # -----------------------------------------------------------------------

    async def async_recover(self, snapshot: dict | None) -> None:
        """Recover an active session after an HA restart.

        Compares the saved session snapshot with the current charger state to
        determine the correct recovery path per FR-026 / FR-027 (Story 14).
        """
        if snapshot is None:
            return
        try:
            await self._async_do_recover(snapshot)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "PlugAnchoredSessionEngine recovery failed — starting fresh: %s. id=%s",
                err,
                snapshot.get("id", "?"),
            )
            self._state = SessionEngineState.IDLE
            self._active_session = None

    async def _async_do_recover(self, snapshot: dict) -> None:
        """Execute the plug-anchored restart recovery logic.

        BUG-3 fix: when the plug entity is not yet available (None — unavailable /
        unknown / entity not loaded at boot), DO NOT silently treat the plug as
        off. That would prematurely complete the session with the wrong disconnect
        time. Instead, defer recovery until a valid plug state arrives.
        """
        now = dt_util.utcnow()

        # Read current charger state — plug may be None if entity not yet loaded.
        current_plug = self._get_plug()
        plug_entity_id = self._entry.options.get(CONF_PLUG_ENTITY)

        if current_plug is None and plug_entity_id:
            # Plug entity not yet reporting a usable value — defer recovery (BUG-3).
            if self._debug_logger:
                snap_id = snapshot.get("id", "?")
                self._debug_logger.log(
                    DEBUG_CAT_HA_RESTART_DETECTED,
                    "RECOVERY_DEFERRED_WAITING_FOR_PLUG — plug entity unavailable at "
                    f"recovery time (entity_id={plug_entity_id} snapshot_id={snap_id})",
                )
            _LOGGER.info(
                "PlugAnchoredSessionEngine recovery: deferring — plug entity %s "
                "not yet available; will retry on first valid state",
                plug_entity_id,
            )
            await self._defer_recovery_until_plug_ready(snapshot, plug_entity_id)
            return

        current_cable_lock = self._get_cable_lock()
        current_energy = self._get_energy() or 0.0
        current_power = self._get_power() or 0.0

        if self._debug_logger:
            self._debug_logger.log(
                DEBUG_CAT_HA_RESTART_DETECTED,
                f"restart detected — snapshot id={snapshot.get('id', '?')} "
                f"current_plug={current_plug} cable_lock={current_cable_lock} "
                f"power={current_power:.0f}W energy={current_energy:.3f}kWh",
            )

        energy_start = float(snapshot.get("energy_start_kwh", 0.0))
        energy_counter_reset = current_energy < energy_start

        # Determine whether plug is currently on (explicit string compare;
        # None was handled by the early return above).
        plug_on = current_plug == "on"

        if plug_on and not energy_counter_reset:
            # Cable still in — resume the session (FR-026)
            await self._resume_session_from_snapshot(snapshot, current_energy, current_power, now)
        else:
            # Cable removed or energy counter reset — complete the old session (FR-027)
            reason = "energy counter reset" if energy_counter_reset else "plug was off at restart"
            _LOGGER.info(
                "PlugAnchoredSessionEngine recovery: completing old session (%s) id=%s",
                reason,
                snapshot.get("id", "?"),
            )
            await self._complete_snapshot_as_session(snapshot, current_energy, now)

            if self._debug_logger:
                self._debug_logger.log(
                    DEBUG_CAT_SESSION_FORCE_ENDED_BY_RESTART,
                    f"session ended at restart — reason={reason} id={snapshot.get('id', '?')}",
                )

    async def _defer_recovery_until_plug_ready(self, snapshot: dict, plug_entity_id: str) -> None:
        """Register a one-shot listener that re-runs recovery when plug entity is valid.

        BUG-3 fix: avoids the silent-corruption path where current_plug == None at
        boot causes the engine to assume plug is off and prematurely complete the
        session with the restart timestamp as disconnected_at.

        HIGH-1 fix: also registers a timeout (DEFERRED_RECOVERY_TIMEOUT_MIN). If
        the plug entity never reports a valid state within the window (e.g. user
        has a typo in their plug entity ID, charger is permanently offline), the
        engine fires a persistent notification and force-completes the snapshot
        rather than staying deferred forever.
        """

        async def _on_plug_ready(event: Event) -> None:
            new_state = event.data.get("new_state")
            if new_state is None:
                return
            if new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                return
            # Plug entity now has a valid state — cancel listener + timeout and resume recovery.
            if self._deferred_recovery_unsub is not None:
                self._deferred_recovery_unsub()
                self._deferred_recovery_unsub = None
            if self._deferred_recovery_timeout_unsub is not None:
                self._deferred_recovery_timeout_unsub()
                self._deferred_recovery_timeout_unsub = None
            if self._debug_logger:
                self._debug_logger.log(
                    DEBUG_CAT_HA_RESTART_DETECTED,
                    f"plug entity now valid ({new_state.state!r}) — running deferred recovery",
                )
            try:
                await self._async_do_recover(snapshot)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error(
                    "PlugAnchoredSessionEngine deferred recovery failed: %s id=%s",
                    err,
                    snapshot.get("id", "?"),
                )
                self._state = SessionEngineState.IDLE
                self._active_session = None

        async def _on_recovery_timeout(_now: datetime) -> None:
            # Cancel the plug-state listener (it will no longer be needed).
            if self._deferred_recovery_unsub is not None:
                try:
                    self._deferred_recovery_unsub()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("deferred-recovery unsub failed at timeout: %s", err)
                self._deferred_recovery_unsub = None
            self._deferred_recovery_timeout_unsub = None

            snap_id = snapshot.get("id", "?")
            _LOGGER.error(
                "PlugAnchoredSessionEngine: deferred recovery timed out after %d min "
                "— plug entity %s never reported a valid state; force-completing "
                "snapshot id=%s",
                DEFERRED_RECOVERY_TIMEOUT_MIN,
                plug_entity_id,
                snap_id,
            )

            if self._debug_logger:
                self._debug_logger.log(
                    DEBUG_CAT_RECOVERY_TIMEOUT,
                    f"deferred recovery timed out after {DEFERRED_RECOVERY_TIMEOUT_MIN} min "
                    f"— plug entity {plug_entity_id} never valid; "
                    f"force-completing snapshot id={snap_id}",
                )

            # User-visible notification (FR — HIGH-1).
            try:
                persistent_notification.async_create(
                    self._hass,
                    message=(
                        f"The plug entity **{plug_entity_id}** did not report a valid "
                        f"state within {DEFERRED_RECOVERY_TIMEOUT_MIN} minutes after Home "
                        "Assistant restart. The previously active session has been "
                        "force-completed with a data gap.\n\n"
                        "Please verify that your plug entity is configured correctly "
                        "(Integration → Configure → Observation entities) and that the "
                        "charger is online."
                    ),
                    title="EV Charging Manager — session recovery timed out",
                    notification_id=(
                        f"ev_charging_manager_recovery_timeout_{self._entry.entry_id}"
                    ),
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "PlugAnchoredSessionEngine: failed to create recovery-timeout notification: %s",
                    err,
                )

            # Force-complete the snapshot as a reconstructed session (mirrors BUG-3
            # path but with restart-time as disconnected_at).
            try:
                now = dt_util.utcnow()
                current_energy = self._get_energy() or float(
                    snapshot.get("energy_start_kwh", 0.0)
                ) + float(snapshot.get("energy_kwh", 0.0))
                await self._complete_snapshot_as_session(snapshot, current_energy, now)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error(
                    "PlugAnchoredSessionEngine: force-complete after recovery timeout "
                    "failed: %s id=%s",
                    err,
                    snap_id,
                )
                self._state = SessionEngineState.IDLE
                self._active_session = None

        self._deferred_recovery_unsub = async_track_state_change_event(
            self._hass, [plug_entity_id], _on_plug_ready
        )
        self._engine_unsubs.append(self._deferred_recovery_unsub)

        self._deferred_recovery_timeout_unsub = async_call_later(
            self._hass,
            DEFERRED_RECOVERY_TIMEOUT_MIN * 60,
            _on_recovery_timeout,
        )
        self._engine_unsubs.append(self._deferred_recovery_timeout_unsub)

    async def _resume_session_from_snapshot(
        self,
        snapshot: dict,
        current_energy: float,
        current_power: float,
        now: datetime,
    ) -> None:
        """Resume an active session after HA restart with plug still connected."""
        # Restore session object
        session = Session(
            id=snapshot["id"],
            user_name=snapshot.get("user_name", "Unknown"),
            user_type=snapshot.get("user_type", "unknown"),
            vehicle_name=snapshot.get("vehicle_name"),
            vehicle_battery_kwh=snapshot.get("vehicle_battery_kwh"),
            efficiency_factor=snapshot.get("efficiency_factor"),
            rfid_index=snapshot.get("rfid_index"),
            rfid_uid=snapshot.get("rfid_uid"),
            charger_name=snapshot.get("charger_name", ""),
            started_at=snapshot.get("started_at", now.isoformat()),
            connected_at=(
                snapshot.get("connected_at") or snapshot.get("started_at", now.isoformat())
            ),
            energy_start_kwh=float(snapshot.get("energy_start_kwh", 0.0)),
            energy_kwh=max(0.0, current_energy - float(snapshot.get("energy_start_kwh", 0.0))),
            cost_total_kr=float(snapshot.get("cost_total_kr", 0.0)),
            cost_method=snapshot.get("cost_method", "static"),
            price_details=snapshot.get("price_details"),
            charger_total_before_kwh=snapshot.get("charger_total_before_kwh"),
            max_power_w=float(snapshot.get("max_power_w", 0.0)),
            charging_started_at=snapshot.get("charging_started_at"),
            charging_ended_at=snapshot.get("charging_ended_at"),
            charging_duration_s=int(snapshot.get("charging_duration_s", 0)),
            charging_window_count=int(snapshot.get("charging_window_count", 0)),
            reconstructed=True,
            data_gap=True,  # always set on restart per FR-026
        )

        self._active_session = session
        self._last_energy_kwh = current_energy
        self._last_power_w = current_power
        self._state = SessionEngineState.TRACKING

        # BUG-2 fix: re-arm the spot-pricing hourly snapshot callback after restart.
        # Without this, _hourly_unsub stays None, the hourly callback never fires,
        # and post-restart energy is bundled incorrectly into the "current hour".
        # Seed hour-state from the current wall clock and energy reading so the
        # next hourly boundary correctly accounts for energy delivered since restart.
        if self._pricing.mode == "spot" and self._hourly_unsub is None:
            self._hour_start_time = now.strftime("%Y-%m-%dT%H:00+00:00")
            self._hour_energy_snapshot = max(0.0, current_energy - session.energy_start_kwh)
            self._hourly_unsub = async_track_utc_time_change(
                self._hass, self._async_hourly_snapshot, minute=0, second=0
            )
            self._engine_unsubs.append(self._hourly_unsub)
            if self._debug_logger:
                self._debug_logger.log(
                    DEBUG_CAT_SESSION_RESUMED,
                    "spot-pricing hourly snapshot callback re-armed at restart"
                    f" (hour_start={self._hour_start_time}"
                    f" hour_energy_snapshot={self._hour_energy_snapshot:.3f}kWh)",
                )

        # Inject a synthetic closed window for any pre-restart open window whose close event
        # was never observed (IC-6: absorb all gap-period energy into this window).
        #
        # Detection: charging_started_at is set in the persisted snapshot. This means
        # a charging window was open when HA restarted. We have no persisted windows[]
        # array (Session.to_dict() does not include it), so any set charging_started_at
        # is sufficient evidence of an unclosed pre-restart window.
        #
        # The synthetic window covers the period from charging_started_at to recovery
        # time (now). Its energy span is session.energy_start_kwh → current_energy,
        # absorbing all pre-restart energy into this window per IC-6. Window N+1 then
        # opens at current_energy, so there is no double-counting.
        if session.charging_started_at is not None and session.charging_ended_at is None:
            try:
                pre_restart_start = datetime.fromisoformat(session.charging_started_at)
                # Clamp: defends against clock skew or corrupt charging_started_at —
                # inject_closed_window() rejects inverted intervals.
                pre_restart_end = now if now >= pre_restart_start else pre_restart_start
                # end_at = recovery time, not the persisted charging_ended_at: we have no
                # evidence about gap-period charging activity, so the most defensible bound
                # is "still charging until SESSION_RESUMED" — see IC-6 rationale.
                synthetic_window = ChargingWindow(
                    start_at=pre_restart_start,
                    end_at=pre_restart_end,
                    energy_start_kwh=session.energy_start_kwh,
                    energy_end_kwh=current_energy,
                    last_power_change_at=pre_restart_end,
                )
                self._window_tracker.inject_closed_window(synthetic_window)
                if self._debug_logger:
                    self._debug_logger.log(
                        DEBUG_CAT_SESSION_RESUMED,
                        f"synthetic window injected for pre-restart open window — "
                        f"window={session.charging_window_count} "
                        f"started_at={session.charging_started_at} "
                        f"ended_at={pre_restart_end.isoformat()} "
                        f"duration={synthetic_window.duration_s()}s "
                        f"energy={synthetic_window.energy_kwh():.3f}kWh",
                    )
            except (ValueError, TypeError, RuntimeError) as exc:
                _LOGGER.warning(
                    "PlugAnchoredSessionEngine: could not inject synthetic pre-restart window "
                    "(charging_started_at=%r): %s — continuing without it",
                    session.charging_started_at,
                    exc,
                )
                if self._debug_logger:
                    self._debug_logger.log(
                        DEBUG_CAT_HA_RESTART_DETECTED,
                        f"synthetic-window injection failed ({type(exc).__name__}: {exc}) — "
                        f"continuing without pre-restart window in tracker; "
                        f"charging_duration_s will be undercounted for this session",
                    )
        # else: session has no pre-restart open window — either WAITING (no window had
        # opened yet) or already finalized (charging_ended_at set); nothing to inject.

        # Reconcile window state: if power > 0, continue/open a window; else keep closed
        if current_power > 0:
            # Open a window (or continue one — we can't distinguish without full history)
            self._window_tracker.open_window(now, current_energy)
            session.charging_window_count += 1
            if session.charging_started_at is None:
                session.charging_started_at = now.isoformat()
            session.charging_ended_at = None
            if self._debug_logger:
                self._debug_logger.log(
                    DEBUG_CAT_CHARGING_WINDOW_OPEN,
                    f"window={session.charging_window_count} opened at restart "
                    f"(power={current_power:.0f}W energy_start={current_energy:.3f}kWh)",
                )
        # If power=0, leave window tracker empty — window will open when power returns

        if self._debug_logger:
            self._debug_logger.log(
                DEBUG_CAT_SESSION_RESUMED,
                f"session resumed after restart — id={session.id} "
                f"user={session.user_name} energy_so_far={session.energy_kwh:.3f}kWh "
                f"data_gap=True reconstructed=True",
            )

        _LOGGER.info(
            "PlugAnchoredSessionEngine: session resumed after restart id=%s user=%s",
            session.id,
            session.user_name,
        )

    async def _complete_snapshot_as_session(
        self, snapshot: dict, current_energy: float, now: datetime
    ) -> None:
        """Complete a snapshot as a reconstructed session with best available data."""
        energy_start = float(snapshot.get("energy_start_kwh", 0.0))
        saved_energy_kwh = float(snapshot.get("energy_kwh", 0.0))
        best_energy = (
            max(0.0, current_energy - energy_start)
            if current_energy >= energy_start
            else saved_energy_kwh
        )

        connected_at_str = snapshot.get("connected_at") or snapshot.get(
            "started_at", now.isoformat()
        )
        try:
            connected_at = datetime.fromisoformat(connected_at_str)
            connection_s = max(0, int((now - connected_at).total_seconds()))
        except (ValueError, TypeError):
            connection_s = 0

        charging_duration_s = int(
            snapshot.get("charging_duration_s") or snapshot.get("duration_seconds", 0)
        )

        session = Session(
            id=snapshot["id"],
            user_name=snapshot.get("user_name", "Unknown"),
            user_type=snapshot.get("user_type", "unknown"),
            vehicle_name=snapshot.get("vehicle_name"),
            vehicle_battery_kwh=snapshot.get("vehicle_battery_kwh"),
            efficiency_factor=snapshot.get("efficiency_factor"),
            rfid_index=snapshot.get("rfid_index"),
            rfid_uid=snapshot.get("rfid_uid"),
            charger_name=snapshot.get("charger_name", ""),
            started_at=connected_at_str,
            ended_at=now.isoformat(),
            connected_at=connected_at_str,
            disconnected_at=now.isoformat(),
            connection_duration_s=connection_s,
            charging_started_at=snapshot.get("charging_started_at"),
            charging_ended_at=now.isoformat() if charging_duration_s > 0 else None,
            charging_duration_s=charging_duration_s,
            charging_window_count=int(snapshot.get("charging_window_count", 0)),
            energy_start_kwh=energy_start,
            energy_kwh=round(best_energy, 3),
            cost_total_kr=float(snapshot.get("cost_total_kr", 0.0)),
            cost_method=snapshot.get("cost_method", "static"),
            price_details=snapshot.get("price_details"),
            charger_total_before_kwh=snapshot.get("charger_total_before_kwh"),
            max_power_w=float(snapshot.get("max_power_w", 0.0)),
            reconstructed=True,
            data_gap=True,
        )

        if charging_duration_s > 0 and session.energy_kwh > 0:
            session.avg_power_w = round((session.energy_kwh * 3_600_000) / charging_duration_s, 1)

        min_duration = self._entry.options.get(
            CONF_MIN_SESSION_DURATION_S, DEFAULT_MIN_SESSION_DURATION_S
        )
        min_energy_kwh = (
            self._entry.options.get(CONF_MIN_SESSION_ENERGY_WH, DEFAULT_MIN_SESSION_ENERGY_WH)
            / 1000.0
        )
        is_micro = connection_s < min_duration or session.energy_kwh < min_energy_kwh

        if not is_micro:
            await self._session_store.add_session(session.to_dict())
            self._hass.bus.async_fire(
                EVENT_SESSION_COMPLETED, self._build_completed_event_data(session)
            )

        self._state = SessionEngineState.IDLE

    # -----------------------------------------------------------------------
    # Setup: register HA entity listeners (T028)
    # -----------------------------------------------------------------------

    @callback
    def async_setup(self) -> None:
        """Register state-change listeners for the plug and supporting entities."""
        entry = self._entry
        plug_entity = entry.options.get(CONF_PLUG_ENTITY)
        cable_lock_entity = entry.options.get(CONF_CABLE_LOCK_ENTITY)
        energy_entity = entry.data.get(CONF_ENERGY_ENTITY)
        power_entity = entry.data.get(CONF_POWER_ENTITY)
        rfid_entity = entry.data.get(CONF_RFID_ENTITY)

        # All entities we subscribe to
        watched = [
            e
            for e in [plug_entity, cable_lock_entity, energy_entity, power_entity, rfid_entity]
            if e
        ]

        # Optional observation-only entities
        for conf_key in (CONF_CABLE_LOCK_ENTITY, "model_status_entity", "error_entity"):
            obs_entity = entry.options.get(conf_key)
            if obs_entity and obs_entity not in watched:
                watched.append(obs_entity)

        if not watched:
            _LOGGER.warning(
                "PlugAnchoredSessionEngine: no charger entities configured, engine inactive"
            )
            return

        unsub = async_track_state_change_event(
            self._hass,
            watched,
            self._async_on_state_change,
        )
        # State-change listener stays for the lifetime of the entry; entry.async_on_unload
        # is appropriate here (single registration). BUG-6 only applies to per-session
        # registrations like the spot-pricing hourly callback.
        entry.async_on_unload(unsub)

        # FR-022 (passive notification dismiss): subscribe to the
        # SIGNAL_RFID_MAPPING_ADDED dispatcher signal so we can dismiss stale
        # unmapped-RFID notifications when the user creates a mapping.
        signal = SIGNAL_RFID_MAPPING_ADDED.format(entry.entry_id)
        signal_unsub = async_dispatcher_connect(
            self._hass,
            signal,
            self._on_rfid_mapping_added,
        )
        entry.async_on_unload(signal_unsub)

        # FR-015 (PR-23): ensure the RFID grace timer is always cancelled on entry
        # reload/unload, preventing a stranded timer from firing a session start
        # after the engine has been torn down.
        entry.async_on_unload(self._cancel_rfid_grace_timer)

        # PR-23 US5 (FR-015, FR-016): register HEARTBEAT log timer and UI dispatch
        # timer. Each is only registered when its option value is > 0 (0 = disabled).
        # The two timers are fully independent — disabling one does not affect the other.
        heartbeat_interval = entry.options.get(
            CONF_HEARTBEAT_LOG_INTERVAL_MIN, DEFAULT_HEARTBEAT_LOG_INTERVAL_MIN
        )
        if heartbeat_interval > 0:
            self._heartbeat_log_timer_unsub = async_track_time_interval(
                self._hass,
                self._emit_heartbeat,
                timedelta(minutes=heartbeat_interval),
            )
            entry.async_on_unload(self._cancel_heartbeat_timer)

        ui_dispatch_interval = entry.options.get(
            CONF_UI_DISPATCH_INTERVAL_S, DEFAULT_UI_DISPATCH_INTERVAL_S
        )
        if ui_dispatch_interval > 0:
            self._ui_dispatch_timer_unsub = async_track_time_interval(
                self._hass,
                self._dispatch_for_ui_tick,
                timedelta(seconds=ui_dispatch_interval),
            )
            entry.async_on_unload(self._cancel_ui_dispatch_timer)

        _LOGGER.debug("PlugAnchoredSessionEngine registered listeners for: %s", watched)

        # Prime observation caches
        obs_map = {
            plug_entity: "_last_plug",
            cable_lock_entity: "_last_cable_lock",
            entry.options.get("model_status_entity"): "_last_model_status",
            entry.options.get("error_entity"): "_last_err",
            rfid_entity: "_last_trx",
        }
        for entity_id, attr_name in obs_map.items():
            if not entity_id:
                continue
            state = self._hass.states.get(entity_id)
            if state and state.state not in _INVALID_STATES:
                setattr(self, attr_name, state.state)

    # -----------------------------------------------------------------------
    # State change dispatch (T028)
    # -----------------------------------------------------------------------

    @callback
    def _async_on_state_change(self, event: Event) -> None:
        """Handle any state change on a watched entity."""
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        new_val = new_state.state if new_state else None

        entry = self._entry
        plug_entity = entry.options.get(CONF_PLUG_ENTITY)
        cable_lock_entity = entry.options.get(CONF_CABLE_LOCK_ENTITY)
        energy_entity = entry.data.get(CONF_ENERGY_ENTITY)
        power_entity = entry.data.get(CONF_POWER_ENTITY)
        rfid_entity = entry.data.get(CONF_RFID_ENTITY)
        model_status_entity = entry.options.get("model_status_entity")
        error_entity = entry.options.get("error_entity")

        # ----- Plug entity: primary session boundary trigger -----
        if entity_id == plug_entity and plug_entity:
            if new_val is not None:
                self._handle_observation_change("PLUG_STATE", "plug", "_last_plug", new_val)
            self._handle_plug_change(new_val)
            self._dispatch_update()
            return

        # ----- Cable lock: observation only (read at plug-off time from HA state) -----
        if entity_id == cable_lock_entity and cable_lock_entity:
            if new_val is not None:
                self._handle_observation_change("CABLE_LOCK", "cus", "_last_cable_lock", new_val)
            return

        # ----- Model status: observation only -----
        if entity_id == model_status_entity and model_status_entity:
            if new_val is not None:
                self._handle_observation_change(
                    "MODEL_STATUS", "modelstatus", "_last_model_status", new_val
                )
            return

        # ----- Error entity: observation only -----
        if entity_id == error_entity and error_entity:
            if new_val is not None:
                self._handle_observation_change("ERR_STATE", "err", "_last_err", new_val)
            return

        # ----- RFID/trx: observation log + defensive mid-session detection -----
        if entity_id == rfid_entity and rfid_entity and new_val is not None:
            self._handle_observation_change("TRX_STATE", "trx", "_last_trx", new_val)
            if self._state == SessionEngineState.TRACKING and self._active_session is not None:
                # Defensive mid-session RFID change detection (FR-N05, Decision 18.5)
                # Log but do NOT alter session attribution
                try:
                    trx_int = int(new_val)
                    rfid_index = trx_int - 1 if trx_int > 0 else None
                    if (
                        rfid_index != self._active_session.rfid_index
                        and new_val not in _INVALID_STATES
                    ):
                        if self._debug_logger:
                            self._debug_logger.log(
                                DEBUG_CAT_TRX_MIDSESSION,
                                f"trx changed mid-session: was"
                                f" rfid_index={self._active_session.rfid_index}"
                                f" now trx={new_val} — ignored (no attribution change)",
                            )
                except (ValueError, TypeError):
                    pass
            return

        # ----- Power entity: drives window open/close logic -----
        if entity_id == power_entity and power_entity:
            self._handle_power_change(new_val)
            self._dispatch_update()
            return

        # ----- Energy entity: update session energy tracking -----
        if entity_id == energy_entity and energy_entity:
            self._handle_energy_update(new_val)
            self._dispatch_update()
            return

    # -----------------------------------------------------------------------
    # Plug handling: session start/end (T024)
    # -----------------------------------------------------------------------

    def _handle_plug_change(self, new_val: str | None) -> None:
        """Handle plug entity state change — the primary session boundary signal.

        BUG-7 fix: STATE_UNAVAILABLE / STATE_UNKNOWN (the literal strings, not
        Python None) used to silently no-op. The go-e WebSocket emits these
        strings when the integration loses contact with the device, and the old
        code kept accumulating energy as if the plug were still on. Treat any
        non-"on"/"off" plug value the same way the None branch does: surface
        the transition to the offline-detector and log it for diagnostics.
        """
        if new_val is None or new_val in _INVALID_STATES:
            # plug entity went unavailable / unknown / null — check for all-offline.
            if self._debug_logger:
                self._debug_logger.log(
                    "PLUG_STATE",
                    f"plug entity reports non-binary value: {new_val!r} — "
                    "treating as offline (BUG-7 fix)",
                )
            # FR-005 (PR-23): if RFID grace timer is active, cancel it and return to
            # IDLE — the session must not start after an invalid plug state.
            if self._rfid_grace is not None:
                self._cancel_rfid_grace_timer()
                self._state = SessionEngineState.IDLE
                if self._debug_logger:
                    self._debug_logger.log(
                        DEBUG_CAT_RFID_GRACE,
                        f"plug entered invalid state ({new_val!r}) during RFID grace "
                        "window — grace cancelled, no session started (FR-005)",
                    )
            self._check_charger_offline()
            return

        # Valid plug value arrived (the early-return above already filtered out
        # _INVALID_STATES) — charger may be back online.
        if self._charger_offline:
            self._charger_offline = False
            if self._debug_logger:
                self._debug_logger.log(
                    DEBUG_CAT_CHARGER_BACK_ONLINE,
                    f"plug entity back online (value={new_val})",
                )

        if new_val == "on":
            self._handle_plug_on()
        elif new_val == "off":
            self._handle_plug_off()
        else:
            # Defensive: any other non-on/off string we don't recognise. Log it.
            if self._debug_logger:
                self._debug_logger.log(
                    "PLUG_STATE",
                    f"plug entity reports unrecognised value {new_val!r}; ignored",
                )

    def _handle_plug_on(self) -> None:
        """Plug transitioned off → on: start a new session (PR-22 FR-001).

        If called while already TRACKING (transient disconnect resolved),
        cancel the grace timer and log DISCONNECT_RESOLVED.

        Implements PR-22 FR-001 (plug-on handling) extended by PR-23 FR-001/FR-007/FR-008
        (RFID grace timer): if rfid_grace_seconds > 0 and the current trx is null/invalid,
        defer session start by starting the RFID grace timer instead of calling
        _async_start_session immediately.  The grace timer will fire _async_start_session
        once trx resolves or the timer expires.
        """
        if self._state == SessionEngineState.TRACKING:
            # Plug returned during a transient disconnect — cancel grace timer
            self._handle_plug_on_during_tracking()
            return

        if self._state != SessionEngineState.IDLE:
            # Already completing — ignore
            return

        # Cancel any lingering grace or disconnect timer (defensive)
        self._cancel_grace_timer()
        self._cancel_rfid_grace_timer()

        self._state = SessionEngineState.TRACKING

        # Determine whether to defer session start via RFID grace timer
        # (PR-23 FR-001, FR-007, FR-008). trx="0" denotes open-access at go-e
        # and is intentionally NOT in _INVALID_STATES, so the immediate-start
        # branch handles it (PR-23 FR-008).
        rfid_grace_seconds = self._entry.options.get(
            CONF_RFID_GRACE_SECONDS, DEFAULT_RFID_GRACE_SECONDS
        )
        current_trx = self._get_trx()

        # rfid_grace_seconds == 0 is the explicit "disabled" sentinel (PR-23 FR-007);
        # falsy short-circuit takes us straight to the immediate-start path.
        if rfid_grace_seconds > 0 and current_trx in _INVALID_STATES:
            # trx is null/unavailable at plug-on and grace is enabled → defer (PR-23 FR-001)
            now = dt_util.utcnow()
            self._start_rfid_grace_timer(now, rfid_grace_seconds)
        else:
            # Either grace is disabled (PR-23 FR-007) or trx is already non-null (PR-23 FR-008) →
            # immediate start (preserves existing behavior)
            self._hass.async_create_task(self._async_start_session())

    def _handle_plug_off(self) -> None:
        """Plug transitioned on → off: validate with cable_lock before ending (PR-22 FR-002/FR-003).

        If cable_lock == Unlocked → real unplug → end session immediately.
        Otherwise → transient disconnect → start grace timer (PR-22 FR-003, FR-004).

        PR-23 FR-004: if the RFID grace timer is still active (session not yet
        started), cancel it and return to IDLE — do NOT start a session.
        """
        if self._state == SessionEngineState.IDLE:
            return  # no session to end

        # FR-028: If all entities just went unavailable simultaneously,
        # do NOT treat this as a real plug-off
        if self._charger_offline:
            _LOGGER.debug("PlugAnchoredSessionEngine: plug=off but charger is offline — ignoring")
            return

        # FR-004 (PR-23): if RFID grace timer is active, a session has not been committed
        # yet — cancel the timer and return to IDLE without starting any session.
        if self._rfid_grace is not None:
            self._cancel_rfid_grace_timer()
            self._state = SessionEngineState.IDLE
            if self._debug_logger:
                self._debug_logger.log(
                    DEBUG_CAT_RFID_GRACE,
                    "plug went off during RFID grace window — grace cancelled, no session started",
                )
            return

        cable_lock = self._get_cable_lock()
        if cable_lock == "Unlocked":
            # Validated real unplug (FR-002)
            self._cancel_grace_timer()
            self._cancel_idle_timer()
            self._state = SessionEngineState.COMPLETING
            self._hass.async_create_task(self._async_complete_session())
        else:
            # Transient disconnect (FR-003) — cable_lock is Locked / unknown / Lock failed
            self._data_gap = True  # engine-level flag; transferred to session on completion
            if self._active_session is not None:
                self._active_session.data_gap = True
            if self._debug_logger:
                self._debug_logger.log(
                    DEBUG_CAT_DISCONNECT_DETECTED,
                    f"plug=off but cable_lock={cable_lock!r} (not Unlocked) — "
                    "treating as transient disconnect, starting grace timer",
                )
            self._start_grace_timer()

    def _start_grace_timer(self) -> None:
        """Start the disconnect grace timer (FR-004)."""
        self._cancel_grace_timer()
        grace_min = self._entry.options.get(CONF_DISCONNECT_GRACE_MIN, DEFAULT_DISCONNECT_GRACE_MIN)
        grace_seconds = grace_min * 60

        @callback
        def _grace_expired(_now: datetime) -> None:
            """Force-end session when grace period expires."""
            if self._state != SessionEngineState.TRACKING:
                return
            if self._debug_logger:
                self._debug_logger.log(
                    DEBUG_CAT_SESSION_FORCE_ENDED_BY_GRACE_TIMEOUT,
                    f"grace timer expired after {grace_min} min — force-ending session",
                )
            self._cancel_idle_timer()
            self._state = SessionEngineState.COMPLETING
            self._hass.async_create_task(self._async_complete_session())

        self._disconnect_grace_cancel = async_call_later(self._hass, grace_seconds, _grace_expired)

    # -----------------------------------------------------------------------
    # Timer cancellation helpers
    # -----------------------------------------------------------------------

    def _safe_cancel(self, attr: str) -> None:
        """Cancel a single unsubscribe/cancel handle by attribute name.

        Idempotent: no-op if attribute is None or absent. Swallows exceptions
        from the cancel callable with a debug log — cancellation is a
        cleanup operation and should not raise back to HA's event loop.
        """
        handle = getattr(self, attr, None)
        if handle is not None:
            try:
                handle()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("%s cancel failed: %s", attr, err)
            setattr(self, attr, None)

    def _cancel_grace_timer(self) -> None:
        """Cancel the disconnect grace timer if active."""
        self._safe_cancel("_disconnect_grace_cancel")

    # -----------------------------------------------------------------------
    # RFID grace timer (PR-23, US1, FR-001..FR-008)
    # Mirrors the disconnect-grace pattern (research.md R1 / IC-1).
    # -----------------------------------------------------------------------

    def _start_rfid_grace_timer(self, plug_on_at: datetime, grace_seconds: int) -> None:
        """Start the RFID grace timer after plug-on with trx=null (FR-001).

        Schedules _rfid_grace_expired to fire after grace_seconds. Also
        registers a temporary trx listener so we fire immediately if trx
        becomes non-null before the timer expires (FR-002).

        Args:
            plug_on_at: UTC timestamp of the plug-on event (used as connected_at, FR-006).
            grace_seconds: Seconds to wait before committing with user=Unknown.
        """
        # Cancel any stale grace state (defensive; should not be active in IDLE)
        self._cancel_rfid_grace_timer()

        @callback
        def _rfid_grace_expired(_fire_time: datetime) -> None:
            """Fire when grace window expires without RFID resolution (FR-003)."""
            # Only proceed if we are still waiting for RFID resolution.
            # If the plug has gone away in the meantime, _cancel_rfid_grace_timer
            # should have already cleared _rfid_grace — check it here as a safety net.
            if self._rfid_grace is None:
                return
            if self._state != SessionEngineState.TRACKING:
                # Engine left TRACKING (plug-off during grace, etc.) — abort
                self._rfid_grace = None
                return

            # Save plug_on_at BEFORE calling _cancel_rfid_grace_timer (which clears the state)
            saved_plug_on_at = self._rfid_grace.plug_on_at
            self._cancel_rfid_grace_timer()

            # Start session with Unknown user; pass plug-on time as connected_at (FR-006)
            self._hass.async_create_task(self._async_start_session(connected_at=saved_plug_on_at))

        cancel_handle = async_call_later(self._hass, grace_seconds, _rfid_grace_expired)

        # Register a temporary trx listener so we can fire immediately if a
        # non-null trx arrives during the grace window (FR-002).
        rfid_entity = self._entry.data.get(CONF_RFID_ENTITY)
        trx_listener_unsub: Callable[[], None] | None = None
        if rfid_entity:

            async def _on_trx_non_null_during_grace(event: Any) -> None:
                """Fire session start with the resolved trx and cancel the grace timer.

                Order of operations matters: save plug_on_at BEFORE calling
                _cancel_rfid_grace_timer (which clears the state), then schedule the
                session start with the saved value.
                """
                new_state = event.data.get("new_state")
                new_val = new_state.state if new_state else None

                # Ignore invalid or null values — keep waiting
                if new_val in _INVALID_STATES:
                    return

                # Grace was already completed or cancelled — do not double-start
                if self._rfid_grace is None:
                    return

                if self._state != SessionEngineState.TRACKING:
                    return

                # Save plug_on_at BEFORE cancelling (cancel clears the state)
                saved_plug_on_at = self._rfid_grace.plug_on_at

                # Cancel the timer (and the trx listener) now that we have the trx
                self._cancel_rfid_grace_timer()

                # Start session immediately with the resolved trx (FR-002).
                # _async_start_session reads trx from the entity directly via _get_trx(),
                # so as long as the new_val is already in the entity state by now
                # (which it is — this callback fires after the state change), it will
                # be resolved by RfidLookup.
                self._hass.async_create_task(
                    self._async_start_session(connected_at=saved_plug_on_at)
                )

            trx_listener_unsub = async_track_state_change_event(
                self._hass,
                [rfid_entity],
                _on_trx_non_null_during_grace,
            )

        self._rfid_grace = _RfidGraceState(
            plug_on_at=plug_on_at,
            cancel=cancel_handle,
            trx_listener_unsub=trx_listener_unsub,
        )

        if self._debug_logger:
            self._debug_logger.log(
                DEBUG_CAT_RFID_GRACE,
                f"RFID grace timer started: waiting up to {grace_seconds}s for non-null trx "
                f"(plug_on_at={plug_on_at.isoformat()})",
            )

    def _cancel_rfid_grace_timer(self) -> None:
        """Cancel the RFID grace timer and associated trx listener. Idempotent (IC-1)."""
        state = self._rfid_grace
        if state is None:
            return
        try:
            state.cancel()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("rfid_grace cancel failed: %s", err)
        if state.trx_listener_unsub is not None:
            try:
                state.trx_listener_unsub()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("rfid_grace trx listener unsub failed: %s", err)
        self._rfid_grace = None

    # -----------------------------------------------------------------------
    # Power handling: charging window lifecycle (T025, T026)
    # -----------------------------------------------------------------------

    def _handle_power_change(self, new_val: str | None) -> None:
        """Handle power entity state change — drives window open/close logic."""
        if self._state != SessionEngineState.TRACKING or self._active_session is None:
            return

        try:
            power_w = float(new_val) if new_val not in _INVALID_STATES else None
        except (ValueError, TypeError):
            power_w = None

        if power_w is None:
            # Energy sensor went unavailable — flag data gap
            if not self._data_gap:
                self._data_gap = True
                self._active_session.data_gap = True
            return

        self._last_power_w = power_w
        if self._active_session is not None:
            self._active_session.max_power_w = max(self._active_session.max_power_w, power_w)

        now = dt_util.utcnow()
        tracker = self._window_tracker
        prev_power = None
        if tracker.active_window() is not None:
            prev_power = tracker.active_window().last_power_value

        # Update the active window's power reading
        tracker.on_power_change(now, power_w)

        was_charging = prev_power is not None and prev_power > 0
        is_charging = power_w > 0

        if is_charging and not tracker.is_open():
            # Power rose from 0 to > 0 — open a new window
            self._cancel_idle_timer()
            self._open_window(now)

        elif not is_charging and tracker.is_open():
            # Power dropped to 0 within an open window — start idle timer
            self._cancel_idle_timer()
            self._start_idle_timer(now)

        elif is_charging and tracker.is_open() and not was_charging:
            # Power resumed within an open window (was 0, now > 0, window already open)
            # This path handles the case where open window tracked a 0 briefly
            self._cancel_idle_timer()

    def _open_window(self, now: datetime) -> None:
        """Open a new charging window (T025)."""
        energy_now = self._last_energy_kwh or 0.0
        session = self._active_session
        if session is None:
            return

        self._window_tracker.open_window(now, energy_now)
        session.charging_window_count += 1
        # charging_started_at is set ONCE for the session (FR-007)
        if session.charging_started_at is None:
            session.charging_started_at = now.isoformat()
        # Clear charging_ended_at when a new window opens (FR-009, Decision 13.4)
        session.charging_ended_at = None

        if self._debug_logger:
            self._debug_logger.log(
                DEBUG_CAT_CHARGING_WINDOW_OPEN,
                f"window={session.charging_window_count} "
                f"energy_start={energy_now:.3f}kWh "
                f"session_id={session.id}",
            )

    def _start_idle_timer(self, _now: datetime) -> None:
        """Start the charging-idle timer (T026). Fires when power has been 0 for idle_timeout."""
        idle_min = self._entry.options.get(
            CONF_CHARGING_IDLE_TIMEOUT_MIN, DEFAULT_CHARGING_IDLE_TIMEOUT_MIN
        )
        idle_seconds = idle_min * 60

        @callback
        def _idle_expired(fire_time: datetime) -> None:
            """Close the current window after idle timeout elapses."""
            if not self._window_tracker.is_open():
                return
            self._close_window(fire_time)
            self._dispatch_update()

        self._idle_timer_cancel = async_call_later(self._hass, idle_seconds, _idle_expired)

    def _cancel_idle_timer(self) -> None:
        """Cancel the idle timer if active (power resumed before timeout)."""
        self._safe_cancel("_idle_timer_cancel")

    def _close_window(self, now: datetime) -> None:
        """Close the active charging window (T026) and fire ev_charging_charged."""
        session = self._active_session
        if session is None or not self._window_tracker.is_open():
            return

        energy_now = self._last_energy_kwh or 0.0
        closed_window = self._window_tracker.close_window(now, energy_now)

        # Update session fields
        session.charging_ended_at = now.isoformat()
        session.charging_duration_s = self._window_tracker.total_charging_duration_s(now)

        if self._debug_logger:
            self._debug_logger.log(
                DEBUG_CAT_CHARGING_WINDOW_CLOSE,
                # Session-wide window counter — _window_tracker.closed_window_count()
                # resets on restart, but session.charging_window_count persists (IC-6).
                f"window={session.charging_window_count} "
                f"energy_end={energy_now:.3f}kWh "
                f"duration={closed_window.duration_s():.0f}s "
                f"session_id={session.id}",
            )

        # Fire ev_charging_charged event (FR-018)
        self._hass.bus.async_fire(
            EVENT_CHARGING_CHARGED,
            {
                "session_id": session.id,
                "window_index": self._window_tracker.closed_window_count(),
                "window_started_at": closed_window.start_at.isoformat(),
                "window_ended_at": now.isoformat(),
                "window_energy_kwh": round(closed_window.energy_kwh(), 3),
                "window_duration_s": closed_window.duration_s(),
                "user_name": session.user_name,
                "user_type": session.user_type,
                "vehicle_name": session.vehicle_name,
                "charger_name": session.charger_name,
                # Cumulative session totals so far
                "session_charging_duration_s": session.charging_duration_s,
                "session_energy_kwh": round(session.energy_kwh, 3),
                "session_window_count": session.charging_window_count,
            },
        )

    # -----------------------------------------------------------------------
    # Energy tracking (T028)
    # -----------------------------------------------------------------------

    def _handle_energy_update(self, new_val: str | None) -> None:
        """Handle energy entity state change — update session energy."""
        if self._state != SessionEngineState.TRACKING or self._active_session is None:
            return

        if new_val in _INVALID_STATES:
            if not self._data_gap:
                self._data_gap = True
                self._active_session.data_gap = True
                _LOGGER.warning(
                    "Energy entity unavailable during active session"
                    " — keeping last value, flagging data gap"
                )
            return

        try:
            energy_kwh = float(new_val)
        except (ValueError, TypeError):
            return

        self._last_energy_kwh = energy_kwh
        session = self._active_session

        # Energy delta from session start
        session.energy_kwh = max(0.0, energy_kwh - session.energy_start_kwh)

        # Mode-aware cost update
        if self._pricing.mode == "static":
            session.cost_total_kr = self._pricing.calculate(session.energy_kwh)
        else:
            completed_cost = self._pricing.calculate_spot_total(session.price_details or [])
            partial_kwh = max(0.0, session.energy_kwh - self._hour_energy_snapshot)
            spot_price = self._read_spot_price()
            partial_detail = self._pricing.calculate_spot_hour(partial_kwh, spot_price)
            session.cost_total_kr = round(completed_cost + partial_detail["cost_kr"], 4)

        # Update guest charge price
        if self._guest_pricing is not None:
            session.charge_price_total_kr = self._calculate_charge_price(session)

        # Update SoC estimate
        if session.vehicle_battery_kwh is not None:
            session.estimated_soc_added_pct = estimate_soc(
                session.energy_kwh, session.efficiency_factor, session.vehicle_battery_kwh
            )

    # -----------------------------------------------------------------------
    # Session start (T029, T030)
    # -----------------------------------------------------------------------

    async def _async_start_session(self, connected_at: datetime | None = None) -> None:
        """Create a new session on plug-on event — WAITING state.

        Args:
            connected_at: Optional plug-on timestamp to use as the session's
                connected_at value (FR-006, PR-23 US1). When provided (RFID
                grace path), this records the actual plug-on time rather than
                the deferred session-start time. When None (immediate path),
                the current time is used (existing behavior).
        """
        # Idempotency guard: a late-arriving scheduled task (e.g. from grace-expiry
        # or trx-resolve callback) must abort if the engine has moved on between
        # task scheduling and execution (FR-004).
        if self._state != SessionEngineState.TRACKING or self._active_session is not None:
            if self._debug_logger:
                self._debug_logger.log(
                    DEBUG_CAT_RFID_GRACE,
                    f"late session-start task aborted (state={self._state.value} "
                    f"active_session={'present' if self._active_session else 'None'})",
                )
            return

        now = dt_util.utcnow()
        now_iso = now.isoformat()
        # Use the caller-provided plug-on time if available (FR-006), else current time.
        connected_at_iso = connected_at.isoformat() if connected_at is not None else now_iso

        trx = self._get_trx()
        rfid_lookup = RfidLookup(self._config_store.data)
        resolution = rfid_lookup.resolve(trx)

        # Debug log RFID resolution
        if self._debug_logger:
            if resolution is not None and resolution.user_type != "unknown":
                self._debug_logger.log(
                    "RFID_READ",
                    f"tag={trx} matched user={resolution.user_name} "
                    f"(rfid_index={resolution.rfid_index})",
                )
            else:
                self._debug_logger.log("RFID_READ", f"tag={trx} unknown")

        # Snapshot energy at session start
        energy = self._get_energy() or 0.0
        self._last_energy_kwh = energy

        power = self._get_power() or 0.0
        self._last_power_w = power

        # Optional RFID UID
        rfid_uid: str | None = None
        uid_entity = self._entry.data.get(CONF_RFID_UID_ENTITY)
        if uid_entity:
            uid_val = self._get_entity_state(uid_entity)
            if uid_val:
                rfid_uid = uid_val

        charger_name = self._entry.data.get(CONF_CHARGER_NAME, "")

        # Create session record — snapshot principle: all user/vehicle/pricing data at start
        self._active_session = Session(
            user_name=resolution.user_name if resolution else "Unknown",
            user_type=resolution.user_type if resolution else "unknown",
            vehicle_name=resolution.vehicle_name if resolution else None,
            vehicle_battery_kwh=resolution.vehicle_battery_kwh if resolution else None,
            efficiency_factor=resolution.efficiency_factor if resolution else None,
            rfid_index=resolution.rfid_index if resolution else None,
            rfid_uid=rfid_uid,
            charger_name=charger_name,
            # Legacy alias (backward compat) — always the session-start time
            started_at=now_iso,
            # PR-22 canonical — plug-on time (may predate session start when RFID grace used)
            connected_at=connected_at_iso,
            energy_start_kwh=energy,
            energy_kwh=0.0,
        )

        # Reset window tracker for this session
        self._window_tracker = ChargingWindowTracker()
        self._data_gap = False
        self._eto_start = self._get_eto()

        # Snapshot guest pricing (Constitution §II Snapshot Principle)
        if resolution is not None and resolution.guest_pricing is not None:
            self._guest_pricing = GuestPricing.from_dict(resolution.guest_pricing)
            self._active_session.charge_price_method = self._guest_pricing.method
        else:
            self._guest_pricing = None

        # Record diagnostic reason for unknown sessions
        session_user_type = resolution.user_type if resolution else "unknown"
        if session_user_type == "unknown":
            if resolution is None:
                self._last_unknown_reason = UNKNOWN_REASON_TRX_NULL
            else:
                self._last_unknown_reason = _RFID_REASON_MAP.get(
                    resolution.reason or "", UNKNOWN_REASON_RFID_UNMAPPED
                )
            self._last_unknown_at = now_iso

        # Spot mode initialization
        if self._pricing.mode == "spot":
            self._active_session.cost_method = "spot"
            self._active_session.price_details = []
            self._hour_energy_snapshot = 0.0
            self._hour_start_time = now.strftime("%Y-%m-%dT%H:00+00:00")
            self._hourly_unsub = async_track_utc_time_change(
                self._hass, self._async_hourly_snapshot, minute=0, second=0
            )
            # BUG-6 fix: do NOT call entry.async_on_unload here. Each session would
            # leak a stale callback into the entry's unload list because
            # _async_complete_session cancels the handle but cannot remove it from
            # the list. Manage lifecycle ourselves via async_unload() below.
            self._engine_unsubs.append(self._hourly_unsub)

        # Story 07: check for unmapped RFID and trigger passive notification if needed
        if session_user_type == "unknown" and resolution is not None:
            if resolution.reason in ("unmapped", "rfid_inactive", "type_error"):
                await self._async_handle_unknown_rfid(resolution.rfid_index, resolution.reason)

        if self._debug_logger:
            self._debug_logger.log(
                "SESSION_START",
                f"session_id={self._active_session.id} user={self._active_session.user_name} "
                f"charger={charger_name} connected_at={connected_at_iso}",
            )
            self._debug_logger.log(
                "ENGINE_DECISION",
                f"IDLE → TRACKING (trigger: plug=on + rfid_resolved) energy_start={energy:.3f}kWh",
            )

        _LOGGER.info(
            "PlugAnchoredSessionEngine: session started id=%s user=%s trx=%s",
            self._active_session.id,
            self._active_session.user_name,
            trx,
        )

        # Fire session_started event (keeping started_at as backward-compat alias)
        self._hass.bus.async_fire(
            EVENT_SESSION_STARTED,
            {
                "session_id": self._active_session.id,
                "user_name": self._active_session.user_name,
                "user_type": self._active_session.user_type,
                "vehicle_name": self._active_session.vehicle_name,
                "rfid_index": self._active_session.rfid_index,
                "rfid_uid": self._active_session.rfid_uid,
                "started_at": now_iso,  # backward-compat alias for connected_at
                "charger": charger_name,
            },
        )

        self._dispatch_update()

        # If power is already > 0 at plug-in (rare: pre-authorized charge), open window
        if power > 0:
            self._open_window(now)
            self._dispatch_update()

    async def _async_handle_unknown_rfid(self, rfid_index: int | None, reason: str) -> None:
        """Passive-notification handler for unmapped RFID at session start.

        PR-22 revision 2026-05-19 (Story 07 — REVISED):
          1. Fire EVENT_UNKNOWN_RFID_DETECTED so user automations can react,
             regardless of the notify_unmapped_rfid option (FR-024).
          2. If notify_unmapped_rfid is True, create a persistent_notification
             with a deterministic ID so the same RFID does not produce
             duplicates and so the dismisser can later clear it (FR-021).
          3. NEVER make an HTTP call to the charger (FR-023, Constitution §I).
          4. NEVER raise — the session lifecycle must not be affected by a
             notification failure (FR-025).
        """
        charger_name = self._entry.data.get(CONF_CHARGER_NAME, "")
        now_iso = dt_util.utcnow().isoformat()

        # FR-019 / FR-024: always fire the event (even when notifications disabled).
        event_payload: dict[str, Any] = {
            "rfid_index": rfid_index,
            "reason": reason,
            "charger_name": charger_name,
            "detected_at": now_iso,
        }
        try:
            self._hass.bus.async_fire(EVENT_UNKNOWN_RFID_DETECTED, event_payload)
        except Exception as err:  # noqa: BLE001
            # Bus errors should not affect session lifecycle.
            _LOGGER.warning(
                "PlugAnchoredSessionEngine: failed to fire EVENT_UNKNOWN_RFID_DETECTED: %s",
                err,
            )

        notify_enabled = self._entry.options.get(
            CONF_NOTIFY_UNMAPPED_RFID, DEFAULT_NOTIFY_UNMAPPED_RFID
        )
        if not notify_enabled:
            return

        # FR-021: deterministic notification ID.
        notif_id_key = str(rfid_index) if rfid_index is not None else "null"
        notification_id = NOTIFICATION_ID_UNKNOWN_RFID.format(notif_id_key)

        # FR-020: notification text MUST be unambiguous about the consequence.
        title = "EV Charging Manager: unmapped RFID tag"
        rfid_label = f"RFID slot {rfid_index}" if rfid_index is not None else "the active RFID slot"
        message = (
            f"The charger **{charger_name}** accepted **{rfid_label}** "
            f"({reason}), but no user mapping exists for it in this integration.\n\n"
            "Energy from this session is being attributed to the **Unknown** "
            "bucket in statistics and per-user totals.\n\n"
            "To fix this, open the integration's **Configure → Add RFID mapping** "
            "flow and assign the tag to a user. This notification will dismiss "
            "automatically once the mapping is created."
        )

        try:
            persistent_notification.async_create(
                self._hass,
                message=message,
                title=title,
                notification_id=notification_id,
            )
            self._active_unmapped_notifications[notification_id] = rfid_index
            if self._debug_logger:
                self._debug_logger.log(
                    DEBUG_CAT_RFID_UNMAPPED_NOTIFIED,
                    f"created persistent notification id={notification_id} "
                    f"rfid_index={rfid_index} reason={reason}",
                )
        except Exception as err:  # noqa: BLE001
            # FR-025: notification failure MUST NOT affect session lifecycle.
            if self._debug_logger:
                self._debug_logger.log(
                    DEBUG_CAT_RFID_UNMAPPED_NOTIFY_FAILED,
                    f"persistent_notification.async_create failed: {err!r} "
                    f"id={notification_id} rfid_index={rfid_index}",
                )
            _LOGGER.warning(
                "PlugAnchoredSessionEngine: failed to create unmapped-RFID notification: %s",
                err,
            )

    @callback
    def _on_rfid_mapping_added(self, rfid_index: int | None) -> None:
        """Auto-dismiss any active unmapped-RFID notification for this index.

        Called via dispatcher signal SIGNAL_RFID_MAPPING_ADDED when ConfigStore
        records a new RFID mapping (FR-022).
        """
        if not self._active_unmapped_notifications:
            return

        # Find any notification IDs whose rfid_index matches.
        to_dismiss = [
            nid for nid, idx in self._active_unmapped_notifications.items() if idx == rfid_index
        ]
        for notification_id in to_dismiss:
            try:
                persistent_notification.async_dismiss(self._hass, notification_id)
                if self._debug_logger:
                    self._debug_logger.log(
                        DEBUG_CAT_RFID_UNMAPPED_NOTIFIED,
                        f"dismissed notification id={notification_id} "
                        f"(mapping added for rfid_index={rfid_index})",
                    )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "PlugAnchoredSessionEngine: dismiss failed for %s: %s",
                    notification_id,
                    err,
                )
            finally:
                self._active_unmapped_notifications.pop(notification_id, None)

    # -----------------------------------------------------------------------
    # Session completion (T027)
    # -----------------------------------------------------------------------

    async def _async_complete_session(self) -> None:
        """Finalize session: compute metrics, apply micro-filter, persist, fire event."""
        session = self._active_session

        if session is None:
            self._state = SessionEngineState.IDLE
            self._dispatch_update()
            return

        # Cancel any pending timers
        self._cancel_idle_timer()
        self._cancel_grace_timer()

        now = dt_util.utcnow()
        now_iso = now.isoformat()

        # Close any open window with current time
        if self._window_tracker.is_open():
            self._close_window(now)

        # Compute final durations
        connected_at_str = session.connected_at or session.started_at
        try:
            connected_at = datetime.fromisoformat(connected_at_str)
            connection_s = max(0, int((now - connected_at).total_seconds()))
        except (ValueError, TypeError):
            connection_s = 0

        session.disconnected_at = now_iso
        session.ended_at = now_iso  # backward compat alias
        session.connection_duration_s = connection_s

        # charging_duration_s is the sum of all window durations (FR-011)
        session.charging_duration_s = self._window_tracker.total_charging_duration_s()

        # avg_power_w computed from charging_duration_s, NOT connection (FR-012)
        if session.charging_duration_s > 0 and session.energy_kwh > 0:
            session.avg_power_w = round(
                (session.energy_kwh * 3_600_000) / session.charging_duration_s, 1
            )
        else:
            session.avg_power_w = 0.0

        # Transfer data quality flags
        session.data_gap = self._data_gap
        session.reconstructed = getattr(session, "reconstructed", False)

        # Spot mode: finalize last partial hour
        if self._pricing.mode == "spot" and self._hourly_unsub is not None:
            # Remove the unsub from the engine-managed list before calling it so
            # async_unload() doesn't double-cancel a stale handle (BUG-6).
            try:
                self._engine_unsubs.remove(self._hourly_unsub)
            except ValueError:
                pass
            self._hourly_unsub()
            self._hourly_unsub = None
            current_relative_energy = (self._last_energy_kwh or 0.0) - session.energy_start_kwh
            kwh_final = max(0.0, current_relative_energy - self._hour_energy_snapshot)
            spot_price = self._read_spot_price()
            final_detail = self._pricing.calculate_spot_hour(kwh_final, spot_price)
            final_detail["hour"] = self._hour_start_time
            final_detail["kwh"] = round(kwh_final, 3)
            if session.price_details is None:
                session.price_details = []
            session.price_details.append(final_detail)
            session.cost_total_kr = self._pricing.calculate_spot_total(session.price_details)

        # ETO cross-validation
        eto_end = self._get_eto()
        if self._eto_start is not None and eto_end is not None:
            session.charger_total_before_kwh = self._eto_start
            session.charger_total_after_kwh = eto_end

        # Final guest charge price
        charge_price = self._calculate_charge_price(session)
        if charge_price is not None:
            session.charge_price_total_kr = charge_price

        # Micro-filter (FR-N04 note: fumble sessions are two separate sessions;
        # micro-filter handles them by discarding sub-threshold records)
        min_duration = self._entry.options.get(
            CONF_MIN_SESSION_DURATION_S, DEFAULT_MIN_SESSION_DURATION_S
        )
        min_energy_kwh = (
            self._entry.options.get(CONF_MIN_SESSION_ENERGY_WH, DEFAULT_MIN_SESSION_ENERGY_WH)
            / 1000.0
        )
        is_micro = connection_s < min_duration or session.energy_kwh < min_energy_kwh

        if self._debug_logger:
            h = connection_s // 3600
            m = (connection_s % 3600) // 60
            s = connection_s % 60
            self._debug_logger.log(
                "SESSION_STOP",
                f"session_id={session.id} energy={session.energy_kwh:.3f}kWh "
                f"connection={h}:{m:02d}:{s:02d} "
                f"charging_duration={session.charging_duration_s}s "
                f"windows={session.charging_window_count} micro={is_micro}",
            )

        # BUG-4 fix: wrap persist + event-fire in try/finally so a disk-full or JSON
        # error cannot leave the engine stuck in COMPLETING with no event fired.
        # The IDLE-reset block at the bottom MUST always run, and the SESSION_COMPLETED
        # event MUST still fire so downstream stats consumers can recover.
        persist_error: Exception | None = None
        try:
            if not is_micro:
                try:
                    await self._session_store.add_session(session.to_dict())
                except Exception as err:  # noqa: BLE001
                    persist_error = err
                    _LOGGER.error(
                        "PlugAnchoredSessionEngine: failed to persist session id=%s: %s",
                        session.id,
                        err,
                        exc_info=True,
                    )
                    # Surface the failure to the user — without this they would
                    # silently see "charging stopped working" with no diagnostic.
                    try:
                        persistent_notification.async_create(
                            self._hass,
                            message=(
                                "Failed to persist a completed charging session. "
                                f"session_id={session.id} error={err!r}.\n\n"
                                "Check Home Assistant logs and available disk space."
                            ),
                            title="EV Charging Manager: session persist failed",
                            notification_id=(f"ev_charging_manager_persist_failed_{session.id}"),
                        )
                    except Exception as notif_err:  # noqa: BLE001
                        _LOGGER.debug(
                            "PlugAnchoredSessionEngine: persist-failure notification "
                            "also failed: %s",
                            notif_err,
                        )
                # Always fire EVENT_SESSION_COMPLETED — even on persist failure —
                # so downstream consumers (stats engine, automations) can react.
                try:
                    self._hass.bus.async_fire(
                        EVENT_SESSION_COMPLETED,
                        self._build_completed_event_data(session),
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning(
                        "PlugAnchoredSessionEngine: failed to fire SESSION_COMPLETED: %s",
                        err,
                    )
                _LOGGER.info(
                    "PlugAnchoredSessionEngine: session completed id=%s energy=%.3f kWh "
                    "connection=%ds charging=%ds windows=%d persist_error=%s",
                    session.id,
                    session.energy_kwh,
                    connection_s,
                    session.charging_duration_s,
                    session.charging_window_count,
                    persist_error,
                )
            else:
                _LOGGER.info(
                    "PlugAnchoredSessionEngine: micro-session discarded id=%s "
                    "duration=%ds energy=%.3f kWh",
                    session.id,
                    connection_s,
                    session.energy_kwh,
                )
        finally:
            # Record last session info for StatusSensor
            self._last_session_user = session.user_name
            self._last_session_rfid_index = session.rfid_index

            # Reset to IDLE — must always happen so the engine does not get
            # stuck in COMPLETING after a persist failure (BUG-4).
            self._active_session = None
            self._window_tracker = ChargingWindowTracker()
            self._guest_pricing = None
            self._last_energy_kwh = 0.0
            self._last_power_w = 0.0
            self._hour_energy_snapshot = 0.0
            self._hour_start_time = ""
            self._data_gap = False
            self._eto_start = None
            self._state = SessionEngineState.IDLE

            self._dispatch_update()

    def _build_completed_event_data(self, session: Session) -> dict[str, Any]:
        """Build the EVENT_SESSION_COMPLETED payload."""
        return {
            "session_id": session.id,
            "user_name": session.user_name,
            "user_type": session.user_type,
            "vehicle_name": session.vehicle_name,
            "energy_kwh": round(session.energy_kwh, 2),
            "cost_kr": round(session.cost_total_kr, 2),
            "charge_price_kr": (
                round(session.charge_price_total_kr, 2)
                if session.charge_price_total_kr is not None
                else None
            ),
            # NOW: charging_duration_s/60 (FR-011, backward compat per contracts/ha-events.md)
            "duration_minutes": round((session.charging_duration_s or 0) / 60),
            "avg_power_w": round(session.avg_power_w, 1),
            "estimated_soc_added_pct": (
                round(session.estimated_soc_added_pct, 1)
                if session.estimated_soc_added_pct is not None
                else None
            ),
            "started_at": session.connected_at or session.started_at,
            "ended_at": session.disconnected_at or session.ended_at,
            "cost_method": session.cost_method,
            "reconstructed": session.reconstructed,
            "data_gap": session.data_gap,
            "rfid_index": session.rfid_index,
            "charger_name": self._entry.data.get("charger_name", "unknown"),
            # PR-22 new fields (revision 2026-05-19: `blocked` removed per FR-032).
            "connection_duration_s": session.connection_duration_s,
            "charging_duration_s": session.charging_duration_s,
            "charging_window_count": session.charging_window_count,
        }

    # -----------------------------------------------------------------------
    # Spot pricing hourly callback
    # -----------------------------------------------------------------------

    @callback
    def _async_hourly_snapshot(self, now: datetime) -> None:
        """Capture energy and cost for the completed hour (spot pricing)."""
        if self._active_session is None or self._pricing.mode != "spot":
            return
        session = self._active_session
        current_relative_energy = (self._last_energy_kwh or 0.0) - session.energy_start_kwh
        kwh_this_hour = max(0.0, current_relative_energy - self._hour_energy_snapshot)
        spot_price = self._read_spot_price()
        detail = self._pricing.calculate_spot_hour(kwh_this_hour, spot_price)
        detail["hour"] = self._hour_start_time
        detail["kwh"] = round(kwh_this_hour, 3)
        if session.price_details is None:
            session.price_details = []
        session.price_details.append(detail)
        session.cost_total_kr = self._pricing.calculate_spot_total(session.price_details)
        self._hour_energy_snapshot = current_relative_energy
        self._hour_start_time = now.strftime("%Y-%m-%dT%H:00+00:00")
        self._dispatch_update()

    # -----------------------------------------------------------------------
    # Charger-offline detection (FR-028)
    # -----------------------------------------------------------------------

    def _check_charger_offline(self) -> None:
        """Check if the charger appears fully offline (all key entities unavailable)."""
        entry = self._entry
        plug_entity = entry.options.get(CONF_PLUG_ENTITY)
        power_entity = entry.data.get(CONF_POWER_ENTITY)
        energy_entity = entry.data.get(CONF_ENERGY_ENTITY)

        def _is_unavail(entity_id: str | None) -> bool:
            if not entity_id:
                return False
            state = self._hass.states.get(entity_id)
            return state is not None and state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)

        # HIGH-3 fix: guard against zero configured entities. all([]) returns True,
        # which previously caused the engine to flip to offline state on any plug-
        # None event when no charger entities were configured at all.
        entities_to_check = [e for e in (plug_entity, power_entity, energy_entity) if e]
        if not entities_to_check:
            return

        all_offline = all(_is_unavail(e) for e in entities_to_check)

        if all_offline and not self._charger_offline:
            self._charger_offline = True
            if self._active_session is not None:
                self._active_session.data_gap = True
                self._data_gap = True
            if self._debug_logger:
                self._debug_logger.log(
                    DEBUG_CAT_CHARGER_OFFLINE,
                    "all charger entities unavailable — charger appears offline; "
                    "session held in tracking state (no grace timer — FR-028)",
                )

    # -----------------------------------------------------------------------
    # Entity state readers (shared helper pattern from legacy engine)
    # -----------------------------------------------------------------------

    def _is_valid_state(self, state_val: str | None) -> bool:
        return state_val not in _INVALID_STATES

    def _get_entity_state(self, entity_id: str | None) -> str | None:
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None:
            return None
        return state.state if self._is_valid_state(state.state) else None

    def _get_plug(self) -> str | None:
        return self._get_entity_state(self._entry.options.get(CONF_PLUG_ENTITY))

    def _get_cable_lock(self) -> str | None:
        return self._get_entity_state(self._entry.options.get(CONF_CABLE_LOCK_ENTITY))

    def _get_trx(self) -> str | None:
        return self._get_entity_state(self._entry.data.get(CONF_RFID_ENTITY))

    def _get_energy(self) -> float | None:
        entity_id = self._entry.data.get(CONF_ENERGY_ENTITY)
        val = self._get_entity_state(entity_id)
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def _get_power(self) -> float | None:
        entity_id = self._entry.data.get(CONF_POWER_ENTITY)
        val = self._get_entity_state(entity_id)
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def _get_eto(self) -> float | None:
        entity_id = self._entry.options.get(CONF_ETO_ENTITY)
        if not entity_id:
            return None
        val = self._get_entity_state(entity_id)
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def _read_spot_price(self) -> float | None:
        entity_id = self._entry.data.get(CONF_SPOT_PRICE_ENTITY)
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    def _calculate_charge_price(self, session: Session) -> float | None:
        if self._guest_pricing is None:
            return None
        if self._guest_pricing.method == "fixed" and self._guest_pricing.price_per_kwh is not None:
            return round(session.energy_kwh * self._guest_pricing.price_per_kwh, 2)
        if self._guest_pricing.method == "markup" and self._guest_pricing.markup_factor is not None:
            return round(session.cost_total_kr * self._guest_pricing.markup_factor, 2)
        return None

    # -----------------------------------------------------------------------
    # Observation logging helpers (from legacy engine)
    # -----------------------------------------------------------------------

    def _format_signal_snapshot(self) -> str:
        live_energy = self._get_energy()
        wh_str = (
            f"{live_energy:.3f}"
            if live_energy is not None
            else (f"{self._last_energy_kwh:.3f}" if self._last_energy_kwh is not None else "?")
        )
        live_power = self._get_power()
        power_str = (
            str(int(live_power))
            if live_power is not None
            else (str(int(self._last_power_w)) if self._last_power_w is not None else "?")
        )
        return f" | wh={wh_str} power={power_str}"

    def _handle_observation_change(
        self,
        category: str,
        signal_token: str,
        last_attr_name: str,
        new_value: str | int | None,
    ) -> None:
        if self._debug_logger is None or not self._debug_logger.enabled:
            return
        before = getattr(self, last_attr_name)
        if new_value == before:
            return
        if category == "ERR_STATE" and before == "-none-" and new_value == "-none-":
            return
        self._debug_logger.log(
            category,
            f"{signal_token} changed: {before} → {new_value}{self._format_signal_snapshot()}",
        )
        if new_value not in _INVALID_STATES:
            setattr(self, last_attr_name, new_value)

    # -----------------------------------------------------------------------
    # Dispatcher
    # -----------------------------------------------------------------------

    def _dispatch_update(self) -> None:
        signal = SIGNAL_SESSION_UPDATE.format(self._entry.entry_id)
        async_dispatcher_send(self._hass, signal)

    # -----------------------------------------------------------------------
    # PR-23 US5: Periodic HEARTBEAT log + UI dispatch tick (FR-012..FR-016)
    # -----------------------------------------------------------------------

    @callback
    def _emit_heartbeat(self, _now: datetime) -> None:
        """Append a HEARTBEAT diagnostic line to the debug log.

        Guards on TRACKING state AND active_session not None. If either guard
        fails at fire time (e.g. the session was completed concurrently), this
        is a silent no-op — no error is raised. FR-012, FR-014.

        Args:
            _now: Current datetime passed by async_track_time_interval (unused
                  directly; dt_util.utcnow() is called for freshness).
        """
        if self._state != SessionEngineState.TRACKING:
            return
        session = self._active_session
        if session is None:
            return
        if self._debug_logger is None:
            return

        now = dt_util.utcnow()

        # connection_s: seconds since plug-on (session.connected_at). Defensive
        # on missing or malformed connected_at; fall back to 0.
        connection_s = 0
        if session.connected_at:
            try:
                connected_dt = dt_util.parse_datetime(session.connected_at)
                if connected_dt is not None:
                    connection_s = max(0, int((now - connected_dt).total_seconds()))
            except Exception:  # noqa: BLE001
                connection_s = 0

        # charging_s: sum of all closed windows + current open window live delta
        charging_s = self._window_tracker.total_charging_duration_s(now)

        # Prefer live readings; fall back to last cached values; finally 0.
        # A genuine 0 between charging windows must surface as 0 (not stale cache).
        energy_kwh = self._get_energy()
        if energy_kwh is None:
            energy_kwh = self._last_energy_kwh if self._last_energy_kwh is not None else 0.0
        live_power = self._get_power()
        if live_power is None:
            live_power = self._last_power_w if self._last_power_w is not None else 0
        power_w = int(live_power)

        self._debug_logger.log(
            DEBUG_CAT_HEARTBEAT,
            f"state={self.get_status_sub_state()} "
            f"window={session.charging_window_count} "
            f"session_id={session.id} "
            f"wh={energy_kwh:.3f} power={power_w} "
            f"connection_s={connection_s} charging_s={charging_s}",
        )

    @callback
    def _dispatch_for_ui_tick(self, _now: datetime) -> None:
        """Send the SIGNAL_SESSION_UPDATE dispatcher signal for live sensor refresh.

        Guards on TRACKING state only — even between charging windows (where no
        open window exists but a session is active) we want the UI to tick.
        No session-presence check: the signal is cheap and sensors handle None.
        FR-013, FR-014.

        Args:
            _now: Current datetime passed by async_track_time_interval (unused).
        """
        if self._state != SessionEngineState.TRACKING:
            return
        self._dispatch_update()

    def _cancel_heartbeat_timer(self) -> None:
        """Cancel the HEARTBEAT log timer if active. Idempotent."""
        self._safe_cancel("_heartbeat_log_timer_unsub")

    def _cancel_ui_dispatch_timer(self) -> None:
        """Cancel the UI dispatch timer if active. Idempotent."""
        self._safe_cancel("_ui_dispatch_timer_unsub")

    # -----------------------------------------------------------------------
    # Plug-on handler when engine is reconnected after transient disconnect
    # -----------------------------------------------------------------------

    def _handle_plug_on_during_tracking(self) -> None:
        """Cable returned to on during a transient disconnect grace window."""
        self._cancel_grace_timer()
        if self._active_session is not None:
            if self._debug_logger:
                self._debug_logger.log(
                    DEBUG_CAT_DISCONNECT_RESOLVED,
                    f"plug returned to on — disconnect was transient; "
                    f"session_id={self._active_session.id}",
                )
