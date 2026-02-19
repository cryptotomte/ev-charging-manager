"""Constants for EV Charging Manager."""

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
CONF_PRICING_MODE = "pricing_mode"
CONF_STATIC_PRICE_KWH = "static_price_kwh"

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

# Default values
DEFAULT_CHARGER_NAME = "EV Charger"
DEFAULT_ENERGY_UNIT = "Wh"
DEFAULT_PRICING_MODE = "static"
DEFAULT_STATIC_PRICE_KWH = 2.50
DEFAULT_CAR_STATUS_CHARGING_VALUE = 2
DEFAULT_CHARGING_EFFICIENCY = 0.90

# ConfigStore settings
STORE_KEY = "ev_charging_manager_config"
STORE_VERSION = 1
