"""Defects found by the adversarial review of pricing (each test failed before its fix).

* Market-rate size keys that ``numeric(6,2)`` cannot hold or that are not positive (``0``, ``99999``) reached the
  database: 500.
* An offer's ``applies_to_size_key`` ``0`` was reported as ``offer_code_taken`` (409) on create and was a 500 on
  PATCH; a key larger than ``numeric(6,2)`` was a 500 on both.
* A cost-configuration PUT dated in the future became the current value at once (nothing resolves rows by date).
* One legacy row whose value does not fit its column (``DataError``) aborted the whole import instead of being
  reported as a violation of that row.
* A ``bom_marketrate`` on-grid/hybrid row carrying ``from_size``/``to_size`` (no home: only upgrade rows have them)
  was imported as a plain row and silently replaced the cells of the row the calculator reads.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from pricing.models import CostConfig, MarketRate, Offer
from pricing.services import legacy_import
from pricing.tests.factories import CostConfigFactory, MarketRateSetFactory, OfferFactory
from pricing.tests.legacy_fixtures import flarize_catalog_pricing, goldenapp

pytestmark = pytest.mark.django_db
SETS = "/api/v1/pricing/market-rate-sets/"
OFFERS = "/api/v1/pricing/offers/"
CONFIG = "/api/v1/pricing/cost-config/"


class TestMarketRateSizeKeys:
    @pytest.mark.parametrize(
        "row,field",
        [
            ({"system_type": "ONGRID", "tier": "VALUE", "size_key": "0"}, "rates[0].size_key"),
            ({"system_type": "ONGRID", "tier": "VALUE", "size_key": "99999"}, "rates[0].size_key"),
            ({"system_type": "ONGRID", "tier": "VALUE", "size_key": "3.125"}, "rates[0].size_key"),
            ({"system_type": "ONGRID", "tier": "VALUE", "size_key": "3", "future_size_key": "0"}, "rates[0].future_size_key"),
            ({"system_type": "UPGRADE", "size_key": "5", "from_size_key": "99999"}, "rates[0].from_size_key"),
            ({"system_type": "UPGRADE", "size_key": "5", "from_size_key": "0"}, "rates[0].from_size_key"),
        ],
    )
    def test_sizes_the_columns_cannot_hold_are_a_validation_error(self, client, row, field):
        rate_set = MarketRateSetFactory()
        response = client.put(f"{SETS}{rate_set.uid}/rates/", {"rates": [{**row, "customer_price_incl_gst": "1"}]}, format="json")
        assert response.status_code == 400, response.content
        assert response.json()["code"] == "validation_error" and field in response.json()["errors"]
        assert not MarketRate.objects.exists()

    def test_the_largest_size_that_fits_is_accepted(self, client):
        rate_set = MarketRateSetFactory()
        row = {"system_type": "ONGRID", "tier": "VALUE", "size_key": "9999.99", "customer_price_incl_gst": "1"}
        response = client.put(f"{SETS}{rate_set.uid}/rates/", {"rates": [row]}, format="json")
        assert response.status_code == 200, response.content
        assert MarketRate.objects.get().size_kw == Decimal("9999.99")


class TestOfferSizeKey:
    @pytest.mark.parametrize("key", ["0", "99999999"])
    def test_create_and_patch_refuse_sizes_that_cannot_be_stored(self, client, key):
        created = client.post(OFFERS, {"name": "Size", "type": "FLAT", "value": "5", "applies_to_size_key": key}, format="json")
        assert created.status_code == 400, created.content
        assert created.json()["code"] == "validation_error" and "applies_to_size_key" in created.json()["errors"]
        offer = OfferFactory()
        patched = client.patch(f"{OFFERS}{offer.uid}/", {"applies_to_size_key": key}, format="json")
        assert patched.status_code == 400, patched.content
        assert "applies_to_size_key" in patched.json()["errors"]
        offer.refresh_from_db()
        assert offer.applies_to_size_key == "" and Offer.objects.count() == 1


class TestCostConfigDates:
    def test_a_future_effective_date_is_refused(self, client):
        CostConfigFactory(key="install_rate", value=2500)
        future = str(timezone.localdate() + timedelta(days=30))
        response = client.put(CONFIG, {"entries": [{"key": "install_rate", "value": 3000}], "effective_from": future}, format="json")
        assert response.status_code == 400, response.content
        assert response.json()["code"] == "effective_from_in_future" and "effective_from" in response.json()["errors"]
        current = {row["key"]: row["value"] for row in client.get(CONFIG).json()["current"]}
        assert current["install_rate"] == 2500 and CostConfig.objects.count() == 1

    def test_today_is_accepted(self, client):
        response = client.put(CONFIG, {"entries": [{"key": "install_rate", "value": 3000}], "effective_from": str(timezone.localdate())}, format="json")
        assert response.status_code == 200, response.content


class TestImportRowsThatDoNotFit:
    def test_a_value_too_long_for_its_column_rejects_only_that_row(self):
        catalog = flarize_catalog_pricing()
        good = catalog["offers"][0]
        catalog["offers"] = [{**good, "id": "offer_long_name", "name": "N" * 200}, good]
        result = legacy_import.import_flarize_pricing(catalog)
        bad = [violation for violation in result["violations"] if violation["source_id"] == "offer_long_name"]
        assert bad and bad[0]["severity"] == "error"
        assert list(Offer.objects.values_list("code", flat=True)) == [good["id"]]
        assert MarketRate.objects.exists() and CostConfig.objects.exists()

    def test_a_non_upgrade_row_with_upgrade_sizes_is_reported_not_merged(self):
        rows = goldenapp("bom_marketrate")
        plain = next(row for row in rows if row["system_type"] == "ongrid" and row["tier"] == "value")
        odd = {**plain, "id": 99, "from_size": "3", "to_size": "5", "size_rates": {"3": 1, "5sp": 2}}
        result = legacy_import.import_bom_market_rates([plain, odd])
        assert [(v["source_id"], v["code"]) for v in result["violations"]] == [("99", "upgrade_sizes_on_non_upgrade")]
        cell = MarketRate.objects.get(system_type="ONGRID", tier="VALUE", size_key="3")
        assert cell.customer_price_incl_gst == Decimal(str(plain["size_rates"]["3"]))  # what the calculator reads
        assert MarketRate.objects.get(system_type="ONGRID", tier="VALUE", size_key="5sp").customer_price_incl_gst == Decimal(str(plain["size_rates"]["5sp"]))

    def test_market_rate_and_matrix_sizes_that_do_not_fit_are_reported(self):
        catalog = flarize_catalog_pricing()
        catalog["marketRates"]["ongrid_value"]["99999"] = 1
        catalog["installationMatrix"]["99999"] = {"flat": 1}
        result = legacy_import.import_flarize_pricing(catalog)
        sources = {violation["source_id"] for violation in result["violations"] if violation["severity"] == "error"}
        assert {"ongrid_value:99999", "99999:flat"} <= sources
        assert MarketRate.objects.filter(size_key="3").exists()
