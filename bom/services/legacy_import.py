"""Legacy importers of the bom configuration (PLAN §7.3 ``bom_bomtemplate``, ``bom_bomslot``, ``bom_bomfixeditem``,
``bom_structuretemplate(+item)``, ``bom_tubeweight``; §7.4 Flarize ``catalog.json`` ``bomTemplates``,
``structureTemplates``, ``tubeWeights``, ``packageProfiles``).

Contract (PLAN §7.1): plain row dicts / parsed JSON in; ``{"created", "updated", "unchanged", "skipped",
"violations", "counts"}`` out; idempotent through ``core_legacy_map`` (a re-run updates or leaves rows, never
duplicates); ``dry_run=True`` rolls everything back; one ``bom.legacy_import`` audit row per call. Violations carry
``source_table``, ``source_id``, ``code``, ``severity`` (``error`` = not imported, ``warning`` = imported, look at it).

Order: the catalog imports first (slots and fixed items resolve ``bom_category`` / ``bom_catalogitem`` ids and Flarize
category slugs / item ids through the catalog's legacy map), then these.

**D-2 (Flarize wins).** A template / structure template / tube weight that the Flarize import wrote is never changed
by the main-backend import: the main-backend rows are mapped onto it and every difference is reported
(``d2_flarize_wins``). When Flarize runs second it replaces the content (its slots, fixed items and structure items
win; children only the main backend had are soft-deleted and reported ``d2_removed``). Either order ends with the
same live rows.

``BomSlot.get_qty`` → ``qty_rule`` (the table in docs/decisions/bom.md; equivalence over every size, tier and battery
band proven in ``bom/tests/test_qty_rules.py``):

* upgrade template, ``qty_mode`` ``newPanels`` → ``{"type": "new_panels"}``; ``fixed`` → ``{"type": "fixed", "qty":
  fixed_qty or 0}``; otherwise → ``size_table`` with ``qty`` (read at the target size);
* other templates → ``size_table`` with ``qty`` (always), ``premium_qty`` (whenever not null — an empty table still
  means 0 for premium), ``bat_qty`` / ``premium_bat_qty`` (when not empty); a ``qty_mode`` there is reported
  ``qty_mode_ignored`` (the standard calculator never read it);
* fixed items: standard templates → ``size_table`` with ``bat_lookup: always`` (the fixed-item resolution),
  ``premium_qty`` / ``bat_qty`` only when not empty; the upgrade template → ``upgrade_path`` over the path keys.
"""

from __future__ import annotations

from decimal import Decimal

from bom.models import FixedItem, PackageProfile, Slot, StructureItemType, StructureTemplate, StructureTemplateItem, Template, TubeWeight
from bom.services import masters
from bom.services.common import fraction_of
from catalog.models import Category, Component
from core.models import LegacyMap
from pricing.services.import_support import BACKEND, FLARIZE, BadValue, DryRunRollback, ImportRun, checksum, dec, guarded, json_ready, mapped, remember

SYSTEM_TYPES = {"ongrid": "ONGRID", "hybrid": "HYBRID", "upgrade": "UPGRADE"}
TEMPLATE_NAMES = {"ongrid": "On-Grid", "hybrid": "Hybrid", "upgrade": "On-Grid Upgrade"}
FILTER_TYPES = {"": "", "ongrid": "ONGRID", "hybrid": "HYBRID"}
FILTER_PHASES = {"": "", "HYB": "HYB"}
STRUCTURE_SLUGS = {"flatRoof": "flat_roof", "sheetRoof": "sheet_roof", "elevated": "elevated"}
LABOUR_RATE_KEYS = {"elevated": "elevated_structure_rate", "sheet_roof": "sheet_structure_rate"}
PROFILE_MATERIALS = {"GP": "GP", "GI": "GI", "AL": "AL"}
PROFILE_INVERTERS = {"ongrid": "ONGRID", "hybrid": "HYBRID", "micro": "MICRO"}

GR_TEMPLATE, GR_SLOT, GR_FIXED = "bom_bomtemplate", "bom_bomslot", "bom_bomfixeditem"
GR_STRUCTURE, GR_STRUCTURE_ITEM, GR_TUBE = "bom_structuretemplate", "bom_structuretemplateitem", "bom_tubeweight"
FL_TEMPLATE, FL_SLOT, FL_FIXED = "catalog.json:bomTemplates", "catalog.json:bomTemplates.slots", "catalog.json:bomTemplates.fixedItems"
FL_STRUCTURE, FL_STRUCTURE_ITEM, FL_TUBE = "catalog.json:structureTemplates", "catalog.json:structureTemplates.items", "catalog.json:tubeWeights"
FL_PROFILE = "catalog.json:packageProfiles"

TEMPLATE_FIELDS = masters.TEMPLATE.fields
SLOT_FIELDS = ("category", "qty_rule", "required", "sort_order", "label", "gst_rate", "is_variable", "filter_type", "filter_phase")
FIXED_FIELDS = ("component", "category", "code", "name", "unit_price", "gst_rate", "qty", "qty_rule", "condition", "section", "unit", "is_tube", "sort_order")
STRUCTURE_ITEM_FIELDS = ("name", "item_type", "tube_size", "weight_kg", "unit_price", "unit", "length_m_per_kw", "qty_rule", "sort_order")


class Run(ImportRun):
    def __init__(self, name: str):
        super().__init__(name)
        self.unchanged = 0

    def count(self, table: str, outcome: str) -> None:
        bucket = self.counts.setdefault(table, {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0})
        bucket[outcome] += 1
        setattr(self, outcome, getattr(self, outcome) + 1)

    def as_dict(self) -> dict:
        return {**super().as_dict(), "unchanged": self.unchanged}


def _run(name: str, payloads, body, *, user, dry_run: bool) -> dict:
    from django.db import transaction

    from audit.services import record
    from flarize.cache_utils import bump

    result = Run(name)
    try:
        with transaction.atomic():
            body(result)
            summary = {key: value for key, value in result.as_dict().items() if key != "violations"}
            record(
                "bom.legacy_import", object_type="bom.template", actor=user, after={"import": name, "checksum": checksum(payloads), "dry_run": dry_run, **summary, "violations": len(result.violations)}
            )
            if dry_run:
                raise DryRunRollback()
    except DryRunRollback:
        pass
    else:
        bump("bom")
    return json_ready(result.as_dict())


# ── entry points ─────────────────────────────────────────────────────────────────────────────────────────────


def import_goldenray_bom(templates, slots, fixed_items, structure_templates, structure_items, tube_weights, *, user=None, dry_run: bool = False) -> dict:
    """Main backend ``bom_*`` configuration rows → ``bom_*`` tables."""
    rows = [list(x or []) for x in (templates, slots, fixed_items, structure_templates, structure_items, tube_weights)]
    return _run("import_goldenray_bom", rows, lambda result: _goldenray(*rows, user=user, result=result), user=user, dry_run=dry_run)


def import_flarize_bom(catalog_json: dict, *, user=None, dry_run: bool = False) -> dict:
    """Flarize ``catalog.json`` ``bomTemplates``, ``structureTemplates``, ``tubeWeights``, ``packageProfiles``."""
    catalog_json = catalog_json or {}
    sections = {key: catalog_json.get(key) or {} for key in ("bomTemplates", "structureTemplates", "tubeWeights", "packageProfiles")}
    return _run("import_flarize_bom", sections, lambda result: _flarize(sections, user=user, result=result), user=user, dry_run=dry_run)


# ── shared machinery ─────────────────────────────────────────────────────────────────────────────────────────


def _owned_by_flarize(target) -> bool:
    return LegacyMap.objects.filter(source_system=FLARIZE, target_table=target._meta.db_table, target_id=target.pk).exists()


def _differences(instance, values: dict) -> dict:
    return {name: value for name, value in values.items() if _differs(getattr(instance, name), value)}


def _differs(current, incoming) -> bool:
    if isinstance(current, Decimal) or isinstance(incoming, Decimal):
        if current is None or incoming is None:
            return current is not incoming
        return Decimal(str(current)) != Decimal(str(incoming))
    return current != incoming


def _shown(values: dict) -> dict:
    return json_ready({name: (str(value) if hasattr(value, "_meta") else value) for name, value in values.items()})


def _upsert(spec, instance, values: dict, *, user, create_extra: dict | None = None) -> tuple[object, str]:
    """Create through the master service, or update only the changed fields (``unchanged`` when none)."""
    if instance is None:
        return masters.create(spec, user=user, data={**values, **(create_extra or {})}), "created"
    if instance.deleted_at is not None:
        return instance, "skipped"
    diff = _differences(instance, values)
    if not diff:
        return instance, "unchanged"
    return masters.update(spec, instance, user=user, data=diff), "updated"


def _takeover(result: Run, table: str, source_id, instance, values: dict) -> None:
    """D-2 when Flarize runs second: report what it changes on a row the main-backend import wrote."""
    if instance is None or instance.deleted_at is not None:
        return
    if not LegacyMap.objects.filter(source_system=BACKEND, target_table=instance._meta.db_table, target_id=instance.pk).exists():
        return
    diff = _differences(instance, {name: value for name, value in values.items() if name != "sort_order"})
    if diff:
        before = {name: getattr(instance, name) for name in diff}
        result.violation(table, source_id, "d2_flarize_wins", f"{instance}: catalog.json replaces the main-backend values.", severity="warning", bom=_shown(before), flarize=_shown(diff))


def _category_by_legacy_id(category_id) -> Category | None:
    if category_id is None:
        return None
    return mapped(BACKEND, "bom_category", category_id, Category)


def _component_by_legacy_id(item_id) -> Component | None:
    if item_id is None:
        return None
    return mapped(BACKEND, "bom_catalogitem", item_id, Component)


def _component_by_sku(sku) -> Component | None:
    if not sku:
        return None
    return mapped(FLARIZE, "catalog.json:items", sku, Component) or Component.objects.filter(sku__iexact=sku).first()


def _json_table(value, column: str):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise BadValue(f"{column}={value!r}: not an object")
    return value


# ── conversions (legacy fields → platform values) ────────────────────────────────────────────────────────────


def slot_rule(row: dict, *, upgrade: bool) -> tuple[dict, list[str]]:
    """``BomSlot`` (or a Flarize slot in the same vocabulary) quantity fields → ``qty_rule``; returns the notes."""
    notes = []
    mode = row.get("qty_mode") or ""
    if upgrade:
        if mode == "newPanels":
            return {"type": "new_panels"}, notes
        if mode == "fixed":
            rule = {"type": "fixed", "qty": row.get("fixed_qty") or 0}
            if row.get("hybrid_qty") is not None:
                rule["hybrid_qty"] = row["hybrid_qty"]
            return rule, notes
        return {"type": "size_table", "qty": _json_table(row.get("qty"), "qty") or {}}, notes
    if mode:
        notes.append("qty_mode_ignored")
    rule: dict = {"type": "size_table", "qty": _json_table(row.get("qty"), "qty") or {}}
    if row.get("premium_qty") is not None:
        rule["premium_qty"] = _json_table(row["premium_qty"], "premium_qty")
    if row.get("bat_qty"):
        rule["bat_qty"] = _json_table(row["bat_qty"], "bat_qty")
    if row.get("premium_bat_qty"):
        rule["premium_bat_qty"] = _json_table(row["premium_bat_qty"], "premium_bat_qty")
    return rule, notes


def fixed_rule(row: dict, *, upgrade: bool) -> tuple[dict, list[str]]:
    notes = []
    if upgrade:
        if row.get("premium_qty") or row.get("bat_qty"):
            notes.append("upgrade_band_tables_ignored")
        return {"type": "upgrade_path", "qty": _json_table(row.get("qty"), "qty") or {}}, notes
    rule: dict = {"type": "size_table", "bat_lookup": "always", "qty": _json_table(row.get("qty"), "qty") or {}}
    if row.get("premium_qty"):
        rule["premium_qty"] = _json_table(row["premium_qty"], "premium_qty")
    if row.get("bat_qty"):
        rule["bat_qty"] = _json_table(row["bat_qty"], "bat_qty")
    return rule, notes


def _gst(value, column: str):
    if value is None or value == "":
        return None
    number = dec(value, column, places=4, digits=7)
    if number < 0 or number > 100:
        raise BadValue(f"{column}={value!r}: not a percentage")
    return fraction_of(number)


def _template_values(system: str, *, name, description, sizes, three_phase, tiers, batteries) -> dict:
    if not isinstance(sizes, dict):
        raise BadValue(f"sizes={sizes!r}: not an object")
    return {
        "system_type": SYSTEM_TYPES[system],
        "name": (name or TEMPLATE_NAMES[system])[:100],
        "description": description or "",
        "is_active": True,
        "sizes": [{"key": str(key), "label": str(label)} for key, label in sizes.items()],
        "three_phase_sizes": [str(key) for key in (three_phase or [])],
        "tiers": [str(tier) for tier in (tiers or [])],
        "battery_configs": [str(band) for band in (batteries or [])],
    }


def _template_for(system_type: str) -> Template | None:
    return Template.objects.filter(system_type=system_type).first()


def _report_notes(result: Run, table: str, source_id, notes: list[str]) -> None:
    messages = {
        "qty_mode_ignored": "qty_mode is set on a non-upgrade slot; the calculator never read it there (size tables kept).",
        "upgrade_band_tables_ignored": "bat_qty/premium_qty on an upgrade fixed item; the upgrade calculator never read them.",
    }
    for note in notes:
        result.violation(table, source_id, note, messages[note], severity="warning")


# ── main backend ─────────────────────────────────────────────────────────────────────────────────────────────


def _goldenray(templates, slots, fixed_items, structure_templates, structure_items, tube_weights, *, user, result: Run) -> None:
    by_id: dict = {}
    frozen: set[int] = set()  # template pks owned by Flarize (D-2)
    for row in templates:
        guarded(result, GR_TEMPLATE, row.get("id"), lambda row=row: _gr_template(row, by_id, frozen, user=user, result=result))
    for row in sorted(slots, key=lambda r: (r.get("template_id") or 0, r.get("pos") or 0, r.get("id") or 0)):
        guarded(result, GR_SLOT, row.get("id"), lambda row=row: _gr_slot(row, by_id, frozen, user=user, result=result))
    positions: dict = {}
    for row in sorted(fixed_items, key=lambda r: r.get("id") or 0):
        template = by_id.get(row.get("template_id"))
        index = positions.setdefault(row.get("template_id"), 0)
        positions[row.get("template_id")] = index + 1
        guarded(result, GR_FIXED, row.get("id"), lambda row=row, template=template, index=index: _gr_fixed(row, template, index, frozen, user=user, result=result))
    structures: dict = {}
    frozen_structures: set[int] = set()
    for row in structure_templates:
        guarded(result, GR_STRUCTURE, row.get("id"), lambda row=row: _gr_structure(row, structures, frozen_structures, user=user, result=result))
    item_positions: dict = {}
    for row in sorted(structure_items, key=lambda r: r.get("id") or 0):
        index = item_positions.setdefault(row.get("template_id"), 0)
        item_positions[row.get("template_id")] = index + 1
        guarded(
            result,
            GR_STRUCTURE_ITEM,
            row.get("id"),
            lambda row=row, index=index: _gr_structure_item(row, structures.get(row.get("template_id")), index, frozen_structures, user=user, result=result),
        )
    for row in tube_weights:
        guarded(result, GR_TUBE, row.get("id"), lambda row=row: _tube_weight(BACKEND, GR_TUBE, row.get("id"), row.get("tube_size"), row.get("weight_kg"), user=user, result=result))


def _gr_template(row, by_id, frozen, *, user, result) -> str:
    system = row.get("system_type")
    if system not in SYSTEM_TYPES:
        raise BadValue(f"system_type={system!r}: unknown system type")
    values = _template_values(
        system,
        name=row.get("label"),
        description=row.get("description"),
        sizes=row.get("sizes") or {},
        three_phase=row.get("three_phase_sizes"),
        tiers=row.get("available_tiers"),
        batteries=row.get("battery_configs"),
    )
    template = mapped(BACKEND, GR_TEMPLATE, row["id"], Template) or _template_for(values["system_type"])
    if template is not None and template.deleted_at is not None:
        result.violation(GR_TEMPLATE, row["id"], "target_deleted", "The template was deleted in the platform; not re-created.", severity="warning")
        return "skipped"
    if template is not None and _owned_by_flarize(template):
        diff = _differences(template, values)
        if diff:
            result.violation(GR_TEMPLATE, row["id"], "d2_flarize_wins", f"{system}: catalog.json wins over bom_bomtemplate.", severity="warning", differences=_shown(diff))
        frozen.add(template.pk)
        outcome = "unchanged"
    else:
        template, outcome = _upsert(masters.TEMPLATE, template, values, user=user)
    remember(BACKEND, GR_TEMPLATE, row["id"], template)
    by_id[row["id"]] = template
    return outcome


def _gr_slot(row, by_id, frozen, *, user, result) -> str:
    template = by_id.get(row.get("template_id"))
    if template is None:
        result.violation(GR_SLOT, row.get("id"), "unknown_template", f"Slot names template id {row.get('template_id')!r}, which was not imported.")
        return "skipped"
    category = _category_by_legacy_id(row.get("category_id"))
    if category is None or category.deleted_at is not None:
        result.violation(GR_SLOT, row.get("id"), "unknown_category", f"Slot names bom_category id {row.get('category_id')!r}, which the catalog import did not map (or was deleted).")
        return "skipped"
    rule, notes = slot_rule(row, upgrade=template.system_type == "UPGRADE")
    values = _slot_values(row, category, rule, gst_column="gst", source=GR_SLOT, result=result)
    return _place_slot(BACKEND, GR_SLOT, row["id"], template, category.slug, values, frozen, notes, user=user, result=result)


def _slot_values(row, category, rule, *, gst_column: str, source: str, result) -> dict:
    filter_type = row.get("filter_type") or ""
    filter_phase = row.get("filter_phase") or ""
    if filter_type not in FILTER_TYPES:
        result.violation(
            source, row.get("id") or row.get("pos"), "unknown_filter_type", f"filter_type {filter_type!r} is not ongrid/hybrid; the calculator treated it as no filter.", severity="warning"
        )
    if filter_phase not in FILTER_PHASES:
        result.violation(source, row.get("id") or row.get("pos"), "unknown_filter_phase", f"filter_phase {filter_phase!r} is not HYB; the calculator treated it as no filter.", severity="warning")
    return {
        "category": category,
        "qty_rule": rule,
        "required": True,
        "sort_order": int(row.get("pos") or 0),
        "label": str(row.get("label") or "")[:100],
        "gst_rate": _gst(row.get(gst_column), gst_column),
        "is_variable": bool(row.get("variable")),
        "filter_type": FILTER_TYPES.get(filter_type, ""),
        "filter_phase": FILTER_PHASES.get(filter_phase, ""),
    }


def _place_slot(system, table, source_id, template, key, values, frozen, notes, *, user, result) -> str:
    if template.pk in frozen:
        slot = Slot.objects.filter(template=template, key=key).first()
        if slot is not None:
            diff = _differences(slot, values)
            if diff:
                result.violation(table, source_id, "d2_flarize_wins", f"{template.system_type}/{key}: catalog.json wins.", severity="warning", differences=_shown(diff))
            remember(system, table, source_id, slot)
        else:
            result.violation(table, source_id, "d2_flarize_wins", f"{template.system_type}/{key}: catalog.json has no such slot; not imported.", severity="warning")
        return "unchanged"
    _report_notes(result, table, source_id, notes)
    slot = mapped(system, table, source_id, Slot)
    if slot is None:
        slot = Slot.all_objects.filter(template=template, key=key, deleted_at__isnull=True).first()
    if slot is not None and slot.deleted_at is not None:
        result.violation(table, source_id, "target_deleted", "The slot was deleted in the platform; not re-created.", severity="warning")
        return "skipped"
    slot, outcome = _upsert(masters.SLOT, slot, values, user=user, create_extra={"template": template, "key": key})
    remember(system, table, source_id, slot)
    return outcome


def _gr_fixed(row, template, index, frozen, *, user, result) -> str:
    if template is None:
        result.violation(GR_FIXED, row.get("id"), "unknown_template", f"Fixed item names template id {row.get('template_id')!r}, which was not imported.")
        return "skipped"
    upgrade = template.system_type == "UPGRADE"
    component = _component_by_legacy_id(row.get("catalog_item_id"))
    if row.get("catalog_item_id") is not None and component is None:
        result.violation(GR_FIXED, row["id"], "unknown_component", f"catalog_item_id {row['catalog_item_id']!r} was not mapped by the catalog import; kept without a component.", severity="warning")
    category = _category_by_legacy_id(row.get("category_id"))
    rule, notes = fixed_rule(row, upgrade=upgrade)
    price = dec(row.get("price"), "price")
    values = {
        "component": component,
        "category": category,
        "code": "",
        "name": str(row.get("name") or "")[:255],
        # The upgrade calculator priced a linked item at the catalog price (the row's own price is 0 there).
        "unit_price": None if (upgrade and component is not None) else price,
        "gst_rate": _gst(row.get("gst"), "gst"),
        "qty": None,
        "qty_rule": rule,
        "condition": {},
        "section": str(row.get("section") or "")[:32],
        "unit": "",
        "is_tube": bool(row.get("is_tube")),
        "sort_order": index,
    }
    return _place_fixed(BACKEND, GR_FIXED, row["id"], template, values, frozen, notes, user=user, result=result)


def _place_fixed(system, table, source_id, template, values, frozen, notes, *, user, result) -> str:
    if template.pk in frozen:
        twin = FixedItem.objects.filter(template=template, name=values["name"]).order_by("sort_order", "id").first()
        if twin is not None:
            diff = _differences(twin, {k: v for k, v in values.items() if k != "sort_order"})
            if diff:
                result.violation(table, source_id, "d2_flarize_wins", f"{template.system_type}/{values['name']}: catalog.json wins.", severity="warning", differences=_shown(diff))
            remember(system, table, source_id, twin)
        else:
            result.violation(table, source_id, "d2_flarize_wins", f"{template.system_type}/{values['name']}: catalog.json has no such fixed item; not imported.", severity="warning")
        return "unchanged"
    _report_notes(result, table, source_id, notes)
    item = mapped(system, table, source_id, FixedItem)
    if item is not None and item.deleted_at is not None:
        result.violation(table, source_id, "target_deleted", "The fixed item was deleted in the platform; not re-created.", severity="warning")
        return "skipped"
    item, outcome = _upsert(masters.FIXED_ITEM, item, values, user=user, create_extra={"template": template})
    remember(system, table, source_id, item)
    return outcome


def _gr_structure(row, structures, frozen, *, user, result) -> str:
    source_slug = str(row.get("slug") or "")
    slug = STRUCTURE_SLUGS.get(source_slug)
    if slug is None:
        raise BadValue(f"slug={source_slug!r}: unknown structure template")
    values = {"slug": slug, "name": str(row.get("label") or slug)[:100], "labour_rate_key": LABOUR_RATE_KEYS.get(slug, "")}
    structure, outcome = _place_structure(BACKEND, GR_STRUCTURE, row["id"], values, frozen, user=user, result=result)
    if structure is not None:
        structures[row["id"]] = structure
    return outcome


def _place_structure(system, table, source_id, values, frozen, *, user, result):
    structure = mapped(system, table, source_id, StructureTemplate) or StructureTemplate.objects.filter(slug=values["slug"]).first()
    if structure is not None and structure.deleted_at is not None:
        result.violation(table, source_id, "target_deleted", "The structure template was deleted in the platform; not re-created.", severity="warning")
        return None, "skipped"
    if system == BACKEND and structure is not None and _owned_by_flarize(structure):
        diff = _differences(structure, values)
        if diff:
            result.violation(table, source_id, "d2_flarize_wins", f"{values['slug']}: catalog.json wins.", severity="warning", differences=_shown(diff))
        frozen.add(structure.pk)
        remember(system, table, source_id, structure)
        return structure, "unchanged"
    if system == FLARIZE:
        _takeover(result, table, source_id, structure, values)
    structure, outcome = _upsert(masters.STRUCTURE_TEMPLATE, structure, values, user=user)
    remember(system, table, source_id, structure)
    return structure, outcome


def _structure_item_values(*, name, kind, tube_size, weight, price, unit, qty, index) -> dict:
    item_type = {"tube": StructureItemType.TUBE, "fixed": StructureItemType.FIXED}.get(str(kind or ""))
    if item_type is None:
        raise BadValue(f"item_type={kind!r}: not tube/fixed")
    return {
        "name": str(name or "")[:255],
        "item_type": item_type,
        "tube_size": str(tube_size or "")[:12],
        "weight_kg": dec(weight, "weight_kg", places=4, digits=8),
        "unit_price": dec(price, "price"),
        "unit": str(unit or "")[:12],
        "length_m_per_kw": None,
        "qty_rule": {"type": "kw_interpolated", "points": _json_table(qty, "qty") or {}},
        "sort_order": index,
    }


def _gr_structure_item(row, structure, index, frozen, *, user, result) -> str:
    if structure is None:
        result.violation(GR_STRUCTURE_ITEM, row.get("id"), "unknown_template", f"Structure item names template id {row.get('template_id')!r}, which was not imported.")
        return "skipped"
    values = _structure_item_values(
        name=row.get("name"), kind=row.get("item_type"), tube_size=row.get("tube_size"), weight=row.get("weight_kg"), price=row.get("price"), unit=row.get("unit"), qty=row.get("qty"), index=index
    )
    return _place_structure_item(BACKEND, GR_STRUCTURE_ITEM, row["id"], structure, values, frozen, user=user, result=result)


def _place_structure_item(system, table, source_id, structure, values, frozen, *, user, result) -> str:
    if structure.pk in frozen:
        twin = StructureTemplateItem.objects.filter(template=structure, name=values["name"]).order_by("sort_order", "id").first()
        if twin is not None:
            diff = _differences(twin, {k: v for k, v in values.items() if k != "sort_order"})
            if diff:
                result.violation(table, source_id, "d2_flarize_wins", f"{structure.slug}/{values['name']}: catalog.json wins.", severity="warning", differences=_shown(diff))
            remember(system, table, source_id, twin)
        else:
            result.violation(table, source_id, "d2_flarize_wins", f"{structure.slug}/{values['name']}: catalog.json has no such item; not imported.", severity="warning")
        return "unchanged"
    item = mapped(system, table, source_id, StructureTemplateItem)
    if item is not None and item.deleted_at is not None:
        result.violation(table, source_id, "target_deleted", "The structure item was deleted in the platform; not re-created.", severity="warning")
        return "skipped"
    item, outcome = _upsert(masters.STRUCTURE_ITEM, item, values, user=user, create_extra={"template": structure})
    remember(system, table, source_id, item)
    return outcome


def _tube_weight(system, table, source_id, tube_size, weight, *, user, result) -> str:
    size = str(tube_size or "").strip()
    if not size:
        raise BadValue("tube_size: blank")
    values = {"tube_size": size[:12], "weight_kg": dec(weight, "weight_kg", places=4, digits=8)}
    if values["weight_kg"] is None or values["weight_kg"] <= 0:
        raise BadValue(f"weight_kg={weight!r}: must be positive")
    row = mapped(system, table, source_id, TubeWeight) or TubeWeight.objects.filter(tube_size=values["tube_size"]).first()
    if row is not None and row.deleted_at is not None:
        result.violation(table, source_id, "target_deleted", "The tube weight was deleted in the platform; not re-created.", severity="warning")
        return "skipped"
    if system == BACKEND and row is not None and _owned_by_flarize(row):
        diff = _differences(row, values)
        if diff:
            result.violation(table, source_id, "d2_flarize_wins", f"{size}: catalog.json wins.", severity="warning", differences=_shown(diff))
        remember(system, table, source_id, row)
        return "unchanged"
    if system == FLARIZE:
        _takeover(result, table, source_id, row, values)
    row, outcome = _upsert(masters.TUBE_WEIGHT, row, values, user=user)
    remember(system, table, source_id, row)
    return outcome


# ── Flarize catalog.json ─────────────────────────────────────────────────────────────────────────────────────


def _flarize(sections: dict, *, user, result: Run) -> None:
    for system, source in sections["bomTemplates"].items():
        guarded(result, FL_TEMPLATE, system, lambda system=system, source=source: _fl_template(system, source, user=user, result=result))
    for source_slug, source in sections["structureTemplates"].items():
        guarded(result, FL_STRUCTURE, source_slug, lambda source_slug=source_slug, source=source: _fl_structure(source_slug, source, user=user, result=result))
    for size, weight in sections["tubeWeights"].items():
        guarded(result, FL_TUBE, size, lambda size=size, weight=weight: _tube_weight(FLARIZE, FL_TUBE, size, size, weight, user=user, result=result))
    for key, source in sections["packageProfiles"].items():
        guarded(result, FL_PROFILE, key, lambda key=key, source=source: _fl_profile(key, source, user=user, result=result))


def _fl_template(system: str, source: dict, *, user, result: Run) -> str:
    if system not in SYSTEM_TYPES:
        raise BadValue(f"{system!r}: unknown system type")
    values = _template_values(
        system,
        name=source.get("label"),
        description=source.get("description"),
        sizes=source.get("sizes") or {},
        three_phase=source.get("threePhase"),
        tiers=source.get("tiers"),
        batteries=source.get("batteryConfigs"),
    )
    template = mapped(FLARIZE, FL_TEMPLATE, system, Template) or _template_for(values["system_type"])
    if template is not None and template.deleted_at is not None:
        result.violation(FL_TEMPLATE, system, "target_deleted", "The template was deleted in the platform; not re-created.", severity="warning")
        return "skipped"
    _takeover(result, FL_TEMPLATE, system, template, values)
    template, outcome = _upsert(masters.TEMPLATE, template, values, user=user)
    remember(FLARIZE, FL_TEMPLATE, system, template)
    upgrade = system == "upgrade"
    kept_slots, kept_items = set(), set()
    for index, slot in enumerate(source.get("slots") or []):
        guarded(result, FL_SLOT, f"{system}:{slot.get('category')}", lambda slot=slot: _fl_slot(system, template, slot, upgrade, kept_slots, user=user, result=result))
    for index, item in enumerate(source.get("fixedItems") or []):
        ident = item.get("id") or item.get("itemId") or item.get("name") or str(index)
        guarded(result, FL_FIXED, f"{system}:{ident}", lambda item=item, index=index, ident=ident: _fl_fixed(system, template, item, index, ident, upgrade, kept_items, user=user, result=result))
    _drop_others(template.slots.exclude(pk__in=kept_slots), FL_SLOT, system, user=user, result=result)
    _drop_others(template.fixed_items.exclude(pk__in=kept_items), FL_FIXED, system, user=user, result=result)
    return outcome


def _drop_others(queryset, table: str, system: str, *, user, result: Run) -> None:
    """D-2: children only the main backend had are removed from a template Flarize now owns."""
    for row in queryset:
        label = getattr(row, "key", None) or getattr(row, "name", "")
        row.soft_delete(user)
        result.violation(table, f"{system}:{label}", "d2_removed", f"{system}/{label}: not in catalog.json; removed (Flarize wins).", severity="warning")


def _fl_slot(system, template, source: dict, upgrade: bool, kept: set, *, user, result) -> str:
    slug = source.get("category")
    category = Category.objects.filter(slug=slug).first()
    if category is None:
        result.violation(FL_SLOT, f"{system}:{slug}", "unknown_category", f"Category {slug!r} is not in the catalog.")
        return "skipped"
    row = {
        "qty_mode": source.get("qtyMode"),
        "fixed_qty": source.get("fixedQty"),
        "hybrid_qty": source.get("hybridQty"),
        "qty": source.get("qty"),
        "premium_qty": source.get("premiumQty"),
        "bat_qty": source.get("batQty"),
        "premium_bat_qty": source.get("premiumBatQty"),
        "filter_type": source.get("filterType"),
        "filter_phase": source.get("filterPhase"),
        "pos": source.get("pos"),
        "label": source.get("label"),
        "variable": source.get("variable", True),
        "gst": source.get("gst"),
    }
    rule, notes = slot_rule(row, upgrade=upgrade)
    values = _slot_values(row, category, rule, gst_column="gst", source=FL_SLOT, result=result)
    source_id = f"{system}:{slug}"
    _report_notes(result, FL_SLOT, source_id, notes)
    slot = mapped(FLARIZE, FL_SLOT, source_id, Slot) or Slot.objects.filter(template=template, key=slug).first()
    if slot is not None and slot.deleted_at is not None:
        slot = None
    _takeover(result, FL_SLOT, source_id, slot, values)
    slot, outcome = _upsert(masters.SLOT, slot, values, user=user, create_extra={"template": template, "key": slug})
    remember(FLARIZE, FL_SLOT, source_id, slot)
    kept.add(slot.pk)
    return outcome


def _fl_fixed(system, template, source: dict, index: int, ident: str, upgrade: bool, kept: set, *, user, result) -> str:
    source_id = f"{system}:{ident}"
    component = _component_by_sku(source.get("itemId"))
    if source.get("itemId") and component is None:
        result.violation(FL_FIXED, source_id, "unknown_component", f"itemId {source['itemId']!r} is not in the catalog; kept without a component.", severity="warning")
    category = Category.objects.filter(slug=source["category"]).first() if source.get("category") else None
    row = {"qty": source.get("qty"), "premium_qty": source.get("premiumQty"), "bat_qty": source.get("batQty")}
    rule, notes = fixed_rule(row, upgrade=upgrade)
    price = dec(source.get("price"), "price")
    values = {
        "component": component,
        "category": category,
        "code": str(source.get("id") or "")[:64],
        "name": str(source.get("name") or "")[:255],
        "unit_price": price if price is not None or component is None else None,
        "gst_rate": _gst(source.get("gst"), "gst"),
        "qty": None,
        "qty_rule": rule,
        "condition": {},
        "section": str(source.get("_section") or "")[:32],
        "unit": str(source.get("unit") or "")[:12],
        "is_tube": bool(source.get("isTube")),
        "sort_order": index,
    }
    if values["unit_price"] is None and component is None:
        values["unit_price"] = Decimal("0.00")
    _report_notes(result, FL_FIXED, source_id, notes)
    item = mapped(FLARIZE, FL_FIXED, source_id, FixedItem)
    if item is None or item.deleted_at is not None:
        # The main backend's row of the same name (D-2: Flarize takes it over, keeping its legacy map entry).
        item = template.fixed_items.filter(name=values["name"]).exclude(pk__in=kept).order_by("sort_order", "id").first()
    _takeover(result, FL_FIXED, source_id, item, values)
    item, outcome = _upsert(masters.FIXED_ITEM, item, values, user=user, create_extra={"template": template})
    remember(FLARIZE, FL_FIXED, source_id, item)
    kept.add(item.pk)
    return outcome


def _fl_structure(source_slug: str, source: dict, *, user, result: Run) -> str:
    slug = STRUCTURE_SLUGS.get(source_slug)
    if slug is None:
        raise BadValue(f"{source_slug!r}: unknown structure template")
    values = {"slug": slug, "name": str(source.get("label") or slug)[:100], "labour_rate_key": LABOUR_RATE_KEYS.get(slug, "")}
    structure, outcome = _place_structure(FLARIZE, FL_STRUCTURE, source_slug, values, set(), user=user, result=result)
    if structure is None:
        return outcome
    kept: set = set()
    for index, item in enumerate(source.get("items") or []):
        source_id = f"{source_slug}:{index}"
        guarded(result, FL_STRUCTURE_ITEM, source_id, lambda item=item, index=index, source_id=source_id: _fl_structure_item(structure, item, index, source_id, kept, user=user, result=result))
    _drop_others(structure.items.exclude(pk__in=kept), FL_STRUCTURE_ITEM, source_slug, user=user, result=result)
    return outcome


def _fl_structure_item(structure, source: dict, index: int, source_id: str, kept: set, *, user, result) -> str:
    values = _structure_item_values(
        name=source.get("name"),
        kind=source.get("type"),
        tube_size=source.get("tubeSize"),
        weight=source.get("weightKg"),
        price=source.get("price"),
        unit=source.get("unit"),
        qty=source.get("qty"),
        index=index,
    )
    item = mapped(FLARIZE, FL_STRUCTURE_ITEM, source_id, StructureTemplateItem)
    if item is None or item.deleted_at is not None:
        item = structure.items.filter(name=values["name"]).exclude(pk__in=kept).order_by("sort_order", "id").first()
    _takeover(result, FL_STRUCTURE_ITEM, source_id, item, values)
    item, outcome = _upsert(masters.STRUCTURE_ITEM, item, values, user=user, create_extra={"template": structure})
    remember(FLARIZE, FL_STRUCTURE_ITEM, source_id, item)
    kept.add(item.pk)
    return outcome


def _fl_profile(key: str, source: dict, *, user, result: Run) -> str:
    material = PROFILE_MATERIALS.get(str(source.get("structureType") or ""))
    inverter = PROFILE_INVERTERS.get(str(source.get("inverterType") or ""))
    if material is None:
        raise BadValue(f"structureType={source.get('structureType')!r}: not GP/GI/AL")
    if inverter is None:
        raise BadValue(f"inverterType={source.get('inverterType')!r}: not ongrid/hybrid/micro")
    component = _component_by_sku(source.get("batteryComponentId"))
    if source.get("batteryComponentId") and component is None:
        result.violation(FL_PROFILE, key, "unknown_component", f"batteryComponentId {source['batteryComponentId']!r} is not in the catalog.", severity="warning")
    quantity = source.get("batteryQuantity") or 0
    if not isinstance(quantity, int) or isinstance(quantity, bool) or not 0 <= quantity <= 2:
        raise BadValue(f"batteryQuantity={quantity!r}: not 0, 1 or 2")
    repair = source.get("repairMarginOverride")
    values = {
        "key": str(source.get("packageKey") or key)[:32],
        "label": str(source.get("label") or key)[:100],
        "structure_material": material,
        "inverter_type": inverter,
        "battery_included": bool(source.get("batteryIncluded")),
        "battery_brand": str(source.get("batteryBrand") or "")[:100],
        "battery_model": str(source.get("batteryModel") or "")[:100],
        "battery_capacity_kwh": dec(source.get("batteryCapacity") or 0, "batteryCapacity", digits=6),
        "battery_quantity": quantity,
        "battery_component": component,
        "structure_labor_override": dec(source.get("structureLaborOverride"), "structureLaborOverride"),
        "repair_margin_override": None if repair is None else fraction_of(dec(repair, "repairMarginOverride", places=4, digits=9)),
        "notes": str(source.get("notes") or ""),
    }
    if values["key"] != key:
        result.violation(FL_PROFILE, key, "profile_key_differs", f"packageKey {values['key']!r} differs from the map key {key!r}; the packageKey is kept.", severity="warning")
    profile = mapped(FLARIZE, FL_PROFILE, key, PackageProfile) or PackageProfile.objects.filter(key=values["key"]).first()
    if profile is not None and profile.deleted_at is not None:
        result.violation(FL_PROFILE, key, "target_deleted", "The package profile was deleted in the platform; not re-created.", severity="warning")
        return "skipped"
    profile, outcome = _upsert(masters.PACKAGE_PROFILE, profile, values, user=user)
    remember(FLARIZE, FL_PROFILE, key, profile)
    return outcome
