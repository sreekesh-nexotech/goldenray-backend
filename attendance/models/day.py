"""``attendance_day`` and ``attendance_correction`` (PLAN §2.9).

``attendance_day`` is one employee's processed work date, written only by the recompute
(``attendance.services.recompute``, engine v4) for dates before today in the office's time zone (A7) — and by a
correction, which pins the day (``is_corrected``) so the recompute leaves it alone until the correction is revoked
(A12). ``first_in`` / ``last_out`` are office wall-clock readings (``timestamp``). ``source_raw_ids`` /
``ignored_raw_ids`` hold ``attendance_raw_punch.id`` values; they never leave the service layer.

``attendance_correction`` is HR's change to one field of one day, with the value it replaced and the reason; revoking
it restores the value and hands the day back to the recompute.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from attendance.models.fields import WallClockDateTimeField
from core.models import BaseModel

STATUSES = ["PRESENT", "LATE", "ABSENT", "HALF_DAY", "WEEKLY_OFF", "HOLIDAY", "ON_LEAVE"]
CORRECTABLE_FIELDS = ["status", "first_in", "last_out", "working_minutes", "break_minutes", "late_minutes", "early_exit_minutes", "overtime_minutes", "is_late", "is_early_exit"]


class AttendanceDay(BaseModel):
    class Status(models.TextChoices):
        PRESENT = "PRESENT", "Present"
        LATE = "LATE", "Late"
        ABSENT = "ABSENT", "Absent"
        HALF_DAY = "HALF_DAY", "Half Day"
        WEEKLY_OFF = "WEEKLY_OFF", "Weekly Off"
        HOLIDAY = "HOLIDAY", "Holiday"
        ON_LEAVE = "ON_LEAVE", "Leave"

    # CASCADE: a processed day is a true child of its employee (employees are only soft-deleted).
    employee = models.ForeignKey("hr.Employee", on_delete=models.CASCADE, related_name="attendance_days")
    work_date = models.DateField()
    # SET_NULL: the posting office / shift / first terminal at compute time; the day survives their removal.
    office = models.ForeignKey("hr.Office", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    shift = models.ForeignKey("hr.Shift", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    first_device = models.ForeignKey("devices.Device", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    first_in = WallClockDateTimeField(null=True, blank=True)
    last_out = WallClockDateTimeField(null=True, blank=True)
    punch_count = models.SmallIntegerField(default=0)
    working_minutes = models.IntegerField(default=0)
    break_minutes = models.IntegerField(default=0)
    late_minutes = models.IntegerField(default=0)
    early_exit_minutes = models.IntegerField(default=0)
    overtime_minutes = models.IntegerField(default=0)
    is_late = models.BooleanField(default=False)
    is_early_exit = models.BooleanField(default=False)
    status = models.CharField(max_length=12, choices=Status.choices)
    worked_on_off_day = models.BooleanField(default=False)
    leave_conflict = models.BooleanField(default=False)
    missing_out = models.BooleanField(default=False)
    source_raw_ids = models.JSONField(default=list, blank=True)
    ignored_raw_ids = models.JSONField(default=list, blank=True)
    processing_version = models.CharField(max_length=24)
    computed_at = models.DateTimeField()
    is_corrected = models.BooleanField(default=False)

    class Meta:
        db_table = "attendance_day"
        ordering = ["-work_date", "employee_id"]
        constraints = [
            models.UniqueConstraint(fields=["employee", "work_date"], condition=Q(deleted_at__isnull=True), name="attendance_day_employee_date_live_uniq"),
            models.CheckConstraint(condition=Q(status__in=STATUSES), name="attendance_day_status_valid"),
            models.CheckConstraint(
                condition=Q(punch_count__gte=0, working_minutes__gte=0, break_minutes__gte=0, late_minutes__gte=0, early_exit_minutes__gte=0, overtime_minutes__gte=0),
                name="attendance_day_counts_non_negative",
            ),
            models.CheckConstraint(condition=Q(last_out__isnull=True) | Q(first_in__isnull=False), name="attendance_day_out_needs_in"),
            models.CheckConstraint(condition=Q(last_out__isnull=True) | Q(last_out__gte=models.F("first_in")), name="attendance_day_out_after_in"),
            models.CheckConstraint(
                condition=Q(missing_out=True, first_in__isnull=False, last_out__isnull=True) | (Q(missing_out=False) & (Q(first_in__isnull=True) | Q(last_out__isnull=False))),
                name="attendance_day_missing_out_derived",
            ),
        ]
        indexes = [
            models.Index(fields=["office", "work_date"], name="attendance_day_office_date_idx"),
            models.Index(fields=["work_date"], name="attendance_day_date_idx"),
            models.Index(fields=["employee", "work_date"], name="attendance_day_employee_idx"),
            models.Index(fields=["shift"], name="attendance_day_shift_idx"),
            models.Index(fields=["first_device"], name="attendance_day_device_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.employee_id} {self.work_date} {self.status}"


class AttendanceCorrection(BaseModel):
    field = models.CharField(max_length=24, choices=[(name, name) for name in CORRECTABLE_FIELDS])
    # CASCADE: a correction is a true child of the day it corrects.
    day = models.ForeignKey(AttendanceDay, on_delete=models.CASCADE, related_name="corrections")
    old = models.JSONField(null=True, blank=True, help_text="The value the correction replaced (restored on revoke).")
    new = models.JSONField(null=True, blank=True)
    reason = models.TextField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    # Attribution: SET_NULL.
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    revoke_reason = models.TextField(blank=True, default="")

    class Meta:
        db_table = "attendance_correction"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(field__in=CORRECTABLE_FIELDS), name="attendance_correction_field_valid"),
            models.CheckConstraint(condition=~Q(reason=""), name="attendance_correction_reason_not_blank"),
            # one active correction per field of a day: revoke it before correcting the field again
            models.UniqueConstraint(fields=["day", "field"], condition=Q(deleted_at__isnull=True, revoked_at__isnull=True), name="attendance_correction_active_field_uniq"),
            models.CheckConstraint(condition=Q(revoked_at__isnull=True) | ~Q(revoke_reason=""), name="attendance_correction_revoke_reason"),
        ]
        indexes = [models.Index(fields=["day", "created_at"], name="attendance_correction_day_idx"), models.Index(fields=["revoked_by"], name="attendance_correction_rev_idx")]

    def __str__(self) -> str:
        return f"{self.day_id}.{self.field}"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and self.deleted_at is None
