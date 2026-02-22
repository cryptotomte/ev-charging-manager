# EV Charging Manager

Track, identify, and analyze EV charging sessions in Home Assistant.

A custom integration for Home Assistant (installable via [HACS](https://hacs.xyz)) that turns your charger's raw sensor data into meaningful per-user charging records. Designed for multi-driver households sharing a single charger.

**Key features:**

- RFID-based user identification
- Per-user energy and cost tracking
- Static and spot pricing support
- Guest charging with billing
- Session events for automations
- InfluxDB export for Grafana analysis
- Automatic RFID card discovery (go-e chargers)
- Session recovery after HA restart

Everything runs locally inside Home Assistant -- no cloud services required.

---

## Prerequisites

Before installing, make sure you have:

- [ ] An EV charger installed and connected to your local network
- [ ] Home Assistant **2025.3** or newer
- [ ] [HACS](https://hacs.xyz) installed
- [ ] A Home Assistant integration for your charger already set up (e.g. [ha-goecharger-api2](https://github.com/marq24/ha-goecharger-api2) for go-e chargers)

---

## Charger Setup

### go-e Chargers

See [docs/charger-profiles/goe-api2.md](docs/charger-profiles/goe-api2.md) for detailed setup instructions, including recommended WebSocket mode configuration and entity mapping.

### Other Chargers

Any charger that exposes the following through a Home Assistant integration can be used:

- [ ] **Car status sensor** -- a sensor entity that reports when the vehicle is charging (e.g. "Charging", "Connected", etc.)
- [ ] **Energy sensor** -- session energy in Wh or kWh
- [ ] **Power sensor** -- current charging power in W
- [ ] **RFID sensor** (optional) -- reports the RFID card index or UID used to authorize a session

If your charger brand is not yet documented, see [Charger Profiles (Community)](#charger-profiles-community) for how to contribute a profile.

---

## Installation

1. Open Home Assistant and navigate to **HACS** > **Integrations**.
2. Click the three-dot menu in the top right and select **Custom repositories**.
3. Enter `cryptotomte/ev-charging-manager` as the repository URL and select **Integration** as the category.
4. Click **Add**, then find **EV Charging Manager** in the HACS store and click **Install**.
5. Restart Home Assistant.

---

## Configuration

### Config Flow (Initial Setup)

1. Go to **Settings** > **Devices & Services** > **Add Integration**.
2. Search for **EV Charging Manager**.
3. Follow the setup wizard:

| Step | What you configure |
|---|---|
| **Charger profile** | Select your charger brand (e.g. go-e) or "Generic" |
| **Charger serial** | A unique name/serial for this charger instance |
| **Entity mapping** | Map the charger's HA entities (car status, energy, power, RFID) |
| **Pricing** | Choose static or spot pricing and enter rates |

After completing the wizard, the integration creates a device and begins monitoring for charging sessions.

### Managing Users

Users represent the people who charge at your location. Each user can have RFID cards mapped to them for automatic identification.

1. Go to **Settings** > **Devices & Services** > **Integrations**.
2. Find **EV Charging Manager** and click on your charger entry.
3. Click **Add User** (subentry).
4. Enter the user's name and type (resident or guest).

Guest users have additional billing options (custom price per kWh or markup factor).

### Managing Vehicles

Vehicles store battery specifications used for SoC estimation.

1. Go to **Settings** > **Devices & Services** > **Integrations**.
2. Find **EV Charging Manager** and click on your charger entry.
3. Click **Add Vehicle** (subentry).
4. Enter the vehicle name, battery capacity, usable capacity, and charging parameters.

### Managing RFID Mappings

RFID mappings connect a physical RFID card to a user (and optionally a vehicle).

**Automatic discovery (go-e chargers):** The integration reads the charger's card registry directly. When adding an RFID mapping, you select from discovered cards and assign them to a user and vehicle.

**Manual fallback (other chargers):** Enter the card index or UID manually based on your charger's documentation.

1. Go to **Settings** > **Devices & Services** > **Integrations**.
2. Find **EV Charging Manager** and click on your charger entry.
3. Click **Add RFID Mapping** (subentry).
4. Select or enter the card, then assign a user and optionally a vehicle.

---

## Who Does What? (Responsibility Matrix)

| What | Who |
|---|---|
| Session detection and tracking | Integration (automatic) |
| RFID user identification | Integration (automatic) |
| Energy and cost calculation | Integration (automatic) |
| Session events for automations | Integration (automatic) |
| Per-user statistics | Integration (automatic) |
| Session recovery after restart | Integration (automatic) |
| Charger hardware installation | User |
| Charger network connectivity | User |
| HA integration for charger | User |
| User / vehicle / RFID setup in config | User |
| Pricing configuration | User |
| InfluxDB setup and maintenance | User |
| Grafana dashboards | User |

In short: you set up the hardware and tell the integration who your users are. The integration handles everything else.

---

## Sensors

The integration creates the following sensor entities per charger:

### Session Sensors

| Sensor | Description |
|---|---|
| **Status** | Current engine state: `idle`, `tracking`, or `completing` |
| **Current User** | Name of the identified user (or "Unknown") |
| **Current Vehicle** | Name of the identified vehicle |
| **Session Energy** | Energy delivered in the current session (kWh) |
| **Session Power** | Current charging power (W) |
| **Session Duration** | Elapsed time of the current session (HH:MM:SS) |
| **Session Cost** | Accumulated cost of the current session |
| **Estimated SoC Added** | Estimated state-of-charge added to the battery (%) |

Session sensors become available when a charging session is active and return to unavailable when idle.

### Per-User Statistics Sensors

For each configured user (and a combined "Guest" set), the integration creates:

| Sensor | Description |
|---|---|
| **Total Energy** | Cumulative energy charged (kWh) |
| **Total Cost** | Cumulative charging cost |
| **Session Count** | Number of completed sessions |
| **Last Session** | Timestamp of the most recent session |
| **Average Energy** | Average energy per session (kWh) |

Statistics sensors include a `monthly_breakdown` attribute with per-month totals.

### Binary Sensor

| Sensor | Description |
|---|---|
| **Charging** | On when a session is actively tracking |

---

## Optional: InfluxDB + Grafana

There are two complementary approaches for long-term analysis. You can use either or both.

### Flow A: Real-time Sensor Logging

Configure Home Assistant's built-in [InfluxDB integration](https://www.home-assistant.io/integrations/influxdb/) to log the EV Charging Manager sensor entities. This gives you continuous time-series data (power curves, cost accumulation, etc.) directly in InfluxDB.

This is entirely managed through HA's InfluxDB integration configuration -- no special setup in EV Charging Manager is needed.

### Flow B: Session Export via Automation

For per-session summary records (one data point per completed charge), use the provided automation template:

1. Copy `automations/ev_charging_manager_influxdb_export.yaml` into your Home Assistant automations.
2. The automation triggers on `ev_charging_manager_session_completed` events and writes a summary record to the InfluxDB measurement `ev_charging_sessions`.

Each record includes: user, vehicle, energy (kWh), cost, duration, cost method, and timestamp.

See `docs/grafana-queries.md` for example Flux/InfluxQL queries to build dashboards.

---

## Troubleshooting

### Sessions Logged as "Unknown"

When a session completes without a recognized user, it is attributed to "Unknown". Common causes:

1. **No RFID mapping** -- the card used is not mapped to a user. Add an RFID Mapping subentry.
2. **trx sensor not updating** -- the charger's RFID/transaction sensor is not reporting values. Verify the entity in Developer Tools > States.
3. **Card not active on charger** -- some chargers require cards to be explicitly enabled. Check your charger's app or configuration.

The integration logs a diagnostic reason code for each unknown session. Check the Home Assistant logs for entries containing `unknown_reason` to pinpoint the cause.

### Cross-Validation Warnings

The integration compares session-tracked energy against the charger's total energy counter (if configured). A warning indicates a deviation that usually means sensor updates were missed during the session (e.g. due to network issues or charger disconnects).

- Verify the charger's network connection is stable.
- If using polling mode, consider switching to WebSocket mode for more reliable updates.

### HA Restart Recovery

The integration automatically recovers active sessions after a Home Assistant restart:

- The active session snapshot is persisted periodically (default: every 5 minutes).
- On startup, the engine restores the session if the same RFID card is still active.
- Very short sessions (< 60s) or sessions with negligible energy (< 50 Wh) may be discarded as micro-sessions.

Check the logs for "Recovered session" entries to confirm recovery occurred. If a session was lost, look for "Discarded micro-session" or "Session continuity mismatch" messages.

---

## Charger Profiles (Community)

Charger profiles define how the integration maps a specific charger brand's entities to the internal data model. Profiles for supported chargers are documented in [docs/charger-profiles/](docs/charger-profiles/).

To contribute a profile for a new charger brand:

1. Identify the HA integration and its entity naming conventions.
2. Document the entity mappings (car status, energy, power, RFID) in a markdown file.
3. Submit a pull request adding the profile to `docs/charger-profiles/`.

See [docs/charger-profiles/README.md](docs/charger-profiles/README.md) for the template and guidelines.

---

## License

This project is licensed under the [MIT License](LICENSE).
