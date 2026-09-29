"""Record scope of ``agreements`` (PLAN §3.2: all / owned): a Sales Executive sees the agreements they own."""

from __future__ import annotations

from agreements.services.common import MODULE
from core import scopes


@scopes.register(MODULE, "owned")
def owned_agreements(queryset, user):
    if queryset.model._meta.model_name == "agreement":
        return queryset.filter(owner=user)
    return queryset.filter(agreement__owner=user)


def visible(queryset, user):
    """``queryset`` (of agreements, or of rows with an ``agreement`` FK) narrowed to what ``user`` may see."""
    return scopes.apply(queryset, user, MODULE)
