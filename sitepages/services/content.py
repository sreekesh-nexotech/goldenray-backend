"""Editing what a page exposes: text slots, image slots and the SEO block (``pages/<uid>/text-slots/<key>/`` …).

Slots are declared by the registry (``sitepages.services.registry``); maintainers change only their *values*:
``value`` on a text slot (capped at ``max_length`` and checked against its ``kind``), ``asset``/``external_url``/
``alt`` on an image slot. The SEO row is created on the first SEO edit (reads never write; until then the page's
public ``seo`` is ``null``, as in the legacy CMS). Each slot/SEO row carries its own ``version`` for
``expected_version``; every change also touches the page aggregate (``pages.touch``: page version + 1, verification
voided, ``sitepages.page_updated`` when the page is live).
"""

from __future__ import annotations

import json
import re

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, validate_email
from django.db import transaction

from audit.services import changes, record, snapshot
from core.errors import DomainError, NotFound
from core.services import check_version, stamp_create
from media.models import MediaAsset
from seo.models import SchemaType
from sitepages.models import Page, PageImageSlot, PageSeo, PageTextSlot
from sitepages.services import pages

SEO_FIELDS = ("seo_title", "meta_description", "canonical_url", "og_title", "og_description", "og_image", "schema_type", "schema_extra", "noindex")
IMAGE_FIELDS = ("asset", "external_url", "alt")
SCHEMA_EXTRA_MAX_BYTES = 16 * 1024
PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()\-]{5,24}$")
_http_url = URLValidator(schemes=["http", "https"])
Kind = PageTextSlot.Kind


def invalid(field: str, message: str, summary: str = "The request is invalid.") -> DomainError:
    return DomainError("validation_error", summary, errors={field: [message]})


def public_image_problem(asset: MediaAsset | None) -> str | None:
    """Why ``asset`` cannot be shown on the website, or ``None`` when it can (live, public, an image, on the CDN)."""
    if asset is None:
        return None
    if asset.deleted_at is not None:
        return "The file has been deleted."
    if asset.kind != MediaAsset.Kind.IMAGE or not asset.is_image:
        return "Must be an image."
    if not asset.is_public or not asset.cdn_url:
        return "Must be a public file (it is shown on the website)."
    return None


# ── Text slots ──────────────────────────────────────────────────────────────────────────────────────────────────────
def text_value_problem(slot: PageTextSlot, value: str) -> str | None:
    """Server-side cap and kind check. An empty value is always allowed: the page falls back to its built-in text."""
    if slot.max_length and len(value) > slot.max_length:
        return f"This field holds up to {slot.max_length} characters — you have {len(value)}."
    if not value:
        return None
    if slot.kind == Kind.URL:
        if value.startswith("/") and not value.startswith("//"):
            return None if " " not in value else "Enter a valid URL."
        try:
            _http_url(value)
        except ValidationError:
            return "Enter a valid http(s) URL or a site path starting with '/'."
    elif slot.kind == Kind.EMAIL:
        try:
            validate_email(value)
        except ValidationError:
            return "Enter a valid e-mail address."
    elif slot.kind == Kind.PHONE and not PHONE_RE.match(value):
        return "Enter a valid phone number (digits, spaces, '+', '-', brackets)."
    elif slot.kind == Kind.SHORT_TEXT and ("\n" in value or "\r" in value):
        return "A short text is a single line."
    return None


def _slot(model, page: Page, key: str, label: str):
    slot = model.objects.select_for_update().filter(page=page, key=key).first()
    if slot is None:
        raise NotFound("slot_not_found", f"This page has no {label} slot '{key}'.")
    return slot


@transaction.atomic
def update_text_slot(page: Page, key: str, *, user, data: dict, expected_version=None) -> PageTextSlot:
    page = pages.lock(page)
    slot = _slot(PageTextSlot, page, key, "text")
    check_version(slot, expected_version)
    value = data.get("value", slot.value)
    problem = text_value_problem(slot, value)
    if problem:
        raise invalid("value", problem, "The text does not fit this slot.")
    if value == slot.value:
        return slot
    before = slot.value
    slot.versioned_update(user, value=value)
    record("sitepages.text_slot_updated", obj=slot, actor=user, before={"value": before}, after={"value": value}, note=f"{page.slug}:{key}")
    pages.touch(page, user)
    return slot


# ── Image slots ─────────────────────────────────────────────────────────────────────────────────────────────────────
@transaction.atomic
def update_image_slot(page: Page, key: str, *, user, data: dict, expected_version=None) -> PageImageSlot:
    page = pages.lock(page)
    slot = _slot(PageImageSlot, page, key, "image")
    check_version(slot, expected_version)
    if "asset" in data:
        problem = public_image_problem(data["asset"])
        if problem:
            raise invalid("asset", problem, "This file cannot be used on the website.")
    values = {name: data[name] for name in IMAGE_FIELDS if name in data and data[name] != getattr(slot, name)}
    if not values:
        return slot
    before = snapshot(slot, IMAGE_FIELDS)
    slot.versioned_update(user, **values)
    changed_before, changed_after = changes(before, snapshot(slot, IMAGE_FIELDS))
    record("sitepages.image_slot_updated", obj=slot, actor=user, before=changed_before, after=changed_after, note=f"{page.slug}:{key}")
    pages.touch(page, user)
    return slot


# ── SEO ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
def current_seo(page: Page) -> PageSeo:
    """The stored SEO row, or an unsaved one with the defaults (``uid`` null, version 1). Never writes."""
    return PageSeo.objects.select_related("og_image", "page").filter(page=page).first() or pages.default_seo(page)


def _validate_seo(data: dict) -> None:
    errors: dict[str, list[str]] = {}
    if "og_image" in data:
        problem = public_image_problem(data["og_image"])
        if problem:
            errors["og_image"] = [problem]
    if "schema_type" in data and data["schema_type"] not in SchemaType.values:
        errors["schema_type"] = [f"Choose one of: {', '.join(SchemaType.values)}."]
    if "schema_extra" in data:
        extra = data["schema_extra"]
        if not isinstance(extra, dict):
            errors["schema_extra"] = ["Must be an object."]
        elif len(json.dumps(extra, default=str)) > SCHEMA_EXTRA_MAX_BYTES:
            errors["schema_extra"] = [f"At most {SCHEMA_EXTRA_MAX_BYTES // 1024} KB of extra structured data."]
    if errors:
        raise DomainError("validation_error", "The SEO settings are invalid.", errors=errors)


@transaction.atomic
def update_seo(page: Page, *, user, data: dict, expected_version=None) -> PageSeo:
    page = pages.lock(page)
    unknown = sorted(set(data) - set(SEO_FIELDS))
    if unknown:
        raise DomainError("validation_error", "Unknown SEO fields.", errors={name: ["Not an editable field."] for name in unknown})
    seo = PageSeo.objects.select_for_update().filter(page=page).first()
    target = seo or pages.default_seo(page)
    check_version(target, expected_version)
    _validate_seo(data)
    values = {name: value for name, value in data.items() if value != getattr(target, name)}
    if not values:
        return target
    if seo is None:
        # First edit: create the row (version 1), then edit it (version 2) so a client holding the unsaved defaults'
        # version 1 cannot overwrite a concurrent first edit (same rule as the company profile).
        # The page row is locked, so concurrent first edits serialise here and the second one sees the row.
        seo = PageSeo(page=page)
        stamp_create(seo, user)
        seo.save()
        record("sitepages.page_seo_created", obj=seo, actor=user, note=page.slug)
    before = snapshot(seo, SEO_FIELDS)
    seo.versioned_update(user, **values)
    changed_before, changed_after = changes(before, snapshot(seo, SEO_FIELDS))
    record("sitepages.page_seo_updated", obj=seo, actor=user, before=changed_before, after=changed_after, note=page.slug)
    pages.touch(page, user)
    return seo
