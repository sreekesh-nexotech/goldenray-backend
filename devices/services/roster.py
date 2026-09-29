"""What a device-user mapping currently is (eSSL ``device_user_state``, per device — A1).

Two questions, kept apart on purpose:

* ``device_state`` — is this PIN still enrolled on that terminal? ``ACTIVE_ON_DEVICE`` / ``MISSING_FROM_DEVICE`` as of
  the last **successful** read of the terminal's whole user table (the SUCCESS USERS sync log's ``users_present``),
  ``PENDING_SYNC`` / ``SYNC_FAILED`` while no read ever succeeded, ``DEVICE_INACTIVE`` once the terminal is deactivated
  here. A FAILED or PARTIAL read never concludes that anybody left (half a user table is no evidence of absence), and
  ADMS pushes never move the watermark (they are not whole-table reads).
* ``software_state`` — is the person still employed here? ``ACTIVE`` / ``DEACTIVATED`` / ``UNLINKED``.

Nothing here writes, contacts a terminal or deletes anything. Watermarks are read with two queries for any number of
devices (latest USERS log and latest SUCCESS USERS log per device), so lists stay free of N+1 queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.db.models import Q
from django.db.models.functions import Lower, Upper
from django.utils.dateparse import parse_datetime

from devices.models import Device, DeviceUser, SyncLog
from hr.models import Employee

ACTIVE_ON_DEVICE = "ACTIVE_ON_DEVICE"
MISSING_FROM_DEVICE = "MISSING_FROM_DEVICE"
PENDING_SYNC = "PENDING_SYNC"
SYNC_FAILED = "SYNC_FAILED"
DEVICE_INACTIVE = "DEVICE_INACTIVE"
DEVICE_STATES = (ACTIVE_ON_DEVICE, MISSING_FROM_DEVICE, PENDING_SYNC, SYNC_FAILED, DEVICE_INACTIVE)

SOFTWARE_ACTIVE = "ACTIVE"
SOFTWARE_DEACTIVATED = "DEACTIVATED"
SOFTWARE_UNLINKED = "UNLINKED"
SOFTWARE_STATES = (SOFTWARE_ACTIVE, SOFTWARE_DEACTIVATED, SOFTWARE_UNLINKED)

SYNC_OK = "OK"
SYNC_NEVER_RUN = "NEVER_RUN"
SYNC_LAST_FAILED = "LAST_FAILED"
SYNC_INCOMPLETE = "INCOMPLETE"
SYNC_STATES = (SYNC_OK, SYNC_NEVER_RUN, SYNC_LAST_FAILED, SYNC_INCOMPLETE)

PRESENCE_ON_DEVICE = "ON_DEVICE"
PRESENCE_OFF_ALL_DEVICES = "OFF_ALL_DEVICES"
PRESENCE_UNCONFIRMED = "UNCONFIRMED"
PRESENCE_NOT_LINKED = "NOT_LINKED"
PRESENCES = (PRESENCE_ON_DEVICE, PRESENCE_OFF_ALL_DEVICES, PRESENCE_UNCONFIRMED, PRESENCE_NOT_LINKED)

CATEGORY_NEW = "NEW_ON_DEVICE"
CATEGORY_MATCHED = "ALREADY_MATCHED"
CATEGORY_MISSING = "MISSING_FROM_DEVICE"
CATEGORY_UNKNOWN = "UNKNOWN"
CATEGORIES = (CATEGORY_NEW, CATEGORY_MATCHED, CATEGORY_MISSING, CATEGORY_UNKNOWN)

ACTION_LINK_EXISTING = "LINK_EXISTING"
ACTION_CREATE_EMPLOYEE = "CREATE_EMPLOYEE"
ACTION_IDENTIFY = "IDENTIFY"
ACTION_NONE = "NONE"
ACTIONS = (ACTION_LINK_EXISTING, ACTION_CREATE_EMPLOYEE, ACTION_IDENTIFY, ACTION_NONE)

DEVICE_DELETION_SUPPORTED = False
DEVICE_DELETION_NOTE = (
    "Device-side user deletion is not implemented. The office agent is read-only by contract (no delete_user/set_user) "
    "and the ADMS receiver never issues a command, so a user deactivated here stays enrolled on the terminal until "
    "someone removes the enrolment at the terminal."
)
_NO_EVIDENCE = {PENDING_SYNC, SYNC_FAILED, DEVICE_INACTIVE}


@dataclass(frozen=True)
class Watermark:
    confirmed_at: datetime | None
    present: frozenset[str] | None  # None: a legacy log without the list (fallback: compare timestamps)
    sync_state: str
    sync_error: str
    last_attempt_at: datetime | None


NO_WATERMARK = Watermark(None, None, SYNC_NEVER_RUN, "", None)


def _present(log: SyncLog) -> frozenset[str] | None:
    raw = (log.details or {}).get("users_present")
    return frozenset(str(pin) for pin in raw) if isinstance(raw, list) else None


def _stamp(log: SyncLog) -> datetime:
    raw = (log.details or {}).get("users_seen_at")
    parsed = parse_datetime(raw) if isinstance(raw, str) else None
    return parsed or log.started_at


def watermarks(device_ids) -> dict[int, Watermark]:
    """``{device_id: Watermark}`` for every id (two queries in total)."""
    ids = sorted({int(pk) for pk in device_ids if pk is not None})
    if not ids:
        return {}
    users = SyncLog.objects.filter(device_id__in=ids, sync_type=SyncLog.Type.USERS).order_by("device_id", "-started_at", "-id")
    latest = {log.device_id: log for log in users.distinct("device_id")}
    success = {log.device_id: log for log in users.filter(status=SyncLog.Status.SUCCESS).distinct("device_id")}
    result = {}
    for pk in ids:
        last, good = latest.get(pk), success.get(pk)
        if last is None:
            state, error = SYNC_NEVER_RUN, ""
        elif last.status == SyncLog.Status.FAILED:
            state, error = SYNC_LAST_FAILED, last.error_message
        elif last.status == SyncLog.Status.PARTIAL:
            state, error = SYNC_INCOMPLETE, last.error_message
        else:
            state, error = SYNC_OK, ""
        result[pk] = Watermark(_stamp(good) if good else None, _present(good) if good else None, state, error or "", last.started_at if last else None)
    return result


def device_state_of(row: DeviceUser, watermark: Watermark, device_active: bool = True) -> str:
    if not device_active:
        return DEVICE_INACTIVE
    if watermark.confirmed_at is None:
        return SYNC_FAILED if watermark.sync_state == SYNC_LAST_FAILED else PENDING_SYNC
    if watermark.present is not None:
        return ACTIVE_ON_DEVICE if row.pin in watermark.present else MISSING_FROM_DEVICE
    if row.last_seen_at is None or row.last_seen_at < watermark.confirmed_at:
        return MISSING_FROM_DEVICE
    return ACTIVE_ON_DEVICE


def employee_is_active(employee) -> bool:
    return employee is not None and employee.is_active and employee.deleted_at is None


def software_state_of(row: DeviceUser) -> str:
    if row.employee_id is None:
        return SOFTWARE_UNLINKED
    return SOFTWARE_ACTIVE if employee_is_active(row.employee) else SOFTWARE_DEACTIVATED


def state_for(row: DeviceUser, watermark: Watermark, device_active: bool) -> dict:
    device_state = device_state_of(row, watermark, device_active)
    software_state = software_state_of(row)
    return {
        "device_state": device_state,
        "software_state": software_state,
        "device_active": device_active,
        "sync_state": watermark.sync_state,
        "sync_error": watermark.sync_error,
        "device_confirmed_at": watermark.confirmed_at,
        "is_active_user": device_state == ACTIVE_ON_DEVICE and software_state != SOFTWARE_DEACTIVATED,
        # deactivated here, still enrolled there: work for a human (the platform cannot remove the enrolment)
        "needs_device_removal": software_state == SOFTWARE_DEACTIVATED and device_state == ACTIVE_ON_DEVICE,
    }


def describe(rows) -> dict[int, dict]:
    """State of every given mapping (rows carry ``device`` and ``employee`` via select_related)."""
    rows = list(rows)
    marks = watermarks(row.device_id for row in rows)
    return {row.pk: state_for(row, marks.get(row.device_id, NO_WATERMARK), bool(row.device.is_active and row.device.deleted_at is None)) for row in rows}


# --------------------------------------------------------------------------------------------------------------------
# Filters (a pageable queryset: the derived states become database conditions)
# --------------------------------------------------------------------------------------------------------------------
_FALSE = Q(pk__in=[])


def device_state_q(state: str, device_ids) -> Q:
    """A condition selecting the device users of ``device_ids`` whose ``device_state`` is ``state``."""
    ids = sorted({int(pk) for pk in device_ids})
    active = set(Device.objects.filter(pk__in=ids, is_active=True).values_list("pk", flat=True))
    marks = watermarks(ids)
    condition = _FALSE
    for pk in ids:
        if pk not in active:
            if state == DEVICE_INACTIVE:
                condition |= Q(device_id=pk)
            continue
        mark = marks.get(pk, NO_WATERMARK)
        if mark.confirmed_at is None:
            if state == (SYNC_FAILED if mark.sync_state == SYNC_LAST_FAILED else PENDING_SYNC):
                condition |= Q(device_id=pk)
            continue
        if mark.present is not None:
            here = Q(device_id=pk, pin__in=sorted(mark.present))
            gone = Q(device_id=pk) & ~Q(pin__in=sorted(mark.present))
        else:
            here = Q(device_id=pk, last_seen_at__gte=mark.confirmed_at)
            gone = Q(device_id=pk) & (Q(last_seen_at__lt=mark.confirmed_at) | Q(last_seen_at__isnull=True))
        if state == ACTIVE_ON_DEVICE:
            condition |= here
        elif state == MISSING_FROM_DEVICE:
            condition |= gone
    return condition


def software_state_q(state: str) -> Q:
    if state == SOFTWARE_UNLINKED:
        return Q(employee__isnull=True)
    if state == SOFTWARE_ACTIVE:
        return Q(employee__isnull=False, employee__is_active=True, employee__deleted_at__isnull=True)
    return Q(employee__isnull=False) & (Q(employee__is_active=False) | Q(employee__deleted_at__isnull=False))


# --------------------------------------------------------------------------------------------------------------------
# One person across every terminal
# --------------------------------------------------------------------------------------------------------------------
def mapping_rows(employee_ids):
    return DeviceUser.objects.filter(employee_id__in=list(employee_ids), device__deleted_at__isnull=True).select_related("device__office", "employee").order_by("device__name", "pin")


def employee_presence(employee_ids) -> dict[int, dict]:
    """``{employee_id: {presence, counts, devices_present_on}}`` for every id asked for (constant queries)."""
    ids = [int(pk) for pk in employee_ids]
    out = {pk: {"presence": PRESENCE_NOT_LINKED, "active_on_devices": 0, "missing_from_devices": 0, "unconfirmed_devices": 0, "total_mappings": 0, "devices_present_on": []} for pk in ids}
    if not ids:
        return out
    rows = list(mapping_rows(ids))
    states = describe(rows)
    for row in rows:
        entry = out[row.employee_id]
        state = states[row.pk]["device_state"]
        entry["total_mappings"] += 1
        if state == ACTIVE_ON_DEVICE:
            entry["active_on_devices"] += 1
            entry["devices_present_on"].append(row.device.name)
        elif state == MISSING_FROM_DEVICE:
            entry["missing_from_devices"] += 1
        else:
            entry["unconfirmed_devices"] += 1
    for entry in out.values():
        if entry["total_mappings"] == 0:
            entry["presence"] = PRESENCE_NOT_LINKED
        elif entry["active_on_devices"]:
            entry["presence"] = PRESENCE_ON_DEVICE
        elif entry["unconfirmed_devices"]:
            entry["presence"] = PRESENCE_UNCONFIRMED
        else:
            entry["presence"] = PRESENCE_OFF_ALL_DEVICES
    return out


# --------------------------------------------------------------------------------------------------------------------
# One terminal: state summary (user-reconciliation) and operator categories (employee-reconciliation)
# --------------------------------------------------------------------------------------------------------------------
def _device_rows(device: Device):
    return list(DeviceUser.objects.filter(device=device).select_related("device", "employee__office").order_by("pin", "id"))


def _employee_ref(employee) -> dict | None:
    if employee is None:
        return None
    return {"uid": employee.uid, "code": employee.code, "full_name": employee.full_name, "is_active": employee_is_active(employee)}


def reconcile(device: Device) -> dict:
    """Stored mappings of one device against the last read of its user table. Read-only and idempotent."""
    rows = _device_rows(device)
    states = describe(rows)
    mark = watermarks([device.pk]).get(device.pk, NO_WATERMARK)
    counts = dict.fromkeys(DEVICE_STATES, 0)
    deactivated = unlinked = 0
    removals = []
    for row in rows:
        state = states[row.pk]
        counts[state["device_state"]] += 1
        deactivated += state["software_state"] == SOFTWARE_DEACTIVATED
        unlinked += state["software_state"] == SOFTWARE_UNLINKED
        if state["needs_device_removal"]:
            removals.append({"device_user_uid": row.uid, "pin": row.pin, "name": row.name, "employee": _employee_ref(row.employee)})
    return {
        "users_last_confirmed_at": mark.confirmed_at,
        "sync_state": mark.sync_state,
        "sync_error": mark.sync_error,
        "device_is_active": device.is_active,
        "total_mappings": len(rows),
        "active_on_device": counts[ACTIVE_ON_DEVICE],
        "missing_from_device": counts[MISSING_FROM_DEVICE],
        "pending_sync": counts[PENDING_SYNC],
        "sync_failed": counts[SYNC_FAILED],
        "device_inactive": counts[DEVICE_INACTIVE],
        "deactivated_in_software": deactivated,
        "unlinked": unlinked,
        "pending_device_removals": removals,
        "device_deletion_supported": DEVICE_DELETION_SUPPORTED,
        "device_deletion_note": DEVICE_DELETION_NOTE,
    }


def employees_by_code(pins) -> dict[str, Employee]:
    """Live employees whose code equals a PIN (case-insensitive, like the code's own uniqueness), keyed by upper(PIN)."""
    keys = sorted({str(pin).strip().upper() for pin in pins if str(pin).strip()})
    if not keys:
        return {}
    return {employee.code.upper(): employee for employee in Employee.objects.annotate(code_key=Upper("code")).filter(code_key__in=keys).select_related("office")}


def name_candidates(names) -> dict[str, list[Employee]]:
    """Live employees whose name equals a terminal name (case-insensitive), at most 5 per name — suggestions only."""
    keys = sorted({str(name).strip().lower() for name in names if str(name or "").strip()})
    if not keys:
        return {}
    found: dict[str, list[Employee]] = {}
    for employee in Employee.objects.annotate(name_key=Lower("full_name")).filter(name_key__in=keys).order_by("id"):
        bucket = found.setdefault(employee.full_name.strip().lower(), [])
        if len(bucket) < 5:
            bucket.append(employee)
    return found


def row_view(row: DeviceUser, state: dict, by_code: dict, by_name: dict) -> dict:
    """One line of the reconciliation table, with the action it allows (a proposal; nothing acts on it)."""
    device_state = state["device_state"]
    suggested, candidates = None, []
    if device_state in (MISSING_FROM_DEVICE, DEVICE_INACTIVE):
        category, action = CATEGORY_MISSING, ACTION_NONE
        if device_state == DEVICE_INACTIVE:
            note = "This device is deactivated here, so no current read stands behind this mapping. Nothing was deleted."
        else:
            note = "The last successful read of this terminal did not list this user. Nothing was deleted: the employee, the mapping and every punch are kept."
    elif row.employee_id is not None:
        category, action, note = CATEGORY_MATCHED, ACTION_NONE, "Linked to an employee and present on the device."
    else:
        suggested = by_code.get(row.pin.strip().upper())
        if suggested is not None:
            category, action = CATEGORY_NEW, ACTION_LINK_EXISTING
            note = f"On the device, not linked here. The PIN matches employee code {suggested.code}."
        else:
            category, action = CATEGORY_UNKNOWN, ACTION_IDENTIFY
            candidates = by_name.get((row.name or "").strip().lower(), [])
            note = "On the device, and its PIN matches no employee code. Somebody has to say who this is."
    ambiguous = len(candidates) > 1
    return {
        "device_user_uid": row.uid,
        "pin": row.pin,
        "name": row.name,
        "category": category,
        "device_state": device_state,
        "software_state": state["software_state"],
        "is_active_user": state["is_active_user"],
        "employee": _employee_ref(row.employee),
        "suggested_employee": _employee_ref(suggested),
        "candidate_employees": [_employee_ref(candidate) for candidate in candidates],
        "ambiguous": ambiguous,
        "available_action": ACTION_IDENTIFY if ambiguous else action,
        "requires_confirmation": action != ACTION_NONE,
        "first_seen_at": row.first_seen_at,
        "last_seen_on_device_at": row.last_seen_at,
        "status_note": note,
    }


def categorise(device: Device) -> dict:
    """Every mapping on ONE device in the four operator categories (read-only; other terminals are never consulted)."""
    rows = _device_rows(device)
    states = describe(rows)
    unlinked = [row for row in rows if row.employee_id is None]
    by_code = employees_by_code(row.pin for row in unlinked)
    by_name = name_candidates(row.name for row in unlinked)
    views = [row_view(row, states[row.pk], by_code, by_name) for row in rows]
    summary = {
        "new": sum(view["category"] == CATEGORY_NEW for view in views),
        "matched": sum(view["category"] == CATEGORY_MATCHED for view in views),
        "missing": sum(view["category"] == CATEGORY_MISSING for view in views),
        "unknown": sum(view["category"] == CATEGORY_UNKNOWN for view in views),
        "total": len(views),
    }
    return {"summary": summary, "rows": views}
