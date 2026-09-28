"""User management (``users/``) and the system-level account operations (bootstrap, HR-driven deactivation).

Escalation guards (standard §3.3 — authority is scoped explicitly, there is no god-flag):

* a role can be assigned only by someone who holds every grant (and scope) of that role;
* nobody can change their own role, deactivate or delete themselves;
* nobody can change a user whose role holds grants they do not hold themselves;
* only a Super Admin may change, deactivate, reset or delete a Super Admin, or assign the Super Admin role;
* changing a user's e-mail or role needs ``users.manage`` besides ``users.edit`` (an e-mail change followed by a
  password reset is an account takeover); an e-mail change voids every open reset/invitation link (they went to the
  old address) and re-sends a pending invitation to the new one.

There are no passwords in this module: a new account gets an unusable password, ``must_reset_password`` and an
invitation link (single use, ``ACCOUNTS_INVITE_TTL``); a forced reset does the same for an existing account.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction

from accounts.models import Role, User
from accounts.services import emails, passwords, sessions
from accounts.services.authz import SUPER_ADMIN_SLUG, can, deny_self_action, ensure_grants_held, invalidate_user, is_self, is_super_admin, is_super_admin_role
from accounts.services.seeds import seed_roles
from audit.services import changes, record, snapshot
from core.errors import Conflict, DomainError, NotFound, PermissionDenied
from core.outbox import emit
from core.services import check_version, stamp_create

PROFILE_FIELDS = ("first_name", "last_name", "phone_e164", "title")
SNAPSHOT_FIELDS = ("email", "first_name", "last_name", "phone_e164", "title", "role", "is_active", "must_reset_password")


def users_queryset():
    return User.objects.select_related("role").order_by("email", "id")


def user_snapshot(user: User) -> dict:
    return snapshot(user, SNAPSHOT_FIELDS)


def normalise_email(email: str) -> str:
    email = User.objects.normalize_email((email or "").strip())
    try:
        validate_email(email)
    except DjangoValidationError:
        raise DomainError("validation_error", "Invalid input.", errors={"email": ["Enter a valid e-mail address."]}) from None
    return email


def _email_taken(email: str, *, exclude_pk=None) -> bool:
    taken = User.objects.filter(email=email)
    if exclude_pk is not None:
        taken = taken.exclude(pk=exclude_pk)
    return taken.exists()


def _email_conflict() -> Conflict:
    return Conflict("email_taken", "Another account already uses this e-mail address.", errors={"email": ["Already in use."]})


def ensure_can_assign_role(actor, role: Role) -> None:
    if role is None or role.deleted_at is not None:
        raise NotFound("role_not_found", "The role does not exist.")
    if is_super_admin_role(role) and not is_super_admin(actor):
        raise PermissionDenied("super_admin_required", "Only a Super Admin may assign the Super Admin role.")
    ensure_grants_held(actor, role.permissions, role.scopes, code="role_exceeds_own_grants", message="You cannot assign a role holding permissions you do not hold.")


def ensure_can_manage_user(actor, target: User) -> None:
    if is_super_admin_role(target.role) and not is_super_admin(actor):
        raise PermissionDenied("super_admin_required", "Only a Super Admin may change a Super Admin.")
    if not is_self(actor, target):
        ensure_grants_held(actor, target.role.permissions, target.role.scopes, code="user_exceeds_own_grants", message="You cannot change a user holding permissions you do not hold.")


def _lock(instance: User, expected_version) -> User:
    target = User.objects.select_for_update(of=("self",)).select_related("role").get(pk=instance.pk)
    check_version(target, expected_version)
    return target


@transaction.atomic
def create_user(*, user, data) -> User:
    """Create an account and e-mail an invitation to set its password."""
    role = data["role"]
    ensure_can_assign_role(user, role)
    email = normalise_email(data["email"])
    if _email_taken(email):
        raise _email_conflict()
    account = User(email=email, role=role, must_reset_password=True, **{name: data.get(name, "") for name in PROFILE_FIELDS})
    account.set_unusable_password()
    stamp_create(account, user)
    try:
        with transaction.atomic():
            account.save()
    except IntegrityError:
        raise _email_conflict() from None
    invite = passwords.issue_reset(account, actor=user, ttl=settings.ACCOUNTS_INVITE_TTL)
    record("accounts.user_created", obj=account, actor=user, after=user_snapshot(account))
    emails.send_invite(account, invite.link, settings.ACCOUNTS_INVITE_TTL)
    return account


@transaction.atomic
def update_user(instance: User, *, user, data, expected_version=None) -> User:
    target = _lock(instance, expected_version)
    ensure_can_manage_user(user, target)
    before = user_snapshot(target)
    values = {name: data[name] for name in PROFILE_FIELDS if name in data and data[name] != getattr(target, name)}
    if "email" in data:
        email = normalise_email(data["email"])
        if email != target.email:
            if not can(user, "users", "manage"):
                raise PermissionDenied("permission_denied", "Changing an e-mail address needs the users.manage permission.")
            if _email_taken(email, exclude_pk=target.pk):
                raise _email_conflict()
            values["email"] = email
    role = data.get("role")
    if role is not None and role.pk != target.role_id:
        if not can(user, "users", "manage"):
            raise PermissionDenied("permission_denied", "Changing a role needs the users.manage permission.")
        if is_self(user, target):
            raise PermissionDenied("own_role_change", "You cannot change your own role.")
        ensure_can_assign_role(user, role)
        values["role"] = role
    if not values:
        return target
    try:
        with transaction.atomic():
            target.versioned_update(user, **values)
    except IntegrityError:
        raise _email_conflict() from None
    invalidate_user(target.uid)
    changed_before, changed_after = changes(before, user_snapshot(target))
    if "email" in values:
        _redirect_open_links(target, actor=user, audit_after=changed_after)
    record("accounts.user_updated", obj=target, actor=user, before=changed_before, after=changed_after)
    return target


def _redirect_open_links(target: User, *, actor, audit_after: dict) -> None:
    """After an e-mail change: links already mailed to the *old* address must stop working (a mistyped invitation
    would otherwise let its recipient set the corrected account's password). A pending invitation is re-sent to the
    new address; an account that already has a password uses "Forgot password" at its new address."""
    audit_after["open_reset_links_voided"] = passwords.void_open_resets(target, actor=actor)
    if target.must_reset_password:
        invite = passwords.issue_reset(target, actor=actor, ttl=settings.ACCOUNTS_INVITE_TTL)
        emails.send_invite(target, invite.link, settings.ACCOUNTS_INVITE_TTL)
        audit_after["invitation_resent"] = True


def _deactivate(target: User, *, user, reason: str, note: str = "") -> None:
    target.versioned_update(user, is_active=False)
    sessions.revoke_all_sessions(target, user=user, reason=reason)
    invalidate_user(target.uid)
    record("accounts.user_deactivated", obj=target, actor=user, actor_kind=None if user is not None else "SYSTEM", before={"is_active": True}, after={"is_active": False, "reason": reason}, note=note)
    emit("accounts.user_deactivated", {"user_uid": str(target.uid), "reason": reason}, aggregate_type="accounts.user", aggregate_uid=target.uid)


@transaction.atomic
def deactivate_user(instance: User, *, user, expected_version=None, note: str = "") -> User:
    target = _lock(instance, expected_version)
    deny_self_action(user, target, message="You cannot deactivate your own account.")
    ensure_can_manage_user(user, target)
    if target.is_active:
        _deactivate(target, user=user, reason="deactivated", note=note)
    return target


@transaction.atomic
def reactivate_user(instance: User, *, user, expected_version=None, note: str = "") -> User:
    target = _lock(instance, expected_version)
    ensure_can_manage_user(user, target)
    if not target.is_active:
        target.versioned_update(user, is_active=True)
        invalidate_user(target.uid)
        record("accounts.user_reactivated", obj=target, actor=user, before={"is_active": False}, after={"is_active": True}, note=note)
        emit("accounts.user_reactivated", {"user_uid": str(target.uid)}, aggregate_type="accounts.user", aggregate_uid=target.uid)
    return target


@transaction.atomic
def force_password_reset(instance: User, *, user, expected_version=None, note: str = "") -> User:
    """Invalidate the password, end every session and e-mail a fresh set-password link."""
    target = _lock(instance, expected_version)
    deny_self_action(user, target, message="Change your own password instead of forcing a reset.")
    ensure_can_manage_user(user, target)
    target.set_unusable_password()
    target.versioned_update(user, password=target.password, must_reset_password=True)
    sessions.revoke_all_sessions(target, user=user, reason="force_reset")
    passwords.void_open_resets(target, actor=user)
    reset = passwords.issue_reset(target, actor=user, ttl=settings.ACCOUNTS_INVITE_TTL)
    invalidate_user(target.uid)
    record("accounts.user_force_reset", obj=target, actor=user, after={"must_reset_password": True}, note=note)
    emails.send_password_reset(target, reset.link, settings.ACCOUNTS_INVITE_TTL)
    return target


@transaction.atomic
def delete_user(instance: User, *, user, expected_version=None) -> None:
    """Soft-delete the account (its e-mail becomes free again); ends every session."""
    target = _lock(instance, expected_version)
    deny_self_action(user, target, message="You cannot delete your own account.")
    ensure_can_manage_user(user, target)
    before = user_snapshot(target)
    sessions.revoke_all_sessions(target, user=user, reason="deleted")
    target.soft_delete(user)
    invalidate_user(target.uid)
    record("accounts.user_deleted", obj=target, actor=user, before=before)


# ----------------------------------------------------------------------------------------------------------------------
# System operations
# ----------------------------------------------------------------------------------------------------------------------
@transaction.atomic
def bootstrap_super_admin(*, email: str, first_name: str = "", last_name: str = "") -> tuple[User, passwords.IssuedReset]:
    """First (or recovery) Super Admin: seeds the roles, creates the account without a password and returns a
    one-time set-password link valid for ``ACCOUNTS_BOOTSTRAP_TTL``."""
    seed_roles()
    role = Role.objects.get(slug=SUPER_ADMIN_SLUG, is_system=True)
    email = normalise_email(email)
    if _email_taken(email):
        raise _email_conflict()
    account = User(email=email, role=role, first_name=first_name, last_name=last_name, must_reset_password=True)
    account.set_unusable_password()
    stamp_create(account, None)
    account.save()
    reset = passwords.issue_reset(account, ttl=settings.ACCOUNTS_BOOTSTRAP_TTL)
    record("accounts.user_bootstrapped", obj=account, actor_kind="SYSTEM", after=user_snapshot(account))
    return account, reset


@transaction.atomic
def deactivate_for_system(user_uid, *, reason: str, note: str = "") -> bool:
    """Deactivate an account on behalf of another context (e.g. HR deactivated the employee). Idempotent."""
    try:
        user_uid = uuid.UUID(str(user_uid))
    except (TypeError, ValueError):
        return False
    target = User.objects.select_for_update(of=("self",)).filter(uid=user_uid).first()
    if target is None or not target.is_active:
        return False
    _deactivate(target, user=None, reason=reason, note=note)
    return True
