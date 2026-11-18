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

After a restart or reload the sub-state is re-derived from the entity values: a cable already in without a card shows `waiting_for_rfid` again. These are exposed through `StatusSensor` and the `get_status_sub_state()` method. See `specs/020-rfid-wait-model/data-model.md §E2` for the binding state-mapping table.

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

## Restart, Reload and Crash Recovery

This section describes the `goe_gemini` profile. The `generic` profile uses the
legacy engine, where a restart or reload during a session can still lose or
split the session.

### What is saved while a session runs

The session in progress is kept on disk as the **active snapshot**: the last
record in the sessions store that has no `disconnected_at`/`ended_at`. Recovery
detects an interrupted session by that missing end time. Besides the session
fields, the snapshot holds a `resume_state` object with the values the session
started with (see the schema below).

The snapshot is written:

- when the session starts,
- when a charging window opens or closes,
- at every hour boundary in spot mode,
- when the plug goes off and the grace timer starts,
- every `persistence_interval_s` (default 300 s),
- when the entry is unloaded (a reload, for example after saving the options),
- when Home Assistant stops.

A reload and an orderly restart therefore keep everything up to the moment the
integration stopped. A crash or power loss loses at most what happened since the
last of these writes.

### What happens at start-up

1. **The plug entity has no value yet** (absent, `unavailable` or `unknown`).
   Recovery waits for it. While it waits, a plug-in starts nothing. If the plug
   is still missing after 60 minutes, the snapshot is completed with the larger
   of its own energy and the counter difference (the counter may have moved on)
   and a notification is shown.
2. **The plug is off**: the car left while Home Assistant was down. The session
   is completed with the larger of the snapshot's energy and the counter
   difference (the go-e counter keeps the connection's final value until the
   next plug-in). If the energy entity has no value yet, recovery waits for its
   first reading, at most 30 seconds, and stops waiting at once if the plug
   changes. Cost, guest charge price and SoC estimate are recomputed from that
   energy, and the end time is the start-up time.
3. **The plug is on.** If the RFID entity is absent or `unavailable`, recovery
   waits for it, at most 30 seconds; `unknown` and `null` mean no card. Then:
   - **The session started with a card and the charger shows a different
     card**: another person has plugged in. The old session is completed with
     its snapshot energy, and a new session starts for the present card and
     takes the whole counter of the new connection.
   - **Otherwise** (same card, no card shown, or a session that started without
     a card): the session is resumed with the price parameters and guest rate it
     started with. The present counter reading is fed through the normal energy
     handling, so a counter that restarted while Home Assistant was down keeps
     the energy already counted. Cost, guest charge price and SoC estimate are
     recomputed at once.

A cable that is already in when there is nothing to recover is handled like a
plug-in: the engine waits for a card (`waiting_for_rfid`), starts a session for
the card shown, or, when no card is shown and power is already flowing, starts
an open-access session at once. This applies at setup and whenever the plug
entity goes from no value to `on`. Such a session starts from the counter's
reading at that moment, as a live plug-in does: at a real plug-in the counter can
still show the previous connection for a moment, so it cannot be taken as this
connection's energy.

### Prices across an interruption

- A price or pricing mode changed in the options form applies from the **next**
  session. The running session keeps the price it started with, also when the
  options form reloads the integration.
- Spot pricing: every kWh is billed exactly once (`kwh` in `price_details` sums
  to `energy_kwh`). If start-up happens in the same hour, that hour continues
  with the price captured at its start. If the hour has passed, the energy not
  yet billed — including energy delivered while Home Assistant was down — is
  billed in that hour at its captured price, and a new hour starts with the
  price read at start-up. A clock that reads earlier than the stored hour does
  not close it.
- If the spot price sensor has not reported since the integration was last set
  up or reloaded when an hour starts, the hour takes the sensor's first value
  within that hour; only if the hour ends first does it use the fallback price.
  A sensor that had reported and is merely unavailable at the hour's start gives
  the fallback price, as before.

### What an interruption still changes

Money can still differ in these cases:

- A second car that charges and leaves within the same outage: its energy is
  added to the previous user's session, because the counter cannot tell the
  two connections apart.
- A card shown only after a session was resumed is not acted on: the session
  keeps the user it started with.
- The charger's counter restarts during the outage and the same card is still
  there: what the new counter had counted before start-up is not billed.
- Energy delivered during an outage is priced at the spot hour that was in
  progress when Home Assistant went down, or at the current hour for a session
  that starts at start-up. If the hour tick comes before the first counter
  reading after a resume, the outage energy is priced at the later hour.
- A spot hour is on the fallback price when the price sensor has no price at the
  hour's start and, if it has not reported since the integration was last set up
  or reloaded, does not report within that hour. A price still missing when the
  session was saved stays on the fallback if the session resumes in the same hour.
- A car plugged in without a card while Home Assistant was down, after the
  previous car left, is added to the resumed session unless the charger shows a
  different card at start-up.
- A session that began entirely while Home Assistant was down is counted from
  start-up: what the counter already showed then is not billed.
- Energy that cannot be known is not attributed to anyone: when another
  person's connection restarted the counter, or the counter reads below the
  snapshot after the car left, the old session keeps its snapshot energy, and
  energy delivered between the last snapshot write and the interruption is lost.

These are not money and are deliberately unchanged:

- Every resumed or recovered session, and every session started with the cable
  already in, has `reconstructed=True` and `data_gap=True`.
- Charging time and window count: a window open at the interruption becomes a
  synthetic window that ends at start-up (see below), and a window that is open
  after start-up counts as a new window.
- A transient disconnect in progress at the interruption is not continued: the
  plug state at start-up decides, so a car that is still unplugged at start-up
  and is plugged back in later becomes a second session (energy and cost are
  correct in total); one plugged back in before start-up continues the session.
- `connected_at` is the start-up time when the plug-in was not observed, and the
  end time is the start-up time when the car left during the outage.

A snapshot written by 0.4.7 (no `resume_state`) resumes with the price
configured now, the guest rate looked up again through the card (used only
while the card still belongs to the same guest), and in spot mode a new hour
that starts at start-up.

### Restart recovery — synthetic window injection

When Home Assistant restarts mid-session, the recovery detects a pre-restart open charging window (via `session.charging_started_at`) and injects a synthetic closed window into the tracker before any subsequent window opens when charging power returns. This ensures `session.charging_duration_s` correctly accounts for the pre-restart charging period. See `specs/019-session-quality-fixes/spec.md` IC-6 for the binding decisions.

---

## Session Store Schema (v1.2)

Sessions are stored under the storage key `ev_charging_manager_sessions`
(store version 1.2). Every record has these fields. The names ending in `_kr` are
historical: they hold amounts in the currency configured in the integration, whatever it is.

| Field | Type | Description |
|-------|------|-------------|
| `id` | str | UUID |
| `user_name` | str | Name at session start, or `Unknown` |
| `user_type` | str | `regular`, `guest` or `unknown` |
| `vehicle_name` | str \| None | Vehicle mapped to the card at session start |
| `vehicle_battery_kwh` | float \| None | Its battery capacity at session start |
| `efficiency_factor` | float \| None | Its charging efficiency at session start |
| `rfid_index` | int \| None | Card index (trx − 1); None without a card |
| `rfid_uid` | str \| None | Card UID, when a UID entity is configured |
| `connected_at` | str | ISO-8601 UTC plug-in time |
| `disconnected_at` | str \| None | ISO-8601 UTC unplug time; None in the active snapshot |
| `started_at`, `ended_at`, `duration_seconds` | | Legacy aliases of `connected_at`, `disconnected_at`, `charging_duration_s` |
| `connection_duration_s` | int | Seconds from `connected_at` to `disconnected_at` |
| `charging_started_at` | str \| None | Start of the first charging window |
| `charging_ended_at` | str \| None | End of the last closed charging window |
| `charging_duration_s` | int | Sum of all charging window durations (seconds) |
| `charging_window_count` | int | Number of charging windows |
| `energy_kwh` | float | Energy delivered (kWh) |
| `energy_start_kwh` | float | Counter reading the session's energy is measured from |
| `avg_power_w` | float | Average power during charging windows (W) |
| `max_power_w` | float | Highest power reading (W) |
| `phases_used`, `max_current_a` | None | Reserved, not filled |
| `cost_total_kr` | float | Cost of the energy |
| `cost_method` | str | `static` (fixed price) or `spot` |
| `price_details` | list \| None | Spot only: one entry per hour (`hour`, `kwh`, `spot_price_kr_kwh`, `total_price_kr_kwh`, `cost_kr`, `fallback`) |
| `charge_price_total_kr` | float \| None | Guests only: what the guest is charged |
| `charge_price_method` | str \| None | Guests only: `fixed` or `markup` |
| `estimated_soc_added_pct` | float \| None | SoC estimate, when the vehicle is known |
| `charger_name` | str | Charger name |
| `charger_total_before_kwh` | float \| None | Charger's lifetime counter at session start (stored at start); in a completed record set only together with `charger_total_after_kwh` |
| `charger_total_after_kwh` | float \| None | Charger's lifetime counter at session end |
| `data_gap` | bool | The record may be incomplete or imprecise (see `session.py` for the full list) |
| `reconstructed` | bool | The session was resumed or completed after a restart or reload, or started with the cable already in |

### `resume_state` (active snapshot only)

The active snapshot also has a `resume_state` object. A completed record and the
events never have it, and version 0.4.7 ignores it after a downgrade.

| Key | Content |
|-----|---------|
| `pricing` | The price parameters the session started with: `mode` (`static` or `spot`) and either `static_price_kwh`, or `spot_price_entity`, `spot_additional_cost_kwh`, `spot_vat_multiplier` and `spot_fallback_price_kwh` |
| `guest_pricing` | The guest rate (`method` with `price_per_kwh` or `markup_factor`), or None |
| `spot_hour` | Spot only: `start` of the hour in progress, its captured `price` and whether that is the `fallback`; None otherwise |

A missing or malformed part falls back as for a 0.4.7 snapshot, with a warning
in the Home Assistant log; so does a number that is not finite, or a fixed
price, markup factor or fallback price below zero. A malformed `price_details`
in the snapshot counts as empty, also with a warning.

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
