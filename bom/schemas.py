"""JSON Schemas (2020-12) of the bom documents: ``qty_rule`` (slots, fixed items, structure items), fixed-item
``condition`` and the template's ``sizes``/``three_phase_sizes``/``tiers``/``battery_configs``.

Quantity rules (evaluated by :mod:`bom.services.qty_rules`; every legacy ``BomSlot.get_qty`` behaviour, the fixed-item
quantity resolution, the upgrade ``qtyMode``s and ``getStructureQty`` are expressible — proven in
``bom/tests/test_qty_rules.py``):

* ``size_table`` — ``qty`` / ``bat_qty`` / ``premium_qty`` / ``premium_bat_qty`` tables keyed by size key (and battery
  band). ``bat_lookup`` ``if_battery_config`` (slots: the band tables apply only with a battery band) or ``always``
  (fixed items). An empty ``premium_qty`` table still means "0 for premium".
* ``fixed`` — a constant ``qty`` (``hybrid_qty`` for hybrid upgrades, kept from Flarize).
* ``new_panels`` — the upgrade's new panel count ``max(1, round((to − from) / panel_kw))``.
* ``upgrade_path`` — ``qty`` keyed by upgrade path (``3_5``), scaled from the closest known path otherwise.
* ``kw_interpolated`` — ``points`` keyed by kW, linearly interpolated (ceil) between them and scaled beyond the last.
* ``per_kw`` / ``per_panel`` — ``qty_per_kw`` × kW / ``qty_per_panel`` × panels, rounded (``ceil``/``round``/``none``),
  optional ``min``.
* ``by_phase`` — ``1P`` / ``3P`` quantities.
"""

from __future__ import annotations

from functools import cache

from jsonschema import Draft202012Validator

NUMBER = {"type": "number", "minimum": 0}
SIZE_TABLE = {"type": "object", "propertyNames": {"minLength": 1, "maxLength": 16}, "additionalProperties": NUMBER}
BAND_TABLE = {"type": "object", "propertyNames": {"minLength": 1, "maxLength": 4}, "additionalProperties": SIZE_TABLE}
ROUNDING = {"enum": ["ceil", "round", "none"]}


def _rule(name: str, properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {
        "type": "object",
        "properties": {"type": {"const": name}, **properties},
        "required": ["type", *required],
        "additionalProperties": False,
    }


QTY_RULE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "bom qty_rule",
    "type": "object",
    "required": ["type"],
    "properties": {"type": {"enum": ["size_table", "fixed", "new_panels", "upgrade_path", "kw_interpolated", "per_kw", "per_panel", "by_phase"]}},
    "oneOf": [
        _rule(
            "size_table",
            {"qty": SIZE_TABLE, "bat_qty": BAND_TABLE, "premium_qty": SIZE_TABLE, "premium_bat_qty": BAND_TABLE, "bat_lookup": {"enum": ["if_battery_config", "always"]}},
        ),
        _rule("fixed", {"qty": NUMBER, "hybrid_qty": NUMBER}, ("qty",)),
        _rule("new_panels", {"panel_kw": {"type": "number", "exclusiveMinimum": 0}}),
        _rule("upgrade_path", {"qty": {"type": "object", "propertyNames": {"pattern": r"^\d+(\.\d+)?_\d+(\.\d+)?$"}, "additionalProperties": NUMBER}}, ("qty",)),
        _rule("kw_interpolated", {"points": {"type": "object", "propertyNames": {"pattern": r"^\d+(\.\d+)?$"}, "additionalProperties": NUMBER}}, ("points",)),
        _rule("per_kw", {"qty_per_kw": NUMBER, "rounding": ROUNDING, "min": NUMBER}, ("qty_per_kw",)),
        _rule("per_panel", {"qty_per_panel": NUMBER, "rounding": ROUNDING, "min": NUMBER}, ("qty_per_panel",)),
        _rule("by_phase", {"1P": NUMBER, "3P": NUMBER}, ("1P", "3P")),
    ],
}

CONDITION_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "bom fixed item condition (all given filters must match; {} = always)",
    "type": "object",
    "properties": {
        "sizes": {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True},
        "phases": {"type": "array", "items": {"enum": ["1P", "3P"]}, "uniqueItems": True},
        "tiers": {"type": "array", "items": {"enum": ["base", "value", "premium"]}, "uniqueItems": True},
        "battery_configs": {"type": "array", "items": {"type": "string", "maxLength": 4}, "uniqueItems": True},
        "min_kw": NUMBER,
        "max_kw": NUMBER,
    },
    "additionalProperties": False,
}

SIZES_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"key": {"type": "string", "minLength": 1, "maxLength": 16}, "label": {"type": "string", "maxLength": 100}},
        "required": ["key", "label"],
        "additionalProperties": False,
    },
}
KEY_LIST_SCHEMA = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 16}, "uniqueItems": True}
TIERS_SCHEMA = {"type": "array", "items": {"enum": ["base", "value", "premium"]}, "uniqueItems": True}
BANDS_SCHEMA = {"type": "array", "items": {"enum": ["0", "1", "2"]}, "uniqueItems": True}


@cache
def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(globals()[name])


def problems(document, schema_name: str) -> list[str]:
    """Human-readable violations of ``document`` against the named schema (empty when valid)."""
    validator = _validator(schema_name)
    messages = []
    for error in sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path)):
        if error.validator == "oneOf" and error.context:
            # Report the branch of the rule's own type, not every alternative.
            kind = document.get("type") if isinstance(document, dict) else None
            branch = [sub for sub in error.context if sub.schema_path and sub.schema_path[0] == _branch_index(kind)]
            for sub in branch or error.context[:1]:
                messages.append(_message(sub))
            continue
        messages.append(_message(error))
    return messages


def _branch_index(kind) -> int | None:
    for index, branch in enumerate(QTY_RULE_SCHEMA["oneOf"]):
        if branch["properties"]["type"]["const"] == kind:
            return index
    return None


def _message(error) -> str:
    path = "/".join(str(part) for part in error.absolute_path)
    return f"{path}: {error.message}" if path else error.message
