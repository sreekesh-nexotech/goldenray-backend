"""Loading and comparing the JavaScript golden files (``engines/tests/golden/core_*.json``).

Numbers are compared as **decimal strings**: the JavaScript's JSON number text parsed as a Decimal against the Python
Decimal, both normalised (``7.90`` = ``7.9``, ``5008`` = ``5008.0``). JSON drops ``undefined`` members, so a key the
JavaScript result does not carry matches a Python ``None``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Any

from engines.money import canonical

GOLDEN = Path(__file__).resolve().parent / "golden"
FIXED_NOW = "2026-09-28T00:00:00.000Z"


@cache
def load(name: str) -> dict[str, Any]:
    with (GOLDEN / name).open(encoding="utf-8") as handle:
        return json.load(handle, parse_float=Decimal)


def is_number(value: Any) -> bool:
    return isinstance(value, (int, Decimal)) and not isinstance(value, bool)


@dataclass(frozen=True)
class Difference:
    path: str
    javascript: Any
    python: Any

    def __str__(self) -> str:
        return f"{self.path}: javascript {self.javascript!r} != python {self.python!r}"

    @property
    def key(self) -> tuple[str, str, str]:
        """``(path, javascript text, python text)`` — numbers in canonical decimal form."""
        return (self.path, text(self.javascript), text(self.python))


def text(value: Any) -> str:
    if is_number(value):
        return canonical(value)
    return json.dumps(value, default=str)


def differences(expected: Any, actual: Any, path: str = "$") -> list[Difference]:
    """Every field where ``actual`` (Python) differs from ``expected`` (the JavaScript's JSON)."""
    if isinstance(actual, Enum):
        actual = actual.value
    if isinstance(actual, tuple):
        actual = list(actual)
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [Difference(path, expected, actual)]
        found = []
        for key in expected:
            if key not in actual:
                found.append(Difference(f"{path}.{key}", expected[key], "<missing>"))
            else:
                found.extend(differences(expected[key], actual[key], f"{path}.{key}"))
        found.extend(Difference(f"{path}.{key}", "<missing>", actual[key]) for key in actual if key not in expected and actual[key] is not None)
        return found
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return [Difference(path, expected, actual)]
        found = []
        for index, (left, right) in enumerate(zip(expected, actual)):
            found.extend(differences(left, right, f"{path}[{index}]"))
        return found
    if is_number(expected) or is_number(actual):
        if is_number(expected) and is_number(actual) and canonical(expected) == canonical(actual):
            return []
        return [Difference(path, expected, actual)]
    if type(expected) is not type(actual) or expected != actual:
        return [Difference(path, expected, actual)]
    return []
