"""SessionEngine — core session tracking state machine for EV Charging Manager."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event, async_track_utc_time_change
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CAR_STATUS_CHARGING_VALUE,
    CONF_CAR_STATUS_ENTITY,
    CONF_CHARGER_NAME,
    CONF_ENERGY_ENTITY,
    CONF_MIN_SESSION_DURATION_S,
    CONF_MIN_SESSION_ENERGY_WH,
    CONF_POWER_ENTITY,
    CONF_RFID_ENTITY,
    CONF_RFID_UID_ENTITY,
    CONF_SPOT_ADDITIONAL_COST_KWH,
    CONF_SPOT_FALLBACK_PRICE_KWH,
    CONF_SPOT_PRICE_ENTITY,
    CONF_SPOT_VAT_MULTIPLIER,
    CONF_STATIC_PRICE_KWH,
    DEFAULT_CAR_STATUS_CHARGING_VALUE,
    DEFAULT_MIN_SESSION_DURATION_S,
    DEFAULT_MIN_SESSION_ENERGY_WH,
    DEFAULT_SPOT_ADDITIONAL_COST_KWH,
    DEFAULT_SPOT_FALLBACK_PRICE_KWH,
    DEFAULT_SPOT_VAT_MULTIPLIER,
    DEFAULT_STATIC_PRICE_KWH,
    EVENT_SESSION_COMPLETED,
    EVENT_SESSION_STARTED,
    SIGNAL_SESSION_UPDATE,
    SessionEngineState,
)
from .models import GuestPricing
from .pricing import PricingEngine, SpotConfig
from .rfid_lookup import RfidLookup
from .session import Session
from .session_store import SessionStore
from .soc import estimate_soc

_LOGGER = logging.getLogger(__name__)

# States that indicate an entity has no valid value
_INVALID_STATES = {STATE_UNAVAILABLE, STATE_UNKNOWN, None, "null", ""}


class SessionEngine:
    """Orchestrate charging session lifecycle for one config entry.

    Listens to HA state-change events for the configured charger entities.
    Implements a 3-state machine: IDLE → TRACKING → COMPLETING → IDLE.

    One SessionEngine instance per config entry, stored in
    hass.data[DOMAIN][entry_id]["session_engine"].
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: Any,  # ConfigEntry — avoiding circular import
        config_store: Any,  # ConfigStore
        session_store: SessionStore,
    ) -> None:
        """Initialize the session engine."""
        self._hass = hass
        self._entry = entry
        self._config_store = config_store
        self._session_store = session_store

        self._state = SessionEngineState.IDLE
        self._active_session: Session | None = None

        # Cached last valid values — kept during transient unavailability
        self._last_energy_kwh: float = 0.0
        self._last_power_w: float = 0.0
        self._last_trx: str | int | None = None

        # Note: RfidLookup is created fresh at session start using current ConfigStore data
        # (ConfigStore may be updated by subentry changes between sessions)

        # Guest pricing snapshot (PR-06) — set at session start, cleared at session end
        self._guest_pricing: GuestPricing | None = None

        # Spot mode tracking state
        self._hour_energy_snapshot: float = 0.0  # relative energy at last hour boundary
        self._hour_start_time: str = ""  # ISO timestamp of current hour start
        self._hourly_unsub: Any = None  # unsubscribe handle for hourly callback

        # Pricing built once (entry.data doesn't change at runtime)
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

    @property
    def state(self) -> SessionEngineState:
        """Return the current engine state."""
        return self._state

    @property
    def active_session(self) -> Session | None:
        """Return the active session, or None if idle."""
        return self._active_session

    @callback
    def async_setup(self) -> None:
        """Register state change listeners for all configured charger entities.

        All listeners are unsubscribed on entry unload via entry.async_on_unload().
        """
        entry = self._entry
        car_status_entity = entry.data.get(CONF_CAR_STATUS_ENTITY)
        rfid_entity = entry.data.get(CONF_RFID_ENTITY)
        energy_entity = entry.data.get(CONF_ENERGY_ENTITY)
        power_entity = entry.data.get(CONF_POWER_ENTITY)

        # Collect all entity IDs we need to watch
        watched = [e for e in [car_status_entity, rfid_entity, energy_entity, power_entity] if e]

        if not watched:
            _LOGGER.warning("SessionEngine: no charger entities configured, engine inactive")
            return

        unsub = async_track_state_change_event(
            self._hass,
            watched,
            self._async_on_state_change,
        )
        entry.async_on_unload(unsub)
        _LOGGER.debug("SessionEngine registered listeners for: %s", watched)

    def _is_valid_state(self, state_val: str | None) -> bool:
        """Return True if state value is a valid (non-unavailable) value."""
        return state_val not in _INVALID_STATES

    def _get_entity_state(self, entity_id: str | None) -> str | None:
        """Return the current state of an entity, or None if unavailable/missing."""
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None:
            return None
        return state.state if self._is_valid_state(state.state) else None

    def _get_car_status(self) -> str | None:
        """Return the current car status entity value."""
        return self._get_entity_state(self._entry.data.get(CONF_CAR_STATUS_ENTITY))

    def _get_trx(self) -> str | None:
        """Return the current RFID/trx entity value (raw string)."""
        return self._get_entity_state(self._entry.data.get(CONF_RFID_ENTITY))

    def _get_energy(self) -> float | None:
        """Return the current energy entity value in kWh, or None if unavailable."""
        entity_id = self._entry.data.get(CONF_ENERGY_ENTITY)
        val = self._get_entity_state(entity_id)
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            _LOGGER.warning("Energy entity %s has non-numeric value: %r", entity_id, val)
            return None

    def _get_power(self) -> float | None:
        """Return the current power entity value in W, or None if unavailable."""
        entity_id = self._entry.data.get(CONF_POWER_ENTITY)
        val = self._get_entity_state(entity_id)
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def _read_spot_price(self) -> float | None:
        """Read current spot price from configured sensor. Returns None if unavailable."""
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

    def _is_charging(self) -> bool:
        """Return True if car status indicates active charging."""
        charging_value = self._entry.data.get(
            CONF_CAR_STATUS_CHARGING_VALUE, DEFAULT_CAR_STATUS_CHARGING_VALUE
        )
        car_status = self._get_car_status()
        return car_status == charging_value

    def _is_trx_active(self) -> bool:
        """Return True if a valid trx value is present (including "0")."""
        trx = self._get_trx()
        return trx is not None  # "null"/unavailable already filtered by _get_entity_state

    @callback
    def _async_on_state_change(self, event: Event) -> None:
        """Handle any state change on a watched entity."""
        # Dispatch based on current state
        if self._state == SessionEngineState.IDLE:
            self._handle_idle_state()
        elif self._state == SessionEngineState.TRACKING:
            self._handle_tracking_state(event)

    def _handle_idle_state(self) -> None:
        """Evaluate IDLE → TRACKING transition."""
        if self._is_charging() and self._is_trx_active():
            # Set state synchronously to prevent duplicate tasks from rapid events
            self._state = SessionEngineState.TRACKING
            self._hass.async_create_task(self._async_start_session())

    def _handle_tracking_state(self, event: Event) -> None:
        """Update session or evaluate TRACKING → COMPLETING transition."""
        # Check for session end condition first
        if not self._is_charging():
            # Set state synchronously to prevent duplicate completion tasks
            self._state = SessionEngineState.COMPLETING
            self._hass.async_create_task(self._async_complete_session())
            return

        # Update energy and power with latest values
        energy = self._get_energy()
        if energy is None:
            _LOGGER.warning(
                "Energy entity unavailable during active session — keeping last value %.3f kWh",
                self._last_energy_kwh,
            )
        else:
            self._last_energy_kwh = energy

        power = self._get_power()
        if power is not None:
            self._last_power_w = power

        # Update active session metrics
        if self._active_session is not None:
            session = self._active_session
            current_energy = self._last_energy_kwh - session.energy_start_kwh
            session.energy_kwh = max(0.0, current_energy)

            # Mode-aware cost calculation
            if self._pricing.mode == "static":
                session.cost_total_kr = self._pricing.calculate(session.energy_kwh)
            else:
                # Spot: running cost = completed hours + current partial hour estimate
                completed_cost = self._pricing.calculate_spot_total(session.price_details or [])
                partial_kwh = max(0.0, session.energy_kwh - self._hour_energy_snapshot)
                spot_price = self._read_spot_price()
                partial_detail = self._pricing.calculate_spot_hour(partial_kwh, spot_price)
                session.cost_total_kr = round(completed_cost + partial_detail["cost_kr"], 4)

            session.max_power_w = max(session.max_power_w, self._last_power_w)

            # Update real-time guest charge price (PR-06)
            if self._guest_pricing is not None:
                session.charge_price_total_kr = self._calculate_charge_price(session)

            if session.vehicle_battery_kwh is not None:
                session.estimated_soc_added_pct = estimate_soc(
                    session.energy_kwh, session.efficiency_factor, session.vehicle_battery_kwh
                )

            _LOGGER.debug(
                "Session update: energy=%.3f kWh, power=%.0f W, cost=%.2f kr",
                session.energy_kwh,
                self._last_power_w,
                session.cost_total_kr,
            )

        # Notify sensor entities
        self._dispatch_update()

    @callback
    def _async_hourly_snapshot(self, now: datetime) -> None:
        """Capture energy snapshot and calculate cost for the completed hour."""
        if self._active_session is None or self._pricing.mode != "spot":
            return

        session = self._active_session
        current_relative_energy = self._last_energy_kwh - session.energy_start_kwh
        kwh_this_hour = max(0.0, current_relative_energy - self._hour_energy_snapshot)
        spot_price = self._read_spot_price()

        if spot_price is None:
            _LOGGER.warning(
                "Spot price sensor '%s' unavailable at %s — using fallback %.2f kr/kWh",
                self._entry.data.get(CONF_SPOT_PRICE_ENTITY),
                self._hour_start_time,
                self._pricing.fallback_price or 0.0,
            )

        detail = self._pricing.calculate_spot_hour(kwh_this_hour, spot_price)
        detail["hour"] = self._hour_start_time
        detail["kwh"] = round(kwh_this_hour, 3)

        session.price_details.append(detail)  # type: ignore[union-attr]
        session.cost_total_kr = self._pricing.calculate_spot_total(session.price_details)

        # Reset for next hour
        self._hour_energy_snapshot = current_relative_energy
        self._hour_start_time = now.strftime("%Y-%m-%dT%H:00+00:00")

        self._dispatch_update()

    async def _async_start_session(self) -> None:
        """Create a new session and transition IDLE → TRACKING."""
        trx = self._get_trx()
        # Create fresh RfidLookup from current ConfigStore data (subentries may have changed)
        rfid_lookup = RfidLookup(self._config_store.data)
        resolution = rfid_lookup.resolve(trx)

        # Snapshot energy at session start
        energy = self._get_energy()
        if energy is None:
            energy = 0.0
        self._last_energy_kwh = energy

        power = self._get_power() or 0.0
        self._last_power_w = power

        # Read optional RFID UID from lri/tsi sensor (T021)
        rfid_uid: str | None = None
        uid_entity = self._entry.data.get(CONF_RFID_UID_ENTITY)
        if uid_entity:
            uid_val = self._get_entity_state(uid_entity)
            if uid_val:
                rfid_uid = uid_val

        now = dt_util.utcnow()
        now_iso = now.isoformat()
        charger_name = self._entry.data.get(CONF_CHARGER_NAME, "")

        self._active_session = Session(
            user_name=resolution.user_name if resolution else "Unknown",
            user_type=resolution.user_type if resolution else "unknown",
            vehicle_name=resolution.vehicle_name if resolution else None,
            vehicle_battery_kwh=resolution.vehicle_battery_kwh if resolution else None,
            efficiency_factor=resolution.efficiency_factor if resolution else None,
            rfid_index=resolution.rfid_index if resolution else None,
            rfid_uid=rfid_uid,
            started_at=now_iso,
            energy_start_kwh=energy,
            energy_kwh=0.0,
            charger_name=charger_name,
        )

        # Snapshot guest pricing at session start (PR-06)
        if resolution is not None and resolution.guest_pricing is not None:
            self._guest_pricing = GuestPricing.from_dict(resolution.guest_pricing)
            self._active_session.charge_price_method = self._guest_pricing.method
        else:
            self._guest_pricing = None

        # Spot mode session initialization
        if self._pricing.mode == "spot":
            self._active_session.cost_method = "spot"
            self._active_session.price_details = []
            self._hour_energy_snapshot = 0.0  # relative to session start
            self._hour_start_time = now.strftime("%Y-%m-%dT%H:00+00:00")
            self._hourly_unsub = async_track_utc_time_change(
                self._hass,
                self._async_hourly_snapshot,
                minute=0,
                second=0,
            )
            self._entry.async_on_unload(self._hourly_unsub)

        self._state = SessionEngineState.TRACKING

        _LOGGER.info(
            "Session started: id=%s user=%s vehicle=%s trx=%s",
            self._active_session.id,
            self._active_session.user_name,
            self._active_session.vehicle_name,
            trx,
        )

        # Fire session_started event
        self._hass.bus.async_fire(
            EVENT_SESSION_STARTED,
            {
                "session_id": self._active_session.id,
                "user_name": self._active_session.user_name,
                "user_type": self._active_session.user_type,
                "vehicle_name": self._active_session.vehicle_name,
                "rfid_index": self._active_session.rfid_index,
                "rfid_uid": self._active_session.rfid_uid,
                "started_at": self._active_session.started_at,
                "charger": charger_name,
            },
        )

        self._dispatch_update()

    async def _async_complete_session(self) -> None:
        """Finalize session: micro-filter, persist, fire event, reset to IDLE."""
        self._state = SessionEngineState.COMPLETING
        session = self._active_session

        if session is None:
            self._state = SessionEngineState.IDLE
            self._dispatch_update()
            return

        # Spot mode: capture final partial hour before unsubscribing
        if self._pricing.mode == "spot" and self._hourly_unsub is not None:
            self._hourly_unsub()
            self._hourly_unsub = None

            current_relative_energy = self._last_energy_kwh - session.energy_start_kwh
            kwh_final = max(0.0, current_relative_energy - self._hour_energy_snapshot)
            spot_price = self._read_spot_price()

            if spot_price is None:
                _LOGGER.warning(
                    "Spot price sensor '%s' unavailable at session end (%s) — "
                    "using fallback %.2f kr/kWh",
                    self._entry.data.get(CONF_SPOT_PRICE_ENTITY),
                    self._hour_start_time,
                    self._pricing.fallback_price or 0.0,
                )

            final_detail = self._pricing.calculate_spot_hour(kwh_final, spot_price)
            final_detail["hour"] = self._hour_start_time
            final_detail["kwh"] = round(kwh_final, 3)

            session.price_details.append(final_detail)  # type: ignore[union-attr]
            session.cost_total_kr = self._pricing.calculate_spot_total(session.price_details)

        now = dt_util.utcnow()
        started = datetime.fromisoformat(session.started_at)
        duration_s = int((now - started).total_seconds())
        session.ended_at = now.isoformat()
        session.duration_seconds = duration_s

        # Calculate avg_power
        if duration_s > 0:
            session.avg_power_w = (session.energy_kwh * 3_600_000) / duration_s
        else:
            session.avg_power_w = 0.0

        # Read options with defaults
        min_duration = self._entry.options.get(
            CONF_MIN_SESSION_DURATION_S, DEFAULT_MIN_SESSION_DURATION_S
        )
        min_energy_wh = self._entry.options.get(
            CONF_MIN_SESSION_ENERGY_WH, DEFAULT_MIN_SESSION_ENERGY_WH
        )
        min_energy_kwh = min_energy_wh / 1000.0

        _LOGGER.info(
            "Session ending: id=%s duration=%ds energy=%.3f kWh (min: %ds, %.3f kWh)",
            session.id,
            duration_s,
            session.energy_kwh,
            min_duration,
            min_energy_kwh,
        )

        is_micro = duration_s < min_duration or session.energy_kwh < min_energy_kwh

        if not is_micro:
            # Calculate final guest charge price (PR-06)
            charge_price = self._calculate_charge_price(session)
            if charge_price is not None:
                session.charge_price_total_kr = charge_price

            # Persist and fire completed event
            await self._session_store.add_session(session.to_dict())
            self._hass.bus.async_fire(
                EVENT_SESSION_COMPLETED,
                {
                    "session_id": session.id,
                    "user_name": session.user_name,
                    "user_type": session.user_type,
                    "vehicle_name": session.vehicle_name,
                    "energy_kwh": round(session.energy_kwh, 2),
                    "cost_kr": round(session.cost_total_kr, 2),
                    "charge_price_kr": round(charge_price, 2) if charge_price is not None else None,
                    "duration_minutes": round(duration_s / 60),
                    "avg_power_w": round(session.avg_power_w, 1),
                    "estimated_soc_added_pct": (
                        round(session.estimated_soc_added_pct, 1)
                        if session.estimated_soc_added_pct is not None
                        else None
                    ),
                    "started_at": session.started_at,
                    "ended_at": session.ended_at,
                    "cost_method": session.cost_method,
                },
            )
            _LOGGER.info("Session completed and persisted: id=%s", session.id)
        else:
            _LOGGER.info(
                "Micro-session discarded: id=%s duration=%ds energy=%.3f kWh",
                session.id,
                duration_s,
                session.energy_kwh,
            )

        # Reset to IDLE
        self._active_session = None
        self._guest_pricing = None
        self._last_energy_kwh = 0.0
        self._last_power_w = 0.0
        self._hour_energy_snapshot = 0.0
        self._hour_start_time = ""
        self._state = SessionEngineState.IDLE
        self._dispatch_update()

    def _calculate_charge_price(self, session: Session) -> float | None:
        """Calculate what the guest pays for the current session energy/cost.

        Fixed: energy_kwh × price_per_kwh
        Markup: cost_total_kr × markup_factor

        Returns None if no guest pricing is configured.
        """
        if self._guest_pricing is None:
            return None
        if self._guest_pricing.method == "fixed" and self._guest_pricing.price_per_kwh is not None:
            return round(session.energy_kwh * self._guest_pricing.price_per_kwh, 2)
        if self._guest_pricing.method == "markup" and self._guest_pricing.markup_factor is not None:
            return round(session.cost_total_kr * self._guest_pricing.markup_factor, 2)
        return None

    def _dispatch_update(self) -> None:
        """Send dispatcher signal to notify sensor entities of state change."""
        signal = SIGNAL_SESSION_UPDATE.format(self._entry.entry_id)
        async_dispatcher_send(self._hass, signal)

    def get_active_session_dict(self) -> dict | None:
        """Return the active session as a dict for periodic persistence, or None."""
        if self._active_session is None:
            return None
        return self._active_session.to_dict()
