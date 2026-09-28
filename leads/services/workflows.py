"""Staff workflow of affiliate applications and warranty requests (``leads/affiliate-applications/``,
``leads/warranty-requests/``): edit, status transitions (POST ``…/transition/``), assign (``leads.manage``), archive.

Transitions::

    affiliate:  NEW ─▶ CONTACTED ─▶ APPROVED | REJECTED;  NEW ─▶ APPROVED | REJECTED;  REJECTED ─▶ NEW (reopen)
    warranty:   NEW ─▶ IN_PROGRESS ─▶ RESOLVED ─▶ CLOSED;  NEW | IN_PROGRESS ─▶ REJECTED;
                RESOLVED ─▶ IN_PROGRESS (reopen);  REJECTED ─▶ NEW (reopen)

Everything is versioned (``expected_version``), audited (the transition note goes to the audit row) and bumps ``leads``.
"""

from __future__ import annotations

from django.db import models, transaction

from audit.services import changes, record, snapshot
from core.errors import Conflict
from core.services import check_version
from flarize.cache_utils import bump
from leads.models import AffiliateApplication, WarrantyRequest
from leads.services.leads import CACHE_NAMESPACE

AFFILIATE_FIELDS = ("full_name", "phone_e164", "email", "profession", "district")
WARRANTY_FIELDS = ("full_name", "phone_e164", "issue_type", "description", "system_details", "customer")
_A, _W = AffiliateApplication.Status, WarrantyRequest.Status
TRANSITIONS: dict[type[models.Model], dict[str, frozenset[str]]] = {
    AffiliateApplication: {
        _A.NEW: frozenset({_A.CONTACTED, _A.APPROVED, _A.REJECTED}),
        _A.CONTACTED: frozenset({_A.APPROVED, _A.REJECTED}),
        _A.APPROVED: frozenset(),
        _A.REJECTED: frozenset({_A.NEW}),
    },
    WarrantyRequest: {
        _W.NEW: frozenset({_W.IN_PROGRESS, _W.REJECTED}),
        _W.IN_PROGRESS: frozenset({_W.RESOLVED, _W.REJECTED}),
        _W.RESOLVED: frozenset({_W.CLOSED, _W.IN_PROGRESS}),
        _W.CLOSED: frozenset(),
        _W.REJECTED: frozenset({_W.NEW}),
    },
}
FIELDS = {AffiliateApplication: AFFILIATE_FIELDS, WarrantyRequest: WARRANTY_FIELDS}


LABELS = {AffiliateApplication: "affiliate_application", WarrantyRequest: "warranty_request"}


def _label(row) -> str:
    return LABELS[type(row)]


def affiliate_queryset():
    return AffiliateApplication.objects.select_related("assignee")


def warranty_queryset():
    return WarrantyRequest.objects.select_related("assignee", "customer")


def _lock(instance, expected_version):
    row = type(instance).objects.select_for_update().get(pk=instance.pk)
    check_version(row, expected_version)
    return row


@transaction.atomic
def update_record(instance, *, user, data: dict, expected_version=None):
    row = _lock(instance, expected_version)
    fields = FIELDS[type(row)]
    values = {name: data[name] for name in fields if name in data and data[name] != getattr(row, name)}
    if not values:
        return row
    before = snapshot(row, fields)
    row.versioned_update(user, **values)
    changed_before, changed_after = changes(before, snapshot(row, fields))
    record(f"leads.{_label(row)}_updated", obj=row, actor=user, before=changed_before, after=changed_after)
    bump(CACHE_NAMESPACE)
    return row


@transaction.atomic
def transition(instance, *, user, status: str, note: str = "", expected_version=None):
    row = _lock(instance, expected_version)
    if row.status == status:
        return row
    allowed = TRANSITIONS[type(row)][row.status]
    if status not in allowed:
        raise Conflict("invalid_transition", f"{row.status} cannot move to {status}.", errors={"status": [f"Allowed: {', '.join(sorted(allowed)) or 'none'}."]})
    previous = row.status
    row.versioned_update(user, status=status)
    record(f"leads.{_label(row)}_status_changed", obj=row, actor=user, before={"status": previous}, after={"status": status, "note": note})
    bump(CACHE_NAMESPACE)
    return row


@transaction.atomic
def assign(instance, *, user, assignee, expected_version=None):
    row = _lock(instance, expected_version)
    if row.assignee_id == getattr(assignee, "pk", None):
        return row
    previous = row.assignee
    row.versioned_update(user, assignee=assignee)
    record(
        f"leads.{_label(row)}_assigned",
        obj=row,
        actor=user,
        before={"assignee": str(previous.uid) if previous else None},
        after={"assignee": str(assignee.uid) if assignee else None},
    )
    bump(CACHE_NAMESPACE)
    return row


@transaction.atomic
def delete_record(instance, *, user, expected_version=None) -> None:
    row = _lock(instance, expected_version)
    row.soft_delete(user)
    record(f"leads.{_label(row)}_deleted", obj=row, actor=user, before=snapshot(row, FIELDS[type(row)]))
    bump(CACHE_NAMESPACE)
