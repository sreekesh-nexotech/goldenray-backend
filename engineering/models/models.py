from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import BaseModel
from engineering.models.choices import Engine, RunResult, Severity, SubjectType, in_choices

LIVE = Q(deleted_at__isnull=True)


class RuleSet(BaseModel):
    rules_version = models.CharField(max_length=16, help_text="PLAN `version` (e.g. phase1e.1); renamed: `version` is the optimistic lock.")
    engine = models.CharField(max_length=24, choices=Engine.choices, default=Engine.CHECKER)
    rules = models.JSONField(help_text="engines.engineering_checker.RuleSet.as_json(): {engine, version, rules[]} with severities and parameters.")
    active = models.BooleanField(default=False)
    activated_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True, default="")

    class Meta:
        db_table = "engineering_rule_set"
        ordering = ["engine", "-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["rules_version"], condition=LIVE, name="engineering_rule_set_version_uniq"),
            models.UniqueConstraint(fields=["engine"], condition=Q(active=True) & LIVE, name="engineering_rule_set_one_active"),
            models.CheckConstraint(condition=in_choices("engine", Engine), name="engineering_rule_set_engine_valid"),
            models.CheckConstraint(condition=Q(rules_version__regex=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$"), name="engineering_rule_set_version_format"),
        ]

    def __str__(self) -> str:
        return f"{self.engine} {self.rules_version}{' (active)' if self.active else ''}"


class Run(BaseModel):
    # PROTECT: a run keeps the rule set it was judged by.
    rule_set = models.ForeignKey(RuleSet, on_delete=models.PROTECT, related_name="runs")
    subject_type = models.CharField(max_length=24, choices=SubjectType.choices)
    subject_uid = models.UUIDField()
    subject_label = models.CharField(max_length=120, blank=True, default="")
    result = models.CharField(max_length=8, choices=RunResult.choices)
    summary = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "engineering_run"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=in_choices("subject_type", SubjectType), name="engineering_run_subject_type_valid"),
            models.CheckConstraint(condition=in_choices("result", RunResult), name="engineering_run_result_valid"),
        ]
        indexes = [
            models.Index(fields=["subject_type", "subject_uid", "-created_at"], name="engineering_run_subject_idx"),
            models.Index(fields=["result", "-created_at"], name="engineering_run_result_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.subject_type} {self.subject_label or self.subject_uid}: {self.result}"


class Finding(BaseModel):
    # CASCADE: a finding is a true child of its run.
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="findings")
    rule_code = models.CharField(max_length=16)
    severity = models.CharField(max_length=8, choices=Severity.choices)
    message = models.TextField()
    context = models.JSONField(default=dict, blank=True)
    identity = models.CharField(max_length=255, help_text="rule|components|pack — stable across runs of one subject (acknowledgements carry over).")
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "engineering_finding"
        ordering = ["run_id", "sort_order", "id"]
        constraints = [models.CheckConstraint(condition=in_choices("severity", Severity), name="engineering_finding_severity_valid")]
        indexes = [
            models.Index(fields=["run", "severity"], name="engineering_finding_run_sev"),
            models.Index(fields=["identity"], name="engineering_finding_identity"),
        ]

    def __str__(self) -> str:
        return f"{self.rule_code} {self.severity}"


class Acknowledgement(BaseModel):
    # CASCADE: an acknowledgement is a true child of its finding.
    finding = models.ForeignKey(Finding, on_delete=models.CASCADE, related_name="acknowledgements")
    # SET_NULL: attribution only.
    acknowledged_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reason = models.TextField()
    at = models.DateTimeField()

    class Meta:
        db_table = "engineering_acknowledgement"
        ordering = ["-at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["finding"], condition=LIVE, name="engineering_acknowledgement_one_per_finding"),
            models.CheckConstraint(condition=~Q(reason=""), name="engineering_acknowledgement_reason_required"),
        ]

    def __str__(self) -> str:
        return f"ack {self.finding_id}"
