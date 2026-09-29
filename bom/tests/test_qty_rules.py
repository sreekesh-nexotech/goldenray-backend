"""``qty_rule`` expresses every legacy quantity behaviour: the importer's rule for each UAT slot / fixed item /
structure item gives exactly what the legacy code computed, over every size, tier and battery band (and sizes and
bands the templates do not offer)."""

import math

import pytest

from bom import schemas
from bom.services import qty_rules
from bom.services.legacy_import import fixed_rule, slot_rule
from bom.services.qty_rules import QtyContext, evaluate
from bom.tests.legacy_fixtures import goldenray_rows

SIZES = ["3", "5", "5sp", "5tp", "6", "8", "10", "7", "4", "12", ""]
TIERS = ["base", "value", "premium", ""]
BANDS = ["0", "1", "2", "", "9", "None"]


def legacy_slot_get_qty(row, size_key, bat_config=None, tier=None):
    """``BomSlot.get_qty`` verbatim (main backend ``bom/models/bom_slot.py``)."""
    if tier == "premium":
        if bat_config and row["premium_bat_qty"]:
            bat_map = row["premium_bat_qty"].get(bat_config, row["premium_bat_qty"].get("0", {}))
            return bat_map.get(size_key, 0)
        if row["premium_qty"] is not None:
            return row["premium_qty"].get(size_key, 0)
    if bat_config and row["bat_qty"]:
        bat_map = row["bat_qty"].get(bat_config, row["bat_qty"].get("0", {}))
        return bat_map.get(size_key, 0)
    return row["qty"].get(size_key, 0)


def legacy_fixed_qty(fi, size, tier, bat_config):
    """``BomCalculator._get_fixed_lines`` quantity resolution verbatim."""
    if tier == "premium" and fi["premium_qty"]:
        return fi["premium_qty"].get(size, 0)
    if fi["bat_qty"]:
        bat_map = fi["bat_qty"].get(bat_config, fi["bat_qty"].get("0", {}))
        return bat_map.get(size, 0) if isinstance(bat_map, dict) else 0
    return fi["qty"].get(size, 0) if fi["qty"] else 0


KNOWN_PATHS = {"3_5": 4, "5_8": 5, "5_10": 9, "8_10": 4}


def legacy_upgrade_fixed_qty(fi, from_kw, to_kw):
    """``BomCalculator._compute_upgrade`` fixed-item quantity verbatim."""
    new_panels = max(1, round((to_kw - from_kw) / 0.55))
    path_key = f"{round(from_kw)}_{round(to_kw)}"
    qty = 0
    if fi["qty"]:
        if path_key in fi["qty"]:
            qty = fi["qty"][path_key]
        else:

            def panels_for_key(k):
                if k in KNOWN_PATHS:
                    return KNOWN_PATHS[k]
                parts = k.split("_")
                try:
                    return max(1, round((float(parts[1]) - float(parts[0])) / 0.55))
                except (IndexError, ValueError):
                    return 4

            available_keys = list(fi["qty"].keys())
            if available_keys:
                closest = min(available_keys, key=lambda k: abs(panels_for_key(k) - new_panels))
                ref_qty = fi["qty"].get(closest, 0)
                ref_panels = panels_for_key(closest)
                all_same = len(set(fi["qty"].values())) == 1
                qty = ref_qty if all_same else max(1, round(ref_qty * new_panels / ref_panels))
    return qty


def legacy_upgrade_slot_qty(slot, from_kw, to_kw):
    new_panels = max(1, round((to_kw - from_kw) / 0.55))
    mode = slot["qty_mode"] or ""
    if mode == "newPanels":
        return new_panels
    if mode == "fixed":
        return slot["fixed_qty"] or 0
    size_key = str(int(to_kw)) if to_kw == int(to_kw) else str(to_kw)
    return slot["qty"].get(size_key, 0) if slot["qty"] else 0


def legacy_structure_qty(qty_map, kw):
    """``BomCalculator._get_structure_qty`` verbatim."""
    if not qty_map:
        return 0
    sizes = sorted(float(k) for k in qty_map.keys())
    if kw <= sizes[0]:
        return qty_map[str(int(sizes[0]) if sizes[0] == int(sizes[0]) else sizes[0])] or qty_map[list(qty_map.keys())[0]]
    if kw >= sizes[-1]:
        last_key = str(int(sizes[-1]) if sizes[-1] == int(sizes[-1]) else sizes[-1])
        last_qty = qty_map.get(last_key) or qty_map.get(str(sizes[-1]))
        if last_qty is None:
            last_qty = list(qty_map.values())[-1]
        return math.ceil(last_qty * kw / sizes[-1])
    for i in range(len(sizes) - 1):
        lo, hi = sizes[i], sizes[i + 1]
        if lo <= kw <= hi:
            lo_key = str(int(lo) if lo == int(lo) else lo)
            hi_key = str(int(hi) if hi == int(hi) else hi)
            lo_qty = qty_map.get(lo_key) or qty_map.get(str(lo), 0)
            hi_qty = qty_map.get(hi_key) or qty_map.get(str(hi), 0)
            ratio = (kw - lo) / (hi - lo)
            return math.ceil(lo_qty + ratio * (hi_qty - lo_qty))
    return list(qty_map.values())[-1]


PATHS = [(3, 5), (5, 8), (5, 10), (8, 10), (3, 10), (3, 8), (2.5, 4.5), (5, 6), (10, 12)]
UPGRADE_TEMPLATE_ID = 3


def _same(a, b) -> bool:
    return a == b and type(a) is type(b)


def test_every_uat_slot_rule_equals_get_qty():
    templates, slots, *_ = goldenray_rows()
    checked = 0
    for row in slots:
        upgrade = row["template_id"] == UPGRADE_TEMPLATE_ID
        rule, _ = slot_rule(row, upgrade=upgrade)
        assert not schemas.problems(rule, "QTY_RULE_SCHEMA"), rule
        if upgrade:
            for from_kw, to_kw in PATHS:
                size_key = str(int(to_kw)) if to_kw == int(to_kw) else str(to_kw)
                ctx = QtyContext(size_key=size_key, upgrade=True, upgrade_from_kw=float(from_kw), upgrade_to_kw=float(to_kw))
                assert _same(evaluate(rule, ctx), legacy_upgrade_slot_qty(row, float(from_kw), float(to_kw))), (row["id"], from_kw, to_kw)
                checked += 1
            continue
        for size in SIZES:
            for tier in TIERS:
                for band in BANDS:
                    expected = legacy_slot_get_qty(row, size, bat_config=band, tier=tier)
                    assert _same(evaluate(rule, QtyContext(size_key=size, tier=tier, bat_config=band)), expected), (row["id"], size, tier, band)
                    checked += 1
    assert checked > 5000


def test_every_uat_fixed_item_rule_equals_the_calculator():
    _, _, fixed, *_ = goldenray_rows()
    checked = 0
    for row in fixed:
        upgrade = row["template_id"] == UPGRADE_TEMPLATE_ID
        rule, _ = fixed_rule(row, upgrade=upgrade)
        assert not schemas.problems(rule, "QTY_RULE_SCHEMA"), rule
        if upgrade:
            for from_kw, to_kw in PATHS:
                ctx = QtyContext(upgrade=True, upgrade_from_kw=float(from_kw), upgrade_to_kw=float(to_kw))
                assert _same(evaluate(rule, ctx), legacy_upgrade_fixed_qty(row, float(from_kw), float(to_kw))), (row["id"], from_kw, to_kw)
                checked += 1
            continue
        for size in SIZES:
            for tier in TIERS:
                for band in BANDS:
                    assert _same(evaluate(rule, QtyContext(size_key=size, tier=tier, bat_config=band)), legacy_fixed_qty(row, size, tier, band)), (row["id"], size, tier, band)
                    checked += 1
    assert checked > 20000


def test_every_uat_structure_item_rule_equals_get_structure_qty():
    *_, items, _ = goldenray_rows()
    for row in items:
        rule = {"type": "kw_interpolated", "points": row["qty"]}
        for kw in (0.5, 1, 2, 3, 3.5, 4, 5, 5.5, 6, 7, 8, 9, 10, 12, 15, 20):
            assert _same(evaluate(rule, QtyContext(kw=float(kw))), legacy_structure_qty(row["qty"], float(kw))), (row["id"], kw)


@pytest.mark.parametrize(
    "rule,ctx,expected",
    [
        ({"type": "fixed", "qty": 3}, QtyContext(), 3),
        ({"type": "per_kw", "qty_per_kw": 2.5, "rounding": "ceil"}, QtyContext(kw=3.0), 8),
        ({"type": "per_kw", "qty_per_kw": 2.5, "rounding": "none"}, QtyContext(kw=3.0), 7.5),
        ({"type": "per_kw", "qty_per_kw": 1, "rounding": "round", "min": 4}, QtyContext(kw=3.0), 4),
        ({"type": "per_panel", "qty_per_panel": 2}, QtyContext(panels=9), 18),
        ({"type": "by_phase", "1P": 1, "3P": 3}, QtyContext(is_three_phase=True), 3),
        ({"type": "by_phase", "1P": 1, "3P": 3}, QtyContext(is_three_phase=False), 1),
        ({"type": "new_panels", "panel_kw": 0.5}, QtyContext(upgrade=True, upgrade_from_kw=3.0, upgrade_to_kw=5.0), 4),
        ({"type": "upgrade_path", "qty": {"3_5": 2}}, QtyContext(), 0),
        ({"type": "upgrade_path", "qty": {}}, QtyContext(upgrade_from_kw=3.0, upgrade_to_kw=5.0), 0),
        ({"type": "kw_interpolated", "points": {}}, QtyContext(kw=3.0), 0),
        ({"type": "size_table", "qty": {"3": 2}}, QtyContext(size_key="3", upgrade=True), 2),
        (None, QtyContext(), 0),
    ],
)
def test_platform_rule_types(rule, ctx, expected):
    assert evaluate(rule, ctx) == expected
    if rule is not None:
        assert schemas.problems(rule, "QTY_RULE_SCHEMA") == []


def test_unknown_rule_type_raises():
    with pytest.raises(ValueError):
        evaluate({"type": "nope"}, QtyContext())


@pytest.mark.parametrize(
    "rule",
    [{"type": "nope"}, {"type": "fixed"}, {"type": "fixed", "qty": -1}, {"type": "size_table", "qty": {"3": "a"}}, {"type": "by_phase", "1P": 1}, {"type": "size_table", "extra": 1}, [], "x"],
)
def test_invalid_rules_are_reported(rule):
    assert schemas.problems(rule, "QTY_RULE_SCHEMA")


def test_conditions():
    ctx = QtyContext(size_key="5sp", tier="value", bat_config="1", kw=5.0, is_three_phase=False)
    assert qty_rules.matches({}, ctx)
    assert qty_rules.matches({"sizes": ["5sp"], "phases": ["1P"], "tiers": ["value"], "battery_configs": ["1"], "min_kw": 3, "max_kw": 5}, ctx)
    for condition in ({"sizes": ["3"]}, {"phases": ["3P"]}, {"tiers": ["base"]}, {"battery_configs": ["0"]}, {"min_kw": 6}, {"max_kw": 4}):
        assert not qty_rules.matches(condition, ctx), condition
    assert schemas.problems({"phases": ["2P"]}, "CONDITION_SCHEMA")
