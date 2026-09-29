"""``customers/<uid>/timeline/`` — one chronological feed built from a registry of providers.

Each context contributes entries about a customer from its own tables (leads today; quotations, agreements, site
inspections and projects register theirs when they land), from its ``AppConfig.ready()``::

    from customers.services import timeline

    @timeline.register("quotations", module="quotations")
    def quotation_entries(customer, *, user, before, limit):
        ...  # yield timeline.Entry(...) newest first, at most `limit`, all strictly older than `before` (if given)

A provider is only asked when the user holds ``<module>.view`` (and applies that module's record scope itself), so
the timeline never shows what the user could not open. The feed is newest first and paged by time: ``limit``
entries (default 50, at most 200) older than ``before``; ``next_before`` is the cursor for the next page.
"""

from __future__ import annotations

import datetime as dt
import heapq
import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from accounts.services.authz import can
from audit.models import AuditLog
from customers.models import Customer, CustomerNote

logger = logging.getLogger("flarize.customers.timeline")

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass(frozen=True)
class Entry:
    at: dt.datetime
    kind: str  # "<context>.<what>", e.g. "leads.converted"
    title: str
    object_type: str = ""
    object_uid: uuid.UUID | None = None
    data: dict = field(default_factory=dict)


Provider = Callable[..., Iterable[Entry]]


@dataclass(frozen=True)
class _Registered:
    key: str
    module: str
    fn: Provider


_PROVIDERS: dict[str, _Registered] = {}


def register(key: str, *, module: str):
    from accounts.registry import MODULES

    if module not in MODULES:
        raise ValueError(f"Unknown registry module {module!r}.")

    def decorator(fn: Provider) -> Provider:
        _PROVIDERS[key] = _Registered(key, module, fn)
        return fn

    return decorator


def unregister(key: str) -> None:
    _PROVIDERS.pop(key, None)


def providers() -> dict[str, str]:
    return {key: provider.module for key, provider in sorted(_PROVIDERS.items())}


def build(customer: Customer, *, user, before: dt.datetime | None = None, limit: int = DEFAULT_LIMIT) -> tuple[list[Entry], dt.datetime | None]:
    """``(entries, next_before)`` — the newest ``limit`` entries older than ``before`` across the allowed providers."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    streams = []
    for key, provider in sorted(_PROVIDERS.items()):
        if not can(user, provider.module, "view"):
            continue
        entries = [entry for entry in provider.fn(customer, user=user, before=before, limit=limit + 1) if before is None or entry.at < before]
        streams.append(sorted(entries, key=lambda entry: entry.at, reverse=True)[: limit + 1])
    merged = list(heapq.merge(*streams, key=lambda entry: entry.at, reverse=True))
    page = merged[:limit]
    next_before = page[-1].at if len(merged) > limit and page else None
    return page, next_before


def _older(queryset, column: str, before):
    return queryset.filter(**{f"{column}__lt": before}) if before is not None else queryset


@register("customers.record", module="customers")
def customer_entries(customer: Customer, *, user, before, limit):
    """The record itself (created, customers merged into it) and its notes."""
    if before is None or customer.created_at < before:
        yield Entry(customer.created_at, "customers.created", f"Customer {customer.code} created", "customers.customer", customer.uid, {"source": customer.source})
    merges = _older(AuditLog.objects.filter(object_type="customers.customer", object_uid=customer.uid, action="customers.merged"), "at", before).order_by("-at")[:limit]
    for row in merges:
        yield Entry(row.at, "customers.merged", "Another customer record was merged into this one", "customers.customer", customer.uid, {"merged_from": (row.after or {}).get("merged_from")})
    notes = _older(CustomerNote.objects.filter(customer=customer), "created_at", before).order_by("-created_at")[:limit]
    for note in notes:
        yield Entry(note.created_at, "customers.note", note.body[:200], "customers.customernote", note.uid, {"pinned": note.pinned})
