"""``catalog_component_change``: the per-field history behind ``GET catalog/components/<uid>/history/``.

Every component write records the fields it changed (``field`` names the component column, ``spec.<column>``,
``tiers``, ``attributes`` or a lifecycle marker such as ``created``/``deleted``) with old/new JSON values and the
reason given. Values are JSON-safe (decimals as strings). Rows are append-only.
"""

from __future__ import annotations

from collections.abc import Mapping

from django.utils import timezone

from catalog.models import Component, ComponentChange
from catalog.services.common import json_safe
from core.models import actor_or_none


def record_change(component: Component, *, user, field: str, old=None, new=None, reason: str = "", at=None) -> ComponentChange:
    return ComponentChange.objects.create(component=component, at=at or timezone.now(), by=actor_or_none(user), field=field[:64], old=json_safe(old), new=json_safe(new), reason=reason or "")


def record_changes(component: Component, *, user, before: Mapping, after: Mapping, reason: str = "", prefix: str = "", at=None) -> list[str]:
    """One row per key whose value differs between ``before`` and ``after``; returns the changed keys."""
    now = at or timezone.now()
    actor = actor_or_none(user)
    rows, changed = [], []
    for key in dict.fromkeys([*before, *after]):
        old, new = json_safe(before.get(key)), json_safe(after.get(key))
        if old == new:
            continue
        changed.append(key)
        rows.append(ComponentChange(component=component, at=now, by=actor, field=f"{prefix}{key}"[:64], old=old, new=new, reason=reason or ""))
    if rows:
        ComponentChange.objects.bulk_create(rows)
    return changed


def history_queryset(component: Component):
    return ComponentChange.objects.filter(component=component).select_related("by").order_by("-at", "-id")
