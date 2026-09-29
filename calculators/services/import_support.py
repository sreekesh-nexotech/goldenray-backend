"""Shared helpers of the calculators' and the EMI calculator's legacy importers (``legacy_import.py`` of both apps).

``Report`` is the importer result contract ``{"created", "updated", "skipped", "violations"}``; :func:`upsert` is the
``core_legacy_map`` idempotent write (re-runs update, never duplicate; rows deleted in the platform stay deleted;
source timestamps survive); :func:`run` imports a batch row by row in savepoints and writes the one audit row.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from decimal import Decimal, InvalidOperation

from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from audit.services import record
from core.models import LegacyMap
from flarize.cache_utils import bump

BACKEND = LegacyMap.SourceSystem.BACKEND


class Report:
    def __init__(self):
        self.created = self.updated = self.skipped = 0
        self.violations: list[dict] = []

    def violation(self, source_id, field: str, message: str) -> None:
        self.violations.append({"source_id": str(source_id), "field": field, "message": message})

    def as_dict(self) -> dict:
        return {"created": self.created, "updated": self.updated, "skipped": self.skipped, "violations": self.violations}


class Skip(Exception):
    """The row cannot be imported (the reason is already in the report)."""


def checksum(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, cls=DjangoJSONEncoder, sort_keys=True, default=str).encode()).hexdigest()


def parse_timestamp(value):
    if value in (None, ""):
        return None
    return parse_datetime(value) if isinstance(value, str) else value


def exact_decimal(row: dict, field: str, report: Report, *, places: int, digits: int, required: bool = True, minimum: Decimal | None = Decimal("0"), scale: int = 0) -> Decimal | None:
    """``row[field]`` (divided by 10**``scale``: 2 turns a percentage into a fraction) as a Decimal that fits
    ``numeric(digits, places)`` exactly — a value that would need rounding is refused, never rounded."""
    value = row.get(field)
    if value is None or value == "":
        if required:
            report.violation(row.get("id"), field, "a value is required")
            raise Skip
        return None
    try:
        number = Decimal(str(value)).scaleb(-scale)
    except (InvalidOperation, TypeError, ValueError):
        report.violation(row.get("id"), field, f"not a number: {value!r}")
        raise Skip from None
    quantum = Decimal(1).scaleb(-places)
    if not number.is_finite() or number != number.quantize(quantum) or abs(number) >= Decimal(10) ** (digits - places):
        report.violation(row.get("id"), field, f"{value!r} does not fit numeric({digits},{places})")
        raise Skip
    if minimum is not None and number < minimum:
        report.violation(row.get("id"), field, f"{value!r} must be at least {minimum}")
        raise Skip
    return number.quantize(quantum)


def whole_number(row: dict, field: str, report: Report, *, minimum: int = 0) -> int:
    value = row.get(field)
    if isinstance(value, bool) or value is None or (isinstance(value, float) and not value.is_integer()):
        report.violation(row.get("id"), field, f"not a whole number: {value!r}")
        raise Skip
    try:
        number = int(value)
    except (TypeError, ValueError):
        report.violation(row.get("id"), field, f"not a whole number: {value!r}")
        raise Skip from None
    if number < minimum:
        report.violation(row.get("id"), field, f"{value!r} must be at least {minimum}")
        raise Skip
    return number


def mapped_id(table: str, source_id) -> int | None:
    return LegacyMap.objects.filter(source_system=BACKEND, source_table=table, source_id=str(source_id)).values_list("target_id", flat=True).first()


def upsert(model, *, table: str, source_id, values: dict, report: Report, created_at=None, updated_at=None):
    target_id = mapped_id(table, source_id)
    if target_id is not None:
        row = model.all_objects.filter(pk=target_id).first()
        if row is None or row.deleted_at is not None:
            report.skipped += 1
            return None
        changed = {name: value for name, value in values.items() if getattr(row, name) != value}
        if not changed:
            report.skipped += 1
            return row
        model.all_objects.filter(pk=row.pk).update(**changed, version=F("version") + 1, updated_at=updated_at or timezone.now())
        report.updated += 1
        return model.all_objects.get(pk=row.pk)
    row = model(**values)
    row.created_at = created_at or timezone.now()
    row.updated_at = updated_at or row.created_at
    model.objects.bulk_create([row])  # bypasses save(): source timestamps survive
    LegacyMap.objects.create(source_system=BACKEND, source_table=table, source_id=str(source_id), target_table=model._meta.db_table, target_id=row.pk)
    report.created += 1
    return row


def run(rows: Iterable[dict], table: str, model, transform: Callable[[dict, Report], dict | None], *, user, app: str, namespace: str) -> dict:
    """Import ``rows`` of legacy ``table`` into ``model``: ``transform`` returns the column values (``None`` or
    :class:`Skip` = not imported); writes ``<app>.legacy_imported`` (counts + sha256) and bumps ``namespace``."""
    rows = list(rows)
    report = Report()
    for row in rows:
        try:
            with transaction.atomic():
                values = transform(row, report)
                if values is None:
                    continue
                updated_at = parse_timestamp(row.get("updated_at"))
                created_at = parse_timestamp(row.get("created_at")) or updated_at
                upsert(model, table=table, source_id=row["id"], values=values, report=report, created_at=created_at, updated_at=updated_at)
        except Skip:
            continue
        except IntegrityError as exc:
            report.violation(row.get("id"), "row", f"rejected by the database: {str(exc).splitlines()[0]}")
    result = report.as_dict()
    record(
        f"{app}.legacy_imported",
        object_type=f"{app}.{model.__name__.lower()}",
        actor=user,
        actor_kind=None if user else "SYSTEM",
        after={
            "source": f"BACKEND {table}",
            "rows": len(rows),
            "checksum": checksum(rows),
            "created": result["created"],
            "updated": result["updated"],
            "skipped": result["skipped"],
            "violations": len(result["violations"]),
        },
    )
    bump(namespace)
    return result
