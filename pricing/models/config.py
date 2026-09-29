"""Commercial configuration (PLAN §2.3): cost config effective rows, installation matrix, statutory fees, validity.

``pricing_cost_config`` — one row per (key, effective window); the open row (``effective_to IS NULL``) is the current
value, a PUT closes it and appends the next one in the same transaction. ``value`` is JSONB and validated per key by
``pricing.services.cost_config.KEYS`` (scalars, fractions, or whole validated documents such as the Flarize
cost-engine configuration and the Project Head rate card).

``pricing_installation_matrix`` — exact install cost per (size, phase, installation type). The Flarize matrix is keyed
by roof type as well, so ``installation_type`` is part of the key (DV).

``pricing_statutory_fee`` — KSEB registration/meter/net-meter fees (the Purchase Agreement ``kseb`` list).

``pricing_validity_policy`` — the quotation validity: one DEFAULT row per key (PLAN) plus Flarize's effective windows
(``kind`` WINDOW: ACTIVE windows containing an instant, latest ``effective_from`` wins; DV).
"""

from django.db import models
from django.db.models import F, Q

from core.models import BaseModel
from pricing.models.choices import LIVE, InstallationType, Phase, StatutoryFeeKind, ValidityKind, ValidityWindowStatus, in_choices

KEY_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$"


class CostConfig(BaseModel):
    key = models.CharField(max_length=48)
    value = models.JSONField()
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    note = models.TextField(blank=True, default="", help_text="Why the value changed.")
    source_ref = models.CharField(max_length=64, blank=True, default="", help_text="Import source (e.g. FLARIZE:cost-config.json).")

    class Meta:
        db_table = "pricing_cost_config"
        ordering = ["key", "-effective_from", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["key"], condition=LIVE & Q(effective_to__isnull=True), name="pricing_cost_config_one_current_per_key"),
            models.CheckConstraint(condition=Q(key__regex=KEY_PATTERN), name="pricing_cost_config_key_format"),
            models.CheckConstraint(condition=Q(effective_to__isnull=True) | Q(effective_to__gte=F("effective_from")), name="pricing_cost_config_effective_window"),
        ]
        indexes = [models.Index(F("key"), F("effective_from").desc(), name="pricing_cost_config_key_from")]

    def __str__(self) -> str:
        return f"{self.key} from {self.effective_from}"


class InstallationMatrix(BaseModel):
    size_kw = models.DecimalField(max_digits=6, decimal_places=2)
    phase = models.CharField(max_length=4, blank=True, default="", choices=Phase.choices)
    installation_type = models.CharField(max_length=10, choices=InstallationType.choices, default=InstallationType.FLAT)
    install_cost = models.DecimalField(max_digits=14, decimal_places=2)
    labour_days = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "pricing_installation_matrix"
        ordering = ["size_kw", "phase", "installation_type", "id"]
        constraints = [
            models.UniqueConstraint(fields=["size_kw", "phase", "installation_type"], condition=LIVE, name="pricing_installation_matrix_cell_uniq"),
            models.CheckConstraint(condition=in_choices("phase", Phase), name="pricing_installation_matrix_phase_valid"),
            models.CheckConstraint(condition=in_choices("installation_type", InstallationType), name="pricing_installation_matrix_type_valid"),
            models.CheckConstraint(condition=Q(size_kw__gt=0), name="pricing_installation_matrix_size_positive"),
            models.CheckConstraint(condition=Q(install_cost__gte=0), name="pricing_installation_matrix_cost_not_negative"),
            models.CheckConstraint(condition=Q(labour_days__isnull=True) | Q(labour_days__gte=0), name="pricing_installation_matrix_days_not_negative"),
        ]

    def __str__(self) -> str:
        return f"{self.size_kw} kW {self.phase or 'any'} {self.installation_type} = {self.install_cost}"

    @property
    def size_key(self) -> str:
        """The Flarize matrix key (``3``, ``5sp``, ``5tp``)."""
        number = self.size_kw.normalize()
        text = format(number, "f")
        return text + {"1P": "sp", "3P": "tp"}.get(self.phase, "")


class StatutoryFee(BaseModel):
    kind = models.CharField(max_length=24, choices=StatutoryFeeKind.choices)
    label = models.CharField(max_length=120)
    phase = models.CharField(max_length=4, null=True, blank=True, help_text="1P / 3P; null = any phase.")
    capacity_kw_max = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True, help_text="Applies up to this capacity; null = any.")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "pricing_statutory_fee"
        ordering = ["kind", "phase", "capacity_kw_max", "-effective_from", "id"]
        constraints = [
            models.UniqueConstraint(fields=["kind", "phase", "capacity_kw_max"], condition=LIVE & Q(effective_to__isnull=True), nulls_distinct=False, name="pricing_statutory_fee_one_current"),
            models.CheckConstraint(condition=in_choices("kind", StatutoryFeeKind), name="pricing_statutory_fee_kind_valid"),
            models.CheckConstraint(condition=Q(phase__isnull=True) | Q(phase__in=["1P", "3P"]), name="pricing_statutory_fee_phase_valid"),
            models.CheckConstraint(condition=Q(amount__gte=0), name="pricing_statutory_fee_amount_not_negative"),
            models.CheckConstraint(condition=Q(capacity_kw_max__isnull=True) | Q(capacity_kw_max__gt=0), name="pricing_statutory_fee_capacity_positive"),
            models.CheckConstraint(condition=Q(effective_to__isnull=True) | Q(effective_to__gte=F("effective_from")), name="pricing_statutory_fee_effective_window"),
            models.CheckConstraint(condition=~Q(label=""), name="pricing_statutory_fee_label_not_blank"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.label} = {self.amount}"


class ValidityPolicy(BaseModel):
    key = models.CharField(max_length=16, default="QUOTATION")
    kind = models.CharField(max_length=8, choices=ValidityKind.choices, default=ValidityKind.DEFAULT)
    days = models.PositiveSmallIntegerField()
    grace_days = models.PositiveSmallIntegerField(default=0)
    effective_from = models.DateTimeField()
    effective_to = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=8, choices=ValidityWindowStatus.choices, default=ValidityWindowStatus.ACTIVE)
    policy_id = models.CharField(max_length=64, blank=True, default="", help_text="WINDOW: the policy id (Flarize policyId).")
    version_label = models.CharField(max_length=80, blank=True, default="", help_text="Source version (Flarize version).")
    note = models.TextField(blank=True, default="")

    class Meta:
        db_table = "pricing_validity_policy"
        ordering = ["key", "kind", "-effective_from", "policy_id", "id"]
        constraints = [
            models.UniqueConstraint(fields=["key"], condition=LIVE & Q(kind=ValidityKind.DEFAULT), name="pricing_validity_policy_one_default"),
            models.UniqueConstraint(fields=["key", "policy_id"], condition=LIVE & Q(kind=ValidityKind.WINDOW), name="pricing_validity_policy_window_uniq"),
            models.CheckConstraint(condition=Q(key__regex=r"^[A-Z][A-Z0-9_]{0,15}$"), name="pricing_validity_policy_key_format"),
            models.CheckConstraint(condition=in_choices("kind", ValidityKind), name="pricing_validity_policy_kind_valid"),
            models.CheckConstraint(condition=in_choices("status", ValidityWindowStatus), name="pricing_validity_policy_status_valid"),
            models.CheckConstraint(condition=Q(days__gte=1), name="pricing_validity_policy_days_positive"),
            models.CheckConstraint(condition=Q(effective_to__isnull=True) | Q(effective_to__gt=F("effective_from")), name="pricing_validity_policy_window_order"),
            models.CheckConstraint(condition=Q(kind=ValidityKind.DEFAULT, policy_id="") | (Q(kind=ValidityKind.WINDOW) & ~Q(policy_id="")), name="pricing_validity_policy_window_id"),
        ]

    def __str__(self) -> str:
        return f"{self.key}/{self.kind} {self.days} days"
