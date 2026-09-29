"""Template attribute slots: option documents and typed values.

* :func:`validate_slot_options` — the ``options`` document per slot type (``ENUM`` → ``{"choices": [...]}``,
  ``NUMBER`` → ``{"min", "max", "default"}``, every other type → ``{}``);
* :func:`coerce_value` — a value checked (and, for the legacy importer, leniently coerced) against its slot.
  ``None`` always means "no value"; required-ness is enforced only at publish time (drafts save loose).
"""

from __future__ import annotations

import datetime as dt
import numbers

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

from blog.models import TemplateAttributeSlot
from blog.services.common import invalid

Type = TemplateAttributeSlot.Type
EMPTY_VALUES = (None, "", [])
# Legacy CMS type names → platform type names (the importer maps them; the API accepts only the platform names).
LEGACY_TYPES = {"text": Type.TEXT, "richtext_blocks": Type.RICHTEXT_BLOCKS, "number": Type.NUMBER, "boolean": Type.BOOL, "bool": Type.BOOL, "date": Type.DATE, "enum": Type.ENUM, "url": Type.URL}
_TRUE = {"true", "1", "yes", "y", "t", "on"}
_FALSE = {"false", "0", "no", "n", "f", "off"}


def _is_number(value) -> bool:
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def validate_slot_options(slot_type: str, options) -> dict:
    """The normalised ``options`` document for ``slot_type`` (400 ``invalid_options`` otherwise)."""
    options = options or {}
    if not isinstance(options, dict):
        raise invalid("options", "Options must be an object.", "invalid_options")
    if slot_type == Type.ENUM:
        unknown = set(options) - {"choices"}
        choices = options.get("choices")
        if unknown or not isinstance(choices, list) or not choices or not all(isinstance(choice, str) and choice.strip() for choice in choices) or len(set(choices)) != len(choices):
            raise invalid("options", 'A choice slot needs {"choices": [...]}: a non-empty list of distinct, non-blank strings.', "invalid_options")
        return {"choices": list(choices)}
    if slot_type == Type.NUMBER:
        unknown = set(options) - {"min", "max", "default"}
        if unknown or not all(_is_number(options[key]) for key in options):
            raise invalid("options", 'A number slot accepts {"min", "max", "default"} with numeric values only.', "invalid_options")
        low, high, default = options.get("min"), options.get("max"), options.get("default")
        if low is not None and high is not None and low > high:
            raise invalid("options", "min must not exceed max.", "invalid_options")
        if default is not None and ((low is not None and default < low) or (high is not None and default > high)):
            raise invalid("options", "default must lie within min and max.", "invalid_options")
        return dict(options)
    if options:
        raise invalid("options", f"A {slot_type} slot takes no options.", "invalid_options")
    return {}


def coerce_value(slot: TemplateAttributeSlot, value, *, lenient: bool = False):
    """``value`` checked against ``slot`` (raises ``ValueError`` with a readable message). ``lenient`` coerces legacy strings."""
    if value is None:
        return None
    kind = slot.type
    if kind == Type.TEXT:
        if lenient and _is_number(value):
            value = str(value)
        if not isinstance(value, str):
            raise ValueError("must be text")
        return value
    if kind == Type.RICHTEXT_BLOCKS:
        if not isinstance(value, list) or not all(isinstance(block, dict) for block in value):
            raise ValueError("must be a list of rich-text blocks")
        return value
    if kind == Type.NUMBER:
        if lenient and isinstance(value, str):
            try:
                value = int(value) if value.strip().lstrip("-").isdigit() else float(value)
            except ValueError:
                raise ValueError("must be a number") from None
        if not _is_number(value):
            raise ValueError("must be a number")
        low, high = (slot.options or {}).get("min"), (slot.options or {}).get("max")
        if (low is not None and value < low) or (high is not None and value > high):
            raise ValueError(f"must be between {low if low is not None else '-∞'} and {high if high is not None else '∞'}")
        return value
    if kind == Type.BOOL:
        if lenient and isinstance(value, (str, int)) and not isinstance(value, bool):
            text = str(value).strip().lower()
            if text in _TRUE:
                return True
            if text in _FALSE:
                return False
        if not isinstance(value, bool):
            raise ValueError("must be true or false")
        return value
    if kind == Type.DATE:
        if not isinstance(value, str):
            raise ValueError("must be a date (YYYY-MM-DD)")
        text = value[:10] if lenient else value
        try:
            return dt.date.fromisoformat(text).isoformat()
        except ValueError:
            raise ValueError("must be a date (YYYY-MM-DD)") from None
    if kind == Type.ENUM:
        choices = (slot.options or {}).get("choices") or []
        if value not in choices:
            raise ValueError(f"must be one of: {', '.join(map(str, choices))}")
        return value
    if kind == Type.URL:
        if not isinstance(value, str):
            raise ValueError("must be a URL")
        try:
            URLValidator(schemes=["http", "https"])(value)
        except ValidationError:
            raise ValueError("must be an http(s) URL") from None
        return value
    raise ValueError(f"unknown slot type {kind}")  # pragma: no cover - the DB check forbids other types
