"""EMI calculator configuration (PLAN §2.8 ``emi_*``, DV-83).

Every column of the legacy ``goldenray.models.emi_config`` tables has a typed home (the calculator's policy depends
on all of them) and the PLAN's additions are kept where they fit the legacy policy: ``effective_from``/``to`` on
rules, ``scheme``/``amount_per_kw``/``cap_amount`` on subsidy rules, bilingual disclaimers on the settings.
Percentages are stored as fractions (``0.1000`` = 10 %); the public API prints them in percent as the legacy did.
``emi_system_size`` is the transitional price source (``EMI_PRICE_SOURCE = MANUAL``) until pack releases take over.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import F, Q

from core.models import BaseModel

SLUG_REGEX = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
HEX_COLOUR_REGEX = r"^#([0-9A-Fa-f]{3}|[0-9A-Fa-f]{4}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$"


def _fraction(**kwargs) -> models.DecimalField:
    return models.DecimalField(max_digits=6, decimal_places=4, **kwargs)


def _money(**kwargs) -> models.DecimalField:
    return models.DecimalField(max_digits=14, decimal_places=2, **kwargs)


def _kw(**kwargs) -> models.DecimalField:
    return models.DecimalField(max_digits=6, decimal_places=2, **kwargs)


def _ordered(table: str, low: str, high: str) -> models.CheckConstraint:
    return models.CheckConstraint(condition=Q(**{f"{low}__isnull": True}) | Q(**{f"{high}__isnull": True}) | Q(**{f"{low}__lte": F(high)}), name=f"{table}_{low}_le_{high}")


class Bank(BaseModel):
    """``emi_bank``: a lender in the comparison table under the calculator."""

    name = models.CharField(max_length=160)
    abbr = models.CharField(max_length=12, help_text='Logo initials, e.g. "SBI".')
    slug = models.SlugField(max_length=64)
    logo_bg = models.CharField(max_length=9, default="#074A4D", help_text="Hex colour behind the logo initials.")
    annual_rate = _fraction(help_text="Headline annual rate as a fraction (0.0565 = 5.65 %), used for sorting and display.")
    min_loan = _money(default=Decimal("0"))
    max_loan = _money(default=Decimal("0"), help_text="0 = no published maximum.")
    upfront_requirement = models.CharField(max_length=160, blank=True, default="")
    eligibility = models.CharField(max_length=255, blank=True, default="")
    cibil_required = models.PositiveSmallIntegerField(default=0, help_text="0 = no published minimum.")
    processing_fee_pct = _fraction(default=Decimal("0"), help_text="Processing fee as a fraction of the loan.")
    processing_fee_note = models.CharField(max_length=120, blank=True, default="", help_text='Shown instead of the percentage when set, e.g. "₹500 flat".')
    approval_min_days = models.PositiveSmallIntegerField(default=0)
    approval_max_days = models.PositiveSmallIntegerField(default=0)
    max_tenure_years = models.PositiveSmallIntegerField(default=0)
    features = ArrayField(models.CharField(max_length=255), default=list, blank=True, help_text="Bullet points shown on the bank's card.")
    best_for = models.CharField(max_length=255, blank=True, default="")
    is_recommended = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "emi_bank"
        ordering = ["sort_order", "annual_rate", "id"]
        constraints = [
            models.UniqueConstraint(fields=["slug"], condition=Q(deleted_at__isnull=True), name="emi_bank_slug_live_uniq"),
            models.CheckConstraint(condition=Q(slug__regex=SLUG_REGEX), name="emi_bank_slug_format"),
            models.CheckConstraint(condition=Q(logo_bg__regex=HEX_COLOUR_REGEX), name="emi_bank_logo_bg_hex"),
            models.CheckConstraint(condition=Q(annual_rate__gte=0) & Q(annual_rate__lt=1) & Q(processing_fee_pct__gte=0) & Q(processing_fee_pct__lt=1), name="emi_bank_rates_fraction"),
            models.CheckConstraint(condition=Q(min_loan__gte=0) & Q(max_loan__gte=0), name="emi_bank_loans_non_negative"),
            models.CheckConstraint(condition=Q(max_loan=0) | Q(min_loan__lte=F("max_loan")), name="emi_bank_loan_band_ordered"),
            models.CheckConstraint(condition=Q(approval_max_days=0) | Q(approval_min_days__lte=F("approval_max_days")), name="emi_bank_approval_days_ordered"),
        ]

    def __str__(self) -> str:
        return self.name


class InterestRateRule(BaseModel):
    """``emi_interest_rate_rule``: the rate for a capacity, system-cost and/or loan band.

    Every band is optional (an unset band never rejects); the highest ``priority`` wins, then the rule with more bounds.
    The loan band (``min_amount``/``max_amount``) is compared with price − down payment (the rate basis).
    """

    label = models.CharField(max_length=120)
    min_kw = _kw(null=True, blank=True)
    max_kw = _kw(null=True, blank=True)
    min_system_cost = _money(null=True, blank=True)
    max_system_cost = _money(null=True, blank=True)
    min_amount = _money(null=True, blank=True, help_text="Loan band lower bound (price − down payment), inclusive.")
    max_amount = _money(null=True, blank=True, help_text="Loan band upper bound, inclusive.")
    annual_rate = _fraction(help_text="Starting annual rate as a fraction.")
    min_annual_rate = _fraction(help_text="Floor: a customer-requested rate is raised to it.")
    is_locked = models.BooleanField(default=False, help_text="Locked rules ignore a customer-requested rate.")
    priority = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    effective_from = models.DateField(null=True, blank=True, help_text="Empty = in force since before the platform.")
    effective_to = models.DateField(null=True, blank=True, help_text="Last day in force; empty = open-ended.")

    class Meta:
        db_table = "emi_interest_rate_rule"
        ordering = ["-priority", F("min_kw").asc(nulls_last=True), F("min_system_cost").asc(nulls_last=True), F("min_amount").asc(nulls_last=True), "id"]
        constraints = [
            _ordered("emi_interest_rate_rule", "min_kw", "max_kw"),
            _ordered("emi_interest_rate_rule", "min_system_cost", "max_system_cost"),
            _ordered("emi_interest_rate_rule", "min_amount", "max_amount"),
            _ordered("emi_interest_rate_rule", "effective_from", "effective_to"),
            models.CheckConstraint(condition=Q(min_annual_rate__gte=0) & Q(annual_rate__lt=1) & Q(annual_rate__gte=F("min_annual_rate")), name="emi_interest_rate_rule_rates_valid"),
        ]

    def __str__(self) -> str:
        return self.label


class SubsidyScheme(models.TextChoices):
    PM_SURYA_GHAR = "PM_SURYA_GHAR", "PM Surya Ghar"
    OTHER = "OTHER", "Other"


class SubsidyRule(BaseModel):
    """``emi_subsidy_rule``: the subsidy for a capacity band (``kw_from`` ≤ kW ≤ ``kw_to``; highest priority wins).

    The subsidy is ``amount`` + ``amount_per_kw`` × kW, capped at ``cap_amount`` — the legacy rules are a flat
    ``amount`` (``amount_per_kw`` 0, no cap).
    """

    scheme = models.CharField(max_length=24, choices=SubsidyScheme.choices, default=SubsidyScheme.PM_SURYA_GHAR)
    label = models.CharField(max_length=120)
    kw_from = _kw(null=True, blank=True, help_text="Inclusive lower bound; empty = no lower bound.")
    kw_to = _kw(null=True, blank=True, help_text="Inclusive upper bound; empty = no upper bound.")
    amount = _money(help_text="Flat subsidy (₹).")
    amount_per_kw = _money(default=Decimal("0"), help_text="Added per kW of the system.")
    cap_amount = _money(null=True, blank=True, help_text="Upper limit of the subsidy; empty = none.")
    priority = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "emi_subsidy_rule"
        ordering = ["-priority", F("kw_from").asc(nulls_last=True), "id"]
        constraints = [
            models.CheckConstraint(condition=Q(scheme__in=SubsidyScheme.values), name="emi_subsidy_rule_scheme_valid"),
            _ordered("emi_subsidy_rule", "kw_from", "kw_to"),
            _ordered("emi_subsidy_rule", "effective_from", "effective_to"),
            models.CheckConstraint(condition=Q(amount__gte=0) & Q(amount_per_kw__gte=0) & (Q(cap_amount__isnull=True) | Q(cap_amount__gte=0)), name="emi_subsidy_rule_money_non_negative"),
        ]

    def __str__(self) -> str:
        return self.label


class EmiSettings(BaseModel):
    """``emi_settings``: the calculator's global knobs (one live row; reads serve the defaults until the first edit)."""

    tenure_min_years = models.PositiveSmallIntegerField(default=1)
    tenure_max_years = models.PositiveSmallIntegerField(default=10)
    tenure_default_years = models.PositiveSmallIntegerField(default=5)
    daily_saving_divisor = models.PositiveSmallIntegerField(default=30, help_text="Daily amount = monthly EMI ÷ this.")
    price_step = _money(default=Decimal("5000.00"), help_text="Step of the price slider (₹).")
    down_payment_min_pct = _fraction(default=Decimal("0.1000"), help_text="Down-payment slider floor (fraction of the price); also the default down payment.")
    down_payment_max_pct = _fraction(default=Decimal("0.9000"))
    down_payment_step_pct = _fraction(default=Decimal("0.0500"))
    down_payment_quick_adds = ArrayField(_money(), default=list, blank=True, help_text="₹ amounts offered as one-tap chips.")
    rate_max = _fraction(default=Decimal("0.1800"), help_text="Upper end of the rate slider (fraction).")
    default_annual_rate = _fraction(default=Decimal("0.0950"), help_text="Rate when no interest rule matches (fraction).")
    panel_life_years = models.PositiveSmallIntegerField(default=25)
    disclaimer_en = models.TextField(blank=True, default="")
    disclaimer_ml = models.TextField(blank=True, default="", help_text="Malayalam; falls back to English.")

    class Meta:
        db_table = "emi_settings"
        constraints = [
            models.UniqueConstraint(models.Value(1), condition=Q(deleted_at__isnull=True), name="emi_settings_singleton"),
            models.CheckConstraint(
                condition=Q(tenure_min_years__gte=1) & Q(tenure_min_years__lte=F("tenure_default_years")) & Q(tenure_default_years__lte=F("tenure_max_years")),
                name="emi_settings_tenure_ordered",
            ),
            models.CheckConstraint(condition=Q(daily_saving_divisor__gte=1) & Q(price_step__gt=0), name="emi_settings_steps_positive"),
            models.CheckConstraint(
                condition=Q(down_payment_min_pct__gt=0) & Q(down_payment_min_pct__lte=F("down_payment_max_pct")) & Q(down_payment_max_pct__lte=1) & Q(down_payment_step_pct__gt=0),
                name="emi_settings_down_payment_band",
            ),
            models.CheckConstraint(condition=Q(rate_max__gte=0) & Q(rate_max__lt=1) & Q(default_annual_rate__gte=0) & Q(default_annual_rate__lt=1), name="emi_settings_rates_fraction"),
        ]

    def __str__(self) -> str:
        return "EMI calculator settings"


class SystemSize(BaseModel):
    """``emi_system_size``: a size tile and its per-kW price — the manual price source (DV-83, ``EMI_PRICE_SOURCE``)."""

    label = models.CharField(max_length=32, help_text='Shown on the tile, e.g. "3kW".')
    capacity_kw = _kw()
    price_per_kw = _money(help_text="System cost = this × capacity.")
    price_min = _money(null=True, blank=True, help_text="Floor of the customer's price slider; empty = the default price.")
    price_max = _money(null=True, blank=True, help_text="Ceiling of the customer's price slider; empty = the default price.")
    monthly_bill_reference = _money(default=Decimal("0"), help_text="Typical monthly bill for this size (the savings comparison).")
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "emi_system_size"
        ordering = ["sort_order", "capacity_kw", "id"]
        constraints = [
            models.UniqueConstraint(fields=["capacity_kw"], condition=Q(deleted_at__isnull=True), name="emi_system_size_capacity_live_uniq"),
            models.CheckConstraint(condition=Q(capacity_kw__gt=0) & Q(price_per_kw__gt=0), name="emi_system_size_positive"),
            models.CheckConstraint(
                condition=(Q(price_min__isnull=True) | Q(price_min__gte=0)) & (Q(price_max__isnull=True) | Q(price_max__gte=0)) & Q(monthly_bill_reference__gte=0),
                name="emi_system_size_money_non_negative",
            ),
            _ordered("emi_system_size", "price_min", "price_max"),
        ]

    def __str__(self) -> str:
        return self.label
