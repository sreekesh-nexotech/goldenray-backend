"""FAQ categories (``faq-categories/``, module ``faqs``): an optional label and filter for FAQs.

Names and slugs are unique among live categories (a blank slug is derived from the name). Deleting is a soft delete
and is refused (409 ``category_in_use``) while any live FAQ — archived ones included — still uses the category:
deactivate it instead, or move those FAQs first. The public FAQ payload shows the category *name*, so a rename bumps
the FAQ cache and emits ``faqs.category_updated`` with the paths of the pages whose published FAQs carry it.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Count, Q, QuerySet
from django.utils.text import slugify

from audit.services import changes, record, snapshot
from core.errors import Conflict, DomainError
from core.outbox import emit
from core.services import check_version, stamp_create
from faqs.models import Faq, FaqCategory
from flarize.cache_utils import bump

CACHE_NAMESPACE = "faqs"
FIELDS = ("name", "slug", "description", "is_active", "sort_order")


def categories_queryset() -> QuerySet:
    live_faqs = Q(faqs__deleted_at__isnull=True)
    return FaqCategory.objects.annotate(
        faq_count=Count("faqs", filter=live_faqs, distinct=True),
        published_faq_count=Count("faqs", filter=live_faqs & Q(faqs__status=Faq.Status.PUBLISHED), distinct=True),
    ).order_by("sort_order", "name", "id")


def category_snapshot(category: FaqCategory) -> dict:
    return snapshot(category, FIELDS)


def _taken(field: str) -> Conflict:
    return Conflict(f"category_{field}_taken", f"Another category already uses this {field}.", errors={field: ["Already in use."]})


def _from_integrity_error(exc: IntegrityError) -> Conflict:
    return _taken("slug" if "faqs_category_slug_uniq" in str(exc) else "name")


def _clean(values: dict, *, instance: FaqCategory | None = None) -> dict:
    """Trim the name, derive/normalise the slug (only derived on create), refuse names/slugs used by live categories."""
    if "name" in values:
        values["name"] = values["name"].strip()
        if not values["name"]:
            raise DomainError("validation_error", "A category needs a name.", errors={"name": ["This field may not be blank."]})
    if "slug" in values or instance is None:
        values["slug"] = slugify(values.get("slug") or "") or (slugify(values.get("name") or "") if instance is None else "")
        if not values["slug"]:
            raise DomainError("validation_error", "Enter a slug (letters, numbers, hyphens).", errors={"slug": ["Enter a valid slug."]})
    others = FaqCategory.objects.exclude(pk=instance.pk) if instance is not None else FaqCategory.objects.all()
    for field in ("name", "slug"):
        if field in values and others.filter(**{field: values[field]}).exists():
            raise _taken(field)
    return values


def _paths_using(category: FaqCategory) -> list[str]:
    return sorted(set(Faq.objects.filter(category=category, status=Faq.Status.PUBLISHED, page__isnull=False).values_list("page__route", flat=True)))


@transaction.atomic
def create_category(*, user, data: dict) -> FaqCategory:
    values = _clean({name: data[name] for name in FIELDS if name in data})
    category = FaqCategory(**values)
    stamp_create(category, user)
    try:
        with transaction.atomic():
            category.save()
    except IntegrityError as exc:  # a concurrent create won the race
        raise _from_integrity_error(exc) from None
    record("faqs.category_created", obj=category, actor=user, after=category_snapshot(category))
    bump(CACHE_NAMESPACE)
    return category


@transaction.atomic
def update_category(instance: FaqCategory, *, user, data: dict, expected_version=None) -> FaqCategory:
    category = FaqCategory.objects.select_for_update().get(pk=instance.pk)
    check_version(category, expected_version)
    values = _clean({name: data[name] for name in FIELDS if name in data}, instance=category)
    values = {name: value for name, value in values.items() if value != getattr(category, name)}
    if not values:
        return category
    before = category_snapshot(category)
    try:
        with transaction.atomic():
            category.versioned_update(user, **values)
    except IntegrityError as exc:  # a concurrent write won the race
        raise _from_integrity_error(exc) from None
    changed_before, changed_after = changes(before, category_snapshot(category))
    record("faqs.category_updated", obj=category, actor=user, before=changed_before, after=changed_after)
    bump(CACHE_NAMESPACE)
    if "name" in values:
        paths = _paths_using(category)
        if paths:
            emit(
                "faqs.category_updated",
                {"category_uid": str(category.uid), "paths": paths},
                aggregate_type="faqs.faqcategory",
                aggregate_uid=category.uid,
                dedup_key=f"faqs.category_updated:{category.uid}:v{category.version}",
            )
    return category


@transaction.atomic
def delete_category(instance: FaqCategory, *, user, expected_version=None) -> None:
    category = FaqCategory.objects.select_for_update().get(pk=instance.pk)
    check_version(category, expected_version)
    in_use = Faq.objects.filter(category=category).count()
    if in_use:
        raise Conflict("category_in_use", f"{in_use} FAQ(s) use this category. Deactivate it instead, or move those FAQs first.", errors={"faq_count": [str(in_use)]})
    category.soft_delete(user)
    record("faqs.category_deleted", obj=category, actor=user, before=category_snapshot(category))
    bump(CACHE_NAMESPACE)
