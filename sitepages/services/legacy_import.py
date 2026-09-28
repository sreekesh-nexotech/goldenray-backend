"""Import of the legacy CMS ``sitepages_*`` tables (PLAN §7.2 #8). Called by ``migrations_tools`` (``import_cms``).

Each function takes the source rows as plain dicts (column names as in the legacy schema, timestamps as ISO strings or
datetimes) and returns ``{"created", "updated", "skipped", "violations"}``:

* **idempotent** through ``core_legacy_map`` (``CMS``, ``<source table>``, ``<source id>``): a mapped row is updated
  when the source changed and skipped otherwise; an unmapped row is first matched on its natural key (a page by
  ``route`` — e.g. a page the registry already created —, a slot by ``(page, key)``, SEO by page) and linked, else
  created. Re-running never duplicates;
* source timestamps and attribution are preserved (``created_by``/``updated_by`` through the imported users' map);
* **violations** are listed, never raised: a row that cannot be imported (unknown status, invalid route, unmapped
  page, slug collision) is skipped; a reference that cannot be resolved (unmapped user or media asset) is imported
  as empty; a value the new rules would refuse (text over its cap) is kept verbatim;
* ``dry_run=True`` runs everything in a transaction that is rolled back, so the counts and violations are exact;
* each call writes one audit row (``sitepages.legacy_import``) with the counts and the SHA-256 of the source rows,
  and bumps the page cache. No outbox events: importing publishes nothing new (PLAN §7.2).

Order: users → media (both map into ``core_legacy_map``) → :func:`import_pages` → :func:`import_page_seo`,
:func:`import_text_slots`, :func:`import_image_slots` (:func:`import_all` runs them in order).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Callable, Iterable

from django.db import models, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from audit.services import record
from core.models import LegacyMap
from flarize.cache_utils import bump
from seo.models import SchemaType
from sitepages.models import Page, PageImageSlot, PageSeo, PageTextSlot
from sitepages.models.page import ROUTE_RE, SLOT_KEY_RE
from sitepages.services.content import text_value_problem
from sitepages.services.pages import CACHE_NAMESPACE
from sitepages.services.registry import slug_for_route

CMS = LegacyMap.SourceSystem.CMS
STATUS_MAP = {"draft": Page.Status.DRAFT, "published": Page.Status.PUBLISHED, "archived": Page.Status.ARCHIVED}
TEXT_KIND_MAP = {"short_text": "SHORT_TEXT", "long_text": "LONG_TEXT", "rich_text": "RICH_TEXT", "markdown": "MARKDOWN", "url": "URL", "email": "EMAIL", "phone": "PHONE"}
USER_TABLE = "accounts_admin_user"
MEDIA_TABLE = "media_asset"


class ImportRun:
    """Counts and violations of one import call."""

    def __init__(self, source_table: str):
        self.source_table = source_table
        self.created = self.updated = self.skipped = 0
        self.violations: list[dict] = []

    def violation(self, source_id, code: str, message: str) -> None:
        self.violations.append({"source_table": self.source_table, "source_id": str(source_id), "code": code, "message": message})

    def as_dict(self) -> dict:
        return {"created": self.created, "updated": self.updated, "skipped": self.skipped, "violations": self.violations}


# ── Shared helpers (also used by faqs.services.legacy_import) ───────────────────────────────────────────────────────
def mapped_id(source_table: str, source_id) -> int | None:
    if source_id in (None, ""):
        return None
    return LegacyMap.objects.filter(source_system=CMS, source_table=source_table, source_id=str(source_id)).values_list("target_id", flat=True).first()


def link(source_table: str, source_id, instance: models.Model) -> None:
    LegacyMap.objects.update_or_create(
        source_system=CMS, source_table=source_table, source_id=str(source_id), defaults={"target_table": instance._meta.db_table, "target_id": instance.pk, "imported_at": timezone.now()}
    )


def timestamp(value) -> dt.datetime | None:
    if value in (None, ""):
        return None
    parsed = value if isinstance(value, dt.datetime) else parse_datetime(str(value))
    if parsed is None:
        raise ValueError(f"not a timestamp: {value!r}")
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed, dt.UTC)


def resolve_user(run: ImportRun, row: dict, column: str) -> int | None:
    legacy = row.get(column)
    if legacy in (None, ""):
        return None
    target = mapped_id(USER_TABLE, legacy)
    if target is None:
        run.violation(row["id"], "unmapped_user", f"{column}={legacy} has no imported user; attribution left empty.")
    return target


def resolve_asset(run: ImportRun, row: dict, column: str) -> int | None:
    legacy = row.get(column)
    if legacy in (None, ""):
        return None
    target = mapped_id(MEDIA_TABLE, legacy)
    if target is None:
        run.violation(row["id"], "unmapped_media", f"{column}={legacy} has no imported media asset; left empty.")
    return target


def checksum(rows: Iterable[dict]) -> str:
    return hashlib.sha256(json.dumps(list(rows), sort_keys=True, default=str).encode()).hexdigest()


def upsert(run: ImportRun, model: type[models.Model], row: dict, *, target: models.Model | None, values: dict, created_at, updated_at, created_by_id=None, updated_by_id=None) -> models.Model:
    """Create or update one target row from ``values`` (column → value, FKs as ``<name>_id``), preserving timestamps."""
    if target is None:
        instance = model(**values, created_by_id=created_by_id, updated_by_id=updated_by_id)
        instance.save()
        model.all_objects.filter(pk=instance.pk).update(created_at=created_at or timezone.now(), updated_at=updated_at or created_at or timezone.now())
        run.created += 1
    else:
        instance = target
        diff = {name: value for name, value in values.items() if getattr(target, name) != value}
        if updated_by_id is not None and target.updated_by_id != updated_by_id:
            diff["updated_by_id"] = updated_by_id
        if diff:
            model.all_objects.filter(pk=target.pk).update(**diff, version=F("version") + 1, updated_at=updated_at or timezone.now())
            run.updated += 1
        else:
            run.skipped += 1
    link(run.source_table, row["id"], instance)
    return instance


def finish(run: ImportRun, rows: list[dict], *, user, object_type: str, namespace: str, action: str, dry_run: bool) -> dict:
    record(
        action,
        object_type=object_type,
        actor=user,
        after={"source_table": run.source_table, "rows": len(rows), "checksum": checksum(rows), **{**run.as_dict(), "violations": len(run.violations)}},
        note="CMS import",
    )
    if dry_run:
        transaction.set_rollback(True)
    else:
        bump(namespace)
    return run.as_dict()


def find_target(model: type[models.Model], source_table: str, row: dict, natural: Callable[[], models.Model | None] | None = None) -> models.Model | None:
    """The mapped target of ``row`` (soft-deleted rows included), else the natural-key match, else ``None``."""
    pk = mapped_id(source_table, row["id"])
    if pk is not None:
        found = model.all_objects.filter(pk=pk).first()
        if found is not None:
            return found
    return natural() if natural is not None else None


def run_import(
    source_table: str,
    rows: list[dict],
    import_row: Callable[[ImportRun, dict], None],
    *,
    user,
    dry_run: bool,
    object_type: str,
    action: str = "sitepages.legacy_import",
    namespace: str = CACHE_NAMESPACE,
) -> dict:
    """Import ``rows`` (in source id order) with ``import_row`` inside one transaction; audit, bump, or roll back."""
    run = ImportRun(source_table)
    ordered = sorted(rows, key=lambda row: int(row["id"]))
    with transaction.atomic():
        for row in ordered:
            import_row(run, row)
        return finish(run, ordered, user=user, object_type=object_type, namespace=namespace, action=action, dry_run=dry_run)


def _page_for(run: ImportRun, row: dict) -> Page | None:
    page_pk = mapped_id("sitepages_page", row.get("page_id"))
    page = Page.all_objects.filter(pk=page_pk).first() if page_pk else None
    if page is None:
        run.violation(row["id"], "unmapped_page", f"page_id={row.get('page_id')} was not imported; row skipped.")
    return page


# ── Tables ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def _import_page(run: ImportRun, row: dict) -> None:
    status = STATUS_MAP.get(str(row.get("status", "")).lower())
    route = row.get("route") or ""
    if status is None:
        run.violation(row["id"], "unknown_status", f"status={row.get('status')!r}; row skipped.")
        return
    if not re.match(ROUTE_RE, route) or len(route) > 255:
        run.violation(row["id"], "invalid_route", f"route={route!r} is not a site path; row skipped.")
        return
    target = find_target(Page, "sitepages_page", row, lambda: Page.objects.filter(route=route).first())
    values = {
        "route": route,
        "title": row["name"],
        "description": row.get("description") or "",
        "group": row.get("group") or "",
        "status": status,
        "is_protected": bool(row.get("is_protected", True)),
        "sort_order": int(row.get("sort_order") or 0),
    }
    if target is None:
        values["slug"] = slug_for_route(route)
        if Page.objects.filter(slug=values["slug"]).exists():
            run.violation(row["id"], "slug_taken", f"slug {values['slug']!r} (from route {route!r}) is used by another page; row skipped.")
            return
    upsert(
        run,
        Page,
        row,
        target=target,
        values=values,
        created_at=timestamp(row.get("created_at")),
        updated_at=timestamp(row.get("updated_at")),
        created_by_id=resolve_user(run, row, "created_by_id"),
        updated_by_id=resolve_user(run, row, "updated_by_id"),
    )


def _import_seo(run: ImportRun, row: dict) -> None:
    page = _page_for(run, row)
    if page is None:
        return
    schema_type = row.get("schema_type") or SchemaType.NONE
    if schema_type not in SchemaType.values:
        run.violation(row["id"], "unknown_schema_type", f"schema_type={schema_type!r} replaced by 'none'.")
        schema_type = SchemaType.NONE
    extra = row.get("schema_extra") or {}
    if isinstance(extra, str):
        extra = json.loads(extra)
    values = {
        "page_id": page.pk,
        "seo_title": row.get("seo_title") or "",
        "meta_description": row.get("meta_description") or "",
        "canonical_url": row.get("canonical_url") or "",
        "og_image_id": resolve_asset(run, row, "og_image_id"),
        "schema_type": schema_type,
        "schema_extra": extra if isinstance(extra, dict) else {},
        "noindex": bool(row.get("noindex")),
    }
    updated_at = timestamp(row.get("updated_at"))
    target = find_target(PageSeo, "sitepages_page_seo", row, lambda: PageSeo.all_objects.filter(page=page).first())
    upsert(run, PageSeo, row, target=target, values=values, created_at=updated_at, updated_at=updated_at, updated_by_id=resolve_user(run, row, "updated_by_id"))


def _import_text_slot(run: ImportRun, row: dict) -> None:
    page = _page_for(run, row)
    if page is None:
        return
    kind = TEXT_KIND_MAP.get(str(row.get("kind", "")).lower())
    if kind is None:
        run.violation(row["id"], "unknown_kind", f"kind={row.get('kind')!r}; row skipped.")
        return
    if not re.match(SLOT_KEY_RE, row.get("key") or ""):
        run.violation(row["id"], "invalid_key", f"key={row.get('key')!r}; row skipped.")
        return
    values = {
        "page_id": page.pk,
        "key": row["key"],
        "label": row.get("label") or row["key"],
        "kind": kind,
        "guidance": row.get("guidance") or "",
        "value": row.get("value") or "",
        "max_length": row.get("max_length") or None,
        "sort_order": int(row.get("order") or 0),
    }
    problem = text_value_problem(PageTextSlot(kind=kind, max_length=values["max_length"]), values["value"])
    if problem:
        run.violation(row["id"], "value_out_of_rules", f"{problem} Kept verbatim.")
    updated_at = timestamp(row.get("updated_at"))
    target = find_target(PageTextSlot, "sitepages_page_text_slot", row, lambda: PageTextSlot.objects.filter(page=page, key=row["key"]).first())
    upsert(run, PageTextSlot, row, target=target, values=values, created_at=updated_at, updated_at=updated_at, updated_by_id=resolve_user(run, row, "updated_by_id"))


def _import_image_slot(run: ImportRun, row: dict) -> None:
    page = _page_for(run, row)
    if page is None:
        return
    if not re.match(SLOT_KEY_RE, row.get("key") or ""):
        run.violation(row["id"], "invalid_key", f"key={row.get('key')!r}; row skipped.")
        return
    values = {
        "page_id": page.pk,
        "key": row["key"],
        "label": row.get("label") or row["key"],
        "guidance": row.get("guidance") or "",
        "asset_id": resolve_asset(run, row, "asset_id"),
        "alt": row.get("alt_text") or "",
        "sort_order": int(row.get("order") or 0),
    }
    updated_at = timestamp(row.get("updated_at"))
    target = find_target(PageImageSlot, "sitepages_page_image_slot", row, lambda: PageImageSlot.objects.filter(page=page, key=row["key"]).first())
    upsert(run, PageImageSlot, row, target=target, values=values, created_at=updated_at, updated_at=updated_at, updated_by_id=resolve_user(run, row, "updated_by_id"))


def import_pages(rows: list[dict], *, user=None, dry_run: bool = False) -> dict:
    return run_import("sitepages_page", rows, _import_page, user=user, dry_run=dry_run, object_type="sitepages.page")


def import_page_seo(rows: list[dict], *, user=None, dry_run: bool = False) -> dict:
    return run_import("sitepages_page_seo", rows, _import_seo, user=user, dry_run=dry_run, object_type="sitepages.pageseo")


def import_text_slots(rows: list[dict], *, user=None, dry_run: bool = False) -> dict:
    return run_import("sitepages_page_text_slot", rows, _import_text_slot, user=user, dry_run=dry_run, object_type="sitepages.pagetextslot")


def import_image_slots(rows: list[dict], *, user=None, dry_run: bool = False) -> dict:
    return run_import("sitepages_page_image_slot", rows, _import_image_slot, user=user, dry_run=dry_run, object_type="sitepages.pageimageslot")


IMPORTERS = (
    ("sitepages_page", import_pages),
    ("sitepages_page_seo", import_page_seo),
    ("sitepages_page_text_slot", import_text_slots),
    ("sitepages_page_image_slot", import_image_slots),
)


def import_all(tables: dict[str, list[dict]], *, user=None, dry_run: bool = False) -> dict[str, dict]:
    """Run every importer in dependency order over ``{source table: rows}`` (missing tables count as empty)."""
    with transaction.atomic():
        results = {table: importer(tables.get(table, []), user=user) for table, importer in IMPORTERS}
        if dry_run:
            transaction.set_rollback(True)
    return results
