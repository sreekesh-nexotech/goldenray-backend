"""The Strapi-v5 **flat** delivery payload (legacy ``cms/delivery/serializers.py``), byte for byte.

Every key the legacy CMS emitted is emitted here, in the same order, with the same null/empty conventions; the
parity suite (``blog/tests/test_delivery_parity.py``) compares response bytes with responses captured from the
legacy server. ``id`` values are the public ``delivery_id`` numbers (the CMS primary keys preserved on import,
DV-17), never table keys. Empty text columns are delivered as ``null`` exactly where the CMS stored ``NULL``
(``seo.*``, ``author.bio``/``role``, ``warning``/``insights`` after the attribute fallback).

The DRF serializers at the bottom only document the shape in OpenAPI; the payload itself is built by the functions.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers

from blog.services.delivery import DeliveryPage

SCALAR_FIELDS = ("id", "documentId", "title", "slug", "excerpt", "summary", "introduction", "readTime", "isFeatured", "sortOrder", "publishedOn", "updatedAt", "publishedAt", "warning", "insights")


def _iso(value):
    return value.isoformat() if value else None


def absolute_media_url(url: str | None) -> str | None:
    """Fully-qualified URLs only: CDN and external URLs pass through; a site-relative one gets the public base."""
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    return f"{getattr(settings, 'PUBLIC_MEDIA_BASE_URL', '')}{url}"


def _asset_url(asset) -> str | None:
    """One place for "CDN URL of a live public asset" (legacy weakness §17 #5: the rule lived in three places)."""
    if asset is None or asset.deleted_at is not None or not asset.is_public:
        return None
    return asset.cdn_url or None


def image_url(image) -> str | None:
    return image.external_url or _asset_url(image.media_asset)


def media_payload(asset) -> dict | None:
    url = _asset_url(asset)
    if not url:
        return None
    return {"url": absolute_media_url(url), "width": asset.width, "height": asset.height, "alternativeText": asset.alternative_text or None}


def _img_urls(entry) -> dict | None:
    """``imgUrls``: single groups → a URL string, repeatable groups → an array; the template decides, else inferred."""
    repeatable = {group.key: group.repeatable for group in entry.template.image_groups.all()} if entry.template_id and entry.template.deleted_at is None else {}
    grouped: dict[str, list] = {}
    for image in entry.images.all():  # ordered by (group_key, position, id)
        url = image_url(image)
        if url:
            grouped.setdefault(image.group_key, []).append(absolute_media_url(url))
    if not grouped:
        return None
    out = {}
    for key, urls in grouped.items():
        is_repeatable = repeatable.get(key)
        if is_repeatable is None:
            is_repeatable = len(urls) > 1
        out[key] = urls if is_repeatable else urls[0]
    return out


def _cover(entry) -> dict | None:
    """``coverImage``: the cover FK wins; else the first image of the well-known ``coverImg`` group."""
    cover = media_payload(entry.cover_image)
    if cover is not None:
        return cover
    for image in entry.images.all():
        if image.group_key != "coverImg":
            continue
        if image.media_asset_id:
            return media_payload(image.media_asset)
        if image.external_url:
            return {"url": absolute_media_url(image.external_url), "width": None, "height": None, "alternativeText": None}
    return None


def _live_seo(entry):
    try:
        seo = entry.seo  # select_related cache (or one query)
    except ObjectDoesNotExist:
        return None
    return seo if seo.deleted_at is None else None


def _attrs(entry) -> dict:
    return {value.slot_key: value.value for value in entry.attribute_values.all()}


def _scalar(value, attrs: dict, slot_key: str):
    """Legacy resolution: the typed column wins; the attribute slot of the same name is the fallback."""
    if value not in (None, ""):
        return value
    fallback = attrs.get(slot_key)
    return fallback if fallback not in (None, "") else None


def _scalars(entry, attrs: dict) -> dict:
    return {
        "id": entry.delivery_id,
        "documentId": str(entry.uid),
        "title": entry.title,
        "slug": entry.slug,
        "excerpt": entry.excerpt or "",
        "summary": entry.summary or [],
        "introduction": entry.introduction or [],
        "readTime": _scalar(entry.read_time, attrs, "readTime"),
        "isFeatured": entry.is_featured,
        "sortOrder": entry.sort_order,
        "publishedOn": _iso(entry.published_on),
        "updatedAt": _iso(entry.updated_at),
        "publishedAt": _iso(entry.published_at),
    }


def entry_payload(entry) -> dict:
    """The full flat payload of one entry (the ``populate=*`` shape, 24 keys)."""
    attrs = _attrs(entry)
    author = None
    if entry.author_id and entry.author.deleted_at is None:
        author = {"id": entry.author.delivery_id, "name": entry.author.name, "bio": entry.author.bio or None, "role": entry.author.role or None}
    seo = _live_seo(entry)
    return {
        **_scalars(entry, attrs),
        "author": author,
        "categories": [{"id": category.delivery_id, "name": category.name, "slug": category.slug} for category in entry.categories.all()],
        "tags": [{"id": tag.delivery_id, "name": tag.name} for tag in entry.tags.all()],
        "badges": [{"id": badge.delivery_id, "label": badge.name, "color": badge.color} for badge in entry.badges.all()],
        "contentBlocks": [{"__component": block.component, "id": block.delivery_id, "body": block.data or []} for block in entry.content_blocks.all()],
        "coverImage": _cover(entry),
        "warning": _scalar(entry.warning, attrs, "warning"),
        "insights": _scalar(entry.insights, attrs, "insights"),
        "seo": (
            {"metaTitle": seo.seo_title or None, "metaDescription": seo.meta_description or None, "canonicalUrl": seo.canonical_url or None, "keywords": seo.keywords or None}
            if seo is not None
            else None
        ),
        "imgUrls": _img_urls(entry),
        "attributes": attrs or None,
    }


def entry_fields_payload(entry, fields: list[str]) -> dict:
    """``fields[]=`` selection: scalars only, ``id`` and ``documentId`` always kept, in payload order."""
    attrs = _attrs(entry)
    wanted = {"id", "documentId"} | set(fields)
    full = {**_scalars(entry, attrs), "warning": _scalar(entry.warning, attrs, "warning"), "insights": _scalar(entry.insights, attrs, "insights")}
    return {key: full[key] for key in SCALAR_FIELDS if key in wanted}


def page_body(page: DeliveryPage, *, extra_meta: dict | None = None) -> dict:
    data = [entry_fields_payload(entry, page.fields) for entry in page.entries] if page.fields else [entry_payload(entry) for entry in page.entries]
    page_count = (page.total + page.page_size - 1) // page.page_size
    body = {"data": data, "meta": {"pagination": {"page": page.page, "pageSize": page.page_size, "pageCount": page_count, "total": page.total}}}
    if page.redirect is not None:
        body["meta"]["redirect"] = page.redirect
    if extra_meta:
        body["meta"].update(extra_meta)
    return body


# ── OpenAPI documentation of the flat shape ─────────────────────────────────────────────────────────────────────────
class _MediaSerializer(serializers.Serializer):
    url = serializers.URLField()
    width = serializers.IntegerField(allow_null=True)
    height = serializers.IntegerField(allow_null=True)
    alternativeText = serializers.CharField(allow_null=True)  # noqa: N815 - Strapi contract


class _AuthorSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    bio = serializers.CharField(allow_null=True)
    role = serializers.CharField(allow_null=True)


class _CategorySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    slug = serializers.CharField()


class _TagSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class _BadgeSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    label = serializers.CharField()
    color = serializers.CharField()


# ``__component`` and ``from`` cannot be written as class attributes (name mangling, keyword).
_BlockSerializer = type("DeliveryBlockSerializer", (serializers.Serializer,), {"__component": serializers.CharField(), "id": serializers.IntegerField(), "body": serializers.JSONField()})


class _SeoSerializer(serializers.Serializer):
    metaTitle = serializers.CharField(allow_null=True)  # noqa: N815
    metaDescription = serializers.CharField(allow_null=True)  # noqa: N815
    canonicalUrl = serializers.CharField(allow_null=True)  # noqa: N815
    keywords = serializers.CharField(allow_null=True)


class DeliveryEntrySerializer(serializers.Serializer):
    id = serializers.IntegerField(help_text="public numeric id (Strapi contract)")
    documentId = serializers.UUIDField(help_text="stable entry identity (= uid)")  # noqa: N815
    title = serializers.CharField()
    slug = serializers.CharField()
    excerpt = serializers.CharField()
    summary = serializers.JSONField(help_text="Strapi-blocks array")
    introduction = serializers.JSONField(help_text="Strapi-blocks array")
    readTime = serializers.IntegerField(allow_null=True)  # noqa: N815
    isFeatured = serializers.BooleanField()  # noqa: N815
    sortOrder = serializers.IntegerField(allow_null=True)  # noqa: N815
    publishedOn = serializers.DateTimeField(allow_null=True)  # noqa: N815
    updatedAt = serializers.DateTimeField()  # noqa: N815
    publishedAt = serializers.DateTimeField(allow_null=True)  # noqa: N815
    author = _AuthorSerializer(allow_null=True, required=False)
    categories = _CategorySerializer(many=True, required=False)
    tags = _TagSerializer(many=True, required=False)
    badges = _BadgeSerializer(many=True, required=False)
    contentBlocks = _BlockSerializer(many=True, required=False)  # noqa: N815
    coverImage = _MediaSerializer(allow_null=True, required=False)  # noqa: N815
    warning = serializers.CharField(allow_null=True, required=False)
    insights = serializers.CharField(allow_null=True, required=False)
    seo = _SeoSerializer(allow_null=True, required=False)
    imgUrls = serializers.JSONField(allow_null=True, required=False, help_text="{groupKey: url | [url, …]}")  # noqa: N815
    attributes = serializers.JSONField(allow_null=True, required=False, help_text="{slotKey: value}")


class _PaginationSerializer(serializers.Serializer):
    page = serializers.IntegerField()
    pageSize = serializers.IntegerField()  # noqa: N815
    pageCount = serializers.IntegerField()  # noqa: N815
    total = serializers.IntegerField()


_RedirectSerializer = type("DeliveryRedirectSerializer", (serializers.Serializer,), {"from": serializers.CharField(), "to": serializers.CharField(), "reason": serializers.CharField()})


class _MetaSerializer(serializers.Serializer):
    pagination = _PaginationSerializer()
    redirect = _RedirectSerializer(required=False, help_text="present when an old slug resolved to the entry's current one")
    preview = serializers.BooleanField(required=False, help_text="present on preview responses")


class DeliveryResponseSerializer(serializers.Serializer):
    data = DeliveryEntrySerializer(many=True)
    meta = _MetaSerializer()
