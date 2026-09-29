"""How the legacy Django endpoints compared request values with table columns — reproduced without a database.

The website calculators and the EMI calculator of the legacy main backend (Django 5.2, PostgreSQL) looked rows up
with ORM filters on raw request values. The ORM prepared each value **when the filter was built** (whether or not
any row existed) and the ports in ``engines.website_calculators`` / ``engines.emi`` must prepare them identically,
or the same request would select a different row or fail differently:

* ``CharField`` ``exact`` (``filter(pincode=v)``, ``filter(model=v)``): ``None`` → ``IS NULL`` (never equal to a
  stored text); anything else is compared as ``str(v)`` — ``682001`` matches ``"682001"``, ``682001.0`` does not;
* ``CharField`` ``iexact`` (``filter(name__iexact=v)``): ``None`` → ``IS NULL``; the value is sent to PostgreSQL
  as it is (the PostgreSQL backend neither converts nor escapes it) and compared as ``UPPER(column) = UPPER(v)`` —
  a text compares case-insensitively (PostgreSQL's one-for-one case mapping, :func:`pg_upper`); a list psycopg2 sent
  as a text literal (``[]`` → ``'{}'``, all-``None`` leaves → ``'{NULL,…}'``) compares as that text; any other JSON
  value (number, boolean, object, any other list) made PostgreSQL fail (``function upper(integer) does not exist``:
  HTTP 500) — :func:`iexact_param`;
* a text parameter containing NUL, or a lone UTF-16 surrogate (``"\\ud800"`` is valid JSON but no UTF-8), cannot be
  sent to PostgreSQL: the legacy endpoint crashed (HTTP 500) — and so did its JSON renderer on a response carrying
  such a text (:func:`check_renderable`);
* ``IntegerField`` ``lte`` / ``gte`` with a float: ``lte`` truncates (``int(v)``), ``gte`` rounds up
  (``math.ceil``); ``inf``/``nan`` raise (500); a prepared value outside int32 matches every row or none
  (Django's ``FullResultSet`` / ``EmptyResultSet``);
* ``DecimalField`` lookups with a float: the float is rounded to the column's ``max_digits`` significant digits
  (``Context(prec=max_digits).create_decimal_from_float``); a non-finite value raised ``ValidationError`` (500).

A value the legacy endpoint crashed on raises :class:`LegacyCrash`; the ports turn every crash into a 400.
"""

from __future__ import annotations

import decimal
import math
from collections.abc import Callable
from decimal import Decimal

INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1
NUL_MESSAGE = "A string literal cannot contain NUL (0x00) characters."


class LegacyCrash(Exception):
    """The legacy endpoint raised an unhandled exception (HTTP 500) for this input."""


CRASH_DETAIL_LIMIT = 300


def crash_detail(exc: BaseException) -> str:
    """``"<class>: <message>"`` of a refused input's exception, for the log — cut to :data:`CRASH_DETAIL_LIMIT`
    characters: the message often quotes the visitor's value (``float("<a 2 MB text>")``)."""
    text = f"{type(exc).__name__}: {exc}".encode("utf-8", "backslashreplace").decode("utf-8")
    return text if len(text) <= CRASH_DETAIL_LIMIT else text[: CRASH_DETAIL_LIMIT - 1] + "…"


def _encodable(text: str) -> bool:
    """Whether ``text`` is UTF-8 text (a lone surrogate is not: psycopg and the JSON renderer both failed on it)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _text(value) -> str:
    text = value if isinstance(value, str) else str(value)
    if "\x00" in text:
        raise LegacyCrash(NUL_MESSAGE)
    if not _encodable(text):
        raise LegacyCrash("the text is not valid UTF-8 (a lone surrogate)")
    return text


def text_param(value) -> str | None:
    """The parameter of ``filter(column=value)`` on a text column (``None`` = ``IS NULL``, matching no stored text)."""
    return None if value is None else _text(value)


def exact_text(value) -> Callable[[str | None], bool]:
    """``filter(column=value)`` on a text column, as a predicate over the stored value."""
    param = text_param(value)
    return lambda column: param is not None and column is not None and column == param


def _pg_upper_char(char: str) -> str:
    """PostgreSQL ``UPPER()`` of one character in the legacy database (C.UTF-8): the Unicode *simple* mapping — one
    character for one — so a character whose full mapping is several letters (``ß``, ``ﬆ``) stays as it is, and the
    iota-subscript letters take their titlecase form (``ᾳ`` → ``ᾼ``). Python's ``str.upper()`` is the full mapping
    (``"ﬆ".upper() == "ST"``). Verified against ``UPPER(chr(i))`` for every code point of the legacy database."""
    upper = char.upper()
    if len(upper) == 1:
        return upper
    title = char.title()
    return title if len(title) == 1 else char


def pg_upper(text: str) -> str:
    """``UPPER(text)`` as the legacy PostgreSQL computed it (see :func:`_pg_upper_char`)."""
    return text.upper() if text.isascii() else "".join(_pg_upper_char(char) for char in text)


def _psycopg2_list_literal(value: list) -> str | None:
    """The text psycopg2 (the legacy driver) sent for a list, when it sent a text literal: ``'{}'`` for an empty list
    and ``'{NULL,…}'`` (nested ``{…}``) for a list whose leaves are all ``None``; ``None`` when it sent an
    ``ARRAY[…]`` construct instead (any other element, or an empty inner list, which becomes ``ARRAY[]``)."""
    if not value:
        return "{}"
    parts = []
    for item in value:
        if item is None:
            parts.append("NULL")
        elif isinstance(item, list) and item:
            inner = _psycopg2_list_literal(item)
            if inner is None:
                return None
            parts.append(inner)
        else:
            return None
    return "{" + ",".join(parts) + "}"


def iexact_param(value) -> str | None:
    """``UPPER(value)`` of ``filter(column__iexact=value)`` as PostgreSQL received it (``None`` = ``IS NULL``, matching
    no stored text). The value reached psycopg2 unconverted: a text is compared case-insensitively; a list psycopg2
    sent as a text literal (``[]`` → ``'{}'``, ``[None]`` → ``'{NULL}'``) is compared as that text; any other value
    (number, boolean, object, a list sent as ``ARRAY[…]``) made PostgreSQL fail — ``function upper(integer) does not
    exist``, ``can't adapt type 'dict'``, ``cannot determine type of empty array``: HTTP 500."""
    if value is None:
        return None
    if isinstance(value, list):
        literal = _psycopg2_list_literal(value)
        if literal is None:
            raise LegacyCrash("function upper(array) does not exist")
        return pg_upper(literal)
    if not isinstance(value, str):
        raise LegacyCrash(f"function upper({type(value).__name__}) does not exist")
    return pg_upper(_text(value))


def iexact_text(value) -> Callable[[str | None], bool]:
    """``filter(column__iexact=value)`` on PostgreSQL: ``UPPER(column) = UPPER(value)`` (see :func:`iexact_param`)."""
    param = iexact_param(value)
    if param is None:
        return lambda column: False
    return lambda column: column is not None and pg_upper(column) == param


def int_lte(value) -> Callable[[int], bool]:
    """``IntegerField`` ``column__lte=value`` (a float truncates; int32 overflow → every row, underflow → none)."""
    prepared = int(value)
    if prepared > INT32_MAX:
        return lambda column: True
    if prepared < INT32_MIN:
        return lambda column: False
    return lambda column: column <= prepared


def int_gte(value) -> Callable[[int], bool]:
    """``IntegerField`` ``column__gte=value`` (a float rounds up; int32 overflow → no row, underflow → every row)."""
    prepared = int(math.ceil(value) if isinstance(value, float) else value)
    if prepared > INT32_MAX:
        return lambda column: False
    if prepared < INT32_MIN:
        return lambda column: True
    return lambda column: column >= prepared


def float_exact(value) -> Callable[[float], bool]:
    """``FloatField`` ``column=value``: the value is compared as a float."""
    prepared = float(value)
    return lambda column: column == prepared


def decimal_param(value, max_digits: int) -> Decimal | None:
    """``DecimalField.to_python`` as used for lookups: a float is rounded to ``max_digits`` significant digits."""
    if value is None:
        return None
    try:
        prepared = decimal.Context(prec=max_digits).create_decimal_from_float(value) if isinstance(value, float) else Decimal(value)
    except (decimal.InvalidOperation, TypeError, ValueError):
        raise LegacyCrash(f"{value!r} value must be a decimal number.") from None
    if not prepared.is_finite():
        raise LegacyCrash(f"{value!r} value must be a decimal number.")
    return prepared


def check_renderable(payload) -> None:
    """The legacy JSON renderer refused ``inf``/``nan`` (DRF ``STRICT_JSON``) and any text that is not UTF-8 (a lone
    surrogate echoed from the request, as a value or a key): such a response was a 500.

    Walks the payload with an explicit stack, not recursion: ``calculate-solar`` echoes the visitor's
    ``property_type`` unchecked, and the legacy (C JSON parser and renderer) answered 200 for a value nested
    thousands of levels deep, far beyond Python's recursion limit."""
    pending = [payload]
    while pending:
        item = pending.pop()
        if isinstance(item, float):
            if not math.isfinite(item):
                raise LegacyCrash("Out of range float values are not JSON compliant")
        elif isinstance(item, str):
            if not _encodable(item):
                raise LegacyCrash("the response text is not valid UTF-8 (a lone surrogate)")
        elif isinstance(item, dict):
            for key, value in item.items():
                pending.append(key)
                pending.append(value)
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
