"""Inspection photos: private media in the reserved ``site-inspections/<inspection uid>`` folder.

Uploads go through ``media.services.assets.upload`` (bytes sniffed, JPEG/PNG/WebP/HEIC ≤ 15 MB, EXIF
``captured_at``, thumbnail task) with ``visibility=PRIVATE``, so a customer's roof never reaches the CDN and the media
library never lists it. The file is stored before the inspection row is locked (media's rule: no storage I/O inside a
transaction); if the photo row cannot be written the asset is removed again.

Every reference to a photo (annotation, evidence, observation) is checked to belong to the same inspection
(:func:`get_photo`). A photo is soft-deleted, and refused (409 ``photo_in_use``) while it is a current rectangle's
photo, a layout reference photo or a live checklist's evidence.
"""

from __future__ import annotations

from django.db import transaction

from core.errors import Conflict, DomainError, NotFound
from core.services import check_version, stamp_create
from media.models import MediaAsset
from media.services import assets
from site_inspections.models import Annotation, EquipmentAssessment, Inspection, LocationApproval, Photo
from site_inspections.services import common

MEDIA_FOLDER = "site-inspections"


def folder_for(inspection: Inspection) -> str:
    return f"{MEDIA_FOLDER}/{inspection.uid}"


def photos_queryset(inspection: Inspection):
    return Photo.objects.filter(inspection=inspection).select_related("asset").order_by("created_at", "id")


def get_photo(inspection: Inspection, uid, *, field: str = "photo_uid") -> Photo:
    """A live photo of *this* inspection; another inspection's photo is reported as not found (400)."""
    photo = Photo.objects.filter(inspection=inspection, uid=uid).select_related("asset").first() if uid else None
    if photo is None:
        raise DomainError("photo_not_in_inspection", "The photo does not belong to this inspection.", errors={field: ["Not a photo of this inspection."]})
    return photo


def upload_photo(instance: Inspection, *, user, file, photo_type: str, stage: int | None = None, caption: str = "") -> Photo:
    common.ensure_engineer_writable(instance)
    asset = assets.upload(user=user, file=file, visibility=MediaAsset.Visibility.PRIVATE, kind=MediaAsset.Kind.PHOTO, folder=folder_for(instance), allow_reserved=True)
    try:
        return _create_photo(instance, user=user, asset=asset, photo_type=photo_type, stage=stage, caption=caption)
    except Exception:
        assets.delete_asset(asset, user=user)
        raise


@transaction.atomic
def _create_photo(instance: Inspection, *, user, asset: MediaAsset, photo_type: str, stage: int | None, caption: str) -> Photo:
    inspection = common.lock(instance)
    common.begin_work(inspection, user)
    photo = Photo(inspection=inspection, asset=asset, photo_type=photo_type, stage=stage, caption=caption or "", captured_at=asset.captured_at)
    stamp_create(photo, user)
    photo.save()
    common.audit("photo_added", inspection, user, after={"photo": str(photo.uid), "photo_type": photo_type, "stage": stage, "asset": str(asset.uid)})
    common.changed(inspection)
    return photo


def references(photo: Photo) -> list[str]:
    inspection = photo.inspection
    found = []
    if Annotation.objects.filter(photo=photo, is_current=True).exists():
        found.append("annotation")
    if photo.pk in (inspection.panel_photo_id, inspection.equipment_photo_id):
        found.append("layout")
    if EquipmentAssessment.objects.filter(evidence_photo=photo).exists():
        found.append("equipment_evidence")
    return found


def in_approval_snapshot(photo: Photo) -> list[int]:
    """Numbers of the location approvals whose frozen snapshot shows ``photo`` — the file the customer (or the paper
    fallback) approved is evidence and is never removed, even after the photo leaves the working set."""
    uid = str(photo.uid)
    found = []
    for number, snapshot in LocationApproval.objects.filter(inspection_id=photo.inspection_id).values_list("number", "location_snapshot"):
        entries = snapshot.values() if isinstance(snapshot, dict) else ()
        if any(isinstance(entry, dict) and str(entry.get("photo")) == uid for entry in entries):
            found.append(number)
    return sorted(found)


@transaction.atomic
def delete_photo(photo: Photo, *, user, expected_version=None) -> None:
    inspection = common.lock(photo.inspection)
    common.ensure_engineer_writable(inspection)
    photo = Photo.objects.select_for_update().select_related("asset", "inspection").get(pk=photo.pk)
    check_version(photo, expected_version)
    used = references(photo)
    if used:
        raise Conflict("photo_in_use", "The photo is referenced by the inspection; replace that reference first.", errors={"photo": used})
    photo.soft_delete(user)
    photo.observations.clear()
    evidence = in_approval_snapshot(photo)
    if not evidence:
        assets.delete_asset(photo.asset, user=user)
    common.audit("photo_deleted", inspection, user, before={"photo": str(photo.uid), "photo_type": photo.photo_type}, after={"file_kept_for_approvals": evidence} if evidence else None)
    common.changed(inspection)


def find(inspection: Inspection, uid) -> Photo:
    photo = photos_queryset(inspection).filter(uid=uid).first()
    if photo is None:
        raise NotFound("not_found", "Photo not found.")
    return photo
