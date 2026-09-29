"""Job applications: the public submission and the staff workflow (``careers/applications/``).

Submission (``POST /api/public/v1/job-applications/``, anonymous):

* the posting (``position_id`` = the posting's ``uid``) must exist and be ``PUBLISHED`` (400 ``position_not_open``);
  its title and department are snapshotted server-side. Without a posting it is a general application;
* the resume (required) and portfolio (optional) are stored as **PRIVATE** ``RESUME`` media in the reserved folder
  ``careers/applications`` (type sniffed from the bytes: PDF, DOC, DOCX; ≤ 10 MB), named after the candidate
  (``Anu_Thomas_Resume.pdf``) so downloads keep the legacy file names. Stored files are removed again if the row
  cannot be written;
* the row, its ``RECEIVED`` timeline entry, an audit row and ``careers.application_received`` commit together.

Staff workflow (each accepts ``expected_version``; archived rows accept only ``restore/``, 409 ``application_archived``):
``status/`` (``JobApplication.TRANSITIONS``, 409 ``invalid_status_transition``), ``assign/`` (link a posting and/or
set the assignee), ``notes/``, ``DELETE`` = archive (soft delete), ``restore/``, ``download/<kind>/`` (a 10-minute
signed URL for the private file). Every change is audited and lands in the timeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from accounts.services.authz import can
from audit.services import changes, record, snapshot
from careers.models import GENERAL_APPLICATION, JobApplication, JobApplicationEvent, JobApplicationNote, JobPosition
from careers.services.validation import download_name
from core.errors import Conflict, DomainError, NotFound
from core.models import actor_or_none
from core.outbox import emit
from core.services import check_version, stamp_create
from media.models import MediaAsset
from media.services import assets as media_assets
from media.services import signing
from media.services.sniffing import sniff

logger = logging.getLogger("flarize.careers")

MEDIA_FOLDER = "careers/applications"
# download kind → (JobApplication attribute, file-name label, public form field)
FILE_KINDS = {"resume": ("resume", "Resume", "resume"), "portfolio": ("portfolio", "Portfolio", "portfolio_file")}
Status = JobApplication.Status
Kind = JobApplicationEvent.Kind

SNAPSHOT_FIELDS = ("status", "position", "position_title", "department_name", "assignee")
CANDIDATE_FIELDS = (
    "position_label",
    "position_title",
    "department_name",
    "name",
    "email",
    "phone_e164",
    "location",
    "linkedin",
    "portfolio_website",
    "current_company",
    "current_role",
    "total_experience",
    "relevant_experience",
    "current_salary",
    "expected_salary",
    "notice_period",
    "heard_about_us",
    "availability",
    "cover_letter",
    "declaration_accepted",
)


def applications_queryset(*, include_archived: bool = False):
    manager = JobApplication.all_objects if include_archived else JobApplication.objects
    return manager.select_related("position", "position__department", "assignee", "resume", "portfolio").order_by("-created_at", "-id")


def application_snapshot(application: JobApplication) -> dict:
    return snapshot(application, SNAPSHOT_FIELDS)


def actor_name(user) -> str:
    actor = actor_or_none(user)
    return actor.get_full_name()[:150] if actor else ""


def add_event(application: JobApplication, kind: str, *, user=None, from_status: str = "", to_status: str = "", detail: str = "") -> JobApplicationEvent:
    return JobApplicationEvent.objects.create(
        application=application,
        kind=kind,
        from_status=from_status,
        to_status=to_status,
        detail=(detail or "")[:255],
        actor=actor_or_none(user),
        actor_name=actor_name(user),
    )


# ----------------------------------------------------------------------------------------------------------------
# Public submission
# ----------------------------------------------------------------------------------------------------------------
def _not_open() -> DomainError:
    return DomainError("position_not_open", "This position is not accepting applications.", errors={"position_id": ["This position is not accepting applications."]})


def open_position(uid) -> JobPosition:
    position = JobPosition.objects.select_related("department").filter(uid=uid).first()
    if position is None or position.status != JobPosition.Status.PUBLISHED:
        raise _not_open()
    return position


def _extension(file, fallback_name: str) -> str:
    """The extension the stored file will carry: from the content when recognisable, else the client's name."""
    try:
        return sniff(file).extension
    except Exception:  # noqa: BLE001 - only a naming hint; media.upload validates the content for real
        name = (fallback_name or "").lower()
        return name[name.rfind(".") :] if "." in name else ""
    finally:
        file.seek(0)


def store_candidate_file(file, *, candidate: str, kind: str, user=None) -> MediaAsset:
    """Store one upload (``kind`` resume/portfolio) as a private RESUME asset named after the candidate.

    Media errors (type sniffed from the content, size) are keyed by the public form field (``resume`` /
    ``portfolio_file``).
    """
    _, label, field = FILE_KINDS[kind]
    name = download_name(candidate, label, _extension(file, getattr(file, "name", "")))
    try:
        return media_assets.upload(user=user, file=file, visibility=MediaAsset.Visibility.PRIVATE, kind=MediaAsset.Kind.RESUME, folder=MEDIA_FOLDER, original_filename=name, allow_reserved=True)
    except DomainError as exc:
        messages = [message for values in (exc.errors or {}).values() for message in values] or [exc.message]
        raise DomainError(exc.code, exc.message, status=exc.status, errors={field: messages}) from None


def _discard(stored: list[MediaAsset]) -> None:
    for asset in stored:
        try:
            media_assets.delete_asset(asset, user=None)
        except Exception:  # noqa: BLE001 - best effort; the asset stays private and unreferenced
            logger.warning("could not discard an application upload", extra={"asset": str(asset.uid)}, exc_info=True)


def submit_application(*, data: dict, resume, portfolio=None, ip: str | None = None) -> JobApplication:
    """Create an application from validated public form data (see the module docstring)."""
    position = open_position(data["position_uid"]) if data.get("position_uid") else None
    stored: list[MediaAsset] = []
    try:
        resume_asset = store_candidate_file(resume, candidate=data["name"], kind="resume")
        stored.append(resume_asset)
        portfolio_asset = None
        if portfolio is not None:
            portfolio_asset = store_candidate_file(portfolio, candidate=data["name"], kind="portfolio")
            stored.append(portfolio_asset)
        return _create_application(data=data, position=position, resume=resume_asset, portfolio=portfolio_asset, ip=ip)
    except BaseException:
        _discard(stored)
        raise


@transaction.atomic
def _create_application(*, data: dict, position: JobPosition | None, resume: MediaAsset, portfolio: MediaAsset | None, ip: str | None) -> JobApplication:
    values = {name: data[name] for name in CANDIDATE_FIELDS if name in data}
    values["position_label"] = values.get("position_label") or GENERAL_APPLICATION
    if position is not None and not JobPosition.objects.select_for_update().filter(pk=position.pk, status=JobPosition.Status.PUBLISHED).exists():
        raise _not_open()  # unpublished while the files were being stored (the caller discards them)
    if position is not None:
        values["position_title"] = position.title
        values["department_name"] = position.department.name
    application = JobApplication(**values, position=position, resume=resume, portfolio=portfolio, ip=ip or None, source=JobApplication.Source.WEBSITE)
    stamp_create(application, None)
    application.save()
    add_event(application, Kind.RECEIVED, detail=application.display_position)
    record("careers.application_received", obj=application, actor=None, actor_kind="SYSTEM", after={"position": str(position.uid) if position else None, "source": application.source})
    emit(
        "careers.application_received",
        {"application_uid": str(application.uid), "position_uid": str(position.uid) if position else None, "display_position": application.display_position},
        aggregate_type="careers.jobapplication",
        aggregate_uid=application.uid,
    )
    return application


# ----------------------------------------------------------------------------------------------------------------
# Staff workflow
# ----------------------------------------------------------------------------------------------------------------
def _locked(instance: JobApplication, expected_version, *, allow_archived: bool = False) -> JobApplication:
    application = JobApplication.all_objects.select_for_update().get(pk=instance.pk)
    check_version(application, expected_version)
    if application.deleted_at is not None and not allow_archived:
        raise Conflict("application_archived", "This application is archived; restore it first.")
    return application


@transaction.atomic
def change_status(instance: JobApplication, *, user, status: str, note: str = "", expected_version=None) -> JobApplication:
    application = _locked(instance, expected_version)
    if status == application.status:
        return application
    if status not in application.allowed_transitions():
        allowed = ", ".join(application.allowed_transitions()) or "none"
        raise Conflict("invalid_status_transition", f"Cannot move from {application.status} to {status}. Allowed: {allowed}.", errors={"status": [f"Allowed: {allowed}."]})
    before = application.status
    application.versioned_update(user, status=status, status_changed_at=timezone.now())
    add_event(application, Kind.STATUS, user=user, from_status=before, to_status=status, detail=note)
    record("careers.application_status_changed", obj=application, actor=user, before={"status": before}, after={"status": status}, note=note)
    emit(
        "careers.application_status_changed",
        {"application_uid": str(application.uid), "from_status": before, "to_status": status},
        aggregate_type="careers.jobapplication",
        aggregate_uid=application.uid,
    )
    return application


_UNSET = object()


@transaction.atomic
def assign(instance: JobApplication, *, user, position=_UNSET, assignee=_UNSET, expected_version=None) -> JobApplication:
    """Link a posting (snapshotting its title/department; the submitted ``position_label`` is kept) and/or set the assignee."""
    application = _locked(instance, expected_version)
    before = application_snapshot(application)
    values = {}
    if position is not _UNSET:
        if position is None or position.deleted_at is not None:
            raise DomainError("validation_error", "Unknown position.", errors={"position_uid": ["This position does not exist."]})
        if position.pk != application.position_id or application.position_title != position.title:
            values.update(position=position, position_title=position.title, department_name=position.department.name)
    if assignee is not _UNSET and (assignee.pk if assignee else None) != application.assignee_id:
        if assignee is not None and (not assignee.is_active or assignee.deleted_at is not None or not can(assignee, "applications", "view")):
            raise DomainError("invalid_assignee", "The assignee must be an active user who can view applications.", errors={"assignee_uid": ["Cannot view applications."]})
        values["assignee"] = assignee
    if not values:
        return application
    application.versioned_update(user, **values)
    if "position" in values:
        add_event(application, Kind.ASSIGNED, user=user, detail=application.position_title)
    if "assignee" in values:
        add_event(application, Kind.ASSIGNEE, user=user, detail=assignee.get_full_name() if assignee else "Unassigned")
    changed_before, changed_after = changes(before, application_snapshot(application))
    record("careers.application_assigned", obj=application, actor=user, before=changed_before, after=changed_after)
    return application


@transaction.atomic
def add_note(instance: JobApplication, *, user, body: str) -> JobApplicationNote:
    application = _locked(instance, None)
    body = (body or "").strip()
    if not body:
        raise DomainError("validation_error", "Write something first.", errors={"body": ["Write something first."]})
    note = JobApplicationNote(application=application, body=body, author_name=actor_name(user))
    stamp_create(note, user)
    note.save()
    add_event(application, Kind.NOTE, user=user)
    record("careers.application_note_added", obj=application, actor=user, after={"note": str(note.uid)})
    return note


@transaction.atomic
def archive_application(instance: JobApplication, *, user, expected_version=None) -> None:
    application = _locked(instance, expected_version, allow_archived=True)
    if application.deleted_at is not None:
        return
    application.soft_delete(user)
    add_event(application, Kind.ARCHIVED, user=user)
    record("careers.application_archived", obj=application, actor=user, before={"archived": False}, after={"archived": True})


@transaction.atomic
def restore_application(instance: JobApplication, *, user, expected_version=None) -> JobApplication:
    application = _locked(instance, expected_version, allow_archived=True)
    if application.deleted_at is None:
        return application
    application.restore(user)
    add_event(application, Kind.RESTORED, user=user)
    record("careers.application_restored", obj=application, actor=user, before={"archived": True}, after={"archived": False})
    return application


@dataclass(frozen=True)
class DownloadLink:
    url: str
    expires_at: datetime | None
    filename: str
    mime_type: str
    size_bytes: int


def download_link(application: JobApplication, kind: str, *, version: str, absolute=None) -> DownloadLink:
    """A short-lived signed URL (``media/download/<token>/``) for the resume or portfolio of ``application``."""
    if kind not in FILE_KINDS:
        raise NotFound("not_found", "Unknown file kind.")
    asset = getattr(application, FILE_KINDS[kind][0])
    if asset is None or asset.deleted_at is not None:
        raise NotFound("file_not_found", "This application has no such file.")
    link = signing.signed_url(asset, version=version, absolute=absolute)
    return DownloadLink(url=link.url, expires_at=link.expires_at, filename=asset.original_filename, mime_type=asset.mime_type, size_bytes=asset.size_bytes)
