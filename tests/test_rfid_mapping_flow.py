"""Tests for enhanced RFID mapping config flow with discovery (PR-08)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.const import DOMAIN
from custom_components.ev_charging_manager.rfid_discovery import DiscoveredCard, DiscoveryError

from .conftest import MOCK_CHARGER_DATA, setup_entry_with_subentries

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _goe_entry() -> MockConfigEntry:
    """Return a go-e config entry (rfid_discovery enabled profile)."""
    return MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        title="My go-e Charger",
    )


def _generic_entry() -> MockConfigEntry:
    """Return a generic charger config entry (no rfid_discovery)."""
    data = dict(MOCK_CHARGER_DATA)
    data["charger_profile"] = "generic"
    return MockConfigEntry(domain=DOMAIN, data=data, title="Generic Charger")


def _make_cards(count: int = 2) -> list[DiscoveredCard]:
    """Create a list of mock discovered cards."""
    programmed = [
        DiscoveredCard(index=0, name="Paul", energy_kwh=0.0, is_programmed=True),
        DiscoveredCard(index=1, name="Petra", energy_kwh=12.3, is_programmed=True),
    ]
    empty = [
        DiscoveredCard(index=i, name=None, energy_kwh=None, is_programmed=False)
        for i in range(2, 10)
    ]
    return (programmed[:count] + empty)[:10]


def _make_provider(
    cards: list[DiscoveredCard] | None = None,
    uid: str | None = None,
    discovery_error: DiscoveryError | None = None,
) -> MagicMock:
    """Build a mock RfidDiscoveryProvider."""
    provider = MagicMock()
    provider.supports_discovery.return_value = True
    if discovery_error:
        provider.get_programmed_cards = AsyncMock(side_effect=discovery_error)
    else:
        provider.get_programmed_cards = AsyncMock(return_value=cards or _make_cards())
    provider.get_last_rfid_uid = AsyncMock(return_value=uid)
    provider.set_rfid_serial_reporting = AsyncMock(return_value=True)
    return provider


async def _add_user(hass: HomeAssistant, entry_id: str, name: str = "Paul") -> str:
    """Add a user subentry and return the subentry_id."""
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "user"), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": name, "type": "regular"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_get_entry(entry_id)
    for sub in entry.subentries.values():
        if sub.subentry_type == "user" and sub.data["name"] == name:
            return sub.subentry_id
    raise RuntimeError(f"User '{name}' subentry not found after creation")


# ---------------------------------------------------------------------------
# T011: US1 — Discovery happy path
# ---------------------------------------------------------------------------


async def test_rfid_flow_discovery_shows_select_card(hass: HomeAssistant) -> None:
    """go-e charger: flow starts with discovery and shows select_card step."""
    entry = _goe_entry()
    await setup_entry_with_subentries(hass, entry)
    await _add_user(hass, entry.entry_id, "Paul")

    provider = _make_provider()

    with patch(
        "custom_components.ev_charging_manager.config_flow.get_discovery_provider",
        return_value=provider,
    ):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "rfid_mapping"), context={"source": "user"}
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "select_card"
    # Options should show programmed cards
    options = result["data_schema"].schema["card_index"].config["options"]
    labels = [o["label"] for o in options]
    assert any("Paul" in lbl for lbl in labels)
    assert any("Petra" in lbl for lbl in labels)


async def test_rfid_flow_discovery_select_and_map(hass: HomeAssistant) -> None:
    """Full discovery flow: select card → map card → create subentry."""
    entry = _goe_entry()
    await setup_entry_with_subentries(hass, entry)
    user_id = await _add_user(hass, entry.entry_id, "Paul")

    provider = _make_provider()

    with patch(
        "custom_components.ev_charging_manager.config_flow.get_discovery_provider",
        return_value=provider,
    ):
        # Step 1: discovery → select_card form
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "rfid_mapping"), context={"source": "user"}
        )
        assert result["step_id"] == "select_card"

        # Step 2: select card #0
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"card_index": "0"}
        )
        assert result["step_id"] == "map_card"

        # Step 3: assign user
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"user_id": user_id}
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY

    # Verify subentry data
    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    rfid_subs = [s for s in loaded_entry.subentries.values() if s.subentry_type == "rfid_mapping"]
    assert len(rfid_subs) == 1
    sub = rfid_subs[0]
    assert sub.data["card_index"] == 0
    assert sub.data["user_id"] == user_id
    assert sub.data["active"] is True


async def test_rfid_flow_user_name_match_suggested(hass: HomeAssistant) -> None:
    """Card named 'Paul' auto-suggests user 'Paul' in map_card step."""
    entry = _goe_entry()
    await setup_entry_with_subentries(hass, entry)
    await _add_user(hass, entry.entry_id, "Paul")

    cards = _make_cards()  # card[0].name == "Paul"
    provider = _make_provider(cards=cards)

    with patch(
        "custom_components.ev_charging_manager.config_flow.get_discovery_provider",
        return_value=provider,
    ):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "rfid_mapping"), context={"source": "user"}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {"card_index": "0"},  # Select "Paul" card
        )

    # map_card step should show suggested values with paul pre-selected
    assert result["step_id"] == "map_card"
    # The suggested_values should contain paul's user_id
    # (HA wraps them in the schema; we verify they're passed via description_placeholders)
    assert result["description_placeholders"]["card_name"] == "Paul"


async def test_rfid_flow_already_mapped_cards_filtered(hass: HomeAssistant) -> None:
    """Cards already mapped are excluded from the select_card dropdown."""
    entry = _goe_entry()
    await setup_entry_with_subentries(hass, entry)
    user_id = await _add_user(hass, entry.entry_id, "Paul")

    provider = _make_provider()

    # First mapping: card #0 via discovery
    with patch(
        "custom_components.ev_charging_manager.config_flow.get_discovery_provider",
        return_value=provider,
    ):
        r = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "rfid_mapping"), context={"source": "user"}
        )
        r = await hass.config_entries.subentries.async_configure(r["flow_id"], {"card_index": "0"})
        await hass.config_entries.subentries.async_configure(r["flow_id"], {"user_id": user_id})

    # Second mapping attempt — card #0 should no longer appear
    provider2 = _make_provider()
    with patch(
        "custom_components.ev_charging_manager.config_flow.get_discovery_provider",
        return_value=provider2,
    ):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "rfid_mapping"), context={"source": "user"}
        )

    assert result["step_id"] == "select_card"
    options = result["data_schema"].schema["card_index"].config["options"]
    values = [o["value"] for o in options]
    assert "0" not in values  # Card #0 already mapped
    assert "1" in values  # Card #1 still available


# ---------------------------------------------------------------------------
# T011: US1 — UID display in select_card
# ---------------------------------------------------------------------------


async def test_rfid_flow_uid_shown_in_description(hass: HomeAssistant) -> None:
    """When rde is enabled, lri UID appears in description_placeholders."""
    entry = _goe_entry()
    await setup_entry_with_subentries(hass, entry)
    await _add_user(hass, entry.entry_id, "Paul")

    provider = _make_provider(uid="04b7d7b2c01690")

    with patch(
        "custom_components.ev_charging_manager.config_flow.get_discovery_provider",
        return_value=provider,
    ):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "rfid_mapping"), context={"source": "user"}
        )

    assert result["step_id"] == "select_card"
    assert "04b7d7b2c01690" in result["description_placeholders"]["last_uid"]


async def test_rfid_flow_no_uid_when_rde_disabled(hass: HomeAssistant) -> None:
    """When rde is disabled, last_uid placeholder is empty string."""
    entry = _goe_entry()
    await setup_entry_with_subentries(hass, entry)
    await _add_user(hass, entry.entry_id, "Paul")

    provider = _make_provider(uid=None)

    with patch(
        "custom_components.ev_charging_manager.config_flow.get_discovery_provider",
        return_value=provider,
    ):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "rfid_mapping"), context={"source": "user"}
        )

    assert result["step_id"] == "select_card"
    assert result["description_placeholders"]["last_uid"] == ""


# ---------------------------------------------------------------------------
# T018: US3 — card_uid stored in subentry
# ---------------------------------------------------------------------------


async def test_rfid_mapping_stores_card_uid_when_available(hass: HomeAssistant) -> None:
    """card_uid from lri is stored in the subentry when a UID is available."""
    entry = _goe_entry()
    await setup_entry_with_subentries(hass, entry)
    user_id = await _add_user(hass, entry.entry_id, "Paul")

    provider = _make_provider(uid="04b7d7b2c01690")

    with patch(
        "custom_components.ev_charging_manager.config_flow.get_discovery_provider",
        return_value=provider,
    ):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "rfid_mapping"), context={"source": "user"}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"card_index": "0"}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"user_id": user_id}
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    rfid_subs = [s for s in loaded_entry.subentries.values() if s.subentry_type == "rfid_mapping"]
    assert rfid_subs[0].data["card_uid"] == "04b7d7b2c01690"


async def test_rfid_mapping_card_uid_none_when_unavailable(hass: HomeAssistant) -> None:
    """card_uid is None in the subentry when no UID is available."""
    entry = _goe_entry()
    await setup_entry_with_subentries(hass, entry)
    user_id = await _add_user(hass, entry.entry_id, "Paul")

    provider = _make_provider(uid=None)

    with patch(
        "custom_components.ev_charging_manager.config_flow.get_discovery_provider",
        return_value=provider,
    ):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "rfid_mapping"), context={"source": "user"}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"card_index": "0"}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"user_id": user_id}
        )

    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    rfid_subs = [s for s in loaded_entry.subentries.values() if s.subentry_type == "rfid_mapping"]
    assert rfid_subs[0].data["card_uid"] is None


# ---------------------------------------------------------------------------
# T023: US4 — Manual fallback
# ---------------------------------------------------------------------------


async def test_generic_charger_shows_manual_form_directly(hass: HomeAssistant) -> None:
    """Generic charger (no discovery) goes directly to manual entry form."""
    entry = _generic_entry()
    await setup_entry_with_subentries(hass, entry)
    await _add_user(hass, entry.entry_id, "Paul")

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "rfid_mapping"), context={"source": "user"}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "manual"


async def test_discovery_error_falls_back_to_manual(hass: HomeAssistant) -> None:
    """DiscoveryError during charger query falls back to manual entry."""
    entry = _goe_entry()
    await setup_entry_with_subentries(hass, entry)
    await _add_user(hass, entry.entry_id, "Paul")

    provider = _make_provider(
        discovery_error=DiscoveryError("Cannot reach charger. Check network connection.")
    )

    with patch(
        "custom_components.ev_charging_manager.config_flow.get_discovery_provider",
        return_value=provider,
    ):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "rfid_mapping"), context={"source": "user"}
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "manual"
    # Error message passed to description_placeholders with "Discovery failed: " prefix
    error_text = result["description_placeholders"].get("discovery_error", "")
    assert "Discovery failed:" in error_text
    assert "Cannot reach charger" in error_text


async def test_manual_form_creates_correct_subentry(hass: HomeAssistant) -> None:
    """Manual form submission creates a correct RFID mapping subentry."""
    entry = _generic_entry()
    await setup_entry_with_subentries(hass, entry)
    user_id = await _add_user(hass, entry.entry_id, "Petra")

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "rfid_mapping"), context={"source": "user"}
    )
    assert result["step_id"] == "manual"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"card_index": "3", "user_id": user_id},
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    rfid_subs = [s for s in loaded_entry.subentries.values() if s.subentry_type == "rfid_mapping"]
    assert len(rfid_subs) == 1
    sub = rfid_subs[0]
    assert sub.data["card_index"] == 3
    assert sub.data["user_id"] == user_id
    assert sub.data["card_uid"] is None


async def test_manual_form_duplicate_card_index_error(hass: HomeAssistant) -> None:
    """Manual form rejects duplicate card index with already_mapped error."""
    entry = _generic_entry()
    await setup_entry_with_subentries(hass, entry)
    user_id = await _add_user(hass, entry.entry_id, "Paul")

    # First mapping
    r = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "rfid_mapping"), context={"source": "user"}
    )
    await hass.config_entries.subentries.async_configure(
        r["flow_id"], {"card_index": "5", "user_id": user_id}
    )

    # Second mapping with same index
    r2 = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "rfid_mapping"), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        r2["flow_id"], {"card_index": "5", "user_id": user_id}
    )

    assert result["type"] == FlowResultType.FORM
    assert "card_index" in result["errors"]
    assert result["errors"]["card_index"] == "already_mapped"


# ---------------------------------------------------------------------------
# T023: US4 — All slots programmed but all already mapped
# ---------------------------------------------------------------------------


async def test_discovery_all_slots_already_mapped_falls_back_to_manual(
    hass: HomeAssistant,
) -> None:
    """When all programmed cards are already mapped, falls back to manual."""
    entry = _goe_entry()
    await setup_entry_with_subentries(hass, entry)
    user_id = await _add_user(hass, entry.entry_id, "Paul")

    # Only card #0 is programmed
    cards = [
        DiscoveredCard(index=0, name="Paul", energy_kwh=0.0, is_programmed=True),
        *[
            DiscoveredCard(index=i, name=None, energy_kwh=None, is_programmed=False)
            for i in range(1, 10)
        ],
    ]
    provider = _make_provider(cards=cards)

    # First: map card #0
    with patch(
        "custom_components.ev_charging_manager.config_flow.get_discovery_provider",
        return_value=provider,
    ):
        r = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "rfid_mapping"), context={"source": "user"}
        )
        r = await hass.config_entries.subentries.async_configure(r["flow_id"], {"card_index": "0"})
        await hass.config_entries.subentries.async_configure(r["flow_id"], {"user_id": user_id})

    # Second attempt: all programmed cards are mapped -> manual fallback
    provider2 = _make_provider(cards=cards)
    with patch(
        "custom_components.ev_charging_manager.config_flow.get_discovery_provider",
        return_value=provider2,
    ):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "rfid_mapping"), context={"source": "user"}
        )

    assert result["step_id"] == "manual"


# ---------------------------------------------------------------------------
# B7: no_programmed_cards branch
# ---------------------------------------------------------------------------


async def test_discovery_no_programmed_cards_falls_back_to_manual(
    hass: HomeAssistant,
) -> None:
    """When charger returns 10 slots but none are programmed, falls back to manual."""
    entry = _goe_entry()
    await setup_entry_with_subentries(hass, entry)
    await _add_user(hass, entry.entry_id, "Paul")

    # All 10 slots are empty (not programmed)
    cards = [
        DiscoveredCard(index=i, name=None, energy_kwh=None, is_programmed=False) for i in range(10)
    ]
    provider = _make_provider(cards=cards)

    with patch(
        "custom_components.ev_charging_manager.config_flow.get_discovery_provider",
        return_value=provider,
    ):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "rfid_mapping"), context={"source": "user"}
        )

    assert result["step_id"] == "manual"


# ---------------------------------------------------------------------------
# B7: Vehicle assignment in discovery flow
# ---------------------------------------------------------------------------


async def _add_vehicle(hass: HomeAssistant, entry_id: str, name: str = "Model 3") -> str:
    """Add a vehicle subentry and return the subentry_id."""
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "vehicle"), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": name,
            "battery_capacity_kwh": 75.0,
            "charging_phases": "3",
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_get_entry(entry_id)
    for sub in entry.subentries.values():
        if sub.subentry_type == "vehicle" and sub.data["name"] == name:
            return sub.subentry_id
    raise RuntimeError(f"Vehicle '{name}' subentry not found after creation")


async def test_rfid_flow_discovery_with_vehicle_assignment(hass: HomeAssistant) -> None:
    """Discovery flow: select card, assign user AND vehicle, creates correct subentry."""
    entry = _goe_entry()
    await setup_entry_with_subentries(hass, entry)
    user_id = await _add_user(hass, entry.entry_id, "Paul")
    vehicle_id = await _add_vehicle(hass, entry.entry_id, "Peugeot 3008")

    provider = _make_provider()

    with patch(
        "custom_components.ev_charging_manager.config_flow.get_discovery_provider",
        return_value=provider,
    ):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "rfid_mapping"), context={"source": "user"}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"card_index": "0"}
        )
        assert result["step_id"] == "map_card"
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"user_id": user_id, "vehicle_id": vehicle_id}
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    rfid_subs = [s for s in loaded_entry.subentries.values() if s.subentry_type == "rfid_mapping"]
    assert len(rfid_subs) == 1
    sub = rfid_subs[0]
    assert sub.data["user_id"] == user_id
    assert sub.data["vehicle_id"] == vehicle_id


# ---------------------------------------------------------------------------
# B7: Reconfigure flow
# ---------------------------------------------------------------------------


async def test_rfid_mapping_reconfigure_changes_user(hass: HomeAssistant) -> None:
    """Reconfigure existing RFID mapping to change the assigned user."""
    entry = _generic_entry()
    await setup_entry_with_subentries(hass, entry)
    paul_id = await _add_user(hass, entry.entry_id, "Paul")
    petra_id = await _add_user(hass, entry.entry_id, "Petra")

    # Create initial mapping for Paul
    r = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "rfid_mapping"), context={"source": "user"}
    )
    r = await hass.config_entries.subentries.async_configure(
        r["flow_id"], {"card_index": "2", "user_id": paul_id}
    )
    assert r["type"] == FlowResultType.CREATE_ENTRY

    # Find the created subentry
    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    rfid_sub = next(
        s for s in loaded_entry.subentries.values() if s.subentry_type == "rfid_mapping"
    )

    # Reconfigure to assign to Petra
    r2 = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "rfid_mapping"),
        context={"source": "reconfigure", "subentry_id": rfid_sub.subentry_id},
    )
    assert r2["step_id"] == "reconfigure"

    result = await hass.config_entries.subentries.async_configure(
        r2["flow_id"], {"user_id": petra_id, "active": True}
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    # Verify the mapping was updated
    loaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    rfid_sub = next(
        s for s in loaded_entry.subentries.values() if s.subentry_type == "rfid_mapping"
    )
    assert rfid_sub.data["user_id"] == petra_id


# ---------------------------------------------------------------------------
# B6: No users abort
# ---------------------------------------------------------------------------


async def test_manual_no_users_aborts(hass: HomeAssistant) -> None:
    """Manual flow aborts if no active users exist."""
    entry = _generic_entry()
    await setup_entry_with_subentries(hass, entry)
    # No users added

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "rfid_mapping"), context={"source": "user"}
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "no_users"
