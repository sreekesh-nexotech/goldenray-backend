"""``attendance_recompute_request`` *(no base; DV-88)* — the debounce queue of the event-driven recompute (A8).

The outbox handlers of ``attendance.punches_ingested`` and ``hr.attendance_inputs_changed`` record what has to be
recomputed (whom, which dates, why) with ``due_at`` = now + the debounce; a Celery task (enqueued on commit, with a
Beat safety net every minute) takes every due request, merges them per employee and recomputes once. A burst of
uploads therefore costs one recompute, nothing is computed inside a terminal's or an agent's request, and a lost task
message loses nothing (the rows stay until they are processed).

Scope: exactly one of ``employee`` (one person), ``office`` (everyone posted there) or ``all_employees``.
"""

from __future__ import annotations

from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class RecomputeRequest(models.Model):
    id = models.BigAutoField(primary_key=True)
    # CASCADE: a pending request for a removed employee/office has nothing left to recompute.
    employee = models.ForeignKey("hr.Employee", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    office = models.ForeignKey("hr.Office", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    all_employees = models.BooleanField(default=False)
    date_from = models.DateField()
    date_to = models.DateField()
    reason = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)
    due_at = models.DateTimeField(default=timezone.now)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")

    class Meta:
        db_table = "attendance_recompute_request"
        ordering = ["due_at", "id"]
        default_permissions = ()
        constraints = [
            models.CheckConstraint(condition=Q(date_to__gte=F("date_from")), name="attendance_recompute_dates_ordered"),
            models.CheckConstraint(
                condition=(
                    Q(employee__isnull=False, office__isnull=True, all_employees=False)
                    | Q(employee__isnull=True, office__isnull=False, all_employees=False)
                    | Q(employee__isnull=True, office__isnull=True, all_employees=True)
                ),
                name="attendance_recompute_one_scope",
            ),
            models.CheckConstraint(condition=~Q(reason=""), name="attendance_recompute_reason_not_blank"),
        ]
        indexes = [
            models.Index(fields=["due_at"], name="attendance_recompute_due_idx"),
            models.Index(fields=["employee"], name="attendance_recompute_emp_idx"),
            models.Index(fields=["office"], name="attendance_recompute_office_idx"),
        ]

    def __str__(self) -> str:
        who = f"employee {self.employee_id}" if self.employee_id else (f"office {self.office_id}" if self.office_id else "everyone")
        return f"{who} {self.date_from}..{self.date_to} ({self.reason})"
