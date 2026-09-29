"""Legacy import of the CMS media library (PLAN §7.2 row 3: CMS ``media_asset`` → ``media_asset``, PUBLIC) and of the
Flarize page-designer assets (PLAN §7.4, :func:`import_flarize_cms_assets`, PRIVATE, for reference).

:func:`import_cms_assets` takes plain row dicts (``SELECT *`` of the CMS table: ``file``, ``storage_path``,
``cdn_url``, ``mime``, ``size``, ``width``, ``height``, ``alternative_text``, ``caption``, ``collection_id``,
timestamps) and returns ``{"created", "updated", "skipped", "violations"}`` (``violations``: ``{"source_table",
"source_id", "code", "message"}``):

* **idempotent** through ``core_legacy_map`` (``CMS``/``media_asset``/<id>) — the map every content importer resolves
  ``media_asset_id``/``cover_image_id``/``og_image_id`` through; a re-run updates changed metadata and never
  duplicates; an asset deleted in the platform since stays deleted (``skipped``);
* **Bunny URLs are kept**: the storage key is the CMS ``storage_path`` (else ``file``) and ``cdn_url`` is copied;
* an asset that only exists under the CMS ``/uploads`` volume (no ``cdn_url``) is **re-uploaded** from the bytes
  ``read_file(path)`` returns into the public storage and gets that storage's URL; without the bytes it cannot be
  served and is refused (listed ``file_unavailable``);
* the SHA-256 is computed whenever the bytes are available (``read_file``); otherwise it stays empty and is listed
  (``checksum_unknown``) — ``verify_migration`` HEAD-checks every ``cdn_url`` instead;
* ``folder`` = the old collection (``collections``: CMS collection id → ``api_uid``), else ``cms``; ``kind`` from the
  MIME type (``application/pdf`` → DOCUMENT, anything else IMAGE); source timestamps are preserved;
* ``dry_run=True`` rolls back and writes no file (the re-upload is only reported); one audit row per call
  (``media.legacy_import``) with the counts and the SHA-256 of the source rows; the ``media`` cache is bumped.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from pathlib import PurePosixPath

from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from audit.services import record
from core.errors import DomainError
from core.models import LegacyMap
from flarize.cache_utils import bump
from media import folders
from media.models import MediaAsset
from media.services.storage import StorageError, storage_for

CMS = LegacyMap.SourceSystem.CMS
TABLE = "media_asset"
CACHE_NAMESPACE = "media"
DEFAULT_FOLDER = "cms"


class Report:
    def __init__(self):
        self.created = self.updated = self.skipped = 0
        self.violations: list[dict] = []

    def violation(self, source_id, code: str, message: str) -> None:
        self.violations.append({"source_table": TABLE, "source_id": str(source_id), "code": code, "message": message})

    def as_dict(self) -> dict:
        return {"created": self.created, "updated": self.updated, "skipped": self.skipped, "violations": self.violations}


def checksum(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, cls=DjangoJSONEncoder, sort_keys=True, default=str).encode()).hexdigest()


def _dt(value):
    if value in (None, ""):
        return None
    return parse_datetime(value) if isinstance(value, str) else value


def storage_key(row: dict) -> str:
    """The CMS object key (``storage_path``, else ``file``) without a leading ``/`` or ``..`` segments."""
    raw = (row.get("storage_path") or row.get("file") or "").strip().lstrip("/")
    parts = [part for part in PurePosixPath(raw).parts if part not in ("", ".", "..")]
    return "/".join(parts)


def _folder(row: dict, collections: Mapping | None, report: Report) -> str:
    collection = (collections or {}).get(row.get("collection_id"), (collections or {}).get(str(row.get("collection_id")))) if row.get("collection_id") is not None else None
    try:
        return folders.validate(collection or DEFAULT_FOLDER) or DEFAULT_FOLDER
    except ValueError:
        report.violation(row["id"], "folder_invalid", f"collection {collection!r} is not a folder name; '{DEFAULT_FOLDER}' was used.")
        return DEFAULT_FOLDER


def _int(value):
    return None if value in (None, "") else int(value)


@transaction.atomic
def import_cms_assets(rows: Iterable[dict], *, collections: Mapping | None = None, read_file: Callable[[str], bytes | None] | None = None, user=None, dry_run: bool = False) -> dict:
    rows = list(rows)
    report = Report()
    for row in rows:
        source_id = row["id"]
        key = storage_key(row)
        if not key:
            report.violation(source_id, "file_missing", "row without a file path; skipped.")
            report.skipped += 1
            continue
        target_id = LegacyMap.objects.filter(source_system=CMS, source_table=TABLE, source_id=str(source_id)).values_list("target_id", flat=True).first()
        existing = MediaAsset.all_objects.filter(pk=target_id).first() if target_id else None
        if existing is not None and existing.deleted_at is not None:
            report.skipped += 1
            continue
        data = read_file(key) if read_file is not None else None
        digest = hashlib.sha256(data).hexdigest() if data is not None else ""
        cdn_url = (row.get("cdn_url") or "").strip()
        if not cdn_url and existing is not None and existing.cdn_url:
            cdn_url = existing.cdn_url  # re-uploaded by an earlier run
        elif not cdn_url:
            if data is None:
                report.violation(source_id, "file_unavailable", f"no CDN URL and {key!r} is not in the legacy uploads volume; skipped.")
                report.skipped += 1
                continue
            if MediaAsset.all_objects.filter(visibility=MediaAsset.Visibility.PUBLIC, file=key).exists():
                # Checked before writing: the storage key is unique, and saving first would overwrite that asset's file.
                report.violation(source_id, "storage_key_taken", f"another asset already uses the storage key {key!r}; skipped.")
                report.skipped += 1
                continue
            if dry_run:
                report.violation(source_id, "reupload_pending", f"{key!r} would be uploaded to the public storage.")
            else:
                try:
                    storage = storage_for(MediaAsset.Visibility.PUBLIC)
                    storage.save(key, data)
                    cdn_url = storage.url(key) or ""
                except StorageError as exc:
                    report.violation(source_id, "reupload_failed", f"{key!r} could not be stored ({exc}); skipped.")
                    report.skipped += 1
                    continue
        if data is None:
            report.violation(source_id, "checksum_unknown", f"{key!r} is not in the legacy uploads volume; checksum not computed.")
        elif row.get("size") not in (None, "") and int(row["size"]) != len(data):
            report.violation(source_id, "size_mismatch", f"the CMS recorded {row['size']} bytes, the file has {len(data)}.")
        mime = (row.get("mime") or row.get("mime_type") or "").strip().lower() or "application/octet-stream"
        values = {
            "visibility": MediaAsset.Visibility.PUBLIC,
            "kind": MediaAsset.Kind.DOCUMENT if mime == "application/pdf" else MediaAsset.Kind.IMAGE,
            "file": key[:512],
            "cdn_url": cdn_url[:512],
            "original_filename": PurePosixPath(row.get("file") or key).name[:255],
            "mime_type": mime[:120],
            "size_bytes": len(data) if data is not None else int(row.get("size") or 0),
            "width": _int(row.get("width")),
            "height": _int(row.get("height")),
            "alternative_text": (row.get("alternative_text") or "")[:255],
            "caption": row.get("caption") or "",
            "folder": _folder(row, collections, report),
        }
        if digest or existing is None:
            values["checksum_sha256"] = digest
        created_at = _dt(row.get("created_at")) or timezone.now()
        updated_at = _dt(row.get("updated_at")) or created_at
        if existing is None:
            asset = MediaAsset(**values)
            try:
                with transaction.atomic():
                    asset.save()
            except IntegrityError:
                report.violation(source_id, "storage_key_taken", f"another asset already uses the storage key {key!r}; skipped.")
                report.skipped += 1
                continue
            MediaAsset.all_objects.filter(pk=asset.pk).update(created_at=created_at, updated_at=updated_at)
            LegacyMap.objects.create(source_system=CMS, source_table=TABLE, source_id=str(source_id), target_table=MediaAsset._meta.db_table, target_id=asset.pk)
            report.created += 1
            continue
        changed = {name: value for name, value in values.items() if getattr(existing, name) != value}
        if changed:
            MediaAsset.all_objects.filter(pk=existing.pk).update(**changed, version=F("version") + 1, updated_at=updated_at)
            report.updated += 1
        else:
            report.skipped += 1
    record(
        "media.legacy_import",
        object_type="media.legacyimport",
        actor=user,
        actor_kind=None if user else "SYSTEM",
        after={"source_table": TABLE, "rows": len(rows), "checksum": checksum(rows), **{**report.as_dict(), "violations": len(report.violations)}},
    )
    if dry_run:
        transaction.set_rollback(True)
    else:
        bump(CACHE_NAMESPACE)
    return report.as_dict()


# ── Flarize cms-assets (PLAN §7.4 last row) ──────────────────────────────────────────────────────────────────────
FLARIZE = LegacyMap.SourceSystem.FLARIZE
FLARIZE_ASSET_TABLE = "cms-state.json:assets"
FLARIZE_FOLDER = "flarize-cms"


class _FlarizeReport(Report):
    def violation(self, source_id, code: str, message: str) -> None:
        self.violations.append({"source_table": FLARIZE_ASSET_TABLE, "source_id": str(source_id), "code": code, "message": message})


@transaction.atomic
def import_flarize_cms_assets(rows: Iterable[dict], *, read_file: Callable[[str], bytes | None] | None = None, user=None, dry_run: bool = False) -> dict:
    """Flarize page-designer assets (``cms-state.json`` ``assets`` records + the files of ``cms-assets/``) → PRIVATE
    ``media_asset`` rows in folder ``flarize-cms``, **for reference only** (PLAN §7.4: the page designer is superseded
    by quotation content versions; nothing links to them).

    Idempotent through ``core_legacy_map`` (``FLARIZE``/``cms-state.json:assets``/<assetId>): a mapped asset is never
    uploaded again. Records that are not ACTIVE (``not_active``), whose file is missing (``file_unavailable``), whose
    bytes do not match the recorded SHA-256 (``checksum_mismatch``, still copied) or that the upload pipeline refuses
    (``file_refused``) are listed. Every upload goes through ``media.services.assets.store_bytes`` (sniffed, size
    policy); ``dry_run=True`` counts the copies without storing a file (the runner's own dry run swaps the storage
    instead). One ``media.legacy_import`` audit row."""
    from media.services.assets import store_bytes

    rows = list(rows)
    report = _FlarizeReport()
    for row in rows:
        source_id = str(row.get("assetId") or "").strip()
        if not source_id:
            report.violation("", "incomplete_row", "an asset record without assetId; skipped.")
            report.skipped += 1
            continue
        target_id = LegacyMap.objects.filter(source_system=FLARIZE, source_table=FLARIZE_ASSET_TABLE, source_id=source_id).values_list("target_id", flat=True).first()
        if target_id is not None:
            report.skipped += 1
            continue
        if str(row.get("status") or "ACTIVE").upper() != "ACTIVE":
            report.violation(source_id, "not_active", f"asset status {row.get('status')!r}; not copied.")
            report.skipped += 1
            continue
        path = f"cms-assets/{PurePosixPath(str(row.get('storagePath') or '')).name}"
        data = read_file(path) if read_file is not None and row.get("storagePath") else None
        if data is None:
            report.violation(source_id, "file_unavailable", f"{path!r} is not in the Flarize data folder; not copied.")
            report.skipped += 1
            continue
        digest = hashlib.sha256(data).hexdigest()
        if row.get("checksum") and row["checksum"] != digest:
            report.violation(source_id, "checksum_mismatch", f"{path!r}: the record says {row['checksum']}, the file is {digest}; copied as it is.")
        if dry_run:
            report.created += 1  # counted, not stored: a dry run writes no file
            continue
        try:
            with transaction.atomic():
                asset = store_bytes(
                    user=user,
                    data=data,
                    filename=str(row.get("filename") or PurePosixPath(path).name)[:255],
                    kind=MediaAsset.Kind.IMAGE,
                    visibility=MediaAsset.Visibility.PRIVATE,
                    folder=FLARIZE_FOLDER,
                )
        except DomainError as exc:
            report.violation(source_id, "file_refused", f"{path!r} refused by the upload pipeline ({exc.code}); not copied.")
            report.skipped += 1
            continue
        uploaded = _dt(row.get("uploadedAt"))
        if uploaded is not None:
            MediaAsset.all_objects.filter(pk=asset.pk).update(created_at=uploaded, updated_at=uploaded)
        LegacyMap.objects.create(source_system=FLARIZE, source_table=FLARIZE_ASSET_TABLE, source_id=source_id[:128], target_table=MediaAsset._meta.db_table, target_id=asset.pk)
        report.created += 1
    record(
        "media.legacy_import",
        object_type="media.legacyimport",
        actor=user,
        actor_kind=None if user else "SYSTEM",
        after={"source_table": FLARIZE_ASSET_TABLE, "rows": len(rows), "checksum": checksum(rows), **{**report.as_dict(), "violations": len(report.violations)}},
    )
    if dry_run:
        transaction.set_rollback(True)
    else:
        bump(CACHE_NAMESPACE)
    return report.as_dict()
