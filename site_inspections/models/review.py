"""Equipment assessments, engineering reviews and observations (PLAN §2.7).

* ``site_inspections_equipment_assessment`` — one checklist per equipment type; ``results`` holds every check of the
  pinned ``checks_version`` definition (``engines.inspection_checks``) and ``status`` is recomputed from them by the
  service over the full definition, never trusted from a client.
* ``site_inspections_engineering_review`` — requested manually or automatically (equipment FAIL/REQUIRES_REVIEW,
  structure decision, roof condition, generation impact); at most one PENDING per inspection.
* ``site_inspections_observation`` — free notes per stage with photos of the same inspection.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import BaseModel
from site_inspections.models.choices import AssessmentReview, AssessmentStatus, EquipmentType, ObservationCategory, ReviewDecision, ReviewTrigger, values


class EquipmentAssessment(BaseModel):
    # A checklist belongs to its inspection: CASCADE (true child).
    inspection = models.ForeignKey("site_inspections.Inspection", on_delete=models.CASCADE, related_name="equipment_assessments")
    equipment_type = models.CharField(max_length=20, choices=EquipmentType.choices)
    checks_version = models.CharField(max_length=8)
    results = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=AssessmentStatus.choices, default=AssessmentStatus.NOT_STARTED)
    issue = models.TextField(blank=True, default="")
    corrective_action = models.TextField(blank=True, default="")
    engineer_remarks = models.TextField(blank=True, default="")
    # Evidence photo of a failed check (same inspection, checked by the service): SET_NULL (photos are soft-deleted).
    evidence_photo = models.ForeignKey("site_inspections.Photo", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    review_status = models.CharField(max_length=12, choices=AssessmentReview.choices, default=AssessmentReview.NOT_REQUIRED)
    # Attribution: SET_NULL.
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.TextField(blank=True, default="")

    class Meta:
        db_table = "site_inspections_equipment_assessment"
        ordering = ["inspection_id", "equipment_type"]
        constraints = [
            models.UniqueConstraint(fields=["inspection", "equipment_type"], condition=Q(deleted_at__isnull=True), name="site_inspections_equipment_one_per_type"),
            models.CheckConstraint(condition=Q(equipment_type__in=values(EquipmentType)), name="site_inspections_equipment_type_valid"),
            models.CheckConstraint(condition=Q(status__in=values(AssessmentStatus)), name="site_inspections_equipment_status_valid"),
            models.CheckConstraint(condition=Q(review_status__in=values(AssessmentReview)), name="site_inspections_equipment_review_valid"),
            models.CheckConstraint(
                condition=~Q(review_status__in=["RESOLVED", "WAIVED"]) | (Q(resolved_at__isnull=False) & ~Q(resolution_note="")), name="site_inspections_equipment_resolution_noted"
            ),
        ]


class EngineeringReview(BaseModel):
    # A review belongs to its inspection: CASCADE (true child).
    inspection = models.ForeignKey("site_inspections.Inspection", on_delete=models.CASCADE, related_name="engineering_reviews")
    trigger = models.CharField(max_length=24, choices=ReviewTrigger.choices)
    # Attribution: SET_NULL (automatic reviews have no requester).
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reason = models.TextField(blank=True, default="")
    # Attribution: SET_NULL.
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decision = models.CharField(max_length=16, choices=ReviewDecision.choices, default=ReviewDecision.PENDING)
    notes = models.TextField(blank=True, default="")
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "site_inspections_engineering_review"
        ordering = ["inspection_id", "created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["inspection"], condition=Q(decision="PENDING", deleted_at__isnull=True), name="site_inspections_review_one_pending"),
            models.CheckConstraint(condition=Q(trigger__in=values(ReviewTrigger)), name="site_inspections_review_trigger_valid"),
            models.CheckConstraint(condition=Q(decision__in=values(ReviewDecision)), name="site_inspections_review_decision_valid"),
            models.CheckConstraint(condition=Q(decision="PENDING") | Q(decided_at__isnull=False), name="site_inspections_review_decided_at"),
        ]


class Observation(BaseModel):
    # An observation belongs to its inspection: CASCADE (true child).
    inspection = models.ForeignKey("site_inspections.Inspection", on_delete=models.CASCADE, related_name="observations")
    stage = models.PositiveSmallIntegerField(null=True, blank=True)
    category = models.CharField(max_length=24, choices=ObservationCategory.choices, default=ObservationCategory.GENERAL)
    note = models.TextField()
    photos = models.ManyToManyField("site_inspections.Photo", related_name="observations", blank=True, db_table="site_inspections_observation_photo")

    class Meta:
        db_table = "site_inspections_observation"
        ordering = ["inspection_id", "created_at", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(category__in=values(ObservationCategory)), name="site_inspections_observation_category_valid"),
            models.CheckConstraint(condition=Q(stage__isnull=True) | Q(stage__gte=1, stage__lte=10), name="site_inspections_observation_stage_range"),
            models.CheckConstraint(condition=~Q(note=""), name="site_inspections_observation_note_present"),
        ]
