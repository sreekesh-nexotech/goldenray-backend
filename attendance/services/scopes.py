"""Record scopes ``self`` / ``office`` of the ``attendance`` module (PLAN §3.2; registered in ``AttendanceConfig.ready``).

The same definitions as hr's ``employees``/``leave`` scopes (``hr.services.scopes``): ``self`` is the employee linked to
the caller's login, ``office`` the employees of the office the caller's own employee record belongs to (the Office
Manager); a caller without a linked employee sees nothing under a narrow scope (fail closed). One filter per scope
serves every attendance queryset — employees, days, corrections and raw punches (a punch belongs to whoever its
``(device, pin)`` is linked to, A1).
"""

from __future__ import annotations

from django.db.models import Exists, OuterRef

from core import scopes
from hr.services.scopes import own_office_id

MODULE = "attendance"


def _employee_condition(prefix: str, *, user=None, office_id=None) -> dict:
    if user is not None:
        return {f"{prefix}user_id": user.pk}
    return {f"{prefix}office_id": office_id}


def _narrow(queryset, **target):
    from attendance.models import AttendanceCorrection, AttendanceDay, RawPunch
    from devices.models import DeviceUser
    from hr.models import Employee

    model = queryset.model
    if model is Employee:
        return queryset.filter(**_employee_condition("", **target))
    if model is AttendanceDay:
        return queryset.filter(**_employee_condition("employee__", **target))
    if model is AttendanceCorrection:
        return queryset.filter(**_employee_condition("day__employee__", **target))
    if model is RawPunch:
        linked = DeviceUser.objects.filter(device_id=OuterRef("device_id"), pin=OuterRef("pin"), **_employee_condition("employee__", **target))
        return queryset.filter(Exists(linked))
    return queryset.none()  # fail closed for anything this module does not know


@scopes.register(MODULE, "self")
def attendance_self(queryset, user):
    return _narrow(queryset, user=user)


@scopes.register(MODULE, "office")
def attendance_office(queryset, user):
    office_id = own_office_id(user)
    return _narrow(queryset, office_id=office_id) if office_id is not None else queryset.none()


def employees_in_scope(user, queryset=None):
    """Live employees whose attendance ``user`` may see (``queryset`` defaults to every live employee)."""
    from hr.models import Employee

    return scopes.apply(queryset if queryset is not None else Employee.objects.all(), user, MODULE)


def scope_of(user) -> str | None:
    return scopes.resolve_scope(user, MODULE)


def scoped(queryset, user):
    """``queryset`` narrowed to ``user``'s attendance scope (fail closed)."""
    return scopes.apply(queryset, user, MODULE)
