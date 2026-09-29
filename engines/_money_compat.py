"""The Flarize money rule (``src/lib/money.js``, ``money.1``) for the commercial engines.

The float-typed twin of engines-core's ``engines.money``. Same rule, different number type: ``engines.money`` is
Decimal-only and raises ``TypeError`` for a ``float``, while the commercial engines compute in doubles for golden parity
(DV-17) and call this rule with floats on every path. Keep this module after the merge; do not switch the commercial
engines to ``engines.money`` (docs/decisions/engines-commercial.md, "Merge notes").

Rule: compute at full double precision, round once when a figure is published, half away from zero to whole rupees,
with an epsilon nudge so binary representation error never moves a rupee (1.005 → 1.01 at two places; here 0.5 → 1).
Where published parts sit side by side (goods GST + service GST = total GST) the total is the sum of the rounded
parts — that composition is the caller's job.
"""

from __future__ import annotations

import math
import sys
from typing import Any, Iterable

from engines._jscompat import is_nullish, js_number, js_round, number_or_zero

CURRENCY = "INR"
ROUNDING_MODE = "HALF_UP"
ROUNDING_UNIT = 1
MONEY_RULE_VERSION = "money.1"

_EPSILON = sys.float_info.epsilon  # Number.EPSILON = 2**-52


def round_money(value: Any) -> int | float | None:
    """``roundMoney``: whole rupees, half away from zero; ``None`` for null or non-finite input."""
    if is_nullish(value):
        return None
    v = js_number(value)
    if not math.isfinite(v):
        return None
    sign = -1 if v < 0 else 1
    magnitude = abs(v)
    return sign * js_round(magnitude + _EPSILON * magnitude)


def sum_exact(values: Iterable[Any] | None) -> int | float:
    """``sumExact``: full-precision sum; non-numbers count as 0. Rounding is the caller's job."""
    total: int | float = 0
    for value in values or []:
        total = total + number_or_zero(value)
    return total


def mul_exact(a: Any, b: Any) -> int | float:
    """``mulExact``: full-precision product; non-numbers count as 0."""
    return number_or_zero(a) * number_or_zero(b)


def is_money(value: Any) -> bool:
    """``isMoney``: not null/undefined, not the empty string, and a finite ``Number(value)``."""
    if is_nullish(value) or value == "" and isinstance(value, str):
        return False
    return math.isfinite(js_number(value))
