"""Public calculators: shape, errors, cache behaviour (snapshot + no-store), throttle scope."""

from decimal import Decimal

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from rest_framework.settings import api_settings

from calculators.models import BillRangeSize, CapacitySize
from calculators.services import sizing

pytestmark = pytest.mark.django_db

BASIC = "/api/public/v1/calculators/basic/"
BASIC_V2 = "/api/public/v1/calculators/basic-v2/"
ADVANCED = "/api/public/v1/calculators/advanced/"
HYBRID = {"Specifications": {"home_type": "Existing Home", "grid_type": "Hybrid", "average_bill": 3500}, "preferenceDetails": {"backup_hours": 4}}


def test_basic_shape(api_client, legacy_tables):
    response = api_client.post(BASIC, {"monthly_bill": 3000, "pincode": "682001", "property_type": "Residential"}, format="json")
    assert response.status_code == 200
    assert response.json() == {
        "estimated_units": 400.05,
        "daily_consumption": 13.34,
        "buffered_daily": 17.34,
        "solar_capacity_kW": 5,
        "area_required": 400,
        "installation_time_days": 5,
        "total_cost": 340000.0,
        "subsidy": 97500.0,
        "pincode": "682001",
        "property_type": "Residential",
    }
    assert response["Cache-Control"] == "no-store"


def test_basic_v2_shape(api_client, legacy_tables):
    response = api_client.post(BASIC_V2, {"monthly_bill": "9000", "pincode": 682001, "property_type": "residential"}, format="json")
    body = response.json()
    assert response.status_code == 200
    assert body["solar_capacity_kW"] == 5.0 and body["installation_time_days"] == "3-7" and body["interest_rate"] == 8.9
    assert body["emi_details"] == {"emi_per_month": 3178.61, "total_payment": 381432.88, "total_interest": 129432.88}  # as the legacy server answered
    assert [len(dataset["data"]) for dataset in body["datasets"]] == [6, 6]
    assert body["pincode"] == 682001 and body["savings"] == body["datasets"][0]["data"][-1] - body["datasets"][1]["data"][-1]


def test_advanced_hybrid_shape(api_client, legacy_tables):
    body = api_client.post(ADVANCED, HYBRID, format="json").json()
    assert body["type"] == "Residential" and body["battery_capacity"] == 4.61 and body["battery_price"] == 140300.0
    assert {"graph_without_solar", "graph_with_solar", "savings", "emi_details", "total_battery_cost"} <= set(body)


@pytest.mark.parametrize(
    ("path", "payload", "status", "code", "message"),
    [
        (BASIC, {"pincode": "682001"}, 400, "missing_fields", "Missing required fields"),
        (BASIC, {"monthly_bill": "3000", "pincode": "682001", "property_type": "Residential"}, 400, "invalid_monthly_bill", "Invalid monthly bill"),
        (BASIC, {"monthly_bill": 3000, "pincode": "999999", "property_type": "Residential"}, 404, "pincode_not_found", "Pincode not found in database"),
        (BASIC_V2, {"monthly_bill": "abc", "pincode": "682001", "property_type": "Residential"}, 400, "invalid_monthly_bill", "Invalid monthly_bill value"),
        (BASIC_V2, {"monthly_bill": 45000, "pincode": "682001", "property_type": "Residential"}, 400, "bill_out_of_range", "Monthly bill out of supported range"),
        (BASIC_V2, {"monthly_bill": 9000, "pincode": "682001", "property_type": "Industrial"}, 404, "no_sizing_row", "No data found for the given bill range and property type"),
        (BASIC_V2, {"monthly_bill": 9000, "pincode": "682001", "property_type": 5}, 400, "invalid_input", "The calculator cannot process these inputs."),
        (ADVANCED, {"Specifications": {"grid_type": "Off Grid"}}, 400, "unsupported_grid_type", "Only On Grid and Hybrid supported in this version."),
        (ADVANCED, [1, 2], 400, "invalid_input", "The calculator cannot process these inputs."),
    ],
)
def test_errors_use_the_envelope_with_the_legacy_message(api_client, legacy_tables, path, payload, status, code, message):
    response = api_client.post(path, payload, format="json")
    assert response.status_code == status
    assert response.json()["code"] == code and response.json()["message"] == message


def test_no_residential_row_is_404(api_client, legacy_tables):
    BillRangeSize.objects.all().delete()
    cache.clear()  # a raw delete bypasses the services (and their cache bump)
    response = api_client.post(ADVANCED, {"Specifications": {"grid_type": "On Grid"}}, format="json")
    assert response.status_code == 404 and response.json()["code"] == "no_matching_installation"


def test_warm_snapshot_costs_no_query_and_staff_writes_refresh_it(api_client, legacy_tables, django_assert_num_queries):
    payload = {"monthly_bill": 3000, "pincode": "682001", "property_type": "Residential"}
    assert api_client.post(BASIC, payload, format="json").json()["total_cost"] == 340000.0
    with django_assert_num_queries(0):
        assert api_client.post(BASIC, payload, format="json").status_code == 200

    size = CapacitySize.objects.get(power_capacity_kw=5)
    sizing.update_row(sizing.CAPACITY_SIZES, size, user=None, data={"total_cost": Decimal("350000.00")})
    assert api_client.post(BASIC, payload, format="json").json()["total_cost"] == 350000.0


def test_inactive_rows_are_not_used(api_client, legacy_tables):

    size = CapacitySize.objects.get(power_capacity_kw=5)
    sizing.update_row(sizing.CAPACITY_SIZES, size, user=None, data={"is_active": False})
    body = api_client.post(BASIC, {"monthly_bill": 3000, "pincode": "682001", "property_type": "Residential"}, format="json").json()
    assert body["total_cost"] is None and body["area_required"] is None


@pytest.mark.parametrize("path", [BASIC, BASIC_V2, ADVANCED])
def test_throttled_with_the_public_read_budget(api_client, legacy_tables, path):
    rates = {**api_settings.DEFAULT_THROTTLE_RATES, "public_read": "2/min", "public_write": "1000/min"}
    with override_settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": rates}):
        api_settings.reload()
        try:
            statuses = [api_client.post(path, {}, format="json").status_code for _ in range(3)]
        finally:
            api_settings.reload()
    assert 429 not in statuses[:2] and statuses[2] == 429


def test_get_is_not_allowed(api_client):
    assert api_client.get(BASIC).status_code == 405


def test_openapi_documents_the_calculators(api_client):
    from drf_spectacular.generators import SchemaGenerator

    schema = SchemaGenerator(api_version="v1").get_schema(request=None, public=True)
    for path in ("/api/public/v1/calculators/basic/", "/api/public/v1/calculators/basic-v2/", "/api/public/v1/calculators/advanced/"):
        operation = schema["paths"][path]["post"]
        assert operation["requestBody"] and "200" in operation["responses"] and "400" in operation["responses"]
