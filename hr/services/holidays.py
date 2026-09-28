"""``hr/holidays/`` — office or global holidays (module ``hr_setup``: view / edit), soft delete.

* one live holiday per office and date, and one global (``office`` null) holiday per date — both partial uniques
  (409 ``holiday_exists``; eSSL enforced neither on update, nor the global one at all);
* ``is_active=False`` retires a holiday but keeps it (past months still explain themselves);
* every write re-computes the affected days (``hr.attendance_inputs_changed``: the office, or every office).
"""

from __future__ import annotations

from django.db import IntegrityError, transaction

from audit.services import changes, record, snapshot
from core.services import stamp_create
from hr.models import Holiday
from hr.services import recompute
from hr.services.common import bump_setup, lock, unique_conflict
from hr.services.validation import invalid

EDITABLE_FIELDS = ("office", "date", "name", "is_active", "notes")
SNAPSHOT_FIELDS = EDITABLE_FIELDS
_TAKEN = ("holiday_exists", "date", "That date is already a holiday for this office (or for every office).")
UNIQUE = {"hr_holiday_office_date_live_uniq": _TAKEN, "hr_holiday_global_date_live_uniq": _TAKEN}


def holidays_queryset():
    return Holiday.objects.select_related("office").order_by("date", "id")


def holiday_snapshot(holiday: Holiday) -> dict:
    return snapshot(holiday, SNAPSHOT_FIELDS)


def _normalise(values: dict) -> dict:
    if "name" in values:
        values["name"] = (values["name"] or "").strip()
        if not values["name"]:
            raise invalid("name", "This field may not be blank.")
    if "notes" in values:
        values["notes"] = values["notes"] or ""
    return values


def _save(action):
    try:
        with transaction.atomic():
            action()
    except IntegrityError as exc:
        raise (unique_conflict(exc, UNIQUE) or exc) from None


def _announce(holiday: Holiday, *days) -> None:
    recompute.for_office(holiday.office.uid if holiday.office_id else None, min(days), max(days), reason="holiday_changed")


@transaction.atomic
def create_holiday(*, user, data) -> Holiday:
    holiday = Holiday(**_normalise({name: data[name] for name in EDITABLE_FIELDS if name in data}))
    stamp_create(holiday, user)
    _save(holiday.save)
    record("hr.holiday_created", obj=holiday, actor=user, after=holiday_snapshot(holiday))
    _announce(holiday, holiday.date)
    bump_setup()
    return holiday


@transaction.atomic
def update_holiday(instance: Holiday, *, user, data, expected_version=None) -> Holiday:
    holiday = lock(Holiday, instance, expected_version, related=("office",))
    before = holiday_snapshot(holiday)
    values = _normalise({name: data[name] for name in EDITABLE_FIELDS if name in data})
    values = {name: value for name, value in values.items() if getattr(holiday, name) != value}
    if not values:
        return holiday
    old_office, old_date = holiday.office, holiday.date
    _save(lambda: holiday.versioned_update(user, **values))
    changed_before, changed_after = changes(before, holiday_snapshot(holiday))
    record("hr.holiday_updated", obj=holiday, actor=user, before=changed_before, after=changed_after)
    if set(values) - {"name", "notes"}:
        if old_office != holiday.office:
            recompute.for_office(old_office.uid if old_office else None, old_date, old_date, reason="holiday_changed")
            _announce(holiday, holiday.date)
        else:
            _announce(holiday, old_date, holiday.date)
    bump_setup()
    return holiday


@transaction.atomic
def delete_holiday(instance: Holiday, *, user, expected_version=None) -> None:
    holiday = lock(Holiday, instance, expected_version, related=("office",))
    holiday.soft_delete(user)
    record("hr.holiday_deleted", obj=holiday, actor=user, before=holiday_snapshot(holiday))
    if holiday.is_active:
        _announce(holiday, holiday.date)
    bump_setup()
