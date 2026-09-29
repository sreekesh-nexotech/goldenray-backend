"""Rule sets (PLAN §2.5 ``engineering_rule_set``): read the active one, activate another.

Exactly one rule set is ACTIVE per engine (partial unique index): activating a set deactivates the previous active set
of the same engine in the same transaction. The stored document is validated by
``engines.engineering_checker.RuleSet.from_json`` before it is ever used.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.services import record
from core.errors import Conflict, DomainError, NotFound
from core.outbox import emit
from core.services import check_version
from engineering.models import Engine, RuleSet
from engines.engineering_checker import RuleSet as EngineRuleSet
from engines.engineering_checker import RuleSetError
from flarize.cache_utils import bump

CACHE_NAMESPACE = "engineering"


def rule_sets_queryset():
    return RuleSet.objects.all()


def active_rule_set(engine: str = Engine.CHECKER) -> RuleSet:
    row = RuleSet.objects.filter(engine=engine, active=True).first()
    if row is None:
        raise Conflict("no_active_rule_set", f"No {engine} rule set is active; activate one under engineering/rule-sets/.")
    return row


def engine_rule_set(row: RuleSet) -> EngineRuleSet:
    """The engine's ``RuleSet`` for a stored row (``RuleSetError`` → 400 ``rule_set_invalid``)."""
    try:
        return EngineRuleSet.from_json(row.rules)
    except RuleSetError as exc:
        raise DomainError(
            "rule_set_invalid", f"Rule set {row.rules_version} is not a valid rule document.", errors={"rules": [f"{e['path']}: {e['message']}" for e in exc.errors] or [str(exc)]}
        ) from None


@transaction.atomic
def activate(rule_set: RuleSet, *, user, expected_version=None, note: str = "") -> RuleSet:
    locked = RuleSet.objects.select_for_update().get(pk=rule_set.pk)
    check_version(locked, expected_version)
    if locked.active:
        raise Conflict("rule_set_already_active", f"Rule set {locked.rules_version} is already active.")
    engine_rule_set(locked)  # never activate a document the checker cannot read
    previous = RuleSet.objects.select_for_update().filter(engine=locked.engine, active=True).first()
    if previous is not None:
        previous.versioned_update(user, active=False)
        record("engineering.rule_set_deactivated", obj=previous, actor=user, after={"active": False, "replaced_by": locked.rules_version})
    try:
        with transaction.atomic():
            locked.versioned_update(user, active=True, activated_at=timezone.now())
    except IntegrityError:
        raise Conflict("rule_set_activation_conflict", "Another rule set was activated at the same time; reload.") from None
    record("engineering.rule_set_activated", obj=locked, actor=user, before={"active": False}, after={"active": True, "previous": previous.rules_version if previous else None}, note=note)
    emit(
        "engineering.rule_set_activated",
        {"rule_set_uid": str(locked.uid), "version": locked.rules_version, "engine": locked.engine, "previous_version": previous.rules_version if previous else None},
        aggregate_type="engineering.rule_set",
        aggregate_uid=locked.uid,
    )
    bump(CACHE_NAMESPACE)
    return locked


def get_rule_set(uid) -> RuleSet:
    row = RuleSet.objects.filter(uid=uid).first()
    if row is None:
        raise NotFound("rule_set_not_found", "No such rule set.")
    return row
