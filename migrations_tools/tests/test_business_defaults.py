"""The business-default lists of the import reports (docs/decisions/business-defaults.md B-2, B-3, B-5, B-12)."""

import json

import pytest

from bom.models import Slot
from migrations_tools.services import business_defaults
from migrations_tools.tests.conftest import BACKEND_FIXTURE, load_tables, run

pytestmark = pytest.mark.django_db


def test_violations_are_grouped_under_their_decision():
    labelled = [
        ("catalog.json:catalog", {"code": "d2_flarize_wins", "source_id": "p1", "message": "price 140 → 150"}),
        ("releases", {"code": "pack_config_market_rate_wins", "source_id": "ONGRID/VALUE/3", "message": "229000 replaces 228000"}),
        ("B-5 hybrid slot phases", {"code": business_defaults.B5_CODE, "source_id": "i9", "message": "…"}),
        ("emi_bank", {"source_id": "3", "field": "slug", "message": "not a slug"}),
        ("emi_bank", {"source_id": "4", "field": "logo_bg", "message": "not a hex colour"}),
        ("emi_bank", {"source_id": "5", "field": "features", "message": "not a list"}),
        ("bom_catalogitem", {"code": "slug", "source_id": "7"}),  # a "slug" code elsewhere is not B-12
    ]
    grouped = business_defaults.group(labelled)
    assert list(grouped) == ["B-2", "B-3", "B-5", "B-12"]
    assert [violation["source_id"] for violation in grouped["B-12"]] == ["3", "4"]
    assert business_defaults.group([]) == {}


def test_changed_values_are_described_with_both_values():
    tiers = {"message": "p1: catalog.json wins over bom_catalogitem.", "differences": [{"field": "tiers", "bom": ["BASE", "VALUE"], "flarize": ["BASE"]}]}
    assert business_defaults.describe(tiers) == "p1: catalog.json wins over bom_catalogitem.; tiers: ['BASE', 'VALUE'] → ['BASE']"
    fixed = {"message": "1/MC4 Connector: catalog.json replaces the main-backend values.", "bom": {"code": ""}, "flarize": {"code": "fi_mc4"}}
    assert business_defaults.describe(fixed).endswith("code: '' → 'fi_mc4'")
    assert business_defaults.describe({"message": "p2 LIST: FLARIZE 13585.00 replaces 14300.00."}) == "p2 LIST: FLARIZE 13585.00 replaces 14300.00."


def test_backend_import_lists_emi_banks_and_hybrid_slot_mismatches(db, tmp_path):
    """B-12 and B-5 on the main backend import: a malformed bank is skipped and listed; the Studio components a HYB
    slot never matches are listed after the BOM step (legacy matching unchanged)."""
    from accounts.services.seeds import seed_roles

    seed_roles()
    tables = load_tables(BACKEND_FIXTURE)
    bank = dict(tables["emi_bank"][0], id=999, slug="Not A Slug!")
    tables["emi_bank"] = [*tables["emi_bank"], bank]
    source = tmp_path / "backend.json"
    source.write_text(json.dumps({"tables": tables}, default=str))
    report = tmp_path / "report.json"
    out = run("import_backend", "--source-fixture", str(source), "--json", str(report))
    data = json.loads(report.read_text())
    assert [violation["source_id"] for violation in data["business_defaults"]["B-12"]] == ["999"]
    assert "B-12 — EMI banks skipped for a malformed slug or logo colour: 1" in out
    expected = business_defaults.hybrid_phase_mismatches()["violations"]
    assert data["business_defaults"].get("B-5", []) == expected
    if Slot.objects.filter(filter_phase="HYB").exists():
        assert expected and all(violation["code"] == business_defaults.B5_CODE for violation in expected)
