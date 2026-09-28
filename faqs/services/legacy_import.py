"""Import of the legacy CMS ``faqs_category`` and ``faqs_faq`` tables (PLAN §7.2 #9). Called by ``migrations_tools``.

Same contract as ``sitepages.services.legacy_import`` (plain row dicts in, ``{"created", "updated", "skipped",
"violations"}`` out, idempotent through ``core_legacy_map``, source timestamps preserved, ``dry_run``, one audit row
per call). Run after the pages: a FAQ's ``page_id`` resolves through the page map.

* categories match on the map, else on ``slug``;
* FAQs match on the map only (a question has no natural key); ``display_order`` → ``sort_order``, the status is
  upper-cased, ``published_at``/``archived_at`` and the SEO block are copied, ``og_image_id`` goes through the media
  map;
* a FAQ whose page was not imported is skipped (listed); an unmapped category or image is imported as empty (listed);
* a PUBLISHED FAQ that the new publish rules would refuse (no answer, archived page) is kept as it was and listed —
  the importer never changes what the website shows.
"""

from __future__ import annotations

import json

from django.db import transaction

from faqs.models import Faq, FaqCategory
from faqs.services.categories import CACHE_NAMESPACE
from faqs.services.faqs import publish_errors
from seo.models import SchemaType
from sitepages.models import Page
from sitepages.services.legacy_import import ImportRun, find_target, mapped_id, resolve_asset, resolve_user, run_import, timestamp, upsert

ACTION = "faqs.legacy_import"
STATUS_MAP = {"draft": Faq.Status.DRAFT, "published": Faq.Status.PUBLISHED, "archived": Faq.Status.ARCHIVED}


def _import_category(run: ImportRun, row: dict) -> None:
    slug = row.get("slug") or ""
    name = row.get("name") or ""
    if not slug or not name:
        run.violation(row["id"], "incomplete_category", "name and slug are required; row skipped.")
        return
    target = find_target(FaqCategory, "faqs_category", row, lambda: FaqCategory.objects.filter(slug=slug).first())
    others = FaqCategory.objects.exclude(pk=target.pk) if target is not None else FaqCategory.objects.all()
    if others.filter(name=name).exists() or others.filter(slug=slug).exists():
        run.violation(row["id"], "category_taken", f"name {name!r} or slug {slug!r} is used by another category; row skipped.")
        return
    values = {"name": name, "slug": slug, "description": row.get("description") or "", "is_active": bool(row.get("is_active", True)), "sort_order": int(row.get("sort_order") or 0)}
    upsert(run, FaqCategory, row, target=target, values=values, created_at=timestamp(row.get("created_at")), updated_at=timestamp(row.get("updated_at")))


def _import_faq(run: ImportRun, row: dict) -> None:
    status = STATUS_MAP.get(str(row.get("status", "")).lower())
    if status is None:
        run.violation(row["id"], "unknown_status", f"status={row.get('status')!r}; row skipped.")
        return
    page_pk = mapped_id("sitepages_page", row.get("page_id"))
    page = Page.all_objects.filter(pk=page_pk).first() if page_pk else None
    if page is None:
        run.violation(row["id"], "unmapped_page", f"page_id={row.get('page_id')} was not imported; row skipped.")
        return
    category_id = None
    if row.get("category_id") not in (None, ""):
        category_id = mapped_id("faqs_category", row["category_id"])
        if category_id is None:
            run.violation(row["id"], "unmapped_category", f"category_id={row['category_id']} was not imported; left empty.")
    schema_type = row.get("schema_type") or SchemaType.NONE
    if schema_type not in SchemaType.values:
        run.violation(row["id"], "unknown_schema_type", f"schema_type={schema_type!r} replaced by 'none'.")
        schema_type = SchemaType.NONE
    extra = row.get("schema_extra") or {}
    if isinstance(extra, str):
        extra = json.loads(extra)
    values = {
        "question": row.get("question") or "",
        "answer": row.get("answer") or "",
        "page_id": page.pk,
        "section": row.get("section") or "",
        "category_id": category_id,
        "sort_order": int(row.get("display_order") or 0),
        "status": status,
        "published_at": timestamp(row.get("published_at")),
        "archived_at": timestamp(row.get("archived_at")),
        "seo_title": row.get("seo_title") or "",
        "meta_description": row.get("meta_description") or "",
        "canonical_url": row.get("canonical_url") or "",
        "og_image_id": resolve_asset(run, row, "og_image_id"),
        "schema_type": schema_type,
        "schema_extra": extra if isinstance(extra, dict) else {},
        "noindex": bool(row.get("noindex")),
    }
    if status == Faq.Status.PUBLISHED:
        problems = publish_errors(Faq(question=values["question"], answer=values["answer"], page=page))
        if problems:
            run.violation(row["id"], "published_incomplete", f"published although {' '.join(problems)} Kept as published.")
    upsert(
        run,
        Faq,
        row,
        target=find_target(Faq, "faqs_faq", row),
        values=values,
        created_at=timestamp(row.get("created_at")),
        updated_at=timestamp(row.get("updated_at")),
        created_by_id=resolve_user(run, row, "created_by_id"),
        updated_by_id=resolve_user(run, row, "updated_by_id"),
    )


def import_categories(rows: list[dict], *, user=None, dry_run: bool = False) -> dict:
    return run_import("faqs_category", rows, _import_category, user=user, dry_run=dry_run, object_type="faqs.faqcategory", action=ACTION, namespace=CACHE_NAMESPACE)


def import_faqs(rows: list[dict], *, user=None, dry_run: bool = False) -> dict:
    return run_import("faqs_faq", rows, _import_faq, user=user, dry_run=dry_run, object_type="faqs.faq", action=ACTION, namespace=CACHE_NAMESPACE)


def import_all(tables: dict[str, list[dict]], *, user=None, dry_run: bool = False) -> dict[str, dict]:
    """Categories, then FAQs (pages must have been imported already)."""
    with transaction.atomic():
        results = {"faqs_category": import_categories(tables.get("faqs_category", []), user=user), "faqs_faq": import_faqs(tables.get("faqs_faq", []), user=user)}
        if dry_run:
            transaction.set_rollback(True)
    return results
