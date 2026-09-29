"""``hr.attendance_inputs_changed`` — tells the attendance package which stored days to recompute (A8).

Contract (the attendance package consumes it in a later wave)::

    {"employee_uids": ["<uuid>", …], "date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD", "reason": "<what changed>"}
    {"office_uid": "<uuid>" | null,  "date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD", "reason": "<what changed>"}

Exactly one of ``employee_uids`` / ``office_uid`` is present; ``office_uid: null`` means every office (a global
holiday or rule). The range is inclusive; ``date_to`` may lie in the future (the consumer never stores days ≥ today,
A7). Changes without a date of their own (a shift's times, an employee's office or shift, an attendance rule
without ``effective_from``) cover the last ``HR_RECOMPUTE_LOOKBACK_DAYS`` days (default 31) up to today.

Emitted by: shift edits (employees whose effective shift it is), office default-shift changes, employee office /
shift / joined / left / activation changes, holiday writes, leave approve / cancel of approved leave / leave created
approved, attendance-rule writes (old and new scope). The devices package emits it for per-device PIN link changes
(A1/A8) through :func:`for_employees` with ``reason="device_link_changed"``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta

from django.conf import settings
from django.db.models import Q

from core.outbox import emit
from hr.services.common import today_in

EVENT = "hr.attendance_inputs_changed"


def lookback_days() -> int:
    return int(getattr(settings, "HR_RECOMPUTE_LOOKBACK_DAYS", 31))


def recent_range(date_from: date | None = None) -> tuple[date, date]:
    """``(date_from or today − lookback, today)``."""
    today = today_in()
    return (date_from or today - timedelta(days=lookback_days())), today


def _range(date_from: date, date_to: date) -> dict:
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    return {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}


def for_employees(employee_uids: Iterable, date_from: date, date_to: date, *, reason: str) -> None:
    uids = sorted({str(uid) for uid in employee_uids if uid})
    if not uids:
        return
    emit(EVENT, {"employee_uids": uids, **_range(date_from, date_to), "reason": reason}, aggregate_type="hr.employee")


def for_office(office_uid, date_from: date, date_to: date, *, reason: str) -> None:
    """``office_uid=None``: every office."""
    emit(EVENT, {"office_uid": str(office_uid) if office_uid else None, **_range(date_from, date_to), "reason": reason}, aggregate_type="hr.office", aggregate_uid=office_uid)


def employees_on_shift(shift) -> list:
    """Uids of live employees whose effective shift is ``shift`` (own shift, or none and the office default)."""
    from hr.models import Employee

    return list(Employee.objects.filter(Q(shift=shift) | Q(shift__isnull=True, office__default_shift=shift)).values_list("uid", flat=True))


def employees_in_office(office) -> list:
    from hr.models import Employee

    return list(Employee.objects.filter(office=office).values_list("uid", flat=True))
