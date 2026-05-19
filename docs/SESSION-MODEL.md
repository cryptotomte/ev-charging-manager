# Session Model — EV Charging Manager

> Describes the plug-anchored session lifecycle introduced in PR-22 (v0.3.0).

---

## Overview

Starting with v0.3.0, sessions for **go-e Gemini** chargers are managed by the
`PlugAnchoredSessionEngine`.  The `plug` entity (a binary sensor reporting
cable presence) is the primary boundary signal, validated by the `cable_lock`
entity.  This eliminates the phantom sessions and race conditions that
plagued the legacy `car_status`-based engine.

---

## Session Lifecycle

```
IDLE
  │  plug=on
  ▼
TRACKING ─────── plug=off + cable_lock=Unlocked ──► session end (immediate)
  │                                                   (energy > 50 Wh: stored;
  │                                                    energy < 50 Wh: discarded)
  │  plug=off + cable_lock ≠ Unlocked
  │  (transient disconnect)
  ▼
TRACKING (grace timer running)
  │  cable_lock=Unlocked before grace expires ──────► session end
  │
  │  plug=on before grace expires ──────────────────► session continues (same session)
  │
  │  grace timer expires (cable_lock ≠ Unlocked) ───► session force-ended, data_gap=True
```

### Status Sub-States

While in `TRACKING`, the engine reports one of four human-readable sub-states:

| Sub-state   | Condition |
|-------------|-----------|
| `waiting`   | Cable in, no energy flowing |
| `charging`  | Power > 0 W |
| `charged`   | Power was > 0, now ≤ 0 (BMS paused or full) |
| `idle`      | Engine is in IDLE state (no session) |

These are exposed through `StatusSensor` and the `get_status_sub_state()` method.

---

## Charging Windows

Within a single session, the vehicle may start and stop charging multiple
times (e.g. BMS balancing, scheduled charging).  Each charging interval is
tracked as a **charging window** by `ChargingWindowTracker`.

- `charging_duration_s` = sum of all window durations
- `connection_duration_s` = total wall-clock time from plug-in to unplug
- `charging_window_count` = number of discrete charging intervals

`avg_power_w` is computed from `charging_duration_s` (not `connection_duration_s`).

---

## Micro-Filter

Short or trivial sessions are discarded before storage:

| Criterion | Threshold | Configurable? |
|-----------|-----------|---------------|
| Connection time | < 60 s | Yes — `min_session_duration_s` in options |
| Energy delivered | < 50 Wh | Yes — `min_session_energy_wh` in options |

Discarded sessions are logged (`MICRO_FILTER` category) but not stored.

---

## Transient Disconnect (Grace Timer)

If the plug sensor reports `off` but the cable lock is **not** `Unlocked`,
the engine interprets this as a transient disconnect (brief power loss, HA
entity glitch) rather than a genuine unplug.

- A grace timer starts (`disconnect_grace_min`, default 10 min).
- If the plug comes back `on` within the grace period, the **same session**
  continues; no gap is recorded.
- If the cable lock transitions to `Unlocked` within the grace period, the
  session ends immediately.
- If the grace timer expires without recovery, the session is force-ended with
  `data_gap=True`.

---

## Charger Outage (FR-028)

When **all** charger entities simultaneously go to `unavailable`, this is
treated as a charger power outage — **not** as an unplug signal.  The grace
timer is **not** started.

- The session remains active indefinitely during the outage.
- On recovery: if `plug=on`, the session continues; if `plug=off + cable_lock=Unlocked`,
  the session ends normally.

---

## Session Store Schema (v1.2)

Completed sessions are stored in `ev_charging_manager_sessions.json` with the
following fields:

| Field | Type | Description |
|-------|------|-------------|
| `id` | str | UUID |
| `started_at` | str | ISO-8601 UTC timestamp when session created |
| `ended_at` | str \| None | ISO-8601 UTC timestamp when session ended |
| `connected_at` | str | ISO-8601 UTC timestamp of first plug-in |
| `disconnected_at` | str \| None | ISO-8601 UTC timestamp of unplug |
| `connection_duration_s` | int | Wall-clock seconds from connected_at to disconnected_at |
| `charging_duration_s` | int | Sum of all charging window durations (seconds) |
| `charging_window_count` | int | Number of discrete charging intervals |
| `energy_kwh` | float | Net energy delivered (kWh) |
| `avg_power_w` | float \| None | Average power during charging windows (W) |
| `user_id` | str \| None | Subentry ID of identified user |
| `user_name` | str \| None | Display name of identified user |
| `user_type` | str \| None | `"regular"` or `"guest"` |
| `vehicle_id` | str \| None | Subentry ID of identified vehicle |
| `charge_price_total_kr` | float \| None | Calculated charge cost |
| `charge_price_method` | str \| None | `"static"`, `"spot"`, or `"fixed"` (guest) |
| `data_gap` | bool | True if a sensor gap or transient disconnect occurred |
| `blocked` | bool | True if session was blocked by unmapped RFID policy |
| `reconstructed` | bool | True if session was reconstructed after HA restart |

---

## Engine Selection

The engine used depends on the charger profile set during initial setup:

| Profile | Engine |
|---------|--------|
| `goe_gemini` | `PlugAnchoredSessionEngine` (v0.3.0+) |
| `generic` | `SessionEngine` (legacy, car_status-based) |

Engine selection occurs in `__init__.py` during `async_setup_entry`.

---

## Advanced Options

Three new options control `PlugAnchoredSessionEngine` timing:

| Option | Default | Range | Description |
|--------|---------|-------|-------------|
| `charging_idle_timeout_min` | 5 min | 3–30 min | Inactivity timeout before window close |
| `disconnect_grace_min` | 10 min | 5–30 min | Grace period for transient disconnects |
| `block_unmapped_rfid` | True | boolean | Block (and flag) sessions from unmapped RFID cards |

Configure via **Settings > Devices & Services > EV Charging Manager > Configure**.
