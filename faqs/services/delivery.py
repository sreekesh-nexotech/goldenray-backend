"""The public FAQ payload (``GET /api/public/v1/faqs/?route=<route>[&section=][&category=]``) and the Studio preview.

The payload is the legacy CMS ``GET /api/faqs?page=<route>`` contract (parity-tested against the legacy server):
``{"data": [{id, question, answer, section, category, order}], "meta": {"page": {name, route}, "count", "schema"}}``
with one deliberate difference: ``id`` is the FAQ's ``uid`` (integer ids never leave the service layer; the website
uses it only as a list key). Rules kept from the legacy endpoint:

* the page is resolved by route whatever its status — the Next.js route renders regardless, and its FAQs with it;
  an unknown route is 404 ``page_not_found``, a missing route 400;
* only PUBLISHED FAQs, ordered by ``(section, sort_order, id)``;
* ``section`` present filters exactly (``section=`` selects the unnamed section), absent means every section;
* ``meta.schema`` is the FAQPage JSON-LD for exactly the returned rows (``seo.schema.faq_page``), ``null`` when none
  has both a question and an answer.

``category=<slug>`` (PLAN §3.3) narrows further. The list is bounded by :data:`PUBLIC_LIMIT` (no unbounded list).
"""

from __future__ import annotations

from core.errors import NotFound
from faqs.models import Faq
from faqs.services.categories import CACHE_NAMESPACE
from faqs.services.faqs import publish_errors
from seo import schema as schema_builders
from sitepages.models import Page
from sitepages.services.delivery import site_url
from sitepages.services.pages import CACHE_NAMESPACE as PAGES_NAMESPACE

PUBLIC_CACHE_NAMESPACES = [CACHE_NAMESPACE, PAGES_NAMESPACE]
PUBLIC_LIMIT = 200


def page_for_route(route: str) -> Page:
    page = Page.objects.filter(route=route).first()
    if page is None:
        raise NotFound("page_not_found", f"Unknown page '{route}'.")
    return page


def published_faqs(page: Page, *, section: str | None = None, category: str | None = None) -> list[Faq]:
    queryset = Faq.objects.filter(page=page, status=Faq.Status.PUBLISHED).select_related("category")
    if section is not None:
        queryset = queryset.filter(section=section)
    if category:
        queryset = queryset.filter(category__slug=category)
    return list(queryset.order_by("section", "sort_order", "id")[:PUBLIC_LIMIT])


def faq_row(faq: Faq) -> dict:
    return {
        "id": str(faq.uid),
        "question": faq.question,
        "answer": faq.answer,
        "section": faq.section,
        "category": faq.category.name if faq.category_id else None,
        "order": faq.sort_order,
    }


def faq_list(route: str, *, section: str | None = None, category: str | None = None) -> dict:
    page = page_for_route(route)
    rows = published_faqs(page, section=section, category=category)
    return {
        "data": [faq_row(faq) for faq in rows],
        "meta": {
            "page": {"name": page.title, "route": page.route},
            "count": len(rows),
            "schema": schema_builders.faq_page(rows, site_url=site_url(), page_url=page.route),
        },
    }


def preview(faq: Faq) -> dict:
    """The FAQ as the site will render it, and the FAQPage schema of the section it joins (itself included)."""
    siblings: list[Faq] = []
    if faq.page_id:
        siblings = [row for row in Faq.objects.filter(page=faq.page, section=faq.section, status=Faq.Status.PUBLISHED) if row.pk != faq.pk]
        siblings = sorted([*siblings, faq], key=lambda row: (row.sort_order, row.id))
    page_route = faq.page.route if faq.page_id else ""
    return {
        "question": faq.question,
        "answer": faq.answer,
        "status": faq.status,
        "page": {"uid": str(faq.page.uid), "name": faq.page.title, "route": faq.page.route} if faq.page_id else None,
        "section": faq.section,
        "position": faq.sort_order,
        "url": f"{site_url()}{page_route}" if page_route else None,
        "schema": schema_builders.faq_page(siblings, site_url=site_url(), page_url=page_route) if siblings else None,
        "publish_errors": publish_errors(faq),
        "seo_status": faq.seo_status(),
        "seo_issues": faq.seo_issues(),
    }
