"""CSV import (``POST catalog/components/import/``) and export (``GET catalog/components/export/``).

Import is two-step, enforced:

1. **dry run** (``dry_run=true``, the default): every row is validated with the API serializers and executed through
   the component services inside a transaction that is rolled back, so the report shows exactly what would happen
   (``create`` / ``update`` / ``unchanged`` / ``error`` per row, brands that would be created). A clean dry run
   returns an ``import_token`` bound to the file's SHA-256 and the user (valid 1 hour);
2. **commit** (``dry_run=false`` + ``import_token``): the same file is imported for real in one transaction — all rows
   or none (400 ``import_has_errors``). Without a matching token: 400 ``dry_run_required``.

Columns: ``sku, category, brand, brand_label, name, model, description, status, tiers, is_public, is_premium,
gst_rate_override, hsn_code_override, unit_override, warranty_product_years, warranty_performance_years,
warranty_extendable_years, warranty_text, engineering_status, notes, datasheet_url, attributes`` and
``spec.<column>`` for the spec table of each exported category. ``category`` is the category slug, ``brand`` the brand
name (created when missing), ``tiers`` ``BASE|VALUE``, ``attributes`` and list-valued spec cells are JSON, the battery
``spec.family`` is the family slug. ``status`` is informational (lifecycle actions change status). An empty cell
means "no value". Rows with an existing SKU update that component (``catalog.edit`` needed); others create DRAFT
components (a blank SKU is generated). Cells that could run as spreadsheet formulas are exported with a leading
``'``, which the import strips again (round trip is lossless).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field

from django.core import signing
from django.db import transaction
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from accounts.services.authz import can
from audit.services import record
from catalog.models import BatteryFamily, Category
from catalog.serializers.components import ComponentUpdateSerializer, ComponentWriteSerializer
from catalog.services import brands, components
from catalog.services.components import SPEC_KEYS, live_tiers
from catalog.services.specs import SPEC_RELATED, get_spec, spec_fields, spec_kind
from core.errors import DomainError

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000
MAX_EXPORT_ROWS = 10000
TOKEN_SALT = "catalog.components.import"
TOKEN_MAX_AGE = 60 * 60
BASE_COLUMNS = (
    "sku",
    "category",
    "brand",
    "brand_label",
    "name",
    "model",
    "description",
    "status",
    "tiers",
    "is_public",
    "is_premium",
    "gst_rate_override",
    "hsn_code_override",
    "unit_override",
    "warranty_product_years",
    "warranty_performance_years",
    "warranty_extendable_years",
    "warranty_text",
    "engineering_status",
    "notes",
    "datasheet_url",
    "attributes",
)
REQUIRED_COLUMNS = ("sku", "category", "name")
JSON_SPEC_FIELDS = {"certifications", "communication", "compatible_battery_families", "compatible_inverters", "compatible_system_types", "compatible_phases", "open_items", "status_history"}
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
ALL_SPEC_COLUMNS = {f"spec.{name}" for kind in SPEC_RELATED for name in spec_fields(kind)}


# ── export ─────────────────────────────────────────────────────────────────────────────────────────────────────


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = str(value)
    if text.startswith(FORMULA_PREFIXES):
        try:
            float(text)
        except ValueError:
            return "'" + text
    return text


def _spec_cell(spec, name: str):
    if name == "family":
        return spec.family.slug if spec.family_id else ""
    return getattr(spec, name)


def export_csv(queryset) -> str:
    rows = list(queryset[: MAX_EXPORT_ROWS + 1])
    if len(rows) > MAX_EXPORT_ROWS:
        raise DomainError("export_too_large", f"More than {MAX_EXPORT_ROWS} components match; narrow the filters.")
    kinds = [kind for kind in SPEC_RELATED if any(spec_kind(component.category) == kind for component in rows)]
    spec_columns = [(kind, name) for kind in kinds for name in spec_fields(kind)]
    header = [*BASE_COLUMNS, *dict.fromkeys(f"spec.{name}" for _, name in spec_columns)]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for component in rows:
        values = {
            "sku": component.sku,
            "category": component.category.slug,
            "brand": component.brand.name if component.brand_id else "",
            "tiers": "|".join(live_tiers(component)),
            **{name: getattr(component, name) for name in BASE_COLUMNS if name not in {"sku", "category", "brand", "tiers"}},
        }
        kind = spec_kind(component.category)
        spec = get_spec(component, kind) if kind else None
        if spec is not None:
            values.update({f"spec.{name}": _spec_cell(spec, name) for name in spec_fields(kind)})
        writer.writerow([_cell(values.get(column)) for column in header])
    return buffer.getvalue()


# ── import ─────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass
class RowResult:
    line: int
    sku: str
    action: str
    errors: dict = field(default_factory=dict)
    brand_created: str = ""


@dataclass
class ImportReport:
    dry_run: bool
    committed: bool = False
    import_token: str | None = None
    rows: list[RowResult] = field(default_factory=list)

    @property
    def summary(self) -> dict:
        counts = {"rows": len(self.rows), "create": 0, "update": 0, "unchanged": 0, "error": 0}
        for row in self.rows:
            counts[row.action] += 1
        return counts


class _Rollback(Exception):
    pass


def file_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def issue_token(content: bytes, user) -> str:
    return signing.dumps({"sha256": file_digest(content), "user": str(user.uid)}, salt=TOKEN_SALT, compress=True)


def check_token(token: str | None, content: bytes, user) -> None:
    try:
        payload = signing.loads(token or "", salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
    except signing.BadSignature:
        payload = None
    if not payload or payload.get("sha256") != file_digest(content) or payload.get("user") != str(user.uid):
        raise DomainError("dry_run_required", "Run a dry run of this exact file first and send its import_token (valid 1 hour).", errors={"import_token": ["Missing, expired or for another file."]})


def _decode(content: bytes) -> list[dict]:
    if len(content) > MAX_BYTES:
        raise DomainError("file_too_large", f"The CSV is larger than {MAX_BYTES // (1024 * 1024)} MB.", status=413)
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise DomainError("invalid_csv", "The file is not UTF-8 text.") from None
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    missing = [name for name in REQUIRED_COLUMNS if name not in header]
    unknown = [name for name in header if name not in BASE_COLUMNS and name not in ALL_SPEC_COLUMNS]
    if missing or unknown:
        errors = {**{name: ["Required column missing."] for name in missing}, **{name: ["Unknown column."] for name in unknown}}
        raise DomainError("invalid_csv", "The CSV header does not match the component format.", errors=errors)
    rows = []
    for row in reader:
        if None in row:
            raise DomainError("invalid_csv", f"Line {reader.line_num} has more cells than the header.")
        rows.append({key: _uncell(value) for key, value in row.items()})
        if len(rows) > MAX_ROWS:
            raise DomainError("invalid_csv", f"More than {MAX_ROWS} rows; split the file.")
    return rows


def _uncell(value: str | None) -> str:
    value = (value or "").strip()
    if value.startswith("'") and value[1:].startswith(FORMULA_PREFIXES):
        return value[1:]
    return value


def _json_cell(value: str, column: str):
    try:
        return json.loads(value)
    except ValueError:
        raise ValidationError({column: ["Not valid JSON."]}) from None


def _errors(detail) -> dict:
    """DRF validation detail / DomainError errors as ``{field: [messages]}`` (nested fields keep their structure)."""

    def convert(value):
        if isinstance(value, dict):
            return {str(key): convert(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [convert(item) for item in value]
        return str(value)

    if isinstance(detail, dict):
        return {str(key): (convert(value) if isinstance(value, (list, dict)) else [str(value)]) for key, value in detail.items()}
    return {"non_field_errors": convert(detail) if isinstance(detail, (list, tuple)) else [str(detail)]}


def _drop_blank(payload: dict, serializer_class) -> dict:
    """Empty cells: null for nullable fields, "" for blank-able text, omitted otherwise."""
    fields = serializer_class().fields
    result = {}
    for name, value in payload.items():
        target = fields.get(name)
        if value != "" or target is None:
            result[name] = value
        elif getattr(target, "allow_null", False):
            result[name] = None
        elif isinstance(target, serializers.CharField) and target.allow_blank:
            result[name] = ""
    return result


def _payload(row: dict, category: Category | None, *, updating: bool, user, result: RowResult) -> dict:
    serializer_class = ComponentUpdateSerializer if updating else ComponentWriteSerializer
    data = {name: row[name] for name in BASE_COLUMNS if name in row and name not in {"sku", "category", "brand", "status", "tiers", "attributes"}}
    if category is not None:
        data["category"] = str(category.uid)
    if "brand" in row:
        if row["brand"]:
            brand, created = brands.ensure_brand(row["brand"], user=user)
            data["brand"] = str(brand.uid)
            result.brand_created = brand.name if created else ""
        else:
            data["brand"] = None
    if row.get("tiers") is not None and "tiers" in row:
        data["tiers"] = [part.strip().upper() for part in row["tiers"].replace(",", "|").split("|") if part.strip()]
    if row.get("attributes"):
        data["attributes"] = _json_cell(row["attributes"], "attributes")
    elif "attributes" in row and not updating:
        data["attributes"] = {}
    data = _drop_blank(data, serializer_class)
    kind = spec_kind(category) if category is not None else None
    spec_cells = {column[5:]: value for column, value in row.items() if column.startswith("spec.")}
    applicable = set(spec_fields(kind)) if kind else set()
    stray = {f"spec.{name}": ["Not a field of this category's spec."] for name, value in spec_cells.items() if value and name not in applicable}
    if stray:
        raise ValidationError(stray)
    spec = {name: value for name, value in spec_cells.items() if name in applicable}
    if kind and any(value != "" for value in spec.values()):
        for name in JSON_SPEC_FIELDS & set(spec):
            spec[name] = _json_cell(spec[name], f"spec.{name}") if spec[name] else ""  # "": null or omitted, see _drop_blank
        if spec.get("family"):
            family = BatteryFamily.objects.filter(slug=spec["family"]).first()
            if family is None:
                raise ValidationError({"spec.family": [f"Unknown battery family {spec['family']!r}."]})
            spec["family"] = str(family.uid)
        write_serializer = serializer_class().fields[SPEC_RELATED[kind]].__class__
        data[SPEC_RELATED[kind]] = _drop_blank(spec, write_serializer)
    return data


def _import_row(row: dict, line: int, *, user, seen: set[str]) -> RowResult:
    sku = row.get("sku", "")
    result = RowResult(line=line, sku=sku, action="error")
    if sku and sku.lower() in seen:
        result.errors = {"sku": ["Duplicate SKU in this file."]}
        return result
    if sku:
        seen.add(sku.lower())
    existing = components.get_by_sku(sku) if sku else None
    category = Category.objects.filter(slug=row.get("category", "")).first() if row.get("category") else None
    if category is None and existing is None:
        result.errors = {"category": [f"Unknown category {row.get('category')!r}."]}
        return result
    if existing is not None and not can(user, "catalog", "edit"):
        result.errors = {"sku": ["Updating existing components needs the catalog.edit permission."]}
        return result
    try:
        with transaction.atomic():
            data = _payload(row, category or existing.category, updating=existing is not None, user=user, result=result)
            if existing is None:
                if sku:
                    data["sku"] = sku
                serializer = ComponentWriteSerializer(data=data)
                serializer.is_valid(raise_exception=True)
                component = components.create_component(user=user, data=dict(serializer.validated_data), reason="CSV import")
                result.sku, result.action = component.sku, "create"
            else:
                data.pop("sku", None)
                serializer = ComponentUpdateSerializer(existing, data=data, partial=True)
                serializer.is_valid(raise_exception=True)
                validated = dict(serializer.validated_data)
                for key in SPEC_KEYS:
                    if key in validated and validated[key] is not None:
                        validated[key] = dict(validated[key])
                version = existing.version
                component = components.update_component(existing, user=user, data=validated, reason="CSV import")
                result.sku, result.action = component.sku, ("update" if component.version != version else "unchanged")
    except ValidationError as exc:
        result.action, result.errors = "error", _errors(exc.detail)
    except DomainError as exc:
        result.action, result.errors = "error", (_errors(exc.errors) if exc.errors else {"non_field_errors": [exc.message]})
    return result


def import_csv(content: bytes, *, user, dry_run: bool = True, import_token: str | None = None) -> ImportReport:
    rows = _decode(content)
    if not dry_run:
        check_token(import_token, content, user)
    report = ImportReport(dry_run=dry_run)
    try:
        with transaction.atomic():
            seen: set[str] = set()
            report.rows = [_import_row(row, line, user=user, seen=seen) for line, row in enumerate(rows, start=2)]
            has_errors = any(row.action == "error" for row in report.rows)
            if dry_run or has_errors:
                raise _Rollback
            record("catalog.components_imported", object_type="catalog.component", actor=user, after={"sha256": file_digest(content), **report.summary}, note="CSV import")
    except _Rollback:
        pass
    if not dry_run and any(row.action == "error" for row in report.rows):
        errors = {f"line {row.line}": [f"{key}: {value}" for key, value in row.errors.items()] for row in report.rows if row.action == "error"}
        raise DomainError("import_has_errors", "Nothing was imported: fix the rows listed and run the dry run again.", errors=dict(list(errors.items())[:50]))
    if dry_run and not any(row.action == "error" for row in report.rows):
        report.import_token = issue_token(content, user)
    report.committed = not dry_run
    return report


def exported_components(queryset):
    """The export's queryset: live components with everything a row needs, ordered by SKU."""
    return queryset.select_related("brand", "category", "panel_spec", "inverter_spec", "battery_spec__family", "structure_spec").order_by("sku", "id")
