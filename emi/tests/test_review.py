"""Review findings on the emi app (calculators-emi review); each test failed before its fix.

* The legacy columns ``cibil_required``, ``approval_*_days``, ``max_tenure_years`` and the settings' tenure band,
  divisor and panel life were ``integer`` (up to 2 147 483 647); the platform columns are ``smallint``. A larger
  legacy value made PostgreSQL raise ``DataError`` (not ``IntegrityError``), which escaped the importer's per-row
  savepoint: the whole import call crashed instead of reporting the row.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from emi.models import Bank, EmiSettings
from emi.services import legacy_import

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
