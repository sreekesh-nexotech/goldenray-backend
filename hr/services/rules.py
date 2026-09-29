"""``hr/attendance-rules/`` — half-day parameters for the engine (module ``hr_setup``: view / edit). eSSL had no API.

``rules`` is JSON validated here and nowhere else (the importer uses the same function): only the recognised keys

* ``half_day_after`` — a clock time ``HH:MM`` (or ``HH:MM:SS``), stored as ``HH:MM[:SS]``; wins when both are set;
* ``half_day_after_minutes`` — whole minutes after the shift start (0 … 720);
* ``half_day_under_minutes`` — whole minutes (0 … 1440): with a late arrival, fewer worked minutes make a half day;

anything else is refused (400, per key). A rule names at most one scope: an office or a shift (400; DB check), none
= global. Names are unique among live rules (409 ``rule_name_taken``). Writes re-compute the affected people from
``effective_from`` (or the look-back window) — both the old and the new scope when it moves.
"""

from __future__ import annotations

from datetime import datetime, time

from django.db import IntegrityError, transaction

from audit.services import changes, record, snapshot
from core.services import stamp_create
from hr.models import AttendanceRule
from hr.services import recompute, validation
from hr.services.common import bump_setup, lock, unique_conflict

RECOGNISED_KEYS = ("half_day_after", "half_day_after_minutes", "half_day_under_minutes")
MINUTE_LIMITS = {"half_day_after_minutes": 720, "half_day_under_minutes": 1440}
EDITABLE_FIELDS = ("name", "office", "shift", "rules", "effective_from", "is_active", "notes")
SNAPSHOT_FIELDS = EDITABLE_FIELDS
UNIQUE = {"hr_attendance_rule_name_live_uniq": ("rule_name_taken", "name", "Another attendance rule already uses this name.")}


def parse_clock(value) -> time | None:
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(value.strip(), fmt).time()
            except ValueError:
                continue
    return None


def rule_errors(payload) -> tuple[dict, dict[str, str]]:
    """``(clean rules, {key: problem})`` — the clean dict keeps only valid recognised keys."""
    if not isinstance(payload, dict):
        return {}, {"rules": "Must be an object."}
    clean, problems = {}, {}
    for key, value in payload.items():
        if key not in RECOGNISED_KEYS:
            problems[key] = f"Unknown key; use {', '.join(RECOGNISED_KEYS)}."
        elif key == "half_day_after":
            parsed = parse_clock(value)
            if parsed is None:
                problems[key] = "Use a clock time HH:MM."
            else:
                clean[key] = parsed.strftime("%H:%M:%S" if parsed.second else "%H:%M")
        elif isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MINUTE_LIMITS[key]:
            problems[key] = f"Use whole minutes between 0 and {MINUTE_LIMITS[key]}."
        else:
            clean[key] = value
    return clean, problems


def validate_rules(payload) -> dict:
    clean, problems = rule_errors(payload)
    if problems:
        raise validation.invalid("rules", "; ".join(f"{key}: {message}" for key, message in problems.items()))
    if not clean:
        raise validation.invalid("rules", f"State at least one of {', '.join(RECOGNISED_KEYS)}.")
    return clean


def rules_queryset():
    return AttendanceRule.objects.select_related("office", "shift").order_by("name", "id")


def rule_snapshot(rule: AttendanceRule) -> dict:
    return snapshot(rule, SNAPSHOT_FIELDS)


def _normalise(values: dict) -> dict:
    if "name" in values:
        values["name"] = (values["name"] or "").strip()
        if not values["name"]:
            raise validation.invalid("name", "This field may not be blank.")
    if "rules" in values:
        values["rules"] = validate_rules(values["rules"])
    if "notes" in values:
        values["notes"] = values["notes"] or ""
    return values


def _check_scope(office, shift) -> None:
    if office is not None and shift is not None:
        raise validation.invalid("shift", "A rule applies to an office or to a shift, not both.")


def _save(action):
    try:
        with transaction.atomic():
            action()
    except IntegrityError as exc:
        raise (unique_conflict(exc, UNIQUE) or exc) from None


def announce(office, shift, effective_from, *, reason: str) -> None:
    """Re-compute whoever the rule scope covers, from ``effective_from`` but at most the look-back window, to today.

    Older history is left alone on purpose (closed months do not change because a rule was edited); it can be
    re-computed explicitly through ``attendance/process/``. A rule that starts in the future changes nothing yet.
    """
    start, today = recompute.recent_range()
    if effective_from is not None:
        if effective_from > today:
            return
        start = max(start, effective_from)
    if shift is not None:
        recompute.for_employees(recompute.employees_on_shift(shift), start, today, reason=reason)
    else:
        recompute.for_office(office.uid if office is not None else None, start, today, reason=reason)


@transaction.atomic
def create_rule(*, user, data) -> AttendanceRule:
    values = _normalise({name: data[name] for name in EDITABLE_FIELDS if name in data})
    if "rules" not in values:
        raise validation.invalid("rules", "This field is required.")
    _check_scope(values.get("office"), values.get("shift"))
    rule = AttendanceRule(**values)
    stamp_create(rule, user)
    _save(rule.save)
    record("hr.attendance_rule_created", obj=rule, actor=user, after=rule_snapshot(rule))
    if rule.is_active:
        announce(rule.office, rule.shift, rule.effective_from, reason="attendance_rule_changed")
    bump_setup()
    return rule


@transaction.atomic
def update_rule(instance: AttendanceRule, *, user, data, expected_version=None) -> AttendanceRule:
    rule = lock(AttendanceRule, instance, expected_version, related=("office", "shift"))
    before = rule_snapshot(rule)
    values = _normalise({name: data[name] for name in EDITABLE_FIELDS if name in data})
    values = {name: value for name, value in values.items() if getattr(rule, name) != value}
    if not values:
        return rule
    _check_scope(values.get("office", rule.office), values.get("shift", rule.shift))
    old = (rule.office, rule.shift, rule.effective_from, rule.is_active)
    _save(lambda: rule.versioned_update(user, **values))
    changed_before, changed_after = changes(before, rule_snapshot(rule))
    record("hr.attendance_rule_updated", obj=rule, actor=user, before=changed_before, after=changed_after)
    if set(values) - {"name", "notes"}:
        old_office, old_shift, old_effective_from, was_active = old
        moved = (rule.office, rule.shift) != (old_office, old_shift)
        if was_active:
            effective = None if old_effective_from is None or rule.effective_from is None else min(old_effective_from, rule.effective_from)
            announce(old_office, old_shift, effective, reason="attendance_rule_changed")
        if rule.is_active and (moved or not was_active):
            announce(rule.office, rule.shift, rule.effective_from, reason="attendance_rule_changed")
    bump_setup()
    return rule


@transaction.atomic
def delete_rule(instance: AttendanceRule, *, user, expected_version=None) -> None:
    rule = lock(AttendanceRule, instance, expected_version, related=("office", "shift"))
    rule.soft_delete(user)
    record("hr.attendance_rule_deleted", obj=rule, actor=user, before=rule_snapshot(rule))
    if rule.is_active:
        announce(rule.office, rule.shift, rule.effective_from, reason="attendance_rule_changed")
    bump_setup()


def applicable_rules(*, office=None, shift=None, on=None, candidates=None):
    """Active rules that apply on ``on`` to people of ``office`` on ``shift``, in merge order (global < office < shift,
    then ``effective_from``) — what the engine merges key by key (eSSL ``rules_for``). ``candidates``: preloaded
    active rules (one query for a whole list)."""
    rules = [
        rule
        for rule in (AttendanceRule.objects.filter(is_active=True) if candidates is None else candidates)
        if (rule.effective_from is None or on is None or rule.effective_from <= on)
        and (rule.shift_id is None or (shift is not None and rule.shift_id == shift.pk))
        and (rule.office_id is None or rule.shift_id is not None or (office is not None and rule.office_id == office.pk))
    ]
    return sorted(rules, key=lambda rule: (2 if rule.shift_id else 1 if rule.office_id else 0, rule.effective_from or datetime.min.date()))


def merged(rules) -> dict:
    result: dict = {}
    for rule in rules:
        clean, _ = rule_errors(rule.rules)
        result.update(clean)
    return result
