"""Shared machinery of the sales importers (``customers`` and ``leads`` ``legacy_import`` modules).

Contract (PLAN §7.1, same shape as the content importers): each import function takes the source rows as plain
dicts and returns ``{"created", "updated", "skipped", "violations"}``.

* **idempotent** through ``core_legacy_map`` (``<system>``, ``<source table>``, ``<source id>``): a mapped row is
  updated when the source changed and counted as skipped otherwise; re-running never duplicates;
* source timestamps and attribution are preserved;
* **violations** are listed, never raised (``{"source_table", "source_id", "code", "message"}``); a row that cannot be
  imported is skipped, a value that cannot be kept is reported with what was done instead;
* ``dry_run=True`` runs everything in a transaction that is rolled back, so the counts and violations are exact;
* each call writes one audit row with the counts and the SHA-256 of the source rows and bumps the caller's cache
  namespaces. Importing emits no outbox events (it creates nothing new for other contexts to react to).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Callable, Iterable

from django.db import models, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from audit.services import record
from core.models import LegacyMap
from flarize.cache_utils import bump


class ImportRun:
    """Counts and violations of one import call."""

    def __init__(self, source_system: str, source_table: str):
        self.source_system = source_system
        self.source_table = source_table
        self.created = self.updated = self.skipped = 0
        self.violations: list[dict] = []

    def violation(self, source_id, code: str, message: str) -> None:
        self.violations.append({"source_table": self.source_table, "source_id": str(source_id), "code": code, "message": message})

    def as_dict(self) -> dict:
        return {"created": self.created, "updated": self.updated, "skipped": self.skipped, "violations": self.violations}

    # ── core_legacy_map ─────────────────────────────────────────────────────────────────────────────────────────────
    def mapped_id(self, source_id, *, source_table: str | None = None, source_system: str | None = None) -> int | None:
        return mapped_id(source_system or self.source_system, source_table or self.source_table, source_id)

    def link(self, source_id, instance: models.Model) -> None:
        LegacyMap.objects.update_or_create(
            source_system=self.source_system,
            source_table=self.source_table,
            source_id=str(source_id),
            defaults={"target_table": instance._meta.db_table, "target_id": instance.pk, "imported_at": timezone.now()},
        )

    def find_target(self, model: type[models.Model], source_id, natural: Callable[[], models.Model | None] | None = None) -> models.Model | None:
        """The mapped target (soft-deleted rows included), else the natural-key match, else ``None``."""
        pk = self.mapped_id(source_id)
        if pk is not None:
            found = model.all_objects.filter(pk=pk).first()
            if found is not None:
                return found
        return natural() if natural is not None else None


def mapped_id(source_system: str, source_table: str, source_id) -> int | None:
    if source_id in (None, ""):
        return None
    return LegacyMap.objects.filter(source_system=source_system, source_table=source_table, source_id=str(source_id)).values_list("target_id", flat=True).first()


def timestamp(value) -> dt.datetime | None:
    """ISO string / datetime → aware datetime (naive values are UTC); ``None`` for empty."""
    if value in (None, ""):
        return None
    parsed = value if isinstance(value, dt.datetime) else parse_datetime(str(value).replace("Z", "+00:00"))
    if parsed is None:
        raise ValueError(f"not a timestamp: {value!r}")
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed, dt.UTC)


def date_value(value) -> dt.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    parsed = parse_date(str(value))
    if parsed is None:
        raise ValueError(f"not a date: {value!r}")
    return parsed


def checksum(rows: Iterable[dict]) -> str:
    return hashlib.sha256(json.dumps(list(rows), sort_keys=True, default=str).encode()).hexdigest()


def upsert(
    run: ImportRun, model: type[models.Model], source_id, *, target: models.Model | None, values: dict, created_at=None, updated_at=None, created_by_id=None, updated_by_id=None
) -> models.Model:
    """Create or update one target row from ``values`` (column → value, FKs as ``<name>_id``), preserving timestamps."""
    if target is None:
        instance = model(**values, created_by_id=created_by_id, updated_by_id=updated_by_id or created_by_id)
        instance.save()
        created = created_at or timezone.now()
        model.all_objects.filter(pk=instance.pk).update(created_at=created, updated_at=updated_at or created)
        instance.refresh_from_db()
        run.created += 1
    else:
        instance = target
        diff = {name: value for name, value in values.items() if getattr(target, name) != value}
        if updated_by_id is not None and target.updated_by_id != updated_by_id:
            diff["updated_by_id"] = updated_by_id
        if diff:
            model.all_objects.filter(pk=target.pk).update(**diff, version=F("version") + 1, updated_at=updated_at or timezone.now())
            instance.refresh_from_db()
            run.updated += 1
        else:
            run.skipped += 1
    run.link(source_id, instance)
    return instance


def run_import(
    run: ImportRun,
    rows: list[dict],
    import_row: Callable[[ImportRun, dict], None],
    *,
    user,
    dry_run: bool,
    action: str,
    object_type: str,
    namespaces: tuple[str, ...],
    order: Callable[[dict], object] | None = None,
) -> dict:
    """Import ``rows`` (source order) with ``import_row`` inside one transaction; audit, bump, or roll back."""
    ordered = sorted(rows, key=order) if order is not None else list(rows)
    with transaction.atomic():
        for row in ordered:
            import_row(run, row)
        record(
            action,
            object_type=object_type,
            actor=user,
            after={"source_system": run.source_system, "source_table": run.source_table, "rows": len(ordered), "checksum": checksum(ordered), **{**run.as_dict(), "violations": len(run.violations)}},
            note=f"{run.source_system} import",
        )
        if dry_run:
            transaction.set_rollback(True)
        elif namespaces:
            bump(*namespaces)
    return run.as_dict()
