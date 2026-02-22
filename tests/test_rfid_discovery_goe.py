"""Tests for rfid_discovery_goe.py -- go-e provider (FW detection, parsing, errors)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ev_charging_manager.rfid_discovery import DiscoveryError
from custom_components.ev_charging_manager.rfid_discovery_goe import GoeRfidDiscovery

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cards_array_response(
    cards: list[dict | None] | None = None,
) -> dict:
    """Build a mock /api/status?filter=cards JSON response."""
    if cards is None:
        # Default: 3 programmed cards, 7 empty
        cards = [
            {"name": "Paul", "energy": 0},
            {"name": "Petra", "energy": 12345},
            {"name": "", "energy": 0},
            {"name": "", "energy": 0},
            {"name": "", "energy": 0},
            {"name": "", "energy": 0},
            {"name": "", "energy": 0},
            {"name": "", "energy": 0},
            {"name": "", "energy": 0},
            {"name": "Gäster", "energy": 9900},
        ]
    return {"cards": cards}


def _make_flat_keys_response(slots: list[tuple[str, int, bool]]) -> dict:
    """Build a flat-keys response dict from (name, energy_wh, installed) tuples."""
    data: dict = {}
    for i, (name, energy, installed) in enumerate(slots):
        data[f"c{i}n"] = name
        data[f"c{i}e"] = energy
        data[f"c{i}i"] = installed
    return data


def _mock_session(responses: list[tuple[int, dict]]) -> MagicMock:
    """Create a mock aiohttp ClientSession returning successive responses.

    N3: Strict — raises AssertionError if more calls are made than expected.
    """
    session = MagicMock()
    call_count = [0]

    class _FakeResp:
        def __init__(self, status: int, body: dict) -> None:
            self.status = status
            self._body = body

        async def json(self) -> dict:
            return self._body

        async def __aenter__(self) -> "_FakeResp":
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

    def _get(url: str, **_: object) -> _FakeResp:
        idx = call_count[0]
        call_count[0] += 1
        if idx >= len(responses):
            raise AssertionError(
                f"Unexpected call #{idx + 1} to session.get({url!r}); "
                f"only {len(responses)} responses configured"
            )
        status, body = responses[idx]
        return _FakeResp(status, body)

    session.get = _get
    return session


# ---------------------------------------------------------------------------
# T008: FW <60 -- cards[] array format
# ---------------------------------------------------------------------------


async def test_fw59_get_programmed_cards_happy_path(hass: HomeAssistant) -> None:
    """FW 59.4: fetches cards[] and returns 10 DiscoveredCard objects."""
    provider = GoeRfidDiscovery("192.168.1.100")
    responses = [
        (200, {"fwv": "59.4"}),  # firmware check
        (200, _make_cards_array_response()),  # cards fetch
    ]
    session = _mock_session(responses)

    with patch(
        "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
        return_value=session,
    ):
        cards = await provider.get_programmed_cards(hass)

    assert len(cards) == 10
    assert cards[0].index == 0
    assert cards[0].name == "Paul"
    assert cards[0].energy_kwh == 0.0
    assert cards[0].is_programmed is True

    assert cards[1].name == "Petra"
    assert cards[1].energy_kwh == pytest.approx(12.345)
    assert cards[1].is_programmed is True

    # Slot 2 is empty name -> not programmed
    assert cards[2].is_programmed is False

    # Slot 9 is "Gäster"
    assert cards[9].name == "Gäster"
    assert cards[9].energy_kwh == pytest.approx(9.9)
    assert cards[9].is_programmed is True


async def test_fw59_energy_conversion_wh_to_kwh(hass: HomeAssistant) -> None:
    """Energy from charger is in Wh -- confirm conversion to kWh (/1000)."""
    provider = GoeRfidDiscovery("192.168.1.100")
    cards_data = [{"name": "Paul", "energy": 5000}] + [{"name": "", "energy": 0}] * 9
    responses = [
        (200, {"fwv": "59.0"}),
        (200, {"cards": cards_data}),
    ]
    session = _mock_session(responses)

    with patch(
        "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
        return_value=session,
    ):
        cards = await provider.get_programmed_cards(hass)

    assert cards[0].energy_kwh == pytest.approx(5.0)


async def test_fw59_unprogrammed_slots_false(hass: HomeAssistant) -> None:
    """Empty card slots have is_programmed=False."""
    provider = GoeRfidDiscovery("192.168.1.100")
    cards_data = [{"name": "", "energy": 0}] * 10
    responses = [
        (200, {"fwv": "50.0"}),
        (200, {"cards": cards_data}),
    ]
    session = _mock_session(responses)

    with patch(
        "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
        return_value=session,
    ):
        cards = await provider.get_programmed_cards(hass)

    assert all(not c.is_programmed for c in cards)


async def test_fw59_na_name_treated_as_none(hass: HomeAssistant) -> None:
    """Card name 'n/a' is treated as None (not programmed)."""
    provider = GoeRfidDiscovery("192.168.1.100")
    cards_data = [{"name": "n/a", "energy": 0}] + [{"name": "", "energy": 0}] * 9
    responses = [
        (200, {"fwv": "59.4"}),
        (200, {"cards": cards_data}),
    ]
    session = _mock_session(responses)

    with patch(
        "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
        return_value=session,
    ):
        cards = await provider.get_programmed_cards(hass)

    assert cards[0].name is None
    assert cards[0].is_programmed is False


async def test_energy_negative_returns_none(hass: HomeAssistant) -> None:
    """Negative energy value is converted to None (displayed as N/A in UI)."""
    provider = GoeRfidDiscovery("192.168.1.100")
    cards_data = [{"name": "Paul", "energy": -1}] + [{"name": "", "energy": 0}] * 9
    responses = [
        (200, {"fwv": "59.0"}),
        (200, {"cards": cards_data}),
    ]
    session = _mock_session(responses)

    with patch(
        "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
        return_value=session,
    ):
        cards = await provider.get_programmed_cards(hass)

    assert cards[0].energy_kwh is None


async def test_energy_too_large_returns_none(hass: HomeAssistant) -> None:
    """Energy > 1,000,000,000 Wh (= > 1,000,000 kWh) is converted to None."""
    provider = GoeRfidDiscovery("192.168.1.100")
    cards_data = [{"name": "Paul", "energy": 1_000_000_001_000}] + [{"name": "", "energy": 0}] * 9
    responses = [
        (200, {"fwv": "59.0"}),
        (200, {"cards": cards_data}),
    ]
    session = _mock_session(responses)

    with patch(
        "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
        return_value=session,
    ):
        cards = await provider.get_programmed_cards(hass)

    assert cards[0].energy_kwh is None


# ---------------------------------------------------------------------------
# T009: FW >=60 -- flat key format
# ---------------------------------------------------------------------------


async def test_fw60_flat_keys_happy_path(hass: HomeAssistant) -> None:
    """FW 60.3: fetches flat keys and returns 10 DiscoveredCard objects."""
    provider = GoeRfidDiscovery("192.168.1.100")

    slots = [
        ("Paul", 0, True),
        ("Petra", 12345, True),
    ] + [("", 0, False)] * 8

    responses = [
        (200, {"fwv": "60.3"}),  # firmware check
        (200, _make_flat_keys_response(slots)),  # flat keys fetch
    ]
    session = _mock_session(responses)

    with patch(
        "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
        return_value=session,
    ):
        cards = await provider.get_programmed_cards(hass)

    assert len(cards) == 10
    assert cards[0].name == "Paul"
    assert cards[0].energy_kwh == 0.0
    assert cards[0].is_programmed is True

    assert cards[1].name == "Petra"
    assert cards[1].energy_kwh == pytest.approx(12.345)
    assert cards[1].is_programmed is True

    assert cards[2].is_programmed is False


async def test_fw60_flat_keys_fallback_to_cards_array(hass: HomeAssistant) -> None:
    """FW >=60: if flat keys return empty, fall back to cards[] array format."""
    provider = GoeRfidDiscovery("192.168.1.100")

    # Flat keys response with no data (empty dict)
    empty_flat = {}
    cards_array = _make_cards_array_response()

    responses = [
        (200, {"fwv": "60.0"}),  # firmware
        (200, empty_flat),  # flat keys -> empty -> triggers fallback
        (200, cards_array),  # cards[] fallback
    ]
    session = _mock_session(responses)

    with patch(
        "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
        return_value=session,
    ):
        cards = await provider.get_programmed_cards(hass)

    assert len(cards) == 10
    # Should have gotten data from cards[] fallback
    assert cards[0].name == "Paul"
    assert cards[0].is_programmed is True


async def test_fw70_flat_keys_all_data(hass: HomeAssistant) -> None:
    """FW 70+ also uses flat keys."""
    provider = GoeRfidDiscovery("192.168.1.100")

    slots = [("Admin", 100000, True)] + [("", 0, False)] * 9
    responses = [
        (200, {"fwv": "70.1"}),
        (200, _make_flat_keys_response(slots)),
    ]
    session = _mock_session(responses)

    with patch(
        "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
        return_value=session,
    ):
        cards = await provider.get_programmed_cards(hass)

    assert cards[0].name == "Admin"
    assert cards[0].energy_kwh == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# T010: Error handling
# ---------------------------------------------------------------------------


async def test_network_timeout_raises_discovery_error(hass: HomeAssistant) -> None:
    """TimeoutError during firmware check raises DiscoveryError."""
    import asyncio

    provider = GoeRfidDiscovery("192.168.1.100")

    class _TimeoutCtx:
        async def __aenter__(self) -> None:
            raise asyncio.TimeoutError

        async def __aexit__(self, *_: object) -> None:
            pass

    session = MagicMock()
    session.get = lambda *a, **k: _TimeoutCtx()

    with (
        patch(
            "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
            return_value=session,
        ),
        pytest.raises(DiscoveryError, match="Cannot reach charger"),
    ):
        await provider.get_programmed_cards(hass)


async def test_connection_refused_raises_discovery_error(hass: HomeAssistant) -> None:
    """Connection error raises DiscoveryError."""
    import aiohttp

    provider = GoeRfidDiscovery("192.168.1.100")

    class _ConnErrCtx:
        async def __aenter__(self) -> None:
            raise aiohttp.ClientConnectionError("Connection refused")

        async def __aexit__(self, *_: object) -> None:
            pass

    session = MagicMock()
    session.get = lambda *a, **k: _ConnErrCtx()

    with (
        patch(
            "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
            return_value=session,
        ),
        pytest.raises(DiscoveryError),
    ):
        await provider.get_programmed_cards(hass)


async def test_unexpected_json_format_raises_discovery_error(hass: HomeAssistant) -> None:
    """Unexpected JSON (no 'cards' key) raises DiscoveryError."""
    provider = GoeRfidDiscovery("192.168.1.100")
    responses = [
        (200, {"fwv": "59.4"}),
        (200, {"unexpected_key": "value"}),  # missing 'cards'
    ]
    session = _mock_session(responses)

    with (
        patch(
            "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
            return_value=session,
        ),
        pytest.raises(DiscoveryError, match="Unexpected card data format"),
    ):
        await provider.get_programmed_cards(hass)


async def test_unparseable_firmware_raises_discovery_error(hass: HomeAssistant) -> None:
    """Non-numeric firmware version raises DiscoveryError."""
    provider = GoeRfidDiscovery("192.168.1.100")
    responses = [
        (200, {"fwv": "invalid.version"}),
    ]
    session = _mock_session(responses)

    with (
        patch(
            "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
            return_value=session,
        ),
        pytest.raises(DiscoveryError, match="Cannot parse firmware version"),
    ):
        await provider.get_programmed_cards(hass)


async def test_missing_fwv_key_raises_discovery_error(hass: HomeAssistant) -> None:
    """Missing fwv key in firmware response raises DiscoveryError."""
    provider = GoeRfidDiscovery("192.168.1.100")
    responses = [
        (200, {}),  # No fwv key
    ]
    session = _mock_session(responses)

    with (
        patch(
            "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
            return_value=session,
        ),
        pytest.raises(DiscoveryError, match="Unexpected firmware version format"),
    ):
        await provider.get_programmed_cards(hass)


async def test_http_error_status_raises_discovery_error(hass: HomeAssistant) -> None:
    """HTTP 500 response raises DiscoveryError."""
    provider = GoeRfidDiscovery("192.168.1.100")
    responses = [
        (500, {}),  # Server error on firmware check
    ]
    session = _mock_session(responses)

    with (
        patch(
            "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
            return_value=session,
        ),
        pytest.raises(DiscoveryError),
    ):
        await provider.get_programmed_cards(hass)


# ---------------------------------------------------------------------------
# T017: UID retrieval (US3)
# ---------------------------------------------------------------------------


async def test_get_last_rfid_uid_rde_enabled(hass: HomeAssistant) -> None:
    """rde=true with valid lri returns the UID string."""
    provider = GoeRfidDiscovery("192.168.1.100")
    responses = [
        (200, {"rde": True, "lri": "04b7d7b2c01690"}),
    ]
    session = _mock_session(responses)

    with patch(
        "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
        return_value=session,
    ):
        uid = await provider.get_last_rfid_uid(hass)

    assert uid == "04b7d7b2c01690"


async def test_get_last_rfid_uid_rde_disabled(hass: HomeAssistant) -> None:
    """rde=false returns None regardless of lri."""
    provider = GoeRfidDiscovery("192.168.1.100")
    responses = [
        (200, {"rde": False, "lri": None}),
    ]
    session = _mock_session(responses)

    with patch(
        "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
        return_value=session,
    ):
        uid = await provider.get_last_rfid_uid(hass)

    assert uid is None


async def test_get_last_rfid_uid_null_lri(hass: HomeAssistant) -> None:
    """rde=true but lri=null returns None."""
    provider = GoeRfidDiscovery("192.168.1.100")
    responses = [
        (200, {"rde": True, "lri": None}),
    ]
    session = _mock_session(responses)

    with patch(
        "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
        return_value=session,
    ):
        uid = await provider.get_last_rfid_uid(hass)

    assert uid is None


async def test_set_rfid_serial_reporting_success(hass: HomeAssistant) -> None:
    """set?rde=true succeeds when response confirms rde=true."""
    provider = GoeRfidDiscovery("192.168.1.100")
    responses = [
        (200, {"rde": True}),
    ]
    session = _mock_session(responses)

    with patch(
        "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
        return_value=session,
    ):
        result = await provider.set_rfid_serial_reporting(hass, enabled=True)

    assert result is True


async def test_set_rfid_serial_reporting_failure(hass: HomeAssistant) -> None:
    """HTTP 500 on set command returns False."""
    provider = GoeRfidDiscovery("192.168.1.100")
    responses = [
        (500, {}),
    ]
    session = _mock_session(responses)

    with patch(
        "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
        return_value=session,
    ):
        result = await provider.set_rfid_serial_reporting(hass, enabled=True)

    assert result is False


# ---------------------------------------------------------------------------
# B7: set_rfid_serial_reporting(enabled=False)
# ---------------------------------------------------------------------------


async def test_set_rfid_serial_reporting_disable(hass: HomeAssistant) -> None:
    """set?rde=false succeeds when response confirms rde=false."""
    provider = GoeRfidDiscovery("192.168.1.100")
    responses = [
        (200, {"rde": False}),
    ]
    session = _mock_session(responses)

    with patch(
        "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
        return_value=session,
    ):
        result = await provider.set_rfid_serial_reporting(hass, enabled=False)

    assert result is True


# ---------------------------------------------------------------------------
# N4: lri as integer returns None (not a string)
# ---------------------------------------------------------------------------


async def test_get_last_rfid_uid_lri_integer_returns_none(hass: HomeAssistant) -> None:
    """lri as integer (not string) returns None."""
    provider = GoeRfidDiscovery("192.168.1.100")
    responses = [
        (200, {"rde": True, "lri": 12345}),
    ]
    session = _mock_session(responses)

    with patch(
        "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
        return_value=session,
    ):
        uid = await provider.get_last_rfid_uid(hass)

    assert uid is None


# ---------------------------------------------------------------------------
# N5: "na" card name treated as unprogrammed
# ---------------------------------------------------------------------------


async def test_fw59_na_lowercase_name_treated_as_none(hass: HomeAssistant) -> None:
    """Card name 'na' (lowercase, without slash) is treated as None."""
    provider = GoeRfidDiscovery("192.168.1.100")
    cards_data = [{"name": "na", "energy": 0}] + [{"name": "", "energy": 0}] * 9
    responses = [
        (200, {"fwv": "59.4"}),
        (200, {"cards": cards_data}),
    ]
    session = _mock_session(responses)

    with patch(
        "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
        return_value=session,
    ):
        cards = await provider.get_programmed_cards(hass)

    assert cards[0].name is None
    assert cards[0].is_programmed is False


# ---------------------------------------------------------------------------
# B7: Short cards[] array (fewer than 10 elements)
# ---------------------------------------------------------------------------


async def test_fw59_short_cards_array_padded_to_10(hass: HomeAssistant) -> None:
    """Cards array with fewer than 10 elements is padded to 10 DiscoveredCards."""
    provider = GoeRfidDiscovery("192.168.1.100")
    # Only 3 elements in the array
    cards_data = [
        {"name": "Paul", "energy": 5000},
        {"name": "Petra", "energy": 0},
        {"name": "", "energy": 0},
    ]
    responses = [
        (200, {"fwv": "55.0"}),
        (200, {"cards": cards_data}),
    ]
    session = _mock_session(responses)

    with patch(
        "custom_components.ev_charging_manager.rfid_discovery_goe.async_get_clientsession",
        return_value=session,
    ):
        cards = await provider.get_programmed_cards(hass)

    assert len(cards) == 10
    assert cards[0].name == "Paul"
    assert cards[0].is_programmed is True
    assert cards[1].name == "Petra"
    assert cards[1].is_programmed is True
    # Slots beyond the array are unprogrammed
    for i in range(3, 10):
        assert cards[i].is_programmed is False
        assert cards[i].name is None
