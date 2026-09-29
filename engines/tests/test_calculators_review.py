"""Review findings on engines.website_calculators / engines.legacy_lookups (calculators-emi review).

Each case was reproduced against the legacy UAT server (``/api/calculate-solar-advanced/``) and the legacy
PostgreSQL database (C.UTF-8) before the fix.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from engines import emi
from engines import legacy_lookups as lookups
from engines import website_calculators as wc

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
    bill_range_sizes=(RESIDENTIAL_6000,),
    device_types=(wc.DeviceType("Toaster", 1000, 1.0, 1), wc.DeviceType("Fan", 60, 1.0, 2)),
    pincodes=frozenset({"682001"}),
)


class TestIexactIsPostgresUpperNotPythonUpper:
    """PostgreSQL ``UPPER()`` (C.UTF-8) maps one character to one character (the Unicode *simple* mapping); Python's
    ``str.upper()`` applies the full mapping, where a ligature or ``ß`` becomes two letters. ``filter(name__iexact=…)``
    must compare as PostgreSQL did (verified against every code point of the legacy database)."""

    @pytest.mark.parametrize(
        ("value", "column", "matches"),
        [
            ("Toaﬆer", "Toaster", False),  # U+FB06 ﬆ: PostgreSQL keeps it, Python makes "ST"
            ("straße", "STRASSE", False),  # ß stays ß in PostgreSQL
            ("ﬁan", "Fian", False),
            ("Reſidential", "Residential", True),  # long s: S in both
            ("ᾳ", "ᾼ", True),  # iota subscript: PostgreSQL gives the titlecase letter ᾼ, Python "ΑΙ"
            ("ΑΙ", "ᾼ", False),
            ("ǆ", "Ǆ", True),
            ("ς", "Σ", True),
            ("residential", "RESIDENTIAL", True),
        ],
    )
    def test_simple_case_mapping(self, value, column, matches):
        assert lookups.iexact_text(value)(column) is matches

    def test_a_ligature_device_name_is_not_the_catalogued_device(self):
        body = {
            "Specifications": {"grid_type": "On Grid", "home_type": "New Home", "estimated_base_load": 100},
            "usageDetails": {"usage_electronic_devices": [{"device_type": "Toaﬆer", "daily_usage": 2, "no_of_units": 4}]},
            "preferenceDetails": {},
        }
        plain = wc.advanced({**body, "usageDetails": {"usage_electronic_devices": []}}, DATA)
        assert wc.advanced(body, DATA)["graph_without_solar"] == plain["graph_without_solar"]  # legacy: 0 W (no such device)


class TestEmiSubsidyIsMoney:
    """A PLAN per-kW subsidy (``amount_per_kw`` × kW) carried fractions of a paisa into the customer's figures
    (``subsidy.amount``, ``net_cost_after_subsidy``); every other amount of the calculator is rounded to paise
    half-up. The legacy flat amounts are whole paise already, so parity is unaffected."""

    PER_KW = emi.SubsidyRule(uid="p", label="p", min_kw=None, max_kw=None, amount=Decimal("0"), amount_per_kw=Decimal("18000.55"))

    def test_per_kw_subsidy_is_rounded_to_paise_half_up(self):
        assert self.PER_KW.subsidy_for(Decimal("3.33")) == Decimal("59941.83")  # 59941.8315
        half = emi.SubsidyRule(uid="h", label="h", min_kw=None, max_kw=None, amount=Decimal("0"), amount_per_kw=Decimal("18000.25"))
        assert half.subsidy_for(Decimal("3.30")) == Decimal("59400.83")  # 59400.825, half-up (not half-even)
        flat = emi.SubsidyRule(uid="f", label="f", min_kw=None, max_kw=None, amount=Decimal("78000.00"))
        assert flat.subsidy_for(Decimal("3.33")) == Decimal("78000.00")

    def test_the_breakdown_prints_paise(self):
        size = emi.SystemSize(uid="00000000-0000-0000-0000-000000000333", label="3.33kW", capacity_kw=Decimal("3.33"), price_per_kw=Decimal("70000.00"))
        config = emi.EmiConfig(settings=emi.EmiSettings(), sizes=(size,), subsidy_rules=(self.PER_KW,))
        result = emi.calculate({"size_uid": size.uid}, config)
        assert result["subsidy"]["amount"] == 59941.83 and result["subsidy"]["net_cost_after_subsidy"] == 173158.17


# ── second review round ───────────────────────────────────────────────────────────────────────────────────────────
class TestIexactOfAListIsWhatPsycopg2Sent:
    """``filter(name__iexact=[])`` did not crash in the legacy: psycopg2 sends an empty list, or a list whose leaves are
    all ``None``, as the text literal ``'{}'`` / ``'{NULL,…}'`` (not an ``ARRAY[…]``), so PostgreSQL compared
    ``UPPER(name) = UPPER('{}')`` and found nothing. Every other list is an ``ARRAY[…]`` (``upper(text[])`` does not
    exist, or an empty inner array has no type): a crash. Recorded against the legacy ORM (Django 5.2, psycopg2 2.9)
    and the UAT server: ``calculate-solar-new`` with ``property_type: [null]`` is 404 "No data found …", and
    ``calculate-solar-advanced`` with ``device_type: []`` counts a 0 W device; the ports answered 400 ``invalid_input``."""

    @pytest.mark.parametrize(
        ("value", "column", "matches"),
        [
            ([], "{}", True),
            ([None], "{null}", True),
            ([None, None], "{NULL,NULL}", True),
            ([[None], [None, None]], "{{NULL},{NULL,NULL}}", True),
            ([], "TV", False),
            ([None], "TV", False),
        ],
    )
    def test_text_literal_lists(self, value, column, matches):
        assert lookups.iexact_text(value)(column) is matches

    @pytest.mark.parametrize("value", [[[]], [[None], []], [1], ["TV"], [None, "a"], [{}], {}, 0, False, 1.5])
    def test_array_or_other_values_crash(self, value):
        with pytest.raises(lookups.LegacyCrash):
            lookups.iexact_text(value)

    def test_an_empty_list_device_is_no_device(self):
        body = {
            "Specifications": {"grid_type": "On Grid", "home_type": "New Home", "estimated_base_load": 100},
            "usageDetails": {"usage_electronic_devices": [{"device_type": [], "daily_usage": 2, "no_of_units": 4}, {"device_type": "Fan", "daily_usage": 3}]},
        }
        only_fan = {**body, "usageDetails": {"usage_electronic_devices": [{"device_type": "Fan", "daily_usage": 3}]}}
        assert wc.advanced(body, DATA) == wc.advanced(only_fan, DATA)

    def test_an_all_null_property_type_is_no_sizing_row(self):
        with pytest.raises(wc.CalculatorError) as excinfo:
            wc.basic_v2({"monthly_bill": 5000, "pincode": "682001", "property_type": [None]}, DATA)
        assert (excinfo.value.code, excinfo.value.status) == ("no_sizing_row", 404)


class TestTheRenderCheckWalksDeepValuesWithoutRecursion:
    """``calculate-solar`` echoes ``property_type`` unchecked; the legacy rendered a value nested thousands of levels
    deep (its JSON parser and renderer are C code) — recorded: 200 up to 9,959 levels. ``check_renderable`` recursed
    in Python and hit the interpreter's limit (~950 levels): ``RecursionError``, HTTP 500 and a ``SystemException``."""

    @staticmethod
    def _nest(value, depth):
        for _ in range(depth):
            value = [value]
        return value

    def test_a_deep_value_is_checked(self):
        lookups.check_renderable({"property_type": self._nest("Residential", 5000)})
        with pytest.raises(lookups.LegacyCrash):
            lookups.check_renderable({"x": self._nest(float("inf"), 5000)})
        with pytest.raises(lookups.LegacyCrash):
            lookups.check_renderable([{"k": self._nest({"\ud800": 1}, 3000)}])

    def test_basic_echoes_a_deep_property_type(self):
        data = wc.CalculatorData(tariffs=TARIFFS, pincodes=frozenset({"682001"}))
        deep = self._nest("Residential", 3000)
        assert wc.basic({"monthly_bill": 3000, "pincode": "682001", "property_type": deep}, data)["property_type"] is deep


class _CountingName(str):
    """A device name that counts how often it is upper-cased (the per-device table scan did it for every device)."""

    calls = 0

    def upper(self):
        type(self).calls += 1
        return str.upper(self)


class TestAdvancedLooksNamesUpOnce:
    """The advanced port matched every device of the request against every catalogued device (``UPPER()`` of each
    name, per device): a 2 MB anonymous body (~40,000 devices) cost ~1 s of CPU against the UAT tables. Names are
    now indexed once per calculation, keeping the legacy ``.first()`` (primary-key order) on a clash."""

    def test_the_table_is_upper_cased_once_whatever_the_request_holds(self):
        _CountingName.calls = 0
        devices = tuple(wc.DeviceType(_CountingName(f"Device {index}"), 100, 1.0, index) for index in range(10))
        data = wc.CalculatorData(tariffs=TARIFFS, bill_range_sizes=(RESIDENTIAL_6000,), device_types=devices)
        requested = [{"device_type": f"device {index % 25}", "daily_usage": 1} for index in range(500)]
        body = {
            "Specifications": {"grid_type": "Hybrid", "home_type": "New Home"},
            "usageDetails": {"usage_electronic_devices": requested},
            "preferenceDetails": {"backup_hours": 2, "preference_electronic_devices": requested},
        }
        result = wc.advanced(body, data)
        assert _CountingName.calls <= len(devices)
        # the ten known names were found: 200 backup devices × 100 Wh + three lights (9 Wh) — no battery in this table
        assert result["battery_info"].endswith("which require 20.01 kWh of energy.")

    def test_the_first_device_in_legacy_order_wins_a_case_clash(self):
        body = {"Specifications": {"grid_type": "On Grid", "home_type": "New Home"}, "usageDetails": {"usage_electronic_devices": [{"device_type": "Fan", "daily_usage": 10}]}}
        clash = wc.CalculatorData(tariffs=TARIFFS, bill_range_sizes=(RESIDENTIAL_6000,), device_types=(wc.DeviceType("fan", 100, 1.0, 2), wc.DeviceType("FAN", 60, 1.0, 1)))
        first_only = wc.CalculatorData(tariffs=TARIFFS, bill_range_sizes=(RESIDENTIAL_6000,), device_types=(wc.DeviceType("FAN", 60, 1.0, 1),))
        assert wc.advanced(body, clash) == wc.advanced(body, first_only)

    def test_a_car_before_a_scooter_and_the_first_car_in_legacy_order(self):
        cars = (wc.Vehicle("Nexon", 0.15, 1.0, 2), wc.Vehicle("Nexon", 0.30, 1.0, 1))
        scooter = wc.Vehicle("Nexon", 0.05, 1.0, 0)
        data = wc.CalculatorData(tariffs=TARIFFS, bill_range_sizes=(RESIDENTIAL_6000,), ev_cars=cars, ev_scooters=(scooter,))
        first_car_only = wc.CalculatorData(tariffs=TARIFFS, bill_range_sizes=(RESIDENTIAL_6000,), ev_cars=(cars[1],))
        body = {"Specifications": {"grid_type": "On Grid", "home_type": "New Home"}, "usageDetails": {"electric_vehicles": [{"model": "Nexon", "daily_avg_km": 40}]}}
        assert wc.advanced(body, data) == wc.advanced(body, first_car_only)


class TestAPackReleaseTileIsSelectedByItsUid:
    """``SizeOption.uid`` (the ``PACK_RELEASE`` provider's tile id, served by the config) is any text — a pack of the
    release has no UUID (``packs_release_pack`` is keyed by release, system type, tier, size, phase and battery) —
    but ``size_uid`` was parsed as a UUID: the website could not select a pack tile (400 "Invalid numeric value")."""

    PACK = emi.SystemSize(uid="R12:ON_GRID:PREMIUM:3.00:1P:NONE", label="3 kW Premium", capacity_kw=Decimal("3.00"), price_per_kw=Decimal("80000.00"), system_cost=Decimal("241234.00"))
    MANUAL = emi.SystemSize(uid="0b1e4f0a-3c1f-4a55-9d59-0c2d6c0c0a01", label="5kW", capacity_kw=Decimal("5.00"), price_per_kw=Decimal("66000.00"))
    CONFIG = emi.EmiConfig(settings=emi.EmiSettings(), sizes=(PACK, MANUAL))

    def test_a_pack_tile_by_its_uid(self):
        result = emi.calculate({"size_uid": self.PACK.uid}, self.CONFIG)
        assert result["system"]["size_uid"] == self.PACK.uid and result["system"]["system_cost"] == 241234.0

    def test_a_uuid_in_any_spelling_still_selects_a_manual_tile(self):
        assert emi.calculate({"size_uid": self.MANUAL.uid.upper()}, self.CONFIG)["system"]["size_uid"] == self.MANUAL.uid

    @pytest.mark.parametrize("value", ["not-a-uid", "r12:on_grid:premium:3.00:1p:none", 5, ["x"]])
    def test_anything_else_is_still_an_invalid_value(self, value):
        with pytest.raises(emi.EmiError) as excinfo:
            emi.calculate({"size_uid": value}, self.CONFIG)
        assert excinfo.value.code == "invalid_number"
