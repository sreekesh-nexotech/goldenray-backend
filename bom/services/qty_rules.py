"""Evaluation of ``qty_rule`` documents (``bom.schemas.QTY_RULE_SCHEMA``) — pure functions, no ORM.

The ``size_table`` rule is the legacy ``BomSlot.get_qty`` (and the ``BomFixedItem`` quantity resolution with
``bat_lookup: always``) operation for operation; ``upgrade_path`` / ``new_panels`` / ``fixed`` are the upgrade
calculator's ``qtyMode``s and fixed-item auto-scaling; ``kw_interpolated`` is ``getStructureQty``. Numbers come back as
the JSON numbers the rule holds (``int`` or ``float``), exactly as the legacy code returned them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_PANEL_KW = 0.55
# The legacy calculator's panel counts of the four known upgrade paths (KNOWN_PATHS in bom/calculator.py).
KNOWN_PATHS = {"3_5": 4, "5_8": 5, "5_10": 9, "8_10": 4}


@dataclass(frozen=True)
class QtyContext:
    size_key: str = ""
    tier: str = ""
    bat_config: str = "0"
    kw: float = 0.0
    is_three_phase: bool = False
    panels: float | None = None
    upgrade: bool = False
    upgrade_from_kw: float | None = None
    upgrade_to_kw: float | None = None


def new_panels(from_kw: float, to_kw: float, panel_kw: float = DEFAULT_PANEL_KW) -> int:
    """``max(1, round((to − from) / panel_kw))`` — the upgrade's new panel count."""
    return max(1, round((to_kw - from_kw) / panel_kw))


def path_key(from_kw: float, to_kw: float) -> str:
    return f"{round(from_kw)}_{round(to_kw)}"


def _band(table: dict, bat_config: str):
    chosen = table.get(bat_config, table.get("0", {}))
    return chosen


def size_table(rule: dict, size_key: str, *, tier: str, bat_config: str) -> float:
    """Legacy ``BomSlot.get_qty(size_key, bat_config=…, tier=…)`` (``bat_lookup`` ``if_battery_config``) and the
    ``BomFixedItem`` resolution (``bat_lookup`` ``always``, no empty ``premium_qty``)."""
    always = rule.get("bat_lookup") == "always"
    if tier == "premium":
        premium_bat = rule.get("premium_bat_qty")
        if premium_bat and (bat_config or always):
            chosen = _band(premium_bat, bat_config)
            return chosen.get(size_key, 0) if isinstance(chosen, dict) else 0
        if "premium_qty" in rule:
            return (rule["premium_qty"] or {}).get(size_key, 0)
    bat = rule.get("bat_qty")
    if bat and (bat_config or always):
        chosen = _band(bat, bat_config)
        return chosen.get(size_key, 0) if isinstance(chosen, dict) else 0
    return (rule.get("qty") or {}).get(size_key, 0)


def upgrade_path(rule: dict, from_kw: float, to_kw: float) -> float:
    """The legacy upgrade fixed-item quantity: the exact path, else scaled from the closest known path."""
    table = rule.get("qty") or {}
    key = path_key(from_kw, to_kw)
    if not table:
        return 0
    if key in table:
        return table[key]
    wanted = new_panels(from_kw, to_kw)

    def panels_for_key(k: str) -> int:
        if k in KNOWN_PATHS:
            return KNOWN_PATHS[k]
        parts = k.split("_")
        try:
            return max(1, round((float(parts[1]) - float(parts[0])) / DEFAULT_PANEL_KW))
        except (IndexError, ValueError):
            return 4

    closest = min(table.keys(), key=lambda k: abs(panels_for_key(k) - wanted))
    reference = table.get(closest, 0)
    all_same = len(set(table.values())) == 1
    return reference if all_same else max(1, round(reference * wanted / panels_for_key(closest)))


def _key_of(number: float) -> str:
    return str(int(number) if number == int(number) else number)


def kw_interpolated(rule: dict, kw: float) -> float:
    """``getStructureQty`` (legacy ``BomCalculator._get_structure_qty``) over ``points``."""
    qty_map = rule.get("points") or {}
    if not qty_map:
        return 0
    sizes = sorted(float(k) for k in qty_map.keys())
    if kw <= sizes[0]:
        # .get: a key written as "3.0" made the legacy lookup raise; here it falls back to the first point.
        return qty_map.get(_key_of(sizes[0])) or qty_map[list(qty_map.keys())[0]]
    if kw >= sizes[-1]:
        last_qty = qty_map.get(_key_of(sizes[-1])) or qty_map.get(str(sizes[-1]))
        if last_qty is None:
            last_qty = list(qty_map.values())[-1]
        return math.ceil(last_qty * kw / sizes[-1])
    for index in range(len(sizes) - 1):
        low, high = sizes[index], sizes[index + 1]
        if low <= kw <= high:
            low_qty = qty_map.get(_key_of(low)) or qty_map.get(str(low), 0)
            high_qty = qty_map.get(_key_of(high)) or qty_map.get(str(high), 0)
            ratio = (kw - low) / (high - low)
            return math.ceil(low_qty + ratio * (high_qty - low_qty))
    return list(qty_map.values())[-1]


def _rounded(value: float, rule: dict) -> float:
    mode = rule.get("rounding", "ceil")
    if mode == "ceil":
        value = math.ceil(value)
    elif mode == "round":
        value = round(value)
    minimum = rule.get("min")
    return max(minimum, value) if minimum is not None else value


def evaluate(rule: dict | None, ctx: QtyContext) -> float:
    """The quantity ``rule`` gives in ``ctx`` (0 when there is none)."""
    if not rule:
        return 0
    kind = rule.get("type")
    if kind == "size_table":
        if ctx.upgrade:
            # The upgrade calculator reads only the plain table at the target size.
            return (rule.get("qty") or {}).get(ctx.size_key, 0)
        return size_table(rule, ctx.size_key, tier=ctx.tier, bat_config=ctx.bat_config)
    if kind == "fixed":
        return rule.get("qty") or 0
    if kind == "new_panels":
        if ctx.upgrade_from_kw is None or ctx.upgrade_to_kw is None:
            return ctx.panels or 0
        return new_panels(ctx.upgrade_from_kw, ctx.upgrade_to_kw, rule.get("panel_kw", DEFAULT_PANEL_KW))
    if kind == "upgrade_path":
        if ctx.upgrade_from_kw is None or ctx.upgrade_to_kw is None:
            return 0
        return upgrade_path(rule, ctx.upgrade_from_kw, ctx.upgrade_to_kw)
    if kind == "kw_interpolated":
        return kw_interpolated(rule, ctx.kw)
    if kind == "per_kw":
        return _rounded(rule["qty_per_kw"] * ctx.kw, rule)
    if kind == "per_panel":
        return _rounded(rule["qty_per_panel"] * (ctx.panels or 0), rule)
    if kind == "by_phase":
        return rule["3P" if ctx.is_three_phase else "1P"]
    raise ValueError(f"Unknown qty_rule type {kind!r}.")


def matches(condition: dict | None, ctx: QtyContext) -> bool:
    """Whether a fixed item's ``condition`` (``bom.schemas.CONDITION_SCHEMA``) holds in ``ctx``."""
    if not condition:
        return True
    if "sizes" in condition and ctx.size_key not in condition["sizes"]:
        return False
    if "phases" in condition and ("3P" if ctx.is_three_phase else "1P") not in condition["phases"]:
        return False
    if "tiers" in condition and ctx.tier not in condition["tiers"]:
        return False
    if "battery_configs" in condition and ctx.bat_config not in condition["battery_configs"]:
        return False
    if "min_kw" in condition and ctx.kw < condition["min_kw"]:
        return False
    if "max_kw" in condition and ctx.kw > condition["max_kw"]:
        return False
    return True


def size_table_for_flarize(rule: dict) -> dict:
    """The Flarize slot/fixed-item quantity keys of a ``size_table`` rule (``qty``, ``batQty``, ``premiumQty``,
    ``premiumBatQty``) for ``engines.bom_builder``."""
    names = {"qty": "qty", "bat_qty": "batQty", "premium_qty": "premiumQty", "premium_bat_qty": "premiumBatQty"}
    return {flarize: rule[name] for name, flarize in names.items() if name in rule}
