"""Record scopes ``office`` / ``self`` for ``employees`` and ``leave`` (PLAN §3.2; registered in ``HrConfig.ready``).

* ``self`` — the employee record linked to the caller's login (``hr_employee.user``), and that employee's leave;
* ``office`` — records of the office the caller's own employee record belongs to (the Office Manager).

A caller without a linked employee (or whose employee has no office) sees nothing under a narrow scope — the
filters fail closed like ``core.scopes``. The attendance package registers its own filters for ``attendance``.
"""

from __future__ import annotations

from core import scopes

_MEMO = "_hr_own_employee"
_MISSING = object()


def own_employee(user):
    """The live employee linked to ``user`` (memoised on the user object for the request), or ``None``."""
    if user is None or getattr(user, "pk", None) is None:
        return None
    cached = getattr(user, _MEMO, _MISSING)
    if cached is not _MISSING:
        return cached
    from hr.models import Employee

    employee = Employee.objects.select_related("office").filter(user_id=user.pk).first()
    setattr(user, _MEMO, employee)
    return employee


def forget(user) -> None:
    if hasattr(user, _MEMO):
        delattr(user, _MEMO)


def own_office_id(user):
    employee = own_employee(user)
    return employee.office_id if employee is not None else None


@scopes.register("employees", "self")
def employees_self(queryset, user):
    return queryset.filter(user_id=user.pk)


@scopes.register("employees", "office")
def employees_office(queryset, user):
    office_id = own_office_id(user)
    return queryset.filter(office_id=office_id) if office_id is not None else queryset.none()


@scopes.register("leave", "self")
def leave_self(queryset, user):
    return queryset.filter(employee__user_id=user.pk)


@scopes.register("leave", "office")
def leave_office(queryset, user):
    office_id = own_office_id(user)
    return queryset.filter(employee__office_id=office_id) if office_id is not None else queryset.none()


def employees_for_module(user, module: str):
    """Live employees within ``user``'s record scope of ``module`` (``employees`` or ``leave``)."""
    from hr.models import Employee

    queryset = Employee.objects.all()
    if module == "leave":
        scope = scopes.resolve_scope(user, "leave")
        if scope == "self":
            return queryset.filter(user_id=user.pk)
        if scope == "office":
            office_id = own_office_id(user)
            return queryset.filter(office_id=office_id) if office_id is not None else queryset.none()
        return queryset if scope == "all" else queryset.none()
    return scopes.apply(queryset, user, module)
