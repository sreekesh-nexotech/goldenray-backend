"""Legacy import of staff accounts (PLAN §7.2 rows 1–2, §7.3 ``auth_user``, §7.4 ``users.json``). Called by
``migrations_tools``.

Functions take plain row dicts (the source tables' columns, ``SELECT *``) and return ``{"created", "updated",
"skipped", "violations"}`` (``violations``: ``{"source_table", "source_id", "code", "message"}``):

* **idempotent** through ``core_legacy_map`` (``CMS``/``accounts_role``, ``CMS``/``accounts_admin_user``,
  ``BACKEND``/``auth_user``, ``FLARIZE``/``users.json``): a re-run updates what changed and never duplicates; a mapped row deleted in the platform
  since stays deleted (``skipped``);
* ``dry_run=True`` runs in a transaction that is rolled back; each call writes one audit row
  (``accounts.legacy_import``) with the counts and the SHA-256 of the source rows.

Roles (CMS ``accounts_role``): the CMS module names are re-keyed onto the registry (:data:`CMS_MODULES`; ``careers`` →
``job_positions`` + ``career_page``); actions the registry does not know for the module are dropped and listed; every
scope is ``all``. The system seeds exist first (``seed_roles``); a CMS role whose slug is a seeded role **updates the
grants only** — the grants of the modules the CMS vocabulary covers are replaced by the CMS grants, the platform-only
modules keep the seed's grants — except ``super-admin``, which always keeps every grant (listed). Any other CMS role is
created as a custom role (``is_system`` false, slug and name kept).

Users (CMS ``accounts_admin_user``, main backend ``auth_user``): the e-mail is the login (lower-cased); an empty,
invalid or duplicate e-mail becomes ``<username>@migrated.invalid``. **Passwords are never migrated**: every account
gets an unusable password and ``must_reset_password`` (reset links are issued by :func:`issue_reset_links` at the
cutover). CMS role: the mapped ``access_role_id``, else by ``role`` (``admin`` → Admin, ``editor`` → Content
Manager, ``author`` → the custom role ``cms-author``: Content Manager with blogs only); main-backend accounts (the
``/bom/`` superusers) become Admin. ``is_superuser``/``is_staff`` are ignored. A live platform account with the same
e-mail (e.g. the bootstrap admin, or the same person in both sources) is adopted: mapped, its role left unchanged,
and audited (``accounts.legacy_user_adopted``) so that a re-run never rewrites it from that row either — only the row
that created an account keeps its role, names and active flag in step with the source.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.models import PasswordReset, Role, User
from accounts.registry import MODULES, SCOPE_ALL, full_access, normalise_scopes
from accounts.services import authz, emails, passwords
from accounts.services.authz import SUPER_ADMIN_SLUG
from accounts.services.seeds import SEEDED_SLUGS
from audit.models import AuditLog
from audit.services import record
from core.models import LegacyMap

CMS = LegacyMap.SourceSystem.CMS
BACKEND = LegacyMap.SourceSystem.BACKEND
FLARIZE = LegacyMap.SourceSystem.FLARIZE
ROLE_TABLE = "accounts_role"
CMS_USER_TABLE = "accounts_admin_user"
BACKEND_USER_TABLE = "auth_user"
FLARIZE_USER_TABLE = "users.json"
USER_TABLES = (CMS_USER_TABLE, BACKEND_USER_TABLE, FLARIZE_USER_TABLE)
# Flarize ``users.json`` role → (platform role slug, ``title``) — PLAN §7.4 row 1; the sales roles keep their Flarize
# flavour in ``title`` (``ENGINEER`` is the spelling users.json actually holds for ENGINEERING).
FLARIZE_ROLES = {
    "ADMIN": ("admin", ""),
    "PROJECT_HEAD": ("project-head", ""),
    "ENGINEERING": ("engineering", ""),
    "ENGINEER": ("engineering", ""),
    "PROCUREMENT": ("procurement", ""),
    "SALES_HEAD": ("sales-head", ""),
    "SALES": ("sales-executive", "Sales"),
    "SALES_CRS": ("sales-executive", "Sales (CRS)"),
    "FIELD_SALES": ("sales-executive", "Field Sales"),
}
MIGRATED_DOMAIN = "migrated.invalid"
ADOPTED_ACTION = "accounts.legacy_user_adopted"

# CMS module → registry modules (PLAN §7.2 row 1).
CMS_MODULES: dict[str, tuple[str, ...]] = {
    "dashboard": ("dashboard",),
    "pages": ("pages",),
    "blogs": ("blogs",),
    "faqs": ("faqs",),
    "media": ("media",),
    "seo": ("seo",),
    "leads": ("leads",),
    "emi": ("emi",),
    "quotations": ("quotations",),
    "careers": ("job_positions", "career_page"),
    "job_positions": ("job_positions",),
    "applications": ("applications",),
    "departments": ("departments",),
    "career_page": ("career_page",),
    "users": ("users",),
    "roles": ("roles",),
    "settings": ("settings",),
}
COVERED_MODULES = frozenset(module for targets in CMS_MODULES.values() for module in targets)
# CMS ``AdminUser.role`` (legacy_role) → platform role slug.
LEGACY_ROLE_SLUGS = {"admin": "admin", "editor": "content-manager", "author": "cms-author"}
AUTHOR_ROLE = {"slug": "cms-author", "name": "Content Author (CMS)", "description": "Blogs only — the CMS author role (PLAN §7.2 row 2)."}
USERNAME_RE = re.compile(r"[^a-z0-9._-]+")


class Report:
    def __init__(self, source_table: str):
        self.source_table = source_table
        self.created = self.updated = self.skipped = 0
        self.violations: list[dict] = []

    def violation(self, source_id, code: str, message: str) -> None:
        self.violations.append({"source_table": self.source_table, "source_id": str(source_id), "code": code, "message": message})

    def as_dict(self) -> dict:
        return {"created": self.created, "updated": self.updated, "skipped": self.skipped, "violations": self.violations}


def checksum(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, cls=DjangoJSONEncoder, sort_keys=True, default=str).encode()).hexdigest()


def _dt(value):
    if value in (None, ""):
        return None
    return parse_datetime(value) if isinstance(value, str) else value


def _mapped(system: str, table: str, source_id):
    return LegacyMap.objects.filter(source_system=system, source_table=table, source_id=str(source_id)).values_list("target_id", flat=True).first()


def _link(system: str, table: str, source_id, instance) -> None:
    LegacyMap.objects.update_or_create(source_system=system, source_table=table, source_id=str(source_id), defaults={"target_table": instance._meta.db_table, "target_id": instance.pk})


def _finish(report: Report, rows: list[dict], *, user, dry_run: bool) -> dict:
    record(
        "accounts.legacy_import",
        object_type="accounts.legacyimport",
        actor=user,
        actor_kind=None if user else "SYSTEM",
        after={"source_table": report.source_table, "rows": len(rows), "checksum": checksum(rows), **{**report.as_dict(), "violations": len(report.violations)}},
    )
    if dry_run:
        transaction.set_rollback(True)
    return report.as_dict()


# ── Roles ────────────────────────────────────────────────────────────────────────────────────────────────────────
def translate_permissions(permissions, report: Report, source_id) -> dict[str, list[str]]:
    """CMS ``{module: [actions]}`` → registry ``{module: [actions]}``; unknown modules/actions are dropped and listed."""
    if isinstance(permissions, str):
        permissions = json.loads(permissions or "{}")
    if not isinstance(permissions, dict):
        report.violation(source_id, "permissions_invalid", "permissions is not an object; no grants imported.")
        return {}
    granted: dict[str, set[str]] = {}
    for cms_module, actions in permissions.items():
        targets = CMS_MODULES.get(cms_module)
        if targets is None:
            report.violation(source_id, "module_dropped", f"CMS module {cms_module!r} has no platform module; dropped.")
            continue
        for module in targets:
            for action in actions if isinstance(actions, list) else []:
                if action in MODULES[module].actions:
                    granted.setdefault(module, set()).add(action)
                else:
                    report.violation(source_id, "action_dropped", f"{cms_module}.{action} is not an action of {module}; dropped.")
    return {module: [action for action in MODULES[module].actions if action in actions] for module, actions in granted.items() if actions}


def _all_scopes(permissions: dict) -> dict[str, str]:
    return normalise_scopes({module: SCOPE_ALL for module in permissions}, permissions)


@transaction.atomic
def import_roles(rows: Iterable[dict], *, user=None, dry_run: bool = False) -> dict:
    """CMS ``accounts_role`` → ``accounts_role`` (run ``seed_roles`` first)."""
    rows = list(rows)
    report = Report(ROLE_TABLE)
    for row in rows:
        source_id = row["id"]
        slug = (row.get("slug") or "").strip()
        if not slug:
            report.violation(source_id, "slug_missing", "role without a slug; row skipped.")
            report.skipped += 1
            continue
        cms_permissions = translate_permissions(row.get("permissions") or {}, report, source_id)
        target_id = _mapped(CMS, ROLE_TABLE, source_id)
        role = Role.all_objects.filter(pk=target_id).first() if target_id else Role.objects.filter(slug=slug).first()
        if role is not None and role.deleted_at is not None:
            report.skipped += 1
            continue
        if role is not None and (role.is_system or slug in SEEDED_SLUGS):
            if role.slug == SUPER_ADMIN_SLUG:
                permissions, scopes = full_access()
                if role.permissions != permissions:
                    report.violation(source_id, "super_admin_kept", "the Super Admin keeps every grant; the CMS grants were not applied.")
            else:
                kept = {module: actions for module, actions in role.permissions.items() if module not in COVERED_MODULES}
                permissions = {**kept, **cms_permissions}
                scopes = normalise_scopes({**role.scopes, **{module: SCOPE_ALL for module in cms_permissions}}, permissions)
            values = {"permissions": permissions, "scopes": scopes}
        else:
            values = {
                "slug": slug,
                "name": (row.get("name") or slug)[:120],
                "description": row.get("description") or "",
                "legacy_role": row.get("legacy_role") if row.get("legacy_role") in Role.LegacyRole.values else None,
                "permissions": cms_permissions,
                "scopes": _all_scopes(cms_permissions),
            }
        if role is None:
            role = Role(is_system=False, **values)
            role.save()
            Role.all_objects.filter(pk=role.pk).update(created_at=_dt(row.get("created_at")) or timezone.now(), updated_at=_dt(row.get("updated_at")) or timezone.now())
            report.created += 1
        else:
            role.normalise_grants()
            probe = Role(**{**{"permissions": role.permissions, "scopes": role.scopes}, **values})
            probe.normalise_grants()
            changed = {name: getattr(probe, name) for name in values if getattr(role, name) != getattr(probe, name)}
            if changed:
                Role.all_objects.filter(pk=role.pk).update(**changed, version=F("version") + 1, updated_at=timezone.now())
                report.updated += 1
            else:
                report.skipped += 1
        _link(CMS, ROLE_TABLE, source_id, role)
        authz.invalidate_role(role.uid)
    return _finish(report, rows, user=user, dry_run=dry_run)


# ── Users ────────────────────────────────────────────────────────────────────────────────────────────────────────
def _author_role() -> Role:
    role = Role.objects.filter(slug=AUTHOR_ROLE["slug"]).first()
    if role is None:
        permissions = {"dashboard": ["view"], "blogs": list(MODULES["blogs"].actions)}
        role = Role(is_system=False, legacy_role=Role.LegacyRole.AUTHOR, permissions=permissions, scopes=_all_scopes(permissions), **AUTHOR_ROLE)
        role.save()
    return role


def _role_for_cms_user(row: dict, report: Report) -> Role | None:
    if row.get("access_role_id") not in (None, ""):
        target = _mapped(CMS, ROLE_TABLE, row["access_role_id"])
        role = Role.objects.filter(pk=target).first() if target else None
        if role is not None:
            return role
        report.violation(row["id"], "role_unmapped", f"access_role {row['access_role_id']} was not imported; the legacy role decides.")
    slug = LEGACY_ROLE_SLUGS.get((row.get("role") or "").lower())
    if slug is None:
        return None
    return _author_role() if slug == AUTHOR_ROLE["slug"] else Role.objects.filter(slug=slug).first()


def _email(row: dict, report: Report, *, exclude_pk=None) -> str:
    raw = (row.get("email") or "").strip().lower()
    username = USERNAME_RE.sub("-", (row.get("username") or f"user-{row['id']}").strip().lower()).strip("-") or f"user-{row['id']}"
    fallback = f"{username}@{MIGRATED_DOMAIN}"
    if not raw:
        report.violation(row["id"], "email_missing", f"no e-mail; the login is {fallback}.")
        return fallback
    try:
        validate_email(raw)
    except Exception:  # noqa: BLE001 - Django raises ValidationError; anything unusable falls back
        report.violation(row["id"], "email_invalid", f"e-mail {raw!r} is invalid; the login is {fallback}.")
        return fallback
    return raw


def _was_adopted(account: User, *, system: str, table: str, source_id) -> bool:
    """Did this legacy row adopt ``account`` (an existing account with its address) rather than create it?"""
    return AuditLog.objects.filter(action=ADOPTED_ACTION, object_uid=account.uid, after__source_system=system, after__source_table=table, after__source_id=str(source_id)).exists()


def _import_user(row: dict, report: Report, *, system: str, table: str, role: Role | None, actor=None) -> None:
    source_id = row["id"]
    target_id = _mapped(system, table, source_id)
    existing = User.all_objects.filter(pk=target_id).first() if target_id else None
    if existing is not None and existing.deleted_at is not None:
        report.skipped += 1
        return
    if existing is not None and _was_adopted(existing, system=system, table=table, source_id=source_id):
        report.skipped += 1  # the account belongs to another source or to the platform: never rewritten from this row
        return
    email = _email(row, report)
    values = {"first_name": (row.get("first_name") or "")[:150], "last_name": (row.get("last_name") or "")[:150], "is_active": bool(row.get("is_active", True))}
    if existing is None:
        adopted = User.objects.filter(email__iexact=email).first()
        if adopted is not None:
            report.violation(source_id, "email_adopted", f"{email} already has a platform account; mapped to it, its role unchanged.")
            _link(system, table, source_id, adopted)
            record(ADOPTED_ACTION, obj=adopted, actor=actor, actor_kind=None if actor else "SYSTEM", after={"source_system": system, "source_table": table, "source_id": str(source_id)})
            report.skipped += 1
            return
        if role is None:
            report.violation(source_id, "role_missing", "no role could be derived; row skipped.")
            report.skipped += 1
            return
        account = User(email=email, role=role, must_reset_password=True, **values)
        account.set_unusable_password()
        try:
            with transaction.atomic():
                account.save()
        except IntegrityError as exc:
            report.violation(source_id, "rejected", f"rejected by the database: {str(exc).splitlines()[0]}")
            report.skipped += 1
            return
        joined = _dt(row.get("date_joined")) or timezone.now()
        User.all_objects.filter(pk=account.pk).update(created_at=joined, updated_at=joined, last_login_at=_dt(row.get("last_login")))
        _link(system, table, source_id, account)
        report.created += 1
        return
    if role is not None and existing.role_id != role.pk and existing.must_reset_password:
        values["role_id"] = role.pk  # the source still decides until the person has taken the account over
    changed = {name: value for name, value in values.items() if getattr(existing, name) != value}
    if changed:
        User.all_objects.filter(pk=existing.pk).update(**changed, version=F("version") + 1, updated_at=timezone.now())
        authz.invalidate_user(existing.uid)
        report.updated += 1
    else:
        report.skipped += 1
    _link(system, table, source_id, existing)


@transaction.atomic
def import_cms_users(rows: Iterable[dict], *, user=None, dry_run: bool = False) -> dict:
    """CMS ``accounts_admin_user`` → ``accounts_user`` (roles imported first)."""
    rows = list(rows)
    report = Report(CMS_USER_TABLE)
    for row in rows:
        _import_user(row, report, system=CMS, table=CMS_USER_TABLE, role=_role_for_cms_user(row, report), actor=user)
    return _finish(report, rows, user=user, dry_run=dry_run)


@transaction.atomic
def import_backend_users(rows: Iterable[dict], *, user=None, dry_run: bool = False) -> dict:
    """Main backend ``auth_user`` (the ``/bom/`` superusers) → ``accounts_user`` with the Admin role."""
    rows = list(rows)
    report = Report(BACKEND_USER_TABLE)
    admin = Role.objects.filter(slug="admin").first()
    if admin is None:
        report.violation("", "admin_role_missing", "the Admin role does not exist; run seed_roles first.")
    for row in rows:
        _import_user(row, report, system=BACKEND, table=BACKEND_USER_TABLE, role=admin, actor=user)
    return _finish(report, rows, user=user, dry_run=dry_run)


@transaction.atomic
def import_flarize_users(rows: Iterable[dict], *, user=None, dry_run: bool = False) -> dict:
    """Flarize ``users.json`` → ``accounts_user`` (PLAN §7.4 row 1): roles mapped by :data:`FLARIZE_ROLES` (the sales
    roles keep their flavour in ``title``), ``status`` ACTIVE → active, forced reset (password hashes are never read).

    Legacy map ``FLARIZE users.json <userId>`` — the key every Flarize importer resolves ``createdBy``/``ownerId``
    through. Same rules as the other sources: e-mail login, an existing account with the address is adopted."""
    rows = list(rows)
    report = Report(FLARIZE_USER_TABLE)
    roles = {slug: Role.objects.filter(slug=slug).first() for slug, _ in FLARIZE_ROLES.values()}
    for raw in rows:
        user_id = str(raw.get("userId") or "").strip()
        if not user_id:
            report.violation("", "incomplete_row", "a user without userId; skipped.")
            report.skipped += 1
            continue
        slug, title = FLARIZE_ROLES.get(str(raw.get("role") or "").upper(), (None, ""))
        role = roles.get(slug) if slug else None
        if slug is None:
            report.violation(user_id, "unknown_role", f"Flarize role {raw.get('role')!r} has no platform role; row skipped.")
            report.skipped += 1
            continue
        names = str(raw.get("name") or "").strip().split(" ", 1)
        row = {
            "id": user_id,
            "email": raw.get("email"),
            "username": raw.get("username") or user_id,
            "first_name": names[0],
            "last_name": names[1] if len(names) > 1 else "",
            "is_active": str(raw.get("status") or "ACTIVE").upper() == "ACTIVE",
            "date_joined": raw.get("createdAt"),
            "last_login": raw.get("lastLoginAt"),
        }
        before = report.created
        _import_user(row, report, system=FLARIZE, table=FLARIZE_USER_TABLE, role=role, actor=user)
        if title and report.created > before:
            User.all_objects.filter(pk=_mapped(FLARIZE, FLARIZE_USER_TABLE, user_id)).update(title=title)
    return _finish(report, rows, user=user, dry_run=dry_run)


# ── Reset links (cutover) ────────────────────────────────────────────────────────────────────────────────────────
def migrated_users():
    """Live accounts mapped from a legacy user table."""
    targets = LegacyMap.objects.filter(source_table__in=USER_TABLES, target_table=User._meta.db_table).values("target_id")
    return User.objects.filter(pk__in=targets)


@transaction.atomic
def issue_reset_links(*, user=None, send: bool = True) -> dict:
    """Issue (and e-mail) a set-password link to every migrated account that still needs one (PLAN §7.2 row 2).

    Accounts that already hold an unused, unexpired link, have set a password since, are inactive or only have a
    ``migrated.invalid`` address are not sent anything (the latter two are listed).
    """
    report = Report("accounts_user")
    now = timezone.now()
    for account in migrated_users().order_by("pk"):
        if not account.must_reset_password:
            report.skipped += 1
            continue
        if not account.is_active or account.email.endswith(f"@{MIGRATED_DOMAIN}"):
            report.violation(account.uid, "not_sent", "inactive account or no real e-mail address; no link sent.")
            report.skipped += 1
            continue
        if PasswordReset.objects.filter(user=account, used_at__isnull=True, expires_at__gt=now).exists():
            report.skipped += 1
            continue
        issued = passwords.issue_reset(account, actor=user, ttl=settings.ACCOUNTS_INVITE_TTL)
        if send:
            emails.send_invite(account, issued.link, settings.ACCOUNTS_INVITE_TTL)
        record("accounts.migration_reset_issued", obj=account, actor=user, actor_kind=None if user else "SYSTEM", after={"email": account.email})
        report.created += 1
    return report.as_dict()
