"""Legacy import for careers (PLAN §7.2 row 10, §7.3 ``job_application``): idempotent, traceable, reported.

Every function takes plain row dicts exactly as the legacy tables hold them (``SELECT *`` of the source table) and
returns ``{"created", "updated", "skipped", "violations"}``:

* each source row is tracked in ``core_legacy_map`` (``CMS``/``BACKEND`` × source table × source id), so a re-run
  updates the rows it created (only the columns that changed) and never duplicates; a platform row that was deleted
  after the import stays deleted (``skipped``);
* rows that cannot be imported are skipped and listed in ``violations`` (``{"source_id", "field", "message"}``); a
  row imported with a gap (a missing resume file, a dropped choice value) is imported and listed too;
* source timestamps are preserved (inserts and updates bypass ``BaseModel.save()``'s ``updated_at``);
* each call writes one ``audit_log`` row (``careers.legacy_imported``) with the counts and the sha256 of the batch.

Order: :func:`import_departments` → :func:`import_positions` (CMS) → :func:`import_applications` →
:func:`import_application_notes` → :func:`import_application_events` (main backend). Applications find their posting
through the CMS map of ``careers_job_position`` (the legacy ``position_id`` is the CMS id); resumes and portfolios
are read with ``read_file(path)`` (bytes or ``None``) from the legacy ``backend_media`` volume and stored as PRIVATE
media, checksum verified.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable, Iterable

from django.contrib.auth import get_user_model
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from audit.services import record
from careers.models import Department, JobApplication, JobApplicationEvent, JobApplicationNote, JobPosition
from careers.services import validation
from careers.services.applications import store_candidate_file
from core.errors import DomainError
from core.models import BaseModel, LegacyMap
from flarize.cache_utils import bump
from media.models import MediaAsset
from media.services import assets as media_assets

CMS = LegacyMap.SourceSystem.CMS
BACKEND = LegacyMap.SourceSystem.BACKEND

POSITION_STATUS = {"draft": "DRAFT", "published": "PUBLISHED", "closed": "CLOSED", "archived": "ARCHIVED"}
APPLICATION_STATUS = {"new": "NEW", "reviewing": "SCREENING", "interview": "INTERVIEW", "selected": "OFFERED", "rejected": "REJECTED"}
EVENT_KIND = {"received": "RECEIVED", "status": "STATUS", "assigned": "ASSIGNED", "archived": "ARCHIVED", "restored": "RESTORED", "note": "NOTE"}
CHOICE_FIELDS = {
    "total_experience": JobApplication.Experience,
    "relevant_experience": JobApplication.Experience,
    "current_salary": JobApplication.Salary,
    "expected_salary": JobApplication.Salary,
    "notice_period": JobApplication.NoticePeriod,
    "heard_about_us": JobApplication.HeardAbout,
}


class Report:
    def __init__(self):
        self.created = self.updated = self.skipped = 0
        self.violations: list[dict] = []

    def violation(self, source_id, field: str, message: str) -> None:
        self.violations.append({"source_id": str(source_id), "field": field, "message": message})

    def as_dict(self) -> dict:
        return {"created": self.created, "updated": self.updated, "skipped": self.skipped, "violations": self.violations}


class Skip(Exception):
    """A row that cannot be imported (the reason is already in the report)."""


def checksum(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, cls=DjangoJSONEncoder, sort_keys=True, default=str).encode()).hexdigest()


def _dt(value):
    if value in (None, ""):
        return None
    return parse_datetime(value) if isinstance(value, str) else value


def _date(value):
    if value in (None, ""):
        return None
    return parse_date(value) if isinstance(value, str) else value


def mapped_id(system: str, table: str, source_id) -> int | None:
    return LegacyMap.objects.filter(source_system=system, source_table=table, source_id=str(source_id)).values_list("target_id", flat=True).first()


def _user(source_id):
    """The platform user imported from a CMS admin user (accounts importer), if any."""
    if source_id in (None, ""):
        return None
    target = mapped_id(CMS, "accounts_admin_user", source_id)
    return get_user_model().all_objects.filter(pk=target).first() if target else None


def _upsert(model, *, system: str, table: str, source_id, values: dict, report: Report, created_at=None, updated_at=None):
    """Insert (tracked in ``core_legacy_map``) or update the mapped row; returns the row or ``None`` when skipped."""
    target_id = mapped_id(system, table, source_id)
    based = issubclass(model, BaseModel)  # the append-only timeline (no base) has no soft delete, version or updated_at
    manager = model.all_objects if based else model.objects
    if target_id is not None:
        row = manager.filter(pk=target_id).first()
        if row is None or (based and row.deleted_at is not None and "deleted_at" not in values):
            report.skipped += 1
            return None
        changed = {name: value for name, value in values.items() if getattr(row, name) != value}
        if not changed:
            report.skipped += 1
            return row
        columns = dict(changed)
        if based:
            columns.update(version=F("version") + 1, updated_at=updated_at or timezone.now())
        manager.filter(pk=row.pk).update(**columns)
        report.updated += 1
        return manager.get(pk=row.pk)
    row = model(**values)
    if created_at is not None:
        row.created_at = created_at
    if based:
        row.updated_at = updated_at or created_at or timezone.now()
    model.objects.bulk_create([row])  # bypasses save(): source timestamps survive
    LegacyMap.objects.create(source_system=system, source_table=table, source_id=str(source_id), target_table=model._meta.db_table, target_id=row.pk)
    report.created += 1
    return row


def _run(rows: Iterable[dict], handle: Callable[[dict, Report], None], *, object_type: str, source: str, user) -> dict:
    rows = list(rows)
    report = Report()
    for row in rows:
        try:
            with transaction.atomic():
                handle(row, report)
        except Skip:
            continue
        except IntegrityError as exc:
            report.violation(row.get("id"), "row", f"rejected by the database: {exc.__class__.__name__}: {str(exc).splitlines()[0]}")
    result = report.as_dict()
    record(
        "careers.legacy_imported",
        object_type=object_type,
        actor=user,
        actor_kind=None if user else "SYSTEM",
        after={"source": source, "rows": len(rows), "checksum": checksum(rows), **{key: value for key, value in result.items() if key != "violations"}, "violations": len(result["violations"])},
    )
    bump("careers:positions")
    return result


# ----------------------------------------------------------------------------------------------------------------
# CMS: departments and positions
# ----------------------------------------------------------------------------------------------------------------
def import_departments(rows: Iterable[dict], *, user=None) -> dict:
    """CMS ``careers_department`` → ``careers_department`` (copy)."""

    def handle(row: dict, report: Report) -> None:
        values = {
            "name": (row.get("name") or "").strip(),
            "slug": (row.get("slug") or "").strip(),
            "description": row.get("description") or "",
            "is_active": bool(row.get("is_active", True)),
            "sort_order": int(row.get("sort_order") or 0),
        }
        if not values["name"] or not values["slug"]:
            report.violation(row.get("id"), "name", "name and slug are required")
            raise Skip
        _upsert(Department, system=CMS, table="careers_department", source_id=row["id"], values=values, report=report, created_at=_dt(row.get("created_at")), updated_at=_dt(row.get("updated_at")))

    return _run(rows, handle, object_type="careers.department", source="CMS careers_department", user=user)


def import_positions(rows: Iterable[dict], *, user=None) -> dict:
    """CMS ``careers_job_position`` → ``careers_job_position``: status map draft/published/closed/archived."""

    def handle(row: dict, report: Report) -> None:
        source_id = row.get("id")
        status = POSITION_STATUS.get((row.get("status") or "").lower())
        if status is None:
            report.violation(source_id, "status", f"unknown status {row.get('status')!r}")
            raise Skip
        department_id = mapped_id(CMS, "careers_department", row.get("department_id"))
        department = Department.all_objects.filter(pk=department_id).first() if department_id else None
        if department is None:
            report.violation(source_id, "department_id", f"department {row.get('department_id')} was not imported")
            raise Skip
        employment_type = row.get("employment_type") or JobPosition.EmploymentType.FULL_TIME
        if employment_type not in JobPosition.EmploymentType.values:
            report.violation(source_id, "employment_type", f"unknown employment type {employment_type!r}")
            raise Skip
        og_image = None
        if row.get("og_image_id"):
            asset_id = mapped_id(CMS, "media_asset", row["og_image_id"])
            og_image = MediaAsset.all_objects.filter(pk=asset_id).first() if asset_id else None
            if og_image is None:
                report.violation(source_id, "og_image_id", f"media asset {row['og_image_id']} was not imported; imported without an OG image")
        schema_extra = row.get("schema_extra") or {}
        if isinstance(schema_extra, str):
            schema_extra = json.loads(schema_extra or "{}")
        values = {
            "title": row.get("title") or "",
            "slug": row.get("slug") or "",
            "department": department,
            "location": row.get("location") or "",
            "employment_type": employment_type,
            "experience_required": row.get("experience_required") or "",
            "description": row.get("description") or "",
            "responsibilities": row.get("responsibilities") or "",
            "requirements": row.get("requirements") or "",
            "benefits": row.get("benefits") or "",
            "application_instructions": row.get("application_instructions") or "",
            "application_deadline": _date(row.get("application_deadline")),
            "status": status,
            "sort_order": int(row.get("sort_order") or 0),
            "published_at": _dt(row.get("published_at")),
            "closed_at": _dt(row.get("closed_at")),
            "seo_title": row.get("seo_title") or "",
            "meta_description": row.get("meta_description") or "",
            "canonical_url": row.get("canonical_url") or "",
            "schema_type": row.get("schema_type") or "none",
            "schema_extra": schema_extra,
            "noindex": bool(row.get("noindex")),
            "og_image": og_image,
            "created_by": _user(row.get("created_by_id")),
            "updated_by": _user(row.get("updated_by_id")),
        }
        target = mapped_id(CMS, "careers_job_position", source_id)
        clash = JobPosition.objects.filter(slug=values["slug"]).exclude(pk=target).exists() if values["slug"] else False
        if clash:
            report.violation(source_id, "slug", f"a live position already uses the slug {values['slug']!r}")
            raise Skip
        _upsert(JobPosition, system=CMS, table="careers_job_position", source_id=source_id, values=values, report=report, created_at=_dt(row.get("created_at")), updated_at=_dt(row.get("updated_at")))

    return _run(rows, handle, object_type="careers.jobposition", source="CMS careers_job_position", user=user)


# ----------------------------------------------------------------------------------------------------------------
# Main backend: applications, notes, events
# ----------------------------------------------------------------------------------------------------------------
def _store_file(read_file, path: str, *, candidate: str, field: str, current: MediaAsset | None, source_id, report: Report) -> MediaAsset | None:
    """The asset for ``path``: the current one when the bytes are unchanged, else a new private upload."""
    if not path:
        return None
    data = read_file(path)
    if data is None:
        report.violation(source_id, field, f"file {path!r} is missing from the legacy media volume; imported without it")
        return current
    digest = hashlib.sha256(data).hexdigest()
    if current is not None and current.checksum_sha256 == digest:
        return current
    buffer = io.BytesIO(data)
    buffer.name = path.rsplit("/", 1)[-1]
    buffer.size = len(data)
    try:
        asset = store_candidate_file(buffer, candidate=candidate, kind="resume" if field == "resume" else "portfolio")
    except DomainError as exc:
        report.violation(source_id, field, f"file {path!r} refused ({exc.code}); imported without it")
        return current
    if asset.checksum_sha256 != digest:  # pragma: no cover - private files are stored byte for byte
        report.violation(source_id, field, "checksum mismatch after copy")
    return asset


def import_applications(rows: Iterable[dict], *, read_file: Callable[[str], bytes | None], user=None) -> dict:
    """Main backend ``job_application`` → ``careers_job_application`` (+ private resume/portfolio media).

    The legacy ``archived_at`` is the soft delete, so a re-import applies the legacy queue's archive/restore — unless
    a platform user archived or restored the application since: then the platform's state is kept (and an
    application archived in the platform is skipped, as everywhere else).
    """

    def handle(row: dict, report: Report) -> None:
        source_id = row.get("id")
        status = APPLICATION_STATUS.get((row.get("status") or "new").lower())
        if status is None:
            report.violation(source_id, "status", f"unknown status {row.get('status')!r}")
            raise Skip
        mobile = validation.indian_mobile(str(row.get("phone") or ""))
        if mobile is None:
            report.violation(source_id, "phone", "not a 10-digit Indian mobile number")
            raise Skip
        position = None
        if row.get("position_id") not in (None, ""):
            target = mapped_id(CMS, "careers_job_position", row["position_id"])
            position = JobPosition.all_objects.filter(pk=target).first() if target else None
            if position is None:
                report.violation(source_id, "position_id", f"CMS position {row['position_id']} was not imported; the snapshot is kept without a link")
        values = {
            "position": position,
            "position_label": row.get("position") or "General application",
            "position_title": row.get("position_title") or "",
            "department_name": row.get("department_name") or "",
            "status": status,
            "status_changed_at": _dt(row.get("status_changed_at")),
            "deleted_at": _dt(row.get("archived_at")),
            "source": JobApplication.Source.IMPORT,
            "name": (row.get("full_name") or "").strip(),
            "email": (row.get("email") or "").strip().lower(),
            "phone_e164": validation.to_e164(mobile),
            "location": row.get("location") or "",
            "linkedin": row.get("linkedin") or "",
            "portfolio_website": row.get("portfolio_website") or "",
            "current_company": row.get("current_company") or "",
            "current_role": row.get("current_role") or "",
            "availability": row.get("availability") or "",
            "cover_letter": row.get("cover_note") or "",
            "declaration_accepted": bool(row.get("declaration_accepted")),
        }
        for name, choices in CHOICE_FIELDS.items():
            value = row.get(name) or ""
            if value and value not in choices.values:
                report.violation(source_id, name, f"unknown choice {value!r}; imported blank")
                value = ""
            values[name] = value
        existing_id = mapped_id(BACKEND, "job_application", source_id)
        existing = JobApplication.all_objects.filter(pk=existing_id).select_related("resume", "portfolio").first() if existing_id else None
        if existing is not None and _archived_in_platform(existing) is not None:
            # A platform user archived/restored it after the import: that decision wins over the legacy archive flag,
            # and a row archived in the platform stays archived (skipped, like every other importer).
            values.pop("deleted_at")
            if existing.deleted_at is not None:
                report.skipped += 1
                return
        values["resume"] = _store_file(read_file, row.get("resume") or "", candidate=values["name"], field="resume", current=existing.resume if existing else None, source_id=source_id, report=report)
        values["portfolio"] = _store_file(
            read_file, row.get("portfolio_file") or "", candidate=values["name"], field="portfolio_file", current=existing.portfolio if existing else None, source_id=source_id, report=report
        )
        if row.get("resume") in (None, ""):
            report.violation(source_id, "resume", "no resume on the legacy row")
        created_at = _dt(row.get("created_at"))
        _upsert(JobApplication, system=BACKEND, table="job_application", source_id=source_id, values=values, report=report, created_at=created_at, updated_at=created_at)
        if existing is not None:
            for field in ("resume", "portfolio"):
                old = getattr(existing, field)
                if old is not None and values[field] is not None and old.pk != values[field].pk:
                    media_assets.delete_asset(old, user=None)

    return _run(rows, handle, object_type="careers.jobapplication", source="BACKEND job_application", user=user)


def _archived_in_platform(application: JobApplication) -> JobApplicationEvent | None:
    """The latest archive/restore a platform user made (imported timeline rows carry no ``actor``), if any."""
    kinds = (JobApplicationEvent.Kind.ARCHIVED, JobApplicationEvent.Kind.RESTORED)
    return application.events.filter(kind__in=kinds, actor__isnull=False).order_by("-created_at", "-id").first()


def _application(source_id, report: Report, row_id) -> JobApplication:
    target = mapped_id(BACKEND, "job_application", source_id)
    application = JobApplication.all_objects.filter(pk=target).first() if target else None
    if application is None:
        report.violation(row_id, "application_id", f"application {source_id} was not imported")
        raise Skip
    return application


def import_application_notes(rows: Iterable[dict], *, user=None) -> dict:
    """Main backend ``job_application_note`` → ``careers_job_application_note`` (``author`` kept as ``author_name``)."""

    def handle(row: dict, report: Report) -> None:
        application = _application(row.get("application_id"), report, row.get("id"))
        body = (row.get("body") or "").strip()
        if not body:
            report.violation(row.get("id"), "body", "empty note")
            raise Skip
        values = {"application": application, "author_name": (row.get("author") or "")[:150], "body": body}
        _upsert(JobApplicationNote, system=BACKEND, table="job_application_note", source_id=row["id"], values=values, report=report, created_at=_dt(row.get("created_at")))

    return _run(rows, handle, object_type="careers.jobapplicationnote", source="BACKEND job_application_note", user=user)


def import_application_events(rows: Iterable[dict], *, user=None) -> dict:
    """Main backend ``job_application_event`` → ``careers_job_application_event`` (kinds and statuses mapped)."""

    def status(value) -> str:
        return APPLICATION_STATUS.get((value or "").lower(), "") if value else ""

    def handle(row: dict, report: Report) -> None:
        application = _application(row.get("application_id"), report, row.get("id"))
        kind = EVENT_KIND.get((row.get("kind") or "").lower())
        if kind is None:
            report.violation(row.get("id"), "kind", f"unknown event kind {row.get('kind')!r}")
            raise Skip
        values = {
            "application": application,
            "kind": kind,
            "from_status": status(row.get("from_status")),
            "to_status": status(row.get("to_status")),
            "detail": (row.get("detail") or "")[:255],
            "actor_name": (row.get("actor") or "")[:150],
            "created_at": _dt(row.get("created_at")) or timezone.now(),
        }
        _upsert(JobApplicationEvent, system=BACKEND, table="job_application_event", source_id=row["id"], values=values, report=report)

    return _run(rows, handle, object_type="careers.jobapplicationevent", source="BACKEND job_application_event", user=user)


__all__ = ["import_application_events", "import_application_notes", "import_applications", "import_departments", "import_positions"]
