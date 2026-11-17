"""Drive goe_gemini sessions through the charger entities and interrupt them (spec 027 § 6.3).

Real storage (``hass_storage``), no patch on ``Store``. Time advances in 60 s steps so the
periodic save and the spot hour tick fire at their real clock times (research 027 Part D).

Interruption models:
- ``reload``: ``hass.config_entries.async_reload`` (the real unload and setup path).
- ``restart``: what the next process sees after an orderly stop or a crash. Home Assistant
  does not unload config entries at stop (research 027 B.2), so only what is on disk
  survives. The storage is frozen, the entry unloaded (dropping its listeners and timers),
  the frozen storage put back (discarding what unload wrote), time may pass and the charger
  change while down, and the entry is set up again with the charger's entities either
  already present or appearing afterwards ("late", one by one in a given order).
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.ev_charging_manager.const import (
    CONF_PERSISTENCE_INTERVAL_S,
    DOMAIN,
    EVENT_CHARGING_CHARGED,
    EVENT_SESSION_COMPLETED,
    EVENT_SESSION_STARTED,
)

PLUG = "binary_sensor.goe_abc123_car_0"
LOCK = "sensor.goe_abc123_cus_value"
TRX = "select.goe_abc123_trx"
ENERGY = "sensor.goe_abc123_wh"
POWER = "sensor.goe_abc123_nrg_11"
CAR = "sensor.goe_abc123_car_value"
ETO = "sensor.goe_abc123_eto"
SPOT = "sensor.spot_price"
NAMES = {
    "plug": PLUG,
    "lock": LOCK,
    "trx": TRX,
    "energy": ENERGY,
    "power": POWER,
    "car": CAR,
    "eto": ETO,
    "spot": SPOT,
}
CHARGER = [PLUG, LOCK, TRX, ENERGY, POWER, CAR, ETO]
# The order in which the charger's entities became valid at three restarts in the
# owner's log (spec 027 § 2.4, § 13.1): this integration is set up first.
FIELD_ORDER = ["plug", "lock", "trx", "power", "energy", "car", "eto"]
PLUG_LAST = ["energy", "power", "trx", "lock", "car", "eto", "plug"]
DAY = "2026-11-17"

# trx value = card_index + 1
TRX_PAUL, TRX_GFIX, TRX_GMARK = "1", "2", "3"

SUBENTRIES = [
    {
        "subentry_id": "u_paul",
        "subentry_type": "user",
        "title": "Paul",
        "unique_id": None,
        "data": {"name": "Paul", "type": "regular", "active": True},
    },
    {
        "subentry_id": "u_gfix",
        "subentry_type": "user",
        "title": "Gaest",
        "unique_id": None,
        "data": {
            "name": "Gaest",
            "type": "guest",
            "active": True,
            "guest_pricing": {"method": "fixed", "price_per_kwh": 5.0},
        },
    },
    {
        "subentry_id": "u_gmark",
        "subentry_type": "user",
        "title": "Markup",
        "unique_id": None,
        "data": {
            "name": "Markup",
            "type": "guest",
            "active": True,
            "guest_pricing": {"method": "markup", "markup_factor": 1.5},
        },
    },
    {
        "subentry_id": "v_car",
        "subentry_type": "vehicle",
        "title": "Car",
        "unique_id": None,
        "data": {
            "name": "Car",
            "battery_capacity_kwh": 50.0,
            "usable_battery_kwh": 50.0,
            "charging_phases": 3,
            "max_charging_power_kw": 11.0,
            "charging_efficiency": 0.9,
        },
    },
    {
        "subentry_id": "r0",
        "subentry_type": "rfid_mapping",
        "title": "Card 0",
        "unique_id": None,
        "data": {
            "card_index": 0,
            "card_uid": None,
            "user_id": "u_paul",
            "vehicle_id": "v_car",
            "active": True,
            "deactivated_by": None,
        },
    },
    {
        "subentry_id": "r1",
        "subentry_type": "rfid_mapping",
        "title": "Card 1",
        "unique_id": None,
        "data": {
            "card_index": 1,
            "card_uid": None,
            "user_id": "u_gfix",
            "vehicle_id": None,
            "active": True,
            "deactivated_by": None,
        },
    },
    {
        "subentry_id": "r2",
        "subentry_type": "rfid_mapping",
        "title": "Card 2",
        "unique_id": None,
        "data": {
            "card_index": 2,
            "card_uid": None,
            "user_id": "u_gmark",
            "vehicle_id": None,
            "active": True,
            "deactivated_by": None,
        },
    },
]

SPOT_DATA = {
    "spot_price_entity": SPOT,
    "spot_additional_cost_kwh": 0.0,
    "spot_vat_multiplier": 1.0,
    "spot_fallback_price_kwh": 9.0,
}


def entry_data(pricing: str) -> dict:
    """Config entry data for the goe_gemini profile at 2.50 kr/kWh or spot."""
    data = {
        "charger_profile": "goe_gemini",
        "charger_serial": "abc123",
        "car_status_entity": CAR,
        "car_status_charging_value": "Charging",
        "energy_entity": ENERGY,
        "energy_unit": "kWh",
        "power_entity": POWER,
        "rfid_entity": TRX,
        "total_energy_entity": None,
        "rfid_uid_entity": None,
        "charger_name": "My go-e Charger",
        "charger_host": "192.168.1.100",
        "pricing_mode": pricing,
        "static_price_kwh": 2.50,
    }
    if pricing == "spot":
        data |= SPOT_DATA
    return data


def sessions_on_disk(storage: dict) -> list[dict]:
    """The session list inside both envelopes (Home Assistant's and the store's own)."""
    raw = storage.get("ev_charging_manager_sessions") or {}
    inner = raw.get("data", [])
    if isinstance(inner, dict):
        inner = inner.get("data", [])
    return inner or []


def snapshot_on_disk(storage: dict) -> dict | None:
    """The active snapshot on disk (the record without an end time), or None."""
    active = [
        s
        for s in sessions_on_disk(storage)
        if s.get("disconnected_at") is None and s.get("ended_at") is None
    ]
    return active[-1] if active else None


class Rig:
    """One charger, one config entry, real storage, a frozen clock."""

    def __init__(
        self,
        hass: HomeAssistant,
        freezer,
        storage: dict,
        *,
        pricing: str = "static",
        energy0: str = "0.0",
        persistence_interval_s: int | None = None,
    ) -> None:
        """Hold the fixtures; nothing happens until setup()."""
        self.hass = hass
        self.freezer = freezer
        self.storage = storage
        self.pricing = pricing
        self.energy0 = energy0
        self.persistence_interval_s = persistence_interval_s
        self.entry: MockConfigEntry | None = None

    async def setup(self, start: str = "10:05:00") -> Rig:
        """Idle charger at ``start``, entry set up, events captured from here on."""
        hass = self.hass
        self.freezer.move_to(f"{DAY} {start}+00:00")
        hass.states.async_set(PLUG, "off")
        hass.states.async_set(LOCK, "Unlocked")
        hass.states.async_set(TRX, "null")
        hass.states.async_set(ENERGY, self.energy0)
        hass.states.async_set(POWER, "0.0")
        hass.states.async_set(CAR, "Idle")
        hass.states.async_set(ETO, "1000.0")
        hass.states.async_set(SPOT, "1.0")
        options = {"eto_entity": ETO, "plug_entity": PLUG, "cable_lock_entity": LOCK}
        if self.persistence_interval_s is not None:
            options[CONF_PERSISTENCE_INTERVAL_S] = self.persistence_interval_s
        self.entry = MockConfigEntry(
            domain=DOMAIN,
            data=entry_data(self.pricing),
            options=options,
            title="T",
            subentries_data=copy.deepcopy(SUBENTRIES),
        )
        self.entry.add_to_hass(hass)
        await hass.config_entries.async_setup(self.entry.entry_id)
        await hass.async_block_till_done()
        self.completed = async_capture_events(hass, EVENT_SESSION_COMPLETED)
        self.started = async_capture_events(hass, EVENT_SESSION_STARTED)
        self.charged = async_capture_events(hass, EVENT_CHARGING_CHARGED)
        return self

    @property
    def engine(self):
        """The session engine of the currently loaded entry."""
        return self.hass.data[DOMAIN][self.entry.entry_id]["session_engine"]

    @property
    def stats(self):
        """The statistics engine of the currently loaded entry."""
        return self.hass.data[DOMAIN][self.entry.entry_id]["stats_engine"]

    async def goto(self, hhmmss: str, *, fire: bool = True) -> None:
        """Advance the clock to ``hhmmss`` in 60 s steps; ``fire=False`` while down."""
        target = datetime.fromisoformat(f"{DAY}T{hhmmss}+00:00")
        while (now := dt_util.utcnow()) < target:
            self.freezer.tick(min(timedelta(seconds=60), target - now))
            if fire:
                async_fire_time_changed(self.hass, dt_util.utcnow())
                await self.hass.async_block_till_done()

    async def set(self, **values) -> None:
        """Set charger entities by short name, one state change at a time."""
        for name, value in values.items():
            self.hass.states.async_set(NAMES[name], str(value))
            await self.hass.async_block_till_done()

    async def plug_in(self, trx: str, power: int = 7000) -> None:
        """Card first, then cable, then power (the blip-first order of the field)."""
        await self.set(trx=trx)
        await self.set(plug="on", lock="Locked")
        await self.set(power=power)

    async def unplug(self) -> None:
        """Genuine unplug: the cable lock opens, then the plug goes off."""
        await self.set(lock="Unlocked")
        await self.set(plug="off")

    def records(self) -> list[dict]:
        """Completed session records on disk."""
        return [
            s
            for s in sessions_on_disk(self.storage)
            if s.get("disconnected_at") is not None or s.get("ended_at") is not None
        ]

    def snapshot(self) -> dict | None:
        """The active snapshot on disk, or None."""
        return snapshot_on_disk(self.storage)

    def money(self) -> list[dict]:
        """What § 6.3 compares, per completed session: event and record side by side.

        Also holds every comparison to one stored record per completed event and no
        active snapshot left behind (spec 027 § 13.3).
        """
        assert len(self.records()) == len(self.completed), "one record per completed event"
        assert self.snapshot() is None, "an active snapshot was left behind"
        out = []
        records = {r["id"]: r for r in self.records()}
        for ev in self.completed:
            rec = records.get(ev.data["session_id"], {})
            details = rec.get("price_details") or []
            out.append(
                {
                    "user": ev.data["user_name"],
                    "energy": ev.data["energy_kwh"],
                    "cost": ev.data["cost_kr"],
                    "charge_price": ev.data["charge_price_kr"],
                    "charge_method": rec.get("charge_price_method"),
                    "record_energy": round(rec.get("energy_kwh", -1), 3),
                    "record_cost": round(rec.get("cost_total_kr", -1), 2),
                    "record_charge_price": rec.get("charge_price_total_kr"),
                    "details_kwh": round(sum(d["kwh"] for d in details), 3),
                    "details": [
                        (d["hour"][11:16], d["kwh"], d["total_price_kr_kwh"], d["fallback"])
                        for d in details
                    ],
                }
            )
        return out


Interruption = Callable[[Rig], Awaitable[None]]


async def reload(rig: Rig) -> None:
    """A real config-entry reload."""
    assert await rig.hass.config_entries.async_reload(rig.entry.entry_id)
    await rig.hass.async_block_till_done()


def restart(
    entities: str = "present",
    *,
    during: Interruption | None = None,
    order: list[str] | None = None,
    absent: list[str] | None = None,
    spot_late: bool = False,
    unavailable: bool = False,
) -> Interruption:
    """An orderly stop or a crash followed by a new setup (see the module docstring).

    entities: "present" = the charger's entities have their values at setup; "late" = they
        are absent at setup and appear afterwards one by one in ``order`` (short names,
        default ``FIELD_ORDER``).
    during: runs while the integration is down (time passes, the charger changes).
    absent: which entities are absent for "late" (default: all charger entities).
    spot_late: the spot price entity is absent at setup as well and appears last.
    unavailable: the late entities read "unavailable" at setup instead of being absent
        (restored from the entity registry before their integration has loaded).
    """

    async def _restart(rig: Rig) -> None:
        hass = rig.hass
        frozen = copy.deepcopy(dict(rig.storage))
        await hass.config_entries.async_unload(rig.entry.entry_id)
        await hass.async_block_till_done()
        rig.storage.clear()
        rig.storage.update(frozen)
        if during is not None:
            await during(rig)
        saved: dict[str, str] = {}
        names = (absent or FIELD_ORDER) if entities == "late" else []
        if spot_late:
            names = [*names, "spot"]
        for eid in [NAMES[n] for n in names]:
            state = hass.states.get(eid)
            if state is not None:
                saved[eid] = state.state
                if unavailable:
                    hass.states.async_set(eid, "unavailable")
                else:
                    hass.states.async_remove(eid)
        await hass.async_block_till_done()
        await hass.config_entries.async_setup(rig.entry.entry_id)
        await hass.async_block_till_done()
        default = [*FIELD_ORDER, "spot"]
        for eid in [NAMES[n] for n in (default if order is None else order)]:
            if eid in saved:
                hass.states.async_set(eid, saved[eid])
                await hass.async_block_till_done()

    return _restart


# Research 027 scenarios A, F and B, shared by the RT test files.


async def scenario_a(rig: Rig, trx: str, at: dict[str, Interruption]) -> None:
    """One continuous charge 10:06–10:26, unplug 10:40 (research 027 scenario A)."""
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(trx)
    await rig.goto("10:08:00")
    await rig.set(energy=0.5, eto=1000.5)
    await rig.goto("10:08:30")
    if "young" in at:
        await at["young"](rig)
    await rig.goto("10:12:00")
    await rig.set(energy=1.5, eto=1001.5)
    await rig.goto("10:17:00")
    await rig.set(energy=2.5, eto=1002.5)
    await rig.goto("10:17:30")
    if "mid" in at:
        await at["mid"](rig)
    await rig.goto("10:22:00")
    if "mid2" in at:
        await at["mid2"](rig)
    await rig.goto("10:25:00")
    await rig.set(energy=4.0, eto=1004.0)
    await rig.goto("10:26:00")
    await rig.set(power=0)
    await rig.goto("10:40:00")
    await rig.unplug()
    await rig.goto("10:41:00")


async def _finish_charge(rig: Rig, *, fire: bool = True) -> None:
    """The car charges to 4.0 kWh and stops; with fire=False this happens while down."""
    await rig.goto("10:25:00", fire=fire)
    await rig.set(energy=4.0, eto=1004.0)
    await rig.goto("10:26:00", fire=fire)
    await rig.set(power=0)
    await rig.goto("10:35:00", fire=fire)


async def scenario_f(rig: Rig, trx: str, interruption: Interruption | None) -> None:
    """The charge finishes during the outage; no energy update follows start-up (research F)."""
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(trx)
    await rig.goto("10:12:00")
    await rig.set(energy=1.5, eto=1001.5)
    await rig.goto("10:17:00")
    await rig.set(energy=2.5, eto=1002.5)
    await rig.goto("10:17:30")
    if interruption is None:
        await _finish_charge(rig)
    else:
        await interruption(rig)
    await rig.goto("10:40:00")
    await rig.unplug()
    await rig.goto("10:41:00")


async def finish_while_down(rig: Rig) -> None:
    await _finish_charge(rig, fire=False)


# Scenario B uninterrupted: 4.0 kWh @1.0 + 3.0 kWh @2.0 + 2.0 kWh @3.0.
B_DETAILS = [("10:00", 4.0, 1.0, False), ("11:00", 3.0, 2.0, False), ("12:00", 2.0, 3.0, False)]
B_TRUTH = {
    "user": "Paul",
    "energy": 9.0,
    "cost": 16.0,
    "charge_price": None,
    "charge_method": None,
    "record_energy": 9.0,
    "record_cost": 16.0,
    "record_charge_price": None,
    "details_kwh": 9.0,
    "details": B_DETAILS,
}


async def scenario_b(rig: Rig, trx: str, at: dict[str, Interruption]) -> None:
    """Spot charge across two hour boundaries, 10:06–12:21 (research 027 scenario B)."""
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(trx)
    await rig.goto("10:30:00")
    await rig.set(energy=3.0)
    await rig.goto("10:40:00")
    if "h1" in at:
        await at["h1"](rig)
    await rig.goto("10:50:00")
    if "span" in at:
        await at["span"](rig)
    await rig.goto("10:55:00")
    await rig.set(energy=4.0)
    await rig.goto("10:59:00")
    await rig.set(spot=2.0)
    await rig.goto("11:30:00")
    await rig.set(energy=6.0)
    await rig.goto("11:40:00")
    if "h2" in at:
        await at["h2"](rig)
    await rig.goto("11:55:00")
    await rig.set(energy=7.0)
    await rig.goto("11:59:00")
    await rig.set(spot=3.0)
    await rig.goto("12:10:00")
    await rig.set(energy=8.0)
    await rig.goto("12:15:00")
    if "final" in at:
        await at["final"](rig)
    await rig.goto("12:20:00")
    await rig.set(energy=9.0)
    await rig.goto("12:21:00")
    await rig.set(power=0)
    await rig.goto("12:30:00")
    await rig.unplug()
    await rig.goto("12:31:00")


INTERRUPTIONS: dict[str, Interruption | None] = {
    "none": None,
    "reload": reload,
    "restart-present": restart(),
    "restart-late": restart("late"),  # FIELD_ORDER
}
