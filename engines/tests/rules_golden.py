"""Loading and comparing the engines-rules golden files (``engines/tests/golden/rules_*.json``).

The files are written by ``generate_rules.mjs`` from the real Flarize JavaScript. Large shared values are stored once
under ``refs`` and referenced as ``{"$ref": name}``; :func:`load` resolves them. Numbers are parsed as ``Decimal`` and
compared by value (``7.90`` = ``7.9``); everything else — keys, key presence, types, strings, list lengths — must be
identical (``JSON.stringify`` drops ``undefined``, and so does :func:`engines.frozen.thaw` of a frozen document).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

from engines.frozen import deep_freeze, thaw

GOLDEN = Path(__file__).resolve().parent / "golden"


def _resolve(value: Any, refs: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return _resolve(refs[value["$ref"]], refs)
        return {key: _resolve(item, refs) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, refs) for item in value]
    return value


@cache
def load(name: str) -> dict[str, Any]:
    """The golden file with every ``$ref`` resolved: ``{header, refs, sections}``."""
    with (GOLDEN / name).open(encoding="utf-8") as handle:
        raw = json.load(handle, parse_float=Decimal)
    refs = raw.get("refs", {})
    return {"header": raw["header"], "refs": {key: _resolve(value, refs) for key, value in refs.items()}, "sections": _resolve(raw["sections"], refs)}


def cases(name: str, section: str) -> list[dict[str, Any]]:
    return load(name)["sections"][section]


def ids(items: list[dict[str, Any]]) -> list[str]:
    return [str(item["id"]) for item in items]


def frozen(value: Any) -> Any:
    """A golden input as the engines receive it from the platform (deep-frozen, numbers as Decimal)."""
    return deep_freeze(value)


@dataclass(frozen=True)
class Difference:
    path: str
    javascript: Any
    python: Any

    def __str__(self) -> str:
        return f"{self.path}: javascript {self.javascript!r} != python {self.python!r}"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, Decimal)) and not isinstance(value, bool)


def normalise(value: Any) -> Any:
    """A Python result as plain JSON-shaped data (dataclasses through ``as_dict``, frozen containers thawed)."""
    return thaw(deep_freeze(value))


def differences(expected: Any, actual: Any) -> list[Difference]:
    """Every place where ``actual`` (a Python result) differs from ``expected`` (the JavaScript's JSON)."""
    return _diff(expected, normalise(actual), "$")


def _diff(expected: Any, actual: Any, path: str) -> list[Difference]:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [Difference(path, expected, actual)]
        found: list[Difference] = []
        for key in expected:
            if key not in actual:
                found.append(Difference(f"{path}.{key}", expected[key], "<missing>"))
            else:
                found.extend(_diff(expected[key], actual[key], f"{path}.{key}"))
        found.extend(Difference(f"{path}.{key}", "<missing>", actual[key]) for key in actual if key not in expected)
        return found
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return [Difference(path, expected, actual)]
        found = []
        for index, (left, right) in enumerate(zip(expected, actual)):
            found.extend(_diff(left, right, f"{path}[{index}]"))
        return found
    if _is_number(expected) or _is_number(actual):
        if _is_number(expected) and _is_number(actual) and Decimal(expected) == Decimal(actual):
            return []
        return [Difference(path, expected, actual)]
    if type(expected) is not type(actual) or expected != actual:
        return [Difference(path, expected, actual)]
    return []


def assert_same(expected: Any, actual: Any, *, allowed: dict[str, str] | None = None) -> None:
    """Exact equality, except the pinned divergences in ``allowed`` (``{path: kind}``), verified by kind."""
    found = differences(expected, actual)
    remaining = []
    for difference in found:
        kind = (allowed or {}).get(difference.path)
        if kind == "representation" and _is_number(difference.javascript) and _is_number(difference.python):
            # the JavaScript printed a binary64 artefact of the same product: |js − py| ≤ |py|·10⁻¹²
            js, py = Decimal(difference.javascript), Decimal(difference.python)
            assert abs(js - py) <= abs(py) * Decimal("1e-12"), difference
            continue
        remaining.append(difference)
    assert not remaining, "\n".join(str(item) for item in remaining[:20])
    for path in allowed or {}:
        assert any(difference.path == path for difference in found), f"pinned divergence {path} no longer occurs — unpin it"


def assert_error(expected: dict[str, Any], error: Exception) -> None:
    """The JavaScript threw ``{code, message}``: the Python exception carries the same message (and code when set)."""
    assert getattr(error, "message", str(error)) == expected["message"], (getattr(error, "message", str(error)), expected["message"])
    if expected.get("code"):
        assert str(getattr(error, "code", None)) == expected["code"]
