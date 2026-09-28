"""The public page payload (``GET /api/public/v1/pages/<slug>/`` and ``…/pages/?route=``) and the Studio preview.

The payload is the legacy CMS ``GET /api/page-content?route=`` contract, field for field (parity-tested against the
legacy server): ``{"data": {route, name, images, text, seo}}``. The website merges it over its built-in defaults, so

* ``images`` maps every declared image slot's ``key`` to ``{url, alt, width, height}`` or ``null`` — "nobody has
  replaced this yet" must stay distinguishable from a replaced image;
* ``text`` carries only slots with a non-empty value;
* ``seo`` is ``null`` until the page's SEO was first edited; ``seo.schema`` is the WebPage JSON-LD generated from the
  record (``seo.schema.web_page``) when the schema type is WebPage, else ``null``.

Only PUBLISHED pages resolve (404 ``page_not_found`` otherwise). Image URLs are the public CDN URLs of the media
assets (``media_asset.cdn_url``); a slot without an asset may point at an ``external_url``.
"""

from __future__ import annotations

from django.conf import settings

from company.services.profile import current_profile
from core.errors import NotFound
from media.models import MediaAsset
from seo import schema as schema_builders
from seo.models import SchemaType
from sitepages.models import Page, PageImageSlot, PageSeo
from sitepages.services import pages

PUBLIC_CACHE_NAMESPACES = [pages.CACHE_NAMESPACE, "media", "company"]


def site_url() -> str:
    return (getattr(settings, "FRONTEND_BASE_URL", "") or "").rstrip("/")


def published_page(*, slug: str | None = None, route: str | None = None) -> Page:
    queryset = pages.with_content(Page.objects.filter(status=Page.Status.PUBLISHED))
    page = queryset.filter(slug=slug).first() if slug is not None else queryset.filter(route=route).first()
    if page is None:
        raise NotFound("page_not_found", f"Unknown page '{slug if slug is not None else route}'.")
    return page


def asset_payload(asset: MediaAsset | None) -> dict | None:
    if asset is None or asset.deleted_at is not None or not asset.is_public or not asset.cdn_url:
        return None
    return {"url": asset.cdn_url, "alt": asset.alternative_text, "width": asset.width, "height": asset.height}


def image_payload(slot: PageImageSlot) -> dict | None:
    image = asset_payload(slot.asset) if slot.asset_id else None
    if image is not None:
        return {**image, "alt": slot.alt or image["alt"]}
    if slot.external_url:
        return {"url": slot.external_url, "alt": slot.alt, "width": None, "height": None}
    return None


def page_schema(seo: PageSeo) -> dict | None:
    if seo.schema_type != SchemaType.WEB_PAGE:
        return None
    return schema_builders.web_page(seo, site_url=site_url(), organisation=current_profile().display_name)


def seo_payload(seo: PageSeo | None) -> dict | None:
    if seo is None:
        return None
    return {
        "title": seo.seo_title,
        "description": seo.meta_description,
        "canonical_url": seo.canonical_url,
        "noindex": seo.noindex,
        "og_image": asset_payload(seo.og_image) if seo.og_image_id else None,
        "schema": page_schema(seo),
    }


def _seo_of(page: Page) -> PageSeo | None:
    try:
        return page.seo
    except PageSeo.DoesNotExist:
        return None


def page_data(page: Page) -> dict:
    return {
        "route": page.route,
        "name": page.title,
        "images": {slot.key: image_payload(slot) for slot in page.image_slots.all()},
        "text": {slot.key: slot.value for slot in page.text_slots.all() if slot.value},
        "seo": seo_payload(_seo_of(page)),
    }


def page_content(page: Page) -> dict:
    """``{"data": …}`` exactly as the legacy ``/api/page-content`` served it."""
    return {"data": page_data(page)}


def preview(page: Page) -> dict:
    """What the website will emit for this page (any status), plus the outstanding SEO issues (Studio preview)."""
    seo = _seo_of(page)
    effective = seo or pages.default_seo(page)
    return {
        "url": f"{site_url()}{page.route}",
        "status": page.status,
        "title": effective.seo_title or page.title,
        "description": effective.meta_description,
        "noindex": effective.noindex,
        "schema": page_schema(effective),
        "seo_status": effective.seo_status(),
        "seo_issues": effective.seo_issues(),
        "content": page_data(page),
    }
