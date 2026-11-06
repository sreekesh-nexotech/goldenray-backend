"""``approvals_change_request`` — a price or configuration change asked for by someone who may not apply it directly.

A Project Head or Procurement user submits the change (the same body the direct endpoint takes); an Admin approves
it, which applies it through the owning app's own serializer and service as the Admin, or rejects it. Nothing
changes until then. What can be requested is the closed set registered in :mod:`approvals.services.kinds`.

* ``kind`` names the registered change kind (``bom.fixed_item``, ``pricing.cost_config`` …);
* ``payload`` is the submitted body, validated on submit and again when applied;
* ``before`` is the target's representation at submit, so the reviewer sees what the change replaces;
* ``base_version`` is the target's ``version`` at submit: applying is refused (409 ``stale_version``) when the
  target changed in between, rather than overwriting the newer edit.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import BaseModel


class ChangeRequest(BaseModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        CANCELLED = "CANCELLED", "Cancelled"

    class Operation(models.TextChoices):
        CREATE = "CREATE", "Create"
        UPDATE = "UPDATE", "Update"
        DELETE = "DELETE", "Delete"
        SET = "SET", "Set values"

    kind = models.CharField(max_length=64, help_text="Registered change kind (approvals.services.kinds).")
    operation = models.CharField(max_length=8, choices=Operation.choices)
    target_uid = models.UUIDField(null=True, blank=True, help_text="The row changed (UPDATE/DELETE); the created row's uid lands in result_uid.")
    target_label = models.CharField(max_length=255, blank=True, default="")
    # Validated documents (re-validated when applied): the submitted body, and the target as it was at submit.
    payload = models.JSONField(default=dict, blank=True)
    before = models.JSONField(default=dict, blank=True)
    base_version = models.PositiveIntegerField(null=True, blank=True)
    reason = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    # Attribution: removing a user never removes the request history.
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True, default="")
    result_uid = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "approvals_change_request"
        ordering = ["-created_at", "-id"]
        indexes = [
            # The approval queue: pending first, newest first.
            models.Index(fields=["status", "created_at"], name="approvals_cr_status_created"),
            # "Is there already a request for this row?"
            models.Index(fields=["kind", "target_uid"], name="approvals_cr_kind_target"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(status__in=["PENDING", "APPROVED", "REJECTED", "CANCELLED"]), name="approvals_change_request_status_valid"),
            models.CheckConstraint(condition=Q(operation__in=["CREATE", "UPDATE", "DELETE", "SET"]), name="approvals_change_request_operation_valid"),
            models.CheckConstraint(condition=~Q(kind=""), name="approvals_change_request_kind_present"),
            # One open request per row: a second one would be decided against a stale "before".
            models.UniqueConstraint(
                fields=["kind", "target_uid"],
                condition=Q(deleted_at__isnull=True, status="PENDING", target_uid__isnull=False),
                name="approvals_change_request_one_pending_per_target",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.operation} [{self.status}]"
