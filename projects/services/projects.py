"""Project records and their lifecycle (PLAN §2.6 ``projects_project``, §3.4 Projects; D-5 minimal).

* ``create``/``update``/``delete`` (soft; only a PLANNED or CANCELLED project may be archived);
* ``set_cost_inputs`` (``pricing_internal``): the Flarize ``costInputs`` document, editable while the project is open;
* ``commission`` (IN_PROGRESS → COMMISSIONED), ``close`` (COMMISSIONED → CLOSED), ``cancel`` (PLANNED/IN_PROGRESS →
  CANCELLED, reason required); ``lock-bom/`` lives in :mod:`projects.services.bom_lock`;
* ``create_from_inspection``: the optional auto-create on ``site_inspections.released`` (D-8, setting
  ``PROJECTS_AUTO_CREATE_ON_RELEASE``), idempotent per inspection.

Every write is versioned (``expected_version`` → 409 ``stale_version``), audited, bumps the ``projects`` cache
namespace and emits ``projects.<event>`` on the outbox.
"""

from __future__ import annotations

import datetime as dt
import json

from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.services import changes, record, snapshot
from core.errors import Conflict, DomainError
from core.outbox import emit
from core.sequences import next_number
from core.services import check_version, stamp_create
from flarize.cache_utils import bump
from projects.models import Project, ProjectStatus

CACHE_NAMESPACE = "projects"
MODULE = "projects"
OPEN = (ProjectStatus.PLANNED, ProjectStatus.IN_PROGRESS)
FINISHED = (ProjectStatus.CLOSED, ProjectStatus.CANCELLED)
EDITABLE_FIELDS = (
    "title",
    "head",
    "scheduled_on",
    "kseb_status",
    "note",
    "quotation_version_uid",
    "agreement_uid",
    "site_inspection_uid",
    "lead_uid",
    "system_type",
    "tier",
    "size_kw",
    "phase",
)
#: Fixed once the BOM is locked (Flarize refused configuration changes after the lock).
SYSTEM_FIELDS = ("system_type", "tier", "size_kw", "phase")
SNAPSHOT_FIELDS = ("number", "customer", "status", *EDITABLE_FIELDS, "commissioned_on", "cancel_reason")


def projects_queryset():
    return Project.objects.select_related("customer", "head", "bom_locked_by", "engineering_run", "created_by", "updated_by")


def project_snapshot(project: Project) -> dict:
    return snapshot(project, SNAPSHOT_FIELDS)


def next_project_number() -> str:
    return next_number("PROJ")


def _json(value) -> dict:
    """Decimals/dates as JSON text (the stored document is plain JSON)."""
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def emit_project_event(event: str, project: Project, **extra) -> None:
    payload = {"project_uid": str(project.uid), "number": project.number, "customer_uid": str(project.customer.uid), "status": project.status, **extra}
    emit(f"projects.{event}", payload, aggregate_type="projects.project", aggregate_uid=project.uid)


def _locked(project: Project, expected_version) -> Project:
    locked = Project.objects.select_for_update().select_related("customer").get(pk=project.pk)
    check_version(locked, expected_version)
    return locked


def _transition(project: Project, allowed: tuple, verb: str) -> None:
    if project.status not in allowed:
        raise Conflict("invalid_transition", f"A {project.get_status_display().lower()} project cannot be {verb}.", errors={"status": [project.status]})


def _conflict_on_inspection(site_inspection_uid) -> Conflict:
    return Conflict("inspection_has_project", "A live project already exists for this site inspection.", errors={"site_inspection_uid": [str(site_inspection_uid)]})


def _save_new(project: Project) -> None:
    try:
        with transaction.atomic():
            project.save()
    except IntegrityError:
        raise _conflict_on_inspection(project.site_inspection_uid) from None


@transaction.atomic
def create_project(*, user, data: dict) -> Project:
    values = {name: data[name] for name in EDITABLE_FIELDS if data.get(name) is not None}
    if values.get("site_inspection_uid") and Project.objects.filter(site_inspection_uid=values["site_inspection_uid"]).exclude(status=ProjectStatus.CANCELLED).exists():
        raise _conflict_on_inspection(values["site_inspection_uid"])
    project = Project(number=next_project_number(), customer=data["customer"], **values)
    stamp_create(project, user)
    _save_new(project)
    record("projects.project_created", obj=project, actor=user, after=project_snapshot(project))
    emit_project_event("created", project, source="staff")
    bump(CACHE_NAMESPACE)
    return project


@transaction.atomic
def update_project(instance: Project, *, user, data: dict, expected_version=None) -> Project:
    project = _locked(instance, expected_version)
    values = {name: data[name] for name in EDITABLE_FIELDS if name in data and data[name] != getattr(project, name)}
    if "customer" in data and data["customer"].pk != project.customer_id:
        values["customer"] = data["customer"]
    if not values:
        return project
    if project.status in FINISHED:
        raise Conflict("project_finished", f"A {project.get_status_display().lower()} project cannot be edited.", errors={"status": [project.status]})
    if project.bom_lock is not None:
        frozen = sorted(name for name in values if name in (*SYSTEM_FIELDS, "customer"))
        if frozen:
            raise Conflict("bom_locked", "The BOM is locked: the customer and the system (type, tier, size, phase) can no longer change.", errors={name: ["Locked with the BOM."] for name in frozen})
    if values.get("site_inspection_uid") and Project.objects.filter(site_inspection_uid=values["site_inspection_uid"]).exclude(pk=project.pk).exclude(status=ProjectStatus.CANCELLED).exists():
        raise _conflict_on_inspection(values["site_inspection_uid"])
    before = project_snapshot(project)
    try:
        with transaction.atomic():
            project.versioned_update(user, **values)
    except IntegrityError:
        raise _conflict_on_inspection(values.get("site_inspection_uid")) from None
    changed_before, changed_after = changes(before, project_snapshot(project))
    record("projects.project_updated", obj=project, actor=user, before=changed_before, after=changed_after)
    emit_project_event("updated", project, fields=sorted(changed_after))
    bump(CACHE_NAMESPACE)
    return project


@transaction.atomic
def delete_project(instance: Project, *, user, expected_version=None) -> None:
    project = _locked(instance, expected_version)
    if project.status not in (ProjectStatus.PLANNED, ProjectStatus.CANCELLED):
        raise Conflict("project_not_deletable", "Only a planned or cancelled project can be archived; cancel it first.", errors={"status": [project.status]})
    project.soft_delete(user)
    record("projects.project_deleted", obj=project, actor=user, before=project_snapshot(project))
    emit_project_event("deleted", project)
    bump(CACHE_NAMESPACE)


@transaction.atomic
def set_cost_inputs(instance: Project, *, user, inputs: dict, expected_version=None) -> Project:
    project = _locked(instance, expected_version)
    if project.status not in OPEN:
        raise Conflict("project_not_open", "Cost inputs can only change while the project is planned or in progress.", errors={"status": [project.status]})
    document = _json(inputs)
    if document == project.cost_inputs:
        return project
    before = project.cost_inputs
    project.versioned_update(user, cost_inputs=document)
    record("projects.cost_inputs_set", obj=project, actor=user, before={"cost_inputs": before}, after={"cost_inputs": document})
    emit_project_event("cost_inputs_set", project)
    bump(CACHE_NAMESPACE)
    return project


@transaction.atomic
def commission(instance: Project, *, user, commissioned_on: dt.date | None = None, kseb_status: str | None = None, note: str = "", expected_version=None) -> Project:
    project = _locked(instance, expected_version)
    _transition(project, (ProjectStatus.IN_PROGRESS,), "commissioned")
    day = commissioned_on or timezone.localdate()
    if day > timezone.localdate():
        raise DomainError("validation_error", "The commissioning date cannot be in the future.", errors={"commissioned_on": ["Cannot be in the future."]})
    if project.bom_locked_at and day < timezone.localtime(project.bom_locked_at).date():
        raise DomainError("validation_error", "The commissioning date cannot precede the BOM lock.", errors={"commissioned_on": ["Before the BOM was locked."]})
    values = {"status": ProjectStatus.COMMISSIONED, "commissioned_on": day, "commissioned_at": timezone.now()}
    if kseb_status:
        values["kseb_status"] = kseb_status
    if note:
        values["note"] = note
    project.versioned_update(user, **values)
    record("projects.project_commissioned", obj=project, actor=user, after={"commissioned_on": day.isoformat(), "kseb_status": project.kseb_status, "note": note})
    emit_project_event("commissioned", project, commissioned_on=day.isoformat())
    bump(CACHE_NAMESPACE)
    return project


@transaction.atomic
def close(instance: Project, *, user, note: str = "", expected_version=None) -> Project:
    project = _locked(instance, expected_version)
    _transition(project, (ProjectStatus.COMMISSIONED,), "closed")
    values = {"status": ProjectStatus.CLOSED, "closed_at": timezone.now()}
    if note:
        values["note"] = note
    project.versioned_update(user, **values)
    record("projects.project_closed", obj=project, actor=user, after={"note": note})
    emit_project_event("closed", project)
    bump(CACHE_NAMESPACE)
    return project


@transaction.atomic
def cancel(instance: Project, *, user, reason: str, expected_version=None) -> Project:
    project = _locked(instance, expected_version)
    reason = (reason or "").strip()
    if not reason:
        raise DomainError("validation_error", "A reason is required.", errors={"reason": ["This field may not be blank."]})
    _transition(project, OPEN, "cancelled")
    previous = project.status
    project.versioned_update(user, status=ProjectStatus.CANCELLED, cancelled_at=timezone.now(), cancel_reason=reason)
    record("projects.project_cancelled", obj=project, actor=user, before={"status": previous}, after={"status": project.status, "reason": reason})
    emit_project_event("cancelled", project, reason=reason)
    bump(CACHE_NAMESPACE)
    return project


@transaction.atomic
def create_from_inspection(*, customer, inspection_uid, lead_uid=None, agreement_uid=None, system_type: str = "", size_kw=None, phase: str = "", released_at=None) -> tuple[Project, bool]:
    """The project of a released site inspection, created once (``(project, created)``); the actor is the system."""
    existing = Project.objects.filter(site_inspection_uid=inspection_uid).exclude(status=ProjectStatus.CANCELLED).first()
    if existing is not None:
        return existing, False
    project = Project(
        number=next_project_number(),
        customer=customer,
        site_inspection_uid=inspection_uid,
        lead_uid=lead_uid,
        agreement_uid=agreement_uid,
        system_type=system_type,
        size_kw=size_kw,
        phase=phase,
        note=f"Created automatically when site inspection {inspection_uid} was released{f' at {released_at}' if released_at else ''}.",
    )
    stamp_create(project, None)
    try:
        with transaction.atomic():
            project.save()
    except IntegrityError:  # a concurrent delivery of the same event won the race
        return Project.objects.get(site_inspection_uid=inspection_uid, status__in=[s for s in ProjectStatus.values if s != ProjectStatus.CANCELLED]), False
    record("projects.project_created", obj=project, actor=None, after=project_snapshot(project), note="site_inspections.released")
    emit_project_event("created", project, source="site_inspections.released", site_inspection_uid=str(inspection_uid))
    bump(CACHE_NAMESPACE)
    return project, True
