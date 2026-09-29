"""Staff-triggered recomputes (``attendance.manage``): ``process/``, ``process-all/``, ``recalculate/``.

Recomputing is safe to repeat: raw punches are never touched, corrected days are left alone (A12) and nothing is
stored for today or later (A7). ``process/`` runs in the request for a bounded window (at most
:data:`MAX_PROCESS_DAYS` days); ``process-all/`` spans every stored punch and is queued (the debounce queue's worker
runs it); ``recalculate/`` is the eSSL "Recalculate Attendance" button — eSSL dialled every terminal first, the
platform never dials one (PLAN §1.4), so it recomputes the window and reports, per terminal, how its punches arrive
and when the last ones came.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.db import transaction
from django.db.models import Max, Min

from attendance.models import RawPunch
from attendance.services import recompute
from attendance.services.common import now, today_for, validate_range
from audit.services import record
from core.errors import DomainError
from devices.models import Device
from devices.services import health
from hr.models import Employee

MAX_PROCESS_DAYS = 93


def _employee_ids(employee_uids) -> list[int] | None:
    if not employee_uids:
        return None
    uids = sorted({str(uid) for uid in employee_uids})
    found = dict(Employee.objects.filter(uid__in=uids).values_list("uid", "pk"))
    missing = [uid for uid in uids if uid not in {str(key) for key in found}]
    if missing:
        raise DomainError("validation_error", "Unknown employees.", errors={"employee_uids": [f"Not found: {', '.join(missing)}."]})
    return sorted(found.values())


def process(*, user, date_from: date, date_to: date, employee_uids=None) -> dict:
    validate_range(date_from, date_to, max_days=MAX_PROCESS_DAYS)
    return recompute.recompute(date_from=date_from, date_to=date_to, employee_ids=_employee_ids(employee_uids), reason="process", user=user)


@transaction.atomic
def process_all(*, user) -> dict:
    """Queue the recompute of every date that has punches (at most the queue's range limit, the latest dates kept)."""
    bounds = RawPunch.objects.aggregate(first=Min("device_time"), last=Max("device_time"))
    if bounds["first"] is None:
        return {"queued": False, "date_from": None, "date_to": None, "requests": 0}
    date_from = bounds["first"].date() - timedelta(days=1)
    date_to = min(bounds["last"].date() + timedelta(days=1), today_for(None))
    date_to = max(date_from, date_to)
    requests = recompute.request_recompute(all_employees=True, date_from=date_from, date_to=date_to, reason="process_all", delay_seconds=0)
    # the queued recompute runs (and is audited) as the system: this row says who asked for it
    record("attendance.process_all_requested", object_type="attendance.attendanceday", actor=user, after={"date_from": date_from.isoformat(), "date_to": date_to.isoformat()})
    return {"queued": True, "date_from": date_from, "date_to": date_to, "requests": requests}


def recalculate(*, user, date_from: date | None = None, date_to: date | None = None, employee_uids=None) -> dict:
    yesterday = today_for(None) - timedelta(days=1)
    date_from = date_from or yesterday
    date_to = date_to or max(date_from, yesterday)
    validate_range(date_from, date_to, max_days=MAX_PROCESS_DAYS)
    result = recompute.recompute(date_from=date_from, date_to=date_to, employee_ids=_employee_ids(employee_uids), reason="recalculate", user=user)
    at = now()
    devices = []
    for device in Device.objects.filter(is_active=True).select_related("office", "agent").order_by("name"):
        block = health.device_block(device, at)
        transport = "ADMS" if device.adms_enabled else ("AGENT" if device.agent_id else "NONE")
        notes = {
            "ADMS": "The terminal pushes its punches; they were recalculated as delivered.",
            "AGENT": "The office agent delivers the punches it reads; the platform never dials a terminal.",
            "NONE": "No agent or push is configured: nothing arrives from this terminal.",
        }
        devices.append(
            {
                "device": device,
                "transport": transport,
                "connection_state": block.get("connection_state"),
                "last_sync_at": device.last_sync_at,
                "last_punch_at": device.last_punch_at,
                "note": notes[transport],
            }
        )
    return {"result": result, "devices": devices, "summary": {"devices": len(devices), "without_transport": sum(1 for item in devices if item["transport"] == "NONE")}}
