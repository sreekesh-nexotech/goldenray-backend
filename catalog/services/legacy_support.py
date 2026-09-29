"""Machinery shared by the catalog legacy importers (``catalog.services.legacy_import``).

* :class:`ImportResult` — the ``{"created","updated","skipped","unchanged","violations","prices","counts"}``
  report every importer returns (``violations`` carry a ``severity``: ``error`` rows were not imported,
  ``warning`` rows were imported and need a human look);
* ``core_legacy_map`` helpers (every imported row is traceable; re-runs update, never duplicate);
* value converters that never invent data (``None`` stays ``None``; floats go through ``str`` into ``Decimal``);
* category and brand provisioning with the documented defaults, attribute-schema extension and the component
  upsert (through ``catalog.services.components``) that preserves source timestamps.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from catalog.models import BomRole, Brand, Category, Component, ComponentChange, ComponentStatus, Unit
from catalog.services import brands as brand_services
from catalog.services import categories as category_services
from catalog.services import components as component_services
from catalog.services.categories import SKU_PREFIX_RE
from catalog.services.history import record_change
from core.models import LegacyMap, actor_or_none

BACKEND = LegacyMap.SourceSystem.BACKEND
FLARIZE = LegacyMap.SourceSystem.FLARIZE

# Category defaults for slugs the importers create. bom_role follows how the Flarize/BOM builder uses the category;
# unit M is the legacy BomCalculator rule (cables measured in metres); prefixes name platform-generated SKUs.
CATEGORY_DEFAULTS: dict[str, dict] = {
    "panel": {"bom_role": BomRole.MAIN_PANEL, "sku_prefix": "PNL"},
    "inverter": {"bom_role": BomRole.MAIN_INVERTER, "sku_prefix": "INV"},
    "battery": {"bom_role": BomRole.BATTERY, "sku_prefix": "BAT"},
    "dcdb": {"bom_role": BomRole.PROTECTION, "sku_prefix": "DCDB"},
    "acdb": {"bom_role": BomRole.PROTECTION, "sku_prefix": "ACDB"},
    "isolator": {"bom_role": BomRole.PROTECTION, "sku_prefix": "ISO"},
    "mccb_box": {"bom_role": BomRole.PROTECTION, "sku_prefix": "MCCB"},
    "change_over": {"bom_role": BomRole.PROTECTION, "sku_prefix": "CHO"},
    "meter": {"bom_role": BomRole.MISC, "sku_prefix": "MTR"},
    "meter_box": {"bom_role": BomRole.MISC, "sku_prefix": "MTB"},
    "dc_cable": {"bom_role": BomRole.CABLE, "sku_prefix": "DCC", "unit": Unit.M},
    "ac_cable": {"bom_role": BomRole.CABLE, "sku_prefix": "ACC", "unit": Unit.M},
    "armoured_cable": {"bom_role": BomRole.CABLE, "sku_prefix": "ARC", "unit": Unit.M},
    "ug_cable": {"bom_role": BomRole.CABLE, "sku_prefix": "UGC", "unit": Unit.M},
    "battery_cable": {"bom_role": BomRole.CABLE, "sku_prefix": "BTC"},
    "earth_cable": {"bom_role": BomRole.EARTHING, "sku_prefix": "ERC"},
    "la_cable": {"bom_role": BomRole.EARTHING, "sku_prefix": "LAC"},
    "cb_rod": {"bom_role": BomRole.EARTHING, "sku_prefix": "CBR"},
    "structure": {"bom_role": BomRole.STRUCTURE, "sku_prefix": "STR"},
    "structure_material": {"bom_role": BomRole.STRUCTURE, "sku_prefix": "STM"},
    "solar_clamp": {"bom_role": BomRole.STRUCTURE, "sku_prefix": "SCL"},
    "service": {"bom_role": BomRole.SERVICE, "sku_prefix": "SRV"},
    "enphase": {"bom_role": BomRole.MISC, "sku_prefix": "ENP"},
    "hoymiles": {"bom_role": BomRole.MISC, "sku_prefix": "HMS"},
    "mc4_connector": {"bom_role": BomRole.MISC, "sku_prefix": "MC4"},
    "cable_tie": {"bom_role": BomRole.MISC, "sku_prefix": "CTI"},
    "flexible_pipe": {"bom_role": BomRole.MISC, "sku_prefix": "FXP"},
    "ss_screw": {"bom_role": BomRole.MISC, "sku_prefix": "SSS"},
    "copper_lug": {"bom_role": BomRole.MISC, "sku_prefix": "CPL"},
    "insulation_tape": {"bom_role": BomRole.MISC, "sku_prefix": "IST"},
    "conduit_pipe": {"bom_role": BomRole.MISC, "sku_prefix": "CDP"},
    "painting_material": {"bom_role": BomRole.MISC, "sku_prefix": "PNT"},
    "welding_material": {"bom_role": BomRole.MISC, "sku_prefix": "WLD"},
    "other": {"bom_role": BomRole.MISC, "sku_prefix": "OTH"},
}
# Used only when the website products are imported before any BOM/Flarize category exists (later imports win).
WEBSITE_CATEGORY_FALLBACK = {"panel": ("Solar Panel", Decimal("0.05")), "inverter": ("Inverter", Decimal("0.05")), "battery": ("Battery", Decimal("0.18"))}


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    violations: list[dict] = field(default_factory=list)
    prices: list[dict] = field(default_factory=list)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)

    def count(self, table: str, outcome: str) -> None:
        bucket = self.counts.setdefault(table, {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0})
        bucket[outcome] += 1
        setattr(self, outcome, getattr(self, outcome) + 1)

    def violation(self, table: str, source_id, code: str, message: str, *, severity: str = "error", **context) -> None:
        self.violations.append({"source_table": table, "source_id": str(source_id), "code": code, "severity": severity, "message": message, **json_ready(context)})

    def as_dict(self) -> dict:
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "violations": self.violations,
            "prices": self.prices,
            "counts": self.counts,
        }


class DryRunRollback(Exception):
    pass


def plain(value):
    """JSON-native copy of a source value for ``attributes`` (Decimals become int/float, never strings)."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def json_ready(value):
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def checksum(*payloads) -> str:
    return hashlib.sha256(json.dumps(payloads, cls=DjangoJSONEncoder, sort_keys=True).encode()).hexdigest()


# ── core_legacy_map ─────────────────────────────────────────────────────────────────────────────────────────────


def mapped_target(source_system: str, source_table: str, source_id, model):
    """``(row, found_map)``: the platform row a source row was imported into (soft-deleted rows included)."""
    entry = LegacyMap.objects.filter(source_system=source_system, source_table=source_table, source_id=str(source_id)).first()
    if entry is None:
        return None, False
    return model.all_objects.filter(pk=entry.target_id).first(), True


def remember(source_system: str, source_table: str, source_id, target) -> None:
    LegacyMap.objects.update_or_create(
        source_system=source_system,
        source_table=source_table,
        source_id=str(source_id),
        defaults={"target_table": target._meta.db_table, "target_id": target.pk},
    )


def mapped_ids(source_system: str, source_table: str, target_table: str) -> set[int]:
    return set(LegacyMap.objects.filter(source_system=source_system, source_table=source_table, target_table=target_table).values_list("target_id", flat=True))


def legacy_user(source_system: str, source_table: str, source_id):
    """The platform user an earlier import created for a source user id (``None`` when not imported)."""
    from django.contrib.auth import get_user_model

    if not source_id:
        return None
    entry = LegacyMap.objects.filter(source_system=source_system, source_table=source_table, source_id=str(source_id)).first()
    return get_user_model().all_objects.filter(pk=entry.target_id).first() if entry else None


# ── values ─────────────────────────────────────────────────────────────────────────────────────────────────────


class BadValue(ValueError):
    def __init__(self, column: str, value, reason: str):
        super().__init__(f"{column}={value!r}: {reason}")
        self.column, self.value, self.reason = column, value, reason


def dec(value, column: str, *, places: int, digits: int) -> Decimal | None:
    """``value`` as a Decimal fitting ``numeric(digits, places)``; raises :class:`BadValue` if it would lose data."""
    if value is None or value == "":
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BadValue(column, value, "not a number") from None
    quantum = Decimal(1).scaleb(-places)
    rounded = number.quantize(quantum)
    if rounded != number:
        raise BadValue(column, value, f"more than {places} decimal places")
    if abs(rounded) >= Decimal(10) ** (digits - places):
        raise BadValue(column, value, f"does not fit numeric({digits},{places})")
    return rounded


def integer(value, column: str, *, minimum: int | None = 0, maximum: int = 2_147_483_647) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BadValue(column, value, "not an integer")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BadValue(column, value, "not an integer") from None
    if number != number.to_integral_value():
        raise BadValue(column, value, "not a whole number")
    result = int(number)
    if (minimum is not None and result < minimum) or result > maximum:
        raise BadValue(column, value, "out of range")
    return result


def boolean(value, column: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise BadValue(column, value, "not a boolean")


def text(value, column: str, *, max_length: int) -> str:
    result = "" if value is None else str(value)
    if len(result) > max_length:
        raise BadValue(column, value, f"longer than {max_length} characters")
    return result


def fraction_from_percent(value, column: str) -> Decimal | None:
    """GST as the sources store it (``18``) → the platform's fraction (``0.1800``)."""
    number = dec(value, column, places=2, digits=5)
    return None if number is None else (number / 100).quantize(Decimal("0.0001"))


def moment(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        result = value
    else:
        result = parse_datetime(str(value))
        if result is None:
            return None
    return result if timezone.is_aware(result) else timezone.make_aware(result, timezone.get_current_timezone())


_MODEL_JUNK = re.compile(r"[^0-9a-z]+")


def model_key(value: str | None) -> str:
    """Matching key of a model designation: case-folded, letters and digits only (``"SG 5.0RS"`` → ``"sg50rs"``)."""
    return _MODEL_JUNK.sub("", (value or "").casefold())


def snake(key: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", key).lower()


# ── categories, brands, schema ─────────────────────────────────────────────────────────────────────────────────


def derived_prefix(slug: str) -> str:
    base = re.sub(r"[^A-Z0-9]", "", slug.upper()) or "CAT"
    base = base if base[0].isalpha() else f"C{base}"
    candidate, counter = base[:6], 2
    while Category.objects.filter(sku_prefix__iexact=candidate).exists() or not SKU_PREFIX_RE.match(candidate):
        candidate = f"{base[:5]}{counter}"
        counter += 1
    return candidate


def provision_category(slug: str, *, name: str, gst_rate: Decimal, sort_order: int, user, result: ImportResult, table: str) -> Category:
    """Create the category with the documented defaults (an unknown slug gets role MISC and a warning)."""
    defaults = CATEGORY_DEFAULTS.get(slug)
    if defaults is None:
        result.violation(table, slug, "unknown_category_role", f"Category {slug!r} has no documented BOM role; created as MISC.", severity="warning")
        defaults = {"bom_role": BomRole.MISC}
    prefix = defaults.get("sku_prefix")
    if not prefix or Category.objects.filter(sku_prefix__iexact=prefix).exists():
        prefix = derived_prefix(slug)
    data = {"slug": slug, "name": name, "gst_rate": gst_rate, "bom_role": defaults["bom_role"], "unit": defaults.get("unit", Unit.NOS), "sku_prefix": prefix, "sort_order": sort_order}
    return category_services.create_category(user=user, data=data)


def json_type(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, (float, Decimal)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def extend_attribute_schema(category: Category, attributes_seen: list[dict], *, user) -> Category:
    """Add every attribute key the source uses (with the JSON types observed) to the category's schema."""
    schema = json.loads(json.dumps(category.attribute_schema or {}))
    properties = schema.setdefault("properties", {})
    changed = not category.attribute_schema
    schema.setdefault("type", "object")
    for attributes in attributes_seen:
        for key, value in attributes.items():
            observed = json_type(value)
            prop = properties.setdefault(key, {})
            current = prop.get("type")
            types = set(current if isinstance(current, list) else [current] if current else [])
            if observed == "integer" and "number" in types:
                continue
            if observed not in types:
                types.add(observed)
                if "integer" in types and "number" in types:
                    types.discard("integer")
                prop["type"] = sorted(types)[0] if len(types) == 1 else sorted(types)
                changed = True
    if not changed:
        return category
    return category_services.update_category(category, user=user, data={"attribute_schema": schema})


def near_matching_brands(brand: Brand) -> list[str]:
    """Existing brands whose name is a whole-word prefix of this one or vice versa (``Adani`` / ``Adani Solar``)."""
    key = brand_services.name_key(brand.name)
    similar = []
    for other in Brand.objects.exclude(pk=brand.pk).only("name"):
        other_key = brand_services.name_key(other.name)
        if key.startswith(other_key + " ") or other_key.startswith(key + " "):
            similar.append(other.name)
    return sorted(similar)


def ensure_brand(name: str | None, *, user, result: ImportResult, table: str, source_id) -> Brand | None:
    brand, created = brand_services.ensure_brand(name, user=user)
    if created:
        similar = near_matching_brands(brand)
        if similar:
            result.violation(
                table,
                source_id,
                "brand_near_match",
                f"Brand {brand.name!r} was created; similar brands exist ({', '.join(similar)}). Merge them in Studio if they are the same maker.",
                severity="warning",
                brand=brand.name,
                similar=similar,
            )
    return brand


# ── components ─────────────────────────────────────────────────────────────────────────────────────────────────


def set_timestamps(instance, *, created_at=None, updated_at=None, created_by=None, updated_by=None) -> None:
    """Keep the source's timestamps/attribution (F1 decision 9): plain UPDATE, no version bump."""
    values = {}
    if created_at is not None and instance.created_at != created_at:
        values["created_at"] = created_at
    if updated_at is not None and instance.updated_at != updated_at:
        values["updated_at"] = updated_at
    if created_by is not None and instance.created_by_id != created_by.pk:
        values["created_by"] = created_by
    if updated_by is not None and instance.updated_by_id != updated_by.pk:
        values["updated_by"] = updated_by
    if values:
        type(instance).all_objects.filter(pk=instance.pk).update(**values)
        for name, value in values.items():
            setattr(instance, name, value)


def earlier(current: datetime | None, incoming: datetime | None) -> datetime | None:
    """``incoming`` when it predates ``current`` (the value to write), else ``None`` (keep ``current``).

    A component fed by several sources (BOM and Flarize share SKUs) was created when the earliest source created it:
    taking the minimum makes ``created_at`` independent of the import order.
    """
    if incoming is None or (current is not None and current <= incoming):
        return None
    return incoming


def set_status(component: Component, status: str, *, user, reason: str = "") -> bool:
    """Mirror the source's lifecycle (importers only; staff use the lifecycle endpoints)."""
    if component.status == status:
        return False
    before = component.status
    values = {"status": status, "status_changed_at": timezone.now()}
    if status == ComponentStatus.RETIRED:
        values["retired_reason"] = reason
    component.versioned_update(user, **values)
    record_change(component, user=user, field="status", old=before, new=status, reason=reason or "legacy import")
    return True


def upsert_component(existing: Component | None, data: dict, *, user, status: str, reason: str, status_reason: str = "") -> tuple[Component, str]:
    """Create or update through the component services; returns ``(component, "created"|"updated"|"unchanged")``."""
    with transaction.atomic():
        if existing is None:
            component = component_services.create_component(user=user, data=data, initial_status=status, reason=reason)
            if status == ComponentStatus.RETIRED and status_reason:
                component.versioned_update(user, retired_reason=status_reason)
            return component, "created"
        version = existing.version
        component = component_services.update_component(existing, user=user, data={key: value for key, value in data.items() if key != "sku"}, reason=reason)
        status_changed = set_status(component, status, user=user, reason=status_reason or reason)
        return component, ("updated" if status_changed or component.version != version else "unchanged")


def import_change_log(component: Component, entries, *, source_system: str, reason: str) -> int:
    """Copy a source change log (Flarize ``changeLog``) once: entries already present (same time/field/value) are skipped."""
    created = 0
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        at = moment(entry.get("at")) or timezone.now()
        field_name = f"source.{snake(str(entry.get('action') or 'change'))}"[:64]
        new = json_ready({key: value for key, value in entry.items() if key not in {"at"}})
        if ComponentChange.objects.filter(component=component, at=at, field=field_name, new=new).exists():
            continue
        user = actor_or_none(legacy_user(source_system, "users.json", entry.get("by")))
        ComponentChange.objects.create(component=component, at=at, by=user, field=field_name, new=new, reason=reason)
        created += 1
    return created
