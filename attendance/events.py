"""Outbox handlers owned by attendance (A8): new punches and changed HR inputs queue a debounced recompute.

Both run in the outbox drainer (a Celery worker) — never inside a terminal's, an agent's or a person's request — and
only *record* what to recompute (``attendance_recompute_request``); ``attendance.tasks.run_due_recomputes`` does the
work once per debounce window. Idempotent: a re-delivered event queues a recompute that changes nothing.

* ``attendance.punches_ingested`` (devices): the linked employees, from the day before the first punch date (overnight
  shifts, zones) to the day after the last.
* ``hr.attendance_inputs_changed`` (hr, devices): ``employee_uids`` or ``office_uid`` (null = everyone) over
  ``date_from … date_to``. Dates ≥ today are never stored (A7), so a range reaching into the future costs nothing.
"""

from __future__ import annotations

from datetime import date, timedelta

from attendance.services.recompute import request_recompute
from core.outbox import Event, handler


def _date(value) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _employee_pks(uids) -> list[int]:
    from hr.models import Employee

    return list(Employee.all_objects.filter(uid__in=[str(uid) for uid in uids or []]).values_list("pk", flat=True))


@handler("attendance.punches_ingested")
def punches_ingested(event: Event) -> None:
    payload = event.payload
    first, last = _date(payload.get("date_from")), _date(payload.get("date_to"))
    employee_ids = _employee_pks(payload.get("employee_uids"))
    if first is None or last is None or not employee_ids:
        return  # unmapped PINs only: nothing to compute until a link exists (the link change recomputes)
    request_recompute(employee_ids=employee_ids, date_from=first - timedelta(days=1), date_to=last + timedelta(days=1), reason=f"punches_{str(payload.get('source') or 'ingested').lower()}")


@handler("hr.attendance_inputs_changed")
def attendance_inputs_changed(event: Event) -> None:
    from hr.models import Office

    payload = event.payload
    first, last = _date(payload.get("date_from")), _date(payload.get("date_to"))
    if first is None or last is None:
        return
    reason = str(payload.get("reason") or "hr_inputs_changed")
    if "employee_uids" in payload:
        employee_ids = _employee_pks(payload.get("employee_uids"))
        if employee_ids:
            request_recompute(employee_ids=employee_ids, date_from=first, date_to=last, reason=reason)
        return
    office_uid = payload.get("office_uid")
    if office_uid is None:
        request_recompute(all_employees=True, date_from=first, date_to=last, reason=reason)
        return
    office_id = Office.all_objects.filter(uid=str(office_uid)).values_list("pk", flat=True).first()
    if office_id is not None:
        request_recompute(office_id=office_id, date_from=first, date_to=last, reason=reason)
