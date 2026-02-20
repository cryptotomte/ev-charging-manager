"""Config flow for EV Charging Manager."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .charger_profiles import CHARGER_PROFILES
from .const import (
    CONF_CAR_STATUS_CHARGING_VALUE,
    CONF_CAR_STATUS_ENTITY,
    CONF_CHARGER_HOST,
    CONF_CHARGER_NAME,
    CONF_CHARGER_PROFILE,
    CONF_CHARGER_SERIAL,
    CONF_ENERGY_ENTITY,
    CONF_ENERGY_UNIT,
    CONF_MAX_STORED_SESSIONS,
    CONF_MIN_SESSION_DURATION_S,
    CONF_MIN_SESSION_ENERGY_WH,
    CONF_PERSISTENCE_INTERVAL_S,
    CONF_POWER_ENTITY,
    CONF_PRICING_MODE,
    CONF_RFID_ENTITY,
    CONF_RFID_UID_ENTITY,
    CONF_STATIC_PRICE_KWH,
    CONF_TOTAL_ENERGY_ENTITY,
    DEFAULT_CHARGER_NAME,
    DEFAULT_CHARGING_EFFICIENCY,
    DEFAULT_ENERGY_UNIT,
    DEFAULT_MAX_STORED_SESSIONS,
    DEFAULT_MIN_SESSION_DURATION_S,
    DEFAULT_MIN_SESSION_ENERGY_WH,
    DEFAULT_PERSISTENCE_INTERVAL_S,
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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return OptionsFlowHandler()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentry types supported by this integration."""
        return {
            "vehicle": VehicleSubentryFlowHandler,
            "user": UserSubentryFlowHandler,
            "rfid_mapping": RfidMappingSubentryFlowHandler,
        }

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.data: dict[str, Any] = {}

    @staticmethod
    def _profile_needs_serial(profile: dict[str, Any]) -> bool:
        """Check if any sensor pattern in the profile contains {serial}."""
        for key, value in profile.items():
            if isinstance(value, str) and "{serial}" in value:
                return True
        return False

    def _resolve_suggested(self, pattern: str | None) -> str | None:
        """Replace {serial} placeholder with actual serial number."""
        if pattern is None:
            return None
        serial = self.data.get(CONF_CHARGER_SERIAL)
        if serial and "{serial}" in pattern:
            return pattern.replace("{serial}", serial)
        return pattern

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step — select charger type."""
        if user_input is not None:
            self.data[CONF_CHARGER_PROFILE] = user_input[CONF_CHARGER_PROFILE]
            profile = CHARGER_PROFILES[self.data[CONF_CHARGER_PROFILE]]
            if self._profile_needs_serial(profile):
                return await self.async_step_serial()
            return await self.async_step_charger_entities()

        return self.async_show_form(
            step_id="user",
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

    async def async_step_serial(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 0b: Ask for charger serial number (profiles with {serial} patterns)."""
        if user_input is not None:
            self.data[CONF_CHARGER_SERIAL] = user_input[CONF_CHARGER_SERIAL]
            return await self.async_step_charger_entities()

        return self.async_show_form(
            step_id="serial",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CHARGER_SERIAL): selector.TextSelector(),
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
                suggested=self._resolve_suggested(profile.get("car_status_sensor")),
            ): entity_selector,
            _make_entity_key(
                CONF_CAR_STATUS_CHARGING_VALUE,
                required=True,
                suggested=str(profile["car_status_charging_value"])
                if profile.get("car_status_charging_value") is not None
                else None,
            ): selector.TextSelector(),
            _make_entity_key(
                CONF_ENERGY_ENTITY,
                required=True,
                suggested=self._resolve_suggested(profile.get("session_energy_sensor")),
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
                suggested=self._resolve_suggested(profile.get("power_sensor")),
            ): entity_selector,
            _make_entity_key(
                CONF_RFID_ENTITY,
                required=False,
                suggested=self._resolve_suggested(profile.get("rfid_sensor")),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain=["sensor", "select"])),
            _make_entity_key(
                CONF_TOTAL_ENERGY_ENTITY,
                required=False,
                suggested=self._resolve_suggested(profile.get("total_energy_sensor")),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            _make_entity_key(
                CONF_RFID_UID_ENTITY,
                required=False,
                suggested=self._resolve_suggested(profile.get("rfid_last_uid_sensor")),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain=["sensor", "select"])),
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

        charger_name = self.data.get(CONF_CHARGER_NAME, DEFAULT_CHARGER_NAME)
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "charger_name": charger_name,
            },
        )


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


class OptionsFlowHandler(OptionsFlow):
    """Handle EV Charging Manager options."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_MIN_SESSION_DURATION_S,
                    default=opts.get(CONF_MIN_SESSION_DURATION_S, DEFAULT_MIN_SESSION_DURATION_S),
                ): vol.All(
                    selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=3600, step=1, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                    vol.Coerce(int),
                ),
                vol.Optional(
                    CONF_MIN_SESSION_ENERGY_WH,
                    default=opts.get(CONF_MIN_SESSION_ENERGY_WH, DEFAULT_MIN_SESSION_ENERGY_WH),
                ): vol.All(
                    selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=10000, step=1, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                    vol.Coerce(int),
                ),
                vol.Optional(
                    CONF_PERSISTENCE_INTERVAL_S,
                    default=opts.get(CONF_PERSISTENCE_INTERVAL_S, DEFAULT_PERSISTENCE_INTERVAL_S),
                ): vol.All(
                    selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=60, max=3600, step=1, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                    vol.Coerce(int),
                ),
                vol.Optional(
                    CONF_MAX_STORED_SESSIONS,
                    default=opts.get(CONF_MAX_STORED_SESSIONS, DEFAULT_MAX_STORED_SESSIONS),
                ): vol.All(
                    selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=100, max=10000, step=1, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                    vol.Coerce(int),
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)


# ---------------------------------------------------------------------------
# Vehicle schema
# ---------------------------------------------------------------------------

VEHICLE_SCHEMA = vol.Schema(
    {
        vol.Required("name"): selector.TextSelector(),
        vol.Required("battery_capacity_kwh"): vol.All(
            selector.NumberSelector(selector.NumberSelectorConfig(min=0.1, max=300.0, step=0.1)),
            vol.Coerce(float),
        ),
        vol.Optional("usable_battery_kwh"): vol.All(
            selector.NumberSelector(selector.NumberSelectorConfig(min=0.1, max=300.0, step=0.1)),
            vol.Coerce(float),
        ),
        vol.Required("charging_phases"): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    {"value": "1", "label": "1-phase"},
                    {"value": "3", "label": "3-phase"},
                ],
                mode=selector.SelectSelectorMode.LIST,
            )
        ),
        vol.Optional("max_charging_power_kw"): vol.All(
            selector.NumberSelector(selector.NumberSelectorConfig(min=0.1, max=100.0, step=0.1)),
            vol.Coerce(float),
        ),
        vol.Optional("charging_efficiency", default=DEFAULT_CHARGING_EFFICIENCY): vol.All(
            selector.NumberSelector(selector.NumberSelectorConfig(min=0.80, max=0.99, step=0.01)),
            vol.Coerce(float),
        ),
    }
)


# ---------------------------------------------------------------------------
# Subentry flow handlers (Vehicle, User, RFID Mapping)
# ---------------------------------------------------------------------------


class VehicleSubentryFlowHandler(ConfigSubentryFlow):
    """Handle vehicle subentry add/edit flows."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Handle adding a new vehicle."""
        if user_input is not None:
            # Coerce charging_phases from string to int (SelectSelector returns string)
            user_input["charging_phases"] = int(user_input["charging_phases"])
            # Default usable_battery_kwh to battery_capacity_kwh
            if not user_input.get("usable_battery_kwh"):
                user_input["usable_battery_kwh"] = user_input["battery_capacity_kwh"]
            return self.async_create_entry(
                title=user_input["name"],
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=VEHICLE_SCHEMA,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle editing an existing vehicle."""
        subentry = self._get_reconfigure_subentry()

        if user_input is not None:
            user_input["charging_phases"] = int(user_input["charging_phases"])
            if not user_input.get("usable_battery_kwh"):
                user_input["usable_battery_kwh"] = user_input["battery_capacity_kwh"]
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                data=user_input,
                title=user_input["name"],
            )

        # Pre-fill with existing values
        suggested = dict(subentry.data)
        suggested["charging_phases"] = str(suggested["charging_phases"])
        schema = self.add_suggested_values_to_schema(VEHICLE_SCHEMA, suggested)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
        )


class UserSubentryFlowHandler(ConfigSubentryFlow):
    """Handle user subentry add/edit flows."""

    def __init__(self) -> None:
        """Initialize the user subentry flow."""
        super().__init__()
        self._user_data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Handle adding a new user — step 1: name + type."""
        if user_input is not None:
            self._user_data = {
                "name": user_input["name"],
                "type": user_input["type"],
                "active": True,
                "created_at": datetime.now(UTC).isoformat(),
            }
            if user_input["type"] == "guest":
                return await self.async_step_guest_pricing()
            return self.async_create_entry(
                title=self._user_data["name"],
                data=self._user_data,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("name"): selector.TextSelector(),
                    vol.Required("type"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"value": "regular", "label": "Regular"},
                                {"value": "guest", "label": "Guest"},
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    async def async_step_guest_pricing(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle guest pricing configuration — step 2 for guest users."""
        errors: dict[str, str] = {}

        if user_input is not None:
            method = user_input.get("guest_pricing_method", "fixed")
            price = user_input.get("price_per_kwh")
            markup = user_input.get("markup_factor")

            # Validate based on pricing method
            if method == "fixed" and not price:
                errors["base"] = "price_required"
            elif method == "markup" and not markup:
                errors["base"] = "markup_required"

            if not errors:
                pricing: dict[str, Any] = {"method": method}
                if method == "fixed":
                    pricing["price_per_kwh"] = price
                elif method == "markup":
                    pricing["markup_factor"] = markup
                self._user_data["guest_pricing"] = pricing
                return self.async_create_entry(
                    title=self._user_data["name"],
                    data=self._user_data,
                )

        return self.async_show_form(
            step_id="guest_pricing",
            data_schema=vol.Schema(
                {
                    vol.Required("guest_pricing_method"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"value": "fixed", "label": "Fixed price"},
                                {"value": "markup", "label": "Markup"},
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Optional("price_per_kwh"): vol.All(
                        selector.NumberSelector(
                            selector.NumberSelectorConfig(min=0.01, max=100.0, step=0.01)
                        ),
                        vol.Coerce(float),
                    ),
                    vol.Optional("markup_factor"): vol.All(
                        selector.NumberSelector(
                            selector.NumberSelectorConfig(min=1.01, max=10.0, step=0.01)
                        ),
                        vol.Coerce(float),
                    ),
                }
            ),
            errors=errors,
        )

    async def _apply_active_change(
        self, entry: ConfigEntry, subentry_id: str, new_active: bool, was_active: bool
    ) -> None:
        """Run cascade logic when user active status changes."""
        from .lifecycle import async_cascade_deactivate_user, async_cascade_reactivate_user

        if was_active and not new_active:
            await async_cascade_deactivate_user(self.hass, entry, subentry_id)
        elif not was_active and new_active:
            await async_cascade_reactivate_user(self.hass, entry, subentry_id)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle editing an existing user — type is immutable (FR-004)."""
        subentry = self._get_reconfigure_subentry()
        entry = self._get_entry()

        if user_input is not None:
            # Build updated data preserving immutable fields
            new_data = dict(subentry.data)
            new_data["name"] = user_input["name"]
            new_data["active"] = user_input.get("active", True)

            was_active = subentry.data.get("active", True)

            # If guest, route to pricing edit
            if new_data["type"] == "guest":
                self._user_data = new_data
                return await self.async_step_reconfigure_guest_pricing()

            # Run cascade before updating the subentry
            await self._apply_active_change(
                entry, subentry.subentry_id, new_data["active"], was_active
            )

            title = new_data["name"]
            if not new_data["active"]:
                title = f"{title} (inactive)"

            return self.async_update_and_abort(
                entry,
                subentry,
                data=new_data,
                title=title,
            )

        # Pre-fill form with current values
        schema = self.add_suggested_values_to_schema(
            vol.Schema(
                {
                    vol.Required("name"): selector.TextSelector(),
                    vol.Required("active"): selector.BooleanSelector(),
                }
            ),
            {"name": subentry.data["name"], "active": subentry.data.get("active", True)},
        )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            description_placeholders={"type": subentry.data["type"]},
        )

    async def async_step_reconfigure_guest_pricing(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle editing guest pricing for an existing guest user."""
        subentry = self._get_reconfigure_subentry()
        entry = self._get_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            method = user_input.get("guest_pricing_method", "fixed")
            price = user_input.get("price_per_kwh")
            markup = user_input.get("markup_factor")

            if method == "fixed" and not price:
                errors["base"] = "price_required"
            elif method == "markup" and not markup:
                errors["base"] = "markup_required"

            if not errors:
                pricing: dict[str, Any] = {"method": method}
                if method == "fixed":
                    pricing["price_per_kwh"] = price
                elif method == "markup":
                    pricing["markup_factor"] = markup
                self._user_data["guest_pricing"] = pricing

                title = self._user_data["name"]
                if not self._user_data.get("active", True):
                    title = f"{title} (inactive)"

                return self.async_update_and_abort(
                    entry,
                    subentry,
                    data=self._user_data,
                    title=title,
                )

        # Pre-fill with existing pricing
        existing_pricing = dict(subentry.data.get("guest_pricing", {}))
        suggested = {
            "guest_pricing_method": existing_pricing.get("method", "fixed"),
            "price_per_kwh": existing_pricing.get("price_per_kwh"),
            "markup_factor": existing_pricing.get("markup_factor"),
        }
        schema = self.add_suggested_values_to_schema(
            vol.Schema(
                {
                    vol.Required("guest_pricing_method"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"value": "fixed", "label": "Fixed price"},
                                {"value": "markup", "label": "Markup"},
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Optional("price_per_kwh"): vol.All(
                        selector.NumberSelector(
                            selector.NumberSelectorConfig(min=0.01, max=100.0, step=0.01)
                        ),
                        vol.Coerce(float),
                    ),
                    vol.Optional("markup_factor"): vol.All(
                        selector.NumberSelector(
                            selector.NumberSelectorConfig(min=1.01, max=10.0, step=0.01)
                        ),
                        vol.Coerce(float),
                    ),
                }
            ),
            suggested,
        )

        return self.async_show_form(
            step_id="reconfigure_guest_pricing",
            data_schema=schema,
            errors=errors,
        )


class RfidMappingSubentryFlowHandler(ConfigSubentryFlow):
    """Handle RFID mapping subentry add/edit flows."""

    def _get_available_card_indices(self, entry: ConfigEntry) -> list[str]:
        """Return unused card indices 0-9 as string options."""
        used = set()
        for sub in entry.subentries.values():
            if sub.subentry_type == "rfid_mapping":
                used.add(sub.data["card_index"])
        return [str(i) for i in range(10) if i not in used]

    def _get_active_users(self, entry: ConfigEntry) -> list[selector.SelectOptionDict]:
        """Return active users as SelectSelector options (FR-019)."""
        options: list[selector.SelectOptionDict] = []
        for sub in entry.subentries.values():
            if sub.subentry_type == "user" and sub.data.get("active", True):
                options.append({"value": sub.subentry_id, "label": sub.data["name"]})
        return options

    def _get_vehicles(self, entry: ConfigEntry) -> list[selector.SelectOptionDict]:
        """Return all vehicles as SelectSelector options."""
        options: list[selector.SelectOptionDict] = []
        for sub in entry.subentries.values():
            if sub.subentry_type == "vehicle":
                options.append({"value": sub.subentry_id, "label": sub.data["name"]})
        return options

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Handle adding a new RFID mapping."""
        entry = self._get_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            card_index = int(user_input["card_index"])

            # Check uniqueness
            for sub in entry.subentries.values():
                if sub.subentry_type == "rfid_mapping" and sub.data["card_index"] == card_index:
                    errors["card_index"] = "already_mapped"
                    break

            if not errors:
                data: dict[str, Any] = {
                    "card_index": card_index,
                    "card_uid": None,
                    "user_id": user_input["user_id"],
                    "vehicle_id": user_input.get("vehicle_id"),
                    "active": True,
                    "deactivated_by": None,
                }
                return self.async_create_entry(
                    title=f"Card #{card_index} (go-e slot {card_index + 1})",
                    data=data,
                    unique_id=f"rfid_{card_index}",
                )

        active_users = self._get_active_users(entry)
        vehicles = self._get_vehicles(entry)

        schema_dict: dict[Any, Any] = {
            vol.Required("card_index"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": str(i), "label": f"Card #{i} (go-e slot {i + 1})"}
                        for i in range(10)
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required("user_id"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=active_users,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
        if vehicles:
            schema_dict[vol.Optional("vehicle_id")] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=vehicles,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle editing an existing RFID mapping."""
        subentry = self._get_reconfigure_subentry()
        entry = self._get_entry()

        if user_input is not None:
            new_data = dict(subentry.data)
            new_data["user_id"] = user_input["user_id"]
            new_data["vehicle_id"] = user_input.get("vehicle_id")
            new_data["active"] = user_input.get("active", True)

            # Individual deactivation tracking
            if not new_data["active"] and subentry.data.get("active", True):
                new_data["deactivated_by"] = "individual"
            elif new_data["active"] and not subentry.data.get("active", True):
                new_data["deactivated_by"] = None

            return self.async_update_and_abort(
                entry,
                subentry,
                data=new_data,
            )

        active_users = self._get_active_users(entry)
        vehicles = self._get_vehicles(entry)

        schema_dict: dict[Any, Any] = {
            vol.Required("user_id"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=active_users,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
        if vehicles:
            schema_dict[vol.Optional("vehicle_id")] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=vehicles,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        schema_dict[vol.Required("active")] = selector.BooleanSelector()

        suggested = {
            "user_id": subentry.data["user_id"],
            "vehicle_id": subentry.data.get("vehicle_id"),
            "active": subentry.data.get("active", True),
        }
        schema = self.add_suggested_values_to_schema(vol.Schema(schema_dict), suggested)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            description_placeholders={
                "card_index": str(subentry.data["card_index"]),
            },
        )
