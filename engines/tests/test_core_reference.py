"""engines-core against the reference outputs of the Flarize engines spec §20, decisions D-7/D-8, and every error path.

The reference numbers are copied from ``platform-reference/flarize-engines-spec.md`` §20 ("Reference outputs from
running the real engines"); the configuration is the legacy ``data/*-config.json`` as captured in the golden files.
"""

from __future__ import annotations

import ast
import dataclasses
from decimal import Decimal
from pathlib import Path

import pytest

from engines import energy, finance, money, savings, subsidy
from engines.tests.golden_support import FIXED_NOW, load

D = Decimal


def saving(profile, investment=None, *, region=None, config=None, **inputs):
    return savings.calculate_savings(savings.SavingsInputs(profile, investment, **inputs), region=region, config=config)


def subsidise(size, config=None, *, calculated_at=None, **inputs):
    return subsidy.calculate_subsidy(subsidy.SubsidyInputs(size, **inputs), config, calculated_at=calculated_at)


def finance_of(principal, config, *, calculated_at=None, **inputs):
    return finance.calculate_finance(finance.FinanceInputs(principal, **inputs), config, calculated_at=calculated_at)


@pytest.fixture(scope="module")
def energy_config() -> energy.EnergyConfig:
    return energy.EnergyConfig.from_json(load("core_energy.json")["header"]["configs"]["real"])


@pytest.fixture(scope="module")
def kerala(energy_config) -> energy.RegionConfig:
    return energy_config.regions["kerala"]


@pytest.fixture(scope="module")
def subsidy_config() -> subsidy.SubsidyConfig:
    return subsidy.SubsidyConfig.from_json(load("core_subsidy.json")["header"]["configs"]["real"])


@pytest.fixture(scope="module")
def finance_config() -> finance.FinanceConfig:
    return finance.FinanceConfig.from_json(load("core_finance.json")["header"]["configs"]["real"])


@pytest.fixture(scope="module")
def savings_config() -> savings.SavingsConfig:
    return savings.SavingsConfig.from_json(load("core_savings.json")["header"]["configs"]["real"])


# ---- §20 energy ------------------------------------------------------------------------------------------------------


def test_reference_energy_profile(energy_config):
    profile = energy.calculate_energy_profile(energy.EnergyInputs(D(3000), "monthly", "single"), energy_config)
    assert profile.available and profile.status == "LIVE"
    assert profile.bi_monthly_units == 659 and profile.bill_breakdown.billing_category == energy.BillingCategory.NON_TELESCOPIC
    breakdown = profile.bill_breakdown
    assert (breakdown.energy_charge, breakdown.fixed_charge, breakdown.duty, breakdown.meter_rent, breakdown.total) == (5008, 480, 501, 12, 6001)
    assert (profile.monthly_consumption, profile.average_tariff_rate, profile.exact_system_size_kw, profile.recommended_system_size_kw) == (330, D("9.09"), D("2.8"), 3)
    assert (profile.daily_generation_low, profile.daily_generation_high, profile.monthly_generation, profile.annual_generation) == (D("10.8"), D("13.2"), 360, 4320)
    assert (profile.monthly_kseb_value_low, profile.monthly_kseb_value_high) == (2945, 3600)
    assert (profile.home_uses_units_per_day, profile.surplus_exported_low, profile.surplus_exported_high) == (11, 0, D("2.2"))


def test_reference_units_to_bill(kerala):
    bill = energy.units_to_bill(300, kerala, "single")
    assert (bill.energy_charge, bill.fixed_charge, bill.duty, bill.meter_rent, bill.total) == (1295, 210, 130, 12, 1647)
    bill = energy.units_to_bill(600, kerala, "single")
    assert (bill.energy_charge, bill.fixed_charge, bill.duty, bill.total) == (4050, 440, 405, 4907)
    assert bill.energy_charge_exact == D("4050.00") and "energyChargeExact" not in bill.as_dict()


def test_duty_is_charged_on_the_unrounded_energy(kerala):
    # 312.5 units: energy 1295 + 12.5 × 7.20 = 1385.0 → duty 138.5 → 139 (on the exact energy, not on the rounded one)
    bill = energy.units_to_bill(D("312.5"), kerala, "single")
    assert (bill.energy_charge, bill.duty) == (1385, 139)
    # 1.5 units: 5.025 → energy 5, duty round(0.5025) = 1
    assert energy.units_to_bill(D("1.5"), kerala, "single").duty == 1


def test_sizing_caps_consumption_at_3000_bi_monthly_units(energy_config):
    profile = energy.calculate_energy_profile(energy.EnergyInputs(D(100000)), energy_config)
    assert profile.bi_monthly_units == 3000 and profile.monthly_consumption == 1500
    assert profile.recommended_system_size_kw == 11  # ceil(1500 / 120 × 0.85) = ceil(10.625): whole kW, not a catalog size


def test_small_bills_have_no_consumption(energy_config):
    profile = energy.calculate_energy_profile(energy.EnergyInputs(D(50)), energy_config)
    assert profile.bi_monthly_units == 0 and profile.average_tariff_rate == 0 and profile.recommended_system_size_kw == 1
    assert saving(profile, D(229000), region=energy_config.regions["kerala"]).status == savings.SavingsError.MISSING_TARIFF


def test_quoted_size_overrides_auto_sizing(energy_config):
    profile = energy.calculate_energy_profile(energy.EnergyInputs(D(3000), system_size_kw=D("5.45")), energy_config)
    assert profile.recommended_system_size_kw == D("5.45") and profile.exact_system_size_kw == D("2.8")
    assert profile.monthly_generation == D("654.00") and profile.daily_generation_low == D("19.6")


# ---- D-8: the '3P' tariff ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("phase", ["3P", "3p", " 3P ", "3 ph", "3-phase", "3PH", "3_phase"])
def test_d8_three_phase_codes_select_the_three_phase_tariff(phase):
    assert energy.resolve_phase(phase) == energy.Phase.THREE
    assert energy.resolve_phase(phase, fix_three_phase_tariff=False) == energy.Phase.SINGLE


@pytest.mark.parametrize("phase", ["three", "Three Phase", "THREE_PHASE"])
def test_three_is_three_either_way(phase):
    assert energy.resolve_phase(phase) == energy.resolve_phase(phase, fix_three_phase_tariff=False) == energy.Phase.THREE


@pytest.mark.parametrize("phase", ["single", "1P", "1 phase", "", None, "3", "33P", "P3"])
def test_other_texts_are_single_phase(phase):
    assert energy.resolve_phase(phase) == energy.resolve_phase(phase, fix_three_phase_tariff=False) == energy.Phase.SINGLE


def test_d8_changes_the_bill_of_a_3p_connection(energy_config):
    inputs = energy.EnergyInputs(D(3000), "monthly", "3P")
    fixed = energy.calculate_energy_profile(inputs, energy_config)
    quirk = energy.calculate_energy_profile(inputs, energy_config, fix_three_phase_tariff=False)
    three = energy.calculate_energy_profile(energy.EnergyInputs(D(3000), "monthly", "three"), energy_config)
    assert fixed == three and fixed.phase == "three" and quirk.phase == "single"
    assert (quirk.bill_breakdown.meter_rent, fixed.bill_breakdown.meter_rent) == (12, 30)
    assert quirk.bi_monthly_units > fixed.bi_monthly_units  # the same bill buys fewer units at the three-phase fixed charges


# ---- §20 savings -------------------------------------------------------------------------------------------------------


def test_reference_savings(energy_config, savings_config):
    profile = energy.calculate_energy_profile(energy.EnergyInputs(D(3000), "monthly", "single"), energy_config)
    result = saving(profile, D(229000), region=energy_config.regions["kerala"], config=savings_config)
    assert (result.monthly_savings, result.monthly_savings_low, result.monthly_savings_high, result.daily_savings) == (2949, 2927, 2949, 98)
    assert (result.annual_savings_amount, result.return_period_months) == (35388, 78)
    assert (result.current_monthly_bill, result.post_solar_bill_low, result.post_solar_bill_high) == (3005, 56, 78)
    payload = result.as_dict()
    assert payload["lifetimeSavingsAmount"] is None and payload["lifetimeKSEBSpend"] is None and payload["graphPoints"] is None
    assert payload["assumptions"]["paybackBasis"] == "GROSS — uses customerTotalIncludingGST, subsidy not deducted"


def test_payback_uses_the_gross_investment(energy_config):
    profile = energy.calculate_energy_profile(energy.EnergyInputs(D(3000)), energy_config)
    region = energy_config.regions["kerala"]
    assert saving(profile, D(229000), region=region).return_period_months == 78
    assert saving(profile, D(229000) - 78000, region=region).return_period_months == 52  # a caller passing a net figure gets a different payback
    assert saving(profile, None, region=region).return_period_months is None


def test_lifetime_projection_when_approved(energy_config):
    profile = energy.calculate_energy_profile(energy.EnergyInputs(D(3000)), energy_config)
    config = savings.SavingsConfig(lifetime_years=25, degradation_rate_per_year=D("0.5"), tariff_escalation_low=D(3), tariff_escalation_high=D(5))
    result = saving(profile, D(229000), region=energy_config.regions["kerala"], config=config)
    points = result.projection.graph_points
    assert len(points) == 25 and points[0].without_solar_cumulative == 3005 * 12
    assert points[0].with_solar_cumulative == 229000 + 67 * 12  # post-solar midpoint (56 + 78) / 2 = 67, no degradation in year 1
    assert result.projection.lifetime_kseb_spend == points[-1].without_solar_cumulative
    assert result.as_dict()["assumptions"]["tariffEscalation"] == "APPROVED_CONFIG — 3%–5% per year (graph uses the midpoint)"


@pytest.mark.parametrize(
    "config",
    [
        savings.SavingsConfig(lifetime_years=25),
        savings.SavingsConfig(lifetime_years=25, degradation_rate_per_year=D("0.5"), tariff_escalation_low=D(100), tariff_escalation_high=D(5)),
        savings.SavingsConfig(lifetime_years=D("2.5"), degradation_rate_per_year=D("0.5"), tariff_escalation_low=D(3), tariff_escalation_high=D(5)),
        savings.SavingsConfig(lifetime_years=0, degradation_rate_per_year=D("0.5"), tariff_escalation_low=D(3), tariff_escalation_high=D(5)),
    ],
)
def test_lifetime_projection_stays_off_until_approved(config):
    assert savings.project_lifetime(config, D(3005), D(56), D(78), D(2949), D(229000)) is savings.NO_PROJECTION


def test_lifetime_projection_needs_a_bill_and_non_negative_savings():
    config = savings.SavingsConfig(lifetime_years=5, degradation_rate_per_year=D(1), tariff_escalation_low=D(3), tariff_escalation_high=D(5))
    assert savings.project_lifetime(config, D(0), D(56), D(78), D(2949)) is savings.NO_PROJECTION
    assert savings.project_lifetime(config, D(3005), None, D(78), D(2949)) is savings.NO_PROJECTION
    assert savings.project_lifetime(config, D(3005), D(56), D(78), D(-1)) is savings.NO_PROJECTION
    assert savings.project_lifetime(config, D(3005), D(56), D(78), D(2949)).available


@pytest.mark.parametrize(
    "mutation, code",
    [
        ({}, savings.SavingsError.MISSING_TARIFF),
        ({"average_tariff_rate": D("9.09"), "monthly_generation": D(0)}, savings.SavingsError.ZERO_GENERATION),
        ({"average_tariff_rate": D("9.09"), "monthly_consumption": D(0)}, savings.SavingsError.ZERO_CONSUMPTION),
    ],
)
def test_savings_blocked_codes(energy_config, mutation, code):
    profile = energy.calculate_energy_profile(energy.EnergyInputs(D(3000)), energy_config)
    profile = dataclasses.replace(profile, **{"average_tariff_rate": D(0), **mutation})
    result = saving(profile, D(229000))
    assert result.status == code and not result.available
    assert result.as_dict() == {"source": "SAVINGS_ENGINE", "status": str(code), "available": False, "reason": result.reason, "engineVersion": "2.0.0"}


def test_savings_without_an_energy_result(energy_config):
    assert saving(None).status == savings.SavingsError.MISSING_ENERGY_RESULT
    blocked = energy.calculate_energy_profile(energy.EnergyInputs(D(0)), energy_config)
    assert saving(blocked).status == savings.SavingsError.MISSING_ENERGY_RESULT


def test_savings_v1_fallback_uses_the_effective_rate(energy_config):
    profile = energy.calculate_energy_profile(energy.EnergyInputs(D(3000)), energy_config)
    result = saving(profile, D(229000), current_bill_amount=D(6000), current_bill_cycle="Bi-Monthly")
    assert result.monthly_savings == js(D(330) * D("9.09")) == 3000  # min(360, 330) × 9.09 = 2999.7
    assert result.current_monthly_bill == 3000 and result.current_bill_breakdown is None
    assert result.savings_basis == savings.FIRST_YEAR_BASIS


def js(value: Decimal) -> Decimal:
    return money.js_round(value)


@pytest.mark.parametrize(
    "cycle, expected", [("bimonthly", "bimonthly"), ("Bi-Monthly", "bimonthly"), ("bi monthly", "bimonthly"), ("BI_MONTHLY", "bimonthly"), ("monthly", "monthly"), (None, "monthly"), ("", "monthly")]
)
def test_normalize_cycle(cycle, expected):
    assert savings.normalize_cycle(cycle) == expected


# ---- §20 subsidy -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("size, amount", [(1, 30000), (2, 60000), (D("2.5"), 69000), (3, 78000), (5, 78000), (10, 78000)])
def test_reference_residential_subsidy(subsidy_config, size, amount):
    result = subsidise(size, config=subsidy_config, calculated_at=FIXED_NOW)
    assert result.available and result.total_subsidy == result.central_subsidy == amount and result.state_subsidy == 0
    assert result.as_dict()["subsidyAmount"] == amount and result.as_dict()["schemeVersion"] == "PMSG_2024_V1"


def test_reference_ineligible_above_10_kw(subsidy_config):
    result = subsidise(11, config=subsidy_config)
    assert result.status == subsidy.SubsidyError.INELIGIBLE_CAPACITY and result.eligibility_status == subsidy.EligibilityStatus.INELIGIBLE
    assert result.total_subsidy == 0


def test_below_1_kw_is_ineligible(subsidy_config):
    assert subsidise(D("0.99"), config=subsidy_config).status == subsidy.SubsidyError.INELIGIBLE_CAPACITY


def test_reference_ghs(subsidy_config):
    result = subsidise(5, subsidy_type="ghs", ghs_houses=4, config=subsidy_config)
    assert (result.eligible_capacity_kw, result.ghs_total_eligible_kw, result.total_subsidy) == (3, 12, 216000)
    assert result.as_dict()["ghsHouses"] == 4


def test_ghs_community_cap(subsidy_config):
    assert subsidise(5, subsidy_type="ghs", ghs_houses=400, config=subsidy_config).total_subsidy == 500 * 18000


def test_reference_non_dcr_gives_it_up(subsidy_config):
    result = subsidise(3, panel_type="NON_DCR", config=subsidy_config)
    assert result.status == subsidy.SubsidyError.GIVE_IT_UP and result.eligibility_status == subsidy.EligibilityStatus.GIVE_IT_UP
    assert result.total_subsidy == 0 and result.as_dict()["dcrRequired"] is False


@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"subsidy_type": "none"}, subsidy.SubsidyError.NO_SUBSIDY),
        ({"subsidy_type": None}, subsidy.SubsidyError.NO_SUBSIDY),
        ({"size": 0}, subsidy.SubsidyError.INVALID_INPUT),
        ({"size": "3"}, subsidy.SubsidyError.INVALID_INPUT),
        ({"connection_type": "commercial"}, subsidy.SubsidyError.INELIGIBLE_CONNECTION),
        ({"panel_type": "non-DCR"}, subsidy.SubsidyError.GIVE_IT_UP),
        ({"subsidy_type": "ghs", "ghs_houses": D("2.5")}, subsidy.SubsidyError.INVALID_INPUT),
        ({"subsidy_type": "rooftop"}, subsidy.SubsidyError.INVALID_INPUT),
        ({"size": 12}, subsidy.SubsidyError.INELIGIBLE_CAPACITY),
    ],
)
def test_subsidy_codes(subsidy_config, kwargs, code):
    size = kwargs.pop("size", 3)
    assert subsidise(size, config=subsidy_config, **kwargs).status == code


def test_subsidy_missing_configuration(subsidy_config):
    assert subsidise(3).status == subsidy.SubsidyError.MISSING_CONFIG
    assert subsidise(3, config=dataclasses.replace(subsidy_config, residential=None)).status == subsidy.SubsidyError.MISSING_CONFIG
    assert subsidise(3, subsidy_type="ghs", config=dataclasses.replace(subsidy_config, ghs=None)).status == subsidy.SubsidyError.MISSING_CONFIG
    assert subsidy.SubsidyError.INELIGIBLE_DCR == "INELIGIBLE_DCR"  # declared by the JavaScript, never produced


def test_dcr_waiver_and_state_top_up(subsidy_config):
    waived = dataclasses.replace(subsidy_config, dcr_required=False, state_top_up_kerala=D(5000))
    result = subsidise(3, panel_type="NON_DCR", config=waived)
    assert result.available and (result.central_subsidy, result.state_subsidy, result.total_subsidy) == (78000, 5000, 83000)


# ---- §20 finance, D-7 --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "principal, rate, emi, daily, repayment, interest",
    [(150000, D("5.75"), 1647, 55, 197640, 47640), (250000, D("7.9"), 3020, 101, 362400, 112400)],
)
def test_reference_finance(finance_config, principal, rate, emi, daily, repayment, interest):
    result = finance_of(D(principal), finance_config, calculated_at=FIXED_NOW)
    assert (result.interest_rate_pct, result.tenure_months) == (rate, 120)
    assert (result.monthly_emi, result.daily_payment, result.total_repayment, result.total_interest) == (emi, daily, repayment, interest)
    assert result.rate_label == "Indicative PM Surya Ghar rate"


def test_d7_tiers_are_data(finance_config):
    """The same principal financed under the Flarize tiers (7.9 %) and the website EMI rules' tiers (8 %)."""
    emi_rules = dataclasses.replace(
        finance_config,
        rate_tiers=(finance.RateTier(D("5.75"), max_finance_amount=D(200000)), finance.RateTier(D(8), min_finance_amount=D("200000.01"))),
    )
    assert finance_of(D(250000), finance_config).interest_rate_pct == D("7.9")
    assert finance_of(D(250000), emi_rules).interest_rate_pct == 8
    assert finance_of(D(250000), emi_rules).monthly_emi == 3033


def test_no_rate_is_hard_coded():
    """D-7: the only numbers in the finance module's code are the JavaScript fallbacks for missing keys."""
    tree = ast.parse(Path(finance.__file__).read_text(encoding="utf-8"))
    docstrings = {id(node.body[0].value) for node in ast.walk(tree) if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and node.body and isinstance(node.body[0], ast.Expr)}
    numbers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and id(node) not in docstrings and not isinstance(node.value, bool):
            if isinstance(node.value, (int, float)):
                numbers.add(D(node.value))
            elif isinstance(node.value, str) and node.value.replace(".", "", 1).isdigit():
                numbers.add(D(node.value))
    assert numbers <= {D(0), D(1), D(7), D(10), D(12), D(18), D(30), D(100)}, numbers


def test_rate_tier_gap_falls_to_the_last_tier(finance_config):
    assert finance.resolve_indicative_rate(D("200000.005"), finance_config) == finance.IndicativeRate(D("7.90"), "Indicative PM Surya Ghar rate")
    assert finance.resolve_indicative_rate(D("200000"), finance_config).rate == D("5.75")
    assert finance.resolve_indicative_rate(D(1), finance.FinanceConfig()).rate == finance.LEGACY_FALLBACK_ANNUAL_INTEREST_RATE
    assert finance.resolve_indicative_rate(D(1), finance.FinanceConfig(default_annual_interest_rate=D("6.5"))).rate == D("6.5")
    assert finance.resolve_indicative_rate(D(1), None).rate == 7


def test_zero_rate_and_zero_principal(finance_config):
    assert finance_of(D(120000), finance_config, annual_interest_rate=0, tenure_years=10).monthly_emi == 1000
    zero = finance_of(D(0), finance_config)
    assert (zero.monthly_emi, zero.daily_payment, zero.total_repayment, zero.total_interest, zero.interest_rate_pct) == (0, 0, 0, 0, D("5.75"))


@pytest.mark.parametrize(
    "principal, kwargs, code",
    [
        (D(-1), {}, finance.FinanceError.INVALID_PRINCIPAL),
        (None, {}, finance.FinanceError.INVALID_PRINCIPAL),
        ("1000", {}, finance.FinanceError.INVALID_PRINCIPAL),
        (D(150000), {"annual_interest_rate": D("-0.5")}, finance.FinanceError.INVALID_INTEREST_RATE),
        (D(150000), {"annual_interest_rate": D("18.01")}, finance.FinanceError.INVALID_INTEREST_RATE),
        (D(150000), {"tenure_years": 0}, finance.FinanceError.INVALID_TENURE),
        (D(150000), {"tenure_years": 11}, finance.FinanceError.INVALID_TENURE),
    ],
)
def test_finance_codes(finance_config, principal, kwargs, code):
    result = finance_of(principal, finance_config, calculated_at=FIXED_NOW, **kwargs)
    assert result.status == code and result.as_dict()["dailyPayment"] is None and result.as_dict()["calculatedAt"] == FIXED_NOW


def test_finance_missing_configuration():
    assert finance_of(D(1), None).status == finance.FinanceError.MISSING_CONFIG


def test_down_payment_on_the_gross_before_subsidy(finance_config, subsidy_config):
    basis = finance.finance_basis(D(229000), D(10), D(78000))
    assert (basis.down_payment_amount, basis.post_subsidy_investment, basis.principal) == (22900, 151000, 128100)
    eligible = subsidise(3, config=subsidy_config)
    result = finance.resolve_finance(D(229000), eligible, finance_config, calculated_at=FIXED_NOW)
    assert (result.loan_amount, result.down_payment_amount, result.gross_quotation_amount, result.post_subsidy_investment) == (128100, 22900, 229000, 151000)
    given_up = subsidise(3, panel_type="NON_DCR", config=subsidy_config)
    assert finance.resolve_finance(D(229000), given_up, finance_config).loan_amount == 229000 - 22900
    assert finance.resolve_finance(D(0), eligible, finance_config) is None and finance.resolve_finance(D(229000), None, None) is None
    assert finance.finance_basis(D(50000), None, D(78000)).principal == 0


# ---- money -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, rounded",
    [(D("0.5"), 1), (D("1.5"), 2), (D("2.5"), 3), (D("-0.5"), -1), (D("-2.5"), -3), (D("1.005"), 1), (D("-0.4"), 0), (None, None), ("abc", None), ("", 0), (" 7 ", 7), (True, 1)],
)
def test_round_money(value, rounded):
    assert money.round_money(value) == rounded
    if rounded == 0:
        assert str(money.round_money(value)) == "0"  # never "-0"


@pytest.mark.parametrize("value, rounded", [(D("2.5"), 3), (D("-2.5"), -2), (D("-2.6"), -3), (D("0.49"), 0), (D("-0.5"), 0)])
def test_js_round_ties_toward_positive_infinity(value, rounded):
    assert money.js_round(value) == rounded and str(money.js_round(value)) != "-0"


def test_floats_are_refused():
    with pytest.raises(TypeError):
        money.to_decimal(1.5)
    with pytest.raises(TypeError):
        money.round_money(2.5)
    with pytest.raises(TypeError):
        money.sum_exact([1, 2.5])
    with pytest.raises(TypeError):
        energy.EnergyInputs(bill_amount=3000.0)
    with pytest.raises(TypeError):
        subsidy.SubsidyInputs(3.0)
    with pytest.raises(TypeError):
        finance.FinanceInputs(150000.0)
    with pytest.raises(TypeError):
        savings.SavingsInputs(None, customer_total_including_gst=229000.0)
    with pytest.raises(TypeError):
        finance.finance_basis(229000.0, 10, 0)


def test_to_decimal_and_json_numbers():
    assert money.to_decimal(" 12.50 ") == D("12.50") and money.to_decimal(7) == 7
    with pytest.raises(ValueError):
        money.to_decimal("abc")
    with pytest.raises(ValueError):
        money.to_decimal(D("NaN"))
    with pytest.raises(TypeError):
        money.to_decimal(True)
    with pytest.raises(TypeError):
        money.to_decimal([1])
    assert money.optional_decimal(None) is None and money.optional_decimal("1") == 1
    assert money.json_number(3.35) == D("3.35") and money.json_number(float("nan")) is None and money.json_number(True) is None and money.json_number("3") is None
    assert money.json_number(D("Infinity")) is None and money.json_number(7) == 7


def test_exact_helpers_and_published_parts():
    assert money.sum_exact([D("0.1"), D("0.2")]) == D("0.3")  # the JavaScript prints 0.30000000000000004
    assert money.sum_exact(None) == 0 and money.mul_exact("x", 3) == 0
    parts, total = money.publish_parts([D("7359.975"), D("10095.3"), None])
    assert parts == (7360, 10095, None) and total == 17455
    assert money.canonical(D("5.0E+3")) == "5000" and money.canonical(D("-0.00")) == "0" and money.canonical(D("7.90")) == "7.9"
    assert money.canonical(D("0.1234567890123456789012345678901234567890")) == "0.123456789012345678901234567890123456789"  # never rounded
    assert money.js_text(None) == "null" and money.js_text(True) == "true" and money.js_text(D("NaN")) == "NaN" and money.js_text(D("-Infinity")) == "-Infinity"
    assert money.is_number(3) and money.is_number(D(3)) and not money.is_number(True) and not money.is_number("3") and not money.is_number(D("NaN"))


def test_gst_composite_from_the_cost_config_keys():
    """PLAN §2.3 stores the shares and rates as fractions; the published GST is the sum of the rounded components."""
    config = money.GstConfig.from_cost_config(gst_goods_share="0.70", gst_goods_rate="0.05", gst_services_share="0.30", gst_services_rate="0.18")
    regime = money.resolve_gst_regime(config)
    assert regime.regime == money.GstRegimeCode.SOLAR_70_30_COMPOSITE and regime.effective_rate_pct == D("8.9")
    applied = money.apply_gst(regime, D(210285))
    goods, service = applied.components
    assert (goods.taxable_value, goods.tax_amount, service.taxable_value, service.tax_amount) == (147200, 7360, 63086, 11355)
    assert applied.total_published == 18715 and applied.total_exact == D("18715.365")


@pytest.mark.parametrize(
    "config, code",
    [
        (money.GstConfig(), money.GstError.GST_NOT_CONFIGURED),
        (money.GstConfig(regime="FLAT"), money.GstError.GST_NOT_CONFIGURED),
        (money.GstConfig(regime="VAT"), money.GstError.GST_REGIME_INVALID),
        (money.GstConfig(regime="SOLAR_70_30_COMPOSITE", goods_valuation_pct=D(70)), money.GstError.GST_SPLIT_INVALID),
        (money.GstConfig(regime="SOLAR_70_30_COMPOSITE", goods_valuation_pct=D(70), goods_rate_pct=D(5), service_valuation_pct=D(20), service_rate_pct=D(18)), money.GstError.GST_SPLIT_INVALID),
        (
            money.GstConfig(regime="SOLAR_70_30_COMPOSITE", goods_valuation_pct=D(70), goods_rate_pct=D(5), service_valuation_pct=D(30), service_rate_pct=D(18), effective_rate_pct=D(9)),
            money.GstError.GST_EFFECTIVE_RATE_MISMATCH,
        ),
    ],
)
def test_gst_errors(config, code):
    with pytest.raises(money.GstConfigError) as raised:
        money.resolve_gst_regime(config)
    assert raised.value.code == code and raised.value.reason


def test_flat_gst():
    regime = money.resolve_gst_regime(money.GstConfig(rate_pct=D(18)))
    assert regime.regime == money.GstRegimeCode.FLAT and money.apply_gst(regime, D("199.99")).total_published == 36


# ---- configuration parsing and validation ------------------------------------------------------------------------------


def test_energy_config_validation_and_regions(energy_config):
    raw = load("core_energy.json")["header"]["configs"]["real"]
    assert energy.validate_energy_config(raw) is raw
    for broken, message in ((None, "non-null object"), ({"configVersion": "x"}, '"regions"'), ({"regions": {}}, '"configVersion"')):
        with pytest.raises(ValueError, match=message):
            energy.validate_energy_config(broken)
    assert energy.list_regions(energy_config) == [{"regionId": "kerala", "regionName": "Kerala", "discom": "KSEB", "tariffName": "LT-1A Domestic", "tariffVersion": "2026-27-phase3"}]
    assert energy.EnergyConfig.from_json({"regions": "nope"}).regions == {}


def test_energy_blocked_codes(energy_config):
    no_region = energy.calculate_energy_profile(energy.EnergyInputs(D(3000)), energy.EnergyConfig(config_version="x", default_region=None))
    assert no_region.error == energy.EnergyError.NO_REGION_CONFIG and 'region "undefined"' in no_region.reason and no_region.as_dict()["regionId"] is None
    region = energy_config.regions["kerala"]
    no_yield = energy.EnergyConfig("x", "kerala", {"kerala": dataclasses.replace(region, yield_assumption=None)})
    assert energy.calculate_energy_profile(energy.EnergyInputs(D(3000)), no_yield).error == energy.EnergyError.NO_YIELD_CONFIG
    no_tariff = energy.EnergyConfig("x", "kerala", {"kerala": dataclasses.replace(region, tariff_slabs=())})
    assert energy.calculate_energy_profile(energy.EnergyInputs(D(3000)), no_tariff).error == energy.EnergyError.NO_TARIFF_CONFIG
    for bill in (D(0), D(-1), None, "abc", True, D("NaN")):
        assert energy.calculate_energy_profile(energy.EnergyInputs(bill), energy_config).error == energy.EnergyError.INVALID_BILL_AMOUNT


def test_energy_profile_round_trips_through_its_payload(energy_config):
    profile = energy.calculate_energy_profile(energy.EnergyInputs(D(3000), system_size_kw=D("3.3")), energy_config)
    again = energy.EnergyProfile.from_payload(profile.as_dict())
    assert again.as_dict() == profile.as_dict()
    assert saving(again, D(229000), region=energy_config.regions["kerala"]) == saving(profile, D(229000), region=energy_config.regions["kerala"])


def test_savings_config_validation():
    assert savings.validate_savings_config(load("core_savings.json")["header"]["configs"]["real"]) == (True, [])
    assert savings.validate_savings_config(None) == (False, ["Config must be a non-null object"])
    assert savings.validate_savings_config({"selfConsumptionModel": "NET", "lifetimeYears": 0}) == (False, ["Unsupported selfConsumptionModel: NET", "lifetimeYears must be a positive number or null"])
    assert savings.validate_savings_config({}) == (False, ["Missing selfConsumptionModel"])


def test_subsidy_config_validation():
    assert subsidy.validate_subsidy_config(load("core_subsidy.json")["header"]["configs"]["real"]) == (True, [])
    assert subsidy.validate_subsidy_config([]) == (False, ["Config must be a non-null object"])
    assert subsidy.validate_subsidy_config({}) == (False, ["Missing residential section", "Missing GHS section", "Missing scheme section"])
    ok, errors = subsidy.validate_subsidy_config({"residential": {"tiers": [], "maxSubsidy": 0, "maxCapacityKw": -1}, "ghs": {"ratePerKw": 0}, "scheme": {"name": "x"}})
    assert not ok and len(errors) == 6


def test_finance_config_validation():
    assert finance.validate_finance_config(load("core_finance.json")["header"]["configs"]["real"]) == (True, [])
    assert finance.validate_finance_config(None) == (False, ["Config must be a non-null object"])
    ok, errors = finance.validate_finance_config({"defaults": {"tenureYears": 0}, "validation": {"maxAnnualInterestRate": 0}, "rateTiers": [{"annualInterestRate": -1}, {}]})
    assert not ok and errors == [
        "defaults.tenureYears must be a positive number",
        "validation.maxAnnualInterestRate must be a positive number",
        "validation.minTenureYears must be a positive number",
        "validation.maxTenureYears must be a positive number",
        "rateTiers[0].annualInterestRate must be a non-negative number",
        "rateTiers[1].annualInterestRate must be a non-negative number",
    ]
    assert finance.validate_finance_config({}) == (False, ["Missing defaults section", "Missing validation section", "rateTiers must be a non-empty array"])


def test_results_are_frozen(energy_config, finance_config):
    profile = energy.calculate_energy_profile(energy.EnergyInputs(D(3000)), energy_config)
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.monthly_consumption = 1
    with pytest.raises(TypeError):
        energy_config.regions["tamilnadu"] = energy_config.regions["kerala"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        finance_of(D(1), finance_config).monthly_emi = 0


def test_binary64_math_round_matches_javascript():
    assert energy._binary64_math_round(2.5) == 3.0 and energy._binary64_math_round(2.4999999999999996) == 2.0
    assert energy._binary64_math_round(-2.5) == -2.0 and energy._binary64_math_round(-2.6) == -3.0 and energy._binary64_math_round(-0.4) == 0.0
