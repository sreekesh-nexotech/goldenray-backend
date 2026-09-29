"""Legacy import of the Purchase Agreement page's ``flarize_agr`` export (fixture exported from the real page by
``fixtures/export_pa_records.mjs``: its demo records plus seven records saved through its own ``saveAgr()``)."""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from agreements.models import Agreement
from agreements.services.legacy_import import import_pa_agreements, report_pa_catalog
from agreements.tests.factories import kseb_fees
from core.models import LegacyMap
from engines.frozen import sha256_hex

FIXTURES = Path(__file__).parent / "fixtures" / "pa"


def _records():
    return json.loads((FIXTURES / "flarize_agr.json").read_text())


@pytest.fixture
def fees(db):
    return kseb_fees()


def _by_id(record_id: str) -> Agreement:
    return Agreement.objects.get(legacy_ref=f"crs/{record_id}")


def test_import_is_idempotent_and_reports(fees, company):
    records = _records()
    result = import_pa_agreements(records, profile="crs")
    assert (result["created"], result["updated"], result["skipped"]) == (7, 0, 0)
    codes = [violation["code"] for violation in result["violations"]]
    assert codes.count("demo_record_not_migrated") == 5
    assert codes.count("customer_without_phone") == 5  # one PA without a phone + the four Sale Order / Extra Structure records
    assert codes.count("customer_created") == 2
    assert Agreement.objects.filter(legacy=True, status="ISSUED").count() == 7
    again = import_pa_agreements(records, profile="crs")
    assert (again["created"], again["updated"], again["skipped"]) == (0, 0, 7)
    assert [v["code"] for v in again["violations"]].count("customer_without_phone") == 0
    assert Agreement.objects.count() == 7 and LegacyMap.objects.filter(source_system="PA", source_table="flarize_agr").count() == 7
    from customers.models import Customer

    assert Customer.objects.filter(source="PA_IMPORT").count() == 7


def test_typed_columns_are_parsed_and_the_record_is_kept(fees, company):
    records = _records()
    import_pa_agreements(records, profile="crs")
    raw = next(record for record in records if record["data"].get("quoteno") == "1024")
    agreement = _by_id(raw["id"])
    assert agreement.number == f"CRS-{raw['id']}" and agreement.kind == "PURCHASE_AGREEMENT" and agreement.legacy and agreement.quotation_version is None
    assert agreement.issued_at.isoformat() == raw["createdAt"].replace(".000Z", "+00:00") and agreement.created_at == agreement.issued_at
    assert agreement.legacy_quotation_ref == "QUO-GR-AS-26-1024"
    assert (agreement.capacity_kw, agreement.phase, agreement.system_type, agreement.variant) == (Decimal("5.00"), "1P", "ON_GRID", "PREMIUM")
    assert (agreement.panel_label, agreement.panel_dcr, agreement.panel_capacity_w, agreement.panel_capacity_label) == ("Waaree - Mono Perc Bifacial - DCR", True, 545, "545W – 580W")
    assert (agreement.inverter_brand, agreement.inverter_type, agreement.battery_label) == ("Growatt", "STRING", "")
    assert (agreement.walkway_required, agreement.ladder_required, agreement.extra_structure) == (True, False, False)
    assert (agreement.original_price, agreement.discount, agreement.final_price) == (Decimal("335000.00"), Decimal("20000.00"), Decimal("315000.00"))
    assert agreement.statutory_fee == fees[1] and agreement.statutory_fee_amount == Decimal("7800.00")
    assert agreement.customer.phone_e164 == "+919000000101" and agreement.customer.address.startswith("House 12")
    assert agreement.payload["legacy_record"] == raw and agreement.payload_sha256 == sha256_hex(agreement.payload)
    assert agreement.payload["agreement"]["uid"] == str(agreement.uid)
    hybrid = _by_id(next(record for record in records if record["data"].get("kw") == "5KW Plant with 6KW Inverter – Hybrid")["id"])
    assert (hybrid.system_type, hybrid.capacity_kw, hybrid.phase, hybrid.battery_label, hybrid.extra_cost) == ("HYBRID", Decimal("5.00"), "3P", "5.0 kWh", Decimal("19500.00"))
    assert hybrid.statutory_fee is None and hybrid.statutory_fee_amount == Decimal("13600.00")
    sale = _by_id(next(record for record in records if record["data"].get("amt") == "585000")["id"])
    assert (sale.kind, sale.original_price, sale.extra_cost, sale.final_price, sale.extra_description) == (
        "SALE_ORDER",
        Decimal("585000.00"),
        Decimal("19500.00"),
        Decimal("604500.00"),
        "Raised GI structure for east-facing slope",
    )
    extra = _by_id(next(record for record in records if record["data"].get("total") == "372000")["id"])
    assert (extra.kind, extra.original_price, extra.extra_cost, extra.final_price, extra.statutory_fee_amount) == (
        "EXTRA_STRUCTURE",
        Decimal("372000.00"),
        Decimal("19500.00"),
        Decimal("391500.00"),
        None,
    )


def test_changed_record_rebuilds_and_bad_rows_are_reported(fees, company):
    records = _records()
    import_pa_agreements(records, profile="crs")
    changed = copy.deepcopy(records)
    target = next(record for record in changed if record["data"].get("quoteno") == "1024")
    target["data"]["discount"] = "25000"
    target["data"]["total"] = "abc"
    changed += [{"id": "agr_x", "type": 9, "data": {}}, {"id": "agr_y", "type": 1, "createdAt": "never", "data": {}}, "junk"]
    result = import_pa_agreements(changed, profile="crs")
    assert (result["created"], result["updated"], result["skipped"]) == (0, 1, 6)
    codes = {violation["code"] for violation in result["violations"]}
    assert {"legacy_record_changed", "unparsed_value", "invalid_record"} <= codes
    agreement = _by_id(target["id"])
    assert agreement.discount == Decimal("25000.00") and agreement.final_price is None and agreement.payload["legacy_record"] == target


def test_profiles_are_separate_and_dry_run_writes_nothing(fees, company):
    records = _records()
    result = import_pa_agreements(records, profile="admin", dry_run=True)
    assert result["created"] == 7 and not Agreement.objects.exists()
    import_pa_agreements(records, profile="crs")
    import_pa_agreements(records, profile="admin")
    assert Agreement.objects.count() == 14 and Agreement.objects.filter(number__startswith="ADMIN-").count() == 7


def test_the_upstash_catalog_is_compared_not_imported(world):
    catalog = json.loads((FIXTURES / "catalog.json").read_text())
    result = report_pa_catalog(catalog)
    assert result["created"] == 0 and result["updated"] == 0
    listed = {violation["source_id"] for violation in result["violations"] if violation["code"] == "not_in_catalog"}
    assert "kseb:3 KW|5400" not in listed and "kseb:15 KW|19500" in listed and world["fees"]
    assert "inverters:Growatt" in listed
    assert any(violation["code"] == "listed_only" for violation in result["violations"])
