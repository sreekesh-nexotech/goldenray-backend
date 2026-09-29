"""Checker runs, findings and acknowledgements (PLAN §2.5).

:func:`record_run` stores one checker pass over a subject: ``checks`` is a list of ``(scope, CheckResult)`` — one per
pack for a pack config version, a single ``("", result)`` for a project BOM — and becomes one ``engineering_run`` (the
worst verdict: FAIL > WARN > PASS) with one ``engineering_finding`` per finding (``Finding.as_row()``, the scope in
``context.pack``). Each finding carries an ``identity`` (``<scope>|<rule>|<component ids>``): an acknowledgement given
on any run of a subject counts for the same finding in later runs of that subject (:func:`acknowledged_identities`),
so re-running the checker never loses what engineering already accepted.

:func:`acknowledge` (``engineering.approve``) accepts a WARN/INFO finding or waives a BLOCK one (PLAN §7.6 #8 "the
BLOCK list is reviewed and either empty or explicitly accepted"); a reason is always required.
"""

from __future__ import annotations

from collections.abc import Iterable

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.services import record
from core.errors import Conflict, DomainError
from core.outbox import emit
from core.services import check_version, stamp_create
from engineering.models import Acknowledgement, Finding, RuleSet, Run, RunResult
from engineering.services.rule_sets import CACHE_NAMESPACE
from engines.engineering_checker import CheckResult
from flarize.cache_utils import bump

ORDER = {RunResult.PASS: 0, RunResult.WARN: 1, RunResult.FAIL: 2}


def identity(scope: str, rule_code: str, component_ids: Iterable) -> str:
    return f"{scope}|{rule_code}|{'+'.join(str(c) for c in component_ids)}"[:255]


def runs_queryset():
    return Run.objects.select_related("rule_set")


def findings_queryset():
    return Finding.objects.select_related("run").prefetch_related("acknowledgements__acknowledged_by")


def acknowledged_identities(subject_type: str, subject_uid) -> set[str]:
    rows = Acknowledgement.objects.filter(finding__run__subject_type=subject_type, finding__run__subject_uid=subject_uid, finding__deleted_at__isnull=True)
    return set(rows.values_list("finding__identity", flat=True))


@transaction.atomic
def record_run(*, rule_set: RuleSet, subject_type: str, subject_uid, subject_label: str, checks: list[tuple[str, CheckResult]], user, extra_summary: dict | None = None) -> Run:
    result = RunResult.PASS
    per_scope = {}
    counts = {"blocked": 0, "warning": 0, "info": 0}
    for scope, check in checks:
        if ORDER[RunResult(check.result.value)] > ORDER[result]:
            result = RunResult(check.result.value)
        summary = check.summary()
        per_scope[scope] = {"status": summary["status"], "counts": summary["counts"], "deterministicKey": summary["deterministicKey"]}
        for name in counts:
            counts[name] += summary["counts"].get(name, 0)
    run = Run(
        rule_set=rule_set,
        subject_type=subject_type,
        subject_uid=subject_uid,
        subject_label=subject_label[:120],
        result=result,
        summary={"rulesVersion": rule_set.rules_version, "counts": counts, "scopes": per_scope, **(extra_summary or {})},
    )
    stamp_create(run, user)
    run.save()
    rows = []
    for scope, check in checks:
        for finding in check.findings:
            row = finding.as_row()
            context = dict(row["context"])
            if scope:
                context["pack"] = scope
            item = Finding(
                run=run,
                rule_code=row["rule_code"],
                severity=row["severity"],
                message=row["message"],
                context=context,
                identity=identity(scope, row["rule_code"], context.get("componentIds") or ()),
                sort_order=len(rows),
            )
            stamp_create(item, user)
            rows.append(item)
    Finding.objects.bulk_create(rows)
    record(
        "engineering.run_recorded",
        obj=run,
        actor=user,
        after={"subject_type": subject_type, "subject_uid": str(subject_uid), "result": result, "counts": counts, "rules_version": rule_set.rules_version},
    )
    emit(
        "engineering.run_recorded",
        {"run_uid": str(run.uid), "subject_type": subject_type, "subject_uid": str(subject_uid), "result": result, "rules_version": rule_set.rules_version},
        aggregate_type="engineering.run",
        aggregate_uid=run.uid,
    )
    bump(CACHE_NAMESPACE)
    return run


@transaction.atomic
def acknowledge(finding: Finding, *, user, reason: str, expected_version=None) -> Acknowledgement:
    locked = Finding.objects.select_for_update().select_related("run").get(pk=finding.pk)
    check_version(locked, expected_version)
    reason = (reason or "").strip()
    if not reason:
        raise DomainError("validation_error", "A reason is required.", errors={"reason": ["This field may not be blank."]})
    if Acknowledgement.objects.filter(finding=locked).exists():
        raise Conflict("finding_already_acknowledged", "This finding is already acknowledged.")
    ack = Acknowledgement(finding=locked, acknowledged_by=user if getattr(user, "pk", None) else None, reason=reason, at=timezone.now())
    stamp_create(ack, user)
    try:
        with transaction.atomic():
            ack.save()
    except IntegrityError:
        raise Conflict("finding_already_acknowledged", "This finding is already acknowledged.") from None
    locked.versioned_update(user)
    action = "engineering.finding_waived" if locked.severity == "BLOCK" else "engineering.finding_acknowledged"
    record(action, obj=locked, actor=user, after={"rule_code": locked.rule_code, "severity": locked.severity, "identity": locked.identity, "reason": reason})
    emit(
        "engineering.finding_acknowledged",
        {
            "finding_uid": str(locked.uid),
            "run_uid": str(locked.run.uid),
            "subject_type": locked.run.subject_type,
            "subject_uid": str(locked.run.subject_uid),
            "rule_code": locked.rule_code,
            "severity": locked.severity,
        },
        aggregate_type="engineering.finding",
        aggregate_uid=locked.uid,
    )
    bump(CACHE_NAMESPACE)
    return ack
