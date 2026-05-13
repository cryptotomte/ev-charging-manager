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
# - plug_sensor: Entity pattern for plug-connected binary sensor (PR-20, optional)
# - cable_lock_sensor: Entity pattern for cable-lock status sensor (PR-20, optional)
# - model_status_sensor: Entity pattern for charger model-status sensor (PR-20, optional)
# - error_sensor: Entity pattern for charger error sensor (PR-20, optional)

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
        "rfid_discovery": {
            "provider": "goe",
            "fw_detection_filter": "fwv",
            "cards_array_filter": "cards",
            "flat_keys_format": True,
        },
        "requires_charger_host": True,
        # PR-20 observation entity patterns
        "plug_sensor": "binary_sensor.goe_{serial}_car_0",
        "cable_lock_sensor": "sensor.goe_{serial}_cus_value",
        "model_status_sensor": "sensor.goe_{serial}_modelstatus_value",
        "error_sensor": "sensor.goe_{serial}_err_value",
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
        # PR-20 observation entity patterns (None = not auto-populated for generic)
        "plug_sensor": None,
        "cable_lock_sensor": None,
        "model_status_sensor": None,
        "error_sensor": None,
    },
}
