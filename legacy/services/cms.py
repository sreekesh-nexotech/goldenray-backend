"""Old CMS delivery (``/studio-api/api/…``): ``<collection>``, ``faqs``, ``job-positions[/<slug>]``, ``page-content``.

The canonical delivery services already answer the CMS payloads (blog: the same bytes; FAQs, pages, careers: the
same keys and values with the platform ``uid`` for the integer ``id`` — docs/decisions/content-*.md,
careers-reference.md). The shim only puts the legacy integer ids back (``core_legacy_map`` CMS ``faqs_faq`` /
``careers_job_position``) and answers the legacy CMS ``Http404`` bodies (``{"detail": "Unknown page '/x'"}``).
"""

from __future__ import annotations

import re

from blog.serializers.delivery import page_body
from blog.services import delivery as blog_delivery
from blog.services.common import DELIVERY_NAMESPACES
from careers.models import JobPosition
from careers.services import public as careers_public
from core.errors import NotFound
from faqs.models import Faq
from faqs.services import delivery as faq_delivery
from legacy.services.ids import CMS, legacy_ids
from sitepages.services import delivery as page_delivery


class LegacyNotFound(NotFound):
    """404 ``{"detail": message}`` (the CMS raised ``Http404(message)``)."""


INDEXED_FILTER = re.compile(r"^filters\[[^\]]+\]\[\$[a-zA-Z]+\]\[\d+\]$")


def legacy_params(params):
    """The query without the indexed Strapi spelling ``filters[f][$in][0]`` — the CMS ignored those keys (its filter
    pattern had no index); the canonical delivery understands them."""
    kept = params.copy()
    for key in [key for key in kept.keys() if INDEXED_FILTER.match(key)]:
        del kept[key]
    return kept


def collection(api_uid: str, params) -> dict:
    try:
        found = blog_delivery.active_collection(api_uid)
    except NotFound:
        raise LegacyNotFound("collection_not_found", f"Unknown collection '{api_uid}'") from None
    return page_body(blog_delivery.query_entries(found, blog_delivery.parse_query(legacy_params(params))))


def _uids_to_ids(model, uids, table: str) -> dict[str, int]:
    pks = dict(model.all_objects.filter(uid__in=list(uids)).values_list("uid", "pk"))
    ids = legacy_ids(model, pks.values(), system=CMS, table=table)
    return {str(uid): ids[pk] for uid, pk in pks.items()}


def faqs(params) -> dict:
    route = params.get("page")
    if not route:
        raise LegacyNotFound("page_required", "A 'page' query parameter is required.")
    try:
        body = faq_delivery.faq_list(route, section=params.get("section"))
    except NotFound:
        raise LegacyNotFound("page_not_found", f"Unknown page '{route}'") from None
    ids = _uids_to_ids(Faq, [row["id"] for row in body["data"]], "faqs_faq")
    body["data"] = [{**row, "id": ids.get(row["id"])} for row in body["data"]]
    return body


def _card(row: dict, ids: dict[str, int]) -> dict:
    return {("id" if key == "uid" else key): (ids.get(value) if key == "uid" else value) for key, value in row.items()}


def job_positions(params) -> dict:
    body = careers_public.public_list(department=params.get("department") or None)
    ids = _uids_to_ids(JobPosition, [row["uid"] for row in body["data"]], "careers_job_position")
    body["data"] = [_card(row, ids) for row in body["data"]]
    return body


def job_position(slug: str) -> dict:
    try:
        body = careers_public.public_detail(slug)
    except NotFound:
        raise LegacyNotFound("not_found", "This position does not exist.") from None
    ids = _uids_to_ids(JobPosition, [body["data"]["uid"]], "careers_job_position")
    body["data"] = _card(body["data"], ids)
    return body


def page_content(params) -> dict:
    route = params.get("route")
    if not route:
        raise LegacyNotFound("route_required", "A 'route' query parameter is required.")
    try:
        return page_delivery.page_content(page_delivery.published_page(route=route))
    except NotFound:
        raise LegacyNotFound("page_not_found", f"Unknown page '{route}'") from None


COLLECTION_NAMESPACES = list(DELIVERY_NAMESPACES)
FAQ_NAMESPACES = faq_delivery.PUBLIC_CACHE_NAMESPACES
PAGE_NAMESPACES = page_delivery.PUBLIC_CACHE_NAMESPACES
CAREERS_NAMESPACES = careers_public.CACHE_NAMESPACES
