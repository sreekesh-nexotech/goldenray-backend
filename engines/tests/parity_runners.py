"""Replay the golden cases (``engines/tests/golden/core_*.json``) against the Python engines.

Each runner takes one case of a golden file and returns the Python result in the JavaScript's shape (``as_dict()``),
ready for :func:`engines.tests.golden_support.differences`. JSON numbers arrive as Decimals (``parse_float``), ints
as ints, so every input reaches the engines exactly as the JavaScript saw it.

:func:`observed_differences` runs every case of every file (energy and savings twice: as the JavaScript with
``fix_three_phase_tariff=False``, and with the D-8 fix against ``outputWithFix``) and returns the differences, keyed
the way ``core_divergences.json`` pins them.
"""

from __future__ import annotations

from functools import cache
from typing import Any, Callable, Iterator

from engines import energy, finance, money, savings, subsidy
from engines.tests.golden_support import FIXED_NOW, Difference, differences, load

#: The two ways an energy/savings case is replayed.
JAVASCRIPT = "javascript"  # fix_three_phase_tariff=False, expectation `output`
D8 = "d8"  # fix_three_phase_tariff=True, expectation `outputWithFix` when the case has one, else `output`


@cache
def energy_config(name: str) -> energy.EnergyConfig:
    return energy.EnergyConfig.from_json(load("core_energy.json")["header"]["configs"][name])


@cache
def savings_energy_config(name: str) -> energy.EnergyConfig:
    return energy.EnergyConfig.from_json(load("core_savings.json")["header"]["energyConfigs"][name])


def _energy_inputs(data: dict[str, Any]) -> energy.EnergyInputs:
    return energy.EnergyInputs(
        bill_amount=data.get("billAmount"),
        billing_cycle=data.get("billingCycle", "monthly"),
        phase=data.get("phase", "single"),
        system_size_kw=data.get("systemSizeKw"),
    )


def run_energy(case: dict[str, Any], *, fix_three_phase_tariff: bool) -> dict[str, Any]:
    result = energy.calculate_energy_profile(_energy_inputs(case["input"]), energy_config(case["config"]), case.get("regionId"), fix_three_phase_tariff=fix_three_phase_tariff)
    return result.as_dict()


def run_savings(case: dict[str, Any], *, fix_three_phase_tariff: bool) -> dict[str, Any]:
    header = load("core_savings.json")["header"]
    data = case["input"]
    energy_cfg = savings_energy_config(data["energyConfig"])
    if "energyResult" in data:
        payload = data["energyResult"]
        profile = energy.EnergyProfile.from_payload(payload) if payload is not None else None
    elif data.get("energyInputs") is not None:
        profile = energy.calculate_energy_profile(_energy_inputs(data["energyInputs"]), energy_cfg, fix_three_phase_tariff=fix_three_phase_tariff)
    else:
        profile = None
    region = None
    if data["withRegion"]:
        region_id = (profile.region_id if profile is not None and profile.region_id else None) or energy_cfg.default_region
        region = energy_cfg.regions.get(region_id)
    config_json = header["configs"].get(data["config"])
    inputs = savings.SavingsInputs(
        profile,
        customer_total_including_gst=data.get("investment"),
        current_bill_amount=data.get("currentBillAmount"),
        current_bill_cycle=data.get("currentBillCycle", "monthly"),
    )
    result = savings.calculate_savings(inputs, region=region, config=savings.SavingsConfig.from_json(config_json) if config_json is not None else None)
    return result.as_dict()


@cache
def subsidy_config(name: str | None) -> subsidy.SubsidyConfig | None:
    if name is None:
        return None
    return subsidy.SubsidyConfig.from_json(load("core_subsidy.json")["header"]["configs"][name])


def run_subsidy(case: dict[str, Any]) -> dict[str, Any]:
    data = case["input"]
    kwargs: dict[str, Any] = {}
    for js_name, name in (("subsidyType", "subsidy_type"), ("ghsHouses", "ghs_houses"), ("panelType", "panel_type"), ("connectionType", "connection_type")):
        if js_name in data:
            kwargs[name] = data[js_name]
    result = subsidy.calculate_subsidy(subsidy.SubsidyInputs(data.get("systemSizeKw"), **kwargs), subsidy_config(case["config"]), calculated_at=FIXED_NOW)
    return result.as_dict()


@cache
def finance_config(name: str | None) -> finance.FinanceConfig | None:
    if name is None:
        return None
    return finance.FinanceConfig.from_json(load("core_finance.json")["header"]["configs"][name])


def run_finance(case: dict[str, Any]) -> dict[str, Any] | None:
    data = case["input"]
    config = finance_config(case["config"])
    if case["fn"] == "resolveFinanceResult":
        # the JavaScript subsidy result object, exactly as resolveFinanceResult received it (resolve_finance reads the
        # payload shape as well as a SubsidyResult; test_core_review checks the two agree)
        subsidy_payload = load("core_finance.json")["header"]["subsidyResults"].get(data["subsidy"])
        resolved = finance.resolve_finance(data["gross"], subsidy_payload, config, calculated_at=FIXED_NOW)
        return resolved.as_dict() if resolved is not None else None
    inputs = finance.FinanceInputs(
        data.get("principal"),
        annual_interest_rate=data.get("annualInterestRate"),
        tenure_years=data.get("tenureYears"),
        down_payment_percentage=data.get("downPaymentPercentage"),
        down_payment_amount=data.get("downPaymentAmount"),
        gross_quotation_amount=data.get("grossQuotationAmount"),
        post_subsidy_investment=data.get("postSubsidyInvestment"),
    )
    result = finance.calculate_finance(inputs, config, calculated_at=data.get("calculatedAt"))
    return result.as_dict()


def _gst_regime_payload(gst_config: money.GstConfig) -> dict[str, Any]:
    try:
        regime = money.resolve_gst_regime(gst_config)
    except money.GstConfigError as error:
        return {"ok": False, "code": str(error.code), "regime": None, "effectiveRatePct": None, "components": None}
    return {
        "ok": True,
        "code": None,
        "regime": str(regime.regime),
        "effectiveRatePct": regime.effective_rate_pct,
        "components": [{"label": c.label, "valuationPct": c.valuation_pct, "ratePct": c.rate_pct} for c in regime.components],
    }


def run_money(case: dict[str, Any]) -> Any:
    data = case["input"]
    fn = case["fn"]
    if fn == "roundMoney":
        return money.round_money(data["value"])
    if fn == "isMoney":
        return money.is_money(data["value"])
    if fn == "sumExact":
        return money.sum_exact(data["values"])
    if fn == "mulExact":
        return money.mul_exact(data["a"], data["b"])
    gst_config = money.GstConfig.from_json(data["gst"])
    if fn == "resolveGstRegime":
        return _gst_regime_payload(gst_config)
    assert fn == "applyGst", fn
    # calculatePricing with a zero margin: selling price = cost = base; an extra of the same base follows the regime
    applied = money.apply_gst(money.resolve_gst_regime(gst_config), data["base"])
    price, published = money.round_money(data["base"]), applied.total_published
    return {
        "components": [component.as_dict() for component in applied.components],
        "totalPublished": published,
        "priceExcludingGST": price,
        "priceIncludingGST": price + published,
        "extraGstAmount": money.round_money(published),
        "extraCustomerPriceIncludingGST": price + published,
    }


def units_to_bill_cases() -> list[tuple[str, dict[str, Any]]]:
    """The direct ``unitsToBill`` cases of ``core_energy_tables.json`` (fractional units, other config shapes)."""
    cases = load("core_energy_tables.json")["unitsToBillCases"]
    return [(f"unitsToBill:{item.get('config', 'real')}:{item['phase']}:{item['units']}", item) for item in cases]


def run_units_to_bill(item: dict[str, Any]) -> dict[str, Any]:
    region = energy_config(item.get("config", "real")).regions["kerala"]
    return energy.units_to_bill(item["units"], region, item["phase"]).as_dict()


RUNNERS: dict[str, Callable[..., Any]] = {
    "core_money.json": run_money,
    "core_energy.json": run_energy,
    "core_savings.json": run_savings,
    "core_subsidy.json": run_subsidy,
    "core_finance.json": run_finance,
}
TWO_VARIANTS = {"core_energy.json", "core_savings.json"}


def replays(name: str) -> Iterator[tuple[dict[str, Any], str, Any, Callable[[], Any]]]:
    """``(case, variant, expected, run)`` for every replay of every case of golden file ``name``."""
    runner = RUNNERS[name]
    for case in load(name)["cases"]:
        if name in TWO_VARIANTS:
            yield case, JAVASCRIPT, case["output"], (lambda case=case: runner(case, fix_three_phase_tariff=False))
            yield case, D8, case.get("outputWithFix", case["output"]), (lambda case=case: runner(case, fix_three_phase_tariff=True))
        else:
            yield case, JAVASCRIPT, case["output"], (lambda case=case: runner(case))


def observed_differences(name: str) -> dict[tuple[str, str, str, str, str], Difference]:
    """Every difference of golden file ``name``: ``(case id, variant, path, javascript, python) → Difference``."""
    found = {}
    for case, variant, expected, run in replays(name):
        for difference in differences(expected, run()):
            found[(case["id"], variant) + difference.key] = difference
    return found
