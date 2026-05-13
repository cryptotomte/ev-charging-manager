"""Tests for charger profile definitions (PR-20 observation entity patterns)."""

from __future__ import annotations

from custom_components.ev_charging_manager.charger_profiles import CHARGER_PROFILES

# ===========================================================================
# T-PRF-01: goe_gemini profile contains the four new observation entity patterns
# ===========================================================================


def test_prf_01_goe_gemini_has_observation_patterns() -> None:
    """T-PRF-01: goe_gemini profile contains four observation entity patterns with {serial}."""
    profile = CHARGER_PROFILES["goe_gemini"]

    for key in ("plug_sensor", "cable_lock_sensor", "model_status_sensor", "error_sensor"):
        assert key in profile, f"goe_gemini profile missing key: {key}"
        assert isinstance(profile[key], str), f"{key} must be a string, got {type(profile[key])}"
        assert "{serial}" in profile[key], (
            f"{key} pattern must contain '{{serial}}' token, got: {profile[key]}"
        )

    # Verify the exact entity patterns
    assert profile["plug_sensor"] == "binary_sensor.goe_{serial}_car_0"
    assert profile["cable_lock_sensor"] == "sensor.goe_{serial}_cus_value"
    assert profile["model_status_sensor"] == "sensor.goe_{serial}_modelstatus_value"
    assert profile["error_sensor"] == "sensor.goe_{serial}_err_value"


# ===========================================================================
# T-PRF-02: generic profile has the four new keys, all None
# ===========================================================================


def test_prf_02_generic_profile_observation_keys_are_none() -> None:
    """T-PRF-02: generic profile contains the four new keys, all set to None."""
    profile = CHARGER_PROFILES["generic"]

    for key in ("plug_sensor", "cable_lock_sensor", "model_status_sensor", "error_sensor"):
        assert key in profile, f"generic profile missing key: {key}"
        assert profile[key] is None, f"{key} must be None on generic profile, got: {profile[key]}"
