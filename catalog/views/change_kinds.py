"""Catalog rows Procurement and a Project Head change through a change request (``approvals``): applied with the
same serializer and service as the catalog CRUD endpoints, by the Admin who approves."""

from __future__ import annotations

from approvals.services.kinds import SET, ChangeKind, register, viewset_kind
from catalog.models import BatteryFamily, Brand, Category, Component
from catalog.services import csv_io
from catalog.views import components, masters
from core.errors import Conflict, DomainError


def register_kinds() -> None:
    viewset_kind("catalog.component", "Catalog component", components.ComponentViewSet, model=Component)
    viewset_kind("catalog.category", "Catalog category", masters.CategoryViewSet, model=Category)
    viewset_kind("catalog.brand", "Brand", masters.BrandViewSet, model=Brand)
    viewset_kind("catalog.battery_family", "Battery family", masters.BatteryFamilyViewSet, model=BatteryFamily)
    register_import_kind()


# ── CSV import (POST catalog/components/import/) ───────────────────────────────────────────────────────────────────
# A bulk upsert of components, so it changes configuration like the single-row edits above. The request carries the
# CSV text; submitting runs the import's own dry run as the requester (a file with row errors is refused now), and
# approving re-runs the dry run as the Admin and commits it — all rows or none, exactly as the endpoint does.


def _csv_bytes(payload) -> bytes:
    text = (payload or {}).get("csv")
    if not isinstance(text, str) or not text.strip():
        raise DomainError("validation_error", "Attach the CSV.", errors={"csv": ["Send the file's text."]})
    content = text.encode("utf-8")
    if len(content) > csv_io.MAX_BYTES:
        raise DomainError("file_too_large", "The CSV is larger than 2 MB.", status=413)
    return content


def _row_errors(report) -> dict:
    return {f"line {row.line}": [f"{key}: {value}" for key, value in row.errors.items()] for row in report.rows if row.action == "error"}


def _validate_import(operation, payload, target, context):
    content = _csv_bytes(payload)
    # The file's content, not the requester's grants: the Admin's catalog.edit applies when it is approved.
    report = csv_io.import_csv(content, user=_user(context), dry_run=True, check_permissions=False)
    errors = _row_errors(report)
    if errors:
        raise DomainError("import_has_errors", "The file has rows that cannot be imported; fix them and ask again.", errors=dict(list(errors.items())[:50]))
    return {"content": content}


def _apply_import(operation, data, target, user, expected_version):
    report = csv_io.import_csv(data["content"], user=user, dry_run=True)
    if _row_errors(report):
        raise Conflict("import_no_longer_valid", "The catalog changed since the request and the file no longer imports cleanly; reject it and ask again.")
    csv_io.import_csv(data["content"], user=user, dry_run=False, import_token=report.import_token)
    return None


def _snapshot_import(target, payload, context):
    report = csv_io.import_csv(_csv_bytes(payload), user=_user(context), dry_run=True, check_permissions=False)
    rows = [{"line": row.line, "sku": row.sku, "action": row.action} for row in report.rows if row.action != "unchanged"]
    return {"summary": report.summary, "rows": rows[:200]}


def _describe_import(target, payload) -> str:
    lines = [line for line in str((payload or {}).get("csv") or "").splitlines() if line.strip()]
    return f"Component CSV import: {max(len(lines) - 1, 0)} row(s)"


def _user(context):
    return getattr((context or {}).get("request"), "user", None)


def register_import_kind() -> None:
    register(
        ChangeKind(
            key="catalog.component_import",
            label="Catalog CSV import",
            module="catalog",
            operations={SET: "create"},
            model=None,
            validate=_validate_import,
            apply=_apply_import,
            snapshot=_snapshot_import,
            describe=_describe_import,
        )
    )
