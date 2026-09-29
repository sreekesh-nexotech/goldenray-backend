"""``site_inspections`` record scopes (PLAN §3.2: all / owned / assigned).

* ``owned`` (Sales Executive) — inspections of customers I own, and the ones I created;
* ``assigned`` (Field Engineer) — inspections assigned to me.

Only inspection querysets are filtered; child rows (photos, approvals, …) are always reached through a visible
inspection. An unknown model sees nothing (fail closed).
"""

from __future__ import annotations

from django.db.models import Q

from core import scopes
from site_inspections.models import Inspection
from site_inspections.services.common import MODULE


@scopes.register(MODULE, "owned")
def owned(queryset, user):
    if queryset.model is not Inspection:
        return queryset.none()
    return queryset.filter(Q(customer__owner=user) | Q(created_by=user))


@scopes.register(MODULE, "assigned")
def assigned(queryset, user):
    if queryset.model is not Inspection:
        return queryset.none()
    return queryset.filter(engineer=user)
