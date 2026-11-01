"""pricing.services.legacy_import — idempotence, dry runs, D-2 (Flarize wins, every difference reported) in both orders."""

from decimal import Decimal

import pytest
from django.db import connection

from audit.models import AuditLog
from catalog.tests import legacy_fixtures as catalog_fixtures
from core.models import LegacyMap
from pricing.models import CostConfig, InstallationMatrix, MarketRate, MarketRateSet, Offer, OfferTransition, Price, PriceKind, StatutoryFee, ValidityKind, ValidityPolicy
from pricing.services import cost_config, legacy_import, market_rates, validity
from pricing.tests.legacy_fixtures import KSEB_FEES, engine_configs, flarize_catalog_pricing, flarize_file, goldenapp

pytestmark = pytest.mark.django_db


def current_values() -> dict:
    return cost_config.current_values()


def cells() -> dict:
    # Keyed on the row's full uniqueness tuple, `dcr` included: without it a DCR and a
    # non-DCR price for the same tier/band/size collapse into one entry here, and a
    # comparison could pass while one of the two was being lost.
    return {
        (r.system_type, r.tier, r.battery_config, r.dcr, r.size_key, r.from_size_key, r.future_size_key, r.variant): r.customer_price_incl_gst
        for r in MarketRate.objects.all()
    }


def codes(result, code) -> list[dict]:
    return [violation for violation in result["violations"] if violation["code"] == code]


class TestGlobalCostsAndCatalogCosts:
    def test_backend_then_flarize(self):
        first = legacy_import.import_bom_global_costs(goldenapp("bom_globalcosts"))
        assert first["created"] == 13 and first["violations"] == []
        values = current_values()
        assert values["gi_rate_per_kg"] == 130 and values["structure_repair_pct"] == 0.1 and values["office_per_project"] == 5500
        rerun = legacy_import.import_bom_global_costs(goldenapp("bom_globalcosts"))
        assert rerun["created"] == 0 and rerun["updated"] == 0 and rerun["skipped"] == 13
        flarize = legacy_import.import_flarize_pricing(flarize_catalog_pricing())
        differences = {violation["key"] for violation in codes(flarize, "d2_flarize_wins") if "key" in violation}
        assert differences == {"gi_rate_per_kg", "structure_labor"}
        values = current_values()
        assert values["gi_rate_per_kg"] == 98.3 and values["structure_labor"] == 3000 and values["transport_extra_rate_per_km"] == 35
        assert values["office_expense_monthly"] == 150000 and values["expected_projects_per_month"] == 10
        assert CostConfig.objects.filter(key="gi_rate_per_kg").count() == 2  # the backend value stays as history

    def test_flarize_then_backend_gives_the_same_values(self):
        legacy_import.import_flarize_pricing(flarize_catalog_pricing())
        flarize_first = current_values()
        backend = legacy_import.import_bom_global_costs(goldenapp("bom_globalcosts"))
        assert {violation["key"] for violation in codes(backend, "d2_flarize_wins")} == {"gi_rate_per_kg", "structure_labor"}
        assert backend["created"] == 0 and current_values() == flarize_first

    def test_dry_run_writes_nothing_but_reports(self):
        result = legacy_import.import_bom_global_costs(goldenapp("bom_globalcosts"), dry_run=True)
        assert result["created"] == 13 and not CostConfig.objects.exists() and not LegacyMap.objects.exists()

    def test_extra_rows_and_bad_values(self):
        row = goldenapp("bom_globalcosts")[0]
        result = legacy_import.import_bom_global_costs([row, {**row, "id": 2}, {**row, "id": 0, "install_rate": "abc"}])
        assert codes(result, "extra_global_costs_row") and codes(result, "invalid_value")


class TestMarketRates:
    def test_both_orders_give_the_same_cells_and_report_every_difference(self):
        backend = legacy_import.import_bom_market_rates(goldenapp("bom_marketrate"))
        assert backend["created"] == 58 and backend["violations"] == []
        rate_set = MarketRateSet.objects.get()
        assert rate_set.name.startswith("Imported ") and rate_set.status == "DRAFT"
        flarize = legacy_import.import_flarize_pricing(flarize_catalog_pricing())
        differences = codes(flarize, "d2_flarize_wins")
        kept = {(v["kept"], v["other"]) for v in differences if "kept" in v and v["kept"] not in ("98.3", "3000")}
        # Flarize wins on a real disagreement, both directions.
        assert kept >= {("228000.00", "230000.00"), ("549901.80", "0.00")}
        # But a zero is "not priced", not a price: `ongrid_base[3]` is 0 in catalog.json
        # against ₹202,539.30 from the main backend, and the price survives. Golden-Ray-BOM
        # version-2 ships a largely emptied list (3 of 59 cells carry a figure), so letting
        # a zero through would move nearly every quote onto cost-plus -- the engine falls
        # back to `grand_total` whenever the rate is 0. An import may add and correct
        # prices; it may not delete them.
        assert ("0.00", "202539.30") not in kept
        assert ("202539.30", "0") in {(v["kept"], v["other"]) for v in codes(flarize, "zero_rate_kept_existing") if "kept" in v}
        backend_first = cells()
        assert MarketRateSet.objects.count() == 1 and len(backend_first) == 59  # + hybrid_value_2_diffBase
        MarketRate.all_objects.all().delete()
        MarketRateSet.all_objects.all().delete()
        LegacyMap.objects.all().delete()
        legacy_import.import_flarize_pricing(flarize_catalog_pricing())
        reverse = legacy_import.import_bom_market_rates(goldenapp("bom_marketrate"))
        # Two real disagreements plus the zero, which is reported under its own code
        # rather than as a Flarize win -- in whichever order the imports ran.
        # One real disagreement wins for Flarize; the two emptied cells take the main
        # backend's figure instead, because a cell with no price is not a price.
        assert len(codes(reverse, "d2_flarize_wins")) == 1
        assert len(codes(reverse, "zero_rate_kept_existing")) == 2
        # The point of the zero rule being symmetric: both orders end at the same cells,
        # which is what this test has always been for.
        assert cells() == backend_first
        again = legacy_import.import_bom_market_rates(goldenapp("bom_marketrate"))
        assert again["created"] == 0 and again["updated"] == 0

    def test_module_content_keys_import_as_separate_prices(self):
        """``_dcr`` / ``_ndcr`` keys are their own cells, including alongside ``_up``.

        Hybrid is quoted separately for DCR and non-DCR modules — the two differ by
        ₹38k–₹64k on the live list — and the key can carry the module content, a future
        size and a variant at once: ``hybrid_value_0_dcr_up5_diffBase``.

        Before the BOM port added the ``dcr`` column, ``MARKET_KEY_RE`` matched ``variant``
        only *after* ``_up``, so none of those shapes parsed: 12 of the 34 live keys were
        reported as ``unknown_market_rate_key`` and skipped, four of them carrying real
        prices. The import still said it succeeded, which is why it went unnoticed.
        """
        rates = {
            "hybrid_value_0_dcr": {"3": 310000, "5": 394000},
            "hybrid_value_0_ndcr": {"3": 272000, "5": 336419},
            "hybrid_value_1_dcr": {"3": 433000},
            "hybrid_value_1_ndcr": {"3": 392089},
            "hybrid_value_0_dcr_up5": {"3": 339000},
            "hybrid_value_0_ndcr_up5": {"3": 300616},
            "hybrid_value_0_dcr_up5_diffBase": {"3": 0},
            "ongrid_value": {"3": 245000},
            "ongrid_value_up5sp": {"3": 290000},
        }
        result = legacy_import.import_flarize_pricing({"marketRates": rates})
        assert not codes(result, "unknown_market_rate_key"), "every key shape must parse"

        # A key names a column of sizes, so one key yields one row per size -- the cell is
        # (key, size_key), which is why this is keyed on both.
        by_cell = {(market_rates.rate_key(row), row.size_key): row for row in MarketRate.objects.all()}
        expected = {(key, size) for key, sizes in rates.items() for size in sizes}
        assert expected <= set(by_cell), f"missing cells: {sorted(expected - set(by_cell))}"

        # The two module contents are distinct prices, not one overwriting the other.
        assert by_cell[("hybrid_value_0_dcr", "3")].customer_price_incl_gst == Decimal("310000.00")
        assert by_cell[("hybrid_value_0_dcr", "5")].customer_price_incl_gst == Decimal("394000.00")
        assert by_cell[("hybrid_value_0_ndcr", "3")].customer_price_incl_gst == Decimal("272000.00")
        assert by_cell[("hybrid_value_0_ndcr", "5")].customer_price_incl_gst == Decimal("336419.00")
        # The ₹38k gap between the two contents at 3 kW is exactly why they cannot share a cell.
        assert by_cell[("hybrid_value_0_dcr", "3")].dcr == "dcr"
        assert by_cell[("hybrid_value_0_ndcr", "3")].dcr == "ndcr"

        # Module content, future size and variant coexist on one row.
        combined = by_cell[("hybrid_value_0_dcr_up5_diffBase", "3")]
        assert (combined.dcr, combined.future_size_key, combined.variant) == ("dcr", "5", "diffBase")

        # On-grid is one price either way, so it carries no module content.
        assert by_cell[("ongrid_value", "3")].dcr == ""
        assert by_cell[("ongrid_value_up5sp", "3")].future_size_key == "5sp"

        # Re-importing changes nothing — the lookup matches on `dcr` too, so the second
        # pass recognises the rows rather than colliding them.
        again = legacy_import.import_flarize_pricing({"marketRates": rates})
        assert again["created"] == 0 and again["updated"] == 0

    def test_module_content_is_rejected_on_non_hybrid_rows(self):
        """Only hybrid is priced per module content; the DB constraint says so too."""
        clean, errors = market_rates.normalise_rate(
            {"system_type": "ONGRID", "tier": "VALUE", "size_key": "3", "dcr": "dcr", "customer_price_incl_gst": Decimal("1")}, 0
        )
        assert not clean and "rates[0].dcr" in errors

    def test_locked_set_and_bad_rows(self):
        legacy_import.import_bom_market_rates(goldenapp("bom_marketrate"))
        MarketRateSet.objects.update(status="ACTIVE")
        locked = legacy_import.import_bom_market_rates(goldenapp("bom_marketrate"))
        assert codes(locked, "market_rate_set_locked")
        MarketRateSet.objects.update(status="RETIRED")
        MarketRateSet.objects.all().delete()
        rows = [
            {"id": 90, "system_type": "solar", "tier": "base", "bat_config": "", "from_size": "", "to_size": "", "size_rates": {"3": 1}},
            {"id": 91, "system_type": "ongrid", "tier": "base", "bat_config": "", "from_size": "", "to_size": "", "size_rates": {}},
            {"id": 92, "system_type": "upgrade", "tier": "", "bat_config": "", "from_size": "3", "to_size": "5", "size_rates": {"rate": 1, "extra": 2}},
            {"id": 93, "system_type": "ongrid", "tier": "base", "bat_config": "", "from_size": "", "to_size": "", "size_rates": '{"3": null, "x": 5}'},
        ]
        result = legacy_import.import_bom_market_rates(rows)
        assert {v["code"] for v in result["violations"]} >= {"unknown_value", "empty_size_rates", "unknown_upgrade_key", "null_rate", "invalid_value"}


class TestOffers:
    def test_backend_and_flarize_offers(self):
        backend = legacy_import.import_bom_offers(goldenapp("bom_offer"))
        assert backend["created"] == 3
        assert dict(Offer.objects.values_list("code", "status")) == {"UAT-MONSOON-5000": "ACTIVE", "UAT-HYBRID-3PCT": "ACTIVE", "UAT-EXPIRED": "ACTIVE"}
        flarize = legacy_import.import_flarize_pricing(flarize_catalog_pricing())
        assert codes(flarize, "possible_duplicate_offer")
        offer = Offer.objects.get(code="offer_1788107665827")
        assert offer.status == "ACTIVE" and offer.content_version == 1 and offer.applies_to_size_key == ""
        assert list(OfferTransition.objects.filter(offer=offer).values_list("to_status", "by_label")) == [("DRAFT", "admin-001"), ("APPROVED", "admin-001"), ("ACTIVE", "admin-001")]
        again = legacy_import.import_flarize_pricing(flarize_catalog_pricing())
        assert OfferTransition.objects.filter(offer=offer).count() == 3 and again["created"] == 0

    def test_same_code_d2(self):
        catalog = flarize_catalog_pricing()
        catalog["offers"][0]["id"] = "UAT-MONSOON-5000"
        catalog["offers"][0]["value"] = 6000
        legacy_import.import_flarize_pricing(catalog)
        backend = legacy_import.import_bom_offers(goldenapp("bom_offer"))
        assert codes(backend, "d2_flarize_wins")[0]["fields"] == ["value"]
        assert Offer.objects.get(code="UAT-MONSOON-5000").value == Decimal("6000.00")
        Offer.all_objects.all().delete()
        LegacyMap.objects.all().delete()
        legacy_import.import_bom_offers(goldenapp("bom_offer"))
        flarize = legacy_import.import_flarize_pricing(catalog)
        assert codes(flarize, "d2_flarize_wins") and Offer.objects.get(code="UAT-MONSOON-5000").value == Decimal("6000.00")

    def test_bad_offer_rows(self):
        row = goldenapp("bom_offer")[0]
        result = legacy_import.import_bom_offers(
            [{**row, "id": 7, "offer_type": "bogo"}, {**row, "id": 8, "offer_type": "percent", "value": Decimal("150")}, {**row, "id": 9, "offer_id": "X9", "name": ""}]
        )
        assert len(result["violations"]) == 3 and not Offer.objects.exists()


class TestFlarizeDocumentsMatrixAndFees:
    def test_documents(self):
        result = legacy_import.import_flarize_documents(
            cost_config_json=flarize_file("cost-config.json"),
            rate_card_json=flarize_file("project-rate-card.json"),
            quotation_policy_json=flarize_file("quotation-policy.json"),
            engine_configs=engine_configs(),
        )
        assert result["violations"] == [], result["violations"]
        values = current_values()
        assert values["cost_engine.config"]["schema"] == "flarize.cost-config/2"
        assert (values["gst_goods_share"], values["gst_goods_rate"], values["gst_services_share"], values["gst_services_rate"]) == (0.7, 0.05, 0.3, 0.18)
        assert values["target_gross_margin_by_tier"]["BASE"] == {"target": 0.2, "minimum": 0.15}
        assert set(values) >= {"rate_card", "energy.config", "savings.config", "subsidy.config", "finance.config"}
        assert ValidityPolicy.objects.get(kind=ValidityKind.DEFAULT).days == 15
        assert ValidityPolicy.objects.filter(kind=ValidityKind.WINDOW).count() == 27
        again = legacy_import.import_flarize_documents(cost_config_json=flarize_file("cost-config.json"), quotation_policy_json=flarize_file("quotation-policy.json"))
        assert again["created"] == 0 and again["updated"] == 0
        from django.utils.dateparse import parse_datetime

        assert validity.resolve(parse_datetime("2026-09-20T00:00:00Z"))["days"] == 7
        assert validity.resolve(parse_datetime("2026-10-20T00:00:00Z"))["days"] == 15
        bad = legacy_import.import_flarize_documents(engine_configs={"weather": {}, "energy": {"regions": {}}})
        assert {v["code"] for v in bad["violations"]} == {"unknown_document", "validation_error"}

    def test_installation_matrix(self):
        legacy_import.import_flarize_pricing(flarize_catalog_pricing())
        assert InstallationMatrix.objects.count() == 18
        five = InstallationMatrix.objects.get(size_kw=5, phase="3P", installation_type="ELEVATED")
        assert five.install_cost == Decimal("32000.00") and five.size_key == "5tp"
        catalog = flarize_catalog_pricing()
        catalog["installationMatrix"]["3"]["flat"] = 16000
        catalog["installationMatrix"]["bad"] = {"flat": 1}
        catalog["installationMatrix"]["4"] = {"roofless": 1}
        result = legacy_import.import_flarize_pricing(catalog)
        assert InstallationMatrix.objects.get(size_kw=3, installation_type="FLAT").install_cost == Decimal("16000.00")
        assert len(codes(result, "unknown_value")) == 2

    def test_kseb_fees(self):
        result = legacy_import.import_pa_kseb_fees(KSEB_FEES + ["20 KW — ₹ 21,000|21000", "no capacity|1"])
        assert result["created"] == 7 and codes(result, "invalid_value")
        assert list(StatutoryFee.objects.order_by("capacity_kw_max").values_list("label", "amount"))[0] == ("3 KW", Decimal("5400.00"))
        assert legacy_import.import_pa_kseb_fees(KSEB_FEES)["skipped"] == 6
        assert legacy_import.import_pa_kseb_fees([{"v": "5500", "l": "3 KW — ₹ 5,500"}])["updated"] == 1


class TestPrices:
    def test_d2_in_both_orders_and_idempotence(self):
        results = catalog_fixtures.import_all(order=("flarize", "bom"))
        flarize_first = legacy_import.import_prices(results["flarize"]["prices"])
        bom_second = legacy_import.import_prices(results["bom"]["prices"])
        d2 = codes(bom_second, "d2_flarize_wins")
        assert d2 and all(v["severity"] == "warning" for v in d2)
        order_a = {row.component.sku: row.amount for row in Price.objects.filter(kind=PriceKind.LIST, effective_to__isnull=True).select_related("component")}
        assert flarize_first["created"] > 200 and legacy_import.import_prices(results["flarize"]["prices"])["created"] == 0
        with connection.cursor() as cursor:  # rows are append-only (a DELETE is refused); start the other order afresh
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute("TRUNCATE pricing_price CASCADE")
        LegacyMap.objects.filter(source_table__startswith="price:").delete()
        bom_first = legacy_import.import_prices(results["bom"]["prices"])
        flarize_second = legacy_import.import_prices(results["flarize"]["prices"])
        assert bom_first["violations"] == [] and len(codes(flarize_second, "d2_flarize_wins")) == len(d2)
        order_b = {row.component.sku: row.amount for row in Price.objects.filter(kind=PriceKind.LIST, effective_to__isnull=True).select_related("component")}
        assert order_a == order_b
        # PLAN §7.6 #7: the current LIST price is the catalog.json price, or the BOM price where Flarize has none.
        expected = {row["sku"]: Decimal(row["amount"]) for row in results["bom"]["prices"] if row["kind"] == "LIST" and row["amount"] is not None}
        expected.update({row["sku"]: Decimal(row["amount"]) for row in results["flarize"]["prices"] if row["kind"] == "LIST" and row["amount"] is not None})
        assert order_a == expected
        assert AuditLog.objects.filter(action="pricing.legacy_import").count() == 5

    def test_platform_prices_are_never_replaced(self):
        from django.db import transaction

        from catalog.tests.factories import ComponentFactory
        from pricing.models import PriceSource
        from pricing.services.prices import write_price

        component = ComponentFactory(sku="zz1")
        with transaction.atomic():
            write_price(component, PriceKind.LIST, Decimal("10"), user=None, source=PriceSource.MANUAL)
        result = legacy_import.import_prices(
            [{"sku": "zz1", "kind": "LIST", "amount": Decimal("12"), "per_watt": None, "source_system": "FLARIZE", "source_table": "catalog.json:items", "source_id": "zz1"}]
        )
        assert codes(result, "platform_price_kept")
        missing = legacy_import.import_prices(
            [{"sku": "nope", "kind": "LIST", "amount": Decimal("1"), "per_watt": None, "source_system": "BACKEND", "source_table": "bom_catalogitem", "source_id": "999"}]
        )
        assert codes(missing, "component_not_found")
        bad = legacy_import.import_prices([{"sku": "zz1", "kind": "SELL", "amount": Decimal("1"), "source_system": "BACKEND", "source_table": "x", "source_id": "1"}])
        assert codes(bad, "invalid_value")
