"""``catalog/public-profiles/`` — website marketing data per component (module ``products_public``).

* one profile per component (re-creating a deleted one restores and overwrites it); the slug is unique among live
  profiles and derived from the brand + model/name when not given;
* ``ratings`` holds 0–100 integers under the known keys; ``pros``/``cons`` are lists of strings, ``faq`` a list of
  ``{question, answer}``, ``gallery`` a list of public image uids;
* ``publish/`` needs a live, public (``is_public``) component that is ACTIVE or DEPRECATED, in a live and active
  category — exactly what the website lists show (409 ``component_not_publishable``); ``unpublish/`` takes it off the
  website. Both are idempotent, audited, bump
  the ``catalog`` cache namespace and emit ``catalog.profile_published`` / ``catalog.profile_unpublished`` (website
  revalidation).
"""

from __future__ import annotations

import uuid

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.services import changes, record, snapshot
from catalog.models import RATING_KEYS, Component, ComponentPublicProfile, ComponentStatus, ProfileStatus
from catalog.services.common import CACHE_NAMESPACE, unique_slug, validation_error
from core.errors import Conflict
from core.outbox import emit
from core.services import check_version, stamp_create
from flarize.cache_utils import bump
from media.models import MediaAsset

EDITABLE_FIELDS = (
    "slug",
    "headline",
    "summary",
    "body",
    "image_url",
    "price_range_label",
    "subsidy_eligible",
    "kerala_climate_score",
    "overall_rating",
    "rating_tier",
    "ratings",
    "pros",
    "cons",
    "faq",
    "gallery",
    "seo_title",
    "seo_description",
)
SNAPSHOT_FIELDS = ("component", *EDITABLE_FIELDS, "status", "published_at")
PUBLISHABLE_STATUSES = (ComponentStatus.ACTIVE, ComponentStatus.DEPRECATED)
MAX_GALLERY = 30


def profiles_queryset():
    return ComponentPublicProfile.objects.select_related("component__category", "component__brand", "component__primary_image").order_by("slug", "id")


def profile_snapshot(profile: ComponentPublicProfile) -> dict:
    return snapshot(profile, SNAPSHOT_FIELDS)


def _slug_taken(slug: str, exclude_pk=None) -> bool:
    return ComponentPublicProfile.objects.filter(slug=slug).exclude(pk=exclude_pk).exists()


def default_slug(component: Component, exclude_pk=None) -> str:
    base = " ".join(part for part in (component.brand_label, component.model or component.name) if part)
    return unique_slug(base or component.sku, lambda slug: _slug_taken(slug, exclude_pk), max_length=160, fallback="product")


def _validate(values: dict) -> None:
    errors: dict = {}
    ratings = values.get("ratings")
    if ratings is not None:
        if not isinstance(ratings, dict):
            errors["ratings"] = ["Must be an object."]
        else:
            bad = [key for key, value in ratings.items() if key not in RATING_KEYS or isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100]
            if bad:
                errors["ratings"] = [f"Use the keys {', '.join(RATING_KEYS)} with integers 0-100 (invalid: {', '.join(map(str, bad))})."]
    for name in ("pros", "cons"):
        if name in values and (not isinstance(values[name], list) or any(not isinstance(item, str) or not item.strip() for item in values[name])):
            errors[name] = ["Must be a list of non-empty strings."]
    faq = values.get("faq")
    if faq is not None and (not isinstance(faq, list) or any(not isinstance(item, dict) or set(item) != {"question", "answer"} for item in faq)):
        errors["faq"] = ["Must be a list of {question, answer} objects."]
    gallery = values.get("gallery")
    if gallery is not None:
        errors.update(_gallery_errors(gallery))
    if errors:
        raise validation_error(errors)


def _gallery_errors(gallery) -> dict:
    if not isinstance(gallery, list) or len(gallery) > MAX_GALLERY:
        return {"gallery": [f"Must be a list of at most {MAX_GALLERY} media uids."]}
    try:
        uids = [str(uuid.UUID(str(item))) for item in gallery]
    except ValueError:
        return {"gallery": ["Every entry must be a media asset uid."]}
    assets = {str(asset.uid): asset for asset in MediaAsset.objects.filter(uid__in=uids)}
    bad = [uid for uid in uids if uid not in assets or not assets[uid].is_public or not assets[uid].is_image]
    if bad:
        return {"gallery": [f"Not a public image: {', '.join(bad)}."]}
    return {}


def _normalise(values: dict) -> dict:
    if "gallery" in values and isinstance(values["gallery"], list):
        values["gallery"] = [str(uuid.UUID(str(item))) for item in values["gallery"]]
    return values


def _slug_conflict() -> Conflict:
    return Conflict("profile_slug_taken", "Another public profile already uses this slug.", errors={"slug": ["Already in use."]})


@transaction.atomic
def create_profile(*, user, data) -> ComponentPublicProfile:
    component = Component.objects.select_for_update().get(pk=data["component"].pk)
    values = {name: data[name] for name in EDITABLE_FIELDS if name in data}
    _validate(values)
    values = _normalise(values)
    existing = ComponentPublicProfile.all_objects.filter(component=component).first()
    if existing is not None and existing.deleted_at is None:
        raise Conflict("profile_exists", "This component already has a public profile.", errors={"component_uid": ["Already has a profile."]})
    if not values.get("slug"):
        values["slug"] = default_slug(component, exclude_pk=existing.pk if existing else None)
    elif _slug_taken(values["slug"], exclude_pk=existing.pk if existing else None):
        raise _slug_conflict()
    try:
        with transaction.atomic():
            if existing is not None:  # the one-to-one row survives soft deletion: restore and overwrite it
                defaults = {field.name: field.get_default() for field in ComponentPublicProfile._meta.concrete_fields if field.name in EDITABLE_FIELDS}
                existing.versioned_update(user, **{**defaults, **values, "status": ProfileStatus.DRAFT, "published_at": None, "deleted_at": None})
                profile = existing
            else:
                profile = ComponentPublicProfile(component=component, **values)
                stamp_create(profile, user)
                profile.save()
    except IntegrityError:
        raise _slug_conflict() from None
    record("catalog.public_profile_created", obj=profile, actor=user, after=profile_snapshot(profile))
    bump(CACHE_NAMESPACE)
    return profile


@transaction.atomic
def update_profile(instance: ComponentPublicProfile, *, user, data, expected_version=None) -> ComponentPublicProfile:
    profile = ComponentPublicProfile.objects.select_for_update().get(pk=instance.pk)
    check_version(profile, expected_version)
    values = {name: data[name] for name in EDITABLE_FIELDS if name in data}
    _validate(values)
    values = {name: value for name, value in _normalise(values).items() if getattr(profile, name) != value}
    if not values:
        return profile
    if values.get("slug") == "":
        raise validation_error({"slug": ["This field may not be blank."]})
    before = profile_snapshot(profile)
    try:
        with transaction.atomic():
            profile.versioned_update(user, **values)
    except IntegrityError:
        raise _slug_conflict() from None
    changed_before, changed_after = changes(before, profile_snapshot(profile))
    record("catalog.public_profile_updated", obj=profile, actor=user, before=changed_before, after=changed_after)
    bump(CACHE_NAMESPACE)
    if profile.status == ProfileStatus.PUBLISHED:
        emit_profile_event("catalog.profile_updated", profile)
    return profile


@transaction.atomic
def delete_profile(instance: ComponentPublicProfile, *, user, expected_version=None) -> None:
    profile = ComponentPublicProfile.objects.select_for_update().get(pk=instance.pk)
    check_version(profile, expected_version)
    was_published = profile.status == ProfileStatus.PUBLISHED
    profile.soft_delete(user)
    record("catalog.public_profile_deleted", obj=profile, actor=user, before=profile_snapshot(profile))
    bump(CACHE_NAMESPACE)
    if was_published:
        emit_profile_event("catalog.profile_unpublished", profile)


def emit_profile_event(event: str, profile: ComponentPublicProfile) -> None:
    component = profile.component
    emit(
        event,
        {"profile_uid": str(profile.uid), "component_uid": str(component.uid), "slug": profile.slug, "category": component.category.slug},
        aggregate_type="catalog.componentpublicprofile",
        aggregate_uid=profile.uid,
    )


def publish_problems(component: Component) -> list[str]:
    problems = []
    if component.deleted_at is not None:
        problems.append("The component has been deleted.")
    if not component.is_public:
        problems.append("The component is not marked public (is_public).")
    if component.status not in PUBLISHABLE_STATUSES:
        problems.append(f"The component is {component.status}; only ACTIVE or DEPRECATED components are shown.")
    category = component.category
    if category.deleted_at is not None or not category.is_active:
        problems.append(f"The component's category {category.slug!r} is not active (its products are not shown).")
    return problems


@transaction.atomic
def publish(instance: ComponentPublicProfile, *, user, expected_version=None) -> ComponentPublicProfile:
    profile = ComponentPublicProfile.objects.select_for_update(of=("self",)).select_related("component__category").get(pk=instance.pk)
    check_version(profile, expected_version)
    problems = publish_problems(profile.component)
    if problems:
        raise Conflict("component_not_publishable", "The component cannot be shown on the website.", errors={"component": problems})
    if profile.status == ProfileStatus.PUBLISHED:
        return profile
    profile.versioned_update(user, status=ProfileStatus.PUBLISHED, published_at=timezone.now())
    record("catalog.profile_published", obj=profile, actor=user, before={"status": ProfileStatus.DRAFT}, after={"status": ProfileStatus.PUBLISHED})
    bump(CACHE_NAMESPACE)
    emit_profile_event("catalog.profile_published", profile)
    return profile


@transaction.atomic
def unpublish(instance: ComponentPublicProfile, *, user, expected_version=None) -> ComponentPublicProfile:
    profile = ComponentPublicProfile.objects.select_for_update(of=("self",)).select_related("component__category").get(pk=instance.pk)
    check_version(profile, expected_version)
    if profile.status == ProfileStatus.DRAFT:
        return profile
    profile.versioned_update(user, status=ProfileStatus.DRAFT)
    record("catalog.profile_unpublished", obj=profile, actor=user, before={"status": ProfileStatus.PUBLISHED}, after={"status": ProfileStatus.DRAFT})
    bump(CACHE_NAMESPACE)
    emit_profile_event("catalog.profile_unpublished", profile)
    return profile
