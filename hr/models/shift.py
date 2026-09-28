"""``hr_shift`` (PLAN §2.9): the only source of working-time rules, including the v4 engine fields.

v4 additions against eSSL (PLAN §2.9 A1–A12): ``overnight_buffer_minutes`` (A6: punches up to ``end_time`` + buffer
belong to the previous work date), ``half_day_after_minutes`` (A5: the half-day deadline is the shift start plus this,
overridable by ``hr_attendance_rule``), ``debounce_minutes`` (A3: punches this close to the previous accepted punch
are ignored). ``half_day_minutes`` is used by the v4 break rule (A2).

Days are ``0`` = Monday … ``6`` = Sunday. ``weekly_off_days`` wins over ``working_days`` when both name a day.
"""

from __future__ import annotations

from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Upper

from core.models import BaseModel

DEFAULT_WORKING_DAYS = [0, 1, 2, 3, 4, 5]
DEFAULT_WEEKLY_OFF_DAYS = [6]
DAY_MINUTES = 24 * 60

# (field, default, maximum) — every minute column is bounded by a DB check.
MINUTE_FIELDS: tuple[tuple[str, int, int], ...] = (
    ("overnight_buffer_minutes", 180, 720),
    ("grace_minutes", 10, 720),
    ("late_threshold_minutes", 0, 720),
    ("early_exit_threshold_minutes", 15, 720),
    ("full_day_minutes", 480, DAY_MINUTES),
    ("half_day_minutes", 240, DAY_MINUTES),
    ("half_day_after_minutes", 30, 720),
    ("break_minutes", 60, 720),
    ("debounce_minutes", 2, 60),
    ("overtime_after_minutes", 480, DAY_MINUTES),
)


def _default_working_days():
    return list(DEFAULT_WORKING_DAYS)


def _default_weekly_off_days():
    return list(DEFAULT_WEEKLY_OFF_DAYS)


class Shift(BaseModel):
    class HalfDayAfterSource(models.TextChoices):
        """Where the displayed arrival deadline comes from (not a column)."""

        RULE = "rule", "An attendance rule"
        SHIFT = "shift", "The shift's own half_day_after_minutes"

    code = models.CharField(max_length=30)
    name = models.CharField(max_length=120)
    start_time = models.TimeField()
    end_time = models.TimeField(help_text="On the next day when is_overnight.")
    is_overnight = models.BooleanField(default=False)
    overnight_buffer_minutes = models.PositiveIntegerField(default=180)
    grace_minutes = models.PositiveIntegerField(default=10)
    late_threshold_minutes = models.PositiveIntegerField(default=0, help_text="Further allowance beyond grace before a day is late.")
    early_exit_threshold_minutes = models.PositiveIntegerField(default=15)
    full_day_minutes = models.PositiveIntegerField(default=480)
    half_day_minutes = models.PositiveIntegerField(default=240)
    half_day_after_minutes = models.PositiveIntegerField(default=30, help_text="Arrivals later than start + this are a half day (A5).")
    break_minutes = models.PositiveIntegerField(default=60)
    auto_deduct_break = models.BooleanField(default=True)
    debounce_minutes = models.PositiveIntegerField(default=2, help_text="Punches within this many minutes of the previous one are ignored (A3).")
    overtime_enabled = models.BooleanField(default=True)
    overtime_after_minutes = models.PositiveIntegerField(default=480)
    working_days = models.JSONField(default=_default_working_days, help_text="Weekdays 0=Monday … 6=Sunday.")
    weekly_off_days = models.JSONField(default=_default_weekly_off_days, help_text="Weekdays 0=Monday … 6=Sunday; wins over working_days.")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "hr_shift"
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(Upper("code"), condition=Q(deleted_at__isnull=True), name="hr_shift_code_live_uniq"),
            models.CheckConstraint(condition=~Q(code=""), name="hr_shift_code_not_blank"),
            # An overnight shift ends on the next day (end ≤ start on the clock); a day shift ends after it starts.
            models.CheckConstraint(
                condition=Q(is_overnight=True, end_time__lte=F("start_time")) | Q(is_overnight=False, end_time__gt=F("start_time")),
                name="hr_shift_overnight_consistent",
            ),
            models.CheckConstraint(condition=Q(half_day_minutes__lte=F("full_day_minutes")), name="hr_shift_half_day_within_full_day"),
            *[models.CheckConstraint(condition=Q(**{f"{name}__lte": maximum}), name=f"hr_shift_{name}_max") for name, _, maximum in MINUTE_FIELDS],
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"
