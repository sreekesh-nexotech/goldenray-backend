"""JavaScript value semantics the Flarize ports need for byte parity (private to ``engines``).

The Flarize engines are JavaScript. Porting them "exactly" means reproducing a handful of language rules that differ
from Python's: truthiness (``[]`` and ``{}`` are truthy), ``||`` / ``??``, ``Number()`` coercion, ``Math.round``
(half toward +infinity), ``Number.prototype.toFixed`` and ``String(number)`` formatting, the key order of
``Object.keys`` (integer-like keys first) and ``JSON.stringify`` equality. Missing object properties are
``UNDEFINED`` (distinct from ``None`` = JSON ``null``) wherever the JS code distinguishes them.

This is the JavaScript-number (binary64) side of the ports; :mod:`engines.jscompat` is the Decimal side used by the
rules and document engines. The rules that do not depend on the number type — white space (``js_trim``), the ASCII-only
number literals ``Number()`` accepts and what counts as an array index — are defined once, in :mod:`engines.jscompat`,
and used here (consolidated at the wave-1 integration). ``UNDEFINED`` stays per side: results cross between the two
only as JSON-like data (``clean``/``deep_freeze`` drop it).

Nothing here imports Django or any app (import-linter contract ``engines-pure``).
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable

from engines.jscompat import DECIMAL_LITERAL, RADIX_LITERAL, is_array_index, js_trim


class _Undefined:
    """JavaScript ``undefined``: a missing property. Falsy, never serialised (dropped from objects like JSON.stringify)."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self):
        return False

    def __repr__(self):
        return "undefined"

    def __reduce__(self):
        return (_Undefined, ())


UNDEFINED = _Undefined()


class JsError(Exception):
    """Base of every error a ported engine raises where the JavaScript engine throws.

    ``js_name`` is the JS error class (``Error``, ``PackConfigError`` …), ``code`` the machine code the JS error
    carried (``None`` when it had none) and ``detail`` its JSON detail.
    """

    js_name = "Error"

    def __init__(self, message: str, code: str | None = None, detail: Any = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail

    def as_dict(self) -> dict:
        return {"name": self.js_name, "message": self.message, "code": self.code, "detail": self.detail}


# ---------------------------------------------------------------------------------------------------------------
# Type tests and coercions
# ---------------------------------------------------------------------------------------------------------------


def is_nullish(value: Any) -> bool:
    return value is None or value is UNDEFINED


def is_number(value: Any) -> bool:
    """``typeof value === 'number'`` (NaN and infinities included)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_num(value: Any) -> bool:
    """``typeof value === 'number' && Number.isFinite(value)`` — no coercion."""
    return is_number(value) and math.isfinite(value)


def is_integer(value: Any) -> bool:
    """``Number.isInteger(value)`` — no coercion."""
    return is_num(value) and float(value).is_integer()


def is_array(value: Any) -> bool:
    return isinstance(value, (list, tuple))


def is_object(value: Any) -> bool:
    """``value != null && typeof value === 'object'`` (arrays included)."""
    return isinstance(value, (dict, list, tuple))


def truthy(value: Any) -> bool:
    """JavaScript truthiness: ``[]`` and ``{}`` are truthy; ``0``, ``NaN``, ``''``, ``null`` and ``undefined`` are not."""
    if value is None or value is UNDEFINED or value is False:
        return False
    if value is True:
        return True
    if is_number(value):
        return value == value and value != 0
    if isinstance(value, str):
        return value != ""
    return True


def js_or(*values: Any) -> Any:
    """``a || b || c``: the first truthy operand, else the last one."""
    for value in values[:-1]:
        if truthy(value):
            return value
    return values[-1]


def nullish(*values: Any) -> Any:
    """``a ?? b ?? c``: the first operand that is neither null nor undefined, else the last one."""
    for value in values[:-1]:
        if not is_nullish(value):
            return value
    return values[-1]


# White space, number literals and array indexes follow the value rules of :mod:`engines.jscompat` (one definition for
# every port): JavaScript digits are ASCII, so ``Number('൩')`` is NaN and ``'1\n'`` is not an array index.


def js_number(value: Any) -> int | float:
    """``Number(value)``."""
    if value is UNDEFINED:
        return math.nan
    if value is None or value is False:
        return 0
    if value is True:
        return 1
    if is_number(value):
        # Python ints are exact at any size; a JS number is a double. Beyond 2**53 only the double exists.
        return float(value) if isinstance(value, int) and abs(value) >= 2**53 else value
    if isinstance(value, Decimal):
        return decimal_to_number(value)
    if isinstance(value, str):
        text = js_trim(value)
        if text == "":
            return 0
        if text in ("Infinity", "+Infinity", "-Infinity"):
            return -math.inf if text.startswith("-") else math.inf
        if DECIMAL_LITERAL.fullmatch(text):
            number = float(text)
            return int(number) if number.is_integer() and abs(number) < 2**53 and not ("." in text or "e" in text.lower()) else number
        if RADIX_LITERAL.fullmatch(text):
            return int(text, 0)
        return math.nan
    if is_array(value):
        return js_number(js_str(value))
    return math.nan


def js_add(a: Any, b: Any) -> Any:
    """``a + b``: string concatenation when either operand is a string (``'4' + 3 === '43'``), else numeric."""
    if isinstance(a, str) or isinstance(b, str) or is_array(a) or is_array(b) or isinstance(a, dict) or isinstance(b, dict):
        return js_str(a) + js_str(b)
    return js_number(a) + js_number(b)


def number_or_zero(value: Any) -> int | float:
    """``Number(value) || 0``."""
    number = js_number(value)
    return number if truthy(number) else 0


def decimal_to_number(value: Decimal) -> int | float:
    """A Decimal as the JS number it denotes (int when integral, else the nearest double)."""
    if value == value.to_integral_value() and abs(value) < 2**53:
        return int(value)
    return float(value)


def json_numbers(value: Any) -> Any:
    """Recursively replace ``Decimal`` by JS numbers so service data can be passed to an engine."""
    if isinstance(value, Decimal):
        return decimal_to_number(value)
    if isinstance(value, dict):
        return {k: json_numbers(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_numbers(v) for v in value]
    return value


def parse_float(value: Any) -> float:
    """``parseFloat(value)``: the longest decimal prefix of the trimmed string ('5sp' → 5)."""
    text = js_trim(js_str(value))
    match = re.match(r"^[+-]?(?:Infinity|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)", text, re.ASCII)
    if not match:
        return math.nan
    literal = match.group(0)
    if literal.endswith("Infinity"):
        return -math.inf if literal.startswith("-") else math.inf
    return float(literal)


# ---------------------------------------------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------------------------------------------


def _as_int(value: float) -> int | float:
    return int(value) if math.isfinite(value) and abs(value) < 2**53 else value


def js_round(value: Any) -> int | float:
    """``Math.round``: nearest integer, ties toward +infinity (``Math.round(-2.5) === -2``)."""
    x = js_number(value)
    if not math.isfinite(x):
        return x
    floor = math.floor(x)
    return _as_int(floor + 1 if x - floor >= 0.5 else floor)


def js_div(a: Any, b: Any) -> int | float:
    """``a / b`` on JS numbers: a zero divisor gives ±Infinity (NaN for 0/0 or NaN/0) instead of raising."""
    x, y = js_number(a), js_number(b)
    if y == 0:
        if x != x or x == 0:
            return math.nan
        return math.copysign(math.inf, x) * math.copysign(1.0, y)
    return x / y


def js_floor(value: Any) -> int | float:
    x = js_number(value)
    return _as_int(math.floor(x)) if math.isfinite(x) else x


def js_ceil(value: Any) -> int | float:
    x = js_number(value)
    return _as_int(math.ceil(x)) if math.isfinite(x) else x


def js_max(*values: Any) -> int | float:
    numbers = [js_number(v) for v in values]
    if any(n != n for n in numbers):
        return math.nan
    return max(numbers) if numbers else -math.inf


def js_min(*values: Any) -> int | float:
    numbers = [js_number(v) for v in values]
    if any(n != n for n in numbers):
        return math.nan
    return min(numbers) if numbers else math.inf


def js_to_fixed(value: Any, digits: int) -> str:
    """``Number.prototype.toFixed``: exact binary value, ties away from zero (the larger magnitude)."""
    x = js_number(value)
    if x != x:
        return "NaN"
    if abs(x) >= 1e21 or math.isinf(x):
        return js_str(x)
    if x == 0:
        x = 0.0
    quantum = Decimal(1).scaleb(-digits)
    return format(Decimal(x).quantize(quantum, rounding=ROUND_HALF_UP), "f")


def to_base36(value: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    out = []
    n = abs(int(value))
    while n:
        n, r = divmod(n, 36)
        out.append(digits[r])
    return ("-" if value < 0 else "") + "".join(reversed(out))


# ---------------------------------------------------------------------------------------------------------------
# Strings
# ---------------------------------------------------------------------------------------------------------------


def _number_to_string(x: int | float) -> str:
    if isinstance(x, int) and abs(x) < 2**53:
        return str(x) if abs(x) < 10**21 else _number_to_string(float(x))
    x = float(x)
    if x != x:
        return "NaN"
    if math.isinf(x):
        return "Infinity" if x > 0 else "-Infinity"
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    digits_tuple = Decimal(repr(abs(x))).normalize().as_tuple()
    digits = "".join(str(d) for d in digits_tuple.digits)
    k = len(digits)
    n = digits_tuple.exponent + k
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        exponent = n - 1
        mantissa = digits[0] + ("." + digits[1:] if k > 1 else "")
        body = f"{mantissa}e{'+' if exponent >= 0 else '-'}{abs(exponent)}"
    return sign + body


def js_str(value: Any) -> str:
    """``String(value)`` / template-literal interpolation."""
    if value is UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if is_number(value):
        return _number_to_string(value)
    if isinstance(value, Decimal):
        return _number_to_string(decimal_to_number(value))
    if isinstance(value, str):
        return value
    if is_array(value):
        return js_join(value, ",")
    return "[object Object]"


def js_join(values: Iterable[Any], separator: str = ",") -> str:
    """``Array.prototype.join``: null and undefined elements become empty strings."""
    return separator.join("" if is_nullish(v) else js_str(v) for v in values)


def js_str_lt(a: str, b: str) -> bool:
    """``a < b`` on JS strings (UTF-16 code-unit order)."""
    if a.isascii() and b.isascii():
        return a < b
    return a.encode("utf-16-be") < b.encode("utf-16-be")


def js_str_key(value: str) -> Any:
    """Sort key giving JS (UTF-16 code-unit) string order."""
    return value if value.isascii() else value.encode("utf-16-be")


# ICU root collation order of the ASCII punctuation and symbols (all sort before digits, digits before letters).
_ICU_PUNCTUATION = " _-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$"


def _collation_element(ch: str) -> tuple:
    if ch.isspace():
        return (0, 0, ord(ch))
    index = _ICU_PUNCTUATION.find(ch)
    if index >= 0:
        return (0, index, 0)
    if ch.isdigit():
        return (1, unicodedata.digit(ch, 0), 0)
    if ch.isalpha():
        base = unicodedata.normalize("NFD", ch)[0].casefold()
        return (2, base, 0)
    return (0, len(_ICU_PUNCTUATION) + ord(ch), 0)


def locale_compare(a: str, b: str) -> int:
    """Approximation of ``a.localeCompare(b)`` under the ICU root collation (Node's default).

    Primary: punctuation < digits < letters (case- and accent-insensitive); then accents; then lower case before
    upper case; then code points. Exact for the ASCII identifiers and ISO timestamps the engines compare.
    """
    primary_a = [_collation_element(c) for c in a]
    primary_b = [_collation_element(c) for c in b]
    if primary_a != primary_b:
        return -1 if primary_a < primary_b else 1
    secondary_a = unicodedata.normalize("NFD", a)
    secondary_b = unicodedata.normalize("NFD", b)
    if secondary_a.casefold() != secondary_b.casefold():
        return -1 if secondary_a.casefold() < secondary_b.casefold() else 1
    tertiary_a = [0 if c.islower() else 1 for c in a]
    tertiary_b = [0 if c.islower() else 1 for c in b]
    if tertiary_a != tertiary_b:
        return -1 if tertiary_a < tertiary_b else 1
    if a == b:
        return 0
    return -1 if js_str_lt(a, b) else 1


# ---------------------------------------------------------------------------------------------------------------
# Objects
# ---------------------------------------------------------------------------------------------------------------


def js_keys(obj: Any) -> list:
    """``Object.keys``: integer-like keys ascending first, then the others in insertion order."""
    if isinstance(obj, dict):
        keys = list(obj.keys())
        integers = sorted((k for k in keys if is_array_index(k)), key=int)
        return integers + [k for k in keys if not is_array_index(k)]
    if is_array(obj):
        return [str(i) for i in range(len(obj))]
    if isinstance(obj, str):
        return [str(i) for i in range(len(obj))]
    return []


def js_entries(obj: Any) -> list[tuple]:
    if isinstance(obj, dict):
        return [(k, obj[k]) for k in js_keys(obj)]
    if is_array(obj):
        return [(str(i), v) for i, v in enumerate(obj)]
    if isinstance(obj, str):
        return [(str(i), c) for i, c in enumerate(obj)]
    return []


def js_values(obj: Any) -> list:
    return [v for _, v in js_entries(obj)]


def prop_key(key: Any) -> str:
    """The property key a value becomes when used as ``obj[key]``."""
    return key if isinstance(key, str) else js_str(key)


def jsget(obj: Any, key: Any) -> Any:
    """``obj?.[key]``: UNDEFINED when the property does not exist (or ``obj`` is nullish)."""
    if isinstance(obj, dict):
        return obj.get(prop_key(key), UNDEFINED)
    if is_array(obj):
        k = prop_key(key)
        if is_array_index(k) and int(k) < len(obj):
            return obj[int(k)]
        if k == "length":
            return len(obj)
        return UNDEFINED
    if isinstance(obj, str):
        k = prop_key(key)
        if is_array_index(k) and int(k) < len(obj):
            return obj[int(k)]
        if k == "length":
            return len(obj)
    return UNDEFINED


def jsget_path(obj: Any, *keys: Any) -> Any:
    """``obj?.[k1]?.[k2]…``."""
    for key in keys:
        if is_nullish(obj):
            return UNDEFINED
        obj = jsget(obj, key)
    return obj


def includes(container: Any, item: Any) -> bool:
    """``Array.prototype.includes`` (SameValueZero) or ``String.prototype.includes``; false for anything else."""
    if isinstance(container, str):
        return js_str(item) in container
    if is_array(container):
        return any(strict_equal(element, item) or (_is_nan(element) and _is_nan(item)) for element in container)
    return False


def _is_nan(value: Any) -> bool:
    return is_number(value) and value != value


def strict_equal(a: Any, b: Any) -> bool:
    """``a === b``."""
    if is_number(a) and is_number(b):
        return a == b
    if is_number(a) or is_number(b):
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if a is None or b is None or a is UNDEFINED or b is UNDEFINED:
        return a is b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if isinstance(a, str) or isinstance(b, str):
        return False
    return a is b


def clean(obj: dict) -> dict:
    """Drop UNDEFINED-valued keys (what ``JSON.stringify`` does to ``{key: undefined}``)."""
    return {k: v for k, v in obj.items() if v is not UNDEFINED}


def js_stringify(value: Any) -> Any:
    """``JSON.stringify(value)`` (compact). Returns UNDEFINED for a top-level undefined, like JS."""
    if value is UNDEFINED:
        return UNDEFINED
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if is_number(value) or isinstance(value, Decimal):
        number = js_number(value)
        return js_str(number) if math.isfinite(number) else "null"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if is_array(value):
        return "[" + ",".join("null" if v is UNDEFINED else js_stringify(v) for v in value) + "]"
    if isinstance(value, dict):
        parts = []
        for key in js_keys(value):
            item = value[key]
            if item is UNDEFINED:
                continue
            parts.append(json.dumps(str(key), ensure_ascii=False) + ":" + js_stringify(item))
        return "{" + ",".join(parts) + "}"
    return UNDEFINED


def json_equal(a: Any, b: Any) -> bool:
    """``JSON.stringify(a) === JSON.stringify(b)``."""
    return js_stringify(a) == js_stringify(b)
