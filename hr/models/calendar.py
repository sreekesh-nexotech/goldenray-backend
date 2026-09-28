"""Calendar inputs of the attendance engine (PLAN §2.9): holidays, leave types, leave records, attendance rules."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Upper

from core.models import BaseModel


class Holiday(BaseModel):
    """A non-working day for one office, or for every office when ``office`` is null.

    A holiday only changes the *status* of a day; punches on it are still counted (A4: worked holidays keep
    ``HOLIDAY`` with ``worked_on_off_day``). ``is_active=False`` retires it while past months still explain themselves.
    """

    # A true child of the office (PLAN: C). Offices are soft-deleted, and the office service soft-deletes these.
    office = models.ForeignKey("hr.Office", null=True, blank=True, on_delete=models.CASCADE, related_name="holidays")
    date = models.DateField()
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, default="", help_text="DV-17: kept from eSSL.")

    class Meta:
        db_table = "hr_holiday"
        ordering = ["date", "id"]
        constraints = [
            models.UniqueConstraint(fields=["office", "date"], condition=Q(deleted_at__isnull=True, office__isnull=False), name="hr_holiday_office_date_live_uniq"),
            models.UniqueConstraint(fields=["date"], condition=Q(deleted_at__isnull=True, office__isnull=True), name="hr_holiday_global_date_live_uniq"),
        ]
        indexes = [models.Index(fields=["date"], name="hr_holiday_date_idx")]

    def __str__(self) -> str:
        return f"{self.date} {self.name}"


class LeaveType(BaseModel):
    """Kinds of leave. eSSL had free text; the import creates one type per distinct string (case-insensitive)."""

    code = models.CharField(max_length=40)
    name = models.CharField(max_length=120)
    paid = models.BooleanField(default=True)
    requires_approval = models.BooleanField(default=True, help_text="When false, self-service requests are approved on creation.")

    class Meta:
        db_table = "hr_leave_type"
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(Upper("code"), condition=Q(deleted_at__isnull=True), name="hr_leave_type_code_live_uniq"),
            models.CheckConstraint(condition=~Q(code=""), name="hr_leave_type_code_not_blank"),
        ]

    def __str__(self) -> str:
        return self.name


class LeaveRecord(BaseModel):
    """An absence request or record. Only APPROVED leave changes what attendance reports."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        CANCELLED = "CANCELLED", "Cancelled"

    # A true child of the employee (PLAN: C); employees are soft-deleted, never removed while they have leave.
    employee = models.ForeignKey("hr.Employee", on_delete=models.CASCADE, related_name="leave_records")
    # Lookup: PROTECT — a leave type in use cannot be deleted. Column name as PLAN (`type_id`).
    leave_type = models.ForeignKey("hr.LeaveType", on_delete=models.PROTECT, related_name="leave_records", db_column="type_id")
    date_from = models.DateField()
    date_to = models.DateField()
    is_half_day = models.BooleanField(default=False)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    reason = models.TextField(blank=True, default="")
    # Attribution: SET_NULL.
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "hr_leave_record"
        ordering = ["-date_from", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(status__in=["PENDING", "APPROVED", "REJECTED", "CANCELLED"]), name="hr_leave_record_status_valid"),
            models.CheckConstraint(condition=Q(date_to__gte=F("date_from")), name="hr_leave_record_dates_ordered"),
            models.CheckConstraint(condition=Q(is_half_day=False) | Q(date_to=F("date_from")), name="hr_leave_record_half_day_single_date"),
        ]
        indexes = [
            models.Index(fields=["employee", "date_from"], name="hr_leave_employee_from_idx"),
            models.Index(fields=["status", "date_from"], name="hr_leave_status_from_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.employee_id} {self.date_from}–{self.date_to} {self.status}"

    @property
    def days(self) -> float:
        return 0.5 if self.is_half_day else float((self.date_to - self.date_from).days + 1)


class AttendanceRule(BaseModel):
    """Named half-day parameters for the engine, scoped globally, to an office or to a shift (never both).

    ``rules`` accepts only the recognised keys (``hr.services.rules.validate_rules``): ``half_day_after``
    (``HH:MM``), ``half_day_after_minutes`` and ``half_day_under_minutes`` (whole minutes ≥ 0). Precedence when the
    engine merges them: global < office < shift, then ``effective_from``; later keys override earlier ones.
    """

    class Scope(models.TextChoices):
        """Derived from ``office``/``shift`` (not a column)."""

        GLOBAL = "GLOBAL", "Every office and shift"
        OFFICE = "OFFICE", "One office"
        SHIFT = "SHIFT", "One shift"

    name = models.CharField(max_length=120)
    # True children of their scope (PLAN: C).
    office = models.ForeignKey("hr.Office", null=True, blank=True, on_delete=models.CASCADE, related_name="attendance_rules")
    shift = models.ForeignKey("hr.Shift", null=True, blank=True, on_delete=models.CASCADE, related_name="attendance_rules")
    rules = models.JSONField(default=dict)
    effective_from = models.DateField(null=True, blank=True, help_text="Applies to work dates on or after this date (always when empty).")
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        db_table = "hr_attendance_rule"
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["name"], condition=Q(deleted_at__isnull=True), name="hr_attendance_rule_name_live_uniq"),
            models.CheckConstraint(condition=Q(office__isnull=True) | Q(shift__isnull=True), name="hr_attendance_rule_single_scope"),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def scope(self) -> str:
        if self.shift_id is not None:
            return self.Scope.SHIFT
        return self.Scope.OFFICE if self.office_id is not None else self.Scope.GLOBAL
