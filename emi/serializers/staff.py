"""Staff shapes of the EMI configuration (``emi/*``): read = full row, create/update = the editable columns.

Rates and percentages are fractions (``annual_rate: "0.0575"`` = 5.75 %), as stored. DRF's generated unique
validators are dropped: live uniqueness is the database's job and answers 409 ``<entity>_exists``.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.validators import RegexValidator
from rest_framework import serializers

from core.serializers import ExpectedVersionMixin
from emi.models import Bank, EmiSettings, InterestRateRule, SubsidyRule, SystemSize
from emi.models.config import HEX_COLOUR_REGEX, SLUG_REGEX
from emi.services.rows import BANK_FIELDS, INTEREST_RULE_FIELDS, SUBSIDY_RULE_FIELDS, SYSTEM_SIZE_FIELDS
from emi.services.settings import FIELDS as SETTINGS_FIELDS

BASE_FIELDS = ["uid", "version", "created_at", "updated_at"]
FRACTION = {"min_value": Decimal("0"), "max_value": Decimal("0.9999")}
MONEY = {"min_value": Decimal("0")}
KW = {"min_value": Decimal("0")}
SORT_ORDER = {"min_value": -1000000, "max_value": 1000000}
# The formats the database checks (emi_bank_slug_format, emi_bank_logo_bg_hex) enforce, answered on the field.
SLUG_FORMAT = RegexValidator(SLUG_REGEX, "Use lowercase letters and digits in words joined by single hyphens, e.g. \"state-bank\".")
HEX_COLOUR = RegexValidator(HEX_COLOUR_REGEX, "Use a hex colour, e.g. \"#074A4D\".")

EXTRA_KWARGS = {
    Bank: {
        "slug": {"required": False, "allow_blank": True, "validators": [SLUG_FORMAT]},
        "logo_bg": {"validators": [HEX_COLOUR]},
        "annual_rate": FRACTION,
        "processing_fee_pct": FRACTION,
        "min_loan": MONEY,
        "max_loan": MONEY,
        "cibil_required": {"max_value": 900},
        "sort_order": SORT_ORDER,
    },
    InterestRateRule: {
        "annual_rate": FRACTION,
        "min_annual_rate": FRACTION,
        "min_kw": KW,
        "max_kw": KW,
        "min_system_cost": MONEY,
        "max_system_cost": MONEY,
        "min_amount": MONEY,
        "max_amount": MONEY,
    },
    SubsidyRule: {"amount": MONEY, "amount_per_kw": MONEY, "cap_amount": MONEY, "kw_from": KW, "kw_to": KW},
    SystemSize: {
        "capacity_kw": {"min_value": Decimal("0.01"), "validators": []},
        "price_per_kw": {"min_value": Decimal("0.01")},
        "price_min": MONEY,
        "price_max": MONEY,
        "monthly_bill_reference": MONEY,
        "sort_order": SORT_ORDER,
    },
}


def _meta(model, fields, extra=None, read_only=False):
    attrs = {"model": model, "fields": fields, "validators": [], "extra_kwargs": extra or {}}
    if read_only:
        attrs["read_only_fields"] = fields
    return type("Meta", (), attrs)


def _build(model, fields: tuple[str, ...], prefix: str):
    read = type(f"{prefix}Serializer", (serializers.ModelSerializer,), {"Meta": _meta(model, ["uid", *fields, *BASE_FIELDS[1:]], read_only=True)})

    def to_representation(self, instance):
        return read(instance, context=self.context).data

    create = type(f"{prefix}CreateSerializer", (serializers.ModelSerializer,), {"Meta": _meta(model, list(fields), EXTRA_KWARGS.get(model)), "to_representation": to_representation})
    update = type(f"{prefix}UpdateSerializer", (ExpectedVersionMixin, create), {"Meta": _meta(model, [*fields, "expected_version"], EXTRA_KWARGS.get(model))})
    return read, create, update


SERIALIZERS = {
    "banks": _build(Bank, BANK_FIELDS, "EmiBank"),
    "interest-rules": _build(InterestRateRule, INTEREST_RULE_FIELDS, "EmiInterestRateRule"),
    "subsidy-rules": _build(SubsidyRule, SUBSIDY_RULE_FIELDS, "EmiSubsidyRule"),
    "system-sizes": _build(SystemSize, SYSTEM_SIZE_FIELDS, "EmiSystemSize"),
}


class EmiSettingsSerializer(serializers.ModelSerializer):
    uid = serializers.UUIDField(read_only=True, allow_null=True, help_text="Null while the defaults are in force (no edit yet).")
    updated_at = serializers.DateTimeField(read_only=True, allow_null=True)

    class Meta:
        model = EmiSettings
        fields = ["uid", *SETTINGS_FIELDS, "version", "updated_at"]
        read_only_fields = fields


class EmiSettingsUpdateSerializer(ExpectedVersionMixin, serializers.ModelSerializer):
    down_payment_quick_adds = serializers.ListField(child=serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01")), required=False, max_length=20)

    class Meta:
        model = EmiSettings
        fields = [*SETTINGS_FIELDS, "expected_version"]
        extra_kwargs = {
            "down_payment_min_pct": {"min_value": Decimal("0.0001"), "max_value": Decimal("1")},
            "down_payment_max_pct": {"min_value": Decimal("0.0001"), "max_value": Decimal("1")},
            "down_payment_step_pct": {"min_value": Decimal("0.0001"), "max_value": Decimal("1")},
            "rate_max": FRACTION,
            "default_annual_rate": FRACTION,
            "price_step": {"min_value": Decimal("0.01")},
            "tenure_min_years": {"min_value": 1, "max_value": 50},
            "tenure_max_years": {"min_value": 1, "max_value": 50},
            "tenure_default_years": {"min_value": 1, "max_value": 50},
            "daily_saving_divisor": {"min_value": 1, "max_value": 366},
            "panel_life_years": {"min_value": 1, "max_value": 60},
        }
