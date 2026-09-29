"""Installation-location rectangles (PLAN §2.7 ``site_inspections_annotation``).

``geometry`` is ``{x, y, w, h}`` in 0–1 **image** space (the client converts from its letterboxed drawing
container); ``w`` and ``h`` are at least 0.03 (the V2 minimum) and the rectangle stays inside the image. Each save
of a type makes a new ``number`` current and keeps the previous one as history (V2 deleted and re-inserted).

The current rectangles are the only source of the inspection's layout columns (``panel_photo``,
``panel_width_m``/``height_m``/``area_m2``, and the equipment ones) — written here in the same transaction, so the two
can never diverge (spec §J 30). ``area_m2`` is ``width_m × height_m``.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction

from core.errors import DomainError
from core.services import stamp_create
from site_inspections.models import Annotation, Inspection
from site_inspections.models.choices import AnnotationType, GeometrySpace
from site_inspections.services import common, photos

MIN_SIDE = Decimal("0.03")
LAYOUT = {
    AnnotationType.PANEL_AREA: ("panel_photo", "panel_width_m", "panel_height_m", "panel_area_m2"),
    AnnotationType.EQUIPMENT_AREA: ("equipment_photo", "equipment_width_m", "equipment_height_m", "equipment_area_m2"),
}


def _number(value, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise DomainError("invalid_geometry", "The rectangle is not valid.", errors={f"geometry.{name}": ["Must be a number."]}) from None
    if not number.is_finite():
        raise DomainError("invalid_geometry", "The rectangle is not valid.", errors={f"geometry.{name}": ["Must be a finite number."]})
    return number


def validate_geometry(geometry) -> dict:
    if not isinstance(geometry, dict) or set(geometry) != {"x", "y", "w", "h"}:
        raise DomainError("invalid_geometry", "The rectangle must be {x, y, w, h} in 0–1 image space.", errors={"geometry": ["Use exactly the keys x, y, w, h."]})
    x, y, w, h = (_number(geometry[key], key) for key in ("x", "y", "w", "h"))
    errors = {}
    if not (0 <= x <= 1) or not (0 <= y <= 1):
        errors["geometry"] = ["x and y must be between 0 and 1."]
    elif w < MIN_SIDE or h < MIN_SIDE:
        errors["geometry"] = [f"w and h must be at least {MIN_SIDE}."]
    elif x + w > 1 or y + h > 1:
        errors["geometry"] = ["The rectangle must stay inside the image."]
    if errors:
        raise DomainError("invalid_geometry", "The rectangle is not valid.", errors=errors)
    return {"x": float(x), "y": float(y), "w": float(w), "h": float(h)}


def area(width_m, height_m) -> Decimal | None:
    if width_m is None or height_m is None:
        return None
    return (Decimal(width_m) * Decimal(height_m)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def annotations_queryset(inspection: Inspection, *, current_only: bool = False):
    queryset = Annotation.objects.filter(inspection=inspection).select_related("photo")
    if current_only:
        queryset = queryset.filter(is_current=True)
    return queryset.order_by("annotation_type", "-number")


@transaction.atomic
def save_annotation(instance: Inspection, *, user, annotation_type: str, photo_uid, geometry: dict, width_m=None, height_m=None, expected_version=None) -> Annotation:
    inspection = common.lock(instance, expected_version)
    common.begin_work(inspection, user)
    photo = photos.get_photo(inspection, photo_uid)
    clean = validate_geometry(geometry)
    previous = Annotation.objects.select_for_update().filter(inspection=inspection, annotation_type=annotation_type, is_current=True).first()
    number = common.next_number(Annotation.all_objects.filter(inspection=inspection, annotation_type=annotation_type))
    if previous is not None:
        previous.versioned_update(user, is_current=False)
    annotation = Annotation(
        inspection=inspection,
        photo=photo,
        annotation_type=annotation_type,
        geometry=clean,
        geometry_space=GeometrySpace.IMAGE,
        width_m=width_m,
        height_m=height_m,
        area_m2=area(width_m, height_m),
        number=number,
        is_current=True,
    )
    stamp_create(annotation, user)
    annotation.save()
    photo_field, width_field, height_field, area_field = LAYOUT[AnnotationType(annotation_type)]
    inspection.versioned_update(user, **{photo_field: photo, width_field: width_m, height_field: height_m, area_field: annotation.area_m2})
    common.audit(
        "annotation_saved",
        inspection,
        user,
        before={"number": previous.number, "photo": str(previous.photo.uid), "geometry": previous.geometry} if previous else None,
        after={"annotation_type": annotation_type, "number": number, "photo": str(photo.uid), "geometry": clean, "width_m": width_m, "height_m": height_m},
    )
    common.changed(inspection)
    return annotation
