"""Website shapes of the EMI calculator (``/api/public/v1/calculators/emi*``).

The payloads are the legacy ones key for key (``EMICalculatorConfigAPIView``, ``EMICalculatorAPIView``,
``EMIQuotationAPIView``) with ``uid`` for the integer ``id`` (``size_uid``, ``rule_uid``); percentages print in
percent as the legacy did (``"interest_rate": "5.65"``) although they are stored as fractions. The request
serializers document the inputs: the endpoints read them as the legacy views did (any JSON value; a text or a number
where a number is expected), so the engine — not DRF — validates them.
"""

from __future__ import annotations

from decimal import Decimal

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from emi.services.calculator import percent


@extend_schema_field(OpenApiTypes.NUMBER)
class WholeOrDecimalField(serializers.Field):
    """A ₹ amount printed as a JSON integer when whole (the legacy JSON list held ``5000``), else as a number."""

    def to_representation(self, value):
        value = Decimal(value)
        return int(value) if value == value.to_integral_value() else float(value)


class PercentField(serializers.DecimalField):
    """A stored fraction printed as the legacy percentage string (``0.1000`` → ``"10.00"``)."""

    def __init__(self, **kwargs):
        super().__init__(max_digits=6, decimal_places=2, **kwargs)

    def to_representation(self, value):
        return super().to_representation(percent(value))


class PublicEmiSettingsSerializer(serializers.Serializer):
    tenure_min_years = serializers.IntegerField()
    tenure_max_years = serializers.IntegerField()
    tenure_default_years = serializers.IntegerField()
    daily_saving_divisor = serializers.IntegerField()
    price_step = serializers.DecimalField(max_digits=14, decimal_places=2)
    down_payment_min_percent = PercentField(source="down_payment_min_pct")
    down_payment_max_percent = PercentField(source="down_payment_max_pct")
    down_payment_step_percent = PercentField(source="down_payment_step_pct")
    down_payment_quick_adds = serializers.ListField(child=WholeOrDecimalField())
    rate_max = PercentField()
    default_interest_rate = PercentField(source="default_annual_rate")
    panel_life_years = serializers.IntegerField()
    updated_at = serializers.DateTimeField(allow_null=True)
    disclaimer_en = serializers.CharField()
    disclaimer_ml = serializers.CharField()


class PublicEmiSystemSizeSerializer(serializers.Serializer):
    uid = serializers.CharField()
    label = serializers.CharField()
    capacity_kw = serializers.DecimalField(max_digits=6, decimal_places=2)
    price_per_kw = serializers.DecimalField(max_digits=14, decimal_places=2)
    system_cost = serializers.SerializerMethodField()
    price_min = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    price_max = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    monthly_bill_reference = serializers.DecimalField(max_digits=14, decimal_places=2)
    sort_order = serializers.IntegerField()
    is_active = serializers.BooleanField()
    created_at = serializers.DateTimeField(allow_null=True)
    updated_at = serializers.DateTimeField(allow_null=True)

    def get_system_cost(self, option) -> float:
        """``price_per_kw × capacity_kw`` (a pack release gives its lump-sum price)."""
        return float(option.system_cost if option.system_cost is not None else option.price_per_kw * option.capacity_kw)


class PublicEmiBankSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    name = serializers.CharField()
    abbr = serializers.CharField()
    slug = serializers.CharField()
    logo_bg = serializers.CharField()
    interest_rate = PercentField(source="annual_rate")
    min_loan = serializers.DecimalField(max_digits=14, decimal_places=2)
    max_loan = serializers.DecimalField(max_digits=14, decimal_places=2)
    upfront_requirement = serializers.CharField()
    eligibility = serializers.CharField()
    cibil_required = serializers.IntegerField()
    processing_fee_percent = PercentField(source="processing_fee_pct")
    processing_fee_note = serializers.CharField()
    approval_min_days = serializers.IntegerField()
    approval_max_days = serializers.IntegerField()
    max_tenure_years = serializers.IntegerField()
    features = serializers.ListField(child=serializers.CharField())
    best_for = serializers.CharField()
    is_recommended = serializers.BooleanField()
    sort_order = serializers.IntegerField()
    is_active = serializers.BooleanField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class PublicEmiConfigSerializer(serializers.Serializer):
    settings = PublicEmiSettingsSerializer()
    system_sizes = PublicEmiSystemSizeSerializer(many=True)
    banks = PublicEmiBankSerializer(many=True)


# ── request documentation (the engine reads the raw JSON, as the legacy view did) ────────────────────────────────
class EmiCalculateRequestSerializer(serializers.Serializer):
    size_uid = serializers.CharField(required=False, help_text="The size tile (from `config`); or give `capacity_kw`.")
    capacity_kw = serializers.JSONField(required=False, help_text="Size in kW (number or numeric text); alias `power_capacity`.")
    tenure_years = serializers.JSONField(required=False, help_text="Whole years within the settings' band; default the settings' default.")
    interest_rate = serializers.JSONField(required=False, help_text="Requested annual rate (%); raised to the band's floor, ignored on a locked band.")
    system_cost = serializers.JSONField(required=False, help_text="Customer's price (₹), clamped to the size's slider band; alias `price`.")
    down_payment_percent = serializers.JSONField(required=False, help_text="Down payment (%), clamped to the settings' band; default the minimum.")
    apply_down_payment = serializers.JSONField(required=False, help_text="Default true; false = no down payment.")
    apply_subsidy = serializers.JSONField(required=False, help_text="Default true; false = the loan is sized without the subsidy.")


class EmiQuotationPackageSerializer(serializers.Serializer):
    system_cost = serializers.JSONField(help_text="Package price (₹).")
    subsidy = serializers.JSONField(required=False, help_text="Subsidy the quotation applies (₹, default 0).")


class EmiQuotationRequestSerializer(serializers.Serializer):
    capacity_kw = serializers.JSONField(help_text="The quoted system size (kW).")
    tenure_years = serializers.JSONField(required=False, help_text="Default 10.")
    packages = serializers.DictField(child=EmiQuotationPackageSerializer(), help_text="{key: {system_cost, subsidy}}, at most 6 packages.")


# ── response documentation ────────────────────────────────────────────────────────────────────────────────────────
class _SystemSerializer(serializers.Serializer):
    size_uid = serializers.CharField(allow_null=True)
    label = serializers.CharField(allow_null=True)
    capacity_kw = serializers.FloatField()
    price_per_kw = serializers.FloatField()
    system_cost = serializers.FloatField()
    price_min = serializers.FloatField()
    price_max = serializers.FloatField()
    price_source = serializers.ChoiceField(choices=["computed", "customer"])
    monthly_bill_reference = serializers.FloatField()


class _DownPaymentSerializer(serializers.Serializer):
    applied = serializers.BooleanField()
    percent = serializers.FloatField()
    amount = serializers.FloatField()
    min_percent = serializers.FloatField()
    max_percent = serializers.FloatField()
    step_percent = serializers.FloatField()
    min_amount = serializers.FloatField()
    max_amount = serializers.FloatField()
    quick_add_amounts = serializers.ListField(child=serializers.FloatField())


class _SubsidySerializer(serializers.Serializer):
    applied = serializers.BooleanField()
    amount = serializers.FloatField()
    net_cost_after_subsidy = serializers.FloatField()


class _LoanSerializer(serializers.Serializer):
    amount = serializers.FloatField()


class _UnlockSerializer(serializers.Serializer):
    rate = serializers.FloatField()
    extra_down_payment = serializers.FloatField()
    down_payment_amount = serializers.FloatField()
    down_payment_percent = serializers.FloatField()
    rule_label = serializers.CharField()


class _InterestSerializer(serializers.Serializer):
    rate = serializers.FloatField()
    basis_amount = serializers.FloatField()
    base_rate = serializers.FloatField()
    min_rate = serializers.FloatField()
    is_locked = serializers.BooleanField()
    requested_rate = serializers.FloatField(allow_null=True)
    rule_uid = serializers.CharField(allow_null=True)
    rule_label = serializers.CharField(allow_null=True)
    unlock = _UnlockSerializer(allow_null=True, help_text="The cheaper band reachable by paying more upfront, if any.")


class _TenureSerializer(serializers.Serializer):
    years = serializers.IntegerField()
    months = serializers.IntegerField()


class _ResultSerializer(serializers.Serializer):
    emi_per_month = serializers.FloatField()
    total_payment = serializers.FloatField()
    total_interest = serializers.FloatField()
    daily_amount = serializers.FloatField()
    daily_saving_divisor = serializers.IntegerField()
    monthly_savings = serializers.FloatField()


class EmiBreakdownSerializer(serializers.Serializer):
    system = _SystemSerializer()
    down_payment = _DownPaymentSerializer()
    subsidy = _SubsidySerializer()
    loan = _LoanSerializer()
    interest = _InterestSerializer()
    tenure = _TenureSerializer()
    result = _ResultSerializer()
    power_capacity_kW = serializers.FloatField()  # noqa: N815 - legacy key
    total_cost = serializers.FloatField()
    total_subsidy = serializers.FloatField()
    final_cost = serializers.FloatField()
    principal = serializers.FloatField()
    interest_rate = serializers.FloatField()
    interest_rate_min = serializers.FloatField()
    interest_rate_locked = serializers.BooleanField()
    tenure_years = serializers.IntegerField()
    tenure_months = serializers.IntegerField()
    emi_per_month = serializers.FloatField()
    total_payment = serializers.FloatField()
    total_interest = serializers.FloatField()
    daily_amount = serializers.FloatField()


class EmiQuotationPackageResultSerializer(serializers.Serializer):
    system_cost = serializers.FloatField()
    down_payment_percent = serializers.FloatField()
    down_payment = serializers.FloatField()
    subsidy = serializers.FloatField()
    rate_basis = serializers.FloatField()
    loan_amount = serializers.FloatField()
    interest_rate = serializers.FloatField()
    rule_label = serializers.CharField(allow_null=True)
    tenure_years = serializers.IntegerField()
    emi_per_month = serializers.FloatField()
    daily_amount = serializers.FloatField()
    daily_saving_divisor = serializers.IntegerField()


class EmiQuotationResponseSerializer(serializers.Serializer):
    tenure_years = serializers.IntegerField()
    packages = serializers.DictField(child=EmiQuotationPackageResultSerializer())
