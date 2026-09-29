"""The leads context's entries in ``customers/<uid>/timeline/`` (registered on import, from ``LeadsConfig.ready``).

Leads of the customer (their creation and conversion) and its warranty requests, both through the ``leads`` record
scope of the viewer (a Sales Executive with ``owned`` sees only what is assigned to them).
"""

from __future__ import annotations

from customers.services import timeline
from leads.models import LeadEvent, WarrantyRequest
from leads.services.scoping import visible


def _older(queryset, column: str, before):
    return queryset.filter(**{f"{column}__lt": before}) if before is not None else queryset


@timeline.register("leads.leads", module="leads")
def lead_entries(customer, *, user, before, limit):
    events = LeadEvent.objects.filter(lead__customer=customer, lead__deleted_at__isnull=True, event__in=[LeadEvent.Event.CREATED, LeadEvent.Event.IMPORTED, LeadEvent.Event.CONVERTED])
    events = _older(visible(events, user), "at", before).select_related("lead").order_by("-at", "-id")[:limit]
    for event in events:
        lead = event.lead
        verb = "converted" if event.event == LeadEvent.Event.CONVERTED else "received"
        yield timeline.Entry(event.at, f"leads.{verb}", f"Lead {lead.number} ({lead.get_kind_display()}) {verb}", "leads.lead", lead.uid, {"status": lead.status, "form": lead.form})


@timeline.register("leads.warranty_requests", module="leads")
def warranty_entries(customer, *, user, before, limit):
    requests = _older(visible(WarrantyRequest.objects.filter(customer=customer), user), "created_at", before).order_by("-created_at", "-id")[:limit]
    for request in requests:
        yield timeline.Entry(request.created_at, "leads.warranty_request", f"Warranty request: {request.get_issue_type_display()}", "leads.warrantyrequest", request.uid, {"status": request.status})
