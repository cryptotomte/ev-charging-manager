"""Story 07 (PR-22 revision 2026-05-19) — passive notification on unmapped RFID.

Tests cover TC-012 through TC-016 in their REVISED form:

  - TC-012: notify_unmapped_rfid=True → persistent_notification.async_create
            called with deterministic ID + EVENT_UNKNOWN_RFID_DETECTED fired
            + zero HTTP calls to the charger.
  - TC-013: notify_unmapped_rfid=False → NO notification, but event still fires.
  - TC-014: dispatcher signal SIGNAL_RFID_MAPPING_ADDED triggers
            persistent_notification.async_dismiss for the matching index.
  - TC-015: Session still completes normally with user_name="Unknown" /
            user_type="unknown" regardless of notification setting.
  - TC-016: Toggling the option mid-session does NOT retroactively suppress an
            already-displayed notification (snapshot at SESSION_START).

The engine must never make an HTTP call to the charger (FR-023, Constitution §I).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.const import (
    CONF_CHARGING_IDLE_TIMEOUT_MIN,
    CONF_DISCONNECT_GRACE_MIN,
    CONF_NOTIFY_UNMAPPED_RFID,
    DEFAULT_CHARGING_IDLE_TIMEOUT_MIN,
    DOMAIN,
    EVENT_UNKNOWN_RFID_DETECTED,
    NOTIFICATION_ID_UNKNOWN_RFID,
    SIGNAL_RFID_MAPPING_ADDED,
)
from custom_components.ev_charging_manager.session_engine_v2 import (
    PlugAnchoredSessionEngine,
)
from tests.conftest import (
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    MOCK_POWER_ENTITY,
    MOCK_TRX_ENTITY,
)

MOCK_PLUG_ENTITY = "binary_sensor.goe_abc123_car_0"
MOCK_CABLE_LOCK_ENTITY = "sensor.goe_abc123_cus_value"

_BASE_OPTIONS = {
    "plug_entity": MOCK_PLUG_ENTITY,
    "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
    CONF_CHARGING_IDLE_TIMEOUT_MIN: DEFAULT_CHARGING_IDLE_TIMEOUT_MIN,
    CONF_DISCONNECT_GRACE_MIN: 10,
}


async def _setup_engine(
    hass: HomeAssistant, notify_enabled: bool = True
) -> tuple[MockConfigEntry, PlugAnchoredSessionEngine]:
    """Create a goe_gemini config entry wired to PlugAnchoredSessionEngine."""
    options = dict(_BASE_OPTIONS)
    options[CONF_NOTIFY_UNMAPPED_RFID] = notify_enabled

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options=options,
        title="Test go-e Charger",
    )

    # Set baseline entity states (idle, plug off).
    hass.states.async_set(MOCK_PLUG_ENTITY, "off")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    hass.states.async_set(MOCK_POWER_ENTITY, "0.0")

    entry.add_to_hass(hass)
    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert isinstance(engine, PlugAnchoredSessionEngine)
    return entry, engine


async def _trigger_unmapped_rfid(hass: HomeAssistant, unmapped_trx: str = "2") -> None:
    """Plug in with an RFID slot that has no mapping → unknown user."""
    # No mapping subentries were added, so any non-null trx will be "unmapped".
    hass.states.async_set(MOCK_TRX_ENTITY, unmapped_trx)
    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# TC-012 (REVISED): notification + event, zero HTTP
# ---------------------------------------------------------------------------


async def test_tc012_unmapped_rfid_creates_notification_and_fires_event(
    hass: HomeAssistant,
) -> None:
    """TC-012: unmapped RFID with notify_unmapped_rfid=True must.

    - call persistent_notification.async_create with deterministic ID
    - fire EVENT_UNKNOWN_RFID_DETECTED
    - NOT make any HTTP call (Constitution §I)
    """
    fired_events: list = []

    @callback_listener
    def _capture(event) -> None:  # noqa: ANN001 - test helper
        fired_events.append(event)

    hass.bus.async_listen(EVENT_UNKNOWN_RFID_DETECTED, _capture)

    entry, _engine = await _setup_engine(hass, notify_enabled=True)

    with (
        patch(
            "custom_components.ev_charging_manager.session_engine_v2."
            "persistent_notification.async_create"
        ) as mock_create,
        patch("aiohttp.ClientSession.request", new_callable=AsyncMock) as mock_http,
    ):
        await _trigger_unmapped_rfid(hass, unmapped_trx="3")  # trx=3 → rfid_index=2

        assert mock_create.called, "persistent_notification.async_create must be invoked"
        kwargs = mock_create.call_args.kwargs
        expected_id = NOTIFICATION_ID_UNKNOWN_RFID.format("2")
        assert kwargs["notification_id"] == expected_id, (
            f"notification_id should be {expected_id!r}, got {kwargs['notification_id']!r}"
        )
        assert "Unknown" in kwargs["message"], (
            "notification message must mention 'Unknown' bucket warning"
        )

        # FR-023: no HTTP request to the charger.
        assert not mock_http.called, "no HTTP call may be made (Constitution §I)"

    # Event was fired regardless of notification.
    assert any(e.data.get("rfid_index") == 2 for e in fired_events), (
        "EVENT_UNKNOWN_RFID_DETECTED must fire with rfid_index=2"
    )


def callback_listener(func):
    """Trivial decorator placeholder — hass.bus.async_listen accepts plain callables."""
    return func


# ---------------------------------------------------------------------------
# TC-013 (REVISED): notify_unmapped_rfid=False → no notification, event still fires
# ---------------------------------------------------------------------------


async def test_tc013_disabled_option_suppresses_notification_but_event_fires(
    hass: HomeAssistant,
) -> None:
    """TC-013: notify_unmapped_rfid=False suppresses notification but event still fires."""
    fired_events: list = []
    hass.bus.async_listen(EVENT_UNKNOWN_RFID_DETECTED, fired_events.append)

    entry, _engine = await _setup_engine(hass, notify_enabled=False)

    with patch(
        "custom_components.ev_charging_manager.session_engine_v2."
        "persistent_notification.async_create"
    ) as mock_create:
        await _trigger_unmapped_rfid(hass, unmapped_trx="3")

        assert not mock_create.called, (
            "notify_unmapped_rfid=False must suppress persistent notification"
        )

    assert any(e.data.get("rfid_index") == 2 for e in fired_events), (
        "EVENT_UNKNOWN_RFID_DETECTED must still fire when notifications disabled"
    )


# ---------------------------------------------------------------------------
# TC-014 (REVISED): mapping-added dispatcher signal → notification dismissed
# ---------------------------------------------------------------------------


async def test_tc014_mapping_added_dismisses_notification(hass: HomeAssistant) -> None:
    """TC-014: when SIGNAL_RFID_MAPPING_ADDED fires for the matching index,
    persistent_notification.async_dismiss must be called with the same ID.
    """
    entry, engine = await _setup_engine(hass, notify_enabled=True)

    with patch(
        "custom_components.ev_charging_manager.session_engine_v2."
        "persistent_notification.async_create"
    ):
        await _trigger_unmapped_rfid(hass, unmapped_trx="3")

    # Engine should now have one active unmapped notification (rfid_index=2).
    assert 2 in engine._active_unmapped_notifications.values(), (
        "engine should track the active unmapped notification by rfid_index"
    )

    with patch(
        "custom_components.ev_charging_manager.session_engine_v2."
        "persistent_notification.async_dismiss"
    ) as mock_dismiss:
        signal = SIGNAL_RFID_MAPPING_ADDED.format(entry.entry_id)
        async_dispatcher_send(hass, signal, 2)
        await hass.async_block_till_done()

        assert mock_dismiss.called, (
            "mapping-added signal must trigger persistent_notification.async_dismiss"
        )
        expected_id = NOTIFICATION_ID_UNKNOWN_RFID.format("2")
        # async_dismiss called positionally: (hass, notification_id)
        call_args = mock_dismiss.call_args
        passed_id = (
            call_args.args[1]
            if len(call_args.args) > 1
            else call_args.kwargs.get("notification_id")
        )
        assert passed_id == expected_id, f"dismiss should target {expected_id!r}, got {passed_id!r}"

    # Engine state cleaned up after dismiss.
    assert 2 not in engine._active_unmapped_notifications.values(), (
        "active notifications dict must be cleared after dismiss"
    )


# ---------------------------------------------------------------------------
# TC-015 (REVISED): session still completes normally regardless of notification
# ---------------------------------------------------------------------------


async def test_tc015_session_completes_unknown_attribution_regardless(
    hass: HomeAssistant,
) -> None:
    """TC-015: the session must record user_name='Unknown' / user_type='unknown'
    regardless of whether a notification was shown. No charger-control side effects.
    """
    entry, engine = await _setup_engine(hass, notify_enabled=True)

    with patch(
        "custom_components.ev_charging_manager.session_engine_v2."
        "persistent_notification.async_create"
    ):
        await _trigger_unmapped_rfid(hass, unmapped_trx="3")

    assert engine.active_session is not None
    assert engine.active_session.user_name == "Unknown"
    assert engine.active_session.user_type == "unknown"
    # And critically — the session is not blocked / not force-stopped.
    # FR-N03: session continues normally.
    assert engine.state.value == "tracking", (
        "session should remain in TRACKING state — no force-stop"
    )


# ---------------------------------------------------------------------------
# TC-016 (REVISED): toggling option mid-session does not retroactively affect
# the already-dispatched notification (snapshot at SESSION_START — Constitution §II).
# ---------------------------------------------------------------------------


async def test_tc016_mid_session_option_change_does_not_revoke_notification(
    hass: HomeAssistant,
) -> None:
    """TC-016: toggling notify_unmapped_rfid mid-session does NOT dismiss the
    notification that was already shown. The user must dismiss it themselves
    (or by adding a mapping). Snapshot principle: notification dispatch
    decision is taken at SESSION_START.
    """
    entry, engine = await _setup_engine(hass, notify_enabled=True)

    with patch(
        "custom_components.ev_charging_manager.session_engine_v2."
        "persistent_notification.async_create"
    ) as mock_create:
        await _trigger_unmapped_rfid(hass, unmapped_trx="3")

        # Notification was created once.
        assert mock_create.call_count == 1

    # Now flip the option (simulates a user disabling notifications mid-charge).
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_NOTIFY_UNMAPPED_RFID: False}
    )
    await hass.async_block_till_done()

    # The engine must NOT spontaneously dismiss the notification just because
    # the option flipped. (Dismissal happens only on a real mapping-add signal.)
    with patch(
        "custom_components.ev_charging_manager.session_engine_v2."
        "persistent_notification.async_dismiss"
    ) as mock_dismiss:
        await hass.async_block_till_done()
        assert not mock_dismiss.called, (
            "flipping the option must NOT auto-dismiss an in-flight notification"
        )

    # Notification tracking state preserved (rfid_index=2 still present).
    assert 2 in engine._active_unmapped_notifications.values()
