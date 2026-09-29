"""engines.emi: the legacy EMI policy rules one by one (the recorded corpora in emi/tests/test_parity.py prove the whole)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from engines import emi

SIZE = emi.SystemSize(
    uid="00000000-0000-0000-0000-000000000003", label="3kW", capacity_kw=Decimal("3.00"), price_per_kw=Decimal("76667.00"), price_min=Decimal("180000.00"), price_max=Decimal("500000.00")
)
LOW = emi.InterestRule(uid="low", label="≤ 2L", rate=Decimal("5.75"), min_rate=Decimal("5.75"), is_locked=True, priority=20, max_loan=Decimal("200000.00"), position=5)
HIGH = emi.InterestRule(uid="high", label="> 2L", rate=Decimal("8.00"), min_rate=Decimal("8.00"), is_locked=True, priority=20, min_loan=Decimal("200000.01"), position=6)
SUBSIDY = emi.SubsidyRule(uid="s", label="3kW+", min_kw=Decimal("3.00"), max_kw=None, amount=Decimal("78000.00"), priority=10)
CONFIG = emi.EmiConfig(settings=emi.EmiSettings(), sizes=(SIZE,), subsidy_rules=(SUBSIDY,), interest_rules=(LOW, HIGH))


def test_rule_order_is_the_legacy_query_order_nulls_last():
    rules = (
        emi.InterestRule(uid="a", label="a", rate=Decimal("1"), min_rate=Decimal("1"), priority=1, min_kw=None, position=1),
        emi.InterestRule(uid="b", label="b", rate=Decimal("1"), min_rate=Decimal("1"), priority=1, min_kw=Decimal("2"), position=2),
        emi.InterestRule(uid="c", label="c", rate=Decimal("1"), min_rate=Decimal("1"), priority=5, position=3),
    )
    config = emi.EmiConfig(settings=emi.EmiSettings(), interest_rules=rules)
    assert [rule.uid for rule in config.ordered_interest_rules()] == ["c", "b", "a"]


def test_priority_then_specificity_then_query_order():
    general = emi.InterestRule(uid="general", label="g", rate=Decimal("9"), min_rate=Decimal("9"), priority=10, position=1)
    specific = emi.InterestRule(uid="specific", label="s", rate=Decimal("7"), min_rate=Decimal("7"), priority=10, max_kw=Decimal("3"), position=2)
    config = emi.EmiConfig(settings=emi.EmiSettings(), interest_rules=(general, specific))
    assert emi.resolve_interest_rule(config, Decimal("3"), Decimal("1000")).uid == "specific"
    assert emi.resolve_interest_rule(config, Decimal("4"), Decimal("1000")).uid == "general"


def test_a_band_on_a_missing_dimension_never_matches():
    cost_rule = emi.InterestRule(uid="c", label="c", rate=Decimal("7"), min_rate=Decimal("7"), max_cost=Decimal("100"))
    assert not cost_rule.matches(Decimal("3"), Decimal("50"), None)
    assert cost_rule.matches(Decimal("3"), Decimal("50"), Decimal("80"))


def test_rate_floor_lock_and_default():
    floating = emi.InterestRule(uid="f", label="f", rate=Decimal("9"), min_rate=Decimal("7"), is_locked=False)
    config = emi.EmiConfig(settings=emi.EmiSettings(), interest_rules=(floating,))
    assert emi.resolve_rate(config, Decimal("3"), Decimal("1"), Decimal("1"), Decimal("6"))[0] == Decimal("7")
    assert emi.resolve_rate(config, Decimal("3"), Decimal("1"), Decimal("1"), Decimal("8"))[0] == Decimal("8")
    assert emi.resolve_rate(config, Decimal("3"), Decimal("1"), Decimal("1"))[0] == Decimal("9")
    assert emi.resolve_rate(CONFIG, Decimal("3"), Decimal("1"), Decimal("1"), Decimal("20"))[0] == Decimal("5.75")  # locked
    assert emi.resolve_rate(emi.EmiConfig(settings=emi.EmiSettings()), Decimal("3"), Decimal("1"), Decimal("1"))[:4] == (Decimal("9.50"), Decimal("9.50"), Decimal("9.50"), False)


def test_subsidy_flat_per_kw_and_cap():
    assert emi.resolve_subsidy(CONFIG, Decimal("2.99")) == Decimal("0") and emi.resolve_subsidy(CONFIG, None) == Decimal("0")
    per_kw = emi.SubsidyRule(uid="p", label="p", min_kw=None, max_kw=None, amount=Decimal("0"), amount_per_kw=Decimal("30000"), cap_amount=Decimal("78000"))
    assert per_kw.subsidy_for(Decimal("2")) == Decimal("60000") and per_kw.subsidy_for(Decimal("5")) == Decimal("78000")


def test_size_lookup_by_capacity_rounds_the_float_to_six_digits():
    assert emi.find_system_size(CONFIG, capacity_kw=3.0000001) is SIZE
    assert emi.find_system_size(CONFIG, capacity_kw=3.00001) is None
    assert emi.find_system_size(CONFIG, size_uid=SIZE.uid, capacity_kw=99) is SIZE
    assert emi.find_system_size(CONFIG) is None


def test_unlock_offers_the_cheapest_reachable_band():
    unlock = emi.resolve_rate_unlock(CONFIG, Decimal("3"), Decimal("230001.00"), Decimal("207000.90"), Decimal("8.00"), Decimal("23000.10"), Decimal("207000.90"))
    assert unlock == {"rate": 5.75, "extra_down_payment": 7100.0, "down_payment_amount": 30100.1, "rule_label": "≤ 2L"}
    assert emi.resolve_rate_unlock(CONFIG, Decimal("3"), Decimal("230001.00"), Decimal("207000.90"), Decimal("8.00"), Decimal("23000.10"), Decimal("25000")) is None


def test_calculate_breakdown():
    result = emi.calculate({"size_uid": SIZE.uid, "tenure_years": "5"}, CONFIG)
    assert result["system"]["system_cost"] == 230001.0 and result["loan"]["amount"] == 129000.9 and result["interest_rate"] == 8.0
    assert result["tenure_months"] == 60 and result["interest"]["unlock"]["down_payment_percent"] == pytest.approx(13.087, abs=0.001)


def test_lump_sum_price_of_a_pack_release():
    pack = emi.SystemSize(uid="pack", label="3", capacity_kw=Decimal("3.00"), price_per_kw=Decimal("1"), system_cost=Decimal("250000.00"))
    config = emi.EmiConfig(settings=emi.EmiSettings(), sizes=(pack,))
    assert emi.calculate({"capacity_kw": 3}, config)["system"]["system_cost"] == 250000.0


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({}, "size_required"),
        ({"size_uid": "not-a-uid"}, "invalid_number"),
        ({"size_uid": 5}, "invalid_number"),
        ({"capacity_kw": 4}, "invalid_request"),
        ({"capacity_kw": 3, "tenure_years": 0}, "invalid_request"),
        ({"capacity_kw": 3, "tenure_years": True, "system_cost": 0.001}, "invalid_request"),
        ({"capacity_kw": "nan"}, "invalid_input"),
        ([1], "invalid_input"),
        ({"capacity_kw": 3, "system_cost": 1e30}, "invalid_input"),
    ],
)
def test_calculate_errors(body, code):
    with pytest.raises(emi.EmiError) as excinfo:
        emi.calculate(body, CONFIG)
    assert excinfo.value.code == code and excinfo.value.status == 400


def test_quotation_errors_carry_the_package_key():
    with pytest.raises(emi.EmiError) as excinfo:
        emi.quotation({"capacity_kw": 3, "packages": {"p": {"system_cost": "x"}}}, CONFIG)
    assert excinfo.value.message == "packages.p: could not convert string to float: 'x'"
    with pytest.raises(emi.EmiError) as excinfo:
        emi.quotation({"capacity_kw": 3, "packages": {f"p{n}": {} for n in range(7)}}, CONFIG)
    assert excinfo.value.code == "too_many_packages"
    with pytest.raises(emi.EmiError) as excinfo:
        emi.quotation({"capacity_kw": 3, "tenure_years": "x", "packages": {"p": {}}}, CONFIG)
    assert excinfo.value.code == "invalid_number"
    with pytest.raises(emi.EmiError) as excinfo:
        emi.quotation({"capacity_kw": 3, "tenure_years": 20, "packages": {"p": {}}}, CONFIG)
    assert excinfo.value.code == "invalid_tenure"
    with pytest.raises(emi.EmiError) as excinfo:
        emi.quotation({"capacity_kw": 3, "packages": {"p": 1}}, CONFIG)
    assert excinfo.value.code == "invalid_package"


def test_quotation_keys_are_cut_to_32_characters():
    result = emi.quotation({"capacity_kw": 3, "packages": {"k" * 40: {"system_cost": 210000, "subsidy": 78000}}}, CONFIG)
    assert list(result["packages"]) == ["k" * 32] and result["packages"]["k" * 32]["rule_label"] == "≤ 2L"
