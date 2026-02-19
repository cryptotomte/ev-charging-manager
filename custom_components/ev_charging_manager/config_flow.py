"""Config flow for EV Charging Manager."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import selector

from .charger_profiles import CHARGER_PROFILES
from .const import (
    CONF_CAR_STATUS_CHARGING_VALUE,
    CONF_CAR_STATUS_ENTITY,
    CONF_CHARGER_HOST,
    CONF_CHARGER_NAME,
    CONF_CHARGER_PROFILE,
    CONF_ENERGY_ENTITY,
    CONF_ENERGY_UNIT,
    CONF_POWER_ENTITY,
    CONF_PRICING_MODE,
    CONF_RFID_ENTITY,
    CONF_RFID_UID_ENTITY,
    CONF_STATIC_PRICE_KWH,
    CONF_TOTAL_ENERGY_ENTITY,
    DEFAULT_CHARGER_NAME,
    DEFAULT_ENERGY_UNIT,
    DEFAULT_PRICING_MODE,
    DEFAULT_STATIC_PRICE_KWH,
    DOMAIN,
)

# Fields that must have a valid, reachable HA entity state
_MANDATORY_ENTITY_FIELDS = [
    CONF_CAR_STATUS_ENTITY,
    CONF_ENERGY_ENTITY,
    CONF_POWER_ENTITY,
]

# Fields that are validated only when provided
_OPTIONAL_ENTITY_FIELDS = [
    CONF_RFID_ENTITY,
    CONF_TOTAL_ENERGY_ENTITY,
    CONF_RFID_UID_ENTITY,
]


def _make_entity_key(
    field: str,
    required: bool,
    suggested: str | None,
) -> vol.Required | vol.Optional:
    """Build a vol schema key with optional suggested_value description."""
    description = {"suggested_value": suggested} if suggested else None
    if required:
        return vol.Required(field, description=description) if description else vol.Required(field)
    return vol.Optional(field, description=description) if description else vol.Optional(field)


def _coerce_charging_value(value: Any) -> int | str:
    """Coerce charging indicator to int if possible, else keep as string."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (ValueError, TypeError):
        return str(value)


class EvChargingManagerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the EV Charging Manager config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step — redirect to charger type selection."""
        return await self.async_step_charger_type()

    async def async_step_charger_type(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 0: Select charger profile."""
        if user_input is not None:
            self.data[CONF_CHARGER_PROFILE] = user_input[CONF_CHARGER_PROFILE]
            return await self.async_step_charger_entities()

        return self.async_show_form(
            step_id="charger_type",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CHARGER_PROFILE): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"value": key, "label": profile["name"]}
                                for key, profile in CHARGER_PROFILES.items()
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    async def async_step_charger_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: Map sensor entities, with profile-based pre-fill suggestions."""
        profile_key = self.data[CONF_CHARGER_PROFILE]
        profile = CHARGER_PROFILES[profile_key]
        errors: dict[str, str] = {}

        if user_input is not None:
            # Coerce charging indicator value to int where possible
            if CONF_CAR_STATUS_CHARGING_VALUE in user_input:
                user_input[CONF_CAR_STATUS_CHARGING_VALUE] = _coerce_charging_value(
                    user_input[CONF_CAR_STATUS_CHARGING_VALUE]
                )

            # Normalize empty optional entity fields to None
            for field in _OPTIONAL_ENTITY_FIELDS:
                if not user_input.get(field):
                    user_input[field] = None
            if not user_input.get(CONF_CHARGER_HOST):
                user_input[CONF_CHARGER_HOST] = None

            errors = await self._validate_entities(user_input)
            if not errors:
                self.data.update(user_input)
                # Ensure all optional entity fields are present (may be absent if not submitted)
                for field in _OPTIONAL_ENTITY_FIELDS:
                    self.data.setdefault(field, None)
                self.data.setdefault(CONF_CHARGER_HOST, None)
                return await self.async_step_pricing()

        entity_selector = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))

        # Build schema fields list to control ordering
        schema_dict: dict[Any, Any] = {
            _make_entity_key(
                CONF_CAR_STATUS_ENTITY,
                required=True,
                suggested=profile.get("car_status_sensor"),
            ): entity_selector,
            _make_entity_key(
                CONF_CAR_STATUS_CHARGING_VALUE,
                required=True,
                suggested=str(profile["car_status_charging_value"])
                if profile.get("car_status_charging_value") is not None
                else None,
            ): vol.Any(vol.Coerce(int), str),
            _make_entity_key(
                CONF_ENERGY_ENTITY,
                required=True,
                suggested=profile.get("session_energy_sensor"),
            ): entity_selector,
            vol.Required(
                CONF_ENERGY_UNIT,
                default=profile.get("session_energy_unit") or DEFAULT_ENERGY_UNIT,
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["Wh", "kWh"],
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            _make_entity_key(
                CONF_POWER_ENTITY,
                required=True,
                suggested=profile.get("power_sensor"),
            ): entity_selector,
            _make_entity_key(
                CONF_RFID_ENTITY,
                required=False,
                suggested=profile.get("rfid_sensor"),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            _make_entity_key(
                CONF_TOTAL_ENERGY_ENTITY,
                required=False,
                suggested=profile.get("total_energy_sensor"),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            _make_entity_key(
                CONF_RFID_UID_ENTITY,
                required=False,
                suggested=profile.get("rfid_last_uid_sensor"),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(CONF_CHARGER_NAME, default=DEFAULT_CHARGER_NAME): selector.TextSelector(),
        }

        # charger_host: required for profiles that need it, optional otherwise
        if profile.get("requires_charger_host"):
            schema_dict[vol.Required(CONF_CHARGER_HOST)] = selector.TextSelector()
        else:
            schema_dict[vol.Optional(CONF_CHARGER_HOST)] = selector.TextSelector()

        return self.async_show_form(
            step_id="charger_entities",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def _validate_entities(self, user_input: dict[str, Any]) -> dict[str, str]:
        """Validate that provided entity IDs exist and are not unavailable."""
        errors: dict[str, str] = {}
        for field in _MANDATORY_ENTITY_FIELDS + _OPTIONAL_ENTITY_FIELDS:
            entity_id = user_input.get(field)
            if not entity_id:
                # Optional fields may be absent
                continue
            state = self.hass.states.get(entity_id)
            if state is None:
                errors[field] = "entity_not_found"
            elif state.state in ("unavailable", "unknown"):
                errors[field] = "entity_unavailable"
        return errors

    async def async_step_pricing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Configure pricing mode and static price."""
        if user_input is not None:
            self.data.update(user_input)
            return await self.async_step_confirm()

        return self.async_show_form(
            step_id="pricing",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PRICING_MODE, default=DEFAULT_PRICING_MODE
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"value": "static", "label": "Static price"},
                                {"value": "spot", "label": "Spot price (placeholder — PR-05)"},
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Required(CONF_STATIC_PRICE_KWH, default=DEFAULT_STATIC_PRICE_KWH): vol.All(
                        selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=0.01,
                                max=100.0,
                                step=0.01,
                                mode=selector.NumberSelectorMode.BOX,
                            )
                        ),
                        vol.Range(min=0.01),
                    ),
                }
            ),
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: Show summary and create config entry on confirmation."""
        if user_input is not None:
            charger_name = self.data.get(CONF_CHARGER_NAME, DEFAULT_CHARGER_NAME)
            return self.async_create_entry(title=charger_name, data=self.data)

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "charger_name": self.data.get(CONF_CHARGER_NAME, DEFAULT_CHARGER_NAME),
            },
        )
