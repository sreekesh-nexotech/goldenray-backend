"""Writes of the bom configuration tables (PLAN §3.4 ``bom/templates|slots|fixed-items|structure-templates|
structure-items|tube-weights/`` CRUD, plus ``bom/package-profiles/``).

Every write is one transaction: validate (documents against ``bom.schemas``, cross-field rules, selectable catalog
references), stamp, compare-and-swap on ``version`` (``expected_version`` → 409 ``stale_version``), audit
(``bom.<thing>_created|updated|deleted``), bump the ``bom`` cache namespace and emit ``bom.configuration_changed``.
Deletes are soft; deleting a template or a structure template deletes its children with it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from django.db import IntegrityError, transaction

from audit.services import changes, record, snapshot
from bom.models import FixedItem, PackageProfile, Slot, StructureItemType, StructureTemplate, StructureTemplateItem, Template, TubeWeight
from bom.services.common import changed, check_document, validation_error
from catalog.services import assert_selectable
from core.errors import Conflict
from core.services import check_version, stamp_create


@dataclass(frozen=True)
class Spec:
    model: type
    name: str  # audit/event noun: template, slot, fixed_item …
    fields: tuple[str, ...]
    conflict: Callable[[], Conflict] | None = None
    validate: Callable[[dict, object | None], None] | None = None
    children: tuple[str, ...] = ()


def _values(spec: Spec, data: dict) -> dict:
    return {name: data[name] for name in spec.fields if name in data}


def _audit_snapshot(spec: Spec, instance) -> dict:
    return snapshot(instance, spec.fields)


def create(spec: Spec, *, user, data: dict):
    values = _values(spec, data)
    if spec.validate:
        spec.validate(values, None)
    instance = spec.model(**values)
    stamp_create(instance, user)
    try:
        with transaction.atomic():
            instance.save()
    except IntegrityError:
        if spec.conflict is None:
            raise
        raise spec.conflict() from None
    record(f"bom.{spec.name}_created", obj=instance, actor=user, after=_audit_snapshot(spec, instance))
    changed(instance, "created")
    return instance


def update(spec: Spec, instance, *, user, data: dict, expected_version=None):
    row = spec.model.objects.select_for_update().get(pk=instance.pk)
    check_version(row, expected_version)
    values = _values(spec, data)
    if spec.validate:
        spec.validate(values, row)
    values = {name: value for name, value in values.items() if getattr(row, name) != value}
    if not values:
        return row
    before = _audit_snapshot(spec, row)
    try:
        with transaction.atomic():
            row.versioned_update(user, **values)
    except IntegrityError:
        if spec.conflict is None:
            raise
        raise spec.conflict() from None
    changed_before, changed_after = changes(before, _audit_snapshot(spec, row))
    record(f"bom.{spec.name}_updated", obj=row, actor=user, before=changed_before, after=changed_after)
    changed(row, "updated")
    return row


def delete(spec: Spec, instance, *, user, expected_version=None) -> None:
    row = spec.model.objects.select_for_update().get(pk=instance.pk)
    check_version(row, expected_version)
    for relation in spec.children:
        for child in getattr(row, relation).all():
            child.soft_delete(user)
    row.soft_delete(user)
    record(f"bom.{spec.name}_deleted", obj=row, actor=user, before=_audit_snapshot(spec, row))
    changed(row, "deleted")


# ── validation rules ─────────────────────────────────────────────────────────────────────────────────────────


def _merged(values: dict, instance, name: str):
    if name in values:
        return values[name]
    return getattr(instance, name) if instance is not None else None


def _validate_template(values: dict, instance) -> None:
    for field, schema in (("sizes", "SIZES_SCHEMA"), ("three_phase_sizes", "KEY_LIST_SCHEMA"), ("tiers", "TIERS_SCHEMA"), ("battery_configs", "BANDS_SCHEMA")):
        if field in values:
            check_document(values[field], schema, field)
    sizes = _merged(values, instance, "sizes") or []
    keys = [entry["key"] for entry in sizes]
    if len(set(keys)) != len(keys):
        raise validation_error({"sizes": ["Size keys must be unique."]})
    unknown = [key for key in (_merged(values, instance, "three_phase_sizes") or []) if key not in keys]
    if unknown:
        raise validation_error({"three_phase_sizes": [f"Not an offered size: {', '.join(unknown)}."]})


def _check_category(category, field: str = "category") -> None:
    if category is not None and (category.deleted_at is not None or not category.is_active):
        raise validation_error({field: ["The category is deleted or inactive."]})


def _check_template_live(template, field: str = "template") -> None:
    if template is not None and template.deleted_at is not None:
        raise validation_error({field: ["The template has been deleted."]})


def _validate_slot(values: dict, instance) -> None:
    if "qty_rule" in values:
        check_document(values["qty_rule"], "QTY_RULE_SCHEMA", "qty_rule")
    _check_template_live(values.get("template"))
    if "category" in values:
        _check_category(values["category"])


def _validate_fixed_item(values: dict, instance) -> None:
    if values.get("qty_rule") is not None:
        check_document(values["qty_rule"], "QTY_RULE_SCHEMA", "qty_rule")
    if "condition" in values:
        check_document(values["condition"] or {}, "CONDITION_SCHEMA", "condition")
    _check_template_live(values.get("template"))
    if values.get("component") is not None and (instance is None or values["component"] != instance.component):
        assert_selectable(values["component"], field="component")
    if "category" in values:
        _check_category(values["category"])
    if _merged(values, instance, "qty") is None and _merged(values, instance, "qty_rule") is None:
        raise validation_error({"qty": ["Give a constant qty or a qty_rule."]})
    if _merged(values, instance, "unit_price") is None and _merged(values, instance, "component") is None:
        raise validation_error({"unit_price": ["Give a unit_price or a component (its current LIST price)."]})


def _validate_structure_item(values: dict, instance) -> None:
    if "qty_rule" in values:
        check_document(values["qty_rule"], "QTY_RULE_SCHEMA", "qty_rule")
    _check_template_live(values.get("template"))
    kind = _merged(values, instance, "item_type")
    if kind == StructureItemType.TUBE and _merged(values, instance, "weight_kg") is None:
        raise validation_error({"weight_kg": ["A tube is priced by weight: give weight_kg."]})
    if kind == StructureItemType.FIXED and _merged(values, instance, "unit_price") is None:
        raise validation_error({"unit_price": ["A fixed structure item needs a unit_price."]})


def _validate_profile(values: dict, instance) -> None:
    component = values.get("battery_component")
    if component is not None and (instance is None or component != instance.battery_component):
        assert_selectable(component, field="battery_component")


# ── the resources ────────────────────────────────────────────────────────────────────────────────────────────

TEMPLATE = Spec(
    Template,
    "template",
    ("system_type", "name", "description", "is_active", "sizes", "three_phase_sizes", "tiers", "battery_configs"),
    lambda: Conflict("template_exists", "A template for this system type already exists.", errors={"system_type": ["Already has a template."]}),
    _validate_template,
    children=("slots", "fixed_items"),
)
SLOT = Spec(
    Slot,
    "slot",
    ("template", "key", "category", "qty_rule", "required", "sort_order", "label", "gst_rate", "is_variable", "filter_type", "filter_phase"),
    lambda: Conflict("slot_key_taken", "The template already has a slot with this key.", errors={"key": ["Already used in this template."]}),
    _validate_slot,
)
FIXED_ITEM = Spec(
    FixedItem,
    "fixed_item",
    ("template", "component", "category", "code", "name", "unit_price", "gst_rate", "qty", "qty_rule", "condition", "section", "unit", "is_tube", "sort_order"),
    None,
    _validate_fixed_item,
)
STRUCTURE_TEMPLATE = Spec(
    StructureTemplate,
    "structure_template",
    ("slug", "name", "labour_rate_key"),
    lambda: Conflict("structure_template_exists", "A structure template with this slug already exists.", errors={"slug": ["Already used."]}),
    None,
    children=("items",),
)
STRUCTURE_ITEM = Spec(
    StructureTemplateItem,
    "structure_item",
    ("template", "name", "item_type", "tube_size", "weight_kg", "unit_price", "unit", "length_m_per_kw", "qty_rule", "sort_order"),
    None,
    _validate_structure_item,
)
TUBE_WEIGHT = Spec(
    TubeWeight,
    "tube_weight",
    ("tube_size", "weight_kg"),
    lambda: Conflict("tube_weight_exists", "This tube size already has a weight.", errors={"tube_size": ["Already configured."]}),
)
PACKAGE_PROFILE = Spec(
    PackageProfile,
    "package_profile",
    (
        "key",
        "label",
        "structure_material",
        "inverter_type",
        "battery_included",
        "battery_brand",
        "battery_model",
        "battery_capacity_kwh",
        "battery_quantity",
        "battery_component",
        "structure_labor_override",
        "repair_margin_override",
        "notes",
    ),
    lambda: Conflict("package_profile_exists", "A package profile with this key already exists.", errors={"key": ["Already used."]}),
    _validate_profile,
)


def _bind(spec: Spec):
    @transaction.atomic
    def create_fn(*, user, data: dict):
        return create(spec, user=user, data=data)

    @transaction.atomic
    def update_fn(instance, *, user, data: dict, expected_version=None):
        return update(spec, instance, user=user, data=data, expected_version=expected_version)

    @transaction.atomic
    def delete_fn(instance, *, user, expected_version=None) -> None:
        delete(spec, instance, user=user, expected_version=expected_version)

    return create_fn, update_fn, delete_fn


create_template, update_template, delete_template = _bind(TEMPLATE)
create_slot, update_slot, delete_slot = _bind(SLOT)
create_fixed_item, update_fixed_item, delete_fixed_item = _bind(FIXED_ITEM)
create_structure_template, update_structure_template, delete_structure_template = _bind(STRUCTURE_TEMPLATE)
create_structure_item, update_structure_item, delete_structure_item = _bind(STRUCTURE_ITEM)
create_tube_weight, update_tube_weight, delete_tube_weight = _bind(TUBE_WEIGHT)
create_package_profile, update_package_profile, delete_package_profile = _bind(PACKAGE_PROFILE)
