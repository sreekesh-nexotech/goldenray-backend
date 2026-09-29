"""emi.services.legacy_import on the committed UAT rows (exported read-only from legacy_goldenapp)."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from audit.models import AuditLog
from emi.models import Bank, EmiSettings, InterestRateRule, SubsidyRule, SystemSize
from emi.services import legacy_import
from emi.tests.factories import EmiSettingsFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def rows():
    return json.loads((Path(__file__).parent / "parity" / "uat" / "legacy_rows.json").read_text())["rows"]


def _all(rows):
    return legacy_import.import_all(
        banks=rows["emi_bank"],
        interest_rules=rows["emi_interest_rate_rule"],
        subsidy_rules=rows["emi_subsidy_rule"],
        settings=rows["emi_calculator_settings"],
        system_sizes=rows["emi_system_size"],
    )


def test_imports_every_table_with_percentages_as_fractions(rows):
    result = _all(rows)
    assert {table: counts["created"] for table, counts in result.items()} == {
        "emi_calculator_settings": 1,
        "emi_bank": 4,
        "emi_interest_rate_rule": 6,
        "emi_subsidy_rule": 3,
        "emi_system_size": 4,
    }
    assert all(not counts["violations"] for counts in result.values())
    sbi = Bank.objects.get(slug="sbi")
    assert sbi.annual_rate == Decimal("0.0565") and sbi.features[0] == "Up to ₹2L at 5.65% (Flarize rate)" and sbi.logo_bg == "#1D4ED8"
    union = Bank.objects.get(slug="union")
    assert union.processing_fee_pct == Decimal("0.0010") and union.processing_fee_note == "₹500 flat"
    loan_rule = InterestRateRule.objects.get(label__startswith="Loan amount up to")
    assert (loan_rule.max_amount, loan_rule.annual_rate, loan_rule.min_annual_rate, loan_rule.is_locked) == (Decimal("200000.00"), Decimal("0.0575"), Decimal("0.0575"), True)
    cost_rule = InterestRateRule.objects.get(label__contains="₹2L to ₹3L")
    assert (cost_rule.min_system_cost, cost_rule.max_system_cost, cost_rule.max_kw, cost_rule.is_active) == (Decimal("200000.01"), Decimal("300000.00"), Decimal("3.00"), False)
    top = SubsidyRule.objects.get(label__endswith="3kW and above")
    assert (top.kw_from, top.kw_to, top.amount, top.amount_per_kw, top.cap_amount, top.scheme) == (Decimal("3.00"), None, Decimal("78000.00"), Decimal("0.00"), None, "PM_SURYA_GHAR")
    settings = EmiSettings.objects.get()
    assert settings.down_payment_min_pct == Decimal("0.1000") and settings.rate_max == Decimal("0.1800") and settings.default_annual_rate == Decimal("0.0950")
    assert settings.down_payment_quick_adds == [Decimal("5000.00"), Decimal("10000.00"), Decimal("20000.00")]
    assert settings.created_at == settings.updated_at  # the legacy row has only updated_at
    assert SystemSize.objects.get(capacity_kw=3).price_min == Decimal("180000.00")


def test_re_runs_are_idempotent(rows):
    _all(rows)
    again = _all(rows)
    assert all(counts["created"] == 0 and counts["updated"] == 0 for counts in again.values())
    assert Bank.objects.count() == 4 and EmiSettings.objects.count() == 1
    changed = [dict(row) for row in rows["emi_bank"]]
    changed[0]["interest_rate"] = "5.70"
    assert legacy_import.import_banks(changed)["updated"] == 1
    assert Bank.objects.get(slug="sbi").annual_rate == Decimal("0.0570")


def test_settings_edited_in_studio_before_the_import_are_kept(rows):
    EmiSettingsFactory(panel_life_years=30)
    result = legacy_import.import_settings(rows["emi_calculator_settings"])
    assert result["created"] == 0 and result["skipped"] == 1 and result["violations"][0]["field"] == "row"
    assert EmiSettings.objects.get().panel_life_years == 30


@pytest.mark.parametrize(
    ("table", "function", "change", "field"),
    [
        ("emi_bank", legacy_import.import_banks, {"slug": "Not Slug"}, "slug"),
        ("emi_bank", legacy_import.import_banks, {"logo_bg": "blue"}, "logo_bg"),
        ("emi_bank", legacy_import.import_banks, {"features": "Fast"}, "features"),
        ("emi_bank", legacy_import.import_banks, {"interest_rate": "5.655"}, "interest_rate"),
        ("emi_interest_rate_rule", legacy_import.import_interest_rules, {"rate": None}, "rate"),
        ("emi_interest_rate_rule", legacy_import.import_interest_rules, {"priority": "high"}, "priority"),
        ("emi_subsidy_rule", legacy_import.import_subsidy_rules, {"amount": "-1"}, "amount"),
        ("emi_system_size", legacy_import.import_system_sizes, {"price_per_kw": "0"}, "price_per_kw"),
        ("emi_calculator_settings", legacy_import.import_settings, {"down_payment_quick_adds": {"a": 1}}, "down_payment_quick_adds"),
        ("emi_calculator_settings", legacy_import.import_settings, {"tenure_min_years": 0}, "tenure_min_years"),
    ],
)
def test_rows_that_would_need_inventing_or_rounding_are_reported(rows, table, function, change, field):
    result = function([{**rows[table][0], **change}])
    assert result["created"] == 0 and field in [violation["field"] for violation in result["violations"]]


def test_rows_the_database_refuses_are_reported(rows):
    rule = {**rows["emi_interest_rate_rule"][0], "rate": "5.00", "min_rate": "6.00"}  # rate below its floor
    result = legacy_import.import_interest_rules([rule])
    assert result["created"] == 0 and result["violations"][0]["field"] == "row"


def test_audited_once_per_batch(rows):
    legacy_import.import_banks(rows["emi_bank"])
    entry = AuditLog.objects.get(action="emi.legacy_imported")
    assert entry.after["source"] == "BACKEND emi_bank" and entry.after["created"] == 4
