"""``catalog/components/`` — the product master record, its tiers and its spec.

* new components start as DRAFT; status moves only through ``catalog.services.lifecycle`` (activate / deprecate /
  retire), never through an edit;
* the SKU is generated as ``<category sku_prefix>-0001`` when not given, is unique case-insensitively among live
  components, and is fixed once the component has left DRAFT (409 ``sku_locked``) — packs and quotations cite it;
* ``attributes`` must satisfy the category's JSON Schema; the spec matching the category's ``bom_role`` is written in
  the same transaction (``panel_spec`` / ``inverter_spec`` / ``battery_spec`` / ``structure_spec``);
* ``brand_label`` (the printed brand) follows the brand's name unless it is given explicitly;
* every change is versioned, written to ``catalog_component_change`` and ``audit_log``, bumps the ``catalog`` cache
  namespace and emits ``catalog.component_created|updated|deleted``;
* a component still referenced (``catalog.services.usage``) cannot be deleted (409 ``component_in_use``).
"""

from __future__ import annotations

import re

from django.db import IntegrityError, transaction
from django.db.models import Prefetch

from audit.services import changes, record, snapshot
from catalog.models import Category, Component, ComponentStatus, ComponentTier, ProfileStatus, Tier
from catalog.services import profiles as profile_services
from catalog.services import usage
from catalog.services.categories import validate_attributes
from catalog.services.common import CACHE_NAMESPACE, DOCUMENT_RULE, IMAGE_RULE, check_assets, validation_error
from catalog.services.history import record_change, record_changes
from catalog.services.specs import REQUIRED_FOR_ACTIVE, SPEC_RELATED, apply_spec, get_spec, spec_kind
from core.errors import Conflict
from core.outbox import emit
from core.services import check_version, stamp_create
from flarize.cache_utils import bump

COMPONENT_FIELDS = (
    "sku",
    "category",
    "brand",
    "brand_label",
    "name",
    "model",
    "description",
    "attributes",
    "gst_rate_override",
    "hsn_code_override",
    "unit_override",
    "is_public",
    "is_premium",
    "warranty_product_years",
    "warranty_performance_years",
    "warranty_extendable_years",
    "warranty_text",
    "engineering_status",
    "notes",
    "datasheet",
    "datasheet_url",
    "primary_image",
)
SNAPSHOT_FIELDS = (*COMPONENT_FIELDS, "status", "deprecated_reason", "retired_reason", "replacement")
ASSET_RULES = {"datasheet": DOCUMENT_RULE, "primary_image": IMAGE_RULE}
SPEC_KEYS = tuple(SPEC_RELATED.values())
TIER_ORDER = {Tier.BASE: 0, Tier.VALUE: 1, Tier.PREMIUM: 2}
# Statuses in which a panel/inverter must carry its spec (activate/ enforces it; so does a category move).
LIVE_STATUSES = (ComponentStatus.ACTIVE, ComponentStatus.DEPRECATED)
SKU_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")


def components_queryset():
    return Component.objects.select_related(
        "category", "brand", "replacement", "datasheet", "primary_image", "panel_spec", "inverter_spec", "battery_spec__family", "structure_spec", "public_profile"
    ).prefetch_related(Prefetch("tiers", queryset=ComponentTier.objects.order_by("tier")))


def sorted_tiers(tiers) -> list[str]:
    return sorted(set(tiers), key=lambda tier: TIER_ORDER.get(tier, 99))


def live_tiers(component: Component) -> list[str]:
    return sorted_tiers(row.tier for row in component.tiers.all())


def component_snapshot(component: Component) -> dict:
    return snapshot(component, SNAPSHOT_FIELDS)


def get_by_sku(sku: str) -> Component | None:
    return Component.objects.filter(sku__iexact=(sku or "").strip()).first()


def generate_sku(category: Category) -> str:
    """Next free ``<PREFIX>-<nnnn>`` for the category. The category row is locked, so concurrent creates serialise."""
    Category.objects.select_for_update().filter(pk=category.pk).first()
    prefix = category.sku_prefix
    pattern = rf"^{re.escape(prefix)}-([0-9]+)$"
    numbers = [int(sku.rsplit("-", 1)[1]) for sku in Component.all_objects.filter(sku__iregex=pattern).values_list("sku", flat=True)]
    return f"{prefix}-{max(numbers, default=0) + 1:04d}"


def _sku_conflict() -> Conflict:
    return Conflict("sku_taken", "Another component already uses this SKU.", errors={"sku": ["Already in use."]})


def _check_sku(sku: str, *, exclude_pk=None) -> str:
    sku = (sku or "").strip()
    if not SKU_RE.match(sku):
        raise validation_error({"sku": ["1-32 letters, digits, '.', '_' or '-', starting with a letter or digit."]})
    if Component.objects.filter(sku__iexact=sku).exclude(pk=exclude_pk).exists():
        raise _sku_conflict()
    return sku


def _check_spec_keys(category: Category, data) -> tuple[str | None, dict | None]:
    kind = spec_kind(category)
    given = {key: data[key] for key in SPEC_KEYS if data.get(key) is not None}
    wrong = {key: ["This category has no such spec."] for key in given if key != SPEC_RELATED.get(kind)}
    if wrong:
        raise validation_error(wrong, f"Components of category {category.slug!r} carry {'a ' + kind + ' spec' if kind else 'no spec'}.")
    return kind, (given.get(SPEC_RELATED[kind]) if kind else None)


def _check_category(category: Category) -> None:
    if category.deleted_at is not None or not category.is_active:
        raise validation_error({"category": ["The category is not active."]})


def set_tiers(component: Component, tiers, *, user) -> tuple[list[str], list[str]]:
    """Make the live tier rows equal ``tiers`` (restoring earlier rows, never duplicating); returns (before, after)."""
    wanted = set(tiers)
    unknown = sorted(wanted - set(Tier.values))
    if unknown:
        raise validation_error({"tiers": [f"Unknown tier: {', '.join(unknown)}."]})
    rows = {row.tier: row for row in ComponentTier.all_objects.filter(component=component).order_by("id")}
    before = sorted_tiers(tier for tier, row in rows.items() if row.deleted_at is None)
    for tier in wanted:
        row = rows.get(tier)
        if row is None:
            row = ComponentTier(component=component, tier=tier)
            stamp_create(row, user)
            row.save()
        elif row.deleted_at is not None:
            row.restore(user)
    for tier, row in rows.items():
        if tier not in wanted and row.deleted_at is None:
            row.soft_delete(user)
    return before, sorted_tiers(wanted)


def _emit(event: str, component: Component, **extra) -> None:
    emit(event, {"component_uid": str(component.uid), "sku": component.sku, **extra}, aggregate_type="catalog.component", aggregate_uid=component.uid)


@transaction.atomic
def create_component(*, user, data, initial_status: str = ComponentStatus.DRAFT, reason: str = "") -> Component:
    """Create a component (DRAFT unless an importer passes ``initial_status``)."""
    category = data["category"]
    _check_category(category)
    kind, spec_values = _check_spec_keys(category, data)
    values = {name: data[name] for name in COMPONENT_FIELDS if name in data and name != "sku"}
    values["sku"] = _check_sku(data["sku"]) if data.get("sku") else generate_sku(category)
    if values.get("brand") is not None and not values.get("brand_label"):
        values["brand_label"] = values["brand"].name
    values.setdefault("attributes", {})
    validate_attributes(category, values["attributes"])
    check_assets(values, ASSET_RULES)
    component = Component(**values, status=initial_status)
    stamp_create(component, user)
    try:
        with transaction.atomic():
            component.save()
    except IntegrityError:
        raise _sku_conflict() from None
    tiers = set_tiers(component, data.get("tiers") or [], user=user)[1]
    spec_after = apply_spec(component, kind, spec_values, user=user)[1] if spec_values is not None else {}
    after = {**component_snapshot(component), "tiers": tiers, "spec": spec_after}
    record_change(component, user=user, field="created", new=after, reason=reason)
    record("catalog.component_created", obj=component, actor=user, after=after, note=reason)
    bump(CACHE_NAMESPACE)
    _emit("catalog.component_created", component, category=category.slug)
    return component


def _changed_values(component: Component, data) -> dict:
    values = {}
    for name in COMPONENT_FIELDS:
        if name in data and getattr(component, name) != data[name]:
            values[name] = data[name]
    return values


@transaction.atomic
def update_component(instance: Component, *, user, data, expected_version=None, reason: str = "") -> Component:
    component = Component.objects.select_for_update(of=("self",)).select_related("category", "brand").get(pk=instance.pk)
    check_version(component, expected_version)
    values = _changed_values(component, data)
    category = values.get("category", component.category)
    if "category" in values:
        _check_category(category)
        old_kind, new_kind = spec_kind(component.category), spec_kind(category)
        if old_kind != new_kind and get_spec(component, old_kind) is not None:
            raise Conflict("spec_kind_change", f"The component carries a {old_kind} spec; move it to a category with the same spec table.", errors={"category": ["Spec table would change."]})
    kind, spec_values = _check_spec_keys(category, data)
    if "category" in values and component.status in LIVE_STATUSES and kind in REQUIRED_FOR_ACTIVE and spec_values is None and get_spec(component, kind) is None:
        # activate/ refuses a panel/inverter without its spec; a category move must not bypass that rule.
        raise Conflict("spec_required", f"A {component.status} component needs the {kind} spec in this category; send it with the move.", errors={f"{kind}_spec": ["Required."]})
    if "sku" in values:
        if component.status != ComponentStatus.DRAFT:
            raise Conflict("sku_locked", "The SKU is fixed once the component has left DRAFT.", errors={"sku": ["Cannot be changed."]})
        values["sku"] = _check_sku(values["sku"], exclude_pk=component.pk)
    if "brand" in values and "brand_label" not in data:
        old_brand_name = component.brand.name if component.brand else ""
        if component.brand_label in ("", old_brand_name):
            values["brand_label"] = values["brand"].name if values["brand"] else ""
    if "attributes" in values or "category" in values:
        validate_attributes(category, values.get("attributes", component.attributes))
    check_assets(values, ASSET_RULES)
    before = component_snapshot(component)
    tiers_before = live_tiers(component)
    try:
        with transaction.atomic():
            if values:
                component.versioned_update(user, **values)
    except IntegrityError:
        raise _sku_conflict() from None
    tiers_after = set_tiers(component, data["tiers"], user=user)[1] if "tiers" in data and data["tiers"] is not None else tiers_before
    spec_before, spec_after = apply_spec(component, kind, spec_values, user=user) if spec_values is not None else ({}, {})
    nested_changed = tiers_after != tiers_before or spec_before != spec_after
    if nested_changed and not values:
        component.versioned_update(user)  # the aggregate changed: bump its version
    if not values and not nested_changed:
        return component
    after = component_snapshot(component)
    changed = record_changes(component, user=user, before=before, after=after, reason=reason)
    if tiers_after != tiers_before:
        record_change(component, user=user, field="tiers", old=tiers_before, new=tiers_after, reason=reason)
    changed += [f"spec.{name}" for name in record_changes(component, user=user, before=spec_before, after=spec_after, reason=reason, prefix="spec.")]
    audit_before, audit_after = changes(before, after)
    if tiers_after != tiers_before:
        audit_before["tiers"], audit_after["tiers"] = tiers_before, tiers_after
        changed.append("tiers")
    if spec_before != spec_after:
        audit_before["spec"], audit_after["spec"] = changes(spec_before, spec_after)
    record("catalog.component_updated", obj=component, actor=user, before=audit_before, after=audit_after, note=reason)
    bump(CACHE_NAMESPACE)
    _emit("catalog.component_updated", component, fields=sorted(set(changed)))
    return component


@transaction.atomic
def delete_component(instance: Component, *, user, expected_version=None) -> None:
    component = Component.objects.select_for_update().get(pk=instance.pk)
    check_version(component, expected_version)
    usage.ensure_not_in_use(component)
    profile = getattr(component, "public_profile", None)  # reverse one-to-one: None when there is no profile
    if profile is not None and profile.deleted_at is None:
        was_published = profile.status == ProfileStatus.PUBLISHED
        profile.soft_delete(user)
        if was_published:  # the website is told, exactly as when the profile itself is deleted
            profile_services.emit_profile_event("catalog.profile_unpublished", profile)
    before = component_snapshot(component)
    component.soft_delete(user)
    record_change(component, user=user, field="deleted", old={"sku": component.sku, "status": component.status})
    record("catalog.component_deleted", obj=component, actor=user, before=before)
    bump(CACHE_NAMESPACE)
    _emit("catalog.component_deleted", component)
