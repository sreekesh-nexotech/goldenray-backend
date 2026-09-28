"""Outbox handlers owned by accounts.

``hr.employee_deactivated`` (PLAN §3.5) → deactivate the linked user and end their sessions. Contract for the
producer (hr): payload ``{"employee_uid": "<uuid>", "user_uid": "<uuid>" | null}``; a null ``user_uid`` (employee
without a Studio login) is a no-op. The handler is idempotent (re-delivery finds the user already inactive).

The handler acts as SYSTEM and cannot re-check who linked the employee to the account: the hr package must guard
``hr/employees/…/link-user/`` with ``accounts.services.users.ensure_can_manage_user(actor, target)``, otherwise
holding ``employees`` alone would let someone deactivate any account (a Super Admin's included) by linking it to an
employee and deactivating that employee.
"""

from accounts.services.users import deactivate_for_system
from core.outbox import Event, handler


@handler("hr.employee_deactivated")
def deactivate_linked_user(event: Event) -> None:
    user_uid = event.payload.get("user_uid")
    if not user_uid:
        return
    employee_uid = event.payload.get("employee_uid") or ""
    deactivate_for_system(user_uid, reason="employee_deactivated", note=f"HR deactivated employee {employee_uid}".strip())
