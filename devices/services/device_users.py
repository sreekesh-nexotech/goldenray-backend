"""``devices/device-users/`` — who each terminal user is, one device row at a time (A1).

* ``link/`` sets or clears the employee of **this device row only** (eSSL ``map-pin`` linked every row with the PIN on
  every terminal, spec §I.8). ``map-pin/`` names the device explicitly and creates the row when the PIN has only ever
  been seen in punches. ``auto-link/`` links unlinked rows of active devices whose PIN equals a live employee's code
  (case-insensitive, like the code's uniqueness) and never overwrites a link.
* ``resolve/`` acts on one row of a reconciliation with ``confirm: true`` (400 ``confirmation_required`` otherwise):
  ``LINK_EXISTING`` (409 ``device_user_already_linked`` when the row belongs to someone else) or ``CREATE_EMPLOYEE``
  (needs ``employees.create`` as well; code defaults to the PIN, name to the terminal's name, office to the device's).
* Every link change re-computes the recent attendance of the employees involved: ``hr.attendance_inputs_changed``
  with ``reason="device_link_changed"`` (A8), through :func:`hr.services.recompute.for_employees`.
* ``unmapped/`` lists the PINs of one device that produced punches (from the punch store) but have no linked row there.
"""

from __future__ import annotations

from django.db import transaction

from accounts.services.authz import can
from audit.services import record
from core.errors import Conflict, DomainError, PermissionDenied
from core.services import stamp_create
from devices.models import Device, DeviceUser
from devices.services import punch_sink, roster
from devices.services.common import NS_DEVICE_USERS, bump_devices, lock
from hr.services import employees as hr_employees
from hr.services import recompute


def device_users_queryset():
    return DeviceUser.objects.select_related("device__office", "employee__office").order_by("device__name", "pin", "id")


def _recompute(*employees, reason: str = "device_link_changed") -> None:
    uids = [employee.uid for employee in employees if employee is not None]
    if uids:
        recompute.for_employees(uids, *recompute.recent_range(), reason=reason)


def _link(row: DeviceUser, employee, *, user, action: str, note: str = "") -> DeviceUser:
    previous = row.employee
    if row.employee_id == (employee.pk if employee else None):
        return row
    row.versioned_update(user, employee=employee)
    record(
        action,
        obj=row,
        actor=user,
        before={"employee": str(previous.uid) if previous else None},
        after={"employee": str(employee.uid) if employee else None, "device": str(row.device.uid), "pin": row.pin},
        note=note,
    )
    _recompute(previous, employee)
    bump_devices(NS_DEVICE_USERS)
    return row


@transaction.atomic
def link(instance: DeviceUser, *, user, employee, expected_version=None) -> DeviceUser:
    row = lock(DeviceUser, instance, expected_version, related=("device", "employee"))
    return _link(row, employee, user=user, action="devices.device_user_linked" if employee else "devices.device_user_unlinked")


@transaction.atomic
def auto_link(*, user, device: Device | None = None) -> dict:
    candidates = DeviceUser.objects.select_for_update(of=("self",)).select_related("device").filter(employee__isnull=True, device__is_active=True, device__deleted_at__isnull=True)
    if device is not None:
        candidates = candidates.filter(device=device)
    rows = list(candidates.order_by("device_id", "pin"))
    by_code = roster.employees_by_code(row.pin for row in rows)
    linked, unlinked = 0, []
    for row in rows:
        employee = by_code.get(row.pin.strip().upper())
        if employee is None:
            unlinked.append(row.pin)
            continue
        _link(row, employee, user=user, action="devices.device_user_linked", note="auto-link: PIN equals the employee code")
        linked += 1
    return {"linked": linked, "still_unlinked": len(unlinked), "unlinked_pins": sorted(set(unlinked))}


@transaction.atomic
def resolve(instance: DeviceUser, *, user, action: str, confirm: bool, employee=None, employee_code: str = "", full_name: str = "", office=None, shift=None, expected_version=None) -> DeviceUser:
    if not confirm:
        raise DomainError("confirmation_required", "Confirmation required: nothing is created or linked without confirm=true.", errors={"confirm": ["Must be true."]})
    row = lock(DeviceUser, instance, expected_version, related=("device__office", "employee"))
    if action == roster.ACTION_LINK_EXISTING:
        if employee is None:
            raise DomainError("validation_error", "Invalid input.", errors={"employee_uid": ["Required to link."]})
        if row.employee_id is not None and row.employee_id != employee.pk:
            raise Conflict("device_user_already_linked", f"This terminal user is already linked to {row.employee.full_name}. Unlink it first if that link is wrong.")
        return _link(row, employee, user=user, action="devices.device_user_linked", note="resolve: LINK_EXISTING")
    if action == roster.ACTION_CREATE_EMPLOYEE:
        if row.employee_id is not None:
            raise Conflict("device_user_already_linked", f"This terminal user is already linked to {row.employee.full_name}.")
        if not can(user, "employees", "create"):
            raise PermissionDenied("permission_denied", "Creating an employee needs employees.create.")
        code = (employee_code or row.pin).strip()
        name = (full_name or row.name).strip()
        if not name:
            raise DomainError("validation_error", "Invalid input.", errors={"full_name": ["Required: the terminal gave this user no name."]})
        data = {"code": code, "full_name": name, "office": office if office is not None else row.device.office}
        if shift is not None:
            data["shift"] = shift
        created = hr_employees.create_employee(user=user, data=data)
        return _link(row, created, user=user, action="devices.device_user_linked", note="resolve: CREATE_EMPLOYEE")
    raise DomainError("validation_error", "Invalid input.", errors={"action": [f"Use {roster.ACTION_LINK_EXISTING} or {roster.ACTION_CREATE_EMPLOYEE}."]})


@transaction.atomic
def map_pin(*, user, device: Device, pin: str, employee) -> tuple[DeviceUser, bool]:
    """Attribute ``pin`` on ``device`` to ``employee``; creates the row when the terminal never reported it."""
    pin = (pin or "").strip()
    if not pin:
        raise DomainError("validation_error", "Invalid input.", errors={"pin": ["This field may not be blank."]})
    row = DeviceUser.objects.select_for_update(of=("self",)).select_related("device", "employee").filter(device=device, pin=pin).first()
    created = row is None
    if created:
        row = DeviceUser(device=device, pin=pin, employee=None, raw_payload={"created_by": "map-pin"})
        stamp_create(row, user)
        row.save()
        record("devices.device_user_created", obj=row, actor=user, after={"device": str(device.uid), "pin": pin})
    return _link(row, employee, user=user, action="devices.device_user_linked", note="map-pin"), created


def unmapped(device: Device) -> list[dict]:
    """PINs of ``device`` with punches but no linked row there (the per-device work queue of A1)."""
    activity = punch_sink.get().pin_activity(device)
    rows = {row.pin: row for row in DeviceUser.objects.filter(device=device)}
    by_code = roster.employees_by_code(item["pin"] for item in activity)
    out = []
    for item in activity:
        pin = str(item["pin"])
        row = rows.get(pin)
        if row is not None and row.employee_id is not None:
            continue
        suggested = by_code.get(pin.strip().upper())
        out.append(
            {
                "pin": pin,
                "name": row.name if row is not None else "",
                "punch_count": int(item.get("punch_count") or 0),
                "first_punch_at": item.get("first_punch_at"),
                "last_punch_at": item.get("last_punch_at"),
                "has_device_user_row": row is not None,
                "device_user_uid": row.uid if row is not None else None,
                "suggested_employee": {"uid": suggested.uid, "code": suggested.code, "full_name": suggested.full_name, "is_active": suggested.is_active} if suggested else None,
            }
        )
    out.sort(key=lambda entry: (-entry["punch_count"], entry["pin"]))
    return out
