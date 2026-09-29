"""Deep-frozen JSON documents: the Python form of the Flarize ``deepFreeze`` / ``JSON.parse(JSON.stringify(x))`` idiom.

Issued documents (the quotation payload, the commercial and BOM snapshots, the commercial freeze) are values: once
built they must never change. :func:`deep_freeze` copies a JSON-shaped value into immutable containers —
:class:`FrozenDict` (a ``dict`` whose mutators raise ``TypeError``, as a frozen object throws in strict-mode
JavaScript) and ``tuple`` — so no live reference to master data survives into a frozen document. :func:`thaw` turns it
back into plain ``dict``/``list`` for storage, and :func:`to_json` / :func:`sha256_hex` give the canonical JSON text
(JavaScript number formatting) the platform stores beside ``document_payload``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from engines.jscompat import UNDEFINED, number_text, to_decimal

__all__ = ["FrozenDict", "deep_freeze", "thaw", "jsonable", "to_json", "sha256_hex", "is_frozen"]


class FrozenDict(dict):
    """A ``dict`` that refuses every mutation (``TypeError``). Equal to, and JSON-serialisable like, a plain dict."""

    __slots__ = ()

    def _refuse(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("frozen document: cannot be modified")

    __setitem__ = __delitem__ = _refuse  # type: ignore[assignment]
    clear = pop = popitem = setdefault = update = _refuse  # type: ignore[assignment]

    def __ior__(self, other: Any) -> FrozenDict:  # type: ignore[override]
        raise TypeError("frozen document: cannot be modified")

    def __copy__(self) -> FrozenDict:
        return self

    def __deepcopy__(self, memo: dict) -> FrozenDict:
        return self

    def __reduce__(self) -> tuple:
        return (FrozenDict, (dict(self),))

    def __repr__(self) -> str:
        return f"FrozenDict({dict.__repr__(self)})"


def _key(key: Any) -> str:
    if isinstance(key, str):
        return str.__str__(key)
    raise TypeError(f"document keys must be strings, not {type(key).__name__}")


def deep_freeze(value: Any) -> Any:
    """An immutable deep copy of a JSON-shaped value (``JSON.parse(JSON.stringify(v))`` then ``deepFreeze``).

    Mappings become :class:`FrozenDict`, sequences ``tuple``; members holding :data:`~engines.jscompat.UNDEFINED`
    are dropped and ``UNDEFINED`` array items become ``None`` (as ``JSON.stringify`` does); a ``float`` becomes the
    Decimal its JSON text meant (NaN/±∞ → ``None``). Objects with ``as_dict()`` are frozen through it.
    """
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, str):
        return str.__str__(value)  # a StrEnum member becomes its plain value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, float):
        return to_decimal(value) if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return FrozenDict((_key(key), deep_freeze(item)) for key, item in value.items() if item is not UNDEFINED)
    if isinstance(value, (list, tuple)):
        return tuple(None if item is UNDEFINED else deep_freeze(item) for item in value)
    if hasattr(value, "as_dict"):
        return deep_freeze(value.as_dict())
    if value is UNDEFINED:
        return None
    raise TypeError(f"not a JSON value: {type(value).__name__}")


def is_frozen(value: Any) -> bool:
    """True when ``value`` and everything inside it is immutable (what ``Object.isFrozen`` checks, deeply)."""
    if isinstance(value, FrozenDict):
        return all(is_frozen(item) for item in value.values())
    if isinstance(value, tuple):
        return all(is_frozen(item) for item in value)
    return value is None or isinstance(value, (str, bool, int, Decimal))


def thaw(value: Any) -> Any:
    """A mutable deep copy (``dict``/``list``) of a frozen document, ready for a JSONB column."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return value


def jsonable(value: Any) -> Any:
    """Plain ``json``-serialisable data for a JSONB column (``JSONField`` without a custom encoder): containers thawed,
    a Decimal as ``int`` when whole, else the ``float`` its shortest text denotes (``Decimal('2.50')`` → ``2.5``)."""
    if isinstance(value, Mapping):
        return {key: jsonable(item) for key, item in value.items() if item is not UNDEFINED}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        return int(value) if value == value.to_integral_value() else float(number_text(value))
    if value is UNDEFINED:
        return None
    return value


def _encode(value: Any, sort_keys: bool) -> str:
    if value is None or value is UNDEFINED:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, Decimal, float)):
        number = to_decimal(value)
        return number_text(number) if number.is_finite() else "null"
    if isinstance(value, Mapping):
        items = sorted(value.items()) if sort_keys else value.items()
        return "{" + ",".join(f"{json.dumps(_key(key), ensure_ascii=False)}:{_encode(item, sort_keys)}" for key, item in items if item is not UNDEFINED) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode(item, sort_keys) for item in value) + "]"
    if hasattr(value, "as_dict"):
        return _encode(value.as_dict(), sort_keys)
    raise TypeError(f"not a JSON value: {type(value).__name__}")


def to_json(value: Any, *, sort_keys: bool = False) -> str:
    """``JSON.stringify(value)``: compact, numbers in JavaScript form (``229000``, ``0.1``), non-ASCII kept."""
    return _encode(value, sort_keys)


def sha256_hex(value: Any) -> str:
    """SHA-256 of the canonical JSON (sorted keys, compact, UTF-8) — ``document_payload_sha256``."""
    return hashlib.sha256(to_json(value, sort_keys=True).encode("utf-8")).hexdigest()
