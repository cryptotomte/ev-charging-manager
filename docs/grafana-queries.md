# Grafana Query Reference

This document describes analytical queries you can build in Grafana using data exported from EV Charging Manager via the InfluxDB automation template.

## Data Flows

### Flow A: Real-time Sensor Logging (User Responsibility)

Configure the Home Assistant InfluxDB integration to log these sensors for real-time analysis:

| Analysis | Sensor | Unit |
|----------|--------|------|
| Power curve during session | ev_charging_manager_session_power | W |
| Charger power (raw) | goe_{serial}_nrg_11 | W |
| Energy counter consistency | goe_{serial}_eto vs ev_charging_manager_session_energy | kWh |

This is independent of EV Charging Manager -- configure it in your HA InfluxDB integration settings.

### Flow B: Session Data Export (Automation Template)

The automation template (`automations/ev_charging_manager_influxdb_export.yaml`) writes completed session data to the `ev_charging_sessions` measurement. This is the primary data source for the queries below.

## Session Data Queries

### Cost per User per Month

Track charging costs broken down by user over time.

```
SELECT SUM("cost_kr") FROM "ev_charging_sessions"
WHERE $timeFilter
GROUP BY "user_name", time(30d)
```

### Energy per User per Month

Track energy consumption per user.

```
SELECT SUM("energy_kwh") FROM "ev_charging_sessions"
WHERE $timeFilter
GROUP BY "user_name", time(30d)
```

### Sessions per Week

Count charging sessions per user per week.

```
SELECT COUNT("energy_kwh") FROM "ev_charging_sessions"
WHERE $timeFilter
GROUP BY "user_name", time(7d)
```

### Average Session Duration per User

Compare average charging time across users.

```
SELECT MEAN("duration_minutes") FROM "ev_charging_sessions"
WHERE $timeFilter
GROUP BY "user_name"
```

### Guest Billing Total

Calculate total guest charge revenue.

```
SELECT SUM("charge_price_kr") FROM "ev_charging_sessions"
WHERE "user_type" = 'guest' AND $timeFilter
```

### Sessions with Data Gaps

Find sessions where data quality issues were detected.

```
SELECT * FROM "ev_charging_sessions"
WHERE "data_gap" = true AND $timeFilter
```

### Static vs Spot Pricing Breakdown

Compare how many sessions used each pricing method.

```
SELECT COUNT(*) FROM "ev_charging_sessions"
WHERE $timeFilter
GROUP BY "cost_method", time(30d)
```

## Omitted Fields

The following fields from the original PRD measurement schema are not currently available in the session data:

- **max_power_w** -- Maximum power during session. Not tracked by SessionEngine. Can be derived from Flow A sensor data if logged.
- **phases_used** -- Number of charging phases used. Not detected at runtime.

These may be added in a future update.

## Tips

- Use Grafana's variable feature to create a `$user` dropdown from the `user_name` tag.
- Set the InfluxDB data source time precision to "seconds" for accurate session timestamps.
- The `started_at` timestamp is used as the measurement time -- sessions appear at their start time, not end time.
