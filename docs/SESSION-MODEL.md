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
  │  cable_lock=Unlocked (plug still off) ──────────► session end (PR-25: confirmed unplug,
  │                                                    disconnect time = plug-off moment)
  │
  │  plug=on before grace expires ──────────────────► session continues (same session)
  │
  │  grace timer expires (cable_lock ≠ Unlocked) ───► session force-ended, data_gap=True
```

### Status Sub-States

The Status sensor exposes one of six human-readable sub-states derived from engine state, plug state, trx state, and window state.

| Sub-state | Engine state | Plug | trx | Description |
|---|---|---|---|---|
| `idle` | IDLE | off | null/0 | No session, no blip in flight |
| `waiting_for_plug` | IDLE | off | non-null + non-zero | Blip received, waiting for cable insertion |
| `waiting_for_rfid` | TRACKING | on | null | Cable in, waiting for RFID blip or power flow |
| `initializing` | TRACKING | on | non-null | Session active, no charging window yet |
| `charging` | TRACKING | on | non-null | Charging window currently open |
| `charged` | TRACKING | on | non-null | Window(s) closed, cable still in |

These are exposed through `StatusSensor` and the `get_status_sub_state()` method. See `specs/020-rfid-wait-model/data-model.md §E2` for the binding state-mapping table.

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
- `data_gap` is provisionally set to `True` while the grace timer runs.

A pending grace timer has **three** possible resolutions:

1. **Resume** — the plug returns to `on` within the grace period: the **same
   session** continues, the grace timer is cancelled, and the provisional
   `data_gap` flag stays as it is (the disconnect really was transient).
2. **Confirmed unplug (PR-25, `cable_lock→Unlocked`)** — the cable lock
   transitions to `Unlocked` **while the plug is still `off`**: this confirms a
   genuine unplug and the session ends immediately (see below).
3. **Force-end** — the grace timer expires with no recovery and no `Unlocked`
   confirmation: the session is force-ended with `data_gap=True`
   (`SESSION_FORCE_ENDED_BY_GRACE_TIMEOUT`).

### Confirmed unplug via lagging `cable_lock→Unlocked` (PR-25)

On the go-e Gemini, a genuine unplug fires `plug: on→off` **0–3 s before**
`cable_lock: Locked→Unlocked`. At the plug-off instant `cable_lock` therefore
still reads `Locked`, so the synchronous check in `_handle_plug_off` classifies
the unplug as a transient disconnect and starts the grace timer. The lagging
`cable_lock→Unlocked` event re-evaluates that decision.

When **all** of the following hold, the engine treats the `cable_lock→Unlocked`
transition as a confirmed genuine unplug and completes the session immediately
(`SESSION_ENDED_BY_CABLE_UNLOCK`), cancelling the grace timer so it cannot also
fire:

- the new cable-lock value is `Unlocked`,
- a disconnect grace timer is pending,
- there is an active session,
- the plug is currently `off` (a car connected — plug `on` — is **not** a
  confirmation; the session continues),
- the charger is not offline (the charger-outage path stays authoritative).

Two values differ from a synchronous plug-off completion:

- **Disconnect time** is recorded as the original `plug→off` timestamp (the
  moment the car was physically removed), **not** the later confirmation time,
  so `connection_duration_s` is not inflated by the 0–3 s race gap.
- **`data_gap`** is reverted to its pre-disconnect value (the provisional
  `True` set at plug-off is cleared), **unless** a genuine gap was independently
  recorded earlier in the session (e.g. a sensor went `unavailable` mid-charge),
  in which case it is preserved.

This is the mechanism that lets a second driver who plugs in shortly after the
first unplugs get a **fresh, correctly-attributed session** instead of having
their charge folded into the previous driver's session.

---

## Charger Outage (FR-028)

When **all** charger entities simultaneously go to `unavailable`, this is
treated as a charger power outage — **not** as an unplug signal.  The grace
timer is **not** started.

- The session remains active indefinitely during the outage.
- On recovery: if `plug=on`, the session continues; if `plug=off + cable_lock=Unlocked`,
  the session ends normally.

---

## HA Restart Recovery

When Home Assistant restarts while a `PlugAnchoredSessionEngine` session is
active, the deferred-recovery path detects that a session was in progress
(via the persisted snapshot's `reconstructed`/`data_gap` fields and the
current plug entity state) and resumes it.  The recovered session is marked
`data_gap=True` and `reconstructed=True` in the session store.

### Restart recovery — synthetic window injection

When Home Assistant restarts mid-session, the deferred-recovery path detects a pre-restart open charging window (via `session.charging_started_at`) and injects a synthetic closed window into the tracker before any subsequent window opens when charging power returns. This ensures `session.charging_duration_s` correctly accounts for the pre-restart charging period. See `specs/019-session-quality-fixes/spec.md` IC-6 for the binding decisions.

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

These options control `PlugAnchoredSessionEngine` timing (two existing in v0.3.0, two new in v0.4.0):

| Option | Default | Range | Description |
|--------|---------|-------|-------------|
| `charging_idle_timeout_min` | 5 min | 3–30 min | Inactivity timeout before window close |
| `disconnect_grace_min` | 10 min | 5–30 min | Grace period for transient disconnects |
| `heartbeat_log_interval_min` | 5 min | 0 (disable) or 1–30 min | Minutes between HEARTBEAT debug-log entries during active charging |
| `ui_dispatch_interval_s` | 60 s | 0 (disable) or 10–300 s | Seconds between live UI updates for session-derived sensors during active charging |

Configure via **Settings > Devices & Services > EV Charging Manager > Configure**.
