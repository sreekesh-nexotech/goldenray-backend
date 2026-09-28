"""eSSL import (PLAN §7.5 "eSSL (Postgres)"): users, offices, shifts, employees, holidays, leave, attendance rules.

Every function takes plain row dicts exactly as the eSSL tables hold them (``SELECT *``) and returns
``{"created", "updated", "skipped", "violations"}``:

* each source row is tracked in ``core_legacy_map`` (``ESSL`` × source table × source id), so a re-run updates the
  rows it created (only the columns that changed) and never duplicates; a platform row deleted after the import
  stays deleted (``skipped``);
* rows that cannot be imported are skipped and listed in ``violations`` (``{"source_id", "field", "message"}``);
  rows imported with a repair (an unknown time zone, an unparsable phone, a dropped rule key) are imported and
  listed too;
* source timestamps are preserved (inserts bypass ``BaseModel.save()``); each call writes one ``audit_log`` row
  (``hr.legacy_imported``) with the counts and the sha256 of the batch.

Order: :func:`import_users` → :func:`import_shifts` → :func:`import_offices` → :func:`import_employees` →
:func:`import_holidays` → :func:`import_leave_types` → :func:`import_leave_records` → :func:`import_attendance_rules`.

Users (PLAN §7.5): roles ADMIN→Admin, HR→HR, USER/VIEWER→Staff; bcrypt hashes are stored as Django ``bcrypt$<hash>``
(``accounts.hashers.LegacyBCryptPasswordHasher``, 72-byte truncation like eSSL) so old passwords keep working and are
upgraded to Argon2 on the first login; e-mail is the platform login, so an account without a valid e-mail gets
``<username>@migrated.invalid`` (reported: set a real address before that person can sign in); an account whose
e-mail already belongs to a platform user is linked to it, never overwritten (reported).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from datetime import date, datetime, time, timedelta

from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime, parse_time

from accounts.models import Role, User
from accounts.services.authz import invalidate_user
from accounts.services.seeds import seed_roles
from audit.services import record
from core.errors import DomainError
from core.models import LegacyMap
from flarize.cache_utils import bump
from hr.models import AttendanceRule, Employee, Holiday, LeaveRecord, LeaveType, Office, Shift
from hr.models.office import DEFAULT_TIMEZONE
from hr.models.shift import DEFAULT_WEEKLY_OFF_DAYS, DEFAULT_WORKING_DAYS, MINUTE_FIELDS
from hr.services import validation
from hr.services.common import NS_EMPLOYEES, NS_LEAVE, NS_SETUP, available_timezones
from hr.services.rules import rule_errors

ESSL = LegacyMap.SourceSystem.ESSL
ROLE_SLUGS = {"ADMIN": "admin", "HR": "hr", "USER": "staff", "VIEWER": "staff"}
BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")
MIGRATED_EMAIL_DOMAIN = "migrated.invalid"
UNUSABLE_PASSWORD = "!essl-import-no-bcrypt-hash"  # "!" = Django's unusable-password prefix; fixed so a re-run changes nothing
LEGACY_HALF_DAY_AFTER = time(10, 0)  # eSSL v3 DEFAULT_HALF_DAY_AFTER (A5)
IDENTITY_METHODS = set(Employee.IdentityMethod.values)
LEAVE_STATUSES = set(LeaveRecord.Status.values)
LEAVE_TYPE_TABLE = "leave_records.leave_type"
LINKED_USERS_TABLE = "users.linked"  # marker: the eSSL user was linked to an account that already existed


class Report:
    def __init__(self):
        self.created = self.updated = self.skipped = 0
        self.violations: list[dict] = []

    def violation(self, source_id, field: str, message: str) -> None:
        self.violations.append({"source_id": str(source_id), "field": field, "message": message})

    def as_dict(self) -> dict:
        return {"created": self.created, "updated": self.updated, "skipped": self.skipped, "violations": self.violations}


class Skip(Exception):
    """A row that cannot be imported (the reason is already in the report)."""


def checksum(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, cls=DjangoJSONEncoder, sort_keys=True, default=str).encode()).hexdigest()


def _dt(value):
    if value in (None, ""):
        return None
    return parse_datetime(value) if isinstance(value, str) else value


def _date(value):
    if value in (None, ""):
        return None
    return parse_date(value) if isinstance(value, str) else value


def _time(value):
    if value in (None, ""):
        return None
    return parse_time(value) if isinstance(value, str) else value


def _text(value) -> str:
    return (value or "").strip() if isinstance(value, str) else ("" if value is None else str(value))


def mapped_id(table: str, source_id) -> int | None:
    if source_id in (None, ""):
        return None
    return LegacyMap.objects.filter(source_system=ESSL, source_table=table, source_id=str(source_id)).values_list("target_id", flat=True).first()


def mapped(model, table: str, source_id):
    target = mapped_id(table, source_id)
    return model.all_objects.filter(pk=target).first() if target else None


def _remember(table: str, source_id, row) -> None:
    LegacyMap.objects.create(source_system=ESSL, source_table=table, source_id=str(source_id), target_table=row._meta.db_table, target_id=row.pk)


def _upsert(model, *, table: str, source_id, values: dict, report: Report, created_at=None, updated_at=None, preserve: Callable | None = None):
    """Insert (tracked in ``core_legacy_map``) or update the mapped row; returns the row or ``None`` when skipped.

    ``preserve(row, changed)`` may drop keys from ``changed`` that must not be overwritten on a re-run.
    """
    target_id = mapped_id(table, source_id)
    if target_id is not None:
        row = model.all_objects.filter(pk=target_id).first()
        if row is None or row.deleted_at is not None:
            report.skipped += 1
            return None
        changed = {name: value for name, value in values.items() if getattr(row, name) != value}
        if preserve is not None:
            preserve(row, changed)
        if not changed:
            report.skipped += 1
            return row
        columns = {model._meta.get_field(name).attname: (value.pk if hasattr(value, "_meta") else value) for name, value in changed.items()}
        model.all_objects.filter(pk=row.pk).update(**columns, version=F("version") + 1, updated_at=updated_at or timezone.now())
        report.updated += 1
        return model.all_objects.get(pk=row.pk)
    row = model(**values)
    row.created_at = created_at or timezone.now()
    row.updated_at = updated_at or row.created_at
    model.objects.bulk_create([row])  # bypasses save(): source timestamps survive
    _remember(table, source_id, row)
    report.created += 1
    return row


def _run(rows: Iterable[dict], handle: Callable[[dict, Report], None], *, table: str, object_type: str, user, namespaces: tuple[str, ...]) -> dict:
    rows = list(rows)
    report = Report()
    for row in rows:
        try:
            with transaction.atomic():
                handle(row, report)
        except Skip:
            continue
        except IntegrityError as exc:
            report.violation(row.get("id"), "row", f"rejected by the database: {str(exc).splitlines()[0]}")
    result = report.as_dict()
    record(
        "hr.legacy_imported",
        object_type=object_type,
        actor=user,
        actor_kind=None if user else "SYSTEM",
        after={
            "source": f"ESSL {table}",
            "rows": len(rows),
            "checksum": checksum(rows),
            "created": report.created,
            "updated": report.updated,
            "skipped": report.skipped,
            "violations": len(report.violations),
        },
    )
    for namespace in namespaces:
        bump(namespace)
    return result


# ----------------------------------------------------------------------------------------------------------------
# Users
# ----------------------------------------------------------------------------------------------------------------
def _platform_email(row: dict, report: Report) -> str:
    raw = _text(row.get("email")).lower()
    try:
        email = validation.email(raw)
    except DomainError:
        email = ""
    if not email:
        email = f"{_text(row.get('username')).lower() or 'essl-user-' + str(row.get('id'))}@{MIGRATED_EMAIL_DOMAIN}"
        report.violation(row.get("id"), "email", f"no valid e-mail ({raw or 'empty'}); the login is {email} until an Admin sets a real address")
    return email


def _password(row: dict, report: Report) -> tuple[str, bool]:
    raw = _text(row.get("password_hash"))
    if raw.startswith(BCRYPT_PREFIXES):
        return f"bcrypt${raw}", False
    report.violation(row.get("id"), "password_hash", "not a bcrypt hash: the account must set a new password (reset link)")
    return UNUSABLE_PASSWORD, True


def import_users(rows: Iterable[dict], *, roles: Iterable[dict], user=None) -> dict:
    """eSSL ``users`` → ``accounts_user`` (roles from the eSSL ``roles`` rows; see the module docstring)."""
    seed_roles()
    role_names = {row["id"]: _text(row.get("name")).upper() for row in roles}
    platform_roles = {role.slug: role for role in Role.objects.filter(slug__in=set(ROLE_SLUGS.values()), is_system=True)}

    def handle(row: dict, report: Report) -> None:
        source_id = row.get("id")
        name = role_names.get(row.get("role_id"))
        slug = ROLE_SLUGS.get(name or "")
        if slug is None:
            report.violation(source_id, "role_id", f"role {name or 'none'} has no platform equivalent; imported as Staff")
            slug = "staff"
        email = _platform_email(row, report)
        password, must_reset = _password(row, report)
        first, _, last = _text(row.get("full_name")).partition(" ")
        values = {
            "email": email,
            "first_name": first[:150],
            "last_name": last.strip()[:150],
            "is_active": bool(row.get("is_active", True)),
            "role": platform_roles[slug],
            "password": password,
            "must_reset_password": must_reset,
        }
        if mapped_id(LINKED_USERS_TABLE, source_id) is not None:  # linked to a pre-existing account: never overwritten
            report.skipped += 1
            return
        if mapped_id("users", source_id) is None:
            existing = User.objects.filter(email=email).first()
            if existing is not None:
                _remember("users", source_id, existing)
                _remember(LINKED_USERS_TABLE, source_id, existing)
                report.violation(source_id, "email", f"{email} already belongs to a platform account; linked to it, nothing overwritten (check its role: eSSL had {name})")
                report.skipped += 1
                return

        def preserve(account: User, changed: dict) -> None:
            # A password the platform already upgraded (the person signed in) or reset is never replaced by the old hash.
            if "password" in changed and not (account.password.startswith("bcrypt$") or account.password == UNUSABLE_PASSWORD):
                changed.pop("password")
                changed.pop("must_reset_password", None)

        account = _upsert(User, table="users", source_id=source_id, values=values, report=report, created_at=_dt(row.get("created_at")), updated_at=_dt(row.get("updated_at")), preserve=preserve)
        if account is not None:
            invalidate_user(account.uid)

    return _run(rows, handle, table="users", object_type="accounts.user", user=user, namespaces=())


# ----------------------------------------------------------------------------------------------------------------
# Shifts and offices
# ----------------------------------------------------------------------------------------------------------------
def _weekdays(row: dict, field: str, default: list[int], report: Report) -> list[int]:
    try:
        return validation.weekdays(row.get(field) if row.get(field) is not None else default, field)
    except DomainError:
        report.violation(row.get("id"), field, f"invalid weekdays {row.get(field)!r}; using {default}")
        return list(default)


def import_shifts(rows: Iterable[dict], *, user=None) -> dict:
    """eSSL ``shifts`` → ``hr_shift``. The v4 fields take the PLAN defaults; ``office_id`` (informational in eSSL) is
    not kept; an overnight flag that contradicts the clock is corrected; the half-day deadline change (A5) is listed."""

    def handle(row: dict, report: Report) -> None:
        source_id = row.get("id")
        start, end = _time(row.get("start_time")), _time(row.get("end_time"))
        code, name = _text(row.get("code")), _text(row.get("name"))
        if not code or not name or start is None or end is None:
            report.violation(source_id, "row", "code, name, start_time and end_time are required")
            raise Skip
        overnight = bool(row.get("is_overnight"))
        if overnight != (end <= start):
            overnight = end <= start
            report.violation(source_id, "is_overnight", f"{start:%H:%M}–{end:%H:%M} is {'an overnight' if overnight else 'a day'} shift; is_overnight set to {overnight}")
        values = {"code": code[:30], "name": name[:120], "start_time": start, "end_time": end, "is_overnight": overnight}
        for field, default, maximum in MINUTE_FIELDS:
            value = row.get(field, default)
            if value is None or isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                report.violation(source_id, field, f"{value!r} outside 0–{maximum}; using {default}")
                value = default
            values[field] = value
        if values["half_day_minutes"] > values["full_day_minutes"]:
            report.violation(source_id, "half_day_minutes", "longer than a full day; set to full_day_minutes")
            values["half_day_minutes"] = values["full_day_minutes"]
        values.update(
            auto_deduct_break=bool(row.get("auto_deduct_break", True)),
            overtime_enabled=bool(row.get("overtime_enabled", True)),
            working_days=_weekdays(row, "working_days", DEFAULT_WORKING_DAYS, report),
            weekly_off_days=_weekdays(row, "weekly_off_days", DEFAULT_WEEKLY_OFF_DAYS, report),
            is_active=bool(row.get("is_active", True)),
        )
        if row.get("office_id") is not None:
            report.violation(source_id, "office_id", "the shift's office link was informational in eSSL and is not kept (use the office's default shift)")
        v4_deadline = (datetime.combine(date(2000, 1, 1), start) + timedelta(minutes=values["half_day_after_minutes"])).time()
        if not overnight and v4_deadline != LEGACY_HALF_DAY_AFTER:
            report.violation(
                source_id,
                "half_day_after_minutes",
                f"eSSL made arrivals after 10:00 a half day; v4 uses start + {values['half_day_after_minutes']} min = {v4_deadline:%H:%M} (A5)"
                " — adjust the shift or add an attendance rule to keep 10:00",
            )
        elif overnight:
            report.violation(source_id, "half_day_after_minutes", f"eSSL's 10:00 deadline never applied to this night shift; v4 uses start + {values['half_day_after_minutes']} min (A5)")
        _upsert(Shift, table="shifts", source_id=source_id, values=values, report=report, created_at=_dt(row.get("created_at")), updated_at=_dt(row.get("updated_at")))

    return _run(rows, handle, table="shifts", object_type="hr.shift", user=user, namespaces=(NS_SETUP,))


def import_offices(rows: Iterable[dict], *, user=None) -> dict:
    """eSSL ``offices`` → ``hr_office``; an unknown time zone becomes Asia/Kolkata (reported); the default shift
    through the shift map."""

    def handle(row: dict, report: Report) -> None:
        source_id = row.get("id")
        code, name = _text(row.get("code")), _text(row.get("name"))
        if not code or not name:
            report.violation(source_id, "row", "code and name are required")
            raise Skip
        zone = _text(row.get("timezone")) or DEFAULT_TIMEZONE
        if zone not in available_timezones():
            report.violation(source_id, "timezone", f"unknown time zone {zone!r}; set to {DEFAULT_TIMEZONE}")
            zone = DEFAULT_TIMEZONE
        default_shift = None
        if row.get("default_shift_id") is not None:
            default_shift = mapped(Shift, "shifts", row["default_shift_id"])
            if default_shift is None:
                report.violation(source_id, "default_shift_id", f"shift {row['default_shift_id']} was not imported; no default shift")
        values = {"code": code[:30], "name": name[:150], "address": _text(row.get("address")), "timezone": zone, "default_shift": default_shift, "is_active": bool(row.get("is_active", True))}
        _upsert(Office, table="offices", source_id=source_id, values=values, report=report, created_at=_dt(row.get("created_at")), updated_at=_dt(row.get("updated_at")))

    return _run(rows, handle, table="offices", object_type="hr.office", user=user, namespaces=(NS_SETUP,))


# ----------------------------------------------------------------------------------------------------------------
# Employees
# ----------------------------------------------------------------------------------------------------------------
def import_employees(rows: Iterable[dict], *, user=None) -> dict:
    """eSSL ``employees`` → ``hr_employee``: office/shift/login through the maps; phones as E.164 (else blank,
    reported); ``attendance_identity_method`` → ``identity_method``; ``photo_url`` (never applied in production) is
    not migrated."""

    def handle(row: dict, report: Report) -> None:
        source_id = row.get("id")
        code, full_name = _text(row.get("employee_code")), _text(row.get("full_name"))
        if not code or not full_name:
            report.violation(source_id, "row", "employee_code and full_name are required")
            raise Skip
        references = {}
        for field, model, table in (("office", Office, "offices"), ("shift", Shift, "shifts")):
            references[field] = mapped(model, table, row.get(f"{field}_id")) if row.get(f"{field}_id") is not None else None
            if row.get(f"{field}_id") is not None and references[field] is None:
                report.violation(source_id, f"{field}_id", f"{field} {row[f'{field}_id']} was not imported; left empty")
        try:
            phone = validation.phone_e164(row.get("phone"))
        except DomainError:
            report.violation(source_id, "phone", f"{row.get('phone')!r} is not a valid phone number; left empty")
            phone = ""
        try:
            email = validation.email(row.get("email"))
        except DomainError:
            report.violation(source_id, "email", f"{row.get('email')!r} is not a valid e-mail address; left empty")
            email = ""
        method = _text(row.get("attendance_identity_method")).upper() or Employee.IdentityMethod.UNSPECIFIED
        if method not in IDENTITY_METHODS:
            report.violation(source_id, "attendance_identity_method", f"unknown value {method!r}; UNSPECIFIED")
            method = Employee.IdentityMethod.UNSPECIFIED
        if row.get("photo_url"):
            report.violation(source_id, "photo_url", "photo references are not migrated (upload the photo again)")
        login = None
        if row.get("user_id") is not None:
            login = mapped(User, "users", row["user_id"])
            if login is None:
                report.violation(source_id, "user_id", f"login {row['user_id']} was not imported; not linked")
            else:
                holder = Employee.all_objects.filter(user=login).exclude(pk=mapped_id("employees", source_id) or 0).first()
                if holder is not None:
                    report.violation(source_id, "user_id", f"login already linked to employee {holder.code}; not linked")
                    login = None
                elif not row.get("is_active", True) and login.is_active:
                    report.violation(source_id, "is_active", "inactive employee with an active login (eSSL deactivation kept logins); deactivate it again to retire the login")
        values = {
            "code": code[:50],
            "full_name": full_name[:150],
            "office": references["office"],
            "shift": references["shift"],
            "department": _text(row.get("department"))[:100],
            "designation": _text(row.get("designation"))[:100],
            "email": email,
            "phone_e164": phone,
            "joined_on": _date(row.get("joined_on")),
            "identity_method": method,
            "is_active": bool(row.get("is_active", True)),
            "user": login,
        }
        _upsert(Employee, table="employees", source_id=source_id, values=values, report=report, created_at=_dt(row.get("created_at")), updated_at=_dt(row.get("updated_at")))

    return _run(rows, handle, table="employees", object_type="hr.employee", user=user, namespaces=(NS_EMPLOYEES,))


# ----------------------------------------------------------------------------------------------------------------
# Holidays
# ----------------------------------------------------------------------------------------------------------------
def import_holidays(rows: Iterable[dict], *, user=None) -> dict:
    """eSSL ``holidays`` → ``hr_holiday`` (office through the map, null = every office). eSSL let a second global
    holiday onto a date through PATCH; the partial unique refuses it here (reported, skipped)."""

    def handle(row: dict, report: Report) -> None:
        source_id = row.get("id")
        day, name = _date(row.get("holiday_date")), _text(row.get("name"))
        if day is None or not name:
            report.violation(source_id, "row", "holiday_date and name are required")
            raise Skip
        office = None
        if row.get("office_id") is not None:
            office = mapped(Office, "offices", row["office_id"])
            if office is None:
                report.violation(source_id, "office_id", f"office {row['office_id']} was not imported")
                raise Skip
        clash = Holiday.objects.filter(office=office, date=day).exclude(pk=mapped_id("holidays", source_id) or 0).first()
        if clash is not None:
            report.violation(source_id, "holiday_date", f"{day} is already a holiday for this scope ({clash.name}); skipped")
            raise Skip
        values = {"office": office, "date": day, "name": name[:120], "is_active": bool(row.get("is_active", True)), "notes": _text(row.get("notes"))}
        _upsert(Holiday, table="holidays", source_id=source_id, values=values, report=report, created_at=_dt(row.get("created_at")), updated_at=_dt(row.get("updated_at")))

    return _run(rows, handle, table="holidays", object_type="hr.holiday", user=user, namespaces=(NS_SETUP,))


# ----------------------------------------------------------------------------------------------------------------
# Leave
# ----------------------------------------------------------------------------------------------------------------
def leave_type_code(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", text.upper()).strip("_")[:40] or "LEAVE"


def import_leave_types(rows: Iterable[dict], *, user=None) -> dict:
    """One ``hr_leave_type`` per distinct eSSL ``leave_records.leave_type`` string. Strings that differ only in case
    or punctuation share a type (``Sick``/``sick`` → ``SICK``, reported). Paid and approval default to true."""
    strings = sorted({_text(row.get("leave_type")) or "LEAVE" for row in rows})

    def handle(text: str, report: Report) -> None:
        if mapped_id(LEAVE_TYPE_TABLE, text) is not None:
            report.skipped += 1
            return
        code = leave_type_code(text)
        existing = LeaveType.objects.filter(code__iexact=code).first()
        if existing is not None:
            _remember(LEAVE_TYPE_TABLE, text, existing)
            report.violation(text, "leave_type", f"merged into leave type {existing.code} ({existing.name})")
            report.skipped += 1
            return
        leave_type = LeaveType(code=code, name=text[:120], paid=True, requires_approval=True)
        leave_type.save()
        _remember(LEAVE_TYPE_TABLE, text, leave_type)
        report.created += 1

    return _run(
        [{"id": text, "leave_type": text} for text in strings],
        lambda row, report: handle(row["leave_type"], report),
        table=LEAVE_TYPE_TABLE,
        object_type="hr.leave_type",
        user=user,
        namespaces=(NS_SETUP,),
    )


def import_leave_records(rows: Iterable[dict], *, user=None) -> dict:
    """eSSL ``leave_records`` → ``hr_leave_record``: employee and type through the maps, status 1:1; a multi-day
    "half day" becomes full days (reported); overlaps eSSL allowed are imported and reported."""

    def handle(row: dict, report: Report) -> None:
        source_id = row.get("id")
        employee = mapped(Employee, "employees", row.get("employee_id"))
        if employee is None:
            report.violation(source_id, "employee_id", f"employee {row.get('employee_id')} was not imported")
            raise Skip
        leave_type = mapped(LeaveType, LEAVE_TYPE_TABLE, _text(row.get("leave_type")) or "LEAVE")
        if leave_type is None:
            report.violation(source_id, "leave_type", f"leave type {row.get('leave_type')!r} was not imported (run import_leave_types first)")
            raise Skip
        date_from, date_to = _date(row.get("date_from")), _date(row.get("date_to"))
        if date_from is None or date_to is None or date_to < date_from:
            report.violation(source_id, "date_to", f"invalid range {row.get('date_from')} – {row.get('date_to')}")
            raise Skip
        status = _text(row.get("status")).upper()
        if status not in LEAVE_STATUSES:
            report.violation(source_id, "status", f"unknown status {row.get('status')!r}")
            raise Skip
        half = bool(row.get("is_half_day"))
        if half and date_to != date_from:
            report.violation(source_id, "is_half_day", f"a half day over {date_from} – {date_to}; imported as full days")
            half = False
        if status in ("PENDING", "APPROVED"):
            clash = (
                LeaveRecord.objects.filter(employee=employee, status__in=("PENDING", "APPROVED"), date_from__lte=date_to, date_to__gte=date_from)
                .exclude(pk=mapped_id("leave_records", source_id) or 0)
                .first()
            )
            if clash is not None:
                report.violation(source_id, "date_from", f"overlaps {clash.status.lower()} leave {clash.date_from} – {clash.date_to} (imported as in eSSL)")
        values = {
            "employee": employee,
            "leave_type": leave_type,
            "date_from": date_from,
            "date_to": date_to,
            "is_half_day": half,
            "status": status,
            "reason": _text(row.get("reason")),
            "decided_at": _dt(row.get("updated_at")) if status != "PENDING" else None,
        }
        _upsert(LeaveRecord, table="leave_records", source_id=source_id, values=values, report=report, created_at=_dt(row.get("created_at")), updated_at=_dt(row.get("updated_at")))

    return _run(rows, handle, table="leave_records", object_type="hr.leave_record", user=user, namespaces=(NS_LEAVE,))


# ----------------------------------------------------------------------------------------------------------------
# Attendance rules
# ----------------------------------------------------------------------------------------------------------------
def import_attendance_rules(rows: Iterable[dict], *, user=None) -> dict:
    """eSSL ``attendance_rules`` → ``hr_attendance_rule``: only the recognised keys survive (the others and invalid
    values are dropped, reported); a rule naming both an office and a shift keeps the shift (eSSL applied it as a
    shift rule)."""

    def handle(row: dict, report: Report) -> None:
        source_id = row.get("id")
        name = _text(row.get("name"))
        if not name:
            report.violation(source_id, "name", "name is required")
            raise Skip
        office = mapped(Office, "offices", row.get("office_id")) if row.get("office_id") is not None else None
        shift = mapped(Shift, "shifts", row.get("shift_id")) if row.get("shift_id") is not None else None
        for field, source, target in (("office_id", row.get("office_id"), office), ("shift_id", row.get("shift_id"), shift)):
            if source is not None and target is None:
                report.violation(source_id, field, f"{source} was not imported")
                raise Skip
        if office is not None and shift is not None:
            report.violation(source_id, "office_id", "names both an office and a shift; kept as a shift rule (how eSSL applied it)")
            office = None
        clean, problems = rule_errors(row.get("rules") or {})
        for key, message in problems.items():
            report.violation(source_id, f"rules.{key}", f"dropped: {message}")
        values = {
            "name": name[:120],
            "office": office,
            "shift": shift,
            "rules": clean,
            "effective_from": _date(row.get("effective_from")),
            "is_active": bool(row.get("is_active", True)),
            "notes": _text(row.get("notes")),
        }
        _upsert(AttendanceRule, table="attendance_rules", source_id=source_id, values=values, report=report, created_at=_dt(row.get("created_at")), updated_at=_dt(row.get("updated_at")))

    return _run(rows, handle, table="attendance_rules", object_type="hr.attendance_rule", user=user, namespaces=(NS_SETUP,))


def import_all(tables: dict[str, list[dict]], *, user=None) -> dict[str, dict]:
    """Every eSSL HR table in dependency order (``tables`` keyed by the eSSL table name)."""
    return {
        "users": import_users(tables.get("users", []), roles=tables.get("roles", []), user=user),
        "shifts": import_shifts(tables.get("shifts", []), user=user),
        "offices": import_offices(tables.get("offices", []), user=user),
        "employees": import_employees(tables.get("employees", []), user=user),
        "holidays": import_holidays(tables.get("holidays", []), user=user),
        "leave_types": import_leave_types(tables.get("leave_records", []), user=user),
        "leave_records": import_leave_records(tables.get("leave_records", []), user=user),
        "attendance_rules": import_attendance_rules(tables.get("attendance_rules", []), user=user),
    }
