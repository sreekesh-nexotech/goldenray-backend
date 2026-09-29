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
