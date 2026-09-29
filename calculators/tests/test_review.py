"""Review findings on the public calculators (calculators-emi review); each was reproduced before the fix.

* A device name the legacy PostgreSQL ``UPPER()`` did not fold (a ligature) matched a catalogued device.
* A deeply nested JSON body (legacy: ``RecursionError``, HTTP 500) answered 500 and was recorded as a
  ``SystemException`` — any anonymous caller could flood the error sink; a legacy 500 must be a 400.
* The refused-input log line carried the exception text unbounded (``float("<2 MB text>")`` echoes the text).
"""

from __future__ import annotations

import json
import logging

import pytest

from core.models import SystemException

pytestmark = pytest.mark.django_db

ADVANCED = "/api/public/v1/calculators/advanced/"
PUBLIC_POSTS = ["/api/public/v1/calculators/basic/", "/api/public/v1/calculators/basic-v2/", ADVANCED, "/api/public/v1/calculators/emi/", "/api/public/v1/calculators/emi/quotation/"]


def test_a_ligature_device_name_is_not_a_catalogued_device(api_client, legacy_tables):
    """Recorded from the legacy UAT server: ``Toaﬆer`` (U+FB06) is no device there (0 W), ``Toaster`` is 1000 W."""
    body = {
        "Specifications": {"grid_type": "On Grid", "home_type": "New Home", "estimated_base_load": 100},
        "usageDetails": {"usage_electronic_devices": [{"device_type": "Toaﬆer", "daily_usage": 2, "no_of_units": 4}]},
    }
    response = api_client.post(ADVANCED, body, format="json")
    assert response.status_code == 200
    assert response.json()["graph_without_solar"] == [0, 22379, 50940, 87393, 133917, 193295]
    assert response.json()["savings"] == -238887
    body["usageDetails"]["usage_electronic_devices"][0]["device_type"] = "Toaster"
    assert api_client.post(ADVANCED, body, format="json").json()["graph_without_solar"] == [0, 129797, 295455, 506880, 776719, 1121110]


@pytest.mark.parametrize("path", PUBLIC_POSTS)
@pytest.mark.parametrize("text", ["[" * 100_000 + "]" * 100_000, '{"monthly_bill": ' + "[" * 100_000 + "]" * 100_000 + "}"], ids=["array", "in-object"])
def test_a_deeply_nested_body_is_a_400_and_no_system_exception(api_client, path, text):
    response = api_client.post(path, data=text.encode(), content_type="application/json")
    assert response.status_code == 400
    assert response.json()["code"] == "parse_error"
    assert not SystemException.objects.exists()


def test_the_refused_input_log_line_is_bounded(api_client, legacy_tables, caplog):
    huge = "x" * 200_000
    body = {"Specifications": {"grid_type": "On Grid"}, "usageDetails": {"usage_electronic_devices": [{"device_type": "TV", "daily_usage": huge}]}}
    with caplog.at_level(logging.INFO, logger="flarize.calculators"):
        response = api_client.post(ADVANCED, data=json.dumps(body), content_type="application/json")
    assert response.status_code == 400 and response.json()["code"] == "invalid_input"
    records = [record for record in caplog.records if record.getMessage() == "calculator input refused"]
    assert records and all(len(record.detail) <= 500 for record in records)
    assert "ValueError" in records[0].detail


# A lone UTF-16 surrogate ("\ud800" in JSON) is a valid JSON string but no UTF-8 text: the legacy endpoints crashed
# (HTTP 500) when such a value reached PostgreSQL (psycopg cannot encode it) or the response (the JSON renderer
# encodes to UTF-8). Recorded from the legacy UAT server; a legacy 500 is a 400 invalid_input.
SURROGATE_CASES = [
    ("/api/public/v1/calculators/basic-v2/", '{"monthly_bill": 9000, "pincode": "\\ud800", "property_type": "Residential"}'),
    ("/api/public/v1/calculators/basic-v2/", '{"monthly_bill": 9000, "pincode": "682001", "property_type": "Resi\\ud800"}'),
    ("/api/public/v1/calculators/basic/", '{"monthly_bill": 3000, "pincode": "682001", "property_type": "R\\ud800"}'),
    (ADVANCED, '{"Specifications": {"grid_type": "On Grid"}, "usageDetails": {"usage_electronic_devices": [{"device_type": "T\\ud800V", "daily_usage": 2}]}}'),
    (ADVANCED, '{"Specifications": {"grid_type": "On Grid"}, "usageDetails": {"electric_vehicles": [{"model": "X\\udc00", "daily_avg_km": 2}]}}'),
    ("/api/public/v1/calculators/emi/quotation/", '{"capacity_kw": 3, "packages": {"a\\ud800": {"system_cost": 200000}}}'),
    ("/api/public/v1/calculators/emi/quotation/", '{"capacity_kw": 3, "packages": {"a\\ud800": 5}}'),
]


@pytest.mark.parametrize(("path", "text"), SURROGATE_CASES, ids=[f"case{index}" for index in range(len(SURROGATE_CASES))])
def test_a_lone_surrogate_the_legacy_crashed_on_is_invalid_input(api_client, legacy_tables, path, text):
    response = api_client.post(path, data=text.encode(), content_type="application/json")
    assert response.status_code == 400, response.content[:300]
    assert response.json()["code"] == "invalid_input"


@pytest.mark.parametrize(
    ("path", "text"),
    [
        (ADVANCED, '{"Specifications": {"grid_type": "On Grid", "home_type": "\\ud800"}}'),  # compared in Python only
        ("/api/public/v1/calculators/basic/", '{"monthly_bill": 3000, "pincode": "682001", "property_type": "Residential", "x\\ud800": 1}'),  # never read
    ],
    ids=["compared-only", "unread-key"],
)
def test_a_lone_surrogate_the_legacy_never_used_is_harmless(api_client, legacy_tables, path, text):
    assert api_client.post(path, data=text.encode(), content_type="application/json").status_code == 200


# ── second review round ───────────────────────────────────────────────────────────────────────────────────────────
BASIC = "/api/public/v1/calculators/basic/"
BASIC_V2 = "/api/public/v1/calculators/basic-v2/"


@pytest.mark.parametrize("depth", [1000, 3000, 9000])
def test_basic_echoes_a_deeply_nested_property_type_like_the_legacy(api_client, legacy_tables, depth):
    """``calculate-solar`` echoes ``property_type`` unchecked. Recorded from the legacy UAT server: 200 with the value
    echoed up to 9,959 levels deep. The port's render check recursed in Python: from ~950 levels, HTTP 500 and a
    ``SystemException`` per anonymous request."""
    nested = "[" * depth + '"Residential"' + "]" * depth
    response = api_client.post(BASIC, data=('{"monthly_bill": 3000, "pincode": "682001", "property_type": ' + nested + "}").encode(), content_type="application/json")
    assert response.status_code == 200
    assert response.content.decode().endswith('"pincode":"682001","property_type":' + nested + "}")
    assert not SystemException.objects.exists()


@pytest.mark.parametrize("property_type", ["[null]", "[null, null]", "[[null], [null, null]]"])
def test_an_all_null_list_property_type_is_no_sizing_row(api_client, legacy_tables, property_type):
    """Recorded from the legacy UAT server: psycopg2 sent such a list as the text ``'{NULL,…}'``, so
    ``type__iexact`` found no row — 404; the port crashed on it (400 ``invalid_input``)."""
    text = '{"monthly_bill": 9000, "pincode": "682001", "property_type": ' + property_type + "}"
    response = api_client.post(BASIC_V2, data=text.encode(), content_type="application/json")
    assert response.status_code == 404
    assert response.json()["code"] == "no_sizing_row" and response.json()["message"] == "No data found for the given bill range and property type"


@pytest.mark.parametrize("property_type", ["[[]]", "[[null], []]", "[{}]", '[null, "a"]'])
def test_a_list_psycopg2_sent_as_an_array_is_invalid_input(api_client, legacy_tables, property_type):
    """Recorded from the legacy UAT server: HTTP 500 (``ARRAY[…]`` has no ``upper()``, or cannot be typed)."""
    text = '{"monthly_bill": 9000, "pincode": "682001", "property_type": ' + property_type + "}"
    response = api_client.post(BASIC_V2, data=text.encode(), content_type="application/json")
    assert response.status_code == 400 and response.json()["code"] == "invalid_input"


def test_an_empty_list_device_type_is_no_device(api_client, legacy_tables):
    """Recorded from the legacy UAT server (``calculate-solar-advanced``): ``device_type: []`` is falsy, so it is looked
    up (``name__iexact=[]`` → ``UPPER('{}')``) and found nowhere — a 0 W device; the port answered 400."""
    body = {
        "Specifications": {"grid_type": "On Grid", "home_type": "New Home", "estimated_base_load": 100},
        "usageDetails": {"usage_electronic_devices": [{"device_type": [], "daily_usage": 2, "no_of_units": 4}, {"device_type": "TV", "daily_usage": 3}]},
    }
    response = api_client.post(ADVANCED, body, format="json")
    assert response.status_code == 200
    assert response.json() == {
        "bill_range": 6000,
        "power_capacity": 3.0,
        "time_to_complete": "3-7",
        "overall_setup_cost": 230000.0,
        "total_subsidy": 78000.0,
        "emi_details": {"emi_per_month": 1725.93, "total_payment": 207111.51},
        "area_required": 240,
        "loan_available": "2,00,000",
        "per_kw_rate": 76667.0,
        "final_cost": 152000.0,
        "interest_rate": 6.5,
        "type": "Residential",
        "graph_without_solar": [0, 26407, 60110, 103124, 158022, 228088],
        "graph_with_solar": [152000.0, 261283, 373131, 388252, 407551, 432182],
        "savings": -204094,
    }
    body["usageDetails"]["usage_electronic_devices"][0]["device_type"] = [None]  # truthy: `.lower()` of a list (legacy 500)
    assert api_client.post(ADVANCED, body, format="json").json()["code"] == "invalid_input"
