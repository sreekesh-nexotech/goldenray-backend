"""``installations/stats/`` == the legacy ``installation-stats/`` for every captured query.

Golden responses (``fixtures/legacy_backend/installation_stats.json``) were captured from the shared UAT legacy server
("uat": its 20 seeded installations) and from a private copy enriched with 8 more (capture-year rows, other statuses,
multi-district, out-of-list and foreign pincodes — ``enrich_private.sql``). Here the same installations (exported
read-only, personal data masked) go through ``leads.services.legacy_import`` and the legacy ``pincodes`` table is the
pincode directory; every 200 body must be identical, and a missing/empty pincode is a 400 on ``pincode`` with the
legacy message.
"""

import datetime as dt
from unittest import mock

import pytest

from leads.services import legacy_import
from leads.tests.conftest import load_fixture

pytestmark = pytest.mark.django_db
GOLDEN = load_fixture("installation_stats.json")
CAPTURED_ON = dt.date.fromisoformat(GOLDEN["captured_on"])


def _query(case) -> str:
    if case.get("raw_query"):
        return case["raw_query"]
    pincode = case["pincode"]
    if pincode is None:
        return ""
    values = pincode if isinstance(pincode, list) else [pincode]
    from urllib.parse import urlencode

    return urlencode([("pincode", value) for value in values])


@pytest.mark.parametrize("dataset", ["uat", "enriched"])
def test_every_captured_query_matches(api_client, legacy_pincodes, dataset):
    rows = load_fixture(f"customer_installations_{dataset}.json")
    result = legacy_import.import_customer_installations(rows)
    assert (result["created"], result["violations"]) == (len(rows), [])
    with mock.patch("django.utils.timezone.localdate", return_value=CAPTURED_ON):
        for case in GOLDEN[dataset]:
            response = api_client.get(f"/api/public/v1/installations/stats/?{_query(case)}")
            if case["status"] == 200:
                assert response.status_code == 200, (case, response.json())
                assert response.json() == case["body"], case
            else:
                assert response.status_code == case["status"] == 400
                assert response.json()["errors"] == {"pincode": [case["body"]["error"]]}


def test_golden_covers_the_interesting_variants():
    bodies = [case["body"] for case in GOLDEN["enriched"] if case["status"] == 200]
    districts = {body["district"] for body in bodies}
    assert {"ALAPPUZHA", "PATHANAMTHITTA", "Ernakulam", "Alappuzha", "Kerala"} <= districts  # listed, first-of-two, fallbacks
    assert any(body["current_year_installations"] for body in bodies) and any(body["pincode_installations"] and not body["district_installations"] for body in bodies)
