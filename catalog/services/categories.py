"""``catalog/categories/`` — tax, unit, BOM role and the attribute JSON Schema of each component category.

* ``attribute_schema`` must be a valid JSON Schema (draft 2020-12) whose root is ``{"type": "object", …}``; changing
  it re-validates every live component of the category (409 ``attribute_schema_conflict`` listing the SKUs);
* changing ``bom_role`` to a role with another spec table is refused while components carry specs
  (409 ``spec_kind_change``), and to the panel/inverter role while ACTIVE/DEPRECATED components lack that spec
  (409 ``spec_required``, as ``activate/``);
* a category with live components cannot be deleted (409 ``category_in_use``).
"""

from __future__ import annotations

import re

from django.db import IntegrityError, transaction
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from audit.services import changes, record, snapshot
from catalog.models import Category, Component, ComponentStatus
from catalog.services.common import CACHE_NAMESPACE, validation_error
from catalog.services.specs import REQUIRED_FOR_ACTIVE, SPEC_RELATED, spec_kind
from core.errors import Conflict
from core.services import check_version, stamp_create
from flarize.cache_utils import bump

SNAPSHOT_FIELDS = ("slug", "name", "bom_role", "gst_rate", "hsn_code", "unit", "attribute_schema", "sku_prefix", "sort_order", "is_active")
EDITABLE_FIELDS = SNAPSHOT_FIELDS
MAX_REPORTED = 20
SKU_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")


def categories_queryset():
    return Category.objects.order_by("sort_order", "name", "id")


def check_attribute_schema(schema) -> None:
    if not isinstance(schema, dict):
        raise validation_error({"attribute_schema": ["Must be a JSON object (a JSON Schema)."]})
    if not schema:
        return
    if schema.get("type") != "object":
        raise validation_error({"attribute_schema": ['The root of the schema must be {"type": "object", ...}.']})
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise validation_error({"attribute_schema": [f"Not a valid JSON Schema: {exc.message}"]}) from None


def attribute_errors(schema: dict, attributes) -> list[str]:
    """Validation messages for ``attributes`` under ``schema`` (empty schema: any JSON object)."""
    if not isinstance(attributes, dict):
        return ["Must be a JSON object."]
    if not schema:
        return []
    validator = Draft202012Validator(schema)
    messages = []
    for error in sorted(validator.iter_errors(attributes), key=lambda e: list(e.absolute_path)):
        path = "/".join(str(part) for part in error.absolute_path)
        messages.append(f"{path}: {error.message}" if path else error.message)
    return messages


def validate_attributes(category: Category, attributes) -> None:
    messages = attribute_errors(category.attribute_schema or {}, attributes)
    if messages:
        raise validation_error({"attributes": messages}, "The attributes do not match the category's schema.")


def _integrity_conflict(exc: IntegrityError) -> Conflict:
    if "sku_prefix" in str(exc):
        return Conflict("category_sku_prefix_taken", "Another category already uses this SKU prefix.", errors={"sku_prefix": ["Already in use."]})
    return Conflict("category_slug_taken", "Another category already uses this slug.", errors={"slug": ["Already in use."]})


def _clean(values: dict) -> dict:
    if "sku_prefix" in values:
        values["sku_prefix"] = (values["sku_prefix"] or "").upper()
        if not SKU_PREFIX_RE.match(values["sku_prefix"]):
            raise validation_error({"sku_prefix": ["2-8 letters or digits, starting with a letter (e.g. PNL)."]})
    if "gst_rate" in values and values["gst_rate"] is not None and not (0 <= values["gst_rate"] <= 1):
        raise validation_error({"gst_rate": ["A fraction between 0 and 1 (0.18 = 18 %)."]})
    if "attribute_schema" in values:
        check_attribute_schema(values["attribute_schema"])
    return values


@transaction.atomic
def create_category(*, user, data) -> Category:
    values = _clean({name: data[name] for name in EDITABLE_FIELDS if name in data})
    category = Category(**values)
    stamp_create(category, user)
    try:
        with transaction.atomic():
            category.save()
    except IntegrityError as exc:
        raise _integrity_conflict(exc) from None
    record("catalog.category_created", obj=category, actor=user, after=snapshot(category, SNAPSHOT_FIELDS))
    bump(CACHE_NAMESPACE)
    return category


def _check_schema_against_components(category: Category, schema: dict) -> None:
    conflicts = {}
    for sku, attributes in Component.objects.filter(category=category).values_list("sku", "attributes").iterator():
        messages = attribute_errors(schema, attributes)
        if messages:
            conflicts[sku] = messages[:3]
            if len(conflicts) >= MAX_REPORTED:
                break
    if conflicts:
        raise Conflict("attribute_schema_conflict", "Live components of this category do not satisfy the new schema.", errors=conflicts)


def _check_role_change(category: Category, new_role: str) -> None:
    old_kind = spec_kind(category)
    new_kind = spec_kind(Category(bom_role=new_role))
    if old_kind == new_kind:
        return
    if old_kind is not None:
        with_spec = Component.objects.filter(category=category, **{f"{SPEC_RELATED[old_kind]}__isnull": False}).count()
        if with_spec:
            raise Conflict("spec_kind_change", f"{with_spec} components carry a {old_kind} spec; the new role uses another spec table.", errors={"bom_role": ["Spec table would change."]})
    if new_kind in REQUIRED_FOR_ACTIVE:
        # activate/ refuses a panel/inverter without its spec: live components must not become such items spec-less.
        missing = list(
            Component.objects.filter(category=category, status__in=(ComponentStatus.ACTIVE, ComponentStatus.DEPRECATED), **{f"{SPEC_RELATED[new_kind]}__isnull": True})
            .order_by("sku")
            .values_list("sku", flat=True)[:MAX_REPORTED]
        )
        if missing:
            raise Conflict("spec_required", f"Active components of this category have no {new_kind} spec; add it or move them first.", errors={sku: [f"No {new_kind} spec."] for sku in missing})


@transaction.atomic
def update_category(instance: Category, *, user, data, expected_version=None) -> Category:
    category = Category.objects.select_for_update().get(pk=instance.pk)
    check_version(category, expected_version)
    values = _clean({name: data[name] for name in EDITABLE_FIELDS if name in data})
    values = {name: value for name, value in values.items() if getattr(category, name) != value}
    if not values:
        return category
    if "attribute_schema" in values:
        _check_schema_against_components(category, values["attribute_schema"])
    if "bom_role" in values:
        _check_role_change(category, values["bom_role"])
    before = snapshot(category, SNAPSHOT_FIELDS)
    try:
        with transaction.atomic():
            category.versioned_update(user, **values)
    except IntegrityError as exc:
        raise _integrity_conflict(exc) from None
    changed_before, changed_after = changes(before, snapshot(category, SNAPSHOT_FIELDS))
    record("catalog.category_updated", obj=category, actor=user, before=changed_before, after=changed_after)
    bump(CACHE_NAMESPACE)
    return category


@transaction.atomic
def delete_category(instance: Category, *, user, expected_version=None) -> None:
    category = Category.objects.select_for_update().get(pk=instance.pk)
    check_version(category, expected_version)
    in_use = Component.objects.filter(category=category).count()
    if in_use:
        raise Conflict("category_in_use", f"{in_use} live components belong to this category.", errors={"components": [str(in_use)]})
    category.soft_delete(user)
    record("catalog.category_deleted", obj=category, actor=user, before=snapshot(category, SNAPSHOT_FIELDS))
    bump(CACHE_NAMESPACE)
