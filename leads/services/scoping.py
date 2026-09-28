"""``leads`` record scope. ``owned`` = assigned to me: leads, affiliate applications, warranty requests and
installations by ``assignee``; notes and events through their lead. Unknown models see nothing (fail closed)."""

from __future__ import annotations

from core import scopes
from leads.models import AffiliateApplication, CustomerInstallation, Lead, LeadEvent, LeadNote, WarrantyRequest

MODULE = "leads"
OWNER_PATHS: dict[type, str] = {
    Lead: "assignee",
    LeadNote: "lead__assignee",
    LeadEvent: "lead__assignee",
    AffiliateApplication: "assignee",
    WarrantyRequest: "assignee",
    CustomerInstallation: "assignee",
}


@scopes.register(MODULE, "owned")
def assigned_to_me(queryset, user):
    path = OWNER_PATHS.get(queryset.model)
    return queryset.filter(**{path: user}) if path else queryset.none()


def visible(queryset, user):
    return scopes.apply(queryset, user, MODULE)
