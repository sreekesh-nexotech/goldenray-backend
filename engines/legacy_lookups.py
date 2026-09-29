"""How the legacy Django endpoints compared request values with table columns — reproduced without a database.

The website calculators and the EMI calculator of the legacy main backend (Django 5.2, PostgreSQL) looked rows up
with ORM filters on raw request values. The ORM prepared each value **when the filter was built** (whether or not
any row existed) and the ports in ``engines.website_calculators`` / ``engines.emi`` must prepare them identically,
or the same request would select a different row or fail differently:

* ``CharField`` ``exact`` (``filter(pincode=v)``, ``filter(model=v)``): ``None`` → ``IS NULL`` (never equal to a
  stored text); anything else is compared as ``str(v)`` — ``682001`` matches ``"682001"``, ``682001.0`` does not;
* ``CharField`` ``iexact`` (``filter(name__iexact=v)``): ``None`` → ``IS NULL``; the value is sent to PostgreSQL
  as it is (the PostgreSQL backend neither converts nor escapes it) and compared as ``UPPER(column) = UPPER(v)`` —
  a text compares case-insensitively, any other JSON value (number, boolean, list, object) made PostgreSQL fail
  (``function upper(integer) does not exist``: HTTP 500);
* a text parameter containing NUL cannot be sent to PostgreSQL: the legacy endpoint crashed (HTTP 500);
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


def _text(value) -> str:
    text = value if isinstance(value, str) else str(value)
    if "\x00" in text:
        raise LegacyCrash(NUL_MESSAGE)
    return text


def text_param(value) -> str | None:
    """The parameter of ``filter(column=value)`` on a text column (``None`` = ``IS NULL``, matching no stored text)."""
    return None if value is None else _text(value)


def exact_text(value) -> Callable[[str | None], bool]:
    """``filter(column=value)`` on a text column, as a predicate over the stored value."""
    param = text_param(value)
    return lambda column: param is not None and column is not None and column == param


def iexact_text(value) -> Callable[[str | None], bool]:
    """``filter(column__iexact=value)`` on PostgreSQL: ``UPPER(column) = UPPER(value)`` for a text value."""
    if value is None:
        return lambda column: False
    if not isinstance(value, str):
        raise LegacyCrash(f"function upper({type(value).__name__}) does not exist")
    param = _text(value).upper()
    return lambda column: column is not None and column.upper() == param


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


def check_finite(payload) -> None:
    """The legacy JSON renderer refused ``inf``/``nan`` (DRF ``STRICT_JSON``): such a response was a 500."""
    if isinstance(payload, float):
        if not math.isfinite(payload):
            raise LegacyCrash("Out of range float values are not JSON compliant")
    elif isinstance(payload, dict):
        for item in payload.values():
            check_finite(item)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            check_finite(item)
