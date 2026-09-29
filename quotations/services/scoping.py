"""Record scope of ``quotations`` (PLAN §3.2: all / owned): a Sales Executive sees the quotations they own."""

from __future__ import annotations

from core import scopes
from quotations.services.common import MODULE


@scopes.register(MODULE, "owned")
def owned_quotations(queryset, user):
    model = queryset.model
    if model._meta.model_name == "quotation":
        return queryset.filter(owner=user)
    return queryset.filter(quotation__owner=user)


def visible(queryset, user):
    """``queryset`` (of quotations, or of rows with a ``quotation`` FK) narrowed to what ``user`` may see."""
    return scopes.apply(queryset, user, MODULE)
