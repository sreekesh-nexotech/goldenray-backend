"""Review findings on engines-core (adversarial review): each test failed before its fix and stays in the suite.

* a numeric string for the investment or the V1 current bill was accepted by ``SavingsInputs`` and then silently
  ignored (payback ``None`` while the payload said "GROSS — uses customerTotalIncludingGST"); the JavaScript coerces
  it (``"229000" > 0``);
* ``resolve_finance`` ignored a subsidy result given in its payload shape (``as_dict()``, the JavaScript result object)
  and financed the subsidy too; a float gross returned ``None`` instead of raising;
* ``GstConfig.from_cost_config`` refused the JSONB numbers the PLAN §2.3 keys are stored as (``0.70`` arrives as a
  float from ``json.loads``/``JSONField``);
* ``EnergyProfile.from_payload`` turned a BLOCKED energy payload into an *available* profile with empty fields, so
  savings answered ``MISSING_TARIFF`` where the JavaScript answers ``MISSING_ENERGY_RESULT``;
* ``EnergyConfig`` could not be pickled or deep-copied (its read-only region mapping), so it could not be cached.
"""

from __future__ import annotations

import copy
import json
import pickle
from decimal import Decimal

import pytest

from engines import energy, finance, money, savings, subsidy
from engines.tests.golden_support import FIXED_NOW, load

D = Decimal


@pytest.fixture(scope="module")
def energy_config() -> energy.EnergyConfig:
    return energy.EnergyConfig.from_json(load("core_energy.json")["header"]["configs"]["real"])


@pytest.fixture(scope="module")
def subsidy_config() -> subsidy.SubsidyConfig:
    return subsidy.SubsidyConfig.from_json(load("core_subsidy.json")["header"]["configs"]["real"])


@pytest.fixture(scope="module")
def finance_config() -> finance.FinanceConfig:
    return finance.FinanceConfig.from_json(load("core_finance.json")["header"]["configs"]["real"])


def _profile(energy_config, bill=D(3000)) -> energy.EnergyProfile:
    return energy.calculate_energy_profile(energy.EnergyInputs(bill), energy_config)


# ---- savings: numeric strings are numbers, never silently dropped ------------------------------------------------------


def test_savings_investment_as_numeric_text_is_used(energy_config):
    profile, region = _profile(energy_config), energy_config.regions["kerala"]
    as_number = savings.calculate_savings(savings.SavingsInputs(profile, D(229000)), region=region)
    as_text = savings.calculate_savings(savings.SavingsInputs(profile, " 229000 "), region=region)
    assert as_number.return_period_months == 78
    assert as_text.return_period_months == 78 and as_text == as_number


def test_savings_v1_current_bill_as_numeric_text_is_used(energy_config):
    profile = _profile(energy_config)
    as_number = savings.calculate_savings(savings.SavingsInputs(profile, D(229000), current_bill_amount=D(3100)))
    as_text = savings.calculate_savings(savings.SavingsInputs(profile, D(229000), current_bill_amount="3100"))
    assert as_number.current_monthly_bill == 3100  # the customer's stated monthly bill, not generation × rate (3000)
    assert as_text.current_monthly_bill == 3100 and as_text == as_number


def test_savings_inputs_normalise_to_decimal_and_refuse_junk():
    inputs = savings.SavingsInputs(None, "229000", "6000.50")
    assert (inputs.customer_total_including_gst, inputs.current_bill_amount) == (D(229000), D("6000.50"))
    assert isinstance(inputs.customer_total_including_gst, Decimal)
    with pytest.raises(ValueError):
        savings.SavingsInputs(None, "two lakh")
    with pytest.raises(TypeError):
        savings.SavingsInputs(None, current_bill_amount=6000.5)


# ---- finance: the subsidy result in either shape; floats refused --------------------------------------------------------


def test_resolve_finance_reads_a_subsidy_payload(finance_config, subsidy_config):
    eligible = subsidy.calculate_subsidy(subsidy.SubsidyInputs(3), subsidy_config, calculated_at=FIXED_NOW)
    as_result = finance.resolve_finance(D(229000), eligible, finance_config, calculated_at=FIXED_NOW)
    as_payload = finance.resolve_finance(D(229000), eligible.as_dict(), finance_config, calculated_at=FIXED_NOW)
    assert as_result.loan_amount == 128100  # 229000 − 22900 down payment − 78000 subsidy
    assert as_payload == as_result
    given_up = subsidy.calculate_subsidy(subsidy.SubsidyInputs(3, panel_type="NON_DCR"), subsidy_config)
    assert finance.resolve_finance(D(229000), given_up.as_dict(), finance_config).loan_amount == 206100
    # `subsidyResult.totalSubsidy ?? 0`: an available result without an amount counts 0
    assert finance.resolve_finance(D(229000), {"available": True, "totalSubsidy": None}, finance_config).loan_amount == 206100


def test_resolve_finance_refuses_a_float_gross(finance_config):
    with pytest.raises(TypeError):
        finance.resolve_finance(229000.0, None, finance_config)


# ---- GST: the cost-config keys as stored (JSONB numbers) ----------------------------------------------------------------


def test_gst_from_cost_config_reads_jsonb_numbers():
    stored = json.loads('{"gst_goods_share": 0.70, "gst_goods_rate": 0.05, "gst_services_share": 0.30, "gst_services_rate": 0.18}')
    assert all(isinstance(value, float) for value in stored.values())  # what JSONField hands back
    config = money.GstConfig.from_cost_config(**stored)
    assert config == money.GstConfig.from_cost_config(gst_goods_share="0.70", gst_goods_rate="0.05", gst_services_share="0.30", gst_services_rate="0.18")
    assert (config.goods_valuation_pct, config.goods_rate_pct, config.service_valuation_pct, config.service_rate_pct) == (70, 5, 30, 18)
    regime = money.resolve_gst_regime(config)
    assert regime.effective_rate_pct == D("8.9") and money.apply_gst(regime, D(210285)).total_published == 18715


def test_gst_from_cost_config_missing_or_junk_keys():
    missing = money.GstConfig.from_cost_config(gst_goods_share=D("0.70"), gst_goods_rate=D("0.05"), gst_services_share=D("0.30"), gst_services_rate=None)
    with pytest.raises(money.GstConfigError) as raised:
        money.resolve_gst_regime(missing)
    assert raised.value.code == money.GstError.GST_SPLIT_INVALID
    with pytest.raises(TypeError):
        money.GstConfig.from_cost_config(gst_goods_share=True, gst_goods_rate=D("0.05"), gst_services_share=D("0.30"), gst_services_rate=D("0.18"))
    with pytest.raises(ValueError):
        money.GstConfig.from_cost_config(gst_goods_share="seventy", gst_goods_rate=D("0.05"), gst_services_share=D("0.30"), gst_services_rate=D("0.18"))
    with pytest.raises(ValueError):
        money.GstConfig.from_cost_config(gst_goods_share=float("nan"), gst_goods_rate=D("0.05"), gst_services_share=D("0.30"), gst_services_rate=D("0.18"))


# ---- energy payloads: a blocked result stays blocked ----------------------------------------------------------------------


def test_blocked_energy_payload_round_trips_as_blocked(energy_config):
    blocked = energy.calculate_energy_profile(energy.EnergyInputs(D(0)), energy_config)
    again = energy.EnergyProfile.from_payload(blocked.as_dict())
    assert isinstance(again, energy.EnergyBlocked) and not again.available
    assert again == blocked and again.as_dict() == blocked.as_dict()
    result = savings.calculate_savings(savings.SavingsInputs(again, D(229000)), region=energy_config.regions["kerala"])
    assert result.status == savings.SavingsError.MISSING_ENERGY_RESULT  # the JavaScript's answer, not MISSING_TARIFF


def test_energy_config_survives_pickle_and_deepcopy(energy_config):
    """A parsed configuration can be cached (Django's cache pickles) and copied; its regions stay read-only."""
    for again in (pickle.loads(pickle.dumps(energy_config)), copy.deepcopy(energy_config)):
        assert again == energy_config and again.regions["kerala"] == energy_config.regions["kerala"]
        with pytest.raises(TypeError):
            again.regions["tamilnadu"] = again.regions["kerala"]
        profile = energy.calculate_energy_profile(energy.EnergyInputs(D(3000)), again)
        assert profile == energy.calculate_energy_profile(energy.EnergyInputs(D(3000)), energy_config)


def test_blocked_energy_payload_with_an_unknown_code_keeps_it():
    again = energy.EnergyProfile.from_payload({"available": False, "status": "BLOCKED", "error": "SOMETHING_NEW", "reason": "r", "regionId": "kerala"})
    assert isinstance(again, energy.EnergyBlocked) and again.as_dict()["error"] == "SOMETHING_NEW"
