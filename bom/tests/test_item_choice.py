"""Per-slot part selection: the pool a slot offers, and honouring a choice from it.

The source lets a sales person swap the brand on a line (``available`` /
``allAvailable``, App.jsx:1912). The platform computed the same pool and threw it
away, so the Studio had nothing to populate a dropdown from and no way to say
which part it wanted.

What is pinned here is mostly about what the feature must NOT do: offer a part the
engine's own gates rejected, re-price the public website, or ship the price list to
the warehouse inside the picking list.
"""

from datetime import date
from decimal import Decimal

import pytest

from bom.services.website_quote import dispatch_body, quote
from catalog.models import ComponentStatus
from catalog.tests.factories import CategoryFactory, ComponentFactory, ComponentTierFactory
from pricing.services.cost_config import put_values
from pricing.tests.factories import PriceFactory

pytestmark = pytest.mark.django_db
TODAY = date(2026, 6, 1)


def _body(**over):
    return {"sys_type": "ongrid", "size": "3", "tier": "value", **over}


def _composite():
    put_values(user=None, entries=[{"key": "gst.quote_regime", "value": {"mode": "COMPOSITE", "rate_pct": 9}}])


@pytest.fixture
def two_panels(world):
    """A second panel in the same tier, so the panel slot is a real choice.

    Both panels need a DCR ``PanelSpec``: a quote defaults to ``module_dcr="dcr"``, and
    the content gate reads ``dcr is not True`` as non-DCR -- so a panel whose content was
    never recorded is filtered out and the slot has nothing to choose between. That is
    the engine behaving correctly; it just makes the fixture's intent explicit.
    """
    from catalog.models import Category
    from catalog.tests.factories import PanelSpecFactory

    panels = Category.objects.get(slug="panel")
    PanelSpecFactory(component=panels.components.get(name="Panel 540"), wattage_w=540, is_dcr=True)
    other = ComponentFactory(category=panels, name="Panel 620", status=ComponentStatus.ACTIVE)
    for tier in ("BASE", "VALUE", "PREMIUM"):
        ComponentTierFactory(component=other, tier=tier)
    PriceFactory(component=other, amount=Decimal("15900.00"))
    PanelSpecFactory(component=other, wattage_w=620, is_dcr=True)
    return other


def _panel_line(result):
    return next(line for line in result["bom_lines"] if line["section"] == "panel")


class TestOptionsAreOffered:
    def test_per_line_sends_no_options_at_all(self, two_panels):
        """The public website's response shape is unchanged: every added behaviour is gated."""
        line = _panel_line(quote(_body(), today=TODAY))
        assert "options" not in line and "component" not in line

    def test_composite_offers_the_pool_and_names_the_choice(self, two_panels):
        _composite()
        line = _panel_line(quote(_body(), today=TODAY))
        assert len(line["options"]) == 2
        assert line["component"] in {option["component"] for option in line["options"]}
        assert all({"component", "name", "brand", "unit_price"} <= set(option) for option in line["options"])

    def test_a_slot_with_one_candidate_offers_nothing(self, world):
        """A dropdown of one is worse than no dropdown: it implies a choice that is not there."""
        _composite()
        line = _panel_line(quote(_body(), today=TODAY))
        assert "options" not in line


class TestChoosing:
    def test_a_requested_part_is_quoted(self, two_panels):
        _composite()
        line = _panel_line(quote(_body(), today=TODAY))
        other = next(o for o in line["options"] if o["component"] != line["component"])
        after = _panel_line(quote(_body(item_overrides=[{"pos": line["pos"], "component": other["component"]}]), today=TODAY))
        assert after["component"] == other["component"]
        assert after["name"] == other["name"]
        assert after["unit_price"] == other["unit_price"]

    def test_a_mapping_is_accepted_as_well_as_a_list(self, two_panels):
        _composite()
        line = _panel_line(quote(_body(), today=TODAY))
        other = next(o for o in line["options"] if o["component"] != line["component"])
        after = _panel_line(quote(_body(item_overrides={str(line["pos"]): other["component"]}), today=TODAY))
        assert after["component"] == other["component"]

    def test_an_unoffered_part_is_refused_and_recorded(self, two_panels):
        """The gates stay gates. An override naming a part they rejected would bring it
        back by the back door, so the engine's own choice stands and says why."""
        _composite()
        line = _panel_line(quote(_body(), today=TODAY))
        result = quote(_body(item_overrides=[{"pos": line["pos"], "component": "NOT-A-SKU"}]), today=TODAY)
        assert _panel_line(result)["component"] == line["component"]
        warnings = [w for w in result["resolution_warnings"] if w["resolution"] == "REQUESTED_UNAVAILABLE"]
        assert len(warnings) == 1 and warnings[0]["category"] == "panel"

    def test_a_stale_slot_position_is_ignored_not_fatal(self, two_panels):
        """Someone changes the size while a choice is in flight. Refusing the whole quote
        over a slot that no longer exists would make the screen unusable."""
        _composite()
        result = quote(_body(item_overrides=[{"pos": 999, "component": "X"}]), today=TODAY)
        assert result["bom_lines"]

    def test_an_override_is_ignored_under_per_line(self, two_panels):
        """PER_LINE is the public website. A caller must not be able to re-price it by
        sending a field the regime does not implement."""
        line = _panel_line(quote(_body(), today=TODAY))
        after = _panel_line(quote(_body(item_overrides=[{"pos": line["pos"], "component": "anything"}]), today=TODAY))
        assert after["name"] == line["name"] and after["unit_price"] == line["unit_price"]

    def test_choosing_a_costlier_part_moves_cost_but_not_a_rate_led_price(self, two_panels):
        """Where a market rate exists it *is* the price, so a swap moves margin rather
        than what the customer pays. Showing it as a price change would be wrong."""
        _composite()
        first = quote(_body(), today=TODAY)
        line = _panel_line(first)
        dearer = max(line["options"], key=lambda o: o["unit_price"])
        second = quote(_body(item_overrides=[{"pos": line["pos"], "component": dearer["component"]}]), today=TODAY)
        assert second["pricing"]["market_rate"] > 0
        assert second["pricing"]["customer_price"] == first["pricing"]["customer_price"]
        assert second["totals"]["material_ex_gst"] != first["totals"]["material_ex_gst"]


    def test_choosing_a_different_wattage_re_derives_the_panel_count(self, two_panels):
        """The count comes from the chosen module, not the template's figure. Swapping a
        540 W panel for a 620 W one is 5 panels, not 6 -- and anything priced per panel
        follows, which is where this gets expensive."""
        _composite()
        line = _panel_line(quote(_body(), today=TODAY))
        other = next(o for o in line["options"] if o["component"] != line["component"])
        after = _panel_line(quote(_body(item_overrides=[{"pos": line["pos"], "component": other["component"]}]), today=TODAY))
        # 3 kW: ceil(3000/540) = 6, ceil(3000/620) = 5.
        assert {line["qty"], after["qty"]} == {6, 5}


class TestDispatchDoesNotLeakPrices:
    def test_option_prices_are_stripped_from_the_picking_list(self, two_panels):
        """The whole point of the dispatch body. Stripping only the line's own four money
        keys would have shipped the price list inside the response that withholds it."""
        _composite()
        body = dispatch_body(quote(_body(mode="dispatch"), today=TODAY))
        options = [option for line in body["bom_lines"] for option in (line.get("options") or [])]
        assert options, "the fixture should offer a choice to strip"
        assert all("unit_price" not in option for option in options)
        assert all("unit_price" not in line for line in body["bom_lines"])

    def test_but_the_alternatives_are_still_named(self, two_panels):
        """An installer reading a substitution off the sheet is the point of the list."""
        _composite()
        body = dispatch_body(quote(_body(mode="dispatch"), today=TODAY))
        options = [option for line in body["bom_lines"] for option in (line.get("options") or [])]
        assert all(option["name"] for option in options)


class TestRequestValidation:
    def test_a_malformed_item_overrides_is_a_field_error(self, world):
        from bom.services.website_quote import QuoteInvalid

        with pytest.raises(QuoteInvalid) as caught:
            quote(_body(item_overrides="nonsense"), today=TODAY)
        assert "item_overrides" in caught.value.errors
