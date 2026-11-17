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
