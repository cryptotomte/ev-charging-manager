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

# Default values
DEFAULT_CHARGER_NAME = "EV Charger"
DEFAULT_ENERGY_UNIT = "Wh"
DEFAULT_PRICING_MODE = "static"
DEFAULT_STATIC_PRICE_KWH = 2.50
DEFAULT_CAR_STATUS_CHARGING_VALUE = 2
