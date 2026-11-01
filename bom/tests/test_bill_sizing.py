"""``POST /api/v1/bom/bill-sizing/`` — a bill becomes a system size.

Driven by the tariff in ``pricing_cost_config["energy.config"]``, which is the live one
and is updatable by an admin when KSEB issues a new order.

**Why this is not pinned to Golden-Ray-BOM's numbers.** The tool hardcodes the 2025-27
slabs (50 units at ₹3.25, 100 at ₹4.05, …, non-telescopic above 250); the platform's
config carries a different order (100 at ₹3.35, 200 at ₹4.25, …, threshold 500). The two
disagree, most visibly at low bills — a ₹3,000 bi-monthly bill is 1.3 kW on the tool's
table and 2.0 kW on the platform's, which is a different recommended size.

Per the decision recorded in the plan, the platform's config wins: a tariff that lives in
configuration can be corrected without a release, and a hardcoded one silently goes stale.
So these tests assert the *behaviour* — the monthly/bi-monthly handling, the sizing rule,
the phase warning, the savings cap — against the real fixture, rather than freezing
figures from a tariff that may already be superseded.
"""

from decimal import Decimal

import pytest

from bom.services import bill_sizing
from pricing.tests.legacy_fixtures import engine_configs

pytestmark = pytest.mark.django_db
URL = "/api/v1/bom/bill-sizing/"


@pytest.fixture(scope="module")
def kerala() -> dict:
    """The committed Flarize energy configuration — the same one the engines read."""
    return engine_configs()["energy"]

ONGRID_SIZES = [
    {"key": "3", "label": "3 kW"},
    {"key": "5sp", "label": "5 kW 1P"},
    {"key": "5tp", "label": "5 kW 3P"},
    {"key": "6", "label": "6 kW"},
    {"key": "8", "label": "8 kW"},
    {"key": "10", "label": "10 kW"},
]
THREE_PHASE = ["5tp", "6", "8", "10"]


def _recommend(config, bill, frequency="bimonthly", house_phase="1P"):
    return bill_sizing.recommend(
        bill=Decimal(str(bill)),
        frequency=frequency,
        house_phase=house_phase,
        sizes=bill_sizing.size_options(ONGRID_SIZES, THREE_PHASE),
        config=config,
    )


class TestBillPeriod:
    def test_a_monthly_bill_is_doubled_before_it_reaches_the_tariff(self, kerala):
        """KSEB bills bi-monthly, so a monthly figure covers half the period.

        Reading one as the other halves or doubles the recommendation — the single most
        consequential mistake this endpoint could make, and the reason the period is a
        required choice rather than something inferred from the amount.
        """
        monthly = _recommend(kerala, 3000, frequency="monthly")
        bimonthly = _recommend(kerala, 3000, frequency="bimonthly")
        assert monthly["bi_monthly_bill"] == 6000.0 and bimonthly["bi_monthly_bill"] == 3000.0
        # The same figure read monthly describes a house using roughly twice as much.
        assert monthly["monthly_units"] > bimonthly["monthly_units"]
        assert monthly["exact_kw"] > bimonthly["exact_kw"]

    def test_a_bigger_bill_never_suggests_a_smaller_system(self, kerala):
        """Monotonic, which a slab table with a non-telescopic jump could easily break."""
        sizes = [_recommend(kerala, bill)["exact_kw"] for bill in (2000, 5000, 10000, 20000, 40000)]
        assert sizes == sorted(sizes), sizes


class TestSizing:
    def test_the_smallest_size_that_covers_enough_wins(self, kerala):
        """Smallest-that-fits, not nearest: exported units earn less than bought ones.

        At the 85 % coverage target a system is accepted once it covers most of the bill,
        so the chosen size sits at or below the exact requirement rather than above it.
        """
        result = _recommend(kerala, 8000)
        assert result["recommended_kw"] is not None
        assert result["recommended_kw"] >= result["exact_kw"] * 0.85

    def test_a_single_phase_house_is_warned_before_it_outgrows_its_supply(self, kerala):
        """Above 5 kW the customer must apply to KSEB — better said now than at install."""
        assert _recommend(kerala, 40000, house_phase="1P")["phase_upgrade_needed"] is True
        assert _recommend(kerala, 3000, house_phase="1P")["phase_upgrade_needed"] is False

    def test_three_phase_is_not_offered_the_smallest_system(self, kerala):
        """A 3 kW system does not justify a three-phase connection (App.jsx:3005)."""
        assert _recommend(kerala, 5000, house_phase="3P")["recommended_size"] != "3"

    def test_savings_cannot_exceed_what_the_house_uses(self, kerala):
        """A system only offsets consumption; the surplus is exported, not deducted.

        Without the cap a large system on a small bill would claim to save more than the
        customer pays, which is the most embarrassing number a quote could carry.
        """
        result = _recommend(kerala, 2000)
        monthly_bill = result["bill_breakdown"]["total"] / 2
        assert result["monthly_savings"] <= round(monthly_bill) + 1

    def test_the_recommendation_is_one_of_the_offered_sizes(self, kerala):
        """Never a size the template cannot build."""
        offered = {size["key"] for size in ONGRID_SIZES}
        for bill in (2000, 6000, 15000, 40000):
            for phase in ("1P", "3P"):
                key = _recommend(kerala, bill, house_phase=phase)["recommended_size"]
                assert key is None or key in offered, (bill, phase, key)


class TestEndpoint:
    def test_permissions(self, api_client, outsider, viewer, world):
        body = {"bill": 5000}
        assert api_client.post(URL, body, format="json").status_code == 401
        assert outsider.post(URL, body, format="json").status_code == 403
        assert viewer.post(URL, body, format="json").status_code in (200, 503)

    def test_validation(self, viewer, world):
        assert viewer.post(URL, {"bill": 0}, format="json").status_code == 400
        assert viewer.post(URL, {"bill": 5000, "frequency": "weekly"}, format="json").status_code == 400
        assert viewer.post(URL, {"bill": 5000, "house_phase": "2P"}, format="json").status_code == 400

    def test_an_unconfigured_tariff_is_a_503_not_a_wrong_answer(self):
        """Without a tariff there is no honest conversion, so it refuses rather than guesses."""
        from core.errors import DomainError

        with pytest.raises(DomainError) as caught:
            bill_sizing.recommend(
                bill=Decimal("5000"), frequency="bimonthly", house_phase="1P",
                sizes=bill_sizing.size_options(ONGRID_SIZES, THREE_PHASE), config=None,
            )
        assert caught.value.code == "bill_sizing_not_configured"
