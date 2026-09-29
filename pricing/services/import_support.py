"""Shared machinery of the pricing and procurement legacy importers (PLAN §7.1 contract).

Each import function takes plain source rows / parsed JSON and returns ``{"created", "updated", "skipped",
"violations"}`` (plus ``counts`` per source table). Idempotent through ``core_legacy_map`` (re-running updates or skips,
never duplicates); ``dry_run=True`` runs everything in a transaction that is rolled back; one audit row per call with
the counts and the SHA-256 of the input. Violations: ``{source_table, source_id, code, severity, message, …}`` —
``error`` rows were not imported, ``warning`` rows were imported and need a look (D-2 differences are warnings).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Callable
from decimal import Decimal, InvalidOperation

from django.core.serializers.json import DjangoJSONEncoder
from django.db import DataError, IntegrityError, models, transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from audit.services import record
from core.models import LegacyMap
from flarize.cache_utils import bump

BACKEND = LegacyMap.SourceSystem.BACKEND
FLARIZE = LegacyMap.SourceSystem.FLARIZE
PA = LegacyMap.SourceSystem.PA
SOURCE_RANK = {BACKEND: 1, PA: 1, FLARIZE: 2}


class ImportRun:
    def __init__(self, name: str):
        self.name = name
        self.created = self.updated = self.skipped = 0
        self.violations: list[dict] = []
        self.counts: dict[str, dict[str, int]] = {}

    def count(self, table: str, outcome: str) -> None:
        bucket = self.counts.setdefault(table, {"created": 0, "updated": 0, "skipped": 0})
        bucket[outcome] += 1
        setattr(self, outcome, getattr(self, outcome) + 1)

    def violation(self, table: str, source_id, code: str, message: str, *, severity: str = "error", **context) -> None:
        self.violations.append({"source_table": table, "source_id": str(source_id), "code": code, "severity": severity, "message": message, **json_ready(context)})

    def as_dict(self) -> dict:
        return {"created": self.created, "updated": self.updated, "skipped": self.skipped, "violations": self.violations, "counts": self.counts}


class DryRunRollback(Exception):
    pass


def json_ready(value):
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def checksum(*payloads) -> str:
    return hashlib.sha256(json.dumps(payloads, cls=DjangoJSONEncoder, sort_keys=True, default=str).encode()).hexdigest()


def run(name: str, action: str, payloads, body: Callable[[ImportRun], None], *, user, dry_run: bool, object_type: str, namespaces: tuple[str, ...] = ()) -> dict:
    """Run ``body`` in one transaction; audit; roll back on ``dry_run``; bump ``namespaces`` otherwise."""
    result = ImportRun(name)
    try:
        with transaction.atomic():
            body(result)
            record(
                action,
                object_type=object_type,
                actor=user,
                after={"import": name, "checksum": checksum(payloads), **{key: value for key, value in result.as_dict().items() if key != "violations"}, "violations": len(result.violations)},
                note=f"{name} import{' (dry run)' if dry_run else ''}",
            )
            if dry_run:
                raise DryRunRollback()
    except DryRunRollback:
        pass
    else:
        if namespaces:
            bump(*namespaces)
    return result.as_dict()


def guarded(result: ImportRun, table: str, source_id, fn: Callable[[], str | None]) -> None:
    """Run one source row in its own savepoint; a bad value or a domain error rejects only that row.

    ``fn`` returns the outcome (``created`` / ``updated`` / ``skipped``) or ``None`` when it counted itself.
    """
    from core.errors import DomainError

    try:
        with transaction.atomic():
            outcome = fn()
    except BadValue as exc:
        result.violation(table, source_id, "invalid_value", str(exc))
        result.count(table, "skipped")
        return
    except DomainError as exc:
        result.violation(table, source_id, exc.code, exc.message, errors=exc.errors)
        result.count(table, "skipped")
        return
    except IntegrityError as exc:
        result.violation(table, source_id, "integrity_error", str(exc).splitlines()[0])
        result.count(table, "skipped")
        return
    except DataError as exc:  # a value the column cannot hold (too long, numeric overflow): this row only
        result.violation(table, source_id, "invalid_value", str(exc).splitlines()[0])
        result.count(table, "skipped")
        return
    if outcome:
        result.count(table, outcome)


# ── core_legacy_map ─────────────────────────────────────────────────────────────────────────────────────────────


def mapped(source_system: str, source_table: str, source_id, model: type[models.Model]):
    entry = LegacyMap.objects.filter(source_system=source_system, source_table=source_table, source_id=str(source_id)).first()
    if entry is None:
        return None
    manager = getattr(model, "all_objects", model.objects)
    return manager.filter(pk=entry.target_id).first()


def remember(source_system: str, source_table: str, source_id, target: models.Model) -> None:
    LegacyMap.objects.update_or_create(
        source_system=source_system,
        source_table=source_table,
        source_id=str(source_id),
        defaults={"target_table": target._meta.db_table, "target_id": target.pk, "imported_at": timezone.now()},
    )


def mapped_from(target: models.Model, *, source_table_prefix: str = "") -> set[str]:
    """The source systems that mapped a row onto ``target`` (optionally only tables starting with a prefix)."""
    rows = LegacyMap.objects.filter(target_table=target._meta.db_table, target_id=target.pk)
    if source_table_prefix:
        rows = rows.filter(source_table__startswith=source_table_prefix)
    return set(rows.values_list("source_system", flat=True))


def legacy_user(source_system: str, source_id):
    """The platform user an earlier user import created for a source user id (``None`` when not imported)."""
    from django.contrib.auth import get_user_model

    if not source_id:
        return None
    for table in ("users.json", "auth_user"):
        entry = LegacyMap.objects.filter(source_system=source_system, source_table=table, source_id=str(source_id)).first()
        if entry is not None:
            return get_user_model().all_objects.filter(pk=entry.target_id).first()
    return None


def component_for(source_system: str, source_table: str, source_id, sku: str | None = None):
    """The catalog component a source row was imported into (``core_legacy_map``), else the live one with that SKU."""
    from catalog.models import Component

    component = mapped(source_system, source_table, source_id, Component) if source_table else None
    if component is None and sku:
        component = Component.objects.filter(sku__iexact=sku).first()
    return component


# ── values ─────────────────────────────────────────────────────────────────────────────────────────────────────


class BadValue(ValueError):
    pass


def dec(value, column: str, *, places: int = 2, digits: int = 14) -> Decimal | None:
    """``value`` as a Decimal fitting ``numeric(digits, places)``; :class:`BadValue` when it would lose data."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BadValue(f"{column}={value!r}: not a number")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BadValue(f"{column}={value!r}: not a number") from None
    if not number.is_finite():
        raise BadValue(f"{column}={value!r}: not a finite number")
    quantum = Decimal(1).scaleb(-places)
    if number.quantize(quantum) != number:
        raise BadValue(f"{column}={value!r}: more than {places} decimal places")
    if abs(number) >= Decimal(10) ** (digits - places):
        raise BadValue(f"{column}={value!r}: does not fit numeric({digits},{places})")
    return number.quantize(quantum)


def moment(value) -> dt.datetime | None:
    if value in (None, ""):
        return None
    parsed = value if isinstance(value, dt.datetime) else parse_datetime(str(value).replace("Z", "+00:00"))
    if parsed is None:
        raise BadValue(f"{value!r}: not a timestamp")
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed, dt.UTC)


def day(value) -> dt.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value)
    parsed = parse_date(text[:10]) if len(text) >= 10 else None
    if parsed is None:
        raise BadValue(f"{value!r}: not a date")
    return parsed


def set_timestamps(instance: models.Model, *, created_at=None, updated_at=None) -> None:
    """Keep the source's timestamps (plain UPDATE, no version bump)."""
    values = {}
    if created_at is not None and instance.created_at != created_at:
        values["created_at"] = created_at
    if updated_at is not None and instance.updated_at != updated_at:
        values["updated_at"] = updated_at
    if values:
        type(instance).all_objects.filter(pk=instance.pk).update(**values)
        for name, value in values.items():
            setattr(instance, name, value)
