"""Review findings on the emi app (calculators-emi review); each test failed before its fix.

* The legacy columns ``cibil_required``, ``approval_*_days``, ``max_tenure_years`` and the settings' tenure band,
  divisor and panel life were ``integer`` (up to 2 147 483 647); the platform columns are ``smallint``. A larger
  legacy value made PostgreSQL raise ``DataError`` (not ``IntegrityError``), which escaped the importer's per-row
  savepoint: the whole import call crashed instead of reporting the row.
* A ``PACK_RELEASE`` tile could not be selected: ``size_uid`` had to be a UUID, the provider's tile ids are texts.
* A bank slug or logo colour in the wrong format was refused by the database checks with a generic
  ``non_field_errors`` instead of naming the field.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from emi.models import Bank, EmiSettings
from emi.services import legacy_import, price_sources
from emi.tests import factories

pytestmark = pytest.mark.django_db


@pytest.fixture
def rows():
    return json.loads((Path(__file__).parent / "parity" / "uat" / "legacy_rows.json").read_text())["rows"]


def test_a_bank_value_beyond_the_column_range_is_reported_and_the_other_rows_imported(rows):
    banks = [dict(bank) for bank in rows["emi_bank"]]
    banks[0]["approval_max_days"] = 40_000  # a legacy PositiveIntegerField could hold it
    result = legacy_import.import_banks(banks)
    assert result["created"] == len(banks) - 1
    assert [violation["source_id"] for violation in result["violations"]] == [str(banks[0]["id"])]
    assert Bank.objects.count() == len(banks) - 1


def test_a_settings_value_beyond_the_column_range_is_reported(rows):
    settings = [{**rows["emi_calculator_settings"][0], "daily_saving_divisor": 70_000}]
    result = legacy_import.import_settings(settings)
    assert result["created"] == 0 and result["violations"][0]["field"] == "row"
    assert not EmiSettings.objects.exists()


# ── second review round ───────────────────────────────────────────────────────────────────────────────────────────
PREMIUM = "R12:ON_GRID:PREMIUM:3.00:1P:NONE"
ECONOMY = "R12:ON_GRID:ECONOMY:3.00:1P:NONE"


@pytest.fixture
def pack_release(settings):
    """``EMI_PRICE_SOURCE = PACK_RELEASE`` with a provider whose tile ids are not UUIDs (a release pack has none)."""
    settings.EMI_PRICE_SOURCE = "PACK_RELEASE"
    price_sources.register(
        lambda: [
            price_sources.SizeOption(uid=PREMIUM, label="3 kW Premium", capacity_kw=Decimal("3.00"), price_per_kw=Decimal("80000.00"), system_cost=Decimal("241234.00"), sort_order=1),
            price_sources.SizeOption(uid=ECONOMY, label="3 kW Economy", capacity_kw=Decimal("3.00"), price_per_kw=Decimal("70000.00"), system_cost=Decimal("210000.00"), sort_order=2),
        ],
        cache_namespaces=("packs",),
    )
    yield
    price_sources.reset()


def test_a_pack_release_tile_is_selected_by_the_uid_the_config_serves(api_client, pack_release):
    """The config serves each tile's ``uid`` for the website to post back as ``size_uid``; a ``PACK_RELEASE`` tile's
    uid is the provider's text, which ``size_uid`` refused (400 "Invalid numeric value in request") — and two packs
    of one size can only be told apart by it."""
    tiles = api_client.get("/api/public/v1/calculators/emi/config/").json()["system_sizes"]
    assert [tile["uid"] for tile in tiles] == [PREMIUM, ECONOMY]
    for tile in tiles:
        response = api_client.post("/api/public/v1/calculators/emi/", {"size_uid": tile["uid"]}, format="json")
        assert response.status_code == 200, response.json()
        assert response.json()["system"]["size_uid"] == tile["uid"] and response.json()["system"]["system_cost"] == tile["system_cost"]
    refused = api_client.post("/api/public/v1/calculators/emi/", {"size_uid": "R12:ON_GRID:PREMIUM"}, format="json")
    assert refused.status_code == 400 and refused.json()["code"] == "invalid_number"


@pytest.mark.parametrize(("field", "value"), [("slug", "SBI_Home"), ("slug", "sbi--home"), ("logo_bg", "blue"), ("logo_bg", "#12345")])
def test_a_bank_slug_or_colour_in_the_wrong_format_names_the_field(auth_client, make_user, field, value):
    """The database checks (``emi_bank_slug_format``, ``emi_bank_logo_bg_hex``) caught these, but the answer was a
    generic ``non_field_errors`` ("The values break a data rule.") — the Studio form could not point at the field."""
    editor = auth_client(make_user(grants={"emi": ["view", "edit"]}))
    response = editor.post("/api/v1/emi/banks/", {"name": "SBI", "abbr": "SBI", "annual_rate": "0.0565", field: value}, format="json")
    assert response.status_code == 400
    assert response.json()["code"] == "validation_error" and list(response.json()["errors"]) == [field]
    bank = factories.BankFactory(slug="sbi")
    patched = editor.patch(f"/api/v1/emi/banks/{bank.uid}/", {field: value}, format="json")
    assert patched.status_code == 400 and list(patched.json()["errors"]) == [field]
    assert Bank.objects.get(pk=bank.pk).version == 1
