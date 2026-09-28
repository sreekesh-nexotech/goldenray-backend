"""FAQ authoring and workflow (``faqs/``, module ``faqs``).

* **Create/edit** — question, answer, page (by uid), section, category, sort order and the SEO block. A new FAQ is a
  DRAFT at the end of its ``(page, section)``. A draft may be incomplete; a PUBLISHED FAQ may not become incomplete
  through an edit (400 ``faq_not_publishable``). Every content edit clears the review stamp.
* **Workflow** — ``publish`` DRAFT|ARCHIVED → PUBLISHED (refused with 400 ``faq_not_publishable`` and the reasons
  while :func:`publish_errors` is not empty), ``unpublish`` PUBLISHED → DRAFT, ``archive`` DRAFT|PUBLISHED → ARCHIVED,
  ``restore`` ARCHIVED → DRAFT, ``verify`` stamps the current content as reviewed. Asking for the current state is a
  no-op; anything else is 409 ``invalid_transition``. ``published_at`` is set on the first publication and never
  cleared.
* **Reorder** — one ``(page, section)`` renumbered in one transaction from an ordered list of uids (legacy
  semantics: uids that are not members are ignored and take no position; members left out keep their relative order
  after the listed ones).
* Anything that changes what the website shows (a published FAQ's content, a publish/unpublish/archive, a reorder
  moving published FAQs) emits ``faqs.faq_published`` / ``faq_unpublished`` / ``faq_archived`` / ``faq_updated`` /
  ``faqs_reordered`` with the affected ``paths`` for the revalidation handler (owned by the SEO/blog package), and
  bumps the ``faqs`` cache namespace.
"""

from __future__ import annotations

import json

from django.db import transaction
from django.db.models import Count, Max, QuerySet
from django.utils import timezone

from audit.services import changes, record, snapshot
from core.errors import Conflict, DomainError, NotFound
from core.models.base import actor_or_none
from core.outbox import emit
from core.services import check_version, stamp_create
from faqs.models import Faq
from faqs.services.categories import CACHE_NAMESPACE
from flarize.cache_utils import bump
from sitepages.models import Page
from sitepages.services.content import SCHEMA_EXTRA_MAX_BYTES, public_image_problem

Status = Faq.Status
CONTENT_FIELDS = ("question", "answer", "page", "section", "category", "sort_order")
SEO_FIELDS = ("seo_title", "meta_description", "canonical_url", "og_title", "og_description", "og_image", "schema_type", "schema_extra", "noindex")
EDITABLE_FIELDS = CONTENT_FIELDS + SEO_FIELDS
#: Fields whose change alters the public payload of a published FAQ.
PUBLIC_FIELDS = ("question", "answer", "page", "section", "category", "sort_order")
SNAPSHOT_FIELDS = (*EDITABLE_FIELDS, "status")

TRANSITIONS: dict[str, tuple[frozenset[str], str, str]] = {
    "publish": (frozenset({Status.DRAFT, Status.ARCHIVED}), Status.PUBLISHED, "published"),
    "unpublish": (frozenset({Status.PUBLISHED}), Status.DRAFT, "unpublished"),
    "archive": (frozenset({Status.DRAFT, Status.PUBLISHED}), Status.ARCHIVED, "archived"),
    "restore": (frozenset({Status.ARCHIVED}), Status.DRAFT, "restored"),
}


# ── Reads ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def faqs_queryset() -> QuerySet:
    return Faq.objects.select_related("page", "category", "og_image", "created_by", "updated_by", "verified_by").order_by("page__sort_order", "page_id", "section", "sort_order", "id")


def publish_errors(faq: Faq) -> list[str]:
    """Everything blocking publication, worded for the editor (shown on every read so Publish can say why not)."""
    errors = []
    if not (faq.question or "").strip():
        errors.append("A question is required.")
    if not (faq.answer or "").strip():
        errors.append("An answer is required.")
    if faq.page_id is None:
        errors.append("Choose the page this FAQ appears on.")
    elif faq.page.status == Page.Status.ARCHIVED:
        errors.append(f"'{faq.page.title}' is archived — restore it or pick another page.")
    return errors


def next_sort_order(page: Page | None, section: str) -> int:
    last = Faq.objects.filter(page=page, section=section or "").aggregate(last=Max("sort_order"))["last"]
    return 0 if last is None else last + 1


def faq_snapshot(faq: Faq) -> dict:
    return snapshot(faq, SNAPSHOT_FIELDS)


def _paths(*pages: Page | None) -> list[str]:
    return sorted({page.route for page in pages if page is not None})


def emit_faq_event(name: str, faq: Faq, *, paths: list[str], **extra) -> None:
    if not paths:
        return
    emit(
        f"faqs.{name}",
        {"faq_uid": str(faq.uid), "page_uid": str(faq.page.uid) if faq.page_id else None, "status": faq.status, "paths": paths, **extra},
        aggregate_type="faqs.faq",
        aggregate_uid=faq.uid,
        dedup_key=f"faqs.{name}:{faq.uid}:v{faq.version}",
    )


def _lock(faq: Faq) -> Faq:
    locked = Faq.objects.select_for_update(of=("self",)).select_related("page").filter(pk=faq.pk).first()
    if locked is None:
        raise NotFound("faq_not_found", "This FAQ no longer exists.")
    return locked


def _validate(data: dict) -> None:
    errors: dict[str, list[str]] = {}
    if "question" in data and not (data["question"] or "").strip():
        errors["question"] = ["A question is required."]
    if data.get("category") is not None and data["category"].deleted_at is not None:
        errors["category"] = ["This category has been deleted."]
    if data.get("page") is not None and data["page"].deleted_at is not None:
        errors["page"] = ["This page no longer exists."]
    if "og_image" in data:
        problem = public_image_problem(data["og_image"])
        if problem:
            errors["og_image"] = [problem]
    if "schema_extra" in data and (not isinstance(data["schema_extra"], dict) or len(json.dumps(data["schema_extra"], default=str)) > SCHEMA_EXTRA_MAX_BYTES):
        errors["schema_extra"] = ["Must be an object of at most 16 KB."]
    if errors:
        raise DomainError("validation_error", "The FAQ is invalid.", errors=errors)


def _normalise(data: dict) -> dict:
    values = {name: data[name] for name in EDITABLE_FIELDS if name in data}
    if "question" in values:
        values["question"] = (values["question"] or "").strip()
    if "section" in values:
        values["section"] = (values["section"] or "").strip()
    return values


# ── Create / update ─────────────────────────────────────────────────────────────────────────────────────────────────
@transaction.atomic
def create_faq(*, user, data: dict) -> Faq:
    values = _normalise(data)
    _validate(values)
    if values.get("sort_order") is None:
        values["sort_order"] = next_sort_order(values.get("page"), values.get("section", ""))
    faq = Faq(**values, status=Status.DRAFT)
    stamp_create(faq, user)
    faq.save()
    record("faqs.faq_created", obj=faq, actor=user, after=faq_snapshot(faq))
    bump_cache()
    return faq


@transaction.atomic
def update_faq(instance: Faq, *, user, data: dict, expected_version=None) -> Faq:
    faq = _lock(instance)
    check_version(faq, expected_version)
    values = _normalise(data)
    _validate(values)
    values = {name: value for name, value in values.items() if value != getattr(faq, name)}
    if not values:
        return faq
    if ("page" in values or "section" in values) and "sort_order" not in values:
        # Moving to another list: land at its end rather than colliding with an existing position.
        values["sort_order"] = next_sort_order(values.get("page", faq.page), values.get("section", faq.section))
    old_page = faq.page
    before = faq_snapshot(faq)
    if faq.status == Status.PUBLISHED:
        candidate = Faq(**{**{name: getattr(faq, name) for name in CONTENT_FIELDS}, **{k: v for k, v in values.items() if k in CONTENT_FIELDS}})
        problems = publish_errors(candidate)
        if problems:
            raise DomainError("faq_not_publishable", "A published FAQ must stay complete. Unpublish it first to leave it incomplete.", errors={"publish": problems})
    faq.versioned_update(user, **values, verified_at=None, verified_by=None)
    changed_before, changed_after = changes(before, faq_snapshot(faq))
    record("faqs.faq_updated", obj=faq, actor=user, before=changed_before, after=changed_after)
    bump_cache()
    if faq.status == Status.PUBLISHED and any(name in values for name in PUBLIC_FIELDS):
        emit_faq_event("faq_updated", faq, paths=_paths(old_page, faq.page))
    return faq


def bump_cache() -> None:
    bump(CACHE_NAMESPACE)


# ── Workflow ────────────────────────────────────────────────────────────────────────────────────────────────────────
@transaction.atomic
def transition(instance: Faq, action: str, *, user, expected_version=None) -> Faq:
    sources, target, past = TRANSITIONS[action]
    faq = _lock(instance)
    check_version(faq, expected_version)
    if faq.status == target:
        return faq
    if faq.status not in sources:
        raise Conflict("invalid_transition", f"A {faq.get_status_display().lower()} FAQ cannot be {past}.", errors={"status": [f"Allowed from: {', '.join(sorted(sources))}."]})
    if target == Status.PUBLISHED:
        problems = publish_errors(faq)
        if problems:
            raise DomainError("faq_not_publishable", "This FAQ is not ready to publish.", errors={"publish": problems})
    now = timezone.now()
    values: dict = {"status": target}
    if target == Status.PUBLISHED:
        values["published_at"] = faq.published_at or now
        values["archived_at"] = None
    elif target == Status.ARCHIVED:
        values["archived_at"] = now
    elif action == "restore":
        values["archived_at"] = None
    before = faq.status
    faq.versioned_update(user, **values)
    record(f"faqs.faq_{past}", obj=faq, actor=user, before={"status": before}, after={"status": target})
    bump_cache()
    if target == Status.PUBLISHED or before == Status.PUBLISHED:
        emit_faq_event(f"faq_{past}", faq, paths=_paths(faq.page), previous_status=before)
    return faq


def publish(faq: Faq, *, user, expected_version=None) -> Faq:
    return transition(faq, "publish", user=user, expected_version=expected_version)


def unpublish(faq: Faq, *, user, expected_version=None) -> Faq:
    return transition(faq, "unpublish", user=user, expected_version=expected_version)


def archive(faq: Faq, *, user, expected_version=None) -> Faq:
    return transition(faq, "archive", user=user, expected_version=expected_version)


def restore(faq: Faq, *, user, expected_version=None) -> Faq:
    return transition(faq, "restore", user=user, expected_version=expected_version)


@transaction.atomic
def verify(instance: Faq, *, user, expected_version=None) -> Faq:
    faq = _lock(instance)
    check_version(faq, expected_version)
    if faq.status == Status.ARCHIVED:
        raise Conflict("invalid_transition", "An archived FAQ cannot be verified.", errors={"status": ["Restore the FAQ first."]})
    faq.versioned_update(user, verified_at=timezone.now(), verified_by=actor_or_none(user))
    record("faqs.faq_verified", obj=faq, actor=user, after={"verified_at": faq.verified_at.isoformat(), "version": faq.version})
    bump_cache()
    return faq


# ── Reorder ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@transaction.atomic
def reorder(page: Page, section: str, ordered_uids: list, *, user) -> list[Faq]:
    """Renumber one ``(page, section)`` to ``ordered_uids`` (0, 1, …); returns the section in its new order."""
    section = (section or "").strip()
    members = {str(faq.uid): faq for faq in Faq.objects.select_for_update().filter(page=page, section=section).order_by("sort_order", "id")}
    applied = [uid for uid in dict.fromkeys(str(uid) for uid in ordered_uids) if uid in members]
    tail = [faq for uid, faq in members.items() if uid not in set(applied)]  # already in (sort_order, id) order
    new_order = [members[uid] for uid in applied] + tail
    before = [str(faq.uid) for faq in members.values()]
    changed = moved_public = False
    for position, faq in enumerate(new_order):
        if faq.sort_order != position:
            changed = True
            moved_public = moved_public or faq.status == Status.PUBLISHED
            faq.versioned_update(user, sort_order=position)
    if changed:
        record("faqs.faqs_reordered", obj=page, actor=user, before={"section": section, "order": before}, after={"section": section, "order": [str(faq.uid) for faq in new_order]})
        bump_cache()
    if moved_public:
        emit("faqs.faqs_reordered", {"page_uid": str(page.uid), "section": section, "paths": [page.route]}, aggregate_type="sitepages.page", aggregate_uid=page.uid)
    return new_order


# ── Dashboard ───────────────────────────────────────────────────────────────────────────────────────────────────────
def dashboard_counts(user) -> dict[str, int]:
    counts = {row["status"]: row["n"] for row in Faq.objects.order_by().values("status").annotate(n=Count("id"))}
    return {"published": counts.get(Status.PUBLISHED, 0), "draft": counts.get(Status.DRAFT, 0), "archived": counts.get(Status.ARCHIVED, 0)}
