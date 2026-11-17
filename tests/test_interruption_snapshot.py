"""Spec 027 RT-27…RT-33: the active snapshot, the unchanged contract and unchanged behaviour."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.ev_charging_manager.const import EVENT_UNKNOWN_RFID_DETECTED
from tests.session_rig import (
    B_TRUTH,
    SPOT,
    TRX_GFIX,
    TRX_GMARK,
    TRX_PAUL,
    Interruption,
    Rig,
    reload,
    restart,
    scenario_a,
    scenario_b,
    snapshot_on_disk,
)

SPOT_PRICING = {
    "mode": "spot",
    "spot_price_entity": SPOT,
    "spot_additional_cost_kwh": 0.0,
    "spot_vat_multiplier": 1.0,
    "spot_fallback_price_kwh": 9.0,
}
MARKUP = {"method": "markup", "markup_factor": 1.5}


def _resume_state(hour: str, price: float) -> dict:
    return {
        "pricing": SPOT_PRICING,
        "guest_pricing": MARKUP,
        "spot_hour": {"start": f"2026-11-17T{hour}+00:00", "price": price, "fallback": False},
    }


async def test_rt27_snapshot_written_at_each_session_event(hass, hass_storage, freezer) -> None:
    """RT-27: start, window open, window close, hour tick, plug off and stop each write.

    The periodic save is pushed out to 3600 s (first at 11:05) so every change seen on disk
    comes from the event under test; the energy set just before each event is the marker.
    """
    rig = Rig(hass, freezer, hass_storage, pricing="spot", persistence_interval_s=3600)
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.set(trx=TRX_GMARK)
    await rig.set(plug="on", lock="Locked")
    start = rig.snapshot()
    assert start is not None and start["energy_kwh"] == 0.0
    assert start["charger_total_before_kwh"] == 1000.0
    assert start["resume_state"] == _resume_state("10:00", 1.0)

    await rig.set(energy=0.2)
    await rig.set(power=7000)
    window_open = rig.snapshot()
    assert window_open["energy_kwh"] == 0.2
    assert window_open["charging_started_at"] is not None

    await rig.goto("10:30:00")
    await rig.set(energy=1.0)
    await rig.set(power=0)
    assert rig.snapshot()["energy_kwh"] == 0.2, "an energy update alone writes nothing"
    await rig.goto("10:36:00")  # idle timer (5 min) closes the window at 10:35
    window_close = rig.snapshot()
    assert window_close["energy_kwh"] == 1.0
    assert window_close["charging_ended_at"] is not None

    await rig.goto("10:50:00")
    await rig.set(energy=1.2, spot=2.0)
    await rig.goto("11:00:00")
    tick = rig.snapshot()
    assert tick["energy_kwh"] == 1.2
    assert [(d["hour"], d["kwh"]) for d in tick["price_details"]] == [
        ("2026-11-17T10:00+00:00", 1.2)
    ]
    assert tick["resume_state"] == _resume_state("11:00", 2.0)

    await rig.goto("11:10:00")
    await rig.set(energy=1.5)
    await rig.set(plug="off")  # cable lock still Locked: transient, grace starts
    assert rig.snapshot()["energy_kwh"] == 1.5

    await rig.set(energy=1.6)
    live = rig.engine.active_session
    built = rig.engine.get_active_session_dict()
    assert built["price_details"] == live.price_details
    assert built["price_details"] is not live.price_details
    assert all(a is not b for a, b in zip(built["price_details"], live.price_details, strict=True))

    await hass.async_stop()
    stop = rig.snapshot()
    assert stop["energy_kwh"] == 1.6
    assert stop["resume_state"] == _resume_state("11:00", 2.0)


async def test_rt28_write_for_a_session_no_longer_held_does_nothing(
    hass, hass_storage, freezer
) -> None:
    """RT-28: a write requested for a discarded or completed session id is skipped."""
    rig = Rig(hass, freezer, hass_storage, persistence_interval_s=3600)
    await rig.setup()
    await rig.goto("10:06:00")

    # Micro session: in and out within the 60 s minimum; the discard clears the snapshot.
    await rig.set(trx=TRX_PAUL)
    await rig.set(plug="on", lock="Locked")
    micro_id = rig.engine.active_session.id
    await rig.unplug()
    assert rig.engine.active_session is None and rig.snapshot() is None
    await rig.engine.async_write_snapshot(micro_id)
    assert rig.snapshot() is None, "a discarded session must not be resurrected"

    # Session A completes, session B runs; a late write for A must not touch the disk.
    await rig.goto("10:10:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:20:00")
    await rig.set(energy=1.0)
    first_id = rig.engine.active_session.id
    await rig.unplug()
    assert [r["id"] for r in rig.records()] == [first_id]
    await rig.goto("10:30:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:40:00")
    await rig.set(energy=1.4)  # B has 0.4 kWh in memory, 0.0 on disk
    assert rig.snapshot()["energy_kwh"] == 0.0

    await rig.engine.async_write_snapshot(first_id)

    assert rig.snapshot()["energy_kwh"] == 0.0
    assert [r["id"] for r in rig.records()] == [first_id]


# The keys on main at 4def44d (spec 027 § 4.2): only values may change.
RECORD_KEYS = {
    "avg_power_w", "charge_price_method", "charge_price_total_kr", "charger_name",
    "charger_total_after_kwh", "charger_total_before_kwh", "charging_duration_s",
    "charging_ended_at", "charging_started_at", "charging_window_count", "connected_at",
    "connection_duration_s", "cost_method", "cost_total_kr", "data_gap", "disconnected_at",
    "duration_seconds", "efficiency_factor", "ended_at", "energy_kwh", "energy_start_kwh",
    "estimated_soc_added_pct", "id", "max_current_a", "max_power_w", "phases_used",
    "price_details", "reconstructed", "rfid_index", "rfid_uid", "started_at", "user_name",
    "user_type", "vehicle_battery_kwh", "vehicle_name",
}  # fmt: skip
COMPLETED_KEYS = {
    "session_id", "user_name", "user_type", "vehicle_name", "energy_kwh", "cost_kr",
    "charge_price_kr", "duration_minutes", "avg_power_w", "estimated_soc_added_pct",
    "started_at", "ended_at", "cost_method", "reconstructed", "data_gap", "rfid_index",
    "charger_name", "connection_duration_s", "charging_duration_s", "charging_window_count",
}  # fmt: skip
STARTED_KEYS = {
    "session_id", "user_name", "user_type", "vehicle_name", "rfid_index", "rfid_uid",
    "started_at", "charger",
}  # fmt: skip
CHARGED_KEYS = {
    "session_id", "window_index", "window_started_at", "window_ended_at", "window_energy_kwh",
    "window_duration_s", "user_name", "user_type", "vehicle_name", "charger_name",
    "session_charging_duration_s", "session_energy_kwh", "session_window_count",
}  # fmt: skip
UNKNOWN_RFID_KEYS = {"rfid_index", "reason", "charger_name", "detected_at"}


async def test_rt29_records_and_events_keep_the_keys_of_main(hass, hass_storage, freezer):
    """RT-29: resumed (spot, guest), split at a card change, completed after the car
    left, and an unmapped card: every record and payload has exactly main's keys."""
    unknown = async_capture_events(hass, EVENT_UNKNOWN_RFID_DETECTED)
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(TRX_GMARK)
    await rig.goto("10:30:00")
    await rig.set(energy=1.0)
    await reload(rig)
    await rig.goto("10:40:00")

    async def card_change_then_left(rig: Rig) -> None:
        await rig.unplug()
        await rig.set(energy=0.0)
        await rig.plug_in(TRX_GFIX)
        await rig.set(energy=0.5)

    await restart(during=card_change_then_left)(rig)
    await rig.goto("10:50:00")

    async def left(rig: Rig) -> None:
        await rig.set(energy=0.9)
        await rig.unplug()

    await restart(during=left)(rig)
    await rig.goto("11:00:00")
    await rig.plug_in("9")
    await rig.goto("11:10:00")
    await rig.set(energy=1.5)
    await rig.unplug()

    assert len(rig.records()) == 3
    assert all(set(record) == RECORD_KEYS for record in rig.records())
    assert [set(ev.data) for ev in rig.completed] == [COMPLETED_KEYS] * 3
    assert all(set(ev.data) == STARTED_KEYS for ev in rig.started)
    assert rig.charged and all(set(ev.data) == CHARGED_KEYS for ev in rig.charged)
    assert [set(ev.data) for ev in unknown] == [UNKNOWN_RFID_KEYS]


def _as_written_by_047(rig: Rig) -> None:
    """Turn the snapshot on disk into the shape 0.4.7 wrote (hand-built on purpose)."""
    snapshot = snapshot_on_disk(rig.storage)
    del snapshot["resume_state"]
    snapshot["charger_total_before_kwh"] = None


async def _upgrade(rig: Rig) -> None:
    _as_written_by_047(rig)


async def _upgrade_and_remap_guest_card(rig: Rig) -> None:
    _as_written_by_047(rig)
    subentry = rig.entry.subentries["r1"]
    rig.hass.config_entries.async_update_subentry(
        rig.entry, subentry, data={**subentry.data, "user_id": "u_gmark"}
    )


@pytest.mark.parametrize(
    ("trx", "during", "charge"),
    [
        (TRX_PAUL, _upgrade, None),
        (TRX_GFIX, _upgrade, 20.0),
        (TRX_GFIX, _upgrade_and_remap_guest_card, None),
    ],
    ids=["regular", "guest", "guest-card-now-another-guest"],
)
async def test_rt30_snapshot_written_by_047_fixed_price(
    hass, hass_storage, freezer, trx, during, charge
) -> None:
    """RT-30: price from the entry, guest rate looked up again (only while the card
    still names the same guest). Allowed difference: no charger totals (0.4.7 had none)."""
    rig = Rig(hass, freezer, hass_storage)
    await scenario_a(rig, trx, {"mid": restart(during=during)})
    (money,) = rig.money()
    assert (money["energy"], money["cost"], money["charge_price"]) == (4.0, 10.0, charge)
    assert money["charge_method"] == (None if charge is None else "fixed")


async def test_rt30_snapshot_written_by_047_spot(hass, hass_storage, freezer) -> None:
    """RT-30, spot: the hour starts at the resume with the price captured then and every
    kWh not in price_details is billed from there (D-45 with D-39's billed energy). The
    restart falls in hour 11, whose live price is that hour's, so as uninterrupted."""
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await scenario_b(rig, TRX_PAUL, {"h2": restart(during=_upgrade)})
    assert rig.money() == [B_TRUTH]


BAD_PRICE = {"mode": "static", "static_price_kwh": "4"}
BAD_HOUR = {"start": "x", "price": 9.0, "fallback": False}


@pytest.mark.parametrize(
    ("trx", "pricing", "resume_state", "expected"),
    [
        (TRX_GFIX, "static", "garbage", (10.0, 20.0)),
        (TRX_GFIX, "static", {}, (10.0, 20.0)),
        (TRX_PAUL, "static", {"pricing": BAD_PRICE}, (10.0, None)),
        (TRX_GFIX, "static", {"pricing": None, "guest_pricing": {"method": "fixed"}}, (10.0, 20.0)),
        (TRX_PAUL, "spot", {"pricing": SPOT_PRICING, "spot_hour": BAD_HOUR}, (16.0, None)),
    ],
    ids=["not-a-dict", "empty", "pricing-malformed", "guest-malformed", "spot-hour-malformed"],
)  # fmt: skip
async def test_rt31_resume_state_with_missing_or_malformed_parts(
    hass, hass_storage, freezer, caplog, trx, pricing, resume_state, expected
) -> None:
    """RT-31: setup does not fail; the session resumes with the D-45 fallbacks (entry
    price, guest looked up, spot hour from the resume) and a warning is logged."""

    async def corrupt(rig: Rig) -> None:
        snapshot_on_disk(rig.storage)["resume_state"] = resume_state

    rig = Rig(hass, freezer, hass_storage, pricing=pricing)
    if pricing == "spot":
        await scenario_b(rig, trx, {"h2": restart(during=corrupt)})
    else:
        await scenario_a(rig, trx, {"mid": restart(during=corrupt)})

    assert rig.entry.state is ConfigEntryState.LOADED
    (money,) = rig.money()
    assert (money["cost"], money["charge_price"]) == expected
    assert len(rig.started) == 1, "the session resumed rather than restarted"
    assert "resume_state" in caplog.text and "spec 027 D-45" in caplog.text


async def scenario_g(rig: Rig, interruption: Interruption) -> None:
    """Research 027 scenario G: a transient unplug at 10:27, re-plugged at 10:31."""
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:12:00")
    await rig.set(energy=1.0)
    await rig.goto("10:18:00")
    await rig.set(energy=3.0)
    await rig.goto("10:27:00")
    await rig.set(power=0)
    await rig.set(plug="off")  # cable lock stays Locked: grace period
    await rig.goto("10:29:00")
    await interruption(rig)
    await rig.goto("10:31:00")
    await rig.set(plug="on")
    await rig.set(power=7000)
    await rig.goto("10:40:00")
    await rig.set(energy=4.0)
    await rig.goto("10:45:00")
    await rig.set(power=0)
    await rig.goto("10:50:00")
    await rig.unplug()
    await rig.goto("10:55:00")


async def scenario_c(rig: Rig, interruption: Interruption) -> None:
    """Research 027 scenario C: window 1 10:06–10:21, a pause, window 2 10:40–10:56."""
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:10:00")
    await rig.set(energy=1.0)
    await rig.goto("10:15:00")
    await rig.set(energy=2.0)
    await rig.goto("10:16:00")
    await rig.set(power=0)
    await rig.goto("10:30:00")
    await interruption(rig)
    await rig.goto("10:40:00")
    await rig.set(power=3000)
    await rig.goto("10:45:00")
    await rig.set(energy=2.2)
    await rig.goto("10:50:00")
    await rig.set(energy=2.5)
    await rig.goto("10:51:00")
    await rig.set(power=0)
    await rig.goto("11:05:00")
    await rig.unplug()
    await rig.goto("11:06:00")


def _completed(rig: Rig) -> list[tuple]:
    return [
        (
            ev.data["energy_kwh"],
            ev.data["cost_kr"],
            ev.data["charging_duration_s"],
            ev.data["charging_window_count"],
        )
        for ev in rig.completed
    ]


async def test_rt33_reload_during_a_grace_period_as_on_main(hass, hass_storage, freezer):
    """RT-33: plug state at start-up stays authoritative (spec 018 FR-030): two sessions
    as on main (research 027 G/replug/reload), 10.0 kr in total."""
    rig = Rig(hass, freezer, hass_storage)
    await scenario_g(rig, reload)
    assert _completed(rig) == [(3.0, 7.5, 0, 1), (1.0, 2.5, 1140, 1)]


async def test_rt33_reload_during_a_pause_between_windows_as_on_main(
    hass, hass_storage, freezer
) -> None:
    """RT-33: one session, money and charging time as on main (research 027
    C/reload.paused); both window events report window_index 1 (D-48, unchanged)."""
    rig = Rig(hass, freezer, hass_storage)
    await scenario_c(rig, reload)
    assert _completed(rig) == [(2.5, 6.25, 1860, 2)]
    assert [ev.data["window_index"] for ev in rig.charged] == [1, 1]
