"""Public EMI calculator: shape, errors, config caching (ETag/304, invalidation), no-store, throttle scope, price source."""

from decimal import Decimal

import pytest
from django.conf import settings
from django.test import override_settings
from rest_framework.settings import api_settings

from emi.models import SystemSize
from emi.services import price_sources, rows
from emi.tests.factories import BankFactory, InterestRateRuleFactory, SubsidyRuleFactory, SystemSizeFactory

pytestmark = pytest.mark.django_db

CONFIG = "/api/public/v1/calculators/emi/config/"
CALCULATE = "/api/public/v1/calculators/emi/"
QUOTATION = "/api/public/v1/calculators/emi/quotation/"


@pytest.fixture(autouse=True)
def _no_provider():
    price_sources.reset()
    yield
    price_sources.reset()


@pytest.fixture
def policy():
    size = SystemSizeFactory(
        label="3kW", capacity_kw=Decimal("3.00"), price_per_kw=Decimal("76667.00"), price_min=Decimal("180000.00"), price_max=Decimal("500000.00"), monthly_bill_reference=Decimal("6000.00")
    )
    SubsidyRuleFactory(kw_from=Decimal("3.00"), amount=Decimal("78000.00"))
    low = InterestRateRuleFactory(label="Loan up to ₹2L — 5.75%", max_amount=Decimal("200000.00"), annual_rate=Decimal("0.0575"), min_annual_rate=Decimal("0.0575"))
    InterestRateRuleFactory(label="Loan above ₹2L — 8%", min_amount=Decimal("200000.01"), annual_rate=Decimal("0.0800"), min_annual_rate=Decimal("0.0800"))
    BankFactory(slug="sbi", name="State Bank of India", annual_rate=Decimal("0.0565"))
    return size, low


def test_config_shape_and_percentages(api_client, policy):
    size, _low = policy
    response = api_client.get(CONFIG)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"settings", "system_sizes", "banks"}
    assert body["settings"]["down_payment_min_percent"] == "10.00" and body["settings"]["rate_max"] == "18.00" and body["settings"]["default_interest_rate"] == "9.50"
    assert body["settings"]["down_payment_quick_adds"] == [] and body["settings"]["updated_at"] is None
    tile = body["system_sizes"][0]
    assert tile["uid"] == str(size.uid) and tile["capacity_kw"] == "3.00" and tile["price_per_kw"] == "76667.00" and tile["system_cost"] == 230001.0
    assert body["banks"][0]["interest_rate"] == "5.65" and body["banks"][0]["processing_fee_percent"] == "0.00" and "id" not in body["banks"][0]


def test_config_is_cached_with_an_etag_and_refreshed_by_staff_writes(api_client, policy, django_assert_num_queries):
    size, _low = policy
    first = api_client.get(CONFIG)
    assert first["Cache-Control"] == "public, max-age=60" and first["X-Cache"] == "MISS" and first["ETag"]
    with django_assert_num_queries(0):
        again = api_client.get(CONFIG, HTTP_IF_NONE_MATCH=first["ETag"])
    assert again.status_code == 304
    rows.update_row(rows.SYSTEM_SIZES, size, user=None, data={"price_per_kw": Decimal("70000.00")})
    refreshed = api_client.get(CONFIG)
    assert refreshed["X-Cache"] == "MISS" and refreshed.json()["system_sizes"][0]["price_per_kw"] == "70000.00"


def test_calculate_shape(api_client, policy):
    size, low = policy
    response = api_client.post(CALCULATE, {"size_uid": str(size.uid), "tenure_years": 5}, format="json")
    assert response.status_code == 200 and response["Cache-Control"] == "no-store"
    body = response.json()
    assert body["system"]["size_uid"] == str(size.uid) and body["system"]["system_cost"] == 230001.0
    assert body["down_payment"]["amount"] == 23000.1 and body["subsidy"]["amount"] == 78000.0 and body["loan"]["amount"] == 129000.9
    # rate basis 230001 − 23000.10 = 207000.90 > ₹2L → 8 %; paying ₹7,100 more upfront unlocks 5.75 %
    assert body["interest"]["rate"] == 8.0 and body["interest"]["rule_label"] == "Loan above ₹2L — 8%" and body["interest"]["rule_uid"] != str(low.uid)
    assert body["interest"]["unlock"]["rate"] == 5.75 and body["interest"]["unlock"]["extra_down_payment"] == 7100.0
    assert body["tenure_months"] == 60 and body["emi_per_month"] == body["result"]["emi_per_month"] > 0
    assert "size_id" not in body["system"] and "rule_id" not in body["interest"]


def test_calculate_by_capacity_and_rate_band(api_client, policy):
    body = api_client.post(CALCULATE, {"capacity_kw": "3", "down_payment_percent": 60}, format="json").json()
    assert body["interest"]["rate"] == 5.75 and body["interest"]["rule_label"] == "Loan up to ₹2L — 5.75%"


@pytest.mark.parametrize(
    ("payload", "code", "message"),
    [
        ({}, "size_required", "Provide either size_uid or capacity_kw"),
        ({"capacity_kw": "five"}, "invalid_number", "Invalid numeric value in request"),
        ({"size_uid": 12}, "invalid_number", "Invalid numeric value in request"),
        ({"capacity_kw": 7}, "invalid_request", "No active system size configured for 7.0 kW"),
        ({"capacity_kw": 3, "tenure_years": 11}, "invalid_request", "tenure_years must be between 1 and 10"),
        ({"capacity_kw": "inf"}, "invalid_input", "The EMI calculator cannot process these inputs."),
        ([1], "invalid_input", "The EMI calculator cannot process these inputs."),
    ],
)
def test_calculate_errors(api_client, policy, payload, code, message):
    response = api_client.post(CALCULATE, payload, format="json")
    assert response.status_code == 400 and response.json()["code"] == code and response.json()["message"] == message


def test_quotation_shape_and_errors(api_client, policy):
    body = api_client.post(QUOTATION, {"capacity_kw": 3, "packages": {"economy": {"system_cost": 210000, "subsidy": 78000}}}, format="json").json()
    assert body["tenure_years"] == 10 and body["packages"]["economy"]["down_payment"] == 21000.0 and body["packages"]["economy"]["loan_amount"] == 111000.0
    for payload, code in (
        ({"capacity_kw": 3}, "invalid_packages"),
        ({"capacity_kw": 0, "packages": {"a": {}}}, "capacity_required"),
        ({"capacity_kw": 3, "packages": {"a": {"system_cost": 0}}}, "invalid_package"),
    ):
        response = api_client.post(QUOTATION, payload, format="json")
        assert response.status_code == 400 and response.json()["code"] == code


def test_calculations_read_a_warm_snapshot_without_queries(api_client, policy, django_assert_num_queries):
    size, _low = policy
    payload = {"size_uid": str(size.uid)}
    assert api_client.post(CALCULATE, payload, format="json").status_code == 200
    with django_assert_num_queries(0):
        assert api_client.post(CALCULATE, payload, format="json").status_code == 200
        assert api_client.post(QUOTATION, {"capacity_kw": 3, "packages": {"a": {"system_cost": 250000}}}, format="json").status_code == 200


def test_inactive_and_deleted_sizes_are_not_offered(api_client, policy):
    size, _low = policy
    rows.update_row(rows.SYSTEM_SIZES, size, user=None, data={"is_active": False})
    assert api_client.get(CONFIG).json()["system_sizes"] == []
    assert api_client.post(CALCULATE, {"size_uid": str(size.uid)}, format="json").json()["code"] == "invalid_request"
    assert SystemSize.all_objects.count() == 1


@override_settings(EMI_PRICE_SOURCE="PACK_RELEASE")
def test_pack_release_prices(api_client, policy):
    assert api_client.get(CONFIG).status_code == 503
    price_sources.register(
        lambda: [price_sources.SizeOption(uid="pack-3kw", label="3 kW Premium", capacity_kw=Decimal("3.00"), price_per_kw=Decimal("80000.00"), system_cost=Decimal("241234.00"))],
        cache_namespaces=("packs",),
    )
    config = api_client.get(CONFIG).json()
    assert config["system_sizes"][0]["uid"] == "pack-3kw" and config["system_sizes"][0]["system_cost"] == 241234.0
    body = api_client.post(CALCULATE, {"capacity_kw": 3}, format="json").json()
    assert body["system"]["system_cost"] == 241234.0 and body["system"]["size_uid"] == "pack-3kw"


@pytest.mark.parametrize(("method", "path", "payload"), [("post", CALCULATE, {}), ("post", QUOTATION, {}), ("get", CONFIG, None)])
def test_throttled_with_the_public_read_budget(api_client, policy, method, path, payload):
    rates = {**api_settings.DEFAULT_THROTTLE_RATES, "public_read": "2/min", "public_write": "1000/min"}
    with override_settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": rates}):
        api_settings.reload()
        try:
            statuses = [getattr(api_client, method)(path, payload, format="json").status_code for _ in range(3)]
        finally:
            api_settings.reload()
    assert 429 not in statuses[:2] and statuses[2] == 429
