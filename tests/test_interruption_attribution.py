"""Spec 027 RT-17…RT-26: who was plugged in, a cable already in at setup, a car that left.

Same acceptance rule as ``test_interruption_fixed_price``: the interrupted runs and the
uninterrupted run ("none") are held to stated expected values; allowed differences are
named in each test.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.ev_charging_manager.charger_profiles import CHARGER_PROFILES
from custom_components.ev_charging_manager.const import (
    DEFERRED_RECOVERY_TIMEOUT_MIN,
    RECOVERY_INPUT_WAIT_S,
    SessionEngineState,
    SessionSubState,
)
from tests.session_rig import (
    FIELD_ORDER,
    PLUG_LAST,
    TRX_GFIX,
    TRX_PAUL,
    Interruption,
    Rig,
    reload,
    restart,
    scenario_a,
)

PLUG_ANCHORED_PROFILES = {
    key for key, profile in CHARGER_PROFILES.items() if profile["supports_plug_anchored_model"]
}


def test_p1_guard_counter_belongs_to_the_connection() -> None:
    """PRELIMINARY P-1 (spec 027 § 10), guard.

    Assumption: with the cable in, the charger's energy counter (go-e ``wh``) is the
    energy of the current connection — it keeps its value after the unplug and restarts
    at zero at the next plug-in. D-50 and D-51 therefore start a session with
    energy_start_kwh = 0.0 when the connection began before start-up (exercised by
    RT-17 and RT-23). Evidence: three plug-ins in the owner's debug log (2026-11-08,
    -10, -13). A profile that reuses the plug-anchored engine must restate it here.
    """
    assert PLUG_ANCHORED_PROFILES == {"goe_gemini"}


def test_p2_guard_card_value_belongs_to_the_connection() -> None:
    """PRELIMINARY P-2 (spec 027 § 10), guard.

    Assumption: the card value (go-e ``trx``) does not change within one connection, so
    a different card at recovery means a new connection and D-50 splits a session that
    has a card (exercised by RT-17). Evidence: hardware test 2026-05-19 and one 12-hour
    connection in the field log; released by the pre-release field test with two cards.
    A profile that reuses the plug-anchored engine must restate it here.
    """
    assert PLUG_ANCHORED_PROFILES == {"goe_gemini"}


def _summary(rig: Rig) -> list[tuple]:
    return [
        (m["user"], m["energy"], m["cost"], m["charge_price"], m["record_energy"])
        for m in rig.money()
    ]


LATE_ORDERS = {"none": None, "restart-present": None, **{
    "restart-late-field": FIELD_ORDER, "restart-late-pluglast": PLUG_LAST,
}}  # fmt: skip


def _restart_in(intr: str, during: Interruption | None = None) -> Interruption:
    """The restart of ``LATE_ORDERS[intr]``: entities present, or late in that order."""
    order = LATE_ORDERS[intr]
    if order is None:
        return restart(during=during)
    return restart("late", during=during, order=order)


async def _swap_users(rig: Rig, *, fire: bool = True) -> None:
    """Paul's car leaves at 10:30 (3.5 kWh); the guest plugs in at 10:35, the counter
    restarts at zero with the new connection, and charges 1.0 kWh by 10:44."""
    await rig.goto("10:30:00", fire=fire)
    await rig.set(energy=3.5)
    await rig.set(power=0)
    await rig.unplug()
    await rig.set(trx="null")
    await rig.goto("10:35:00", fire=fire)
    await rig.set(energy=0.0)
    await rig.plug_in(TRX_GFIX)
    await rig.goto("10:44:00", fire=fire)
    await rig.set(energy=1.0)
    await rig.goto("10:45:00", fire=fire)


async def _swap_users_while_down(rig: Rig) -> None:
    await _swap_users(rig, fire=False)


async def scenario_h(rig: Rig, interruption: Interruption | None, *, trx: str = TRX_PAUL):
    """Research 027 scenario H: the first user leaves and another plugs in, 10:22–10:45."""
    await rig.setup()
    await rig.goto("10:06:00")
    if trx == "null":
        await rig.set(plug="on", lock="Locked")
        await rig.set(power=7000)
    else:
        await rig.plug_in(trx)
    await rig.goto("10:12:00")
    await rig.set(energy=1.0)
    await rig.goto("10:18:00")
    await rig.set(energy=3.0)
    await rig.goto("10:22:00")
    if interruption is None:
        await _swap_users(rig)
    else:
        await interruption(rig)
    await rig.goto("10:55:00")
    await rig.set(energy=2.0)
    await rig.goto("10:56:00")
    await rig.set(power=0)
    await rig.goto("11:05:00")
    await rig.unplug()
    await rig.goto("11:06:00")


GUEST_2KWH = ("Gaest", 2.0, 5.0, 10.0, 2.0)


@pytest.mark.parametrize("intr", ["none", "restart-present", "restart-late"])
async def test_rt17_another_user_plugged_in_during_the_outage(
    hass, hass_storage, freezer, intr
) -> None:
    """RT-17: two sessions; Paul gets the snapshot's energy, the guest the whole counter.

    Allowed difference: Paul's 0.5 kWh delivered after the last snapshot (10:20) cannot
    be known once the guest's connection restarted the counter, so Paul has 3.0 kWh
    instead of 3.5. Neither user gets the other's energy.
    """
    interruption = {
        "none": None,
        "restart-present": restart(during=_swap_users_while_down),
        "restart-late": restart("late", during=_swap_users_while_down),
    }[intr]
    rig = Rig(hass, freezer, hass_storage)
    await scenario_h(rig, interruption)

    paul_energy = 3.5 if intr == "none" else 3.0
    assert _summary(rig) == [
        ("Paul", paul_energy, paul_energy * 2.5, None, paul_energy),
        GUEST_2KWH,
    ]
    assert len(rig.started) == 2
    assert rig.stats.user_stats["Paul"].total_energy_kwh == pytest.approx(paul_energy)
    assert rig.stats.guest_last.charge_price_kr == 10.0
    assert rig.snapshot() is None


async def _counter_restart(rig: Rig, *, fire: bool = True) -> None:
    """The charger reboots at 10:25; its counter restarts at zero and reaches 1.0 kWh."""
    await rig.goto("10:25:00", fire=fire)
    await rig.set(energy=0.0)
    await rig.goto("10:35:00", fire=fire)
    await rig.set(energy=1.0)


async def _counter_restart_while_down(rig: Rig) -> None:
    await _counter_restart(rig, fire=False)


@pytest.mark.parametrize("base", [0.0, 10.0])
@pytest.mark.parametrize("intr", list(LATE_ORDERS))
async def test_rt18_same_card_counter_restarted_during_the_outage(
    hass, hass_storage, freezer, intr, base
) -> None:
    """RT-18: one session for the same user, the snapshot's energy kept.

    D-38 replays the present reading through the live reset rule, which takes the first
    reading after a reset as the new baseline. Live, that reading is 0.0 and nothing is
    lost (5.0 kWh). Across the outage it is 1.0, so the session keeps the snapshot's
    3.0 kWh plus the 1.0 kWh counted after start-up: 4.0 kWh. That 1.0 kWh difference is
    the D-38 rule as decided (§ 3 wins over RT-18's prose; spec 027 § 12, X-1).
    """
    rig = Rig(hass, freezer, hass_storage, energy0=str(base))
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:12:00")
    await rig.set(energy=base + 1.0)
    await rig.goto("10:18:00")
    await rig.set(energy=base + 3.0)
    await rig.goto("10:22:00")
    if intr == "none":
        await _counter_restart(rig)
    else:
        await _restart_in(intr, _counter_restart_while_down)(rig)
    await rig.goto("10:45:00")
    await rig.set(energy=2.0)
    await rig.goto("10:46:00")
    await rig.set(power=0)
    await rig.goto("10:50:00")
    await rig.unplug()

    energy = 5.0 if intr == "none" else 4.0
    assert _summary(rig) == [("Paul", energy, energy * 2.5, None, energy)]
    assert len(rig.started) == 1


async def _card_cleared_while_down(rig: Rig) -> None:
    await rig.set(trx="null")


@pytest.mark.parametrize("intr", ["none", "restart-present", "restart-late"])
async def test_rt19_no_card_at_recovery_resumes(hass, hass_storage, freezer, intr) -> None:
    """RT-19: plug on, no card on the charger: the snapshot is resumed (as RT-01)."""
    interruption = {
        "none": _card_cleared_while_down,
        "restart-present": restart(during=_card_cleared_while_down),
        "restart-late": restart("late", during=_card_cleared_while_down),
    }[intr]
    rig = Rig(hass, freezer, hass_storage)
    await scenario_a(rig, TRX_PAUL, {"mid": interruption})
    assert _summary(rig) == [("Paul", 4.0, 10.0, None, 4.0)]
    assert len(rig.started) == 1


async def _paul_plugs_in_after_unknown(rig: Rig, *, fire: bool = True) -> None:
    """The open-access car leaves at 10:30 (3.5 kWh); Paul plugs in with his card."""
    await rig.goto("10:30:00", fire=fire)
    await rig.set(energy=3.5)
    await rig.set(power=0)
    await rig.unplug()
    await rig.goto("10:35:00", fire=fire)
    await rig.set(energy=0.0)
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:44:00", fire=fire)
    await rig.set(energy=1.0)
    await rig.goto("10:45:00", fire=fire)


async def _paul_plugs_in_after_unknown_while_down(rig: Rig) -> None:
    await _paul_plugs_in_after_unknown(rig, fire=False)


async def _open_access_session(rig: Rig, *, card_after_power: bool = False) -> None:
    """Cable in at 10:06, power before any card: an Unknown session, 3.0 kWh by 10:18."""
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.set(plug="on", lock="Locked")
    await rig.set(power=7000)  # power during the RFID wait: open access, Unknown
    if card_after_power:
        await rig.set(trx=TRX_PAUL)  # a moment later: logged, the session is kept
    await rig.goto("10:12:00")
    await rig.set(energy=1.0)
    await rig.goto("10:18:00")
    await rig.set(energy=3.0)
    await rig.goto("10:22:00")
    assert rig.engine.active_session.rfid_index is None


@pytest.mark.parametrize("intr", list(LATE_ORDERS))
async def test_rt20_card_on_a_new_connection_after_a_cardless_snapshot(
    hass, hass_storage, freezer, intr
) -> None:
    """RT-20 (D-50): the Unknown car leaves during the outage and Paul plugs in with his
    card. A snapshot without a card is resumed whatever the charger shows, so there is
    one session and no energy is counted twice.

    Allowed difference (D-50, D-57): Paul's connection is added to the Unknown session
    through the live reset rule (4.0 kWh in one session for Unknown 3.5 + Paul 2.0).
    """
    rig = Rig(hass, freezer, hass_storage)
    await _open_access_session(rig)
    if intr == "none":
        await _paul_plugs_in_after_unknown(rig)
    else:
        await _restart_in(intr, _paul_plugs_in_after_unknown_while_down)(rig)
    await rig.goto("10:55:00")
    await rig.set(energy=2.0)
    await rig.set(power=0)
    await rig.goto("11:05:00")
    await rig.unplug()

    if intr == "none":
        assert _summary(rig) == [
            ("Unknown", 3.5, 8.75, None, 3.5),
            ("Paul", 2.0, 5.0, None, 2.0),
        ]
    else:
        assert _summary(rig) == [("Unknown", 4.0, 10.0, None, 4.0)]
        assert len(rig.started) == 1


@pytest.mark.parametrize("intr", ["reload", *LATE_ORDERS])
async def test_rt20_card_after_power_in_the_same_connection(
    hass, hass_storage, freezer, intr
) -> None:
    """RT-20 (D-50): power beat the card at the plug-in, so the session has no card and
    the card shows a moment later in the same connection. Any interruption resumes it:
    one session with the whole 5.0 kWh, as on main."""
    rig = Rig(hass, freezer, hass_storage)
    await _open_access_session(rig, card_after_power=True)
    if intr == "reload":
        await reload(rig)
    elif intr != "none":
        await _restart_in(intr)(rig)
    await rig.goto("10:55:00")
    await rig.set(energy=5.0)
    await rig.set(power=0)
    await rig.goto("11:05:00")
    await rig.unplug()

    assert _summary(rig) == [("Unknown", 5.0, 12.5, None, 5.0)]
    assert len(rig.started) == 1


@pytest.mark.parametrize("intr", ["restart-present", "restart-late-field"])
async def test_d50_split_session_is_flagged_reconstructed(hass, hass_storage, freezer, intr):
    """D-50: the session started for the other card at recovery is flagged
    ``reconstructed`` and ``data_gap``; the snapshot's session as every reconstruction."""
    rig = Rig(hass, freezer, hass_storage)
    await scenario_h(rig, _restart_in(intr, _swap_users_while_down))
    assert [(ev.data["user_name"], ev.data["reconstructed"], ev.data["data_gap"])
            for ev in rig.completed] == [("Paul", True, True), ("Gaest", True, True)]  # fmt: skip


async def _tick(rig: Rig, seconds: int) -> None:
    rig.freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(rig.hass, dt_util.utcnow())
    await rig.hass.async_block_till_done()


async def test_rt21_recovery_waits_for_the_rfid_entity(hass, hass_storage, freezer) -> None:
    """RT-21 (D-49): plug valid, RFID entity a few seconds late: the decision waits and
    nothing starts meanwhile; when the guest's card appears the snapshot splits as in
    RT-17."""
    late_trx = ["plug", "lock", "power", "energy", "car", "eto"]

    async def interruption(rig: Rig) -> None:
        await restart("late", during=_swap_users_while_down, order=late_trx)(rig)
        await _tick(rig, 5)
        engine = rig.engine
        assert engine.state == SessionEngineState.IDLE
        assert engine.active_session is None
        assert rig.snapshot()["user_name"] == "Paul", "the snapshot waits on disk"
        assert rig.completed == []
        await rig.set(trx=TRX_GFIX)

    rig = Rig(hass, freezer, hass_storage)
    await scenario_h(rig, interruption)
    assert _summary(rig) == [("Paul", 3.0, 7.5, None, 3.0), GUEST_2KWH]


async def test_rt21_rfid_entity_never_valid_resumes_after_the_wait(
    hass, hass_storage, freezer
) -> None:
    """RT-21 (D-49): the RFID entity never becomes valid: after RECOVERY_INPUT_WAIT_S the
    snapshot is resumed (no card is no evidence), and the session ends as uninterrupted."""

    async def interruption(rig: Rig) -> None:
        await restart("late", absent=["trx"], order=[])(rig)
        await _tick(rig, RECOVERY_INPUT_WAIT_S - 1)
        assert rig.engine.active_session is None, "the decision is still waiting"
        await _tick(rig, 2)
        assert rig.engine.active_session.user_name == "Paul"

    assert RECOVERY_INPUT_WAIT_S == 30
    rig = Rig(hass, freezer, hass_storage)
    await scenario_a(rig, TRX_PAUL, {"mid": interruption})
    assert _summary(rig) == [("Paul", 4.0, 10.0, None, 4.0)]
    assert len(rig.started) == 1


async def test_rt21_plug_off_with_the_rfid_entity_invalid_completes_at_once(
    hass, hass_storage, freezer
) -> None:
    """RT-21 (D-49): the car left during the outage and the RFID entity is missing: the
    plug alone decides, the snapshot is completed at setup without any wait."""
    rig = Rig(hass, freezer, hass_storage)
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:12:00")
    await rig.set(energy=1.0)
    await rig.goto("10:18:00")
    await rig.set(energy=3.0)
    await rig.goto("10:22:00")

    async def leaves(rig: Rig) -> None:
        await rig.set(energy=3.8, power=0)
        await rig.unplug()

    await restart("late", during=leaves, absent=["trx"], order=[])(rig)
    assert _summary(rig) == [("Paul", 3.8, 9.5, None, 3.8)]


async def test_d49_plug_off_energy_never_valid_completes_after_the_wait(
    hass, hass_storage, freezer
) -> None:
    """D-49: plug off and the energy entity never valid: after RECOVERY_INPUT_WAIT_S the
    snapshot is completed with its own energy (3.0 kWh at the 10:20 save)."""
    rig = Rig(hass, freezer, hass_storage)
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:12:00")
    await rig.set(energy=1.0)
    await rig.goto("10:18:00")
    await rig.set(energy=3.0)
    await rig.goto("10:22:00")

    async def leaves(rig: Rig) -> None:
        await rig.set(energy=3.8, power=0)
        await rig.unplug()

    await restart("late", during=leaves, absent=["energy"], order=[])(rig)
    await _tick(rig, RECOVERY_INPUT_WAIT_S - 1)
    assert rig.completed == [], "the completion waits for the energy entity"
    await _tick(rig, 2)
    assert _summary(rig) == [("Paul", 3.0, 7.5, None, 3.0)]


async def _counter_moves_while_down(rig: Rig) -> None:
    await rig.set(energy=2.5)


@pytest.mark.parametrize("pricing", ["static", "spot"])
async def test_rt21_timeout_still_completes_the_snapshot(
    hass, hass_storage, freezer, pricing
) -> None:
    """RT-21 / D-49: the plug entity never becomes valid: after the 60-minute timeout the
    snapshot is completed with the counter, which moved on to 2.5 kWh during the outage
    (D-52), and the spot energy is billed once in the stored hour (D-39)."""
    rig = Rig(hass, freezer, hass_storage, pricing=pricing)
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:12:00")
    await rig.set(energy=1.5)
    await rig.goto("10:15:30")  # the periodic save at 10:15 holds 1.5 kWh
    await restart("late", during=_counter_moves_while_down, absent=["plug"], order=[])(rig)
    assert rig.engine.active_session is None and rig.completed == []

    assert DEFERRED_RECOVERY_TIMEOUT_MIN == 60
    await rig.goto("11:16:30")

    cost = 6.25 if pricing == "static" else 2.5
    assert _summary(rig) == [("Paul", 2.5, cost, None, 2.5)]
    if pricing == "spot":
        (money,) = rig.money()
        assert money["details"] == [("10:00", 2.5, 1.0, False)]
        assert money["details_kwh"] == 2.5
    assert rig.engine.state == SessionEngineState.IDLE


async def _counter_restarted_below_the_snapshot(rig: Rig) -> None:
    await rig.set(energy=12.0)


@pytest.mark.parametrize("pricing", ["static", "spot"])
async def test_d52_timeout_never_completes_below_the_snapshot(
    hass, hass_storage, freezer, pricing
) -> None:
    """D-52: the counter restarted during the outage and reads 12.0 kWh, below the
    snapshot's 15.0 but above its start (10.0). The timeout completes with the
    snapshot's 5.0 kWh, not 2.0, and spot bills exactly that energy."""
    rig = Rig(hass, freezer, hass_storage, pricing=pricing, energy0="10.0")
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:30:00")
    await rig.set(energy=15.0)
    await rig.goto("10:40:00")  # the periodic save at 10:35 holds 5.0 kWh
    await restart("late", during=_counter_restarted_below_the_snapshot, absent=["plug"], order=[])(
        rig
    )
    await rig.goto("11:45:00")

    (money,) = rig.money()
    assert (money["energy"], money["record_energy"]) == (5.0, 5.0)
    if pricing == "spot":
        assert money["details"] == [("10:00", 5.0, 1.0, False)]
        assert (money["details_kwh"], money["cost"]) == (5.0, 5.0)
    else:
        assert money["cost"] == 12.5


async def scenario_v(rig: Rig, interruption: Interruption | None) -> None:
    """Research 027 scenario V: cable in at 10:06 without a card; the card at 10:10."""
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.set(plug="on", lock="Locked")
    await rig.goto("10:08:00")
    if interruption is not None:
        await interruption(rig)
    assert rig.engine.get_status_sub_state() == SessionSubState.WAITING_FOR_RFID
    await rig.goto("10:10:00")
    await rig.set(trx=TRX_PAUL)
    await rig.set(power=7000)
    await rig.goto("10:20:00")
    await rig.set(energy=2.0)
    await rig.goto("10:30:00")
    await rig.set(energy=4.0)
    await rig.goto("10:31:00")
    await rig.set(power=0)
    await rig.goto("10:40:00")
    await rig.unplug()


@pytest.mark.parametrize("intr", ["reload", *LATE_ORDERS])
async def test_rt22_waiting_for_rfid_at_the_interruption(hass, hass_storage, freezer, intr):
    """RT-22: one session with full energy and cost after the blip, also when the plug
    entity appears after setup (D-51).

    Allowed difference: connected_at is the start-up time, the plug-in was not observed.
    """
    rig = Rig(hass, freezer, hass_storage)
    interruption = {"none": None, "reload": reload}.get(intr) or _restart_in(intr)
    await scenario_v(rig, interruption)
    assert _summary(rig) == [("Paul", 4.0, 10.0, None, 4.0)]
    assert len(rig.started) == 1


async def _card_and_cable_while_down(rig: Rig) -> None:
    """Paul shows his card and plugs in at 10:10 while down; 2.0 kWh by 10:20."""
    await rig.goto("10:10:00", fire=False)
    await rig.set(trx=TRX_PAUL)
    await rig.set(plug="on", lock="Locked")
    await rig.set(power=7000)
    await rig.goto("10:20:00", fire=False)
    await rig.set(energy=2.0)


@pytest.mark.parametrize("intr", list(LATE_ORDERS))
async def test_rt23_cable_and_card_in_at_setup_without_snapshot(
    hass, hass_storage, freezer, intr
) -> None:
    """RT-23 (and P-1): the session starts at setup with energy_start_kwh 0.0 and takes
    the whole counter of the connection (4.0 kWh), not only what follows start-up, in
    every start order (D-51: a plug that appears after setup is a cable already in)."""
    rig = Rig(hass, freezer, hass_storage)
    await rig.setup()
    if intr == "none":
        await rig.goto("10:10:00")
        await rig.plug_in(TRX_PAUL)
        await rig.goto("10:20:00")
        await rig.set(energy=2.0)
    else:
        await _restart_in(intr, _card_and_cable_while_down)(rig)
        session = rig.engine.active_session
        assert session.energy_start_kwh == 0.0
        assert (session.energy_kwh, session.cost_total_kr) == (2.0, 5.0)
    await rig.goto("10:30:00")
    await rig.set(energy=4.0)
    await rig.set(power=0)
    await rig.goto("10:40:00")
    await rig.unplug()

    assert _summary(rig) == [("Paul", 4.0, 10.0, None, 4.0)]
    assert len(rig.started) == 1
    assert rig.completed[0].data["reconstructed"] is (intr != "none")


CARD_FIRST = ["trx", "plug", "lock", "power", "energy", "car", "eto"]


@pytest.mark.parametrize("order", [FIELD_ORDER, CARD_FIRST], ids=["plug-first", "card-first"])
async def test_d51_plug_from_unavailable_to_on_is_a_cable_already_in(
    hass, hass_storage, freezer, order
) -> None:
    """D-51: the charger's entities read "unavailable" at setup and then become valid,
    the plug before or after the card. The plug-on follows no valid off, so the session
    takes the whole counter of the connection, as with the plug present at setup."""
    rig = Rig(hass, freezer, hass_storage)
    await rig.setup()
    await restart("late", during=_card_and_cable_while_down, order=order, unavailable=True)(rig)
    session = rig.engine.active_session
    assert (session.energy_start_kwh, session.energy_kwh) == (0.0, 2.0)
    await rig.goto("10:30:00")
    await rig.set(energy=4.0)
    await rig.set(power=0)
    await rig.goto("10:40:00")
    await rig.unplug()

    assert _summary(rig) == [("Paul", 4.0, 10.0, None, 4.0)]
    assert (rig.completed[0].data["reconstructed"], rig.completed[0].data["data_gap"]) == (
        True,
        True,
    )


async def _open_access_car_while_down(rig: Rig) -> None:
    """An open-access car (no card) is plugged in at 10:10 and charges 2.0 kWh by 10:20."""
    await rig.goto("10:10:00", fire=False)
    await rig.set(plug="on", lock="Locked")
    await rig.set(power=7000)
    await rig.goto("10:20:00", fire=False)
    await rig.set(energy=2.0)


@pytest.mark.parametrize("intr", ["restart-present", "restart-late-pluglast"])
async def test_d51_open_access_with_power_already_flowing_starts_at_once(
    hass, hass_storage, freezer, intr
) -> None:
    """D-51: cable in, no card and power already above zero: no power event will ever
    resolve an RFID wait, so the open-access session starts at once with the whole
    counter."""
    rig = Rig(hass, freezer, hass_storage)
    await rig.setup()
    await _restart_in(intr, _open_access_car_while_down)(rig)
    session = rig.engine.active_session
    assert session is not None and session.user_name == "Unknown"
    assert (session.energy_start_kwh, session.energy_kwh) == (0.0, 2.0)
    await rig.goto("10:30:00")
    await rig.set(energy=4.0)
    await rig.set(power=0)
    await rig.goto("10:40:00")
    await rig.unplug()

    assert _summary(rig) == [("Unknown", 4.0, 10.0, None, 4.0)]


async def test_d51_after_a_timeout_completion_the_baseline_of_main_applies(
    hass, hass_storage, freezer
) -> None:
    """D-51 exception: this engine force-completed Paul's snapshot at the 60-minute
    timeout with the counter (2.5 kWh). When the plug then returns with the same car,
    the new session starts at the counter's reading, not at zero, so the 2.5 kWh are
    not counted again."""
    rig = Rig(hass, freezer, hass_storage)
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:12:00")
    await rig.set(energy=1.5)
    await rig.goto("10:15:30")
    await restart("late", during=_counter_moves_while_down, absent=["plug"], order=[])(rig)
    await rig.goto("11:16:30")
    assert _summary(rig) == [("Paul", 2.5, 6.25, None, 2.5)]

    await rig.set(plug="on")
    session = rig.engine.active_session
    assert (session.energy_start_kwh, session.energy_kwh) == (2.5, 0.0)
    await rig.goto("11:30:00")
    await rig.set(energy=3.0)
    await rig.set(power=0)
    await rig.goto("11:40:00")
    await rig.unplug()

    assert _summary(rig) == [("Paul", 2.5, 6.25, None, 2.5), ("Paul", 0.5, 1.25, None, 0.5)]


async def test_rt24_cable_in_while_recovery_is_deferred(hass, hass_storage, freezer) -> None:
    """RT-24: with the RFID entity missing at setup the recovery waits (D-49) and the
    cable-in handling does not run: no RFID wait, and power flowing meanwhile starts no
    session."""

    async def interruption(rig: Rig) -> None:
        await restart("late", absent=["trx"], order=[])(rig)
        engine = rig.engine
        assert engine.state == SessionEngineState.IDLE
        assert engine.get_status_sub_state() == SessionSubState.IDLE
        await rig.set(power=7100)
        assert engine.active_session is None and len(rig.started) == 1
        await rig.set(trx=TRX_PAUL)

    rig = Rig(hass, freezer, hass_storage)
    await scenario_a(rig, TRX_PAUL, {"mid": interruption})
    assert _summary(rig) == [("Paul", 4.0, 10.0, None, 4.0)]
    assert len(rig.started) == 1


async def _guest_leaves(rig: Rig, *, after_unplug: str | None, fire: bool = True) -> None:
    await rig.goto("10:30:00", fire=fire)
    await rig.set(energy=3.8, eto=1003.8)
    await rig.set(power=0)
    await rig.unplug()
    await rig.set(trx="null")
    if after_unplug is not None:
        await rig.set(energy=after_unplug)
    await rig.goto("10:45:00", fire=fire)


async def scenario_u(
    rig: Rig,
    trx: str,
    *,
    after_unplug: str | None,
    interrupted: bool,
    order: list[str] | None = None,
):
    """Research 027 scenario U: the car leaves at 10:30 with 3.8 kWh (down 10:22–10:45).

    ``order``: the charger's entities appear after setup in this order (restart-late).
    """
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(trx)
    await rig.goto("10:12:00")
    await rig.set(energy=1.0)
    await rig.goto("10:18:00")
    await rig.set(energy=3.0)
    await rig.goto("10:22:00")
    if not interrupted:
        await _guest_leaves(rig, after_unplug=after_unplug)
    else:

        async def during(rig: Rig) -> None:
            await _guest_leaves(rig, after_unplug=after_unplug, fire=False)

        if order is None:
            await restart(during=during)(rig)
        else:
            await restart("late", during=during, order=order)(rig)
    await rig.goto("10:46:00")


@pytest.mark.parametrize("intr", list(LATE_ORDERS))
async def test_rt25_car_left_counter_kept(hass, hass_storage, freezer, intr) -> None:
    """RT-25: energy from the final counter (D-47), cost recomputed (D-37), charger
    totals present, in every start order (D-49 waits for the energy entity when the
    plug comes first). Allowed differences: the end time is the start-up time; in the
    field order the lifetime counter is not yet valid when the energy decides, so the
    record has neither charger total (D-56: both or neither)."""
    rig = Rig(hass, freezer, hass_storage)
    await scenario_u(
        rig, TRX_PAUL, after_unplug=None, interrupted=intr != "none", order=LATE_ORDERS[intr]
    )
    assert _summary(rig) == [("Paul", 3.8, 9.5, None, 3.8)]
    (record,) = rig.records()
    totals = (record["charger_total_before_kwh"], record["charger_total_after_kwh"])
    assert totals == ((None, None) if intr == "restart-late-field" else (1000.0, 1003.8))


@pytest.mark.parametrize("intr", list(LATE_ORDERS))
async def test_rt25_spot_car_left_closes_the_stored_hour(hass, hass_storage, freezer, intr):
    """RT-25, spot (D-39): the remaining energy closes into the stored hour at its price."""
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await scenario_u(
        rig, TRX_PAUL, after_unplug=None, interrupted=intr != "none", order=LATE_ORDERS[intr]
    )
    (money,) = rig.money()
    assert money["details"] == [("10:00", 3.8, 1.0, False)]
    assert (money["energy"], money["cost"], money["details_kwh"]) == (3.8, 3.8, 3.8)


@pytest.mark.parametrize("intr", ["none", "restart-present"])
async def test_rt26_car_left_counter_below_the_snapshot(hass, hass_storage, freezer, intr):
    """RT-26: the counter reads 0.0 after the unplug: the snapshot's 3.0 kWh are kept and
    the session is not discarded. Allowed difference: the 0.8 kWh after the last
    snapshot (10:20) are unknown."""
    rig = Rig(hass, freezer, hass_storage)
    await scenario_u(rig, TRX_GFIX, after_unplug="0.0", interrupted=intr != "none")
    energy = 3.8 if intr == "none" else 3.0
    assert _summary(rig) == [("Gaest", energy, energy * 2.5, energy * 5.0, energy)]
    assert rig.snapshot() is None


async def test_rt17_new_user_charged_more_than_the_snapshot(hass, hass_storage, freezer) -> None:
    """RT-17: the new connection's counter (4.5 kWh) exceeds Paul's snapshot (3.0 kWh);
    Paul still gets only his snapshot's energy and the guest the whole counter."""

    async def guest_charges_more(rig: Rig) -> None:
        await _swap_users(rig, fire=False)
        await rig.set(energy=4.5)

    rig = Rig(hass, freezer, hass_storage)
    await scenario_h(rig, restart(during=guest_charges_more))
    # The scenario's later 2.0 kWh reading is a live counter reset for the guest.
    assert _summary(rig) == [("Paul", 3.0, 7.5, None, 3.0), ("Gaest", 4.5, 11.25, 22.5, 4.5)]
