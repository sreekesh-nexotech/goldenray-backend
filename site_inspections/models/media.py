"""``site_inspections_photo`` and ``site_inspections_annotation`` (PLAN §2.7).

Photos are private ``media_asset`` rows in the reserved ``site-inspections`` folder (never in the media library).
Annotations are rectangles ``{x, y, w, h}`` in **image-normalised** coordinates (0–1 of the photo, not of the V2
4:3 letterbox container). Saving a rectangle keeps history: a new ``number`` becomes current and the previous one is
kept with ``is_current=false``. A composite foreign key ``(photo_id, inspection_id) → photo(id, inspection_id)``
(migration 0002) makes it impossible to annotate another inspection's photo.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import BaseModel
from site_inspections.models.choices import AnnotationType, GeometrySpace, PhotoType, values


class Photo(BaseModel):
    # A photo belongs to its inspection: CASCADE (true child; inspections are only soft-deleted).
    inspection = models.ForeignKey("site_inspections.Inspection", on_delete=models.CASCADE, related_name="photos")
    # The stored file is a media master row: PROTECT (media deletion is usage-guarded anyway).
    asset = models.ForeignKey("media.MediaAsset", on_delete=models.PROTECT, related_name="+")
    photo_type = models.CharField(max_length=24, choices=PhotoType.choices, default=PhotoType.OTHER)
    stage = models.PositiveSmallIntegerField(null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)
    caption = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        db_table = "site_inspections_photo"
        ordering = ["created_at", "id"]
        constraints = [
            # Target of the annotation composite foreign key.
            models.UniqueConstraint(fields=["id", "inspection"], name="site_inspections_photo_id_inspection_uniq"),
            models.CheckConstraint(condition=Q(photo_type__in=values(PhotoType)), name="site_inspections_photo_type_valid"),
            models.CheckConstraint(condition=Q(stage__isnull=True) | Q(stage__gte=1, stage__lte=10), name="site_inspections_photo_stage_range"),
        ]
        indexes = [models.Index(fields=["inspection", "photo_type"], name="si_photo_insp_type")]


class Annotation(BaseModel):
    # A rectangle belongs to its inspection: CASCADE (true child).
    inspection = models.ForeignKey("site_inspections.Inspection", on_delete=models.CASCADE, related_name="annotations")
    # Drawn on one photo of the same inspection (composite FK in migration 0002): CASCADE (true child of the photo).
    photo = models.ForeignKey(Photo, on_delete=models.CASCADE, related_name="annotations")
    annotation_type = models.CharField(max_length=16, choices=AnnotationType.choices)
    geometry = models.JSONField(help_text="{x, y, w, h} in 0–1 image space (LEGACY_CONTAINER: the V2 4:3 container space).")
    geometry_space = models.CharField(max_length=16, choices=GeometrySpace.choices, default=GeometrySpace.IMAGE)
    width_m = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    height_m = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    area_m2 = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    number = models.PositiveSmallIntegerField()
    is_current = models.BooleanField(default=True)

    class Meta:
        db_table = "site_inspections_annotation"
        ordering = ["inspection_id", "annotation_type", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["inspection", "annotation_type"], condition=Q(is_current=True, deleted_at__isnull=True), name="site_inspections_annotation_one_current"),
            models.UniqueConstraint(fields=["inspection", "annotation_type", "number"], name="site_inspections_annotation_number_uniq"),
            models.CheckConstraint(condition=Q(annotation_type__in=values(AnnotationType)), name="site_inspections_annotation_type_valid"),
            models.CheckConstraint(condition=Q(geometry_space__in=values(GeometrySpace)), name="site_inspections_annotation_space_valid"),
            models.CheckConstraint(condition=Q(width_m__isnull=True) | Q(width_m__gt=0), name="site_inspections_annotation_width_positive"),
            models.CheckConstraint(condition=Q(height_m__isnull=True) | Q(height_m__gt=0), name="site_inspections_annotation_height_positive"),
        ]
        indexes = [models.Index(fields=["photo", "inspection"], name="si_annotation_photo_insp")]
