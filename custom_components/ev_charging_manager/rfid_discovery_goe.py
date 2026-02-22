"""go-e charger RFID discovery provider for EV Charging Manager."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DISCOVERY_TIMEOUT,
    GOE_FILTER_CARDS,
    GOE_FILTER_FWV,
    GOE_FILTER_LRI_RDE,
    GOE_FLAT_KEY_SUFFIX_ENERGY,
    GOE_FLAT_KEY_SUFFIX_INSTALLED,
    GOE_FLAT_KEY_SUFFIX_NAME,
    GOE_FLAT_KEYS_FW_THRESHOLD,
    MAX_CARD_SLOTS,
    MAX_ENERGY_KWH,
)
from .rfid_discovery import DiscoveredCard, DiscoveryError

if TYPE_CHECKING:
    import aiohttp
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# N1: Pre-computed flat key filter string (c0n,c0e,c0i,c1n,...,c9n,c9e,c9i)
_GOE_FLAT_KEY_FILTER = ",".join(
    f"c{i}{suffix}"
    for i in range(MAX_CARD_SLOTS)
    for suffix in (
        GOE_FLAT_KEY_SUFFIX_NAME,
        GOE_FLAT_KEY_SUFFIX_ENERGY,
        GOE_FLAT_KEY_SUFFIX_INSTALLED,
    )
)


class GoeRfidDiscovery:
    """RFID discovery provider for go-e chargers.

    Supports both firmware formats:
    - FW < 60: cards[] array format (/api/status?filter=cards)
    - FW >= 60: flat key format (/api/status?filter=c0n,c0e,c0i,...,c9n,c9e,c9i)
    """

    def __init__(self, host: str) -> None:
        """Initialise with the charger hostname or IP address."""
        self._host = host

    def supports_discovery(self) -> bool:
        """Return True -- go-e chargers always support discovery via direct HTTP API."""
        return True

    async def get_programmed_cards(self, hass: HomeAssistant) -> list[DiscoveredCard]:
        """Read all 10 card slots from the charger using firmware-appropriate format.

        Detects firmware version first, then selects the correct data format.
        FW >= 60 tries flat keys first and falls back to cards[] array if empty.

        Returns:
            List of 10 DiscoveredCard objects (one per slot, including unprogrammed).

        Raises:
            DiscoveryError: On network timeout, connection error, or parse failure.
        """
        session = async_get_clientsession(hass)
        fw_major = await self._get_firmware_major(session)

        if fw_major >= GOE_FLAT_KEYS_FW_THRESHOLD:
            # Try flat keys first (FW >= 60)
            cards = await self._fetch_flat_keys(session)
            if not cards:
                # Fall back to cards[] array format
                _LOGGER.debug(
                    "go-e %s: flat keys returned no data on FW %s, falling back to cards[]",
                    self._host,
                    fw_major,
                )
                cards = await self._fetch_cards_array(session)
        else:
            cards = await self._fetch_cards_array(session)

        return cards

    async def get_last_rfid_uid(self, hass: HomeAssistant) -> str | None:
        """Read the last-scanned RFID UID from the charger.

        Returns the UID string if rde=true and lri is set, otherwise None.
        """
        session = async_get_clientsession(hass)
        url = f"http://{self._host}/api/status?filter={GOE_FILTER_LRI_RDE}"
        try:
            async with asyncio.timeout(DISCOVERY_TIMEOUT):
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return None
                    data: dict[str, Any] = await resp.json()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("go-e %s: failed to fetch last RFID UID", self._host, exc_info=True)
            return None

        if not data.get("rde", False):
            return None
        lri = data.get("lri")
        if lri and isinstance(lri, str):
            return lri
        return None

    async def set_rfid_serial_reporting(self, hass: HomeAssistant, enabled: bool) -> bool:
        """Enable or disable RFID serial reporting (rde) on the charger.

        Returns True if the command succeeded, False otherwise.
        """
        session = async_get_clientsession(hass)
        value = "true" if enabled else "false"
        url = f"http://{self._host}/api/set?rde={value}"
        try:
            async with asyncio.timeout(DISCOVERY_TIMEOUT):
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return False
                    result: dict[str, Any] = await resp.json()
                    return result.get("rde") == enabled
        except Exception:  # noqa: BLE001
            _LOGGER.debug("go-e %s: failed to set rde=%s", self._host, value, exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _fetch_api(
        self, session: aiohttp.ClientSession, filter_str: str, context: str
    ) -> dict[str, Any]:
        """Fetch JSON from the charger API with standard error handling.

        Args:
            session: aiohttp client session.
            filter_str: The filter query parameter value.
            context: Human-readable context for error messages.

        Returns:
            Parsed JSON response dict.

        Raises:
            DiscoveryError: On timeout, connection error, or non-200 status.
        """
        url = f"http://{self._host}/api/status?filter={filter_str}"
        try:
            async with asyncio.timeout(DISCOVERY_TIMEOUT):
                async with session.get(url) as resp:
                    if resp.status != 200:
                        raise DiscoveryError(f"Charger returned HTTP {resp.status} for {context}")
                    return await resp.json()
        except DiscoveryError:
            raise
        except TimeoutError as exc:
            raise DiscoveryError("Cannot reach charger. Check network connection.") from exc
        except Exception as exc:
            raise DiscoveryError(f"Connection error: {exc}") from exc

    async def _get_firmware_major(self, session: aiohttp.ClientSession) -> int:
        """Fetch and parse the firmware major version number.

        Raises:
            DiscoveryError: If the request fails or version cannot be parsed.
        """
        data = await self._fetch_api(session, GOE_FILTER_FWV, "firmware version query")

        fwv = data.get("fwv")
        if not fwv or not isinstance(fwv, str):
            raise DiscoveryError("Unexpected firmware version format from charger.")
        try:
            major_str = fwv.split(".")[0]
            return int(major_str)
        except (ValueError, IndexError) as exc:
            raise DiscoveryError(f"Cannot parse firmware version '{fwv}'.") from exc

    async def _fetch_cards_array(self, session: aiohttp.ClientSession) -> list[DiscoveredCard]:
        """Fetch card data using the FW <60 cards[] array format.

        Raises:
            DiscoveryError: On network or parse failure.
        """
        data = await self._fetch_api(session, GOE_FILTER_CARDS, "card data")

        cards_raw = data.get("cards")
        if not isinstance(cards_raw, list):
            _LOGGER.warning(
                "go-e %s: unexpected cards[] format: %s",
                self._host,
                str(data)[:200],
            )
            raise DiscoveryError("Unexpected card data format from charger.")

        return self._parse_cards_array(cards_raw)

    def _parse_cards_array(self, cards_raw: list) -> list[DiscoveredCard]:
        """Parse the cards[] array into DiscoveredCard objects.

        Each element is a dict with keys: name, energy, cardId (programmed flag inferred
        from whether name/energy is set). go-e uses an array of up to 10 slots.
        """
        cards: list[DiscoveredCard] = []
        for i in range(MAX_CARD_SLOTS):
            # N2: Collapsed duplicate empty-slot branches
            if i < len(cards_raw) and isinstance(cards_raw[i], dict):
                slot = cards_raw[i]
                name = self._parse_card_name(slot.get("name"))
                energy_wh = slot.get("energy", 0)
                energy_kwh = self._parse_energy_wh(energy_wh)
                # B3: A slot is programmed if it has a meaningful name or non-zero energy.
                # energy_wh can be None when JSON has "energy": null, so the guard is needed.
                raw_name = slot.get("name", "")
                is_programmed = bool(raw_name and raw_name.lower() not in ("n/a", "na")) or (
                    energy_wh is not None and energy_wh > 0
                )
            else:
                name = None
                energy_kwh = None
                is_programmed = False

            cards.append(
                DiscoveredCard(
                    index=i,
                    name=name,
                    energy_kwh=energy_kwh,
                    is_programmed=is_programmed,
                )
            )
        return cards

    async def _fetch_flat_keys(self, session: aiohttp.ClientSession) -> list[DiscoveredCard]:
        """Fetch card data using the FW >=60 flat key format.

        Queries c0n,c0e,c0i,...,c9n,c9e,c9i keys.
        Returns empty list if no data found (triggers fallback to cards[] array).
        """
        data = await self._fetch_api(session, _GOE_FLAT_KEY_FILTER, "flat key card data")
        return self._parse_flat_keys(data)

    def _parse_flat_keys(self, data: dict[str, Any]) -> list[DiscoveredCard]:
        """Parse flat key response (FW >=60) into DiscoveredCard objects.

        Keys: c{N}n (name), c{N}e (energy Wh), c{N}i (installed/programmed bool).
        Returns empty list if all keys are missing (triggers fallback).
        """
        cards: list[DiscoveredCard] = []
        any_data = False

        for i in range(MAX_CARD_SLOTS):
            name_key = f"c{i}{GOE_FLAT_KEY_SUFFIX_NAME}"
            energy_key = f"c{i}{GOE_FLAT_KEY_SUFFIX_ENERGY}"
            installed_key = f"c{i}{GOE_FLAT_KEY_SUFFIX_INSTALLED}"

            if name_key in data or energy_key in data or installed_key in data:
                any_data = True

            raw_name = data.get(name_key)
            name = self._parse_card_name(raw_name)
            energy_kwh = self._parse_energy_wh(data.get(energy_key, 0))
            is_programmed = bool(data.get(installed_key, False))

            cards.append(
                DiscoveredCard(
                    index=i,
                    name=name,
                    energy_kwh=energy_kwh,
                    is_programmed=is_programmed,
                )
            )

        if not any_data:
            return []

        return cards

    @staticmethod
    def _parse_card_name(raw: Any) -> str | None:
        """Normalise a card name string; return None for empty/placeholder values."""
        if not raw or not isinstance(raw, str):
            return None
        stripped = raw.strip()
        if stripped.lower() in ("", "n/a", "na"):
            return None
        return stripped

    @staticmethod
    def _parse_energy_wh(raw: Any) -> float | None:
        """Convert energy from Wh (int/float) to kWh (float).

        Returns None for negative or unreasonably large values (> MAX_ENERGY_KWH).
        """
        if raw is None:
            return None
        try:
            wh = float(raw)
        except (TypeError, ValueError):
            return None
        if wh < 0:
            return None
        kwh = wh / 1000.0
        if kwh > MAX_ENERGY_KWH:
            return None
        return kwh
