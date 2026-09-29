"""engines.website_calculators / engines.legacy_lookups: the legacy semantics the parity corpora rely on, case by case.

The recorded corpora (calculators/tests/test_parity.py) prove the ports end to end against the legacy server; these
tests pin the individual rules without a database, including the ones no recorded data can reach (an empty tariff
table, rows the database would refuse, int32 overflow of the ORM).
"""

from __future__ import annotations

import ast
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from engines import legacy_lookups as lookups
from engines import website_calculators as wc

ENGINES = Path(__file__).resolve().parents[1]

TARIFFS = (
    wc.TariffSlab(0, 300, Decimal("6.75"), 1),
    wc.TariffSlab(301, 350, Decimal("7.60"), 2),
    wc.TariffSlab(351, 400, Decimal("7.95"), 3),
    wc.TariffSlab(401, 500, Decimal("8.25"), 4),
    wc.TariffSlab(501, None, Decimal("9.20"), 5),
)
RESIDENTIAL_6000 = wc.BillRangeSize(
    6000, "Residential", Decimal("3"), "3-7", Decimal("230000"), Decimal("78000"), 240, "2,00,000", Decimal("76667"), Decimal("152000"), Decimal("6.50"), Decimal("55000"), 1
)
DATA = wc.CalculatorData(
    tariffs=TARIFFS,
    capacity_sizes=(wc.CapacitySize(Decimal("5"), 5, Decimal("340000"), Decimal("97500"), 400),),
    bill_range_sizes=(RESIDENTIAL_6000,),
    device_types=(wc.DeviceType("TV", 100, 1.0, 1), wc.DeviceType("AC 1 ton", 1200, 0.35, 2)),
    ev_cars=(wc.Vehicle("Tata Nexon EV", 0.152, 1.0, 1),),
    ev_scooters=(wc.Vehicle("Ather 450X", 0.028, None, 1),),
    batteries=(wc.Battery(Decimal("4.61"), Decimal("140300"), 1), wc.Battery(Decimal("5.00"), Decimal("145000"), 2)),
    pincodes=frozenset({"682001"}),
)


class TestLegacyLookups:
    def test_exact_text_compares_str_of_the_value(self):
        assert lookups.exact_text(682001)("682001") and not lookups.exact_text(682001.0)("682001")
        assert lookups.exact_text(["x"])("['x']") and not lookups.exact_text(None)("None")
        with pytest.raises(lookups.LegacyCrash):
            lookups.exact_text("68\x0001")

    def test_iexact_is_upper_equality_of_a_text_only(self):
        assert lookups.iexact_text("residential")("Residential")
        assert not lookups.iexact_text(None)("None")
        assert not lookups.iexact_text("Resi_dential")("Residential")
        for value in (5, True, ["Residential"], {"a": 1}, 1.5):
            with pytest.raises(lookups.LegacyCrash):
                lookups.iexact_text(value)
        with pytest.raises(lookups.LegacyCrash):
            lookups.iexact_text("Res\x00")

    def test_integer_lookups_truncate_ceil_and_overflow_like_the_orm(self):
        assert lookups.int_lte(12.9)(12) and not lookups.int_lte(12.9)(13)
        assert lookups.int_gte(12.1)(13) and not lookups.int_gte(12.1)(12)
        assert lookups.int_lte(1e30)(10**9) and not lookups.int_lte(-1e30)(0)
        assert not lookups.int_gte(1e30)(10**9) and lookups.int_gte(-1e30)(0)
        for value, error in ((float("inf"), OverflowError), (float("nan"), ValueError)):
            with pytest.raises(error):
                lookups.int_lte(value)
            with pytest.raises(error):
                lookups.int_gte(value)

    def test_decimal_lookups_round_a_float_to_the_column_digits(self):
        assert lookups.decimal_param(4.61000000000001, 10) == Decimal("4.610000000")
        assert lookups.decimal_param(3.0000001, 6) == Decimal("3.00000")
        assert lookups.decimal_param(7, 6) == Decimal(7) and lookups.decimal_param(None, 6) is None
        for value in (float("inf"), float("nan"), "abc"):
            with pytest.raises(lookups.LegacyCrash):
                lookups.decimal_param(value, 10)

    def test_float_lookup_and_finite_check(self):
        assert lookups.float_exact(5)(5.0)
        with pytest.raises(lookups.LegacyCrash):
            lookups.check_renderable({"a": [1.0, float("inf")]})
        lookups.check_renderable({"a": [1.0, (2, 3.5)], "b": None})


class TestBasic:
    def test_bill_through_the_slabs(self):
        result = wc.basic({"monthly_bill": 3000, "pincode": "682001", "property_type": "Residential"}, DATA)
        assert result["estimated_units"] == 400.05 and result["solar_capacity_kW"] == 5 and result["total_cost"] == 340000.0

    def test_a_bill_beyond_every_finite_slab_keeps_whole_units(self):
        data = wc.CalculatorData(tariffs=(wc.TariffSlab(0, 100, Decimal("5"), 1),), pincodes=frozenset({"682001"}))
        result = wc.basic({"monthly_bill": 5000, "pincode": "682001", "property_type": "x"}, data)
        assert result["estimated_units"] == 100 and isinstance(result["estimated_units"], int)

    def test_no_priced_size_gives_nulls(self):
        result = wc.basic({"monthly_bill": 9000, "pincode": "682001", "property_type": {"any": "json"}}, DATA)
        assert (result["total_cost"], result["area_required"], result["property_type"]) == (None, None, {"any": "json"})

    @pytest.mark.parametrize(
        ("body", "code", "status"),
        [
            ({"pincode": "682001", "property_type": "R"}, "missing_fields", 400),
            ({"monthly_bill": "3000", "pincode": "682001", "property_type": "R"}, "invalid_monthly_bill", 400),
            ({"monthly_bill": -1, "pincode": "682001", "property_type": "R"}, "invalid_monthly_bill", 400),
            ({"monthly_bill": 3000, "pincode": "999999", "property_type": "R"}, "pincode_not_found", 404),
            ({"monthly_bill": float("inf"), "pincode": "682001", "property_type": "R"}, "invalid_input", 400),
            ([1], "invalid_input", 400),
        ],
    )
    def test_errors(self, body, code, status):
        with pytest.raises(wc.CalculatorError) as excinfo:
            wc.basic(body, DATA)
        assert (excinfo.value.code, excinfo.value.status) == (code, status)

    def test_a_zero_rate_slab_is_a_crash(self):
        data = wc.CalculatorData(tariffs=(wc.TariffSlab(0, None, Decimal("0"), 1),), pincodes=frozenset({"682001"}))
        with pytest.raises(wc.CalculatorError) as excinfo:
            wc.basic({"monthly_bill": 3000, "pincode": "682001", "property_type": "R"}, data)
        assert excinfo.value.code == "invalid_input" and excinfo.value.detail  # the cause is kept for the log


class TestBasicV2:
    def test_bands(self):
        assert [wc.bill_range_for(bill) for bill in (-5, 6000, 6001, 15500, 15501, 40000)] == [6000, 6000, 8000, 15500, 20000, 40000]
        assert wc.bill_range_for(40001) is None

    def test_row_graph_and_emi(self):
        result = wc.basic_v2({"monthly_bill": "5000", "pincode": 682001, "property_type": "RESIDENTIAL"}, DATA)
        assert result["solar_capacity_kW"] == 3.0 and result["interest_rate"] == 6.5 and result["emi_details"]["emi_per_month"] > 0
        assert result["datasets"][1]["data"][0] == 152000.0 and result["savings"] == result["datasets"][0]["data"][-1] - result["datasets"][1]["data"][-1]

    def test_without_any_tariff_the_legacy_view_crashed(self):
        data = wc.CalculatorData(bill_range_sizes=(RESIDENTIAL_6000,), pincodes=frozenset({"682001"}))
        with pytest.raises(wc.CalculatorError) as excinfo:
            wc.basic_v2({"monthly_bill": 5000, "pincode": "682001", "property_type": "Residential"}, data)
        assert excinfo.value.code == "invalid_input"

    def test_a_row_without_a_final_cost_is_a_crash(self):
        row = wc.BillRangeSize(6000, "Residential", Decimal("3"), "3-7", Decimal("1"), Decimal("0"), 1, "N/A", final_cost=None)
        data = wc.CalculatorData(tariffs=TARIFFS, bill_range_sizes=(row,), pincodes=frozenset({"682001"}))
        with pytest.raises(wc.CalculatorError) as excinfo:
            wc.basic_v2({"monthly_bill": 5000, "pincode": "682001", "property_type": "Residential"}, data)
        assert excinfo.value.code == "invalid_input"

    def test_loan_text(self):
        assert [wc._loan_amount(text) for text in ("2,00,000-6,00,000", "75,000", "N/A", "x-y", None)] == [200000, 75000, 0, 0, 0]


class TestAdvanced:
    def test_on_grid(self):
        body = {"Specifications": {"grid_type": "On Grid", "average_bill": 2000}, "usageDetails": {"usage_electronic_devices": [{"device_type": "tv", "no_of_units": 2, "daily_usage": 4}]}}
        result = wc.advanced(body, DATA)
        assert result["type"] == "Residential" and result["bill_range"] == 6000 and "battery_capacity" not in result

    def test_hybrid_default_backup_uses_the_smallest_sufficient_battery(self):
        result = wc.advanced({"Specifications": {"grid_type": "Hybrid"}}, DATA)
        assert result["default_backup_hours"] == 3 and result["battery_capacity"] == 4.61 and result["inverter_price"] == 55000.0

    def test_hybrid_without_a_sufficient_battery_takes_the_largest(self):
        body = {"Specifications": {"grid_type": "Hybrid"}, "preferenceDetails": {"backup_hours": 4, "preference_electronic_devices": [{"device_type": "AC 1 ton", "no_of_units": 4, "daily_usage": 4}]}}
        result = wc.advanced(body, DATA)
        assert result["battery_capacity"] == 5.0 and result["battery_info"].startswith("No battery can provide 4 hours")

    def test_hybrid_without_batteries(self):
        data = wc.CalculatorData(tariffs=TARIFFS, bill_range_sizes=(RESIDENTIAL_6000,))
        result = wc.advanced({"Specifications": {"grid_type": "Hybrid"}}, data)
        assert result["battery_info"].startswith("No battery found that can provide 3 hours")

    def test_ev_lookup_is_exact_and_falls_back_to_scooters(self):
        advanced = wc._Advanced(DATA)
        assert advanced.vehicle("Ather 450X").model == "Ather 450X" and advanced.vehicle("ather 450x") is None

    def test_no_residential_row_is_404(self):
        with pytest.raises(wc.CalculatorError) as excinfo:
            wc.advanced({"Specifications": {"grid_type": "On Grid"}}, wc.CalculatorData(tariffs=TARIFFS))
        assert excinfo.value.status == 404

    def test_the_average_bill_needs_a_tariff(self):
        with pytest.raises(wc.CalculatorError) as excinfo:
            wc.advanced({"Specifications": {"grid_type": "On Grid", "average_bill": 100}}, wc.CalculatorData(bill_range_sizes=(RESIDENTIAL_6000,)))
        assert excinfo.value.code == "invalid_input"


def test_emi_helpers_match_the_two_legacy_functions():
    assert wc.emi_with_interest(0, 8.9) == {"emi_per_month": 0, "total_payment": 0, "total_interest": 0}
    assert wc.emi_advanced(100000, 0) == {"emi_per_month": 0, "total_payment": 0}
    assert wc.emi_with_interest(252000.0, 8.9)["emi_per_month"] == wc.emi_advanced(252000.0, 8.9)["emi_per_month"]


@pytest.mark.parametrize("module", ["website_calculators", "emi", "legacy_lookups"])
def test_the_ports_import_only_the_standard_library_and_engines(module):
    names = set()
    for node in ast.walk(ast.parse((ENGINES / f"{module}.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    assert not {name for name in names if name not in ("engines", "__future__") and name not in sys.stdlib_module_names}
