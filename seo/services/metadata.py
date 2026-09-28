"""Page metadata (staff ``seo/metadata/`` CRUD, public ``seo/metadata/<page>/``) — replaces ``goldenray.Metadata``.

* ``page`` is the route key the frontend asks for: lower-case, no leading/trailing slash, ``home`` for ``/``
  (the legacy keys: ``home``, ``about``, ``projects/123`` …); unique among live rows (409 ``page_taken``);
* ``keywords`` is a list of distinct non-blank strings; ``og_image`` must be a public image and wins over
  ``og_image_url`` in the public payload;
* every write is versioned, audited, bumps ``seo:metadata`` and asks the website to revalidate that page
  (``website.revalidate_requested``).
"""

from __future__ import annotations

import re

from django.db import IntegrityError, transaction

from audit.services import changes, record, snapshot
from core.errors import Conflict, DomainError, NotFound
from core.outbox import emit
from core.services import check_version, stamp_create
from flarize.cache_utils import bump
from media.models import MediaAsset
from seo.models import PageMetadata
from seo.models.metadata import PAGE_KEY_REGEX

NAMESPACE = "seo:metadata"
FIELDS = ("page", "title", "description", "keywords", "og_type", "og_image_url", "og_image")
_PAGE_RE = re.compile(PAGE_KEY_REGEX)
_OG_TYPE_RE = re.compile(r"^[a-z][a-z0-9._:-]*$")
MAX_KEYWORDS = 50


def normalise_page(value: str) -> str:
    """``"/About/"`` → ``"about"``; ``"/"`` or ``""`` → ``"home"``."""
    page = (value or "").strip().strip("/").lower()
    return page or "home"


def page_path(page: str) -> str:
    return "/" if page == "home" else f"/{page}"


def _invalid(field: str, message: str) -> DomainError:
    return DomainError("validation_error", message, errors={field: [message]})


def _validate(values: dict) -> dict:
    if "page" in values:
        values["page"] = normalise_page(values["page"])
        if len(values["page"]) > 200 or not _PAGE_RE.match(values["page"]):
            raise _invalid("page", "A route such as 'about' or 'projects/123' (lower-case letters, digits, '-', '_', '.', '~', '/').")
    if "keywords" in values:
        keywords = [str(keyword).strip() for keyword in values["keywords"] or []]
        if any(not keyword or len(keyword) > 100 for keyword in keywords) or len(keywords) > MAX_KEYWORDS:
            raise _invalid("keywords", f"Up to {MAX_KEYWORDS} non-blank keywords of at most 100 characters.")
        values["keywords"] = list(dict.fromkeys(keywords))
    if "og_type" in values and not _OG_TYPE_RE.match(values["og_type"] or ""):
        raise _invalid("og_type", "An Open Graph type such as 'website' or 'article'.")
    image = values.get("og_image")
    if image is not None and (image.deleted_at is not None or not image.is_public or image.kind not in (MediaAsset.Kind.IMAGE, MediaAsset.Kind.PHOTO)):
        raise DomainError("invalid_media", "The Open Graph image must be a public image.", errors={"og_image_uid": ["Must be a public image."]})
    return values


def _taken() -> Conflict:
    return Conflict("page_taken", "Metadata for this page already exists.", errors={"page": ["Already in use."]})


def _revalidate(*pages: str) -> None:
    emit("website.revalidate_requested", {"reason": "seo_metadata", "paths": list(dict.fromkeys(page_path(page) for page in pages))}, aggregate_type="seo.pagemetadata")


def metadata_queryset():
    return PageMetadata.objects.select_related("og_image").order_by("page", "id")


def public_metadata(page: str) -> PageMetadata:
    row = metadata_queryset().filter(page=normalise_page(page)).first()
    if row is None:
        raise NotFound("page_metadata_not_found", f"No metadata for page '{normalise_page(page)}'.")
    return row


def image_url(row: PageMetadata) -> str:
    asset = row.og_image
    if asset is not None and asset.deleted_at is None and asset.is_public and asset.cdn_url:
        return asset.cdn_url
    return row.og_image_url


@transaction.atomic
def create_metadata(*, user, data) -> PageMetadata:
    values = _validate({name: data[name] for name in FIELDS if name in data})
    row = PageMetadata(**values)
    stamp_create(row, user)
    try:
        with transaction.atomic():
            row.save()
    except IntegrityError:
        raise _taken() from None
    record("seo.page_metadata_created", obj=row, actor=user, after=snapshot(row, FIELDS))
    bump(NAMESPACE)
    _revalidate(row.page)
    return row


@transaction.atomic
def update_metadata(instance: PageMetadata, *, user, data, expected_version=None) -> PageMetadata:
    row = PageMetadata.objects.select_for_update().get(pk=instance.pk)
    check_version(row, expected_version)
    values = _validate({name: data[name] for name in FIELDS if name in data})
    values = {name: value for name, value in values.items() if value != getattr(row, name)}
    if not values:
        return row
    before, old_page = snapshot(row, FIELDS), row.page
    try:
        with transaction.atomic():
            row.versioned_update(user, **values)
    except IntegrityError:
        raise _taken() from None
    changed_before, changed_after = changes(before, snapshot(row, FIELDS))
    record("seo.page_metadata_updated", obj=row, actor=user, before=changed_before, after=changed_after)
    bump(NAMESPACE)
    _revalidate(old_page, row.page)
    return row


@transaction.atomic
def delete_metadata(instance: PageMetadata, *, user, expected_version=None) -> None:
    row = PageMetadata.objects.select_for_update().get(pk=instance.pk)
    check_version(row, expected_version)
    row.soft_delete(user)
    record("seo.page_metadata_deleted", obj=row, actor=user, before=snapshot(row, FIELDS))
    bump(NAMESPACE)
    _revalidate(row.page)
