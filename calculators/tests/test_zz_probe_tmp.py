"""TEMPORARY reviewer probe (not committed): nesting depths the JSON parser accepts vs what the ports survive."""

import pytest

from core.models import SystemException

pytestmark = pytest.mark.django_db

PATHS = {
    "basic": ("/api/public/v1/calculators/basic/", '{{"monthly_bill": 3000, "pincode": "682001", "property_type": {nested}}}'),
    "basic-pincode": ("/api/public/v1/calculators/basic/", '{{"monthly_bill": 3000, "pincode": {nested}, "property_type": "Residential"}}'),
    "v2-pincode": ("/api/public/v1/calculators/basic-v2/", '{{"monthly_bill": 3000, "pincode": {nested}, "property_type": "Residential"}}'),
    "advanced-model": ("/api/public/v1/calculators/advanced/", '{{"Specifications": {{"grid_type": "On Grid"}}, "usageDetails": {{"electric_vehicles": [{{"model": {nested}}}]}}}}'),
    "emi-quote": ("/api/public/v1/calculators/emi/quotation/", '{{"capacity_kw": 3, "packages": {{"a": {nested}}}}}'),
    "emi-quote-key": ("/api/public/v1/calculators/emi/quotation/", '{{"capacity_kw": 3, "packages": {{"a": {{"system_cost": {nested}}}}}}}'),
    "emi": ("/api/public/v1/calculators/emi/", '{{"capacity_kw": 3, "apply_subsidy": {nested}}}'),
}


@pytest.mark.parametrize("key", list(PATHS))
def test_probe(api_client, legacy_tables, key):
    path, template = PATHS[key]
    results = {}
    for depth in (100, 300, 500, 700, 800, 900, 950, 1000, 1100, 1200, 1400, 1600, 2000, 3000, 5000):
        nested = "[" * depth + '"682001"' + "]" * depth
        response = api_client.post(path, data=template.format(nested=nested).encode(), content_type="application/json")
        results[depth] = (response.status_code, response.json().get("code") if response.status_code != 200 else "ok")
    print(key, results, SystemException.objects.count())
    assert all(status != 500 for status, _code in results.values()), results
