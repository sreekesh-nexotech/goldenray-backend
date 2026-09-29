"""``site_inspections_location_approval`` and ``site_inspections_additional_work_item`` (PLAN §2.7).

A location approval freezes both current rectangles (``location_snapshot``) when it is requested; the customer answers
through a signed link after an OTP to ``customer_phone_e164`` (only the SHA-256 of the link token is stored). A
paper-signed answer (PLAN D-13) is recorded by staff with the uploaded scan and a reason.

Additional-work items follow the enforced seven-state lifecycle; an ``EXTRA_STRUCTURE`` agreement (referenced by
uid, agreements context) carries the cost from COST_CALCULATED on.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import BaseModel
from site_inspections.models.choices import ApprovalStatus, WorkStatus, WorkType, WorkUnit, values
from site_inspections.models.inspection import E164_RE


class LocationApproval(BaseModel):
    # An approval belongs to its inspection: CASCADE (true child).
    inspection = models.ForeignKey("site_inspections.Inspection", on_delete=models.CASCADE, related_name="approvals")
    number = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=10, choices=ApprovalStatus.choices, default=ApprovalStatus.PENDING)
    location_snapshot = models.JSONField(null=True, blank=True, help_text="Both current rectangles when requested (engines.inspection_readiness.build_location_snapshot).")
    customer_name = models.CharField(max_length=255)
    customer_phone_e164 = models.CharField(max_length=16, blank=True, default="")
    token_hash = models.CharField(max_length=64, blank=True, default="", help_text="SHA-256 (hex) of the link token; the token itself is never stored.")
    expires_at = models.DateTimeField(null=True, blank=True)
    otp_verified_at = models.DateTimeField(null=True, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    responded_ip = models.GenericIPAddressField(null=True, blank=True)
    customer_comment = models.TextField(blank=True, default="")
    # Private SIGNATURE asset drawn by the customer: SET_NULL (media rows are only soft-deleted, usage-guarded).
    signature_asset = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # Scan of the paper-signed approval (D-13): SET_NULL, see signature_asset.
    paper_scan_asset = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # The staff member who recorded a paper approval: SET_NULL (attribution).
    approved_by_staff = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    paper_reason = models.TextField(blank=True, default="")

    class Meta:
        db_table = "site_inspections_location_approval"
        ordering = ["inspection_id", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["inspection"], condition=Q(status="PENDING", deleted_at__isnull=True), name="site_inspections_approval_one_pending"),
            models.UniqueConstraint(fields=["inspection", "number"], name="site_inspections_approval_number_uniq"),
            models.UniqueConstraint(fields=["token_hash"], condition=~Q(token_hash=""), name="site_inspections_approval_token_uniq"),
            models.CheckConstraint(condition=Q(status__in=values(ApprovalStatus)), name="site_inspections_approval_status_valid"),
            models.CheckConstraint(condition=Q(customer_phone_e164="") | Q(customer_phone_e164__regex=E164_RE), name="site_inspections_approval_phone_format"),
            models.CheckConstraint(condition=Q(token_hash="") | Q(token_hash__regex=r"^[0-9a-f]{64}$"), name="site_inspections_approval_token_hex"),
            models.CheckConstraint(condition=~Q(status="PENDING") | (~Q(token_hash="") & Q(expires_at__isnull=False)), name="site_inspections_approval_pending_has_link"),
            models.CheckConstraint(condition=Q(approved_by_staff__isnull=True) | (Q(paper_scan_asset__isnull=False) & ~Q(paper_reason="")), name="site_inspections_approval_paper_documented"),
        ]


class AdditionalWorkItem(BaseModel):
    # A work item belongs to its inspection: CASCADE (true child).
    inspection = models.ForeignKey("site_inspections.Inspection", on_delete=models.CASCADE, related_name="additional_work")
    work_type = models.CharField(max_length=24, choices=WorkType.choices)
    required = models.BooleanField(default=True)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    unit = models.CharField(max_length=8, choices=WorkUnit.choices, blank=True, default="")
    dimensions = models.CharField(max_length=120, blank=True, default="")
    reason = models.TextField(blank=True, default="")
    customer_impacting = models.BooleanField(default=False)
    status = models.CharField(max_length=22, choices=WorkStatus.choices, default=WorkStatus.IDENTIFIED)
    agreement_uid = models.UUIDField(null=True, blank=True, help_text="The EXTRA_STRUCTURE agreement that prices this item (agreements context; no FK).")
    # Attribution of the customer decision (APPROVED/REJECTED): SET_NULL.
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "site_inspections_additional_work_item"
        ordering = ["inspection_id", "work_type"]
        constraints = [
            models.UniqueConstraint(fields=["inspection", "work_type"], condition=Q(deleted_at__isnull=True), name="site_inspections_work_one_per_type"),
            models.CheckConstraint(condition=Q(work_type__in=values(WorkType)), name="site_inspections_work_type_valid"),
            models.CheckConstraint(condition=Q(status__in=values(WorkStatus)), name="site_inspections_work_status_valid"),
            models.CheckConstraint(condition=Q(unit="") | Q(unit__in=values(WorkUnit)), name="site_inspections_work_unit_valid"),
            models.CheckConstraint(condition=Q(quantity__isnull=True) | Q(quantity__gte=0), name="site_inspections_work_quantity_non_negative"),
            models.CheckConstraint(condition=~Q(status__in=["COST_CALCULATED", "CUSTOMER_QUOTE_SENT"]) | Q(agreement_uid__isnull=False), name="site_inspections_work_costed_has_agreement"),
            models.CheckConstraint(condition=~Q(status__in=["APPROVED", "REJECTED"]) | Q(decided_at__isnull=False), name="site_inspections_work_decided_at"),
        ]
