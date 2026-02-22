"""RFID discovery protocol, data types, and helpers for EV Charging Manager."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from homeassistant.helpers import entity_registry as er

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


@dataclass
class DiscoveredCard:
    """A single RFID card slot read from the charger during discovery.

    Not persisted — used only within the config flow session.
    """

    index: int
    name: str | None
    energy_kwh: float | None
    is_programmed: bool

    def __post_init__(self) -> None:
        """Validate field invariants."""
        if not (0 <= self.index <= 9):
            raise ValueError(f"Card index must be 0-9, got {self.index}")
        if self.energy_kwh is not None and self.energy_kwh < 0.0:
            raise ValueError(f"energy_kwh must be >= 0.0 when set, got {self.energy_kwh}")


class DiscoveryError(Exception):
    """Raised when RFID card discovery fails (network error, parse error, etc.)."""

    def __init__(self, message: str) -> None:
        """Initialise with a user-friendly error message."""
        super().__init__(message)
        self.message = message


class RfidDiscoveryProvider(Protocol):
    """Protocol that charger-specific implementations fulfill for RFID discovery."""

    def supports_discovery(self) -> bool:
        """Return True if this charger can enumerate programmed RFID cards."""
        ...

    async def get_programmed_cards(self, hass: HomeAssistant) -> list[DiscoveredCard]:
        """Read programmed cards from the charger.

        Returns all 10 card slots. Caller filters by is_programmed=True.
        Raises DiscoveryError on network/parse failures.
        """
        ...

    async def get_last_rfid_uid(self, hass: HomeAssistant) -> str | None:
        """Read the last-scanned RFID UID from the charger.

        Returns the UID string, or None if unavailable (rde not enabled
        or no card scanned recently).
        """
        ...

    async def set_rfid_serial_reporting(self, hass: HomeAssistant, enabled: bool) -> bool:
        """Enable or disable RFID serial reporting (rde) on the charger.

        Returns True if the command succeeded, False otherwise.
        """
        ...


async def async_get_charger_host(
    hass: HomeAssistant,
    entity_id: str,
) -> str | None:
    """Look up the charger host from the go-e HA integration config entry.

    Lookup chain: entity_id → entity registry → config_entry_id → config entry → data["host"]

    Args:
        hass: Home Assistant instance.
        entity_id: Any entity from our config (e.g., car_status_entity).

    Returns:
        Charger hostname/IP string, or None if lookup fails at any step.
    """
    entity_registry = er.async_get(hass)
    entity_entry = entity_registry.async_get(entity_id)
    if entity_entry is None or entity_entry.config_entry_id is None:
        return None

    config_entry = hass.config_entries.async_get_entry(entity_entry.config_entry_id)
    if config_entry is None:
        return None

    return config_entry.data.get("host")


def suggest_user_for_card(
    card_name: str | None,
    users: list[dict],
) -> str | None:
    """Return user_id of best name match for a discovered card, or None.

    Case-insensitive exact match on card_name vs user_name.
    Returns None if no match or card_name is None/empty.

    Args:
        card_name: Name string from the charger card slot (may be None).
        users: List of user dicts, each with "user_id" and "user_name" keys.

    Returns:
        user_id of matched user, or None if no match.
    """
    if not card_name:
        return None
    card_name_lower = card_name.lower()
    for user in users:
        if user.get("user_name", "").lower() == card_name_lower:
            return user.get("user_id")
    return None


def get_discovery_provider(
    profile: dict,
    host: str,
) -> RfidDiscoveryProvider | None:
    """Return an instantiated discovery provider for the given charger profile.

    Looks up the provider identifier from the profile's rfid_discovery block
    and returns the corresponding provider instance. Returns None if no
    discovery is configured or the provider is unknown.

    Args:
        profile: Charger profile dict (from CHARGER_PROFILES).
        host: Charger hostname/IP to pass to the provider constructor.

    Returns:
        Instantiated RfidDiscoveryProvider, or None for manual fallback.
    """
    # Deferred imports: rfid_discovery_goe imports DiscoveredCard/DiscoveryError from this
    # module, so a top-level import would create a circular dependency.
    from .const import PROVIDER_GOE  # noqa: PLC0415
    from .rfid_discovery_goe import GoeRfidDiscovery  # noqa: PLC0415

    rfid_discovery_config = profile.get("rfid_discovery")
    if rfid_discovery_config is None:
        return None

    provider_key = rfid_discovery_config.get("provider")
    registry: dict[str, type] = {
        PROVIDER_GOE: GoeRfidDiscovery,
    }
    provider_class = registry.get(provider_key)
    if provider_class is None:
        return None

    return provider_class(host)
