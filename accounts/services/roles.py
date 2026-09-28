"""Role management (``roles/``): CRUD with registry normalisation and escalation guards.

* ``permissions``/``scopes`` are normalised **strictly** against the registry: unknown modules, actions or scopes
  are a 400 with the offending entries, never silently dropped.
* Nobody can create or edit a role to hold grants (or wider scopes) they do not hold themselves, nor change a role
  that already holds more than they do.
* Nobody can change the grants of the role they hold (``own_role_locked``) — an Admin cannot widen, or accidentally
  strip, their own access. Only a Super Admin may touch the Super Admin role.
* System (seeded) roles are editable but never deletable, and their slugs are fixed; seeded slugs are reserved.
* A role still held by a live user cannot be deleted (409 ``role_in_use``).
"""

from __future__ import annotations

from collections.abc import Mapping

from django.db import IntegrityError, transaction
from django.db.models import Count, Q

from accounts.models import Role, User
from accounts.registry import MODULES, RegistryError, normalise_permissions, normalise_scopes
from accounts.services.authz import ensure_grants_held, invalidate_role, is_super_admin, is_super_admin_role
from accounts.services.seeds import SEEDED_SLUGS
from audit.services import changes, record
from core.errors import Conflict, DomainError, PermissionDenied
from core.services import check_version, stamp_create

SNAPSHOT_FIELDS = ("slug", "name", "description", "permissions", "scopes")


def roles_queryset():
    """Live roles with the number of live users holding each."""
    return Role.objects.annotate(user_count=Count("users", filter=Q(users__deleted_at__isnull=True))).order_by("name", "id")


def role_snapshot(role: Role) -> dict:
    return {name: getattr(role, name) for name in SNAPSHOT_FIELDS}


def normalise_grants(permissions, scopes) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Strict registry normalisation; raises ``DomainError(validation_error)`` listing every offending entry."""
    errors: dict[str, list[str]] = {}
    try:
        normalised = normalise_permissions(permissions, strict=True)
    except RegistryError as exc:
        errors["permissions"] = [f"{key}: {message}" for key, messages in exc.errors.items() for message in messages]
        normalised = normalise_permissions(permissions)
    if scopes is not None and not isinstance(scopes, Mapping):
        errors["scopes"] = ["Must be an object of {module: scope}."]
    else:
        problems = []
        for module, scope in (scopes or {}).items():
            spec = MODULES.get(module)
            if spec is None:
                problems.append(f"{module}: Unknown module.")
            elif scope not in spec.scopes:
                problems.append(f"{module}: Scope {scope!r} is not allowed (allowed: {', '.join(spec.scopes)}).")
            elif module not in normalised:
                problems.append(f"{module}: No permission is granted on this module.")
        if problems:
            errors["scopes"] = problems
    if errors:
        raise DomainError("validation_error", "The role's permissions do not match the permission registry.", errors=errors)
    return normalised, normalise_scopes(scopes, normalised)


def _ensure_can_change(user, role: Role) -> None:
    if is_super_admin_role(role) and not is_super_admin(user):
        raise PermissionDenied("super_admin_required", "Only a Super Admin may change the Super Admin role.")
    ensure_grants_held(user, role.permissions, role.scopes, code="role_exceeds_own_grants", message="You cannot change a role holding permissions you do not hold.")


def _ensure_slug_available(slug: str, *, exclude_pk=None) -> None:
    if slug in SEEDED_SLUGS:
        raise Conflict("slug_reserved", f"The slug {slug!r} is reserved for a system role.", errors={"slug": ["Reserved for a system role."]})
    taken = Role.objects.filter(slug=slug)
    if exclude_pk is not None:
        taken = taken.exclude(pk=exclude_pk)
    if taken.exists():
        raise Conflict("slug_taken", f"A role with the slug {slug!r} already exists.", errors={"slug": ["Already in use."]})


@transaction.atomic
def create_role(*, user, data) -> Role:
    slug = data["slug"]
    permissions, scopes = normalise_grants(data.get("permissions") or {}, data.get("scopes") or {})
    ensure_grants_held(user, permissions, scopes, code="role_exceeds_own_grants", message="You cannot create a role holding permissions you do not hold.")
    _ensure_slug_available(slug)
    role = Role(slug=slug, name=data["name"], description=data.get("description", ""), permissions=permissions, scopes=scopes, is_system=False)
    stamp_create(role, user)
    try:
        with transaction.atomic():
            role.save()
    except IntegrityError:
        raise Conflict("slug_taken", f"A role with the slug {slug!r} already exists.", errors={"slug": ["Already in use."]}) from None
    record("accounts.role_created", obj=role, actor=user, after=role_snapshot(role))
    return role


@transaction.atomic
def update_role(instance: Role, *, user, data, expected_version=None) -> Role:
    role = Role.objects.select_for_update().get(pk=instance.pk)
    check_version(role, expected_version)
    _ensure_can_change(user, role)
    before = role_snapshot(role)
    values = {}
    if "slug" in data and data["slug"] != role.slug:
        if role.is_system:
            raise Conflict("system_role_slug_locked", "A system role's slug cannot change.", errors={"slug": ["System role slugs are fixed."]})
        _ensure_slug_available(data["slug"], exclude_pk=role.pk)
        values["slug"] = data["slug"]
    for name in ("name", "description"):
        if name in data and data[name] != getattr(role, name):
            values[name] = data[name]
    if "permissions" in data or "scopes" in data:
        wanted_permissions = data.get("permissions", role.permissions)
        # Stored scopes are kept for the modules that stay permitted; only client-supplied scopes are validated.
        wanted_scopes = data["scopes"] if "scopes" in data else {module: scope for module, scope in role.scopes.items() if module in (wanted_permissions or {})}
        permissions, scopes = normalise_grants(wanted_permissions, wanted_scopes)
        if (permissions, scopes) != (role.permissions, role.scopes):
            if getattr(user, "role_id", None) == role.pk:
                raise PermissionDenied("own_role_locked", "You cannot change the permissions of the role you hold.")
            ensure_grants_held(user, permissions, scopes, code="role_exceeds_own_grants", message="You cannot grant permissions you do not hold.")
            values.update(permissions=permissions, scopes=scopes)
    if not values:
        return role
    try:
        with transaction.atomic():
            role.versioned_update(user, **values)
    except IntegrityError:
        raise Conflict("slug_taken", "A role with this slug already exists.", errors={"slug": ["Already in use."]}) from None
    invalidate_role(role.uid)
    changed_before, changed_after = changes(before, role_snapshot(role))
    record("accounts.role_updated", obj=role, actor=user, before=changed_before, after=changed_after)
    return role


@transaction.atomic
def delete_role(instance: Role, *, user, expected_version=None) -> None:
    role = Role.objects.select_for_update().get(pk=instance.pk)
    check_version(role, expected_version)
    if role.is_system:
        raise Conflict("system_role_protected", "System roles cannot be deleted.")
    _ensure_can_change(user, role)
    holders = User.all_objects.filter(role=role, deleted_at__isnull=True).count()
    if holders:
        raise Conflict("role_in_use", f"The role is held by {holders} user(s); assign them another role first.", errors={"users": [f"{holders} user(s) hold this role."]})
    role.soft_delete(user)
    invalidate_role(role.uid)
    record("accounts.role_deleted", obj=role, actor=user, before=role_snapshot(role))
