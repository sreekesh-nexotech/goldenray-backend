"""Public delivery (``/api/public/<v>/content/<collection>/``): the legacy CMS Strapi-v5-flat query contract.

The query language is the legacy CMS subset, kept exactly (``cms/delivery/query.py``): ``populate`` (accepted,
responses are always fully populated), ``filters[<field>][<$op>]``, ``fields[n]``, ``pagination[page]`` /
``pagination[pageSize]`` (default 25, clamped 1…200) and ``sort[n]`` (``field``, ``field:asc``, ``field:desc``).
Filtering and sorting resolve through one closed camelCase → column map; anything else is silently ignored — that
map is the security boundary. Serves **published** entries of **active** collections only.

Fixed legacy weaknesses (CMS_BLUEPRINT §17): a value that does not parse for its column (``isFeatured=yes``,
``readTime=abc``) is a 400 ``invalid_filter`` instead of a 500 (#1); ``$in`` with one comma-separated value is split
(#11) and the Strapi indexed form ``filters[f][$in][0]=`` is understood; ``fields=a,b`` is split. Boolean values
accept ``true/false/1/0/t/f/yes/no`` in any case (Django's parser only accepted ``True``/``t``/``1``).

A lookup for exactly one slug (``$eq``/``$eqi`` on ``slug``, no other filter) that matches nothing is retried against
the entry's active slug history; a hit returns the entry under its current slug and ``meta.redirect``.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass, field

from django.db.models import Prefetch, QuerySet
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from blog.models import Badge, Category, Collection, ContentBlock, Entry, EntryAttributeValue, EntryImage, EntrySlugHistory, Tag, TemplateImageGroup
from core.errors import DomainError, NotFound

FIELD_MAP = {
    "slug": "slug",
    "title": "title",
    "documentId": "uid",
    "isFeatured": "is_featured",
    "sortOrder": "sort_order",
    "publishedOn": "published_on",
    "publishedAt": "published_at",
    "updatedAt": "updated_at",
    "readTime": "read_time",
}
COLUMN_TYPES = {
    "slug": "text",
    "title": "text",
    "uid": "uuid",
    "is_featured": "bool",
    "sort_order": "int",
    "published_on": "datetime",
    "published_at": "datetime",
    "updated_at": "datetime",
    "read_time": "int",
}
OP_MAP = {
    "$eq": "exact",
    "$eqi": "iexact",
    "$in": "in",
    "$contains": "icontains",
    "$ne": "exact",  # applied with exclude()
    "$null": "isnull",
    "$gt": "gt",
    "$gte": "gte",
    "$lt": "lt",
    "$lte": "lte",
}
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 25

_FILTER_RE = re.compile(r"^filters\[(?P<field>[^\]]+)\]\[(?P<op>\$[a-zA-Z]+)\](?P<index>\[\d+\])?$")
_FIELDS_RE = re.compile(r"^fields\[\d+\]$")
_SORT_RE = re.compile(r"^sort\[\d+\]$")
_TRUE = {"true", "1", "t", "yes", "y", "on"}
_FALSE = {"false", "0", "f", "no", "n", "off"}


@dataclass
class DeliveryQuery:
    filters: dict = field(default_factory=dict)
    excludes: dict = field(default_factory=dict)
    fields: list[str] | None = None
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    sort: list[str] = field(default_factory=list)
    single_slug: str | None = None


def _bad(param: str, message: str) -> DomainError:
    return DomainError("invalid_filter", f"{param}: {message}", errors={param: [message]})


def _coerce(column: str, raw: str, param: str):
    kind = COLUMN_TYPES[column]
    if kind == "text":
        return raw
    if kind == "int":
        try:
            return int(str(raw).strip())
        except ValueError:
            raise _bad(param, "must be a whole number.") from None
    if kind == "bool":
        text = str(raw).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise _bad(param, "must be true or false.")
    if kind == "uuid":
        try:
            return uuid.UUID(str(raw).strip())
        except ValueError:
            raise _bad(param, "must be a UUID.") from None
    text = str(raw).strip()
    value = parse_datetime(text)
    if value is None:
        day = parse_date(text)
        if day is None:
            raise _bad(param, "must be an ISO 8601 date or date-time.")
        value = dt.datetime.combine(day, dt.time.min)
    if timezone.is_naive(value):
        value = timezone.make_aware(value)  # as Django does for a naive value (the legacy server's behaviour)
    return value


def _in_values(params, keys: list[str]) -> list[str]:
    values: list[str] = []
    for key in keys:
        values.extend(params.getlist(key))
    if len(values) == 1 and "," in values[0]:
        values = [value for value in values[0].split(",")]
    return values


def parse_filters(params) -> tuple[dict, dict]:
    """``(filter_kwargs, exclude_kwargs)`` for the ORM; unknown fields/operators are ignored, bad values are 400."""
    filters, excludes = {}, {}
    in_keys: dict[str, list[str]] = {}
    for raw_key in params.keys():
        match = _FILTER_RE.match(raw_key)
        if not match:
            continue
        column, op = FIELD_MAP.get(match.group("field")), match.group("op")
        if column is None or op not in OP_MAP:
            continue
        if match.group("index") and op != "$in":
            continue
        if op == "$in":
            in_keys.setdefault(column, []).append(raw_key)
            continue
        value = params.get(raw_key)
        if op == "$null":
            filters[f"{column}__isnull"] = str(value).lower() in ("1", "true", "yes")
        elif op == "$contains":
            filters[f"{column}__icontains"] = value
        elif op == "$ne":
            excludes[f"{column}__exact"] = _coerce(column, value, raw_key)
        else:
            filters[f"{column}__{OP_MAP[op]}"] = _coerce(column, value, raw_key)
    for column, keys in in_keys.items():
        filters[f"{column}__in"] = [_coerce(column, value, keys[0]) for value in _in_values(params, sorted(keys))]
    return filters, excludes


def single_slug_lookup(params) -> str | None:
    """The slug when the query is unambiguously a lookup for exactly one (``$eq``/``$eqi`` on slug, no other filter)."""
    slug = None
    for raw_key in params.keys():
        match = _FILTER_RE.match(raw_key)
        if not match:
            continue
        if match.group("field") != "slug" or match.group("op") not in ("$eq", "$eqi") or match.group("index"):
            return None
        if slug is not None:
            return None
        values = params.getlist(raw_key)
        if len(values) != 1:
            return None
        slug = values[0]
    return slug or None


def parse_fields(params) -> list[str] | None:
    requested: list[str] = []
    for raw_key in params.keys():
        if _FIELDS_RE.match(raw_key) or raw_key == "fields":
            for value in params.getlist(raw_key):
                requested.extend(part for part in value.split(",") if part)
    return requested or None


def parse_pagination(params) -> tuple[int, int]:
    try:
        page = max(1, int(params.get("pagination[page]", "1")))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(params.get("pagination[pageSize]", str(DEFAULT_PAGE_SIZE)))
    except (TypeError, ValueError):
        page_size = DEFAULT_PAGE_SIZE
    return page, max(1, min(page_size, MAX_PAGE_SIZE))


def parse_sort(params) -> list[str]:
    raw: list[str] = []
    for raw_key in params.keys():
        if _SORT_RE.match(raw_key) or raw_key == "sort":
            raw.extend(params.getlist(raw_key))
    tokens = []
    for item in raw:
        name, _, direction = item.partition(":")
        column = FIELD_MAP.get(name.strip())
        if column:
            tokens.append(f"-{column}" if direction.strip().lower() == "desc" else column)
    return tokens


def parse_query(params) -> DeliveryQuery:
    filters, excludes = parse_filters(params)
    page, page_size = parse_pagination(params)
    return DeliveryQuery(filters=filters, excludes=excludes, fields=parse_fields(params), page=page, page_size=page_size, sort=parse_sort(params), single_slug=single_slug_lookup(params))


# ── Querysets ────────────────────────────────────────────────────────────────────────────────────────────────────────
def active_collection(api_uid: str) -> Collection:
    """The active collection behind a public route; unknown **or** inactive answer 404 identically."""
    collection = Collection.objects.filter(api_uid=api_uid, is_active=True).first()
    if collection is None:
        raise NotFound("collection_not_found", f"Unknown collection '{api_uid}'.")
    return collection


# ``attributes`` keeps the order the values were written in (the CMS delivered its rows in insertion order, and both
# the importer and the replace-all writer insert them in source/client order), never an alphabetical re-sort.
ATTRIBUTE_ORDER = ("id",)


def with_payload_relations(queryset: QuerySet, *, full: bool = True) -> QuerySet:
    """Everything the flat payload embeds, in a fixed number of queries (no N+1)."""
    if not full:
        return queryset.prefetch_related(Prefetch("attribute_values", queryset=EntryAttributeValue.objects.order_by(*ATTRIBUTE_ORDER)))
    return queryset.select_related("collection", "template", "author", "cover_image", "seo").prefetch_related(
        Prefetch("template__image_groups", queryset=TemplateImageGroup.objects.order_by("position", "id")),
        Prefetch("categories", queryset=Category.objects.order_by("name", "id")),
        Prefetch("tags", queryset=Tag.objects.order_by("name", "id")),
        Prefetch("badges", queryset=Badge.objects.order_by("name", "id")),
        Prefetch("content_blocks", queryset=ContentBlock.objects.order_by("position", "delivery_id")),
        Prefetch("images", queryset=EntryImage.objects.select_related("media_asset").order_by("group_key", "position", "id")),
        Prefetch("attribute_values", queryset=EntryAttributeValue.objects.order_by(*ATTRIBUTE_ORDER)),
    )


def published_entries(collection: Collection) -> QuerySet:
    return Entry.objects.filter(collection=collection, status=Entry.Status.PUBLISHED)


def resolve_alias(collection: Collection, slug: str) -> Entry | None:
    """The **published** entry an active alias points at (never resurrects a drafted or archived entry)."""
    alias = (
        EntrySlugHistory.objects.filter(slug=slug, active=True, collection=collection, entry__status=Entry.Status.PUBLISHED, entry__deleted_at__isnull=True)
        .order_by("-created_at", "-id")
        .select_related("entry")
        .first()
    )
    return alias.entry if alias else None


@dataclass
class DeliveryPage:
    entries: list[Entry]
    total: int
    page: int
    page_size: int
    fields: list[str] | None
    redirect: dict | None = None


def query_entries(collection: Collection, query: DeliveryQuery, *, base: QuerySet | None = None) -> DeliveryPage:
    queryset = base if base is not None else published_entries(collection)
    if query.filters:
        queryset = queryset.filter(**query.filters)
    if query.excludes:
        queryset = queryset.exclude(**query.excludes)
    if query.sort:
        queryset = queryset.order_by(*query.sort, "delivery_id")
    total = queryset.count()
    redirect = None
    if total == 0 and not query.excludes and query.single_slug:
        aliased = resolve_alias(collection, query.single_slug)
        if aliased is not None:
            queryset, total = published_entries(collection).filter(pk=aliased.pk), 1
            redirect = {"from": query.single_slug, "to": aliased.slug, "reason": "slug_changed"}
    start = (query.page - 1) * query.page_size
    entries = list(with_payload_relations(queryset, full=not query.fields)[start : start + query.page_size])
    return DeliveryPage(entries=entries, total=total, page=query.page, page_size=query.page_size, fields=query.fields, redirect=redirect)


def entry_by_slug(collection: Collection, slug: str, query: DeliveryQuery) -> DeliveryPage:
    """``content/<collection>/<slug>/``: the one-item list payload (alias resolution included), 404 when absent."""
    single = DeliveryQuery(fields=query.fields, page=1, page_size=DEFAULT_PAGE_SIZE, single_slug=slug, filters={"slug__exact": slug})
    page = query_entries(collection, single)
    if not page.entries:
        raise NotFound("entry_not_found", f"No published entry '{slug}' in '{collection.api_uid}'.")
    return page


def preview_page(entry: Entry, query: DeliveryQuery) -> DeliveryPage:
    """The previewed entry (any status) as a one-item page."""
    entries = list(with_payload_relations(Entry.objects.filter(pk=entry.pk), full=not query.fields))
    return DeliveryPage(entries=entries, total=len(entries), page=1, page_size=DEFAULT_PAGE_SIZE, fields=query.fields)
