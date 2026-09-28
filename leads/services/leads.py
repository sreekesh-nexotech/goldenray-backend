"""Staff lead workflow (PLAN §3.4 Sales ``leads/``): create, edit, assign, status, lost, spam, convert, notes, archive.

Status rules (``Lead`` docstring): NEW/CONTACTED/QUALIFIED are *open* and move freely among themselves; LOST needs a
reason; SPAM and LOST can be reopened (→ NEW); CONVERTED is terminal and only reached through :func:`convert_lead`
(or a ``quotations.issued`` event for the customer). Every change appends a ``leads_lead_event`` row, writes an audit
row, is versioned (``expected_version`` → 409 ``stale_version``) and bumps the ``leads`` cache namespace.

Conversion links the lead to the live customer with the same phone number (matched by phone only, never by name) or
creates one (needs ``customers.create``), owned by the lead's assignee (else the converting user), and emits
``leads.converted``.
"""

from __future__ import annotations

from django.db import transaction

from accounts.services.authz import can
from audit.services import changes, record, snapshot
from core.errors import Conflict, DomainError, PermissionDenied
from core.outbox import emit
from core.sequences import next_number
from core.services import check_version, stamp_create
from customers.models import Customer
from customers.services.customers import ensure_customer, find_by_phone
from flarize.cache_utils import bump
from leads.models import Lead, LeadEvent, LeadNote

CACHE_NAMESPACE = "leads"
EDITABLE_FIELDS = ("kind", "name", "phone_e164", "email", "pincode", "district", "message")
SNAPSHOT_FIELDS = ("number", "kind", "form", "name", "phone_e164", "email", "pincode", "district", "message", "status", "assignee", "customer", "lost_reason", "otp_verified_at")
OPEN = frozenset(Lead.OPEN_STATUSES)


def leads_queryset():
    return Lead.objects.select_related("assignee", "customer")


def lead_snapshot(lead: Lead) -> dict:
    return snapshot(lead, SNAPSHOT_FIELDS)


def add_event(lead: Lead, event: str, *, by=None, data: dict | None = None) -> LeadEvent:
    from core.models import actor_or_none

    return LeadEvent.objects.create(lead=lead, event=event, by=actor_or_none(by), data=data or {})


def _uid(obj) -> str | None:
    return str(obj.uid) if obj is not None else None


def _lock(instance: Lead, expected_version) -> Lead:
    lead = Lead.objects.select_for_update().get(pk=instance.pk)
    check_version(lead, expected_version)
    return lead


def created(lead: Lead, *, user, actor_kind: str | None = None, channel: str) -> None:
    """Event, audit, outbox and cache for a lead just inserted (staff or website)."""
    add_event(lead, LeadEvent.Event.CREATED, by=user, data={"channel": channel, "form": lead.form})
    record("leads.lead_created", obj=lead, actor=user, actor_kind=actor_kind, after=lead_snapshot(lead))
    emit(
        "leads.created",
        {"lead_uid": str(lead.uid), "number": lead.number, "kind": lead.kind, "form": lead.form, "channel": channel, "customer_uid": _uid(lead.customer)},
        aggregate_type="leads.lead",
        aggregate_uid=lead.uid,
        dedup_key=f"leads.created:{lead.uid}",
    )
    bump(CACHE_NAMESPACE)


@transaction.atomic
def create_lead(*, user, data: dict) -> Lead:
    """A lead entered in Studio (no OTP; ``form`` STUDIO). Assigned to its creator unless ``assignee`` says otherwise."""
    assignee = data.get("assignee", user)
    if (assignee is None or assignee.pk != user.pk) and not can(user, "leads", "manage"):
        raise PermissionDenied("assign_forbidden", "Assigning a lead to someone else needs the leads manage permission.", errors={"assignee_uid": ["Not allowed."]})
    values = {name: data[name] for name in EDITABLE_FIELDS if data.get(name) not in (None,)}
    payload = {key: data[key] for key in ("details",) if data.get(key)}
    lead = Lead(number=next_number("LEAD"), form=Lead.Form.STUDIO, payload=payload, assignee=assignee, customer=find_by_phone(values.get("phone_e164", "")), **values)
    stamp_create(lead, user)
    lead.save()
    created(lead, user=user, channel="studio")
    return lead


@transaction.atomic
def update_lead(instance: Lead, *, user, data: dict, expected_version=None) -> Lead:
    lead = _lock(instance, expected_version)
    values = {name: data[name] for name in EDITABLE_FIELDS if name in data and data[name] != getattr(lead, name)}
    if not values:
        return lead
    if "phone_e164" in values:
        values["otp_verified_at"] = None  # the verification was for the old number
    before = lead_snapshot(lead)
    lead.versioned_update(user, **values)
    changed_before, changed_after = changes(before, lead_snapshot(lead))
    add_event(lead, LeadEvent.Event.UPDATED, by=user, data={"fields": sorted(changed_after)})
    record("leads.lead_updated", obj=lead, actor=user, before=changed_before, after=changed_after)
    bump(CACHE_NAMESPACE)
    return lead


@transaction.atomic
def assign_lead(instance: Lead, *, user, assignee, expected_version=None) -> Lead:
    lead = _lock(instance, expected_version)
    if lead.assignee_id == getattr(assignee, "pk", None):
        return lead
    previous = lead.assignee
    lead.versioned_update(user, assignee=assignee)
    add_event(lead, LeadEvent.Event.ASSIGNED, by=user, data={"from": _uid(previous), "to": _uid(assignee)})
    record("leads.lead_assigned", obj=lead, actor=user, before={"assignee": _uid(previous)}, after={"assignee": _uid(assignee)})
    bump(CACHE_NAMESPACE)
    return lead


def _transition(lead: Lead, *, user, status: str, note: str = "", lost_reason: str = "") -> Lead:
    previous = lead.status
    lead.versioned_update(user, status=status, lost_reason=lost_reason)
    add_event(lead, LeadEvent.Event.STATUS_CHANGED, by=user, data={"from": previous, "to": status, **({"note": note} if note else {}), **({"lost_reason": lost_reason} if lost_reason else {})})
    record("leads.lead_status_changed", obj=lead, actor=user, before={"status": previous}, after={"status": status, "lost_reason": lost_reason, "note": note})
    bump(CACHE_NAMESPACE)
    return lead


def _refuse_converted(lead: Lead) -> None:
    if lead.status == Lead.Status.CONVERTED:
        raise Conflict("lead_converted", "This lead was converted to a customer; its status can no longer change.")


@transaction.atomic
def change_status(instance: Lead, *, user, status: str, note: str = "", expected_version=None) -> Lead:
    """Move an open lead among NEW / CONTACTED / QUALIFIED, or reopen a LOST/SPAM lead (→ NEW)."""
    lead = _lock(instance, expected_version)
    _refuse_converted(lead)
    if status not in OPEN:
        raise DomainError("invalid_status", "Use mark-lost/, mark-spam/ or convert/ for that status.", errors={"status": ["Choose NEW, CONTACTED or QUALIFIED."]})
    if lead.status == status:
        return lead
    if lead.status not in OPEN and status != Lead.Status.NEW:
        raise Conflict("invalid_transition", f"A {lead.status} lead can only be reopened as NEW.")
    return _transition(lead, user=user, status=status, note=note)


@transaction.atomic
def mark_lost(instance: Lead, *, user, reason: str, expected_version=None) -> Lead:
    lead = _lock(instance, expected_version)
    _refuse_converted(lead)
    if lead.status not in OPEN:
        raise Conflict("invalid_transition", f"A {lead.status} lead cannot be marked lost; reopen it first.")
    return _transition(lead, user=user, status=Lead.Status.LOST, lost_reason=reason)


@transaction.atomic
def mark_spam(instance: Lead, *, user, note: str = "", expected_version=None) -> Lead:
    lead = _lock(instance, expected_version)
    _refuse_converted(lead)
    if lead.status == Lead.Status.SPAM:
        return lead
    return _transition(lead, user=user, status=Lead.Status.SPAM, note=note)


def _customer_values(lead: Lead) -> dict:
    details = (lead.payload or {}).get("details") or {}
    address = details.get("Address") or details.get("address") or ""
    return {"name": lead.name, "phone_e164": lead.phone_e164, "email": lead.email, "pincode": lead.pincode, "district": lead.district, "address": str(address)[:2000]}


@transaction.atomic
def convert_lead(instance: Lead, *, user, customer: Customer | None = None, expected_version=None) -> tuple[Lead, Customer, bool]:
    """Link the lead to ``customer`` (chosen by the user), else to the customer with its phone, else a new customer."""
    lead = _lock(instance, expected_version)
    if lead.status == Lead.Status.CONVERTED:
        raise Conflict("lead_already_converted", "This lead was already converted.", errors={"customer_uid": [_uid(lead.customer) or ""]})
    if lead.status == Lead.Status.SPAM:
        raise Conflict("invalid_transition", "A spam lead cannot be converted; reopen it first.")
    was_created = False
    if customer is None:
        if not lead.phone_e164:
            raise DomainError(
                "phone_required", "The lead has no phone number to find or create its customer; choose a customer.", errors={"customer_uid": ["Required for a lead without a phone number."]}
            )
        existing = find_by_phone(lead.phone_e164)
        if existing is None and not can(user, "customers", "create"):
            raise PermissionDenied("customer_create_forbidden", "Converting this lead creates a customer, which needs the customers create permission.")
        source = Customer.Source.SALES_ENTRY if lead.form == Lead.Form.STUDIO else Customer.Source.WEBSITE
        customer, was_created = ensure_customer(user=user, values=_customer_values(lead), lead=lead, source=source, owner=lead.assignee or user)
    previous = lead.status
    lead.versioned_update(user, status=Lead.Status.CONVERTED, customer=customer, lost_reason="")
    add_event(lead, LeadEvent.Event.CONVERTED, by=user, data={"from": previous, "customer_uid": str(customer.uid), "customer_created": was_created})
    record("leads.lead_converted", obj=lead, actor=user, before={"status": previous}, after={"status": lead.status, "customer": str(customer.uid), "customer_created": was_created})
    emit(
        "leads.converted",
        {"lead_uid": str(lead.uid), "customer_uid": str(customer.uid), "customer_created": was_created},
        aggregate_type="leads.lead",
        aggregate_uid=lead.uid,
        dedup_key=f"leads.converted:{lead.uid}",
    )
    bump(CACHE_NAMESPACE)
    return lead, customer, was_created


@transaction.atomic
def mark_converted_for_customer(customer: Customer, *, reason: str) -> int:
    """Open leads of ``customer`` become CONVERTED (``quotations.issued`` handler; system actor). Idempotent."""
    count = 0
    for lead in Lead.objects.select_for_update().filter(customer=customer, status__in=list(OPEN)).order_by("pk"):
        previous = lead.status
        lead.versioned_update(None, status=Lead.Status.CONVERTED)
        add_event(lead, LeadEvent.Event.CONVERTED, data={"from": previous, "customer_uid": str(customer.uid), "reason": reason})
        record("leads.lead_converted", obj=lead, actor_kind="SYSTEM", before={"status": previous}, after={"status": Lead.Status.CONVERTED, "customer": str(customer.uid), "reason": reason})
        count += 1
    if count:
        bump(CACHE_NAMESPACE)
    return count


@transaction.atomic
def delete_lead(instance: Lead, *, user, expected_version=None) -> None:
    lead = _lock(instance, expected_version)
    lead.soft_delete(user)
    add_event(lead, LeadEvent.Event.ARCHIVED, by=user)
    record("leads.lead_deleted", obj=lead, actor=user, before=lead_snapshot(lead))
    bump(CACHE_NAMESPACE)


def notes_queryset(lead: Lead):
    return LeadNote.objects.filter(lead=lead).select_related("created_by").order_by("-created_at", "-id")


@transaction.atomic
def add_note(lead: Lead, *, user, body: str) -> LeadNote:
    note = LeadNote(lead=lead, body=body)
    stamp_create(note, user)
    note.save()
    add_event(lead, LeadEvent.Event.NOTE_ADDED, by=user, data={"note_uid": str(note.uid)})
    record("leads.note_added", obj=lead, actor=user, after={"note": str(note.uid)})
    bump(CACHE_NAMESPACE)
    return note


def events_queryset(lead: Lead):
    return LeadEvent.objects.filter(lead=lead).select_related("by").order_by("-at", "-id")
