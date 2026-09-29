"""Agreement → inspection linking (PLAN §3.5; Plan 2 §3.2 "Origin and linking").

Driven only by the outbox events ``agreements.issued`` and ``agreements.superseded`` (payload contract in
``docs/decisions/site-inspections.md`` → *Event contracts*); nothing here imports the agreements context.

``apply_agreement(payload)`` — idempotent on ``agreement_uid`` (at-least-once delivery):

1. an inspection already linked to ``agreement_uid`` (archived ones included) → nothing to do;
2. ``supersedes_uid`` names the agreement an inspection is linked to → **refresh** it: the new agreement uid/number/
   version, the ``quoted_*`` technical copy, engineer-verifiable fields filled **only where empty**, a new AGREEMENT
   snapshot; a changed ``system_type`` resets the checklists that no longer apply and, once the inspection is
   COMPLETED or later, sends it back (REVISION_REQUIRED, approvals superseded);
3. otherwise **link**: the customer's most recent live PRE_SALE inspection without an agreement and not APPROVED /
   INSTALLATION_READY / REJECTED — matched by the customer foreign key only (never by name or phone) — is converted in
   place (origin AGREEMENT); if there is none a new AGREEMENT inspection is created (DRAFT, the agreement's system type,
   never UNDECIDED), pointing at the customer's last pre-sale visit as ``pre_sale_source`` when one exists.

EXTRA_STRUCTURE agreements (``kind``) price additional work and never create or link inspections.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from catalog.models import Component
from core.sequences import next_number
from core.services import stamp_create
from customers.models import Customer
from customers.services.phones import try_normalise
from site_inspections.models import Inspection
from site_inspections.models.choices import Origin, Phase, SnapshotSource, Status, SystemType
from site_inspections.services import common, snapshots
from site_inspections.services.inspections import AFTER_COMPLETION, reset_equipment, send_back

LINKABLE_KINDS = frozenset({"PURCHASE_AGREEMENT", "SALE_ORDER"})
NOT_LINKABLE = (Status.APPROVED, Status.INSTALLATION_READY, Status.REJECTED)
FILL_TEXT = {"consumer_number": 20, "address": 10_000, "district": 100, "location": 120}


class PayloadError(ValueError):
    """The event does not follow the documented contract (the outbox parks it after its retries; see ops)."""


def _uuid(value, name: str, *, required: bool = True):
    if value in (None, ""):
        if required:
            raise PayloadError(f"{name} is required")
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise PayloadError(f"{name} is not a UUID: {value!r}") from None


def _decimal(value):
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PayloadError(f"not a number: {value!r}") from None
    return number if number.is_finite() and number >= 0 else None


def _customer(uid) -> Customer:
    customer = Customer.all_objects.filter(uid=uid).first()
    seen = set()
    while customer is not None and customer.deleted_at is not None and customer.merged_into_id and customer.pk not in seen:
        seen.add(customer.pk)
        customer = Customer.all_objects.filter(pk=customer.merged_into_id).first()
    if customer is None or customer.deleted_at is not None:
        raise PayloadError(f"customer {uid} does not exist")
    return customer


def _component(uid):
    parsed = _uuid(uid, "component", required=False)
    return Component.objects.filter(uid=parsed).first() if parsed else None


def parse(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise PayloadError("payload must be an object")
    system_type = payload.get("system_type")
    if system_type not in (SystemType.ON_GRID, SystemType.HYBRID):
        raise PayloadError(f"system_type must be ON_GRID or HYBRID, got {system_type!r}")
    fields = payload.get("fields")
    fields = {} if fields is None else fields
    if not isinstance(fields, dict):
        raise PayloadError("fields must be an object")
    phase = payload.get("phase") or ""
    return {
        "agreement_uid": _uuid(payload.get("agreement_uid"), "agreement_uid"),
        "number": str(payload.get("number") or "")[:32],
        "version": payload.get("version") if isinstance(payload.get("version"), int) and payload.get("version") > 0 else None,
        "kind": payload.get("kind") or "PURCHASE_AGREEMENT",
        "customer_uid": _uuid(payload.get("customer_uid"), "customer_uid"),
        "supersedes_uid": _uuid(payload.get("supersedes_uid"), "supersedes_uid", required=False),
        "quotation_uid": _uuid(payload.get("quotation_uid"), "quotation_uid", required=False),
        "lead_uid": _uuid(payload.get("lead_uid"), "lead_uid", required=False),
        "system_type": system_type,
        "size_kw": _decimal(payload.get("size_kw")),
        "phase": phase if phase in Phase.values else "",
        "tier": payload.get("tier"),
        "issued_at": payload.get("issued_at"),
        "fields": fields,
    }


def _quoted(data: dict) -> dict:
    fields = data["fields"]
    capacity = fields.get("panel_capacity_w")
    return {
        "quoted_size_kw": data["size_kw"],
        "quoted_panel": _component(fields.get("panel_uid")),
        "quoted_panel_capacity_w": capacity if isinstance(capacity, int) and capacity > 0 else None,
        "quoted_inverter": _component(fields.get("inverter_uid")),
        "quoted_battery": _component(fields.get("battery_uid")) if data["system_type"] == SystemType.HYBRID else None,
        "quoted_structure_type": str(fields.get("structure_type") or "")[:32],
        "quoted_structure_material": str(fields.get("structure_material") or "")[:64],
        "quotation_version_uid": _uuid(fields.get("quotation_version_uid"), "fields.quotation_version_uid", required=False),
    }


def _fill_empty(inspection: Inspection, data: dict) -> dict:
    """Engineer-verifiable values from the agreement, only where the inspection has none (spec §J 24)."""
    fields = data["fields"]
    values = {}
    for name, limit in FILL_TEXT.items():
        text = str(fields.get(name) or "").strip()[:limit]
        if text and not getattr(inspection, name):
            values[name] = text
    pincode = str(fields.get("pincode") or "").strip()
    if pincode and not inspection.pincode and len(pincode) == 6 and pincode.isdigit() and pincode[0] != "0":
        values["pincode"] = pincode
    phone = try_normalise(fields.get("registered_phone")) if fields.get("registered_phone") else None
    if phone and not inspection.registered_phone_e164:
        values["registered_phone_e164"] = phone
    wheeling = fields.get("wheeling_required")
    if isinstance(wheeling, bool) and inspection.wheeling_required is None:
        values["wheeling_required"] = wheeling
    if data["phase"] and not inspection.phase:
        values["phase"] = data["phase"]
    return values


def _agreement_extra(data: dict) -> dict:
    return {"kind": data["kind"], "tier": data["tier"], "issued_at": data["issued_at"], "quotation_uid": str(data["quotation_uid"]) if data["quotation_uid"] else None}


def _apply(inspection: Inspection, data: dict, *, version: int, action: str) -> Inspection:
    previous_type = inspection.system_type
    values = {
        "origin": Origin.AGREEMENT,
        "agreement_uid": data["agreement_uid"],
        "agreement_number": data["number"],
        "agreement_version": version,
        "system_type": data["system_type"],
        **_quoted(data),
        **_fill_empty(inspection, data),
    }
    if values["quotation_version_uid"] is None:
        values.pop("quotation_version_uid")
    inspection.versioned_update(None, **values)
    removed = reset_equipment(inspection, user=None) if previous_type != inspection.system_type else []
    snapshots.add(inspection, user=None, source=SnapshotSource.AGREEMENT, data=snapshots.build(inspection, agreement=_agreement_extra(data)))
    common.audit(
        action,
        inspection,
        None,
        after={"agreement_uid": str(data["agreement_uid"]), "agreement_number": data["number"], "agreement_version": version, "system_type": data["system_type"], "reset_equipment": removed},
    )
    if previous_type != inspection.system_type and inspection.status in AFTER_COMPLETION:
        send_back(inspection, user=None, reason=f"The agreement changed the system type from {previous_type} to {inspection.system_type}.")
    common.changed(inspection)
    return inspection


@transaction.atomic
def apply_agreement(payload: dict) -> Inspection | None:
    data = parse(payload)
    if data["kind"] not in LINKABLE_KINDS:
        return None
    if Inspection.all_objects.filter(agreement_uid=data["agreement_uid"]).exists():
        return None
    customer = _customer(data["customer_uid"])
    if data["supersedes_uid"] is not None:
        linked = Inspection.objects.select_for_update().filter(agreement_uid=data["supersedes_uid"]).first()
        if linked is not None:
            return _apply(linked, data, version=data["version"] or (linked.agreement_version or 0) + 1, action="agreement_superseded")
    candidate = (
        Inspection.objects.select_for_update().filter(customer=customer, origin=Origin.PRE_SALE, agreement_uid__isnull=True).exclude(status__in=NOT_LINKABLE).order_by("-created_at", "-id").first()
    )
    if candidate is not None:
        return _apply(candidate, data, version=data["version"] or 1, action="linked_to_agreement")
    source = Inspection.objects.filter(customer=customer, origin=Origin.PRE_SALE).order_by("-created_at", "-id").first()
    inspection = Inspection(
        number=next_number("SV"),
        visit_date=timezone.localdate(),
        customer=customer,
        origin=Origin.AGREEMENT,
        system_type=data["system_type"],
        agreement_uid=data["agreement_uid"],
        pre_sale_source=source,
        address=customer.address,
        pincode=customer.pincode,
        district=customer.district,
        location=customer.location,
    )
    stamp_create(inspection, None)
    inspection.save()
    common.audit("created", inspection, None, after={"number": inspection.number, "origin": Origin.AGREEMENT, "customer": str(customer.uid)})
    return _apply(inspection, data, version=data["version"] or 1, action="linked_to_agreement")
