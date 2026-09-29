"""``dashboard/`` counters for the ``leads`` module (record scope applied)."""

from __future__ import annotations

from core import dashboard
from leads.models import AffiliateApplication, Lead, WarrantyRequest
from leads.services.scoping import visible


@dashboard.register("leads")
def lead_counts(user) -> dict[str, int]:
    leads = visible(Lead.objects.all(), user)
    return {
        "new": leads.filter(status=Lead.Status.NEW).count(),
        "open": leads.filter(status__in=list(Lead.OPEN_STATUSES)).count(),
        "affiliate_applications_new": visible(AffiliateApplication.objects.filter(status=AffiliateApplication.Status.NEW), user).count(),
        "warranty_requests_open": visible(WarrantyRequest.objects.filter(status__in=[WarrantyRequest.Status.NEW, WarrantyRequest.Status.IN_PROGRESS]), user).count(),
    }
