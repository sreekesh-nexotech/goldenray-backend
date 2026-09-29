"""Maintained pages: reads for the Studio and the page lifecycle (``pages/`` staff, module ``pages``).

A page is an aggregate root: its text slots, image slots and SEO row are edited through
``sitepages.services.content``, and every such edit *touches* the page (version + 1, ``updated_at`` now, verification
cleared) so that ``publish``/``verify`` with ``expected_version`` can never act on content the caller has not seen,
and ``updated_at`` is the page's true last-modified time (sitemap ``lastmod``).

Lifecycle (``pages.publish``): ``publish`` DRAFT|ARCHIVED → PUBLISHED, ``unpublish`` PUBLISHED → DRAFT, ``archive``
DRAFT|PUBLISHED → ARCHIVED, ``restore`` ARCHIVED → DRAFT. Asking for the state the page is already in is a no-op;
anything else outside the table is 409 ``invalid_transition``. ``verify`` (``pages.verify``) stamps
``verified_by``/``verified_at`` on the current content. Changes that alter what the website serves emit
``sitepages.page_published`` / ``page_unpublished`` / ``page_archived`` / ``page_updated`` for the revalidation
handler (owned by the SEO/blog package) with ``paths``. Pages are never created or deleted here: the registry
(``sitepages.services.registry``) and the legacy importer own their existence.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Prefetch, Q, QuerySet
from django.utils import timezone

from audit.services import changes, record, snapshot
from core.errors import Conflict, NotFound
from core.models.base import actor_or_none
from core.outbox import emit
from core.services import check_version
from flarize.cache_utils import bump
from sitepages.models import Page, PageImageSlot, PageSeo, PageTextSlot

CACHE_NAMESPACE = "sitepages"
CAREER_PAGE_SLUG = "career"
SNAPSHOT_FIELDS = ("slug", "route", "title", "description", "group", "template", "status", "is_protected", "sort_order")
EDITABLE_FIELDS = ("sort_order",)
Status = Page.Status

# action → (allowed source states, target state, past tense for audit/events)
TRANSITIONS: dict[str, tuple[frozenset[str], str, str]] = {
    "publish": (frozenset({Status.DRAFT, Status.ARCHIVED}), Status.PUBLISHED, "published"),
    "unpublish": (frozenset({Status.PUBLISHED}), Status.DRAFT, "unpublished"),
    "archive": (frozenset({Status.DRAFT, Status.PUBLISHED}), Status.ARCHIVED, "archived"),
    "restore": (frozenset({Status.ARCHIVED}), Status.DRAFT, "restored"),
}


# ── Reads ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def _live_faqs() -> Q:
    # Reverse relation of faqs.Faq.page (no import: sitepages sits below faqs).
    return Q(faqs__deleted_at__isnull=True) & ~Q(faqs__status="ARCHIVED")


def pages_queryset() -> QuerySet:
    """Studio list rows: SEO row joined, slot and FAQ counts annotated (one query per page of results)."""
    return (
        Page.objects.select_related("seo", "verified_by")
        .annotate(
            image_slot_count=Count("image_slots", filter=Q(image_slots__deleted_at__isnull=True), distinct=True),
            text_slot_count=Count("text_slots", filter=Q(text_slots__deleted_at__isnull=True), distinct=True),
            faq_count=Count("faqs", filter=_live_faqs(), distinct=True),
        )
        .order_by("sort_order", "title", "id")
    )


def with_content(queryset: QuerySet) -> QuerySet:
    """Prefetch what a detail/preview/public payload reads: slots (with assets) and the SEO row's image."""
    return queryset.select_related("seo", "seo__og_image", "verified_by").prefetch_related(
        Prefetch("text_slots", queryset=PageTextSlot.objects.order_by("sort_order", "id")),
        Prefetch("image_slots", queryset=PageImageSlot.objects.select_related("asset").order_by("sort_order", "id")),
    )


def detail_queryset() -> QuerySet:
    return with_content(pages_queryset())


def detail(page: Page) -> Page:
    """``page`` re-read with everything the detail serializer shows (counts, SEO, slots)."""
    return detail_queryset().get(pk=page.pk)


def default_seo(page: Page) -> PageSeo:
    """An unsaved SEO block with the defaults for ``page`` (version 1, ``pk`` None).

    Only the forward cache is set: ``PageSeo(page=page)`` would also cache it as ``page.seo`` (one-to-one), making a
    page without SEO look as if it had one to every later reader of the same instance.
    """
    seo = PageSeo(page_id=page.pk)
    PageSeo.page.field.set_cached_value(seo, page)
    return seo


def page_snapshot(page: Page) -> dict:
    return snapshot(page, SNAPSHOT_FIELDS)


def event_payload(page: Page, **extra) -> dict:
    return {"page_uid": str(page.uid), "slug": page.slug, "route": page.route, "status": page.status, "paths": [page.route], **extra}


def emit_page_event(name: str, page: Page, **extra) -> None:
    """Outbox event for the website revalidation handler; one per page version (idempotent)."""
    emit(f"sitepages.{name}", event_payload(page, **extra), aggregate_type="sitepages.page", aggregate_uid=page.uid, dedup_key=f"sitepages.{name}:{page.uid}:v{page.version}")


def lock(page: Page) -> Page:
    locked = Page.objects.select_for_update().filter(pk=page.pk).first()
    if locked is None:
        raise NotFound("page_not_found", "This page no longer exists.")
    return locked


def touch(page: Page, user) -> Page:
    """Record a content change on the aggregate (``page`` must be locked): new version, verification voided."""
    page.versioned_update(user, verified_at=None, verified_by=None)
    bump(CACHE_NAMESPACE)
    if page.is_published:
        emit_page_event("page_updated", page)
    return page


# ── Writes ──────────────────────────────────────────────────────────────────────────────────────────────────────────
@transaction.atomic
def update_page(instance: Page, *, user, data: dict, expected_version=None) -> Page:
    """``PATCH pages/<uid>/``: list ordering only (title, route and status are not maintainer-editable)."""
    page = lock(instance)
    check_version(page, expected_version)
    values = {name: data[name] for name in EDITABLE_FIELDS if name in data and data[name] != getattr(page, name)}
    if not values:
        return page
    before = page_snapshot(page)
    page.versioned_update(user, **values)
    changed_before, changed_after = changes(before, page_snapshot(page))
    record("sitepages.page_updated", obj=page, actor=user, before=changed_before, after=changed_after)
    bump(CACHE_NAMESPACE)
    return page


@transaction.atomic
def transition(instance: Page, action: str, *, user, expected_version=None) -> Page:
    sources, target, past = TRANSITIONS[action]
    page = lock(instance)
    check_version(page, expected_version)
    if page.status == target:
        return page
    if page.status not in sources:
        raise Conflict("invalid_transition", f"A {page.get_status_display().lower()} page cannot be {past}.", errors={"status": [f"Allowed from: {', '.join(sorted(sources))}."]})
    before = page.status
    page.versioned_update(user, status=target)
    record(f"sitepages.page_{past}", obj=page, actor=user, before={"status": before}, after={"status": target})
    bump(CACHE_NAMESPACE)
    if target == Status.PUBLISHED or before == Status.PUBLISHED:  # what the website serves changed
        emit_page_event(f"page_{past}", page, previous_status=before)
    return page


def publish(page: Page, *, user, expected_version=None) -> Page:
    return transition(page, "publish", user=user, expected_version=expected_version)


def unpublish(page: Page, *, user, expected_version=None) -> Page:
    return transition(page, "unpublish", user=user, expected_version=expected_version)


def archive(page: Page, *, user, expected_version=None) -> Page:
    return transition(page, "archive", user=user, expected_version=expected_version)


def restore(page: Page, *, user, expected_version=None) -> Page:
    return transition(page, "restore", user=user, expected_version=expected_version)


@transaction.atomic
def verify(instance: Page, *, user, expected_version=None) -> Page:
    """Stamp the current content as reviewed. Any later slot/SEO edit clears the stamp (see :func:`touch`)."""
    page = lock(instance)
    check_version(page, expected_version)
    if page.status == Status.ARCHIVED:
        raise Conflict("invalid_transition", "An archived page cannot be verified.", errors={"status": ["Restore the page first."]})
    page.versioned_update(user, verified_at=timezone.now(), verified_by=actor_or_none(user))
    record("sitepages.page_verified", obj=page, actor=user, after={"verified_at": page.verified_at.isoformat(), "version": page.version})
    bump(CACHE_NAMESPACE)
    return page


# ── Dashboard ───────────────────────────────────────────────────────────────────────────────────────────────────────
def dashboard_counts(user) -> dict[str, int]:
    counts = {row["status"]: row["n"] for row in Page.objects.order_by().values("status").annotate(n=Count("id"))}
    return {"published": counts.get(Status.PUBLISHED, 0), "draft": counts.get(Status.DRAFT, 0), "unverified": Page.objects.filter(status=Status.PUBLISHED, verified_at__isnull=True).count()}
