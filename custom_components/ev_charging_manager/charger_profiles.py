"""Known charger profiles for EV Charging Manager."""

from __future__ import annotations

from typing import Any

# Profile dict structure:
# - name: Display name shown in UI
# - car_status_sensor: Entity pattern for car status (uses {serial} token)
# - car_status_charging_value: Sensor value that means "currently charging"
# - session_energy_sensor: Entity pattern for session energy
# - session_energy_unit: Default energy unit ("Wh" or "kWh")
# - power_sensor: Entity pattern for power
# - total_energy_sensor: Entity pattern for total energy (optional)
# - rfid_sensor: Entity pattern for RFID/transaction sensor (optional)
# - rfid_last_uid_sensor: Entity pattern for last RFID UID (optional)
# - rfid_discovery: Placeholder for PR-08 RFID discovery config
# - requires_charger_host: Whether the charger host/IP field is required

CHARGER_PROFILES: dict[str, dict[str, Any]] = {
    "goe_gemini": {
        "name": "go-e Charger (Gemini / Gemini flex)",
        "car_status_sensor": "sensor.goe_{serial}_car_value",
        "car_status_charging_value": "Charging",
        "session_energy_sensor": "sensor.goe_{serial}_wh",
        "session_energy_unit": "kWh",
        "power_sensor": "sensor.goe_{serial}_nrg_11",
        "total_energy_sensor": "sensor.goe_{serial}_eto",
        "rfid_sensor": "select.goe_{serial}_trx",
        "rfid_last_uid_sensor": None,
        "rfid_discovery": None,  # Placeholder — implemented in PR-08
        "requires_charger_host": True,
    },
    "easee_home": {
        "name": "Easee Home / Charge",
        "car_status_sensor": "sensor.easee_status",
        "car_status_charging_value": "charging",
        "session_energy_sensor": "sensor.easee_session_energy",
        "session_energy_unit": "kWh",
        "power_sensor": "sensor.easee_power",
        "total_energy_sensor": None,
        "rfid_sensor": None,
        "rfid_last_uid_sensor": None,
        "rfid_discovery": None,
        "requires_charger_host": False,
    },
    "zaptec": {
        "name": "Zaptec Go / Pro",
        "car_status_sensor": None,
        "car_status_charging_value": None,
        "session_energy_sensor": None,
        "session_energy_unit": None,
        "power_sensor": None,
        "total_energy_sensor": None,
        "rfid_sensor": None,
        "rfid_last_uid_sensor": None,
        "rfid_discovery": None,
        "requires_charger_host": False,
    },
    "generic": {
        "name": "Other / Manual configuration",
        "car_status_sensor": None,
        "car_status_charging_value": None,
        "session_energy_sensor": None,
        "session_energy_unit": None,
        "power_sensor": None,
        "total_energy_sensor": None,
        "rfid_sensor": None,
        "rfid_last_uid_sensor": None,
        "rfid_discovery": None,
        "requires_charger_host": False,
    },
}
