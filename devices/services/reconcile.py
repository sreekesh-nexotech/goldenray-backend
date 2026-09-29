"""Estate-wide reconciliation of the employee list against every terminal (``POST hr/employees/reconcile-devices/``).

The Employees-side counterpart of the per-device categories (eSSL ``employee_reconcile``), with the same evidence:

* ``added`` — on a terminal (confirmed by its last successful read), PIN matching no employee code, the terminal gave
  a name: an employee would be created;
* ``updated`` — PIN matching a live employee's code, row not linked: the link would be made;
* ``unchanged`` — linked and present; ``unknown`` — matching nothing and nameless (a human must say who it is);
* ``removed`` — an active employee every successful read has stopped listing: they would be deactivated here;
* ``unconfirmed`` — some of their terminals were never read, failed, or are deactivated: nothing is concluded;
* ``unlinked`` — an active employee with no device identity at all.

Preview writes nothing. ``apply`` (with ``confirm``, checked by hr) links, creates (needs ``employees.create``) and
deactivates (needs ``employees.archive``; hr's own guards apply — nobody retires their own record or one whose login
they could not manage) exactly what the plan names; entries the caller may not act on are listed in ``skipped``.
Nothing is deleted and nothing is written to a terminal. ``read_devices`` cannot dial a terminal from the server: it
asks each active agent-delivered device's agent to re-read its user table on its next cycle and says so per device.
"""

from __future__ import annotations

from django.db import transaction

from accounts.services.authz import can
from core import scopes
from core.errors import DomainError, PermissionDenied
from devices.models import Device, DeviceUser
from devices.services import device_users, devices, health, roster
from hr.models import Employee
from hr.services import employees as hr_employees

ADDED, UPDATED, REMOVED, UNCHANGED, UNKNOWN, UNLINKED, UNCONFIRMED = "added", "updated", "removed", "unchanged", "unknown", "unlinked", "unconfirmed"
BUCKETS = (ADDED, UPDATED, REMOVED, UNCHANGED, UNKNOWN, UNLINKED, UNCONFIRMED)


def _device_ref(device: Device) -> dict:
    return {"uid": device.uid, "name": device.name, "serial_number": device.serial_number or device.expected_serial, "office": device.office.name if device.office_id else None}


def request_reads(user) -> list[dict]:
    """Ask the agents of every active device to re-read the user tables (one request per agent-delivered device)."""
    report = []
    for device in Device.objects.filter(is_active=True).select_related("office", "agent").order_by("name"):
        outcome = devices.request_user_read(device, user=user)
        report.append({"device": _device_ref(device), "transport": outcome["transport"], "read_requested": outcome["requested"], "note": outcome["note"]})
    return report


def plan() -> dict:
    """What the stored facts say should happen. Derives, decides, writes nothing."""
    rows = list(DeviceUser.objects.filter(device__is_active=True, device__deleted_at__isnull=True).select_related("device__office", "employee").order_by("device__name", "pin"))
    states = roster.describe(rows)
    by_code = roster.employees_by_code(row.pin for row in rows if row.employee_id is None)
    employees = list(Employee.objects.select_related("office").order_by("code"))
    presence = roster.employee_presence(employee.pk for employee in employees)
    buckets: dict[str, list[dict]] = {name: [] for name in BUCKETS}
    for row in rows:
        if states[row.pk]["device_state"] != roster.ACTIVE_ON_DEVICE:
            continue  # not confirmed on the terminal: judged from the employee side below
        entry = {"device_user_uid": row.uid, "device": _device_ref(row.device), "pin": row.pin, "name": row.name, "employee": None}
        if row.employee_id is not None:
            entry.update(employee={"uid": row.employee.uid, "code": row.employee.code, "full_name": row.employee.full_name}, reason="Already linked and present on the device.")
            buckets[UNCHANGED].append(entry)
            continue
        match = by_code.get(row.pin.strip().upper())
        if match is not None:
            entry.update(employee={"uid": match.uid, "code": match.code, "full_name": match.full_name}, reason=f"The PIN matches employee code {match.code}; only the link is missing.")
            buckets[UPDATED].append(entry)
        elif (row.name or "").strip():
            entry.update(
                employee={"uid": None, "code": row.pin, "full_name": row.name.strip()},
                reason=f"On the device and matching no employee code: an employee would be created as {row.pin} — {row.name.strip()}.",
            )
            buckets[ADDED].append(entry)
        else:
            entry["reason"] = "On the device, matching no employee code, and the terminal gave no name: somebody has to say who this is."
            buckets[UNKNOWN].append(entry)
    for employee in employees:
        info = presence[employee.pk]
        if not employee.is_active:
            continue
        entry = {
            "employee": {"uid": employee.uid, "code": employee.code, "full_name": employee.full_name},
            "office": employee.office.name if employee.office_id else None,
            "presence": info["presence"],
            "total_mappings": info["total_mappings"],
            "active_on_devices": info["active_on_devices"],
        }
        if info["presence"] == roster.PRESENCE_OFF_ALL_DEVICES:
            entry["reason"] = f"{info['missing_from_devices']} enrolment(s), none still listed by a successful read: the employee would be deactivated here (history and mappings are kept)."
            buckets[REMOVED].append(entry)
        elif info["presence"] == roster.PRESENCE_UNCONFIRMED:
            entry["reason"] = "At least one of this employee's terminals was never read, could not be read, or is deactivated here: nothing is concluded."
            buckets[UNCONFIRMED].append(entry)
        elif info["presence"] == roster.PRESENCE_NOT_LINKED:
            entry["reason"] = "No device identity is linked to this employee, so no terminal has anything to say about them."
            buckets[UNLINKED].append(entry)
    summary = {name: len(buckets[name]) for name in BUCKETS}
    summary.update(total_device_users=len(rows), total_employees=len(employees))
    return {"summary": summary, **buckets}


def _apply(user, current: dict) -> dict:
    created = linked = deactivated = 0
    skipped = []
    for entry in current[UPDATED]:
        row = DeviceUser.objects.select_related("device").filter(uid=entry["device_user_uid"], employee__isnull=True).first()
        employee = Employee.objects.filter(uid=entry["employee"]["uid"]).first()
        if row is None or employee is None:
            continue
        device_users.link(row, user=user, employee=employee)
        linked += 1
    may_create = can(user, "employees", "create")
    for entry in current[ADDED]:
        row = DeviceUser.objects.select_related("device__office").filter(uid=entry["device_user_uid"], employee__isnull=True).first()
        if row is None:
            continue
        # re-read: an earlier entry may have created this code for the same PIN on another terminal (one employee, two links)
        employee = roster.employees_by_code([row.pin]).get(row.pin.strip().upper())
        if employee is None:
            if not may_create:
                skipped.append({"bucket": ADDED, "device_user_uid": row.uid, "reason": "creating employees needs employees.create"})
                continue
            try:
                employee = hr_employees.create_employee(user=user, data={"code": row.pin, "full_name": row.name.strip(), "office": row.device.office})
            except DomainError as exc:
                skipped.append({"bucket": ADDED, "device_user_uid": row.uid, "reason": exc.message})
                continue
            created += 1
        else:
            linked += 1
        device_users.link(row, user=user, employee=employee)
    may_archive = can(user, "employees", "archive")
    for entry in current[REMOVED]:
        employee = Employee.objects.select_related("user__role").filter(uid=entry["employee"]["uid"], is_active=True).first()
        if employee is None:
            continue
        if not may_archive:
            skipped.append({"bucket": REMOVED, "employee_uid": employee.uid, "reason": "deactivating employees needs employees.archive"})
            continue
        try:
            hr_employees.deactivate_employee(employee, user=user, note="device reconciliation: listed by no terminal's last successful read")
        except DomainError as exc:
            skipped.append({"bucket": REMOVED, "employee_uid": employee.uid, "reason": exc.message})
            continue
        deactivated += 1
    return {"created": created, "linked": linked, "deactivated": deactivated, "skipped": skipped}


def reconcile_employees(*, user, read_devices: bool = True, apply: bool = False) -> dict:
    """The provider behind ``hr.registries.device_reconciler`` (hr checks ``confirm`` for ``apply``).

    The reconciliation spans every terminal and every employee, so it needs the ``employees`` record scope ``all``: an
    office- or self-scoped caller would otherwise see, link and deactivate people outside their scope (403).
    """
    if scopes.resolve_scope(user, "employees") != scopes.ALL:
        raise PermissionDenied("permission_denied", "The device reconciliation covers every office and terminal: it needs the employees scope 'all'.")
    reads = request_reads(user) if read_devices else []
    current = plan()
    changes = None
    if apply:
        with transaction.atomic():
            changes = _apply(user, current)
        current = plan()  # the buckets describe what now exists, never an intention
    return {
        "read_devices": read_devices,
        "applied": apply,
        "changes": changes,
        "devices": reads,
        "devices_read_requested": sum(1 for item in reads if item["read_requested"]),
        "devices_not_readable": sum(1 for item in reads if item["transport"] != health.AGENT_DELIVERED),
        "device_deletion_supported": roster.DEVICE_DELETION_SUPPORTED,
        "device_deletion_note": roster.DEVICE_DELETION_NOTE,
        **current,
    }
