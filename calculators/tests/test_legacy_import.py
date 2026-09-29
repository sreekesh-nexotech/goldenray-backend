"""calculators.services.legacy_import on the committed UAT rows (exported read-only from legacy_goldenapp)."""

from decimal import Decimal

import pytest

from audit.models import AuditLog
from calculators.models import BillRangeSize, CapacitySize, PropertyType
from calculators.services import legacy_import
from calculators.tests.parity.support import load_rows
from core.models import LegacyMap
from flarize.cache_utils import get_versions

pytestmark = pytest.mark.django_db


@pytest.fixture
def rows():
    return load_rows("uat")


def test_imports_every_column_of_both_tables(rows):
    result = legacy_import.import_all(solar_installations=rows["solar_installations"], solar_installation_new=rows["solar_installation_new"])
    assert result["solar_installations"] == {"created": 10, "updated": 0, "skipped": 0, "violations": []}
    assert result["solar_installation_new"] == {"created": 18, "updated": 0, "skipped": 0, "violations": []}
    four = CapacitySize.objects.get(power_capacity_kw=4)
    assert (four.installation_days, four.total_cost, four.total_subsidy, four.area_required_sqft) == (4, Decimal("272000.00"), Decimal("78000.00"), 320)
    first = BillRangeSize.objects.get(bill_range=6000, property_type=PropertyType.RESIDENTIAL)
    assert first.installation_days_range == "3-7" and first.loan_available == "2,00,000"
    assert first.interest_rate == Decimal("0.0650") and first.per_kw_rate == Decimal("76667.00") and first.inverter_price == Decimal("55000.00")
    assert first.created_at.isoformat() == "2026-09-28T17:39:27.303798+00:00"
    no_inverter = BillRangeSize.objects.get(bill_range=14000, property_type=PropertyType.COMMERCIAL)
    assert no_inverter.inverter_price is None and no_inverter.interest_rate == Decimal("0.0000")
    assert LegacyMap.objects.filter(source_system="BACKEND", source_table="solar_installation_new").count() == 18


def test_re_runs_update_and_never_duplicate(rows):
    legacy_import.import_capacity_sizes(rows["solar_installations"])
    assert legacy_import.import_capacity_sizes(rows["solar_installations"]) == {"created": 0, "updated": 0, "skipped": 10, "violations": []}
    changed = [dict(row) for row in rows["solar_installations"]]
    changed[0]["total_cost"] = "275000.00"
    result = legacy_import.import_capacity_sizes(changed)
    assert (result["created"], result["updated"], result["skipped"]) == (0, 1, 9)
    assert CapacitySize.objects.get(power_capacity_kw=4).total_cost == Decimal("275000.00")
    assert CapacitySize.objects.count() == 10


def test_a_row_deleted_in_the_platform_stays_deleted(rows):
    legacy_import.import_capacity_sizes(rows["solar_installations"])
    CapacitySize.objects.get(power_capacity_kw=4).soft_delete()
    result = legacy_import.import_capacity_sizes(rows["solar_installations"])
    assert result["skipped"] == 10 and CapacitySize.objects.count() == 9


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"power_capacity": 0}, "power_capacity"),
        ({"power_capacity": 3.1415}, "power_capacity"),
        ({"total_cost": "abc"}, "total_cost"),
        ({"total_cost": "-5.00"}, "total_cost"),
        ({"total_subsidy": None}, "total_subsidy"),
        ({"time_to_complete": 2.5}, "time_to_complete"),
        ({"area_required": -1}, "area_required"),
        ({"area_required": True}, "area_required"),
    ],
)
def test_capacity_rows_that_would_need_inventing_or_rounding_are_reported(rows, change, field):
    result = legacy_import.import_capacity_sizes([{**rows["solar_installations"][0], **change}])
    assert result["created"] == 0 and [violation["field"] for violation in result["violations"]] == [field]


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"type": "Industrial"}, "type"),
        ({"type": None}, "type"),
        ({"interest_rate": "6.505"}, "interest_rate"),
        ({"bill_range": 0}, "bill_range"),
        ({"final_cost": "1e20"}, "final_cost"),
    ],
)
def test_bill_range_rows_that_would_need_inventing_or_rounding_are_reported(rows, change, field):
    result = legacy_import.import_bill_range_sizes([{**rows["solar_installation_new"][0], **change}])
    assert result["created"] == 0 and field in [violation["field"] for violation in result["violations"]]


def test_a_type_in_another_case_imports_with_a_note(rows):
    result = legacy_import.import_bill_range_sizes([{**rows["solar_installation_new"][0], "type": "residential"}])
    assert result["created"] == 1 and result["violations"][0]["field"] == "type"
    assert BillRangeSize.objects.get().property_type == PropertyType.RESIDENTIAL


def test_duplicates_are_refused_by_the_database(rows):
    row = rows["solar_installations"][0]
    result = legacy_import.import_capacity_sizes([row, {**row, "id": 999}])
    assert result["created"] == 1 and result["violations"][0]["field"] == "row"


def test_audited_once_per_batch_and_cache_bumped(rows):
    before = get_versions(["calculators:sizes"])["calculators:sizes"]
    legacy_import.import_bill_range_sizes(rows["solar_installation_new"])
    entry = AuditLog.objects.get(action="calculators.legacy_imported")
    assert entry.after["rows"] == 18 and entry.after["created"] == 18 and len(entry.after["checksum"]) == 64
    assert get_versions(["calculators:sizes"])["calculators:sizes"] > before
