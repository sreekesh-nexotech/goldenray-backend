"""Legacy importers for the catalog (PLAN §7.3, §7.4; mapping tables in docs/decisions/catalog.md).

* :func:`import_goldenray_products` — main backend ``solar_panels`` / ``solar_inverters`` / ``batteries`` rows →
  components (+ spec) + a PUBLISHED public profile each;
* :func:`import_bom_catalog` — main backend ``bom_category`` / ``bom_catalogitem`` / ``bom_itemtier`` rows;
* :func:`import_flarize_catalog` — Flarize ``catalog.json`` (``categories`` with their ``items``) and
  ``battery-master.json`` (the battery engineering overlay);
* :func:`merge_prices` — applies D-2 to the price lists the importers return (pricing imports them later).

Every function takes plain row dicts (as read from the source), is idempotent through ``core_legacy_map`` (a re-run
updates, never duplicates; a mapped row someone deleted since is reported, not re-created) and returns
``{"created", "updated", "unchanged", "skipped", "violations", "prices", "counts"}``. ``dry_run=True`` runs everything
and rolls back. One ``catalog.legacy_import`` audit row per call records the counts and the input checksum.

Rules:

* **Brands** are matched case-insensitively after trimming (``RenewSys`` = ``Renewsys``); a new brand whose name is
  a whole-word prefix of an existing one (``Adani`` / ``Adani Solar``) is reported (``brand_near_match``), never merged.
  The source's spelling is kept per component in ``brand_label``.
* **De-duplication** of website products against BOM/Flarize components: same category, same brand, same size
  (panel watts / inverter kW / battery kWh) and the same model designation compared on letters and digits only. One
  candidate → one record (the website adds its marketing profile and fills empty spec fields; different values are
  reported as ``value_conflict`` and the existing value is kept); several → ``ambiguous_match`` (row not imported);
  none → a new public component, with ``possible_match`` warnings for same brand + size but another model.
* **D-2**: where Flarize ``catalog.json`` and ``bom_*`` describe the same SKU differently, Flarize wins, whichever runs
  first; every differing field is reported (``d2_flarize_wins``).
* Values are converted without loss or invention: a number that does not fit its column, an unknown enum value or an
  over-long text rejects the row (``invalid_value``); ``null`` stays ``null``. Source keys without a typed column land
  in ``component.attributes`` (the category schema is extended with their JSON types).
* Prices are **returned**, not written: ``{"sku", "kind", "amount", "per_watt", "source_system", "source_table",
  "source_id"}``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from decimal import Decimal

from django.db import models, transaction

from audit.services import record
from catalog.models import (
    Category,
    Component,
    ComponentPublicProfile,
    ComponentStatus,
    ComponentTier,
    EngineeringStatus,
    InverterTopology,
    InverterType,
    OverallRating,
    PanelTechnology,
    PanelType,
    ProcurementStatus,
    ProfileStatus,
    RatingTier,
    Tier,
)
from catalog.services import components as component_services
from catalog.services import profiles as profile_services
from catalog.services.brands import find_by_name
from catalog.services.components import get_by_sku, live_tiers, sorted_tiers
from catalog.services.history import record_change
from catalog.services.legacy_support import (
    BACKEND,
    FLARIZE,
    WEBSITE_CATEGORY_FALLBACK,
    BadValue,
    DryRunRollback,
    ImportResult,
    boolean,
    checksum,
    dec,
    earlier,
    ensure_brand,
    extend_attribute_schema,
    fraction_from_percent,
    import_change_log,
    integer,
    json_ready,
    legacy_user,
    mapped_ids,
    mapped_target,
    model_key,
    moment,
    plain,
    provision_category,
    remember,
    set_timestamps,
    snake,
    text,
    upsert_component,
)
from catalog.services.specs import REQUIRED_FOR_ACTIVE, SPEC_RELATED, get_spec, spec_fields, spec_kind
from core.errors import DomainError
from core.models import LegacyMap

PANELS, INVERTERS, BATTERIES = "solar_panels", "solar_inverters", "batteries"
BOM_CATEGORY, BOM_ITEM, BOM_TIER = "bom_category", "bom_catalogitem", "bom_itemtier"
FL_CATEGORY, FL_ITEM, FL_BATTERY = "catalog.json:categories", "catalog.json:items", "battery-master.json:batteries"
COMPONENT_TABLE = "catalog_component"

PANEL_TYPES = {"monocrystalline": PanelType.MONOCRYSTALLINE, "polycrystalline": PanelType.POLYCRYSTALLINE, "bifacial": PanelType.BIFACIAL}
TECHNOLOGIES = {"n-type-topcon": PanelTechnology.N_TYPE_TOPCON, "p-type-perc": PanelTechnology.P_TYPE_PERC, "hjt": PanelTechnology.HJT, "ibc": PanelTechnology.IBC}
OVERALL_RATINGS = {"excellent": OverallRating.EXCELLENT, "very-good": OverallRating.VERY_GOOD, "good": OverallRating.GOOD}
RATING_TIERS = {"premium": RatingTier.PREMIUM, "mid-range": RatingTier.MID_RANGE, "value": RatingTier.VALUE}
WEBSITE_INVERTER_TYPES = {
    "string": (InverterType.ONGRID, InverterTopology.STRING),
    "hybrid": (InverterType.HYBRID, None),
    "microinverter": (InverterType.ONGRID, InverterTopology.MICRO),
    "optimized-string": (InverterType.ONGRID, InverterTopology.OPTIMIZED_STRING),
}
SOURCE_INVERTER_TYPES = {"ongrid": (InverterType.ONGRID, None), "hybrid": (InverterType.HYBRID, None), "micro": (InverterType.ONGRID, InverterTopology.MICRO)}
PHASES = {"1P": "1P", "3P": "3P"}
HYBRID_PHASES = {"1P-HYB": "1P", "3P-HYB": "3P"}  # bom_catalogitem.PHASE_CHOICES
RETIRED_ENGINEERING = {EngineeringStatus.REJECTED, EngineeringStatus.WITHDRAWN}
RETIRED_PROCUREMENT = {ProcurementStatus.INACTIVE, ProcurementStatus.DISCONTINUED}


# ── entry points ───────────────────────────────────────────────────────────────────────────────────────────────


def import_goldenray_products(panels: Iterable[dict], inverters: Iterable[dict], batteries: Iterable[dict], *, user=None, dry_run: bool = False) -> dict:
    """Main backend website products → components + specs + PUBLISHED public profiles (battery prices returned)."""
    panels, inverters, batteries = list(panels or []), list(inverters or []), list(batteries or [])
    return _run("import_goldenray_products", user, dry_run, (panels, inverters, batteries), lambda result: _import_website(panels, inverters, batteries, user=user, result=result))


def import_bom_catalog(categories: Iterable[dict], items: Iterable[dict], tiers: Iterable[dict], *, user=None, dry_run: bool = False) -> dict:
    """Main backend BOM catalog → categories, components, tiers (LIST prices returned)."""
    categories, items, tiers = list(categories or []), list(items or []), list(tiers or [])
    return _run("import_bom_catalog", user, dry_run, (categories, items, tiers), lambda result: _import_bom(categories, items, tiers, user=user, result=result))


def import_flarize_catalog(catalog_json: dict, battery_master_json: dict | None, *, user=None, dry_run: bool = False) -> dict:
    """Flarize ``catalog.json`` categories/items + ``battery-master.json`` → catalog (authoritative, D-2)."""
    catalog_json, battery_master_json = catalog_json or {}, battery_master_json or {}
    return _run(
        "import_flarize_catalog",
        user,
        dry_run,
        (catalog_json.get("categories") or {}, battery_master_json),
        lambda result: _import_flarize(catalog_json, battery_master_json, user=user, result=result),
    )


def merge_prices(flarize_prices: Iterable[dict], bom_prices: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """D-2 on price lists: per (sku, kind) the Flarize price wins; returns ``(prices, differences)``."""
    merged: dict[tuple[str, str], dict] = {}
    for price in bom_prices or []:
        merged[(price["sku"].lower(), price["kind"])] = price
    differences = []
    for price in flarize_prices or []:
        key = (price["sku"].lower(), price["kind"])
        other = merged.get(key)
        if other is not None and (other.get("amount") != price.get("amount") or other.get("per_watt") != price.get("per_watt")):
            differences.append(
                {
                    "sku": price["sku"],
                    "kind": price["kind"],
                    "bom": {"amount": other.get("amount"), "per_watt": other.get("per_watt")},
                    "flarize": {"amount": price.get("amount"), "per_watt": price.get("per_watt")},
                }
            )
        merged[key] = price
    return list(merged.values()), json_ready(differences)


def _run(name: str, user, dry_run: bool, payloads, body: Callable[[ImportResult], None]) -> dict:
    result = ImportResult()
    try:
        with transaction.atomic():
            body(result)
            summary = {key: value for key, value in result.as_dict().items() if key in {"created", "updated", "unchanged", "skipped", "counts"}}
            record(
                "catalog.legacy_import",
                object_type="catalog.component",
                actor=user,
                after={"function": name, "dry_run": dry_run, "sha256": checksum(*payloads), "violations": len(result.violations), **summary},
            )
            if dry_run:
                raise DryRunRollback
    except DryRunRollback:
        pass
    return json_ready(result.as_dict())


def _guarded(result: ImportResult, table: str, source_id, fn: Callable[[], str | None]) -> None:
    """Run one source row in its own savepoint; a bad value or domain error rejects only that row."""
    try:
        with transaction.atomic():
            outcome = fn()
    except BadValue as exc:
        result.violation(table, source_id, "invalid_value", f"{exc.column}: {exc.reason}.", column=exc.column, value=exc.value)
        outcome = "skipped"
    except DomainError as exc:
        result.violation(table, source_id, exc.code, exc.message, errors=exc.errors)
        outcome = "skipped"
    if outcome:
        result.count(table, outcome)


# ── comparison helpers ─────────────────────────────────────────────────────────────────────────────────────────


def _blank(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _same(current, incoming) -> bool:
    if isinstance(current, models.Model) or isinstance(incoming, models.Model):
        return getattr(current, "pk", None) == getattr(incoming, "pk", None)
    if _blank(current) and _blank(incoming):
        return True
    if isinstance(current, Decimal) or isinstance(incoming, Decimal):
        try:
            return Decimal(str(current)) == Decimal(str(incoming))
        except Exception:  # noqa: BLE001 - not comparable as numbers
            return False
    return current == incoming


def _shown(value):
    if isinstance(value, models.Model):
        return str(value)
    return json_ready(value)


# The fields ``bom_catalogitem`` can describe: D-2 compares only these (a field BOM never carries is no disagreement).
BOM_FIELDS = {
    "component": {"name", "brand_label", "brand", "model", "gst_rate_override"},
    "spec": {"wattage_w", "is_dcr", "kw", "phase", "inverter_type"},
    "attributes": {"phase", "type"},
}


def _differences(component: Component, plan: dict, *, only: dict | None = None) -> list[dict]:
    """Fields where ``component`` and ``plan`` disagree (restricted to ``only`` = ``{"component": {...}, ...}``)."""

    def wanted(section: str, name: str) -> bool:
        return only is None or name in only.get(section, ())

    diffs = []
    for name, value in plan["component"].items():
        current = getattr(component, name)
        if wanted("component", name) and not _same(current, value):
            diffs.append({"field": name, "current": _shown(current), "incoming": _shown(value)})
    kind = spec_kind(component.category)
    spec = get_spec(component, kind) if kind else None
    for name, value in plan.get("spec", {}).items():
        current = getattr(spec, name) if spec is not None else None
        if wanted("spec", name) and not _same(current, value):
            diffs.append({"field": f"spec.{name}", "current": _shown(current), "incoming": _shown(value)})
    for key, value in plan.get("attributes", {}).items():
        current = (component.attributes or {}).get(key)
        if wanted("attributes", key) and not _same(current, value):
            diffs.append({"field": f"attributes.{key}", "current": _shown(current), "incoming": _shown(value)})
    if "tiers" in plan and sorted_tiers(plan["tiers"]) != live_tiers(component):
        diffs.append({"field": "tiers", "current": live_tiers(component), "incoming": sorted_tiers(plan["tiers"])})
    return diffs


def _component_data(plan: dict, category: Category, brand, existing: Component | None) -> dict:
    data = {"category": category, "brand": brand, **plan["component"]}
    data.pop("brand_name", None)
    if plan.get("attributes") is not None:
        data["attributes"] = {**((existing.attributes or {}) if existing is not None else {}), **plan["attributes"]}
    if plan.get("spec"):
        kind = spec_kind(category)
        if kind is None:
            raise DomainError("spec_kind_mismatch", f"Category {category.slug!r} has no spec table for this row's technical data.")
        data[SPEC_RELATED[kind]] = plan["spec"]
    if "tiers" in plan:
        data["tiers"] = plan["tiers"]
    return data


def _status_with_spec(status: str, category: Category, data: dict, existing: Component | None, *, result: ImportResult, table: str, source_id) -> str:
    """An ACTIVE panel/inverter needs its spec (wattage / kW): without one it is imported as DRAFT and reported."""
    kind = spec_kind(category)
    if status != ComponentStatus.ACTIVE or kind not in REQUIRED_FOR_ACTIVE:
        return status
    if data.get(SPEC_RELATED[kind]) or (existing is not None and get_spec(existing, kind) is not None):
        return status
    result.violation(table, source_id, "spec_missing", f"No {kind} spec data (wattage / kW); imported as DRAFT.", severity="warning")
    return ComponentStatus.DRAFT


def _record_prices(result: ImportResult, sku: str, prices: list[dict], source_system: str, table: str, source_id) -> None:
    for price in prices:
        if price.get("amount") is None:
            continue
        result.prices.append({"sku": sku, "source_system": source_system, "source_table": table, "source_id": str(source_id), "per_watt": None, **price})


def _category_by_slug(slug: str) -> Category | None:
    return Category.objects.filter(slug=slug).first()


def _tiers(values, column: str) -> list[str]:
    tiers = []
    for value in values or []:
        tier = str(value).upper()
        if tier not in Tier.values:
            raise BadValue(column, value, "unknown tier")
        tiers.append(tier)
    return sorted_tiers(tiers)


# ── BOM catalog ────────────────────────────────────────────────────────────────────────────────────────────────


def _import_bom(categories, items, tiers, *, user, result: ImportResult) -> None:
    by_id: dict = {}
    flarize_categories = {entry.source_id for entry in LegacyMap.objects.filter(source_system=FLARIZE, source_table=FL_CATEGORY)}
    for index, row in enumerate(categories):
        _guarded(result, BOM_CATEGORY, row.get("id"), lambda row=row, index=index: _bom_category(row, index, flarize_categories, user=user, result=result, by_id=by_id))
    tiers_by_item: dict = defaultdict(list)
    known_items = {row.get("id") for row in items}
    for row in tiers:
        if row.get("item_id") not in known_items:
            result.violation(BOM_TIER, row.get("id"), "unknown_item", f"Tier row for unknown item id {row.get('item_id')!r}.")
            result.count(BOM_TIER, "skipped")
            continue
        tiers_by_item[row["item_id"]].append(row)
    flarize_owned = mapped_ids(FLARIZE, FL_ITEM, COMPONENT_TABLE)
    plans = []
    for row in items:
        category = by_id.get(row.get("category_id"))
        if category is None:
            result.violation(BOM_ITEM, row.get("id"), "unknown_category", f"Item {row.get('item_id')!r} names category id {row.get('category_id')!r}, which was not imported.")
            result.count(BOM_ITEM, "skipped")
            continue
        try:
            plans.append((row, category, _bom_plan(row, category, tiers_by_item.get(row["id"], []))))
        except BadValue as exc:
            result.violation(BOM_ITEM, row.get("id"), "invalid_value", f"{exc.column}: {exc.reason}.", column=exc.column, value=exc.value)
            result.count(BOM_ITEM, "skipped")
    for category in {category.pk: category for _, category, _ in plans}.values():
        extend_attribute_schema(category, [plan["attributes"] for _, cat, plan in plans if cat.pk == category.pk], user=user)
    for row, category, plan in plans:
        _guarded(result, BOM_ITEM, row["id"], lambda row=row, category=category, plan=plan: _bom_item(row, Category.objects.get(pk=category.pk), plan, flarize_owned, user=user, result=result))
    _merge_website_twins(user=user, result=result)


def _bom_category(row, index, flarize_categories, *, user, result, by_id) -> str:
    slug = text(row.get("slug"), "slug", max_length=50)
    name = text(row.get("label"), "label", max_length=100)
    gst = fraction_from_percent(row.get("gst_default"), "gst_default")
    category, _ = mapped_target(BACKEND, BOM_CATEGORY, row["id"], Category)
    category = category or Category.all_objects.filter(slug=slug, deleted_at__isnull=True).first()
    if category is not None and category.deleted_at is not None:
        result.violation(BOM_CATEGORY, row["id"], "target_deleted", f"Category {slug!r} was deleted in the platform; not re-created.", severity="warning")
        return "skipped"
    if category is None:
        category = provision_category(slug, name=name, gst_rate=gst, sort_order=index, user=user, result=result, table=BOM_CATEGORY)
        outcome = "created"
    elif slug in flarize_categories:
        diffs = [{"field": key, "current": _shown(getattr(category, key)), "incoming": _shown(value)} for key, value in (("name", name), ("gst_rate", gst)) if not _same(getattr(category, key), value)]
        if diffs:
            result.violation(BOM_CATEGORY, row["id"], "d2_flarize_wins", f"Category {slug!r}: catalog.json wins over bom_category.", severity="warning", slug=slug, differences=diffs)
        outcome = "unchanged"
    else:
        from catalog.services.categories import update_category

        version = category.version
        category = update_category(category, user=user, data={"name": name, "gst_rate": gst})
        outcome = "updated" if category.version != version else "unchanged"
    remember(BACKEND, BOM_CATEGORY, row["id"], category)
    by_id[row["id"]] = category
    return outcome


def _bom_plan(row: dict, category: Category, tier_rows: list[dict]) -> dict:
    kind = spec_kind(category)
    plan = {
        "component": {
            "name": text(row.get("name"), "name", max_length=255),
            "brand_label": text(row.get("brand"), "brand", max_length=100),
            "model": text(row.get("model"), "model", max_length=120),
            "gst_rate_override": fraction_from_percent(row.get("gst_override"), "gst_override"),
        },
        "brand_name": row.get("brand"),
        "spec": {},
        "attributes": {},
        "tiers": _tiers([tier["tier"] for tier in tier_rows], "tier"),
        "tier_rows": tier_rows,
        "prices": [{"kind": "LIST", "amount": dec(row.get("price"), "price", places=2, digits=14), "per_watt": dec(row.get("per_watt"), "per_watt", places=4, digits=10)}],
        "created_at": moment(row.get("created_at")),
        "updated_at": moment(row.get("updated_at")),
    }
    allowed = {"panel": {"watt", "dcr"}, "inverter": {"kw", "phase", "inverter_type"}}.get(kind, {"phase", "inverter_type"})
    for column in ("watt", "dcr", "kw", "phase", "inverter_type"):
        if row.get(column) is not None and column not in allowed:
            raise BadValue(column, row[column], f"category {category.slug!r} has no column for it")
    if kind == "panel":
        plan["spec"] = {"wattage_w": integer(row.get("watt"), "watt", minimum=1), "is_dcr": boolean(row.get("dcr"), "dcr")}
    elif kind == "inverter":
        inverter_type, topology = SOURCE_INVERTER_TYPES.get(row.get("inverter_type") or "", (None, None))
        if row.get("inverter_type") and inverter_type is None:
            raise BadValue("inverter_type", row.get("inverter_type"), "unknown inverter type")
        phase = row.get("phase")
        if phase in HYBRID_PHASES:
            # bom_catalogitem allows 1P-HYB / 3P-HYB on any item (the calculator's HYB slots match on the code): the
            # spec keeps the phase, the exact code stays in attributes.phase (as for DCDB/ACDB rows).
            plan["attributes"]["phase"] = phase
            phase = HYBRID_PHASES[phase]
        if phase and phase not in PHASES:
            raise BadValue("phase", phase, "unknown phase")
        plan["spec"] = {"kw": dec(row.get("kw"), "kw", places=3, digits=7), "phase": phase or None, "inverter_type": inverter_type}
        if topology:
            plan["spec"]["topology"] = topology
    else:
        if row.get("phase"):
            plan["attributes"]["phase"] = str(row["phase"])
        if row.get("inverter_type"):
            plan["attributes"]["type"] = str(row["inverter_type"])
    plan["spec"] = {key: value for key, value in plan["spec"].items() if value is not None}
    return plan


def _bom_item(row: dict, category: Category, plan: dict, flarize_owned: set[int], *, user, result: ImportResult) -> str:
    sku = text(row.get("item_id"), "item_id", max_length=32)
    existing, found = mapped_target(BACKEND, BOM_ITEM, row["id"], Component)
    if found and (existing is None or existing.deleted_at is not None):
        result.violation(BOM_ITEM, row["id"], "target_deleted", f"{sku}: the component was deleted in the platform; not re-created.", severity="warning")
        return "skipped"
    existing = existing or get_by_sku(sku)
    _record_prices(result, sku, plan["prices"], BACKEND, BOM_ITEM, row["id"])
    if existing is not None and existing.pk in flarize_owned:
        existing = Component.objects.select_related("category", "brand").get(pk=existing.pk)
        brand = find_by_name(plan["brand_name"])
        diffs = _differences(existing, {**plan, "component": {**plan["component"], "brand": brand}})
        if existing.category_id != category.pk:
            diffs.insert(0, {"field": "category", "current": existing.category.slug, "incoming": category.slug})
        if diffs:
            differences = [{"field": d["field"], "flarize": d["current"], "bom": d["incoming"]} for d in diffs]
            result.violation(BOM_ITEM, row["id"], "d2_flarize_wins", f"{sku}: catalog.json wins over bom_catalogitem.", severity="warning", sku=sku, differences=differences)
        set_timestamps(existing, created_at=earlier(existing.created_at, plan["created_at"]))
        remember(BACKEND, BOM_ITEM, row["id"], existing)
        _map_tiers(existing, plan, result=result)
        return "unchanged"
    brand = ensure_brand(plan["brand_name"], user=user, result=result, table=BOM_ITEM, source_id=row["id"])
    data = _component_data(plan, category, brand, existing)
    data["sku"] = sku
    status = existing.status if existing is not None else ComponentStatus.ACTIVE
    status = _status_with_spec(status, category, data, existing, result=result, table=BOM_ITEM, source_id=row["id"])
    component, outcome = upsert_component(existing, data, user=user, status=status, reason="legacy import: bom_catalogitem")
    set_timestamps(component, created_at=earlier(component.created_at, plan["created_at"]), updated_at=plan["updated_at"])
    remember(BACKEND, BOM_ITEM, row["id"], component)
    _map_tiers(component, plan, result=result)
    return outcome


def _map_tiers(component: Component, plan: dict, *, result: ImportResult) -> None:
    """Map each ``bom_itemtier`` row to the live tier row it became; a tier Flarize does not offer (D-2) has none: the
    row is reported and counted as skipped (the same outcome as when Flarize runs second, see ``_drop_dead_tier_maps``)."""
    rows = {tier.tier: tier for tier in ComponentTier.objects.filter(component=component)}
    for tier_row in plan["tier_rows"]:
        tier = str(tier_row["tier"]).upper()
        target = rows.get(tier)
        if target is not None:
            remember(BACKEND, BOM_TIER, tier_row["id"], target)
            continue
        LegacyMap.objects.filter(source_system=BACKEND, source_table=BOM_TIER, source_id=str(tier_row["id"])).delete()
        result.violation(
            BOM_TIER, tier_row["id"], "d2_tier_not_kept", f"{component.sku}: catalog.json does not offer tier {tier}; the bom_itemtier row is not kept.", severity="warning", sku=component.sku
        )
        result.count(BOM_TIER, "skipped")


def _drop_dead_tier_maps(component: Component, *, result: ImportResult) -> None:
    """After Flarize replaced a BOM component's tiers (D-2), the ``bom_itemtier`` rows of the tiers it removed map to
    nothing live any more: unmap and report them (the same outcome as when Flarize runs first, see ``_map_tiers``)."""
    dead = ComponentTier.all_objects.filter(component=component, deleted_at__isnull=False)
    stale = LegacyMap.objects.filter(source_system=BACKEND, source_table=BOM_TIER, target_table=ComponentTier._meta.db_table, target_id__in=dead.values("pk"))
    for entry in stale:
        tier = dead.get(pk=entry.target_id).tier
        result.violation(
            BOM_TIER, entry.source_id, "d2_tier_not_kept", f"{component.sku}: catalog.json does not offer tier {tier}; the bom_itemtier row is not kept.", severity="warning", sku=component.sku
        )
    stale.delete()


# ── Flarize catalog.json + battery-master.json ────────────────────────────────────────────────────────────────

COMMON_KEYS = {
    "id",
    "name",
    "brand",
    "tiers",
    "price",
    "perWatt",
    "isPremium",
    "approvalStatus",
    "status",
    "engineeringStatus",
    "note",
    "warranty",
    "gstOverride",
    "unit",
    "createdBy",
    "createdAt",
    "updatedBy",
    "updatedAt",
    "changeLog",
}
PANEL_KEYS = {"watt", "dcr", "voc", "vmp", "isc", "imp", "tempCoeffVoc", "tempCoeffPmax", "cells", "maxSysVoltage", "panelType"}
INVERTER_KEYS = {
    "phase",
    "kw",
    "model",
    "type",
    "mpptCount",
    "mpptVoltageMin",
    "mpptVoltageMax",
    "maxInputVoltage",
    "startVoltage",
    "maxInputCurrent",
    "maxStringsPerMppt",
    "maxIsc",
    "deviceType",
    "panelsPerDevice",
    "optimizerBased",
    "isMicro",
}
STRUCTURE_KEYS = {"tubeSize", "structureRole", "specification"}
MASTER_KEYS = {
    "componentId",
    "brand",
    "model",
    "displayName",
    "batteryType",
    "chemistry",
    "nominalVoltage",
    "minVoltage",
    "maxVoltage",
    "capacityKwh",
    "usableCapacityKwh",
    "continuousChargeCurrent",
    "continuousDischargeCurrent",
    "maximumChargeCurrent",
    "maximumDischargeCurrent",
    "peakCurrent",
    "integratedProtection",
    "protectionType",
    "protectionRating",
    "externalProtectionRequired",
    "communicationProtocol",
    "communicationRequired",
    "compatibleInverters",
    "compatibleSystemTypes",
    "compatiblePhases",
    "purchasePrice",
    "sellingPrice",
    "supplier",
    "supplierReference",
    "datasheetUrl",
    "engineeringStatus",
    "procurementStatus",
    "engineeringNotes",
    "openItems",
    "statusHistory",
    "architecture",
    "changeLog",
}  # every other key of a master record (createdAt, updatedAt, updatedBy, …) is kept in attributes.master_<key>


def _import_flarize(catalog_json: dict, battery_master_json: dict, *, user, result: ImportResult) -> None:
    categories = catalog_json.get("categories") or {}
    masters = dict((battery_master_json or {}).get("batteries") or {})
    item_ids = {str(item.get("id")) for category in categories.values() for item in (category or {}).get("items") or []}
    for key in list(masters):
        if key not in item_ids:
            result.violation(FL_BATTERY, key, "unknown_component", f"battery-master.json names {key!r}, which is not an item of catalog.json.")
            result.count(FL_BATTERY, "skipped")
            masters.pop(key)
    bom_owned = mapped_ids(BACKEND, BOM_ITEM, COMPONENT_TABLE)
    for index, (slug, payload) in enumerate(categories.items()):
        payload = payload or {}
        category = _flarize_category(slug, payload, index, user=user, result=result)
        if category is None:
            for item in payload.get("items") or []:
                result.violation(FL_ITEM, item.get("id"), "category_not_imported", f"Category {slug!r} was not imported.")
                result.count(FL_ITEM, "skipped")
            continue
        plans = []
        for item in payload.get("items") or []:
            try:
                plans.append((item, _flarize_plan(item, category, masters.get(str(item.get("id"))))))
            except BadValue as exc:
                result.violation(FL_ITEM, item.get("id"), "invalid_value", f"{exc.column}: {exc.reason}.", column=exc.column, value=exc.value)
                result.count(FL_ITEM, "skipped")
        category = extend_attribute_schema(category, [plan["attributes"] for _, plan in plans], user=user)
        for item, plan in plans:
            _guarded(result, FL_ITEM, item.get("id"), lambda item=item, plan=plan, category=category: _flarize_item(item, plan, category, bom_owned, user=user, result=result))
    _merge_website_twins(user=user, result=result)


def _flarize_category(slug: str, payload: dict, index: int, *, user, result: ImportResult) -> Category | None:
    try:
        name = text(payload.get("label") or slug, "label", max_length=100)
        gst = fraction_from_percent(payload.get("gstDefault"), "gstDefault")
        if gst is None:
            raise BadValue("gstDefault", None, "missing")
    except BadValue as exc:
        result.violation(FL_CATEGORY, slug, "invalid_value", f"{exc.column}: {exc.reason}.", column=exc.column, value=exc.value)
        result.count(FL_CATEGORY, "skipped")
        return None
    category, _ = mapped_target(FLARIZE, FL_CATEGORY, slug, Category)
    category = category or _category_by_slug(slug)
    if category is not None and category.deleted_at is not None:
        result.violation(FL_CATEGORY, slug, "target_deleted", f"Category {slug!r} was deleted in the platform; its items are not imported.", severity="warning")
        result.count(FL_CATEGORY, "skipped")
        return None
    if category is None:
        category = provision_category(slug, name=name, gst_rate=gst, sort_order=index, user=user, result=result, table=FL_CATEGORY)
        outcome = "created"
    else:
        from catalog.services.categories import update_category

        bom_mapped = LegacyMap.objects.filter(source_system=BACKEND, source_table=BOM_CATEGORY, target_table="catalog_category", target_id=category.pk).exists()
        diffs = [{"field": key, "current": _shown(getattr(category, key)), "incoming": _shown(value)} for key, value in (("name", name), ("gst_rate", gst)) if not _same(getattr(category, key), value)]
        if bom_mapped and diffs and not LegacyMap.objects.filter(source_system=FLARIZE, source_table=FL_CATEGORY, source_id=slug).exists():
            result.violation(FL_CATEGORY, slug, "d2_flarize_wins", f"Category {slug!r}: catalog.json wins over bom_category.", severity="warning", slug=slug, differences=diffs)
        version = category.version
        category = update_category(category, user=user, data={"name": name, "gst_rate": gst, "sort_order": index})
        outcome = "updated" if category.version != version else "unchanged"
    remember(FLARIZE, FL_CATEGORY, slug, category)
    result.count(FL_CATEGORY, outcome)
    return category


def _flarize_status(item: dict, master: dict | None) -> tuple[str, str]:
    status, approval = str(item.get("status") or ""), str(item.get("approvalStatus") or "")
    engineering = str(item.get("engineeringStatus") or "")
    if status == "TEST" or engineering.startswith("TEST_PLACEHOLDER"):
        return ComponentStatus.RETIRED, f"Flarize test placeholder (status {status or '-'}, engineeringStatus {engineering or '-'})."
    if status == "INACTIVE":
        return ComponentStatus.RETIRED, "Flarize status INACTIVE (not selectable)."
    if master:
        eng, proc = master.get("engineeringStatus"), master.get("procurementStatus")
        if eng in RETIRED_ENGINEERING or proc in RETIRED_PROCUREMENT:
            return ComponentStatus.RETIRED, f"battery-master: engineering {eng}, procurement {proc} (never selectable)."
    if approval == "REJECTED":
        return ComponentStatus.RETIRED, "Flarize approvalStatus REJECTED."
    if approval and approval != "APPROVED":
        return ComponentStatus.DRAFT, ""
    return ComponentStatus.ACTIVE, ""


def _flarize_plan(item: dict, category: Category, master: dict | None) -> dict:
    kind = spec_kind(category)
    sku = text(item.get("id"), "id", max_length=32)
    unit = str(item.get("unit") or "").upper()
    if unit and unit not in {"NOS", "M", "KG", "SET"}:
        raise BadValue("unit", item.get("unit"), "unknown unit")
    status, status_reason = _flarize_status(item, master)
    plan = {
        "sku": sku,
        "component": {
            "name": text(item.get("name"), "name", max_length=255),
            "brand_label": text(item.get("brand"), "brand", max_length=100),
            "model": text(item.get("model"), "model", max_length=120),
            "is_premium": bool(boolean(item.get("isPremium"), "isPremium")),
            "gst_rate_override": fraction_from_percent(item.get("gstOverride"), "gstOverride"),
            "unit_override": unit if unit and unit != category.unit else "",
            "warranty_text": text(item.get("warranty"), "warranty", max_length=64),
            "engineering_status": text(item.get("engineeringStatus"), "engineeringStatus", max_length=48),
            "notes": text(item.get("note"), "note", max_length=10000),
        },
        "brand_name": item.get("brand"),
        "spec": {},
        "attributes": {},
        "tiers": _tiers(item.get("tiers"), "tiers"),
        "status": status,
        "status_reason": status_reason,
        "prices": [{"kind": "LIST", "amount": dec(item.get("price"), "price", places=2, digits=14), "per_watt": dec(item.get("perWatt"), "perWatt", places=4, digits=10)}],
        "created_at": moment(item.get("createdAt")),
        "updated_at": moment(item.get("updatedAt")),
        "created_by": item.get("createdBy"),
        "updated_by": item.get("updatedBy"),
        "change_log": item.get("changeLog"),
        "warnings": [],
    }
    consumed = set(COMMON_KEYS)
    if kind == "panel":
        consumed |= PANEL_KEYS
        plan["spec"] = _flarize_panel_spec(item, plan)
    elif kind == "inverter":
        consumed |= INVERTER_KEYS
        plan["spec"] = _flarize_inverter_spec(item)
    elif kind == "structure":
        consumed |= STRUCTURE_KEYS
        plan["spec"] = {
            "tube_size": text(item.get("tubeSize"), "tubeSize", max_length=12),
            "structure_role": text(item.get("structureRole"), "structureRole", max_length=24),
            "specification": text(item.get("specification"), "specification", max_length=255),
        }
    elif kind == "battery" and master:
        plan["spec"], master_prices = _battery_master_spec(master, plan)
        plan["prices"] += master_prices
    for key, value in item.items():
        if key not in consumed:
            plan["attributes"][snake(key)] = plain(value)
    if kind in ("battery", "structure") and not any(not _blank(value) for value in plan["spec"].values()):
        plan["spec"] = {}
    else:
        plan["spec"] = {key: value for key, value in plan["spec"].items() if value is not None}
    return plan


def _flarize_panel_spec(item: dict, plan: dict) -> dict:
    dcr = boolean(item.get("dcr"), "dcr")
    panel_type = item.get("panelType")
    if panel_type not in (None, "DCR", "NON_DCR"):
        raise BadValue("panelType", panel_type, "expected DCR or NON_DCR")
    if panel_type is not None and dcr is not None and (panel_type == "DCR") != dcr:
        plan["warnings"].append(("panel_type_mismatch", f"panelType {panel_type} disagrees with dcr={dcr}; is_dcr follows dcr."))
    return {
        "wattage_w": integer(item.get("watt"), "watt", minimum=1),
        "is_dcr": dcr if dcr is not None else (None if panel_type is None else panel_type == "DCR"),
        "voc_v": dec(item.get("voc"), "voc", places=2, digits=6),
        "vmp_v": dec(item.get("vmp"), "vmp", places=2, digits=6),
        "isc_a": dec(item.get("isc"), "isc", places=2, digits=6),
        "imp_a": dec(item.get("imp"), "imp", places=2, digits=6),
        "temperature_coefficient_voc": dec(item.get("tempCoeffVoc"), "tempCoeffVoc", places=3, digits=5),
        "temperature_coefficient": dec(item.get("tempCoeffPmax"), "tempCoeffPmax", places=3, digits=5),
        "cell_count": integer(item.get("cells"), "cells", maximum=32767),
        "max_system_voltage_v": integer(item.get("maxSysVoltage"), "maxSysVoltage"),
    }


def _flarize_inverter_spec(item: dict) -> dict:
    kind = item.get("type")
    inverter_type, topology = SOURCE_INVERTER_TYPES.get(kind or "", (None, None))
    if kind and inverter_type is None:
        raise BadValue("type", kind, "unknown inverter type")
    device_type = text(item.get("deviceType"), "deviceType", max_length=24)
    if topology is None and device_type == "string_inverter":
        topology = InverterTopology.STRING
    if boolean(item.get("optimizerBased"), "optimizerBased"):
        topology = InverterTopology.OPTIMIZED_STRING
    if boolean(item.get("isMicro"), "isMicro"):
        topology = InverterTopology.MICRO
    phase = item.get("phase")
    if phase and phase not in PHASES:
        raise BadValue("phase", phase, "unknown phase")
    return {
        "kw": dec(item.get("kw"), "kw", places=3, digits=7),
        "phase": phase or None,
        "inverter_type": inverter_type,
        "topology": topology,
        "device_type": device_type,
        "panels_per_device": integer(item.get("panelsPerDevice"), "panelsPerDevice", maximum=32767),
        "mppt_count": integer(item.get("mpptCount"), "mpptCount", maximum=32767),
        "mppt_voltage_min_v": integer(item.get("mpptVoltageMin"), "mpptVoltageMin"),
        "mppt_voltage_max_v": integer(item.get("mpptVoltageMax"), "mpptVoltageMax"),
        "max_pv_voltage_v": integer(item.get("maxInputVoltage"), "maxInputVoltage"),
        "start_voltage_v": integer(item.get("startVoltage"), "startVoltage"),
        "max_input_current_a": dec(item.get("maxInputCurrent"), "maxInputCurrent", places=2, digits=6),
        "max_strings_per_mppt": integer(item.get("maxStringsPerMppt"), "maxStringsPerMppt", maximum=32767),
        "max_isc_a": dec(item.get("maxIsc"), "maxIsc", places=2, digits=6),
    }


def _nullable_list(value, column: str):
    if value is None:
        return None
    if not isinstance(value, list):
        raise BadValue(column, value, "expected a list or null")
    return [str(entry) for entry in value]


def _choice(value, column: str, choices) -> str | None:
    if value is None or value == "":
        return None
    if value not in choices:
        raise BadValue(column, value, "unknown value")
    return value


def _battery_master_spec(master: dict, plan: dict) -> tuple[dict, list[dict]]:
    volts = {"places": 2, "digits": 6}
    amps = {"places": 2, "digits": 7}
    if master.get("displayName") not in (None, plan["component"]["name"]):
        plan["warnings"].append(("display_name_differs", f"battery-master displayName {master.get('displayName')!r} differs from the catalog name; the catalog name is kept."))
    if master.get("brand") not in (None, plan["brand_name"]):
        plan["warnings"].append(("brand_differs", f"battery-master brand {master.get('brand')!r} differs from the catalog brand; the catalog brand is kept."))
    if master.get("model") and not plan["component"]["model"]:
        plan["component"]["model"] = text(master.get("model"), "model", max_length=120)
    if master.get("datasheetUrl"):
        plan["component"]["datasheet_url"] = text(master.get("datasheetUrl"), "datasheetUrl", max_length=500)
    for key, value in master.items():
        if key not in MASTER_KEYS:
            plan["attributes"][f"master_{snake(key)}"] = plain(value)
    if master.get("changeLog"):
        plan["change_log"] = [*(plan.get("change_log") or []), *master["changeLog"]]
    spec = {
        "battery_type": text(master.get("batteryType"), "batteryType", max_length=32),
        "chemistry": _choice(master.get("chemistry"), "chemistry", {"LFP", "LEAD_ACID", "NMC"}),
        "nominal_voltage_v": dec(master.get("nominalVoltage"), "nominalVoltage", **volts),
        "min_voltage_v": dec(master.get("minVoltage"), "minVoltage", **volts),
        "max_voltage_v": dec(master.get("maxVoltage"), "maxVoltage", **volts),
        "capacity_kwh": dec(master.get("capacityKwh"), "capacityKwh", places=2, digits=6),
        "usable_kwh": dec(master.get("usableCapacityKwh"), "usableCapacityKwh", places=2, digits=6),
        "continuous_charge_current_a": dec(master.get("continuousChargeCurrent"), "continuousChargeCurrent", **amps),
        "continuous_discharge_current_a": dec(master.get("continuousDischargeCurrent"), "continuousDischargeCurrent", **amps),
        "maximum_charge_current_a": dec(master.get("maximumChargeCurrent"), "maximumChargeCurrent", **amps),
        "maximum_discharge_current_a": dec(master.get("maximumDischargeCurrent"), "maximumDischargeCurrent", **amps),
        "peak_current_a": dec(master.get("peakCurrent"), "peakCurrent", **amps),
        "integrated_protection": boolean(master.get("integratedProtection"), "integratedProtection"),
        "protection_type": text(master.get("protectionType"), "protectionType", max_length=32),
        "protection_rating": text(master.get("protectionRating"), "protectionRating", max_length=12),
        "external_protection_required": boolean(master.get("externalProtectionRequired"), "externalProtectionRequired"),
        "communication_protocol": text(master.get("communicationProtocol"), "communicationProtocol", max_length=64),
        "communication_required": boolean(master.get("communicationRequired"), "communicationRequired"),
        "compatible_inverters": _nullable_list(master.get("compatibleInverters"), "compatibleInverters"),
        "compatible_system_types": _nullable_list(master.get("compatibleSystemTypes"), "compatibleSystemTypes"),
        "compatible_phases": _nullable_list(master.get("compatiblePhases"), "compatiblePhases"),
        "architecture": text(master.get("architecture"), "architecture", max_length=24),
        "engineering_status": _choice(master.get("engineeringStatus"), "engineeringStatus", set(EngineeringStatus.values)) or EngineeringStatus.PENDING_ENGINEERING_APPROVAL,
        "procurement_status": _choice(master.get("procurementStatus"), "procurementStatus", set(ProcurementStatus.values)) or ProcurementStatus.ACTIVE,
        "engineering_notes": text(master.get("engineeringNotes"), "engineeringNotes", max_length=20000),
        "open_items": _nullable_list(master.get("openItems"), "openItems") or [],
        "status_history": plain(master.get("statusHistory") or []),
        "supplier": text(master.get("supplier"), "supplier", max_length=120),
        "supplier_reference": text(master.get("supplierReference"), "supplierReference", max_length=120),
    }
    # A null list means "not recorded" for the compatibility checks: keep it even though other nulls are dropped.
    spec["_keep_null"] = ("compatible_inverters", "compatible_system_types", "compatible_phases")
    prices = [{"kind": "PURCHASE", "amount": dec(master.get("purchasePrice"), "purchasePrice", places=2, digits=14), "per_watt": None}]
    selling = dec(master.get("sellingPrice"), "sellingPrice", places=2, digits=14)
    listed = next((price for price in plan["prices"] if price["kind"] == "LIST"), None)
    if selling is not None:
        # One LIST price per SKU: catalog.json's (PLAN §7.6 #7); the master's sellingPrice only fills a missing one.
        if listed is None or listed["amount"] is None:
            prices.append({"kind": "LIST", "amount": selling, "per_watt": None})
        elif listed["amount"] != selling:
            plan["warnings"].append(("selling_price_differs", f"battery-master sellingPrice {selling} differs from the catalog.json price {listed['amount']}; the catalog price is the LIST price."))
    return spec, prices


def _flarize_item(item: dict, plan: dict, category: Category, bom_owned: set[int], *, user, result: ImportResult) -> str:
    sku = plan["sku"]
    existing, found = mapped_target(FLARIZE, FL_ITEM, sku, Component)
    if found and (existing is None or existing.deleted_at is not None):
        result.violation(FL_ITEM, sku, "target_deleted", f"{sku}: the component was deleted in the platform; not re-created.", severity="warning")
        return "skipped"
    existing = existing or get_by_sku(sku)
    for code, message in plan["warnings"]:
        result.violation(FL_ITEM, sku, code, message, severity="warning")
    _record_prices(result, sku, plan["prices"], FLARIZE, FL_ITEM, sku)
    brand = ensure_brand(plan["brand_name"], user=user, result=result, table=FL_ITEM, source_id=sku)
    spec = dict(plan["spec"])
    for name in spec.pop("_keep_null", ()):
        spec.setdefault(name, None)
    if existing is not None and existing.pk in bom_owned:
        existing = Component.objects.select_related("category", "brand").get(pk=existing.pk)
        diffs = _differences(existing, {**plan, "spec": spec, "component": {**plan["component"], "brand": brand}}, only=BOM_FIELDS)
        if existing.category_id != category.pk:
            diffs.insert(0, {"field": "category", "current": existing.category.slug, "incoming": category.slug})
        if diffs:
            differences = [{"field": d["field"], "bom": d["current"], "flarize": d["incoming"]} for d in diffs]
            result.violation(FL_ITEM, sku, "d2_flarize_wins", f"{sku}: catalog.json wins over bom_catalogitem.", severity="warning", sku=sku, differences=differences)
    data = _component_data({**plan, "spec": spec}, category, brand, existing)
    data["sku"] = sku
    status = _status_with_spec(plan["status"], category, data, existing, result=result, table=FL_ITEM, source_id=sku)
    component, outcome = upsert_component(existing, data, user=user, status=status, reason="legacy import: Flarize catalog.json", status_reason=plan["status_reason"])
    set_timestamps(
        component,
        created_at=earlier(component.created_at, plan["created_at"]),
        updated_at=plan["updated_at"],
        created_by=legacy_user(FLARIZE, "users.json", plan["created_by"]),
        updated_by=legacy_user(FLARIZE, "users.json", plan["updated_by"]),
    )
    if import_change_log(component, plan["change_log"], source_system=FLARIZE, reason="Flarize changeLog") and outcome == "unchanged":
        outcome = "updated"
    if existing is not None and existing.pk in bom_owned:
        _drop_dead_tier_maps(component, result=result)
    remember(FLARIZE, FL_ITEM, sku, component)
    return outcome


# ── website products (solar_panels, solar_inverters, batteries) ───────────────────────────────────────────────


def _import_website(panels, inverters, batteries, *, user, result: ImportResult) -> None:
    for kind, table, rows in (("panel", PANELS, panels), ("inverter", INVERTERS, inverters), ("battery", BATTERIES, batteries)):
        if not rows:
            continue
        category = _category_by_slug(kind)
        if category is None:
            name, gst = WEBSITE_CATEGORY_FALLBACK[kind]
            category = provision_category(kind, name=name, gst_rate=gst, sort_order=0, user=user, result=result, table=table)
        own = mapped_ids(BACKEND, table, COMPONENT_TABLE)
        for row in rows:
            _guarded(result, table, row.get("id"), lambda row=row, kind=kind, table=table, category=category, own=own: _website_row(kind, table, row, category, own, user=user, result=result))


def _ratings(row: dict, keys: dict[str, str]) -> dict:
    return {target: integer(row.get(source), source, maximum=100) for target, source in keys.items() if row.get(source) is not None}


def _website_panel(row: dict) -> dict:
    return {
        "size": integer(row.get("wattage"), "wattage", minimum=1),
        "model": text(row.get("name"), "name", max_length=120),
        "component": {
            "name": text(f"{row.get('brand')} {row.get('name')} - {row.get('wattage')}Wp", "name", max_length=255),
            "warranty_product_years": integer(row.get("product_warranty"), "product_warranty", maximum=100),
            "warranty_performance_years": integer(row.get("performance_warranty"), "performance_warranty", maximum=100),
        },
        "spec": {
            "wattage_w": integer(row.get("wattage"), "wattage", minimum=1),
            "panel_type": _mapped_choice(row.get("panel_type"), "panel_type", PANEL_TYPES),
            "technology": _mapped_choice(row.get("technology"), "technology", TECHNOLOGIES),
            "efficiency_pct": dec(row.get("efficiency"), "efficiency", places=2, digits=5),
            "temperature_coefficient": dec(row.get("temperature_coefficient"), "temperature_coefficient", places=3, digits=5),
            "noct_c": integer(row.get("noct"), "noct", minimum=-32768, maximum=32767),
            "real_output_at_60c_pct": integer(row.get("real_output_at_60c"), "real_output_at_60c", maximum=32767),
            "ip_rating": text(row.get("ip_rating"), "ip_rating", max_length=10),
            "wind_load_pa": integer(row.get("wind_load"), "wind_load"),
            "moisture_protection": text(row.get("moisture_protection"), "moisture_protection", max_length=50),
            "weight_kg": dec(row.get("weight"), "weight", places=2, digits=6),
            "bifacial_gain_pct": integer(row.get("bifacial_gain"), "bifacial_gain", minimum=-32768, maximum=32767),
            "first_year_drop_pct": dec(row.get("first_year_power_drop"), "first_year_power_drop", places=2, digits=4),
            "annual_degradation_pct": dec(row.get("annual_degradation"), "annual_degradation", places=2, digits=4),
            "output_at_year_25_pct": dec(row.get("output_at_year_25"), "output_at_year_25", places=2, digits=5),
            "manufacturing_capacity": text(row.get("manufacturing_capacity"), "manufacturing_capacity", max_length=50),
            "bloomberg_tier1": boolean(row.get("bloomberg_tier1"), "bloomberg_tier1"),
            "pvel_top_performer": boolean(row.get("pvel_top_performer"), "pvel_top_performer"),
            "bis_certified": boolean(row.get("bis_certified"), "bis_certified"),
            "independent_audit": boolean(row.get("independent_audit"), "independent_audit"),
            "certifications": _string_list(row.get("certifications"), "certifications"),
        },
        "profile": {
            "subsidy_eligible": boolean(row.get("subsidy_eligible"), "subsidy_eligible"),
            "ratings": _ratings(row, {"efficiency": "efficiency_rating", "heat_performance": "heat_performance_rating", "warranty": "warranty_rating", "kerala_climate": "kerala_climate_rating"}),
        },
    }


def _website_inverter(row: dict) -> dict:
    watts = integer(row.get("rated_output_power"), "rated_output_power", minimum=1)
    dc_watts = integer(row.get("maximum_dc_input"), "maximum_dc_input")
    inverter_type, topology = WEBSITE_INVERTER_TYPES.get(row.get("inverter_type") or "", (None, None))
    if inverter_type is None:
        raise BadValue("inverter_type", row.get("inverter_type"), "unknown inverter type")
    kw = dec(Decimal(watts) / 1000, "rated_output_power", places=3, digits=7)
    return {
        "size": kw,
        "model": text(row.get("name"), "name", max_length=120),
        "component": {
            "name": text(f"{row.get('brand')} {row.get('name')}", "name", max_length=255),
            "warranty_product_years": integer(row.get("warranty_years"), "warranty_years", maximum=100),
            "warranty_extendable_years": integer(row.get("extendable_warranty_years"), "extendable_warranty_years", maximum=100),
        },
        "spec": {
            "kw": kw,
            "inverter_type": inverter_type,
            "topology": topology,
            "max_dc_input_kw": None if dc_watts is None else dec(Decimal(dc_watts) / 1000, "maximum_dc_input", places=3, digits=7),
            "mppt_count": integer(row.get("mppt_trackers"), "mppt_trackers", maximum=32767),
            "max_pv_voltage_v": integer(row.get("maximum_dc_voltage"), "maximum_dc_voltage"),
            "max_input_current_text": text(row.get("maximum_input_current"), "maximum_input_current", max_length=50),
            "weight_kg": dec(row.get("weight"), "weight", places=2, digits=6),
            "display": text(row.get("display"), "display", max_length=100),
            "suitable_system_size": text(row.get("suitable_system_size"), "suitable_system_size", max_length=100),
            "efficiency_pct": dec(row.get("maximum_efficiency"), "maximum_efficiency", places=2, digits=5),
            "european_efficiency_pct": dec(row.get("european_efficiency"), "european_efficiency", places=2, digits=5),
            "mppt_efficiency_pct": dec(row.get("mppt_efficiency"), "mppt_efficiency", places=2, digits=5),
            "dc_oversizing_pct": integer(row.get("dc_oversizing"), "dc_oversizing", maximum=32767),
            "ac_overloading_pct": integer(row.get("ac_overloading"), "ac_overloading", maximum=32767),
            "pid_protection": boolean(row.get("pid_protection"), "pid_protection"),
            "iv_curve_scanning": text(row.get("iv_curve_scanning"), "iv_curve_scanning", max_length=100),
            "ip_rating": text(row.get("ip_rating"), "ip_rating", max_length=10),
            "corrosion_protection": text(row.get("corrosion_protection"), "corrosion_protection", max_length=100),
            "operating_temperature": text(row.get("operating_temperature"), "operating_temperature", max_length=50),
            "cooling": text(row.get("cooling"), "cooling", max_length=100),
            "noise_level": text(row.get("noise_level"), "noise_level", max_length=50),
            "dc_surge_protection": text(row.get("dc_surge_protection"), "dc_surge_protection", max_length=50),
            "ac_surge_protection": text(row.get("ac_surge_protection"), "ac_surge_protection", max_length=50),
            "arc_fault_detection": text(row.get("arc_fault_detection"), "arc_fault_detection", max_length=50),
            "grid_protection": boolean(row.get("grid_protection"), "grid_protection"),
            "monitoring_app": text(row.get("monitoring_app"), "monitoring_app", max_length=100),
            "real_time_monitoring": boolean(row.get("real_time_monitoring"), "real_time_monitoring"),
            "remote_diagnostics": text(row.get("remote_diagnostics"), "remote_diagnostics", max_length=100),
            "firmware_updates": text(row.get("firmware_updates"), "firmware_updates", max_length=100),
            "connectivity": text(row.get("connectivity"), "connectivity", max_length=100),
            "certifications": _string_list(row.get("certifications"), "certifications"),
            "brand_trust": text(row.get("brand_trust"), "brand_trust", max_length=50),
            "year_founded": integer(row.get("year_founded"), "year_founded", maximum=32767),
            "countries_served": text(row.get("countries_served"), "countries_served", max_length=50),
            "global_installations": text(row.get("global_installations"), "global_installations", max_length=50),
        },
        "profile": {
            "rating_tier": _mapped_choice(row.get("rating_tier"), "rating_tier", RATING_TIERS),
            "ratings": _ratings(row, {"efficiency": "efficiency_rating", "reliability": "reliability_rating", "warranty": "warranty_rating", "kerala_climate": "kerala_climate_rating"}),
        },
    }


def _website_battery(row: dict) -> dict:
    capacity = dec(row.get("battery_capacity"), "battery_capacity", places=2, digits=6)
    backup = dec(row.get("backup_hour"), "backup_hour", places=2, digits=5)
    if capacity is None:
        raise BadValue("battery_capacity", None, "missing")
    return {
        "size": capacity,
        "model": "",
        "component": {"name": f"Battery {capacity}kWh - {backup}h backup"},
        "spec": {"capacity_kwh": capacity, "backup_hours": backup},
        "profile": {},
        "prices": [{"kind": "LIST", "amount": dec(row.get("battery_price"), "battery_price", places=2, digits=14), "per_watt": None}],
    }


WEBSITE_PLANS = {"panel": _website_panel, "inverter": _website_inverter, "battery": _website_battery}


def _mapped_choice(value, column: str, mapping: dict):
    if value is None or value == "":
        return None
    if value not in mapping:
        raise BadValue(column, value, "unknown value")
    return mapping[value]


def _string_list(value, column: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
        raise BadValue(column, value, "expected a list of strings")
    return value


def _candidates(kind: str, category: Category, brand, model: str, size, exclude: set[int]) -> tuple[list[Component], list[Component]]:
    if brand is None or not model_key(model) or size is None:
        return [], []
    queryset = Component.objects.filter(category=category, brand=brand).exclude(pk__in=exclude)
    lookup = {"panel": "panel_spec__wattage_w", "inverter": "inverter_spec__kw", "battery": "battery_spec__capacity_kwh"}[kind]
    queryset = queryset.filter(**{lookup: size}).order_by("sku")
    exact = [component for component in queryset if model_key(component.model) == model_key(model)]
    near = [component for component in queryset if component not in exact]
    return exact, near


def _shared(component: Component, table: str) -> bool:
    """Whether another source (BOM or Flarize) also feeds this component."""
    return LegacyMap.objects.filter(target_table=COMPONENT_TABLE, target_id=component.pk).exclude(source_system=BACKEND, source_table=table).exists()


def _merge_into(component: Component, plan: dict, *, table: str, source_id, result: ImportResult) -> dict:
    """Fill empty fields of a shared component; report (and keep) values that differ."""
    kind = spec_kind(component.category)
    spec = get_spec(component, kind) if kind else None
    merged = {"component": {}, "spec": {}}
    conflicts = []
    for name, value in plan["component"].items():
        if name == "name":
            continue
        current = getattr(component, name)
        if _blank(current):
            merged["component"][name] = value
        elif not _same(current, value) and not _blank(value):
            conflicts.append({"field": name, "current": _shown(current), "incoming": _shown(value)})
    for name, value in plan["spec"].items():
        current = getattr(spec, name) if spec is not None else None
        if _blank(current):
            if not _blank(value):
                merged["spec"][name] = value
        elif not _same(current, value) and not _blank(value):
            conflicts.append({"field": f"spec.{name}", "current": _shown(current), "incoming": _shown(value)})
    if conflicts:
        result.violation(
            table,
            source_id,
            "value_conflict",
            f"{component.sku}: the website row differs from the catalog record; the catalog values are kept.",
            severity="warning",
            sku=component.sku,
            differences=conflicts,
        )
    return merged


def _website_row(kind: str, table: str, row: dict, category: Category, own: set[int], *, user, result: ImportResult) -> str:
    source_id = row.get("id")
    plan = WEBSITE_PLANS[kind](row)
    existing, found = mapped_target(BACKEND, table, source_id, Component)
    if found and (existing is None or existing.deleted_at is not None):
        result.violation(table, source_id, "target_deleted", "The component was deleted in the platform; not re-created.", severity="warning")
        return "skipped"
    brand_name = row.get("brand")
    if existing is None:
        exact, near = _candidates(kind, category, find_by_name(brand_name), plan["model"], plan["size"], own)
        if len(exact) > 1:
            result.violation(table, source_id, "ambiguous_match", f"{brand_name} {plan['model']} matches several components; not imported.", candidates=[component.sku for component in exact])
            return "skipped"
        if exact:
            existing = exact[0]
        elif near:
            result.violation(
                table,
                source_id,
                "possible_match",
                f"{brand_name} {plan['model']}: same brand and size as {', '.join(c.sku for c in near)} but another model; created separately.",
                severity="warning",
                candidates=[component.sku for component in near],
            )
    brand = ensure_brand(brand_name, user=user, result=result, table=table, source_id=source_id)
    created_at, updated_at = moment(row.get("created_at")), moment(row.get("updated_at"))
    shared = existing is not None and _shared(existing, table)
    if shared:
        existing = Component.objects.select_related("category", "brand").get(pk=existing.pk)
        if brand_name and existing.brand_label != brand_name:
            result.violation(table, source_id, "brand_label_differs", f"{existing.sku}: printed as {existing.brand_label!r} in the catalog, {brand_name!r} on the website.", severity="warning")
        merged = _merge_into(existing, plan, table=table, source_id=source_id, result=result)
        data = {**merged["component"], "is_public": True}
        if merged["spec"]:
            data[SPEC_RELATED[kind]] = merged["spec"]
        status = existing.status
    else:
        data = {"category": category, "brand": brand, "brand_label": text(brand_name, "brand", max_length=100), "model": plan["model"], "is_public": True, **plan["component"]}
        data[SPEC_RELATED[kind]] = dict(plan["spec"])
        status = existing.status if existing is not None and existing.status == ComponentStatus.RETIRED else ComponentStatus.ACTIVE
    component, outcome = upsert_component(existing, data, user=user, status=status, reason=f"legacy import: {table}")
    if not shared:
        own.add(component.pk)  # a later row of the same table never merges into this one
    if not shared:
        set_timestamps(component, created_at=created_at, updated_at=updated_at)
    profile_outcome = _upsert_profile(component, row, plan, created_at=created_at, updated_at=updated_at, user=user, table=table, result=result)
    remember(BACKEND, table, source_id, component)
    _record_prices(result, component.sku, plan.get("prices", []), BACKEND, table, source_id)
    return "updated" if outcome == "unchanged" and profile_outcome != "unchanged" else outcome


def _upsert_profile(component: Component, row: dict, plan: dict, *, created_at, updated_at, user, table: str, result: ImportResult) -> str:
    values = {
        "headline": text(row.get("name"), "name", max_length=200) if row.get("name") is not None else "",
        "summary": text(row.get("description"), "description", max_length=100000),
        "image_url": text(row.get("image_url"), "image_url", max_length=500),
        "price_range_label": text(row.get("price_range"), "price_range", max_length=100),
        "kerala_climate_score": integer(row.get("kerala_climate_score"), "kerala_climate_score", maximum=100),
        "overall_rating": _mapped_choice(row.get("overall_rating"), "overall_rating", OVERALL_RATINGS),
        **plan["profile"],
    }
    profile = ComponentPublicProfile.objects.filter(component=component).first()
    if profile is None:
        profile = profile_services.create_profile(user=user, data={"component": component, **values})
        outcome = "created"
    else:
        version = profile.version
        profile = profile_services.update_profile(profile, user=user, data=values)
        outcome = "updated" if profile.version != version else "unchanged"
    if profile.status != "PUBLISHED":
        problems = profile_services.publish_problems(component)
        if problems:
            result.violation(table, row.get("id"), "profile_not_published", f"{component.sku}: {' '.join(problems)}", severity="warning")
        else:
            profile = profile_services.publish(profile, user=user)
            ComponentPublicProfile.all_objects.filter(pk=profile.pk).update(published_at=created_at or profile.published_at)
    set_timestamps(profile, created_at=created_at, updated_at=updated_at)
    return outcome


# ── website products imported before their BOM / Flarize twin ────────────────────────────────────────────────

WEBSITE_TABLES = {"panel": PANELS, "inverter": INVERTERS, "battery": BATTERIES}
SIZE_FIELDS = {"panel": "wattage_w", "inverter": "kw", "battery": "capacity_kwh"}
# The component columns a website row fills (everything else it carries is spec or public profile).
WEBSITE_COMPONENT_FIELDS = {"panel": ("warranty_product_years", "warranty_performance_years"), "inverter": ("warranty_product_years", "warranty_extendable_years"), "battery": ()}


def _merge_website_twins(*, user, result: ImportResult) -> None:
    """Merge website products that were imported *before* their BOM/Flarize record into it (import order must not matter).

    A website row imported after the catalog is matched at once (``_website_row``: same category, brand, size and model
    on letters and digits). Imported first, it became a public-only component; once a component of another source
    matches it, this pass (run at the end of the BOM and Flarize imports) does what the later website import would have
    done: the catalog record keeps its values, its blanks are filled from the website product (differences reported),
    the public profile and the legacy map move to it, and the website-only component is deleted. Several matches →
    ``ambiguous_match``; nothing is guessed.
    """
    for kind, table in WEBSITE_TABLES.items():
        own = mapped_ids(BACKEND, table, COMPONENT_TABLE)
        for entry in LegacyMap.objects.filter(source_system=BACKEND, source_table=table, target_table=COMPONENT_TABLE).order_by("id"):
            website = Component.objects.select_related("category", "brand").filter(pk=entry.target_id).first()
            if website is None or _shared(website, table):
                continue
            spec = get_spec(website, kind)
            exact, _ = _candidates(kind, website.category, website.brand, website.model, getattr(spec, SIZE_FIELDS[kind], None), own)
            if len(exact) > 1:
                message = f"{website.brand_label} {website.model} matches several catalog components; the website product is kept separate."
                result.violation(table, entry.source_id, "ambiguous_match", message, severity="warning", sku=website.sku, candidates=[component.sku for component in exact])
            elif exact:
                _guarded(
                    result,
                    table,
                    entry.source_id,
                    lambda website=website, twin=exact[0], kind=kind, table=table, source_id=entry.source_id: _absorb_website_twin(
                        website, twin, kind, table=table, source_id=source_id, user=user, result=result
                    ),
                )


def _absorb_website_twin(website: Component, twin: Component, kind: str, *, table: str, source_id, user, result: ImportResult) -> None:
    twin = Component.objects.select_related("category", "brand").get(pk=twin.pk)
    spec = get_spec(website, kind)
    plan = {
        "component": {name: getattr(website, name) for name in WEBSITE_COMPONENT_FIELDS[kind]},
        "spec": {name: getattr(spec, name) for name in spec_fields(kind) if name != "family"} if spec is not None else {},
    }
    if website.brand_label and twin.brand_label != website.brand_label:
        result.violation(table, source_id, "brand_label_differs", f"{twin.sku}: printed as {twin.brand_label!r} in the catalog, {website.brand_label!r} on the website.", severity="warning")
    merged = _merge_into(twin, plan, table=table, source_id=source_id, result=result)
    profile = ComponentPublicProfile.all_objects.filter(component=website).first()
    if profile is not None:  # the one-to-one moves first: deleting the website component would delete a profile it still holds
        ComponentPublicProfile.all_objects.filter(pk=profile.pk).update(component=twin)
    component_services.delete_component(website, user=user)
    LegacyMap.objects.filter(target_table=COMPONENT_TABLE, target_id=website.pk).update(target_id=twin.pk)
    data = {**merged["component"], "is_public": True}
    if merged["spec"]:
        data[SPEC_RELATED[kind]] = merged["spec"]
    reason = f"legacy import: {table} (website product {website.sku} merged)"
    twin, _ = upsert_component(twin, data, user=user, status=twin.status, reason=reason)
    record_change(twin, user=user, field="merged", old=website.sku, new=twin.sku, reason=reason)
    result.violation(table, source_id, "website_twin_merged", f"{website.sku} (imported from the website first) merged into {twin.sku}.", severity="warning", sku=twin.sku)
    if profile is None:
        return
    profile = ComponentPublicProfile.objects.get(pk=profile.pk)
    created_at, updated_at, published_at = profile.created_at, profile.updated_at, profile.published_at
    problems = profile_services.publish_problems(twin)
    if problems:
        result.violation(table, source_id, "profile_not_published", f"{twin.sku}: {' '.join(problems)}", severity="warning")
        if profile.status == ProfileStatus.PUBLISHED:
            profile_services.unpublish(profile, user=user)
        published_at = None
    elif profile.status != ProfileStatus.PUBLISHED:
        profile_services.publish(profile, user=user)
        published_at = created_at
    # The website row's timestamps stay (as when the website import publishes after the catalog): plain UPDATE.
    ComponentPublicProfile.all_objects.filter(pk=profile.pk).update(published_at=published_at, created_at=created_at, updated_at=updated_at)
