"""Old main-backend reads through the shim, compared with the responses captured from the legacy server over the same
rows (each app's committed fixtures, imported through the owning importers): reference lists, products, metadata,
installation stats. Plus shape, caching and the legacy query parameters."""

import datetime as dt
import json
from decimal import Decimal
from unittest import mock

import pytest

from catalog.services import pricing_hooks
from catalog.tests.legacy_fixtures import import_all, legacy_response
from leads.services import legacy_import as leads_import
from leads.tests.conftest import legacy_pincodes, load_fixture  # noqa: F401 - fixture
from legacy.services.ids import SHIM_ID_OFFSET
from legacy.tests.conftest import ordered
from reference.services import legacy_import as reference_import
from reference.tests.factories import DeviceTypeFactory, PincodeOfficeFactory
from reference.tests.test_parity import IMPORTERS, RECORDED, TABLES
from seo.services.legacy_import import import_page_metadata
from seo.tests.test_legacy_import import FIXTURES as SEO_FIXTURES
from seo.tests.test_legacy_import import ROWS as METADATA_ROWS

pytestmark = pytest.mark.django_db


@pytest.fixture
def reference_rows():
    for table, importer in IMPORTERS.items():
        importer(RECORDED["rows"][table])


# ── reference lists ─────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("key", list(TABLES))
def test_reference_list_is_the_legacy_payload(api_client, reference_rows, key, django_assert_max_num_queries):
    with django_assert_max_num_queries(3):
        response = api_client.get(f"/legacy/api/{key}/")
    assert response.status_code == 200 and response["Cache-Control"] == "public, max-age=60" and response["ETag"]
    legacy = sorted(RECORDED["responses"][key], key=lambda row: row["id"])
    assert ordered(response) == legacy
    assert api_client.get(f"/legacy/api/{key}/")["X-Cache"] == "HIT"


def test_reference_field_order_is_the_legacy_serializer_order(api_client):
    DeviceTypeFactory()
    assert list(ordered(api_client.get("/legacy/api/device-types/"))[0]) == ["id", "name", "show_in_ui", "url", "watts"]


def test_pincodes_are_the_post_office_rows(api_client, reference_rows, django_assert_max_num_queries):
    with django_assert_max_num_queries(3):
        body = ordered(api_client.get("/legacy/api/pincodes/"))
    legacy = sorted(RECORDED["rows"]["pincodes"], key=lambda row: row["id"])
    assert [row["id"] for row in body] == [row["id"] for row in legacy]
    assert list(body[0]) == ["id", "pincode", "state", "district", "office_name", "region", "division"]
    assert body == [{key: row[key] for key in body[0]} for row in legacy]


def test_rows_created_on_the_platform_get_shim_ids(api_client, reference_rows):
    office = PincodeOfficeFactory()
    body = ordered(api_client.get("/legacy/api/pincodes/"))
    assert body[-1]["id"] == SHIM_ID_OFFSET + office.pk and body[-1]["office_name"] == office.office_name


def test_inactive_rows_are_hidden(api_client):
    DeviceTypeFactory(name="Shown")
    DeviceTypeFactory(name="Hidden", is_active=False)
    assert [row["name"] for row in ordered(api_client.get("/legacy/api/device-types/"))] == ["Shown"]


def test_reference_importer_is_the_row_source():
    assert set(IMPORTERS) >= {"kseb_tariffs", "device_types", "pincodes"} and reference_import.import_tariffs


# ── products ──────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def products():
    results = import_all(order=("website",))
    prices = {price["sku"]: Decimal(price["amount"]) for price in results["website"]["prices"] if price["source_table"] == "batteries"}

    def provider(components):
        return {component.pk: pricing_hooks.PriceInfo(min_amount=prices[component.sku], max_amount=prices[component.sku]) for component in components if component.sku in prices}

    previous = pricing_hooks.current_provider()
    pricing_hooks.register(provider)
    yield
    pricing_hooks.register(previous)


def _ties_by_id(rows, field):
    return sorted(rows, key=lambda row: (-(row[field] or 0), row["id"]))


@pytest.mark.parametrize("endpoint", ["solar-panels", "solar-inverters"])
def test_product_lists_are_the_legacy_payload(api_client, products, endpoint, django_assert_max_num_queries):
    with django_assert_max_num_queries(8):
        body = ordered(api_client.get(f"/legacy/api/{endpoint}/"))
    legacy = legacy_response(endpoint)
    assert body["meta"] == legacy["meta"]
    assert body["data"] == _ties_by_id(legacy["data"], "kerala_climate_score")  # legacy ties were heap order


def test_batteries_are_the_legacy_payload(api_client, products):
    assert ordered(api_client.get("/legacy/api/batteries/")) == legacy_response("batteries")


@pytest.mark.parametrize(
    "query,expected",
    [
        ("type=bifacial", lambda row: row["panel_type"] == "bifacial"),
        ("rating=excellent,very-good", lambda row: row["overall_rating"] in ("excellent", "very-good")),
        ("minEfficiency=21&maxEfficiency=22", lambda row: 21 <= float(row["efficiency"]) <= 22),
        ("minProductWarranty=15", lambda row: row["product_warranty"] >= 15),
        ("minPerformanceWarranty=30", lambda row: row["performance_warranty"] >= 30),
        ("brand=Waaree", lambda row: row["brand"] == "Waaree"),
        ("minKeralaScore=95", lambda row: row["kerala_climate_score"] >= 95),
        ("ids=1,2", lambda row: row["id"] in (1, 2)),
    ],
)
def test_panel_filters(api_client, products, query, expected):
    everything = legacy_response("solar-panels")["data"]
    body = ordered(api_client.get(f"/legacy/api/solar-panels/?{query}"))
    assert {row["id"] for row in body["data"]} == {row["id"] for row in everything if expected(row)}
    assert body["meta"]["total"] == len(body["data"])


def test_panel_sorting(api_client, products):
    ascending = ordered(api_client.get("/legacy/api/solar-panels/?sort=efficiency&order=asc"))["data"]
    assert [Decimal(row["efficiency"]) for row in ascending] == sorted(Decimal(row["efficiency"]) for row in ascending)
    descending = ordered(api_client.get("/legacy/api/solar-panels/?sort=wattage"))["data"]
    assert [row["wattage"] for row in descending] == sorted((row["wattage"] for row in descending), reverse=True)


@pytest.mark.parametrize(
    "query,expected",
    [
        ("type=hybrid", lambda row: row["inverter_type"] == "hybrid"),
        ("tier=premium", lambda row: row["rating_tier"] == "premium"),
        ("minWarranty=10", lambda row: row["warranty_years"] >= 10),
        ("extendableTo=15", lambda row: (row["extendable_warranty_years"] or 0) >= 15),
        ("brand=Enphase,Huawei", lambda row: row["brand"] in ("Enphase", "Huawei")),
    ],
)
def test_inverter_filters(api_client, products, query, expected):
    everything = legacy_response("solar-inverters")["data"]
    body = ordered(api_client.get(f"/legacy/api/solar-inverters/?{query}"))
    assert {row["id"] for row in body["data"]} == {row["id"] for row in everything if expected(row)}


@pytest.mark.parametrize("query", ["minEfficiency=abc", "ids=x", "sort=bogus", "minWarranty=ten"])
def test_malformed_parameters_are_400_not_the_legacy_500(api_client, products, query):
    endpoint = "solar-inverters" if "Warranty" in query else "solar-panels"
    response = api_client.get(f"/legacy/api/{endpoint}/?{query}")
    assert response.status_code == 400 and list(response.json()) == ["error"]


# ── metadata ──────────────────────────────────────────────────────────────────────────────────────────────────────
def test_metadata_is_the_legacy_payload(api_client, django_assert_max_num_queries):
    import_page_metadata(METADATA_ROWS)
    with django_assert_max_num_queries(3):
        response = api_client.get("/legacy/api/metadata/")
    assert response.content == (SEO_FIXTURES / "metadata_list.json").read_bytes().strip()


# ── installation stats ────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("dataset", ["uat", "enriched"])
def test_installation_stats_are_the_legacy_payload(api_client, legacy_pincodes, dataset):  # noqa: F811
    golden = load_fixture("installation_stats.json")
    leads_import.import_customer_installations(load_fixture(f"customer_installations_{dataset}.json"))
    with mock.patch("django.utils.timezone.localdate", return_value=dt.date.fromisoformat(golden["captured_on"])):
        for case in golden[dataset]:
            if case.get("raw_query") or isinstance(case["pincode"], list):
                continue
            query = "" if case["pincode"] is None else f"?pincode={case['pincode']}"
            response = api_client.get(f"/legacy/api/installation-stats/{query}")
            assert (response.status_code, ordered(response)) == (case["status"], case["body"]), case


def test_installation_stats_is_cached(api_client):
    assert api_client.get("/legacy/api/installation-stats/?pincode=688008")["X-Cache"] == "MISS"
    assert api_client.get("/legacy/api/installation-stats/?pincode=688008")["X-Cache"] == "HIT"


def test_fixture_files_exist():
    assert json.loads((SEO_FIXTURES / "metadata_list.json").read_text())
