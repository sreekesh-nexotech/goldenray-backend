"""Committed legacy CMS exports and golden payloads (``tests/fixtures/legacy_cms/<dataset>/``) and how to load them.

Datasets (captured by ``sitepages/tests/parity/capture_legacy.py``):

* ``uat`` — the shared, seeded UAT legacy database/server (27 pages, 100 FAQs), read only;
* ``enriched`` — a private copy of it enriched by ``parity/enrich_legacy_cms.py`` (SEO rows, slot values, media,
  categories, sections, drafts, archived rows, a draft page, an archived page), plus ``*_after_writes`` golden files
  captured after ``parity/apply_legacy_writes.py`` ran the Studio operations through the legacy code.

The media and user imports are other packages' work; :func:`seed_references` stands in for them by creating one
public media asset per legacy ``media_asset`` row (same CDN URL, alt text and size) and one user per legacy user id,
mapped in ``core_legacy_map`` exactly as those importers map them, plus the company name the WebPage schema reads.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.models import LegacyMap

ROOT = Path(__file__).resolve().parents[2]
DATASETS = ("uat", "enriched")
SITE_URL = "http://localhost:3000"  # the legacy CMS default FRONTEND_BASE_URL the golden files were captured with


def fixture_dir(app: str, dataset: str) -> Path:
    return ROOT / app / "tests" / "fixtures" / "legacy_cms" / dataset


def load(app: str, dataset: str, name: str):
    return json.loads((fixture_dir(app, dataset) / f"{name}.json").read_text())


def seed_references(dataset: str) -> None:
    from accounts.tests.factories import UserFactory
    from company.tests.factories import CompanyProfileFactory
    from media.tests.factories import MediaAssetFactory

    for row in load("sitepages", dataset, "media_asset"):
        asset = MediaAssetFactory(
            cdn_url=row["cdn_url"],
            alternative_text=row["alternative_text"],
            width=row["width"],
            height=row["height"],
            mime_type=row["mime"] or "image/webp",
            size_bytes=row["size"],
            original_filename=row["file"].rsplit("/", 1)[-1],
            folder="cms/general",
        )
        LegacyMap.objects.create(source_system="CMS", source_table="media_asset", source_id=str(row["id"]), target_table="media_asset", target_id=asset.pk)
    for row in load("sitepages", dataset, "accounts_admin_user"):
        user = UserFactory()
        LegacyMap.objects.create(source_system="CMS", source_table="accounts_admin_user", source_id=str(row["id"]), target_table="accounts_user", target_id=user.pk)
    settings_rows = load("sitepages", dataset, "siteconfig_settings")
    if settings_rows and settings_rows[0]["company_name"]:
        CompanyProfileFactory(trade_name=settings_rows[0]["company_name"], legal_name="")


def import_pages(dataset: str) -> dict:
    from sitepages.services import legacy_import

    tables = {table: load("sitepages", dataset, table) for table, _ in legacy_import.IMPORTERS}
    return legacy_import.import_all(tables)


def import_faqs(dataset: str) -> dict:
    from faqs.services import legacy_import

    return legacy_import.import_all({table: load("faqs", dataset, table) for table in ("faqs_category", "faqs_faq")})


def import_dataset(dataset: str, *, faqs: bool = True) -> None:
    seed_references(dataset)
    import_pages(dataset)
    if faqs:
        import_faqs(dataset)


def legacy_uid_map(source_table: str) -> dict[int, str]:
    """``{legacy id: uid}`` of the imported rows of ``source_table``."""
    from faqs.models import Faq
    from sitepages.models import Page

    model = {"faqs_faq": Faq, "sitepages_page": Page}[source_table]
    targets = dict(LegacyMap.objects.filter(source_system="CMS", source_table=source_table).values_list("target_id", "source_id"))
    return {int(targets[pk]): str(uid) for pk, uid in model.all_objects.filter(pk__in=targets).values_list("pk", "uid")}


def with_uids(body, faq_uids: dict[int, str]):
    """The legacy FAQ payload with each integer ``id`` replaced by the uid its row was imported as (PLAN §6.3)."""
    if not body:
        return body
    return {**body, "data": [{**row, "id": faq_uids[row["id"]]} for row in body["data"]]}
