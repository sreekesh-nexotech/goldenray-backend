"""Record scope of ``change_requests`` (all / owned): a requester sees the requests they made; approvers see all."""

from __future__ import annotations

from core import scopes

MODULE = "change_requests"


@scopes.register(MODULE, "owned")
def own_requests(queryset, user):
    return queryset.filter(created_by=user)
