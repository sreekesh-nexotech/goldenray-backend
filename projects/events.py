"""Outbox handlers owned by projects (``@core.outbox.handler("<context>.<event>")``).

``site_inspections.released`` → optional auto-create of the project (PLAN §3.5, D-8), **off by default**: only when
``settings.PROJECTS_AUTO_CREATE_ON_RELEASE`` is true. Payload contract (docs/decisions/projects.md "Event contracts")::

    {"inspection_uid": uuid, "customer_uid": uuid, "lead_uid": uuid | null, "agreement_uid": uuid | null,
     "quotation_version_uid": uuid | null, "system_type": "ON_GRID" | "HYBRID" | "UNDECIDED" | …, "size_kw": number | null, "phase": "1P" | "3P" | null,
     "released_at": ISO date-time}

Idempotent: one live (non-cancelled) project per inspection. A payload that breaks the contract or names an unknown
customer is logged and dropped (retrying cannot fix it); the handler never imports site_inspections code.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings

from core.outbox import Event, handler
from customers.models import Customer
from projects.models import Phase, SystemType
from projects.services.projects import create_from_inspection

logger = logging.getLogger("flarize.projects.events")


def _uuid(value):
    if value in (None, ""):
        return None
    return uuid.UUID(str(value))


def _size(value):
    if value in (None, ""):
        return None
    size = Decimal(str(value)).quantize(Decimal("0.01"))
    if size <= 0 or size >= Decimal("10000"):
        raise InvalidOperation(value)
    return size


def _live_customer(customer_uid) -> Customer | None:
    customer = Customer.all_objects.filter(uid=customer_uid).first()
    hops = 0
    while customer is not None and customer.deleted_at is not None and customer.merged_into_id is not None and hops < 10:
        customer = Customer.all_objects.filter(pk=customer.merged_into_id).first()
        hops += 1
    return customer if customer is not None and customer.deleted_at is None else None


@handler("site_inspections.released")
def create_project_on_release(event: Event) -> None:
    if not getattr(settings, "PROJECTS_AUTO_CREATE_ON_RELEASE", False):
        return
    payload = event.payload or {}
    try:
        inspection_uid = _uuid(payload.get("inspection_uid"))
        customer_uid = _uuid(payload.get("customer_uid"))
        lead_uid = _uuid(payload.get("lead_uid"))
        agreement_uid = _uuid(payload.get("agreement_uid"))
        quotation_version_uid = _uuid(payload.get("quotation_version_uid"))
        size_kw = _size(payload.get("size_kw"))
    except (ValueError, TypeError, InvalidOperation):
        logger.warning("site_inspections.released payload breaks the contract; no project created", extra={"event_id": event.id})
        return
    if inspection_uid is None or customer_uid is None:
        logger.warning("site_inspections.released without inspection_uid/customer_uid; no project created", extra={"event_id": event.id})
        return
    customer = _live_customer(customer_uid)
    if customer is None:
        logger.warning("site_inspections.released names an unknown customer; no project created", extra={"event_id": event.id, "customer_uid": str(customer_uid)})
        return
    system_type = str(payload.get("system_type") or "").upper()
    phase = str(payload.get("phase") or "").upper()
    create_from_inspection(
        customer=customer,
        inspection_uid=inspection_uid,
        lead_uid=lead_uid,
        agreement_uid=agreement_uid,
        quotation_version_uid=quotation_version_uid,
        system_type=system_type if system_type in SystemType.values else "",
        size_kw=size_kw,
        phase=phase if phase in Phase.values else "",
        released_at=payload.get("released_at"),
    )
