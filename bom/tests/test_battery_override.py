"""``battery_override``: a Studio user picks the battery count of a hybrid package (no battery / one / two)."""

from datetime import date
from types import SimpleNamespace

import pytest

from bom.services import website_quote
from bom.services.website_quote import Calculator, QuoteInvalid, parse_request


def _band(params: dict, *, composite: bool = True, bands=None) -> str:
    """``Calculator._battery_band`` on a bare calculator (the band reads only the request, the profiles and the regime)."""
    regime = type("Regime", (Calculator,), {"_composite": composite})
    calc = regime.__new__(regime)
    calc.p = params
    calc.s = SimpleNamespace(battery_bands=bands if bands is not None else {"hybrid_value": 1})
    return calc._battery_band(params["sys_type"], params["tier"])


@pytest.mark.parametrize("override", ["0", "1", "2"])
def test_override_wins_over_the_package_band_for_hybrid(override):
    params = parse_request({"sys_type": "hybrid", "size": "5", "tier": "value", "battery_override": override})
    assert _band(params) == override
    assert _band(params, composite=False) == override


def test_without_override_the_package_decides():
    params = parse_request({"sys_type": "hybrid", "size": "5", "tier": "value", "bat_config": "2"})
    assert params["battery_override"] == "" and _band(params) == "1"


def test_on_grid_never_carries_a_battery():
    params = parse_request({"sys_type": "ongrid", "size": "3", "tier": "value", "battery_override": "2"})
    assert _band(params) == "0"


@pytest.mark.parametrize("value", ["3", "one", 1])
def test_invalid_override_is_a_400(value):
    with pytest.raises(QuoteInvalid) as caught:
        parse_request({"sys_type": "hybrid", "size": "5", "tier": "value", "battery_override": value})
    assert "battery_override" in caught.value.errors


def test_public_quote_ignores_the_override(monkeypatch):
    seen = {}

    class Recorder(Calculator):  # keeps the parser's static helpers (`_size_to_kw`)
        def __init__(self, snapshot, params, today):
            seen.update(params)

        def compute(self):
            return {}

    monkeypatch.setattr(website_quote, "load_snapshot", lambda sys_type: None)
    monkeypatch.setattr(website_quote, "Calculator", Recorder)
    body = {"sys_type": "hybrid", "size": "5", "tier": "value", "battery_override": "2"}
    website_quote.quote(body, today=date(2026, 10, 30))
    assert seen["battery_override"] == ""
    website_quote.quote(body, today=date(2026, 10, 30), staff=True)
    assert seen["battery_override"] == "2"
