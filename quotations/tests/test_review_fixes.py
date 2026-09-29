"""Adversarial review of the quotations package: each test reproduced a defect before its fix."""

from __future__ import annotations

from decimal import Decimal

import pytest

from quotations.models import Quotation, QuotationStatus, Version, VersionStatus
from quotations.tests.conftest import quotation_data
from quotations.tests.factories import QuotationFactory, VersionFactory, issued_quotation

pytestmark = pytest.mark.django_db
BASE = "/api/v1/quotations/"


def _create(client, customer, **overrides):
    response = client.post(BASE, quotation_data(customer, **overrides), format="json")
    assert response.status_code == 201, response.json()
    return response.json()


def _legacy_draft(owner, customer):
    """An imported Flarize DRAFT (never issued): legacy, no PackRelease, the import's selections."""
    quotation = QuotationFactory(owner=owner, customer=customer, legacy=True, legacy_ref="QT-legacy-draft")
    version = VersionFactory(
        quotation=quotation,
        vehicle_type="ACE",
        distance_km=Decimal("60"),
        subsidy_type="residential",
        selections={"appliance_rows": None, "legacy_quotation_id": "QT-legacy-draft"},
    )
    Quotation.objects.filter(pk=quotation.pk).update(current_version=version)
    return quotation


def test_a_legacy_draft_is_refused_cleanly_until_refreshed(world, head, head_user, customer):
    """An imported DRAFT has no PackRelease: preview/PATCH/issue crashed with a 500 (``None.price_release``)."""
    quotation = _legacy_draft(head_user, customer)
    url = f"{BASE}{quotation.uid}/versions/1/"
    for response in (head.post(f"{url}preview/", {}, format="json"), head.patch(url, {"tier": "BASE"}, format="json"), head.post(f"{url}issue/", {}, format="json")):
        assert response.status_code == 409, response.content
        assert response.json()["code"] == "release_required"
    refreshed = head.patch(url, {"refresh_release": True}, format="json")
    assert refreshed.status_code == 200, refreshed.json()
    assert refreshed.json()["pack_release"] == 1 and "legacy_quotation_id" not in refreshed.json()["selections"]
    assert head.post(f"{url}preview/", {}, format="json").status_code == 200


def test_revising_an_imported_quotation(world, head, head_user, customer):
    """Revising an imported ISSUED quotation failed validation on the import's own ``legacy_quotation_id`` selection."""
    quotation = issued_quotation(owner=head_user, customer=customer)
    Version.objects.filter(pk=quotation.current_version_id).update(vehicle_type="ACE", distance_km=Decimal("60"), selections={"appliance_rows": None, "legacy_quotation_id": "QT-x"})
    revised = head.post(f"{BASE}{quotation.uid}/revise/", {}, format="json")
    assert revised.status_code == 201, revised.json()
    assert revised.json()["pack_release"] == 1 and revised.json()["selections"] == {}


def test_editing_a_draft_keeps_an_approved_validity_override(world, head, executive, executive_user, customer):
    """A Sales Head's validity override made every later edit/revise of the draft by the Sales Executive a 403."""
    quotation = _create(executive, customer)
    url = f"{BASE}{quotation['uid']}/versions/1/"
    Version.objects.filter(uid=quotation["current_version"]["uid"]).update(selections={"validity_override_days": 30})
    edited = executive.patch(url, {"language": "ml"}, format="json")
    assert edited.status_code == 200, edited.json()
    assert edited.json()["selections"]["validity_override_days"] == 30
    changed = executive.patch(url, {"selections": {"validity_override_days": 60}}, format="json")
    assert changed.status_code == 403 and changed.json()["code"] == "validity_override_denied"
    # dropping the override needs no approval
    dropped = executive.patch(url, {"selections": {}}, format="json")
    assert dropped.status_code == 200 and "validity_override_days" not in dropped.json()["selections"]


def test_a_cancelled_quotation_is_closed_for_edits_and_discounts(world, executive, head, customer):
    """Cancelling a DRAFT quotation left its draft editable and open to discount requests and decisions."""
    quotation = _create(executive, customer)
    url = f"{BASE}{quotation['uid']}/"
    pending = executive.post(f"{url}discount-requests/", {"amount": "100", "reason": "neighbour"}, format="json").json()
    assert executive.post(f"{url}cancel/", {"reason": "lost"}, format="json").status_code == 200
    for response in (
        executive.patch(f"{url}versions/1/", {"tier": "BASE"}, format="json"),
        executive.post(f"{url}discount-requests/", {"amount": "100", "reason": "again"}, format="json"),
        head.post(f"{url}discount-requests/{pending['uid']}/approve/", {}, format="json"),
    ):
        assert response.status_code == 409, response.json()
        assert response.json()["code"] == "quotation_closed"
    assert Version.objects.get(uid=quotation["current_version"]["uid"]).discount_total == Decimal("0")
    assert Quotation.objects.get(uid=quotation["uid"]).status == QuotationStatus.CANCELLED
    assert Version.objects.get(uid=quotation["current_version"]["uid"]).status == VersionStatus.DRAFT


def test_commercial_snapshots_withhold_margin_and_cost_without_pricing_internal(executive, executive_user, head, head_user):
    """Readers without ``pricing_internal.view`` got the gross profit / target margin of an imported (gross-margin)
    snapshot and the pack domain's structure costing (tube ₹/kg rates) — Flarize withholds both from Sales."""
    from quotations.models import CommercialSnapshot

    quotation = issued_quotation(owner=executive_user)
    record = {
        "cost": {"totalActualProjectCost": 150000},
        "pricing": {"marginType": "GROSS_MARGIN", "targetGrossMargin": 0.2, "grossProfit": 34483, "sellingPriceBeforeGST": 184483, "customerTotalIncludingGST": 229000},
        "pack": {"structureMaterial": {"total": 9830, "lines": [{"name": "GI tube", "unitPrice": 983, "amount": 9830}]}, "marketRate": 220000},
        "offer": None,
    }
    CommercialSnapshot.objects.create(
        quotation_version=quotation.current_version,
        tier="VALUE",
        is_primary=True,
        cost_lines={name: record[name] for name in ("cost", "pricing", "pack", "offer")},
        pins={"marginVersion": "m1", "gstVersion": "g1"},
        margin_check={"landedCostCheck": {"status": "NOT_RUN"}},
        record=record,
    )
    url = f"{BASE}{quotation.uid}/versions/1/"
    snapshot = executive.get(url).json()["commercial_snapshots"][0]
    assert set(snapshot["cost_lines"]) == {"pricing", "offer"}
    assert {"grossProfit", "targetGrossMargin", "marginType"}.isdisjoint(snapshot["cost_lines"]["pricing"])
    assert snapshot["cost_lines"]["pricing"]["customerTotalIncludingGST"] == 229000
    assert snapshot["margin_check"] is None and snapshot["pins"] == {"gstVersion": "g1"}
    Quotation.objects.filter(pk=quotation.pk).update(owner=head_user)
    internal = head.get(url).json()["commercial_snapshots"][0]
    assert internal["cost_lines"]["pricing"]["grossProfit"] == 34483 and internal["cost_lines"]["pack"]["structureMaterial"]["total"] == 9830


def test_email_log_rows_carry_no_integer_id(executive, executive_user):
    from quotations.models import EmailLog

    quotation = issued_quotation(owner=executive_user)
    EmailLog.objects.create(version=quotation.current_version, to="a@example.com")
    logs = executive.get(f"{BASE}{quotation.uid}/versions/1/").json()["email_logs"]
    assert len(logs) == 1 and "id" not in logs[0]
