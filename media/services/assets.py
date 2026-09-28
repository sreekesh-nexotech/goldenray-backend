"""Media asset writes: upload (library and programmatic), metadata edits, usage-guarded delete.

Upload pipeline (PLAN §1.4 "Files"):

1. the kind's policy decides the allowed visibilities, types and maximum size (``media.services.policies``);
2. the type is **sniffed from the bytes** (``media.services.sniffing``) — the client's filename and Content-Type are
   ignored, so a PDF renamed ``photo.png`` is still a PDF;
3. SHA-256, width/height and EXIF ``captured_at`` are recorded; a *public* image carrying GPS data is re-encoded
   without its metadata (orientation applied first) so a customer's roof photo never publishes their location;
4. the bytes are written to the visibility's storage **before** the database transaction opens (a slow Bunny PUT
   never holds a transaction or row locks); if the row cannot be written the stored object is removed again;
5. the row, its audit entry and the ``media`` cache bump commit together; the thumbnail task is enqueued on commit.

Deletion is a soft delete refused (409 ``media_in_use``) while a live row references the asset (``media.usage``);
the stored objects are removed after commit by ``media.tasks.delete_stored_files`` (retried on storage errors).
"""

from __future__ import annotations

import hashlib
import io
import logging
from functools import partial
from typing import BinaryIO

from django.db import transaction
from PIL import ImageOps

from audit.services import changes, record, snapshot
from core.errors import Conflict, DomainError, NotFound
from core.models import actor_or_none
from core.services import check_version, save_versioned, stamp_create
from flarize.cache_utils import bump
from media import folders, usage
from media.models import MediaAsset
from media.services.policies import THUMBNAIL_KINDS, policy_for
from media.services.sniffing import ImageTooLarge, Sniffed, UnsupportedFile, open_image, sniff
from media.services.storage import StorageError, new_key, storage_for, thumbnail_key

logger = logging.getLogger("flarize.media")

CACHE_NAMESPACE = "media"
SNAPSHOT_FIELDS = ("visibility", "kind", "original_filename", "mime_type", "size_bytes", "checksum_sha256", "folder", "alternative_text", "caption")
EDITABLE_FIELDS = ("alternative_text", "caption")


def asset_snapshot(asset: MediaAsset) -> dict:
    return snapshot(asset, SNAPSHOT_FIELDS)


def library_queryset():
    """Live assets of the media library: everything outside the reserved folders (``media.folders``)."""
    return MediaAsset.objects.filter(folders.library_q()).select_related("uploaded_by")


def get_asset(uid, *, library: bool = False) -> MediaAsset:
    queryset = library_queryset() if library else MediaAsset.objects.all()
    asset = queryset.filter(uid=uid).first()
    if asset is None:
        raise NotFound("not_found", "Media asset not found.")
    return asset


# ----------------------------------------------------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------------------------------------------------
def _validate_kind_and_visibility(kind: str, visibility: str):
    if kind not in MediaAsset.Kind.values:
        raise DomainError("validation_error", "Unknown media kind.", errors={"kind": [f"Use one of {', '.join(MediaAsset.Kind.values)}."]})
    if visibility not in MediaAsset.Visibility.values:
        raise DomainError("validation_error", "Unknown visibility.", errors={"visibility": ["Use PUBLIC or PRIVATE."]})
    policy = policy_for(kind)
    if visibility not in policy.visibilities:
        raise DomainError("visibility_not_allowed", f"{kind} files must be private.", errors={"visibility": [f"{kind} files are personal data and must be PRIVATE."]})
    return policy


def _validate_folder(folder: str, *, allow_reserved: bool) -> str:
    try:
        folder = folders.validate(folder)
    except ValueError as exc:
        raise DomainError("validation_error", "Invalid folder.", errors={"folder": [str(exc)]}) from None
    if folder and folders.is_reserved(folder) and not allow_reserved:
        raise DomainError("folder_reserved", "This folder is managed by another part of the system.", errors={"folder": ["Reserved folder."]})
    return folder


def _read_all(fileobj: BinaryIO) -> bytes:
    fileobj.seek(0)
    data = fileobj.read()
    fileobj.seek(0)
    return data


def _size_of(fileobj: BinaryIO) -> int:
    size = getattr(fileobj, "size", None)
    if size is None:
        fileobj.seek(0, 2)
        size = fileobj.tell()
        fileobj.seek(0)
    return int(size)


def _sniff(fileobj: BinaryIO, kind: str, allowed) -> Sniffed:
    try:
        sniffed = sniff(fileobj)
    except ImageTooLarge:
        raise DomainError("image_too_large", "The image has too many pixels.", errors={"file": ["The image resolution is too large."]}) from None
    except UnsupportedFile as exc:
        raise DomainError("unsupported_file_type", "This file type is not accepted.", errors={"file": [str(exc)]}) from None
    if sniffed.mime_type not in allowed:
        accepted = ", ".join(sorted(allowed))
        raise DomainError("unsupported_file_type", "This file type is not accepted.", errors={"file": [f"{kind} accepts {accepted}; the content is {sniffed.mime_type}."]})
    return sniffed


def strip_location(data: bytes, sniffed: Sniffed) -> bytes:
    """Re-encode an image without its metadata (orientation applied first, colour profile kept)."""
    image = ImageOps.exif_transpose(open_image(io.BytesIO(data)))
    options = {"icc_profile": image.info.get("icc_profile")} if image.info.get("icc_profile") else {}
    output = io.BytesIO()
    fmt = sniffed.image_format
    if fmt == "JPEG":
        image.convert("RGB").save(output, "JPEG", quality=92, optimize=True, exif=b"", **options)
    elif fmt == "PNG":
        image.save(output, "PNG", optimize=True, exif=b"", **options)
    elif fmt == "WEBP":
        image.save(output, "WEBP", quality=92, exif=b"", **options)
    else:
        image.save(output, fmt or "HEIF", quality=92)
    return output.getvalue()


# ----------------------------------------------------------------------------------------------------------------
# Writes
# ----------------------------------------------------------------------------------------------------------------
def upload(
    *,
    user,
    file: BinaryIO,
    visibility: str,
    kind: str,
    folder: str = "",
    alternative_text: str = "",
    caption: str = "",
    original_filename: str | None = None,
    allow_reserved: bool = False,
) -> MediaAsset:
    """Validate, sniff, store and record one file. ``allow_reserved`` is for owning contexts (never the library view)."""
    policy = _validate_kind_and_visibility(kind, visibility)
    folder = _validate_folder(folder, allow_reserved=allow_reserved)
    size = _size_of(file)
    if size == 0:
        raise DomainError("empty_file", "The file is empty.", errors={"file": ["The file is empty."]})
    if size > policy.max_bytes:
        raise DomainError("file_too_large", "The file is too large.", status=413, errors={"file": [f"{kind} files may be at most {policy.max_megabytes} MB."]})
    sniffed = _sniff(file, kind, policy.mime_types)
    data = _read_all(file)
    if visibility == MediaAsset.Visibility.PUBLIC and sniffed.has_location:
        data = strip_location(data, sniffed)
    name = (original_filename if original_filename is not None else getattr(file, "name", "")) or f"upload{sniffed.extension}"
    return _store(
        user=user,
        data=data,
        sniffed=sniffed,
        visibility=visibility,
        kind=kind,
        folder=folder,
        original_filename=name.replace("\\", "/").rsplit("/", 1)[-1][:255],
        alternative_text=alternative_text or "",
        caption=caption or "",
    )


def store_bytes(*, user, data: bytes, filename: str, kind: str, visibility: str, folder: str, allow_reserved: bool = True) -> MediaAsset:
    """Store a generated file (e.g. a rendered PDF) through the same validation as an upload."""
    buffer = io.BytesIO(data)
    buffer.size = len(data)
    return upload(user=user, file=buffer, visibility=visibility, kind=kind, folder=folder, original_filename=filename, allow_reserved=allow_reserved)


def _store(*, user, data: bytes, sniffed: Sniffed, visibility: str, kind: str, folder: str, original_filename: str, alternative_text: str, caption: str) -> MediaAsset:
    key = new_key(folder, sniffed.extension)
    try:
        storage = storage_for(visibility)
        storage.save(key, data)
    except StorageError:
        logger.exception("media storage write failed", extra={"visibility": visibility})
        raise DomainError("storage_unavailable", "File storage is temporarily unavailable. Try again shortly.", status=503) from None
    try:
        asset = _create_row(
            user=user,
            key=key,
            cdn_url=(storage.url(key) or "") if visibility == MediaAsset.Visibility.PUBLIC else "",
            data=data,
            sniffed=sniffed,
            visibility=visibility,
            kind=kind,
            folder=folder,
            original_filename=original_filename,
            alternative_text=alternative_text,
            caption=caption,
        )
    except BaseException:
        _discard(storage, key)
        raise
    return asset


@transaction.atomic
def _create_row(*, user, key, cdn_url, data, sniffed, visibility, kind, folder, original_filename, alternative_text, caption) -> MediaAsset:
    asset = MediaAsset(
        visibility=visibility,
        kind=kind,
        file=key,
        cdn_url=cdn_url,
        original_filename=original_filename,
        mime_type=sniffed.mime_type,
        size_bytes=len(data),
        width=sniffed.width,
        height=sniffed.height,
        captured_at=sniffed.captured_at,
        checksum_sha256=hashlib.sha256(data).hexdigest(),
        alternative_text=alternative_text[:255],
        caption=caption,
        folder=folder,
        uploaded_by=actor_or_none(user),
    )
    stamp_create(asset, user)
    asset.save()
    record("media.asset_uploaded", obj=asset, actor=user, after=asset_snapshot(asset))
    bump(CACHE_NAMESPACE)
    if kind in THUMBNAIL_KINDS:
        transaction.on_commit(partial(_enqueue_thumbnail, str(asset.uid)), robust=True)
    return asset


def _discard(storage, key: str) -> None:
    try:
        storage.delete(key)
    except StorageError:
        logger.warning("could not remove an orphaned upload", extra={"storage": storage.name}, exc_info=True)


def _enqueue_thumbnail(asset_uid: str) -> None:
    from media.tasks import generate_thumbnail

    generate_thumbnail.delay(asset_uid)


def _enqueue_file_removal(visibility: str, keys: list[str]) -> None:
    from media.tasks import delete_stored_files

    delete_stored_files.delay(visibility, keys)


@transaction.atomic
def update_asset(instance: MediaAsset, *, user, data, expected_version=None) -> MediaAsset:
    asset = MediaAsset.objects.select_for_update().get(pk=instance.pk)
    check_version(asset, expected_version)
    before = asset_snapshot(asset)
    fields = []
    for name in EDITABLE_FIELDS:
        if name in data and (data[name] or "") != getattr(asset, name):
            setattr(asset, name, data[name] or "")
            fields.append(name)
    if not fields:
        return asset
    save_versioned(asset, user=user, fields=fields)
    changed_before, changed_after = changes(before, asset_snapshot(asset))
    record("media.asset_updated", obj=asset, actor=user, before=changed_before, after=changed_after)
    bump(CACHE_NAMESPACE)
    return asset


def blocking_references(asset: MediaAsset) -> list[str]:
    return [f"{reference.label}: {reference.live}" for reference in usage.references(asset) if reference.live]


@transaction.atomic
def delete_asset(instance: MediaAsset, *, user, expected_version=None) -> None:
    asset = MediaAsset.objects.select_for_update().get(pk=instance.pk)
    check_version(asset, expected_version)
    blocking = blocking_references(asset)
    if blocking:
        raise Conflict("media_in_use", "The file is still used; remove it from those records first.", errors={"references": blocking})
    asset.soft_delete(user)
    record("media.asset_deleted", obj=asset, actor=user, before=asset_snapshot(asset))
    bump(CACHE_NAMESPACE)
    keys = [key for key in (asset.file, asset.thumbnail_key) if key]
    transaction.on_commit(partial(_enqueue_file_removal, asset.visibility, keys), robust=True)


def set_thumbnail(asset: MediaAsset, key: str) -> bool:
    """Record a generated thumbnail (derived data: no version bump, no audit row). False if already set/deleted."""
    updated = MediaAsset.objects.filter(pk=asset.pk, thumbnail_key="").update(thumbnail_key=key)
    if updated:
        asset.thumbnail_key = key
        bump(CACHE_NAMESPACE)
    return bool(updated)


def thumbnail_key_for(asset: MediaAsset) -> str:
    return thumbnail_key(asset.file)
