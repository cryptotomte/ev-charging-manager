"""Constants for EV Charging Manager."""

from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "ev_charging_manager"

# Configuration keys
CONF_CHARGER_PROFILE = "charger_profile"
CONF_CAR_STATUS_ENTITY = "car_status_entity"
CONF_CAR_STATUS_CHARGING_VALUE = "car_status_charging_value"
CONF_ENERGY_ENTITY = "energy_entity"
CONF_ENERGY_UNIT = "energy_unit"
CONF_POWER_ENTITY = "power_entity"
CONF_RFID_ENTITY = "rfid_entity"
CONF_TOTAL_ENERGY_ENTITY = "total_energy_entity"
CONF_RFID_UID_ENTITY = "rfid_uid_entity"
CONF_CHARGER_NAME = "charger_name"
CONF_CHARGER_HOST = "charger_host"
CONF_CHARGER_SERIAL = "charger_serial"
CONF_PRICING_MODE = "pricing_mode"
CONF_STATIC_PRICE_KWH = "static_price_kwh"
CONF_SPOT_PRICE_ENTITY = "spot_price_entity"
CONF_SPOT_ADDITIONAL_COST_KWH = "spot_additional_cost_kwh"
CONF_SPOT_VAT_MULTIPLIER = "spot_vat_multiplier"
CONF_SPOT_FALLBACK_PRICE_KWH = "spot_fallback_price_kwh"

# Subentry configuration keys
CONF_VEHICLE_NAME = "name"
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_USABLE_BATTERY_KWH = "usable_battery_kwh"
CONF_CHARGING_PHASES = "charging_phases"
CONF_CHARGING_EFFICIENCY = "charging_efficiency"
CONF_MAX_CHARGING_POWER_KW = "max_charging_power_kw"
CONF_USER_NAME = "name"
CONF_USER_TYPE = "type"
CONF_GUEST_PRICING_METHOD = "guest_pricing_method"
CONF_PRICE_PER_KWH = "price_per_kwh"
CONF_MARKUP_FACTOR = "markup_factor"
CONF_CARD_INDEX = "card_index"
CONF_USER_ID = "user_id"
CONF_VEHICLE_ID = "vehicle_id"

# Subentry types
SUBENTRY_TYPE_VEHICLE = "vehicle"
SUBENTRY_TYPE_USER = "user"
SUBENTRY_TYPE_RFID_MAPPING = "rfid_mapping"

# Debug logging configuration keys (PR-010)
CONF_DEBUG_LOGGING = "debug_logging"

# Debug logging defaults (PR-010)
DEFAULT_DEBUG_LOGGING = False

# Debug logger buffered-I/O tuning (PR-28, FR-009) — not user-configurable.
# Flush when the buffer reaches this many lines ...
DEBUG_LOG_FLUSH_LINES = 50
# ... or this many seconds after the first buffered line, whichever comes first.
DEBUG_LOG_FLUSH_SECONDS = 5
# Rotate the active log file to a single .1 generation past this size.
DEBUG_LOG_MAX_BYTES = 5 * 1024 * 1024
# Hard buffer cap while the disk is unwritable (10 × flush lines, drop-oldest).
DEBUG_LOG_BUFFER_CAP = 500

# Default values
DEFAULT_CHARGER_NAME = "EV Charger"
DEFAULT_ENERGY_UNIT = "Wh"
DEFAULT_PRICING_MODE = "static"
DEFAULT_STATIC_PRICE_KWH = 2.50
DEFAULT_SPOT_ADDITIONAL_COST_KWH = 0.85
DEFAULT_SPOT_VAT_MULTIPLIER = 1.25
DEFAULT_SPOT_FALLBACK_PRICE_KWH = 2.50
DEFAULT_CAR_STATUS_CHARGING_VALUE = "Charging"
DEFAULT_CHARGING_EFFICIENCY = 0.90

# ConfigStore settings
STORE_KEY = "ev_charging_manager_config"
STORE_VERSION = 1


# Session engine state
class SessionEngineState(StrEnum):
    """States for the session engine state machine."""

    IDLE = "idle"
    TRACKING = "tracking"
    COMPLETING = "completing"


class SessionSubState(StrEnum):
    """Charging session sub-state — derived from engine state, plug, trx, and window state."""

    IDLE = "idle"  # no active session
    WAITING_FOR_PLUG = "waiting_for_plug"  # Blip received, no cable yet
    WAITING_FOR_RFID = "waiting_for_rfid"  # Cable in, no blip yet
    INITIALIZING = "initializing"  # Active session, no charging window opened yet
    CHARGING = "charging"  # active session, charging window currently open
    CHARGED = "charged"  # active session, all known windows closed


# Session options configuration keys
CONF_MIN_SESSION_DURATION_S = "min_session_duration_s"
CONF_MIN_SESSION_ENERGY_WH = "min_session_energy_wh"
CONF_PERSISTENCE_INTERVAL_S = "persistence_interval_s"
CONF_MAX_STORED_SESSIONS = "max_stored_sessions"

# Session options defaults
DEFAULT_MIN_SESSION_DURATION_S = 60
DEFAULT_MIN_SESSION_ENERGY_WH = 50
DEFAULT_PERSISTENCE_INTERVAL_S = 300
DEFAULT_MAX_STORED_SESSIONS = 1000

# Promotion thresholds for stuck-gate scenarios (PR-19 / spec 014).
# When the balancing-cycle gate is engaged but objective evidence shows
# the car is actually charging (sustained interval or significant
# energy delivered), the engine "promotes" the situation to a new
# tracked session. These defaults are derived from forensic data on
# the 2026-04-26 production incident.
DEFAULT_PROMOTE_DURATION_S = 300  # 5 minutes
DEFAULT_PROMOTE_ENERGY_KWH = 0.5  # 0.5 kWh

# Session store settings
SESSION_STORE_KEY = "ev_charging_manager_sessions"
SESSION_STORE_VERSION = 1

# Stats store settings
STATS_STORE_KEY = "ev_charging_manager_stats"
STATS_STORE_VERSION = 1

# Dispatcher signal for stats sensor updates (format with entry_id)
SIGNAL_STATS_UPDATE = "ev_charging_manager_stats_update_{}"

# Session lifecycle events
EVENT_SESSION_STARTED = "ev_charging_manager_session_started"
EVENT_SESSION_COMPLETED = "ev_charging_manager_session_completed"

# Dispatcher signal for sensor updates (format with entry_id)
SIGNAL_SESSION_UPDATE = "ev_charging_manager_session_update_{}"

# Platforms to forward to.
# Note (PR-22 revision 2026-05-19): Platform.SWITCH was previously included for the
# unknown-RFID active-blocking switch, but Story 07 has been reworked into a passive
# notification model (Constitution §I observer-only). No Switch entity is exposed.
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]

# Cross-validation: total energy counter entity (PR-07)
CONF_ETO_ENTITY = "eto_entity"

# Observation entity slots: four optional charger-signal entities stored
# in entry.options.  Auto-populated for goe_gemini profile; None on generic.
CONF_PLUG_ENTITY = "plug_entity"
CONF_CABLE_LOCK_ENTITY = "cable_lock_entity"
CONF_MODEL_STATUS_ENTITY = "model_status_entity"
CONF_ERROR_ENTITY = "error_entity"


# Unknown session diagnostic reason codes (PR-07)
class UnknownReason(StrEnum):
    """Diagnostic reason why a session was created with user=Unknown.

    Used by the session engine to populate engine.last_unknown_reason for
    debugging/diagnostics. StrEnum so that == "string_value" comparisons
    in tests and existing code continue to work unchanged.
    """

    TRX_NULL = "trx_was_null"
    TRX_ZERO = "trx_was_zero"
    RFID_INACTIVE = "rfid_inactive"
    RFID_UNMAPPED = "rfid_unmapped"
    RFID_TYPE_ERROR = "rfid_type_error"
    # PR-24: open-access inference — power flow started without any RFID blip (FR-003)
    OPEN_ACCESS_INFERRED = "open_access_inferred"


# Notification ID for recurring unknown-sessions alert (formatted with entry_id)
NOTIFICATION_ID_UNKNOWN_SESSIONS = "ev_charging_manager_unknown_sessions_{}"

# Threshold for proactive unknown-session notifications (PR-07)
UNKNOWN_SESSION_THRESHOLD = 3
UNKNOWN_SESSION_WINDOW_DAYS = 7

# RFID discovery constants (PR-08)
CONF_CARD_UID = "card_uid"
DISCOVERY_TIMEOUT = 5  # seconds
MAX_CARD_SLOTS = 10
MAX_ENERGY_KWH = 1_000_000

# Provider identifiers
PROVIDER_GOE = "goe"

# go-e API filter strings
GOE_FILTER_FWV = "fwv"
GOE_FILTER_CARDS = "cards"
GOE_FILTER_LRI_RDE = "lri,rde"

# Flat key suffixes for go-e FW >=60 format
GOE_FLAT_KEY_SUFFIX_NAME = "n"
GOE_FLAT_KEY_SUFFIX_ENERGY = "e"
GOE_FLAT_KEY_SUFFIX_INSTALLED = "i"

# Firmware version threshold for flat key format
GOE_FLAT_KEYS_FW_THRESHOLD = 60

# ---------------------------------------------------------------------------
# PR-22: Session boundary redesign — new configuration keys and debug categories
# ---------------------------------------------------------------------------

# New advanced options for the plug-anchored session model (OptionsFlowHandler)
CONF_CHARGING_IDLE_TIMEOUT_MIN = "charging_idle_timeout_min"
CONF_DISCONNECT_GRACE_MIN = "disconnect_grace_min"

# HEARTBEAT log timer — emit a HEARTBEAT debug-log line at this cadence
# during TRACKING state (FR-012, IC-3). Set to 0 to disable.
CONF_HEARTBEAT_LOG_INTERVAL_MIN = "heartbeat_log_interval_min"
DEFAULT_HEARTBEAT_LOG_INTERVAL_MIN = 5
HEARTBEAT_LOG_INTERVAL_MIN_MIN = 0
HEARTBEAT_LOG_INTERVAL_MIN_MAX = 30

# UI dispatch timer — dispatch a session-update signal at this cadence
# during TRACKING state to unblock live sensor ticks (FR-013, IC-3).
# Set to 0 to disable (sensors only re-render on engine state changes).
CONF_UI_DISPATCH_INTERVAL_S = "ui_dispatch_interval_s"
DEFAULT_UI_DISPATCH_INTERVAL_S = 60
UI_DISPATCH_INTERVAL_S_MIN = 0
UI_DISPATCH_INTERVAL_S_ACTIVE_MIN = 10  # Minimum when not disabled (0 = disable sentinel)
UI_DISPATCH_INTERVAL_S_MAX = 300

# PR-22 revision 2026-05-19: passive notification replaces active blocking for Story 07.
# Default to True so existing installs surface attribution gaps; users may disable to
# rely solely on the EVENT_UNKNOWN_RFID_DETECTED event for their own automations.
CONF_NOTIFY_UNMAPPED_RFID = "notify_unmapped_rfid"

# Defaults and ranges for the new advanced options
DEFAULT_CHARGING_IDLE_TIMEOUT_MIN = 5  # minutes; range 3–30
DEFAULT_DISCONNECT_GRACE_MIN = 10  # minutes; range 5–30
DEFAULT_NOTIFY_UNMAPPED_RFID = True

MIN_CHARGING_IDLE_TIMEOUT_MIN = 3
MAX_CHARGING_IDLE_TIMEOUT_MIN = 30
MIN_DISCONNECT_GRACE_MIN = 5
MAX_DISCONNECT_GRACE_MIN = 30

# PR-27 (023-recovery-hardening): epsilon for energy counter-reset detection.
# Shared by restart recovery (FR-002) and live mid-session detection (FR-015).
# 10 Wh — well above float/rounding jitter, far below any genuine reset.
ENERGY_RESET_EPSILON_KWH = 0.01

# Maximum time to wait for the plug entity to become available before giving up
# on deferred restart recovery (HIGH-1 fix). If the plug entity never reports a
# valid state within this window (e.g. user misconfigured the entity id, the
# charger is permanently offline), the engine notifies the user via a persistent
# notification and force-completes the snapshot as a reconstructed session.
DEFERRED_RECOVERY_TIMEOUT_MIN = 60  # minutes

# New debug log categories (FR-034) — used as the first argument to DebugLogger.log().
# The DebugLogger accepts arbitrary category strings; these constants ensure
# consistent spelling across session_engine_v2.py and tests.
DEBUG_CAT_CHARGING_WINDOW_OPEN = "CHARGING_WINDOW_OPEN"
DEBUG_CAT_CHARGING_WINDOW_CLOSE = "CHARGING_WINDOW_CLOSE"
DEBUG_CAT_DISCONNECT_DETECTED = "DISCONNECT_DETECTED"
DEBUG_CAT_DISCONNECT_RESOLVED = "DISCONNECT_RESOLVED"
DEBUG_CAT_HA_RESTART_DETECTED = "HA_RESTART_DETECTED"
DEBUG_CAT_SESSION_RESUMED = "SESSION_RESUMED"
DEBUG_CAT_SESSION_FORCE_ENDED_BY_RESTART = "SESSION_FORCE_ENDED_BY_RESTART"
DEBUG_CAT_SESSION_FORCE_ENDED_BY_GRACE_TIMEOUT = "SESSION_FORCE_ENDED_BY_GRACE_TIMEOUT"
# PR-25 (021-cable-lock-race): a pending transient-disconnect grace timer is
# resolved by cable_lock→Unlocked arriving 0–3 s after plug→off, confirming a
# genuine unplug. Distinguishes this completion path in the debug log from a
# synchronous plug-off completion (SESSION_STOP) and a grace-timeout force-end.
DEBUG_CAT_SESSION_ENDED_BY_CABLE_UNLOCK = "SESSION_ENDED_BY_CABLE_UNLOCK"
DEBUG_CAT_CHARGER_OFFLINE = "CHARGER_OFFLINE"
DEBUG_CAT_CHARGER_BACK_ONLINE = "CHARGER_BACK_ONLINE"
DEBUG_CAT_TRX_MIDSESSION = "TRX_MIDSESSION"
# PR-22 revision 2026-05-19: replaced RFID_BLOCKED / RFID_BLOCK_FAILED / RFID_BLOCK_RELEASED
# with passive-notification categories — no HTTP control of the charger is performed.
DEBUG_CAT_RFID_UNMAPPED_NOTIFIED = "RFID_UNMAPPED_NOTIFIED"
DEBUG_CAT_RFID_UNMAPPED_NOTIFY_FAILED = "RFID_UNMAPPED_NOTIFY_FAILED"

# HIGH-1: deferred restart recovery timed out (plug entity never appeared).
DEBUG_CAT_RECOVERY_TIMEOUT = "RECOVERY_TIMEOUT"

# PR-23: periodic heartbeat during TRACKING state (FR-012, IC-3).
DEBUG_CAT_HEARTBEAT = "HEARTBEAT"

# PR-24: RFID wait lifecycle events (renamed from RFID_GRACE; event-driven model, no timer).
DEBUG_CAT_RFID_WAIT = "RFID_WAIT"

# PR-27 (023-recovery-hardening): data-quality events — currently the
# mid-session energy counter reset (FR-015 rebase).
DEBUG_CAT_DATA_GAP = "DATA_GAP"

# New HA events (PR-22)
EVENT_CHARGING_CHARGED = "ev_charging_charged"
EVENT_UNKNOWN_RFID_DETECTED = "ev_charging_unknown_rfid_detected"

# Dispatcher signal fired by ConfigStore when an RFID mapping is added so the
# unmapped-RFID notification dismisser can clear stale notifications (FR-022).
# Formatted with entry_id at registration time.
SIGNAL_RFID_MAPPING_ADDED = "ev_charging_manager_rfid_mapping_added_{}"

# Persistent notification ID prefix for unmapped RFID.
# Use as: NOTIFICATION_ID_UNKNOWN_RFID.format(rfid_index_or_null_str)
NOTIFICATION_ID_UNKNOWN_RFID = "ev_charging_manager_unmapped_rfid_{}"
