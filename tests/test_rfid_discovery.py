"""Tests for rfid_discovery.py — protocol, data types, and helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ev_charging_manager.rfid_discovery import (
    DiscoveredCard,
    DiscoveryError,
    async_get_charger_host,
    get_discovery_provider,
    suggest_user_for_card,
)

# ---------------------------------------------------------------------------
# T007: DiscoveredCard construction and validation
# ---------------------------------------------------------------------------


def test_discovered_card_valid() -> None:
    """DiscoveredCard accepts valid fields."""
    card = DiscoveredCard(index=0, name="Paul", energy_kwh=12.3, is_programmed=True)
    assert card.index == 0
    assert card.name == "Paul"
    assert card.energy_kwh == 12.3
    assert card.is_programmed is True


def test_discovered_card_none_name() -> None:
    """DiscoveredCard accepts None name."""
    card = DiscoveredCard(index=5, name=None, energy_kwh=0.0, is_programmed=True)
    assert card.name is None


def test_discovered_card_none_energy() -> None:
    """DiscoveredCard accepts None energy_kwh (invalid energy displayed as N/A)."""
    card = DiscoveredCard(index=2, name="Petra", energy_kwh=None, is_programmed=True)
    assert card.energy_kwh is None


def test_discovered_card_zero_energy() -> None:
    """DiscoveredCard accepts zero energy_kwh."""
    card = DiscoveredCard(index=3, name="Guest", energy_kwh=0.0, is_programmed=True)
    assert card.energy_kwh == 0.0


def test_discovered_card_unprogrammed() -> None:
    """DiscoveredCard with is_programmed=False is valid."""
    card = DiscoveredCard(index=9, name=None, energy_kwh=None, is_programmed=False)
    assert card.is_programmed is False


def test_discovered_card_index_out_of_range_low() -> None:
    """DiscoveredCard rejects index < 0."""
    with pytest.raises(ValueError, match="Card index must be 0-9"):
        DiscoveredCard(index=-1, name=None, energy_kwh=None, is_programmed=False)


def test_discovered_card_index_out_of_range_high() -> None:
    """DiscoveredCard rejects index > 9."""
    with pytest.raises(ValueError, match="Card index must be 0-9"):
        DiscoveredCard(index=10, name=None, energy_kwh=None, is_programmed=False)


def test_discovered_card_negative_energy() -> None:
    """DiscoveredCard rejects negative energy_kwh."""
    with pytest.raises(ValueError, match="energy_kwh must be >= 0.0"):
        DiscoveredCard(index=0, name=None, energy_kwh=-1.0, is_programmed=False)


# ---------------------------------------------------------------------------
# T007: DiscoveryError
# ---------------------------------------------------------------------------


def test_discovery_error_has_message() -> None:
    """DiscoveryError carries a user-friendly message attribute."""
    exc = DiscoveryError("Cannot reach charger. Check network connection.")
    assert exc.message == "Cannot reach charger. Check network connection."
    assert str(exc) == "Cannot reach charger. Check network connection."


def test_discovery_error_is_exception() -> None:
    """DiscoveryError is a proper Exception subclass."""
    exc = DiscoveryError("test")
    assert isinstance(exc, Exception)


# ---------------------------------------------------------------------------
# T007: suggest_user_for_card
# ---------------------------------------------------------------------------


USERS = [
    {"user_id": "uid_paul", "user_name": "Paul"},
    {"user_id": "uid_petra", "user_name": "Petra"},
    {"user_id": "uid_guest", "user_name": "Guest"},
]


def test_suggest_user_exact_match() -> None:
    """Exact name match returns the correct user_id."""
    result = suggest_user_for_card("Paul", USERS)
    assert result == "uid_paul"


def test_suggest_user_case_insensitive() -> None:
    """Case-insensitive match: 'paul' matches 'Paul'."""
    result = suggest_user_for_card("paul", USERS)
    assert result == "uid_paul"


def test_suggest_user_case_insensitive_upper() -> None:
    """Case-insensitive match: 'PETRA' matches 'Petra'."""
    result = suggest_user_for_card("PETRA", USERS)
    assert result == "uid_petra"


def test_suggest_user_no_match() -> None:
    """No match returns None."""
    result = suggest_user_for_card("Office Card", USERS)
    assert result is None


def test_suggest_user_none_name() -> None:
    """None card_name returns None."""
    result = suggest_user_for_card(None, USERS)
    assert result is None


def test_suggest_user_empty_name() -> None:
    """Empty card_name returns None."""
    result = suggest_user_for_card("", USERS)
    assert result is None


def test_suggest_user_empty_users() -> None:
    """Empty user list returns None."""
    result = suggest_user_for_card("Paul", [])
    assert result is None


def test_suggest_user_partial_match_no_result() -> None:
    """Partial match does NOT count — must be exact (case-insensitive)."""
    result = suggest_user_for_card("Pau", USERS)
    assert result is None


# ---------------------------------------------------------------------------
# T007: async_get_charger_host
# ---------------------------------------------------------------------------


async def test_async_get_charger_host_success(hass: HomeAssistant) -> None:
    """Happy path: entity found, config entry has 'host'."""
    mock_entity = MagicMock()
    mock_entity.config_entry_id = "entry_abc"

    mock_entry = MagicMock()
    mock_entry.data = {"host": "192.168.1.100"}

    with (
        patch("custom_components.ev_charging_manager.rfid_discovery.er.async_get") as mock_er,
        patch.object(
            hass.config_entries,
            "async_get_entry",
            return_value=mock_entry,
        ),
    ):
        mock_er.return_value.async_get.return_value = mock_entity
        result = await async_get_charger_host(hass, "sensor.goe_abc123_car_value")

    assert result == "192.168.1.100"


async def test_async_get_charger_host_entity_not_found(hass: HomeAssistant) -> None:
    """Entity not in registry returns None."""
    with patch("custom_components.ev_charging_manager.rfid_discovery.er.async_get") as mock_er:
        mock_er.return_value.async_get.return_value = None
        result = await async_get_charger_host(hass, "sensor.unknown")

    assert result is None


async def test_async_get_charger_host_no_config_entry_id(hass: HomeAssistant) -> None:
    """Entity has no config_entry_id returns None."""
    mock_entity = MagicMock()
    mock_entity.config_entry_id = None

    with patch("custom_components.ev_charging_manager.rfid_discovery.er.async_get") as mock_er:
        mock_er.return_value.async_get.return_value = mock_entity
        result = await async_get_charger_host(hass, "sensor.goe_abc123_car_value")

    assert result is None


async def test_async_get_charger_host_entry_not_found(hass: HomeAssistant) -> None:
    """Config entry not found in registry returns None."""
    mock_entity = MagicMock()
    mock_entity.config_entry_id = "entry_abc"

    with (
        patch("custom_components.ev_charging_manager.rfid_discovery.er.async_get") as mock_er,
        patch.object(
            hass.config_entries,
            "async_get_entry",
            return_value=None,
        ),
    ):
        mock_er.return_value.async_get.return_value = mock_entity
        result = await async_get_charger_host(hass, "sensor.goe_abc123_car_value")

    assert result is None


async def test_async_get_charger_host_no_host_key(hass: HomeAssistant) -> None:
    """Config entry with no 'host' key returns None."""
    mock_entity = MagicMock()
    mock_entity.config_entry_id = "entry_abc"

    mock_entry = MagicMock()
    mock_entry.data = {}  # No "host" key

    with (
        patch("custom_components.ev_charging_manager.rfid_discovery.er.async_get") as mock_er,
        patch.object(
            hass.config_entries,
            "async_get_entry",
            return_value=mock_entry,
        ),
    ):
        mock_er.return_value.async_get.return_value = mock_entity
        result = await async_get_charger_host(hass, "sensor.goe_abc123_car_value")

    assert result is None


# ---------------------------------------------------------------------------
# T026: Provider registry and lookup (US5)
# ---------------------------------------------------------------------------


def test_get_discovery_provider_goe() -> None:
    """go-e profile returns GoeRfidDiscovery instance."""
    from custom_components.ev_charging_manager.rfid_discovery_goe import GoeRfidDiscovery

    profile = {
        "rfid_discovery": {
            "provider": "goe",
            "fw_detection_filter": "fwv",
            "cards_array_filter": "cards",
            "flat_keys_format": True,
        }
    }
    provider = get_discovery_provider(profile, "192.168.1.100")
    assert isinstance(provider, GoeRfidDiscovery)
    assert provider.supports_discovery() is True


def test_get_discovery_provider_none_profile() -> None:
    """Profile with rfid_discovery=None returns None (manual fallback)."""
    profile = {"rfid_discovery": None}
    result = get_discovery_provider(profile, "192.168.1.100")
    assert result is None


def test_get_discovery_provider_missing_block() -> None:
    """Profile without rfid_discovery key returns None."""
    profile = {}
    result = get_discovery_provider(profile, "192.168.1.100")
    assert result is None


def test_get_discovery_provider_unknown_provider() -> None:
    """Unknown provider key returns None."""
    profile = {
        "rfid_discovery": {
            "provider": "unknown_brand_xyz",
        }
    }
    result = get_discovery_provider(profile, "192.168.1.100")
    assert result is None


def test_goe_provider_implements_protocol() -> None:
    """GoeRfidDiscovery has all required protocol methods."""
    from custom_components.ev_charging_manager.rfid_discovery_goe import GoeRfidDiscovery

    provider = GoeRfidDiscovery("192.168.1.1")
    assert hasattr(provider, "supports_discovery")
    assert hasattr(provider, "get_programmed_cards")
    assert hasattr(provider, "get_last_rfid_uid")
    assert hasattr(provider, "set_rfid_serial_reporting")
    assert provider.supports_discovery() is True
