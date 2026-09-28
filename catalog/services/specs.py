"""Which spec table a component carries, and writing it.

The category's ``bom_role`` decides the spec kind: MAIN_PANEL → ``panel`` (``catalog_panel_spec``), MAIN_INVERTER →
``inverter``, BATTERY → ``battery``, STRUCTURE → ``structure``; every other role has no spec table (its technical
data lives in ``component.attributes``, validated by the category's JSON Schema).
"""

from __future__ import annotations

from collections.abc import Mapping

from django.db import models

from audit.services import snapshot
from catalog.models import BatteryFamily, BatterySpec, BomRole, Category, Component, InverterSpec, PanelSpec, StructureSpec
from catalog.services.common import validation_error
from core.services import stamp_create

SPEC_MODELS: dict[str, type[models.Model]] = {"panel": PanelSpec, "inverter": InverterSpec, "battery": BatterySpec, "structure": StructureSpec}
ROLE_KINDS = {BomRole.MAIN_PANEL: "panel", BomRole.MAIN_INVERTER: "inverter", BomRole.BATTERY: "battery", BomRole.STRUCTURE: "structure"}
SPEC_RELATED = {"panel": "panel_spec", "inverter": "inverter_spec", "battery": "battery_spec", "structure": "structure_spec"}
REQUIRED_ON_CREATE = {"panel": ("wattage_w",), "inverter": ("kw", "inverter_type"), "battery": (), "structure": ()}
# A component of these kinds cannot be activated without its spec (the BOM builder sizes by wattage / kW).
REQUIRED_FOR_ACTIVE = ("panel", "inverter")
_BASE_COLUMNS = {"id", "uid", "created_at", "updated_at", "created_by", "updated_by", "deleted_at", "version", "component"}


def spec_kind(category: Category | None) -> str | None:
    return ROLE_KINDS.get(category.bom_role) if category is not None else None


def spec_fields(kind: str) -> list[str]:
    model = SPEC_MODELS[kind]
    return [field.name for field in model._meta.concrete_fields if field.name not in _BASE_COLUMNS]


def get_spec(component: Component, kind: str | None = None):
    kind = kind or spec_kind(component.category)
    if kind is None:
        return None
    try:
        return getattr(component, SPEC_RELATED[kind])
    except SPEC_MODELS[kind].DoesNotExist:
        return None


def spec_snapshot(spec) -> dict:
    if spec is None:
        return {}
    kind = next(name for name, model in SPEC_MODELS.items() if isinstance(spec, model))
    return snapshot(spec, spec_fields(kind))


def _check_families(values: Mapping) -> None:
    slugs = values.get("compatible_battery_families")
    if not slugs:
        return
    known = set(BatteryFamily.objects.filter(slug__in=slugs).values_list("slug", flat=True))
    unknown = [slug for slug in slugs if slug not in known]
    if unknown:
        raise validation_error({"compatible_battery_families": [f"Unknown battery family: {', '.join(unknown)}."]})


def apply_spec(component: Component, kind: str, values: Mapping, *, user, field_name: str | None = None) -> tuple[dict, dict]:
    """Create or update ``component``'s ``kind`` spec with ``values``; returns ``(before, after)`` snapshots."""
    field_name = field_name or SPEC_RELATED[kind]
    allowed = set(spec_fields(kind))
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise validation_error({field_name: {name: ["Not a field of this spec."] for name in unknown}})
    if kind == "inverter":
        _check_families(values)
    if kind == "battery" and values.get("family") is not None and values["family"].deleted_at is not None:
        raise validation_error({field_name: {"family": ["The battery family has been deleted."]}})
    spec = get_spec(component, kind)
    if spec is None:
        missing = [name for name in REQUIRED_ON_CREATE[kind] if values.get(name) in (None, "")]
        if missing:
            raise validation_error({field_name: {name: ["This field is required."] for name in missing}})
        spec = SPEC_MODELS[kind](component=component, **dict(values))
        stamp_create(spec, user)
        spec.save()
        setattr(component, SPEC_RELATED[kind], spec)
        return {}, spec_snapshot(spec)
    before = spec_snapshot(spec)
    changed = {name: value for name, value in values.items() if getattr(spec, name) != value}
    for required in REQUIRED_ON_CREATE[kind]:
        if required in changed and changed[required] in (None, ""):
            raise validation_error({field_name: {required: ["This field may not be null."]}})
    if changed:
        spec.versioned_update(user, **changed)
    return before, spec_snapshot(spec)
