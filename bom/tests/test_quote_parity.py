"""Byte parity of the website quote engine with the legacy ``POST /bom/api/calculate/``.

The UAT legacy database (the one the golden responses were captured from) is loaded through the catalog, pricing and
bom importers; every one of the 1,077 captured requests is replayed through :func:`bom.services.website_quote.quote`
with ``today`` = the capture day and compared as serialised JSON (same keys, key order, values and number types —
``13122.0`` is not ``13122``). The legacy 400 bodies are ``{"errors": QuoteInvalid.legacy_errors}``; the 9 legacy
HTTP 500 cases (``custom_discount`` sent as a number) are approved differences: a 400 ``validation_error``.
"""

import json
from datetime import date

import pytest

from bom.services.website_quote import QuoteInvalid, quote
from bom.tests.legacy_fixtures import GOLDEN_DAY, golden_cases, import_legacy_database

pytestmark = pytest.mark.django_db
TODAY = date.fromisoformat(GOLDEN_DAY)


def _dumped(value) -> str:
    return json.dumps(value, ensure_ascii=False)


@pytest.fixture
def legacy_data():
    return import_legacy_database()


def _replay(case):
    try:
        return 200, quote(case["request"], today=TODAY)
    except QuoteInvalid as exc:
        return 400, {"errors": exc.legacy_errors}


def test_every_captured_response_is_identical(legacy_data):
    golden = golden_cases()
    assert golden["captured_on"] == GOLDEN_DAY and len(golden["cases"]) == 1077
    mismatches, statuses, approved = [], {200: 0, 400: 0}, 0
    for index, case in enumerate(golden["cases"]):
        status, body = _replay(case)
        if case["status"] == 500:
            assert status == 400 and isinstance(case["request"].get("custom_discount"), (int, float)), case["request"]
            approved += 1
            continue
        statuses[case["status"]] += 1
        if status != case["status"] or _dumped(body) != _dumped(case["response"]):
            mismatches.append((index, case["request"]))
    assert not mismatches, f"{len(mismatches)} differing cases, first: {mismatches[:3]}"
    assert statuses == {200: 1059, 400: 9} and approved == 9


def test_the_legacy_500_cases_are_validation_errors(legacy_data):
    request = {"sys_type": "ongrid", "size": "3", "tier": "base", "bat_config": "0", "subsidy_type": "residential", "custom_discount": 7500}
    with pytest.raises(QuoteInvalid) as caught:
        quote(request, today=TODAY)
    assert caught.value.code == "validation_error" and "custom_discount" in caught.value.errors


def test_the_public_endpoint_serves_the_same_json(legacy_data, api_client, settings):
    from freezegun import freeze_time

    rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {**rates, "public_write": "1000/min"}}
    golden = golden_cases()["cases"]
    sample = [case for case in golden if case["status"] == 200][::40] + [case for case in golden if case["status"] == 400]
    with freeze_time(f"{GOLDEN_DAY} 12:00:00"):
        for case in sample:
            response = api_client.post("/api/public/v1/bom/quote/", case["request"], format="json")
            if case["status"] == 200:
                assert response.status_code == 200 and _dumped(response.json()) == _dumped(case["response"]), case["request"]
            else:
                assert response.status_code == 400 and response.json()["code"] == "validation_error"
                assert response.json()["message"] == "; ".join(case["response"]["errors"])
