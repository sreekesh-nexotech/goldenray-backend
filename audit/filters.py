"""Filters for ``GET audit/`` (PLAN §3.4): ``object_type``, ``object_uid``, ``actor`` (user uid), ``action``,
``from`` and ``to``.

* ``action`` matches exactly; a trailing ``*`` matches a prefix (``accounts.*``).
* ``from``/``to`` take an ISO date (``2026-09-01``) or date-time (``2026-09-01T10:00:00+05:30``). A date-only ``to``
  includes that whole day; a naive value is read in the server time zone (``Asia/Kolkata``).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import django_filters
from django import forms
from django.utils import timezone

from audit.models import AuditLog


class _BoundaryField(forms.CharField):
    def __init__(self, *args, upper: bool = False, **kwargs):
        self.upper = upper
        super().__init__(*args, **kwargs)

    def to_python(self, value):
        value = super().to_python(value)
        if not value:
            return None
        try:
            if len(value) == 10:
                day = date.fromisoformat(value)
                start = timezone.make_aware(datetime.combine(day, time.min))
                return ("lt", start + timedelta(days=1)) if self.upper else ("gte", start)
            moment = datetime.fromisoformat(value.replace(" ", "+") if value.count(" ") == 1 and "T" in value else value)
        except ValueError:
            raise forms.ValidationError("Use an ISO date (YYYY-MM-DD) or date-time.", code="invalid") from None
        if timezone.is_naive(moment):
            moment = timezone.make_aware(moment)
        return ("lte", moment) if self.upper else ("gte", moment)


class BoundaryFilter(django_filters.Filter):
    field_class = _BoundaryField

    def __init__(self, *args, upper: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.extra["upper"] = upper

    def filter(self, qs, value):
        if not value:
            return qs
        lookup, moment = value
        return qs.filter(**{f"{self.field_name}__{lookup}": moment})


class ActionFilter(django_filters.CharFilter):
    def filter(self, qs, value):
        if not value:
            return qs
        if value.endswith("*"):
            return qs.filter(action__startswith=value[:-1])
        return qs.filter(action=value)


class _AuditLogFilterBase(django_filters.FilterSet):
    object_type = django_filters.CharFilter(help_text="e.g. `accounts.user`.")
    object_uid = django_filters.UUIDFilter()
    actor = django_filters.UUIDFilter(field_name="actor__uid", help_text="Uid of the acting user.")
    action = ActionFilter(max_length=65, help_text="Exact action, or a prefix ending in `*` (e.g. `accounts.*`).")

    class Meta:
        model = AuditLog
        fields: list[str] = []


# ``from`` is a Python keyword, so the filter set is assembled with type() (the metaclass collects both filters).
AuditLogFilter = type(
    "AuditLogFilter",
    (_AuditLogFilterBase,),
    {
        "__module__": __name__,
        "__doc__": "Filters for the audit log list.",
        "from": BoundaryFilter(field_name="at", help_text="Earliest `at` (ISO date or date-time, inclusive)."),
        "to": BoundaryFilter(field_name="at", upper=True, help_text="Latest `at` (ISO date — whole day included — or date-time, inclusive)."),
    },
)
