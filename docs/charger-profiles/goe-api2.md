# go-e Charger Setup Guide (Gemini / Gemini flex)

## Charger Model

This guide covers the **go-e Charger Gemini** and **go-e Charger Gemini flex** models.
These chargers are integrated with Home Assistant via the
[ha-goecharger-api2](https://github.com/marq24/ha-goecharger-api2) custom integration
by marq24, which communicates with the charger's local API v2.

## Network Prerequisites

- The charger must be connected to your local network via WiFi or Ethernet.
- A static IP address or stable hostname is recommended (e.g. `go-echarger_SERIAL.local`).
- **Local API v2 must be activated** in the go-e app: go to **Settings > Advanced > Local API v2** and enable it.

Without Local API v2 enabled, the HA integration cannot communicate with the charger.

## HA Integration Installation

1. Install **ha-goecharger-api2** from HACS: <https://github.com/marq24/ha-goecharger-api2>
2. Add the integration in Home Assistant and enter the charger's IP address or hostname.
3. **WebSocket mode is recommended** for real-time push updates. To enable it:
   - Open the go-e app.
   - Go to **Settings > Advanced > Local WebSocket password** and set a password.
   - Enter the same password when configuring the HA integration.
4. Polling mode works as a fallback but may result in slower or less reliable sensor updates.

## Required Entities

The ha-goecharger-api2 integration creates entities using the charger's serial number. The
following entities are used by EV Charging Manager:

| Function        | Entity Pattern                      | Example                          |
|-----------------|-------------------------------------|----------------------------------|
| Car status      | `sensor.goe_{serial}_car_value`     | `sensor.goe_123456_car_value`    |
| Session energy  | `sensor.goe_{serial}_wh`           | `sensor.goe_123456_wh`          |
| Power           | `sensor.goe_{serial}_nrg_11`       | `sensor.goe_123456_nrg_11`      |
| Total energy    | `sensor.goe_{serial}_eto`          | `sensor.goe_123456_eto`         |
| RFID transaction| `select.goe_{serial}_trx`          | `select.goe_123456_trx`         |

**Important notes:**

- **Session energy (wh):** Despite the name, the HA integration reports this value in **kWh**, not Wh. Use the value as-is without dividing by 1000.
- **Power (nrg\_11):** This is "Power total now" and is reported in **watts** (W).

## RFID Card Configuration

The go-e charger supports up to 10 RFID cards (slots indexed 0--9, where slot 1 = index 0).

### Programming cards

Program your RFID cards using the go-e app under the **Cards** section. Each card can be
assigned a name and linked to a specific energy limit if desired.

### Enabling RFID UID reporting

To allow EV Charging Manager to discover and identify RFID cards, you must enable the
`rde` (RFID data enable) flag on the charger. This setting is **not available in the
go-e app** and must be set via a direct API call:

```
http://CHARGER_HOST/api/set?rde=true
```

Replace `CHARGER_HOST` with your charger's IP address or hostname (e.g.
`go-echarger_123456.local`).

### Auto-discovery

EV Charging Manager auto-discovers programmed cards via the charger's local API. Once
`rde=true` is set and cards are programmed, they will appear in the integration's RFID
mapping configuration.

## EV Charging Manager Config Flow

When adding EV Charging Manager, select **"go-e Charger (Gemini / Gemini flex)"** as the
charger profile. The config flow will pre-fill entity patterns based on your charger's
serial number.

The setup steps are:

1. **Enter charger serial number** -- used to generate the correct entity IDs.
2. **Verify pre-filled entity IDs** -- confirm that the auto-generated entity IDs match your installation. Adjust if your entity naming differs.
3. **Enter charger host/IP** -- required for direct API communication (RFID discovery, card management).
4. **Configure pricing** -- choose between a static price per kWh or a spot pricing sensor for dynamic cost calculation.

## Known Quirks

- **trx is a `select`, not a `sensor`:** The RFID transaction entity (`trx`) is exposed as a `select` entity in HA, not a sensor. Its options are: `null`, `0`, `1`, `2`, `10`.
- **Car status values are strings:** The `car_value` sensor reports string states: `"Idle"`, `"Charging"`, `"WaitCar"`, `"Complete"` -- not numeric codes.
- **Power sensor latency:** In WebSocket mode, power sensor updates (`nrg_11`) may lag 1--2 seconds behind actual charger state changes.
- **Sensors not updating:** If sensors stop updating or never populate, verify that:
  - Local API v2 is enabled in the go-e app.
  - The WebSocket password is set and matches between the go-e app and the HA integration.
  - The charger is reachable on the network from the HA host.
