"""Translation files stay in step with strings.json (spec 026 D-14).

Home Assistant loads translations/<lang>.json, never strings.json, and drops a
localized string whose placeholders differ from the English one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_BASE = Path(__file__).parent.parent / "custom_components" / "ev_charging_manager"
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _load(relative: str) -> dict:
    return json.loads((_BASE / relative).read_text(encoding="utf-8"))


def _flatten(node: dict, prefix: str = "") -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            if not value:
                out[path] = None
            out.update(_flatten(value, path))
        else:
            out[path] = value
    return out


def test_en_json_equals_strings_json() -> None:
    """T-24."""
    assert _load("translations/en.json") == _load("strings.json")


def test_sv_json_has_exactly_the_keys_of_strings_json() -> None:
    """T-25."""
    strings = _flatten(_load("strings.json"))
    sv = _flatten(_load("translations/sv.json"))
    assert sorted(set(strings) - set(sv)) == []
    assert sorted(set(sv) - set(strings)) == []


def test_sv_json_placeholders_match_strings_json() -> None:
    """T-26."""
    strings = _flatten(_load("strings.json"))
    sv = _flatten(_load("translations/sv.json"))
    mismatches = {}
    for key, text in strings.items():
        if not isinstance(text, str):
            continue
        expected = set(_PLACEHOLDER.findall(text))
        actual = set(_PLACEHOLDER.findall(sv.get(key) or ""))
        if expected != actual:
            mismatches[key] = (sorted(expected), sorted(actual))
    assert mismatches == {}


_CURRENCY_WORD = re.compile(r"\bkr\b")
_SWEDISH = re.compile(r"swedish|svensk", re.IGNORECASE)
# D-76: the setup help names kr as the unit of earlier installations; nothing else may.
_NAMES_KR = "config.step.pricing.data_description.currency"
_ENGLISH = {
    _NAMES_KR: 'Unit shown on all cost sensors. Enter your prices in this currency. "kr" is the'
    " unit of installations created before 0.5.0. You can change it later in the options.",
    "options.step.pricing.data_description.currency": "Unit shown on all cost sensors. Enter"
    " your prices in this currency. Changing it relabels existing totals and statistics;"
    " nothing is converted.",
    "options.step.pricing.description": "Currency and static or spot pricing.",
    **{
        f"{step}.data_description.{key}": text
        for step in ("config.step.spot_config", "options.step.pricing")
        for key, text in (
            (
                "spot_price_entity",
                "Select the sensor that provides the current electricity spot price. It must"
                " report the price per kWh in your currency; the value is used as it is.",
            ),
            (
                "spot_additional_cost_kwh",
                "Grid fee + energy tax, excl. VAT (per kWh, in your currency)",
            ),
            (
                "spot_fallback_price_kwh",
                "Price used when spot sensor is unavailable (per kWh, in your currency)",
            ),
        )
    },
    **{
        f"config_subentries.user.step.{step}.data_description.price_per_kwh": "Fixed price per"
        " kWh in the installation''s currency (required for ''fixed'' method)"
        for step in ("guest_pricing", "reconfigure_guest_pricing")
    },
}


def test_no_kr_as_currency_word_and_currency_field_texts() -> None:
    """Spec 028 T-22 (D-69, D-76): strings.json is en.json, pinned above."""
    for name, label in (("translations/en.json", "Currency"), ("translations/sv.json", "Valuta")):
        texts = _flatten(_load(name))
        assert [k for k, v in texts.items() if v and _CURRENCY_WORD.search(v)] == [_NAMES_KR]
        assert [k for k, v in texts.items() if v and _SWEDISH.search(v)] == [], name
        for flow in ("config", "options"):
            assert texts[f"{flow}.step.pricing.data.currency"] == label
            assert texts[f"{flow}.step.pricing.data_description.currency"]
    assert [
        _flatten(_load(name))["config.error.currency_required"]
        for name in ("translations/en.json", "translations/sv.json")
    ] == ["Choose a currency.", "Välj valuta."]


def test_currency_texts_of_d76() -> None:
    """Spec 028 D-76: the English texts as decided; Swedish has a text for each."""
    english = _flatten(_load("translations/en.json"))
    swedish = _flatten(_load("translations/sv.json"))
    assert {key: english[key] for key in _ENGLISH} == _ENGLISH
    assert all(swedish[key] and swedish[key] != english[key] for key in _ENGLISH)
