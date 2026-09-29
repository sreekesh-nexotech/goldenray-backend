"""Employee ↔ Studio login (``link-user/``, ``unlink-user/``) and the employee photo (``photo/``).

Linking (``employees.edit``; DV-1 — the link lives on ``hr_employee.user``):

* either ``user_uid`` (an existing account) or ``email`` (a new account: Staff role, no password, a 72-hour
  invitation link, exactly like ``users/`` creates accounts);
* guards: the employee is active (409 ``employee_inactive``) and not linked to someone else
  (409 ``employee_already_linked``); the account is live (404 ``user_not_found``), not linked to another employee
  (409 ``user_already_linked``) and one the caller could manage in ``users/`` (403 ``user_exceeds_own_grants`` /
  ``super_admin_required``) — required by the accounts ``hr.employee_deactivated`` handler, which acts as SYSTEM;
* the Staff role (PLAN §3.2 "assigned automatically when an employee is linked to a user") replaces the account's
  role only when that role grants nothing beyond Staff — so a Sales Executive or an HR officer who is also an
  employee keeps their role (they already hold more), and nobody changes their own role;
* an inactive account whose role is Staff (a login retired by ``unlink-user/``) is reactivated; any other inactive
  account is refused (409 ``user_inactive``).

Unlinking removes the Staff access: a login whose role is Staff exists only for the employee link, so it is
deactivated (eSSL ``revoke-login`` behaviour; ``User.role`` cannot be empty); a login with another role keeps it.
Nobody unlinks their own login (that would let them approve their own leave).

Photo: a PRIVATE ``PHOTO`` asset in the reserved folder ``hr/employees`` (hidden from the media library); the bytes
are stored before the transaction opens; the replaced or removed asset is deleted.
"""

from __future__ import annotations

from django.db import transaction

from accounts.models import Role, User
from accounts.services import authz
from accounts.services import users as account_users
from accounts.services.seeds import seed_roles
from audit.services import record
from core.errors import Conflict, DomainError, NotFound, PermissionDenied
from flarize.cache_utils import bump
from hr.models import Employee
from hr.services import scopes as hr_scopes
from hr.services.common import NS_EMPLOYEES, lock
from media.models import MediaAsset
from media.services import assets as media_assets

STAFF_SLUG = "staff"
PHOTO_FOLDER = "hr/employees"


def staff_role() -> Role:
    role = Role.objects.filter(slug=STAFF_SLUG, is_system=True).first()
    if role is None:  # the seeded roles are created at deploy; get-or-create keeps a fresh database usable
        seed_roles()
        role = Role.objects.get(slug=STAFF_SLUG, is_system=True)
    return role


def within_staff_grants(role: Role) -> bool:
    """Whether ``role`` grants nothing the Staff role does not (so replacing it by Staff takes nothing away)."""
    return not authz.missing_grants(authz.grants_for_role(staff_role()), role.permissions, role.scopes)


def _split_name(full_name: str) -> tuple[str, str]:
    first, _, last = (full_name or "").strip().partition(" ")
    return first[:150], last.strip()[:150]


def _new_account(actor, employee: Employee, data: dict) -> User:
    first, last = _split_name(employee.full_name)
    return account_users.create_user(
        user=actor,
        data={"email": data["email"], "role": staff_role(), "first_name": data.get("first_name") or first, "last_name": data.get("last_name") or last},
    )


def _existing_account(actor, employee: Employee, user_uid) -> tuple[User, dict]:
    target = User.objects.select_for_update(of=("self",)).select_related("role").filter(uid=user_uid).first()
    if target is None:
        raise NotFound("user_not_found", "The account does not exist.")
    account_users.ensure_can_manage_user(actor, target)
    other = Employee.objects.filter(user=target).exclude(pk=employee.pk).first()
    if other is not None:
        raise Conflict("user_already_linked", f"This login already belongs to employee {other.code}.", errors={"user_uid": ["Linked to another employee."]})
    outcome = {"staff_role_assigned": False, "account_reactivated": False}
    staff = staff_role()
    if target.role_id != staff.pk and within_staff_grants(target.role):
        if authz.is_self(actor, target):
            raise PermissionDenied("own_role_change", "You cannot change your own role.")
        account_users.ensure_can_assign_role(actor, staff)
        before = {"role": str(target.role.uid)}
        target.versioned_update(actor, role=staff)
        authz.invalidate_user(target.uid)
        authz.forget_grants(target)
        record("hr.staff_role_assigned", obj=target, actor=actor, before=before, after={"role": str(staff.uid)}, note=f"linked to employee {employee.code}")
        outcome["staff_role_assigned"] = True
    if not target.is_active:
        if target.role_id != staff.pk:
            raise Conflict("user_inactive", "This login is deactivated; reactivate it in Users first.", errors={"user_uid": ["Deactivated."]})
        account_users.reactivate_user(target, user=actor, note=f"login re-linked to employee {employee.code}")
        target.refresh_from_db()
        outcome["account_reactivated"] = True
    return target, outcome


@transaction.atomic
def link_user(instance: Employee, *, user, data, expected_version=None) -> tuple[Employee, dict]:
    employee = lock(Employee, instance, expected_version, related=("user",))
    if not employee.is_active:
        raise Conflict("employee_inactive", "Activate the employee before linking a login.")
    wanted_uid = data.get("user_uid")
    if employee.user_id is not None and (wanted_uid is None or employee.user.uid != wanted_uid):
        raise Conflict("employee_already_linked", "This employee already has a login; unlink it first.", errors={"user_uid": ["Already linked."]})
    if wanted_uid is not None:
        target, outcome = _existing_account(user, employee, wanted_uid)
        outcome["account_created"] = False
    else:
        target = _new_account(user, employee, data)
        outcome = {"staff_role_assigned": True, "account_reactivated": False, "account_created": True}
    if employee.user_id != target.pk:
        employee.versioned_update(user, user_id=target.pk)  # `user` is also the actor parameter
        record("hr.employee_user_linked", obj=employee, actor=user, after={"user": str(target.uid), **outcome})
    hr_scopes.forget(target)
    bump(NS_EMPLOYEES)
    return employee, outcome


@transaction.atomic
def unlink_user(instance: Employee, *, user, expected_version=None, note: str = "") -> tuple[Employee, dict]:
    employee = lock(Employee, instance, expected_version, related=("user__role",))
    target = employee.user
    if target is None:
        raise Conflict("employee_not_linked", "This employee has no login.")
    authz.deny_self_action(user, target, message="You cannot unlink your own login.")
    account_users.ensure_can_manage_user(user, target)
    employee.versioned_update(user, user_id=None)  # `user` is also the actor parameter
    outcome = {"account_deactivated": False}
    if target.role.slug == STAFF_SLUG and target.role.is_system and target.is_active:
        account_users.deactivate_user(target, user=user, note=note or f"login unlinked from employee {employee.code}")
        outcome["account_deactivated"] = True
    record("hr.employee_user_unlinked", obj=employee, actor=user, before={"user": str(target.uid)}, after=outcome, note=note)
    hr_scopes.forget(target)
    bump(NS_EMPLOYEES)
    return employee, outcome


# ----------------------------------------------------------------------------------------------------------------
# Photo
# ----------------------------------------------------------------------------------------------------------------
def set_photo(instance: Employee, *, user, file, expected_version=None) -> Employee:
    """Store the upload (outside any transaction), then attach it; a failed attach removes the new file again."""
    asset = media_assets.upload(user=user, file=file, visibility=MediaAsset.Visibility.PRIVATE, kind=MediaAsset.Kind.PHOTO, folder=PHOTO_FOLDER, allow_reserved=True)
    try:
        return _attach_photo(instance, asset, user=user, expected_version=expected_version)
    except BaseException:
        media_assets.delete_asset(asset, user=user)
        raise


@transaction.atomic
def _attach_photo(instance: Employee, asset: MediaAsset, *, user, expected_version) -> Employee:
    employee = lock(Employee, instance, expected_version, related=("photo",))
    previous = employee.photo
    employee.versioned_update(user, photo=asset)
    record("hr.employee_photo_set", obj=employee, actor=user, before={"photo": str(previous.uid) if previous else None}, after={"photo": str(asset.uid)})
    if previous is not None:
        media_assets.delete_asset(previous, user=user)
    bump(NS_EMPLOYEES)
    return employee


@transaction.atomic
def remove_photo(instance: Employee, *, user, expected_version=None) -> Employee:
    employee = lock(Employee, instance, expected_version, related=("photo",))
    previous = employee.photo
    if previous is None:
        raise DomainError("no_photo", "This employee has no photo.", status=404)
    employee.versioned_update(user, photo=None)
    record("hr.employee_photo_removed", obj=employee, actor=user, before={"photo": str(previous.uid)})
    media_assets.delete_asset(previous, user=user)
    bump(NS_EMPLOYEES)
    return employee
