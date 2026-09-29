"""JavaScript value semantics the rules and document engines reproduce exactly.

The Flarize modules ported by ``engineering_checker``, ``bom_domain``, ``gate``, ``quotation_payload`` and
``content_fit`` print values into messages (``${quantity}``), test truthiness, coerce text to numbers, count string
length in UTF-16 code units and stamp ISO timestamps. The helpers below give those operations their JavaScript meaning
on Python values, with :data:`UNDEFINED` standing for a missing member (``None`` is JavaScript ``null``).

Numbers are :class:`~decimal.Decimal` (or ``int``); a ``float`` that reaches an engine (a JSON value parsed without
``parse_float=Decimal``) is read back as the decimal its JSON text meant (``repr``), never used in arithmetic. Pure
standard library; no clock, no environment.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Context, Decimal, InvalidOperation, localcontext
from typing import Any

__all__ = [
    "UNDEFINED",
    "Undefined",
    "EXACT",
    "is_nullish",
    "coalesce",
    "js_truthy",
    "js_number",
    "js_finite_number",
    "to_decimal",
    "is_number",
    "number_text",
    "js_string",
    "js_round",
    "round_places",
    "to_fixed",
    "js_trim",
    "utf16_len",
    "parse_iso_ms",
    "iso_from_ms",
    "format_en_in",
    "prop",
    "js_keys",
    "js_array",
]


class Undefined:
    """JavaScript ``undefined``: a missing member. Falsy; ``JSON.stringify`` drops it."""

    _instance: Undefined | None = None

    def __new__(cls) -> Undefined:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "UNDEFINED"

    def __reduce__(self) -> str:
        return "UNDEFINED"


UNDEFINED = Undefined()

#: Arithmetic context: 60 significant digits and every trap on, so sums and products of catalogue and payload figures
#: are exact (a result that would need rounding raises instead of silently losing digits).
EXACT = Context(prec=60, rounding=ROUND_HALF_UP, traps=[InvalidOperation])


def is_nullish(value: Any) -> bool:
    """``value == null`` in JavaScript: ``null`` or ``undefined``."""
    return value is None or value is UNDEFINED


def coalesce(*values: Any) -> Any:
    """``a ?? b ?? c``: the first value that is neither ``null`` nor ``undefined`` (else the last one)."""
    for value in values[:-1]:
        if not is_nullish(value):
            return value
    return values[-1]


def prop(obj: Any, key: str) -> Any:
    """``obj?.[key]``: the member, or :data:`UNDEFINED` when it is missing or ``obj`` is not an object."""
    if isinstance(obj, Mapping):
        return obj.get(key, UNDEFINED)
    return UNDEFINED


#: JavaScript digits are ASCII: every pattern that reads digits is ``re.ASCII`` (Python's ``\d`` would also accept
#: Malayalam, Devanagari, Arabic-Indic or full-width digits, which ``Number()`` and ``Date`` reject).
_ARRAY_INDEX = re.compile(r"0|[1-9]\d*", re.ASCII)


def js_keys(obj: Any) -> list[str]:
    """``Object.keys(obj)`` order: array-index keys ascending numerically, then the other keys in insertion order."""
    if not isinstance(obj, Mapping):
        return []
    keys = list(obj.keys())
    indexes = sorted((key for key in keys if isinstance(key, str) and _ARRAY_INDEX.fullmatch(key) and int(key) < 4294967295), key=int)
    index_set = set(indexes)
    return indexes + [key for key in keys if key not in index_set]


def js_array(value: Any) -> list | None:
    """``Array.isArray(value) ? value : null`` for JSON arrays (``list`` or ``tuple``)."""
    return list(value) if isinstance(value, (list, tuple)) else None


def is_number(value: Any) -> bool:
    """A JavaScript number: ``int`` or ``Decimal`` (``bool`` excluded)."""
    return isinstance(value, (int, Decimal)) and not isinstance(value, bool)


def to_decimal(value: int | Decimal | float) -> Decimal:
    """A number as a Decimal; a float is read back as the decimal its (shortest, JavaScript-equal) text meant."""
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(value)


def js_truthy(value: Any) -> bool:
    """JavaScript truthiness: ``null``, ``undefined``, ``false``, ``0``, ``NaN`` and ``''`` are falsy; objects are truthy."""
    if is_nullish(value) or value is False:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, Decimal, float)):
        number = to_decimal(value)
        return not (number.is_nan() or number == 0)
    if isinstance(value, str):
        return value != ""
    return True


_WHITESPACE = "\t\n\v\f\r                  　﻿"
_DECIMAL_LITERAL = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII)
_RADIX_LITERAL = re.compile(r"0([xX][0-9a-fA-F]+|[oO][0-7]+|[bB][01]+)", re.ASCII)
_NAN = Decimal("NaN")
_INFINITY = Decimal("Infinity")
#: Round-to-nearest-even binary64 limits, exact: a magnitude from 2^1024 − 2^970 up rounds to Infinity, one of at most
#: 2^-1075 (half the smallest subnormal) rounds to zero.
_BINARY64_OVERFLOW = Decimal(2**1024 - 2**970)
_BINARY64_UNDERFLOW = Decimal((0, tuple(int(digit) for digit in str(5**1075)), -1075))


def js_trim(text: str) -> str:
    """``String.prototype.trim``: strips JavaScript white space and line terminators (not Python's ``isspace`` set)."""
    return text.strip(_WHITESPACE)


def _binary64_range(number: Decimal) -> Decimal:
    """A number as JavaScript can hold it: a magnitude beyond binary64 overflows to ±Infinity or underflows to ±0
    (``Number('1e400')`` is ``Infinity``, ``Number('1e-400')`` is ``0``); every representable magnitude keeps its
    exact decimal (DV-24)."""
    if not number.is_finite() or number.is_zero() or -300 < number.adjusted() < 300:
        return number
    if abs(number) >= _BINARY64_OVERFLOW:
        return _INFINITY.copy_sign(number)
    if abs(number) <= _BINARY64_UNDERFLOW:
        return Decimal(0).copy_sign(number)
    return number


def js_number(value: Any) -> Decimal:
    """``Number(value)`` as a Decimal (``Decimal('NaN')`` for NaN, ``±Infinity`` for the infinities and for anything
    beyond the binary64 range, ``±0`` for what underflows it). Digits are ASCII only, as in JavaScript."""
    if value is None or value is False:
        return Decimal(0)
    if value is True:
        return Decimal(1)
    if value is UNDEFINED:
        return _NAN
    if isinstance(value, (int, Decimal, float)):
        return _binary64_range(to_decimal(value))
    if isinstance(value, str):
        text = js_trim(value)
        if text == "":
            return Decimal(0)
        if text in ("Infinity", "+Infinity"):
            return _INFINITY
        if text == "-Infinity":
            return -_INFINITY
        if _DECIMAL_LITERAL.fullmatch(text):
            return _binary64_range(Decimal(text))
        radix = _RADIX_LITERAL.fullmatch(text)
        if radix:
            base = {"x": 16, "o": 8, "b": 2}[radix.group(1)[0].lower()]
            return _binary64_range(Decimal(int(radix.group(1)[1:], base)))
        return _NAN
    if isinstance(value, (list, tuple)):
        return js_number(js_string(value))
    return _NAN


def js_finite_number(value: Any) -> Decimal | None:
    """The checker's ``num()``: ``null``/``undefined``/``''`` → ``None``; otherwise ``Number(v)`` when finite, else ``None``."""
    if is_nullish(value) or value == "":
        return None
    number = js_number(value)
    return number if number.is_finite() else None


def number_text(value: int | Decimal) -> str:
    """``String(number)``: integers without a fraction, no trailing zeros, exponent form below 1e-6 or from 1e21."""
    number = to_decimal(value) if not isinstance(value, Decimal) else value
    if number.is_nan():
        return "NaN"
    if number.is_infinite():
        return "Infinity" if number > 0 else "-Infinity"
    if number == 0:
        return "0"
    number = number.normalize(EXACT)
    magnitude = abs(number)
    if Decimal("1e-6") <= magnitude < Decimal("1e21"):
        text = format(number, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text
    sign, digits, exponent = number.as_tuple()
    mantissa = "".join(map(str, digits))
    power = exponent + len(digits) - 1
    head = mantissa[0] + ("." + mantissa[1:] if len(mantissa) > 1 else "")
    return f"{'-' if sign else ''}{head}e{'+' if power >= 0 else '-'}{abs(power)}"


def js_string(value: Any) -> str:
    """``String(value)`` / template-literal interpolation."""
    if value is None:
        return "null"
    if value is UNDEFINED:
        return "undefined"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, Decimal, float)):
        return number_text(to_decimal(value))
    if isinstance(value, (list, tuple)):
        return ",".join("" if is_nullish(item) else js_string(item) for item in value)
    return "[object Object]"


def js_round(value: int | Decimal) -> Decimal:
    """``Math.round``: the nearest integer, halves toward +∞ (``-2.5`` → ``-2``)."""
    with localcontext(EXACT):
        return (to_decimal(value) + Decimal("0.5")).to_integral_value(rounding=ROUND_FLOOR)


def round_places(value: int | Decimal, places: int) -> Decimal:
    """``Math.round(x × 10^p) / 10^p`` on the exact value (the JavaScript ``round1``/``round2`` helpers)."""
    scale = Decimal(10) ** places
    with localcontext(EXACT):
        return js_round(to_decimal(value) * scale) / scale


def to_fixed(value: int | Decimal, digits: int) -> str:
    """``Number.prototype.toFixed(digits)`` on the exact value: halves away from zero, always ``digits`` decimals."""
    number = to_decimal(value)
    quantum = Decimal(1).scaleb(-digits)
    with localcontext(EXACT):
        rounded = abs(number).quantize(quantum, rounding=ROUND_HALF_UP)
    text = format(rounded, "f")
    return f"-{text}" if number < 0 else text  # (-0.04).toFixed(1) is "-0.0"


def utf16_len(text: str) -> int:
    """``String.prototype.length``: UTF-16 code units (a character outside the BMP counts twice)."""
    return len(text) + sum(1 for character in text if ord(character) > 0xFFFF)


_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?(Z|[+-]\d{2}:\d{2})?)?", re.ASCII)
_EPOCH_DAYS = 719163  # date(1970, 1, 1).toordinal()


def parse_iso_ms(text: Any) -> int | None:
    """Milliseconds since the epoch of an ISO-8601 date or date-time (``Date.parse`` on its ISO form), else ``None``.

    A date alone is UTC midnight; a date-time without an offset is read as UTC (the legacy server ran in UTC).
    Anything that is not an ISO-8601 string — ``null``, a number, ``'September 1, 2026'`` — is ``None``.
    """
    if not isinstance(text, str):
        return None
    match = _ISO.fullmatch(text)
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    try:
        days = date(int(year), int(month), int(day)).toordinal() - _EPOCH_DAYS
    except ValueError:
        return None
    hours, minutes, seconds = int(hour or 0), int(minute or 0), int(second or 0)
    if hours > 24 or minutes > 59 or seconds > 59 or (hours == 24 and (minutes or seconds or int(fraction or 0))):
        return None
    millis = int((fraction or "0")[:3].ljust(3, "0"))
    total = ((days * 24 + hours) * 60 + minutes) * 60_000 + seconds * 1000 + millis
    if offset and offset != "Z":
        sign = 1 if offset[0] == "+" else -1
        total -= sign * (int(offset[1:3]) * 60 + int(offset[4:6])) * 60_000
    return total


def iso_from_ms(millis: int) -> str:
    """``Date.prototype.toISOString``: ``YYYY-MM-DDTHH:MM:SS.sssZ`` (UTC)."""
    days, rest = divmod(millis, 86_400_000)
    day = date.fromordinal(days + _EPOCH_DAYS)
    hours, rest = divmod(rest, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis_part = divmod(rest, 1000)
    return f"{day.year:04d}-{day.month:02d}-{day.day:02d}T{hours:02d}:{minutes:02d}:{seconds:02d}.{millis_part:03d}Z"


def format_en_in(value: int | Decimal) -> str:
    """``Number.prototype.toLocaleString('en-IN')``: Indian digit grouping (``1,23,456``), at most three decimals."""
    number = to_decimal(value)
    with localcontext(EXACT):
        rounded = abs(number).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    whole, _, fraction = format(rounded, "f").partition(".")
    fraction = fraction.rstrip("0")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    text = whole + (f".{fraction}" if fraction else "")
    return f"-{text}" if number < 0 and rounded != 0 else text
