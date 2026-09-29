"""Idempotent importers for the legacy SEO data (PLAN §7.3 ``goldenray_metadata``; §7.2 row 11 SEO defaults).

Functions take plain row dicts and return ``{"created", "updated", "skipped", "violations"}``; ``core_legacy_map``
makes them idempotent (re-running updates, never duplicates) and each call writes one ``seo.legacy_import`` audit row
with the counts and the source checksum.

* :func:`import_page_metadata` — main backend ``goldenray_metadata`` (``page``, ``title``, ``description``,
  ``keywords`` jsonb list, ``imageUrl``, ``ogtype``) → ``seo_page_metadata`` (``og_image_url``, ``og_type``). Page keys
  are normalised (``/About/`` → ``about``); a key that is invalid or clashes with an unrelated row is refused.
* :func:`import_site_seo_defaults` — the CMS ``siteconfig_settings`` SEO defaults (``default_meta_description``,
  ``default_og_image_id``). Their typed home is the company profile (F3, DV-11), so they are written through
  ``company.services.profile.update_profile`` (audited there); the media reference resolves through ``media_map`` or
  the media import's ``core_legacy_map`` rows. Nothing else in ``siteconfig_settings`` is SEO data.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

from django.db import transaction
from django.db.models import F

from audit.services import record
from core.models import LegacyMap
from flarize.cache_utils import bump
from media.models import MediaAsset
from seo.models import PageMetadata
from seo.models.metadata import PAGE_KEY_REGEX
from seo.services.metadata import NAMESPACE, normalise_page

_PAGE_RE = re.compile(PAGE_KEY_REGEX)
_OG_TYPE_RE = re.compile(r"^[a-z][a-z0-9._:-]*$")


def _report() -> dict:
    return {"created": 0, "updated": 0, "skipped": 0, "violations": []}


def _violation(report: dict, table: str, source_id, code: str, message: str) -> None:
    report["violations"].append({"table": table, "source_id": str(source_id), "code": code, "message": message})


def _checksum(rows) -> str:
    return hashlib.sha256(json.dumps(list(rows), sort_keys=True, default=str).encode()).hexdigest()


def _keywords(value, report: dict, source_id) -> list[str]:
    if isinstance(value, str):
        value = [part for part in value.split(",")]
    if not isinstance(value, list):
        _violation(report, "goldenray_metadata", source_id, "keywords_invalid", "keywords were not a list; dropped.")
        return []
    cleaned = [str(keyword).strip()[:100] for keyword in value if str(keyword).strip()]
    return list(dict.fromkeys(cleaned))


@transaction.atomic
def import_page_metadata(rows, *, user=None) -> dict:
    source, table = LegacyMap.SourceSystem.BACKEND, "goldenray_metadata"
    report, rows = _report(), list(rows)
    for row in rows:
        source_id = row["id"]
        page = normalise_page(row.get("page") or "")
        if len(page) > 200 or not _PAGE_RE.match(page):
            _violation(report, table, source_id, "page_invalid", f"page key '{row.get('page')}' is not a route key; row refused.")
            report["skipped"] += 1
            continue
        og_type = (row.get("ogtype") or "website").strip().lower()
        if not _OG_TYPE_RE.match(og_type):
            _violation(report, table, source_id, "og_type_invalid", f"ogtype '{row.get('ogtype')}' is not an Open Graph type; 'website' was used.")
            og_type = "website"
        values = {
            "page": page,
            "title": (row.get("title") or "")[:255],
            "description": row.get("description") or "",
            "keywords": _keywords(row.get("keywords"), report, source_id),
            "og_type": og_type[:32],
            "og_image_url": (row.get("imageUrl") or "")[:500],
        }
        mapped_id = LegacyMap.objects.filter(source_system=source, source_table=table, source_id=str(source_id)).values_list("target_id", flat=True).first()
        existing = PageMetadata.all_objects.filter(pk=mapped_id).first() if mapped_id else None
        clash = PageMetadata.objects.filter(page=page).exclude(pk=getattr(existing, "pk", None)).exists()
        if clash:
            _violation(report, table, source_id, "page_clash", f"page '{page}' already has metadata from another source; row refused.")
            report["skipped"] += 1
            continue
        if existing is None:
            existing = PageMetadata(**values)
            existing.save()
            LegacyMap.objects.update_or_create(source_system=source, source_table=table, source_id=str(source_id), defaults={"target_table": PageMetadata._meta.db_table, "target_id": existing.pk})
            report["created"] += 1
            continue
        changed = {name: value for name, value in values.items() if getattr(existing, name) != value}
        if changed:
            PageMetadata.all_objects.filter(pk=existing.pk).update(**changed, version=F("version") + 1)
            report["updated"] += 1
        else:
            report["skipped"] += 1
    record(
        "seo.legacy_import",
        object_type="seo.legacyimport",
        actor=user,
        after={"table": table, **{key: report[key] for key in ("created", "updated", "skipped")}, "violations": len(report["violations"]), "checksum": _checksum(rows)},
    )
    bump(NAMESPACE)
    return report


def _media(media_map: Mapping | None, source_id):
    if source_id is None:
        return None
    if media_map is not None:
        return media_map.get(source_id, media_map.get(str(source_id)))
    target = LegacyMap.objects.filter(source_system=LegacyMap.SourceSystem.CMS, source_table="media_asset", source_id=str(source_id)).values_list("target_id", flat=True).first()
    return MediaAsset.all_objects.filter(pk=target).first() if target else None


@transaction.atomic
def import_site_seo_defaults(row: Mapping, *, user=None, media_map: Mapping | None = None) -> dict:
    """``siteconfig_settings`` SEO defaults → company profile (``default_meta_description``, ``default_og_image``)."""
    from company.services.profile import current_profile, update_profile

    table = "siteconfig_settings"
    report = _report()
    data = {}
    description = (row.get("default_meta_description") or "").strip()
    if description:
        data["default_meta_description"] = description
    image_id = row.get("default_og_image_id")
    if image_id is not None:
        asset = _media(media_map, image_id)
        if asset is None or asset.deleted_at is not None or not asset.is_public:
            _violation(report, table, row.get("id", 1), "media_unmapped", f"default_og_image {image_id} is not an imported public image; not set.")
        else:
            data["default_og_image"] = asset
    before = current_profile()
    changed = {name: value for name, value in data.items() if getattr(before, name) != value}
    if changed:
        was_saved = before.pk is not None
        profile = update_profile(user=user, data=changed)
        report["updated" if was_saved else "created"] += 1
    else:
        profile = before
        report["skipped"] += 1
    if profile.pk is not None:
        LegacyMap.objects.update_or_create(
            source_system=LegacyMap.SourceSystem.CMS, source_table=table, source_id=str(row.get("id", 1)), defaults={"target_table": profile._meta.db_table, "target_id": profile.pk}
        )
    record(
        "seo.legacy_import",
        object_type="seo.legacyimport",
        actor=user,
        after={"table": table, **{key: report[key] for key in ("created", "updated", "skipped")}, "violations": len(report["violations"]), "checksum": _checksum([row])},
    )
    return report
