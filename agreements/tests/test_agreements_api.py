"""Staff API of ``agreements/``: auth, permissions, record scope, validation envelope, stale versions, happy paths."""

from __future__ import annotations

from decimal import Decimal

import pytest

from agreements.tests.factories import AgreementFactory, issued_version
from core.models import OutboxEvent

BASE = "/api/v1/agreements/"


def _from_quotation(client, version, **extra):
    return client.post(f"{BASE}from-quotation/", {"quotation_version_uid": str(version.uid), **extra}, format="json")


# ── authentication, permission, scope ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_anonymous_is_401(api_client):
    assert api_client.get(BASE).status_code == 401
    assert api_client.post(f"{BASE}from-quotation/", {}, format="json").status_code == 401


def test_missing_permission_is_403(outsider, version):
    assert outsider.get(BASE).status_code == 403
    assert _from_quotation(outsider, version).status_code == 403


def test_executive_cannot_issue_or_supersede(executive, executive_user, world):
    agreement = AgreementFactory(owner=executive_user)
    assert executive.post(f"{BASE}{agreement.uid}/issue/", {}, format="json").status_code == 403
    assert executive.post(f"{BASE}{agreement.uid}/supersede/", {}, format="json").status_code == 403


def test_owned_scope_lists_own_agreements_only(executive, executive_user, head, world):
    mine = AgreementFactory(owner=executive_user)
    other = AgreementFactory()
    listed = executive.get(BASE).json()["results"]
    assert [row["uid"] for row in listed] == [str(mine.uid)]
    assert executive.get(f"{BASE}{other.uid}/").status_code == 404
    assert {row["uid"] for row in head.get(BASE).json()["results"]} == {str(mine.uid), str(other.uid)}


def test_list_filters_and_query_budget(head, world, django_assert_max_num_queries):
    for _ in range(6):
        AgreementFactory()
    version = issued_version()
    AgreementFactory(kind="PURCHASE_AGREEMENT", quotation_version=version, customer=version.quotation.customer)
    with django_assert_max_num_queries(12):
        response = head.get(BASE)
    assert response.status_code == 200 and response.json()["count"] == 7
    assert head.get(BASE, {"kind": "PURCHASE_AGREEMENT"}).json()["count"] == 1
    assert head.get(BASE, {"quotation": str(version.quotation.uid)}).json()["count"] == 1
    assert head.get(BASE, {"status": "ISSUED"}).json()["count"] == 0


# ── from quotation ──────────────────────────────────────────────────────────────────────────────────────────────────


def test_from_quotation_pins_the_issued_version(head, version, world):
    response = _from_quotation(head, version)
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["status"] == "DRAFT" and body["kind"] == "PURCHASE_AGREEMENT" and body["number"] == ""
    assert body["quotation"]["version_uid"] == str(version.uid)
    assert body["system_type"] == "ON_GRID" and body["capacity_kw"] == "3.00" and body["phase"] == "1P" and body["variant"] == "VALUE"
    assert body["panel"]["uid"] == str(world["panel"].uid) and body["panel_capacity_w"] == 550 and body["panel_dcr"] is True and body["panel_qty"] == 6
    assert body["inverter"]["uid"] == str(world["inverter"].uid) and body["inverter_type"] == "STRING" and body["inverter_brand"] == "Sungrow"
    assert body["structure_template_uid"] == str(world["structure"].uid) and body["structure_material"] == "2.5×1.5 Square Tube 16 Gauge GP"
    assert (body["original_price"], body["discount"], body["final_price"]) == ("229000.00", "7000.00", "222000.00")
    assert body["statutory_fee_amount"] == "5400.00" and body["statutory_fee_label"] == "3 KW"
    assert OutboxEvent.objects.filter(event_type="agreements.created").count() == 1


def test_from_quotation_validation_envelope(head, world):
    response = head.post(f"{BASE}from-quotation/", {"quotation_version_uid": "nope", "kind": "EXTRA_STRUCTURE"}, format="json")
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "validation_error" and {"quotation_version_uid", "kind"} <= set(body["errors"])


def test_from_quotation_outside_the_quotation_scope(executive, version):
    response = _from_quotation(executive, version)
    assert response.status_code == 400 and response.json()["errors"]["quotation_version_uid"]


def test_from_quotation_twice_is_a_conflict(head, version):
    assert _from_quotation(head, version).status_code == 201
    response = _from_quotation(head, version)
    assert response.status_code == 409 and response.json()["code"] == "agreement_exists"
    assert _from_quotation(head, version, kind="SALE_ORDER").status_code == 201


# ── draft edit ──────────────────────────────────────────────────────────────────────────────────────────────────────


def test_patch_quotation_draft_keeps_pinned_values(head, version):
    agreement = _from_quotation(head, version).json()
    response = head.patch(f"{BASE}{agreement['uid']}/", {"final_price": "1", "original_price": "1", "panel_uid": agreement["panel"]["uid"]}, format="json")
    assert response.status_code == 400 and response.json()["code"] == "field_pinned"
    assert set(response.json()["errors"]) == {"original_price", "panel_uid"}
    response = head.patch(
        f"{BASE}{agreement['uid']}/", {"walkway_required": True, "language": "ml", "consumer_number": "1155", "registered_phone": "9847012345", "expected_version": agreement["version"]}, format="json"
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["walkway_required"] is True and body["language"] == "ml" and body["registered_phone"] == "+919847012345" and body["final_price"] == "222000.00"


def test_patch_stale_version(head, version):
    agreement = _from_quotation(head, version).json()
    response = head.patch(f"{BASE}{agreement['uid']}/", {"ladder_required": True, "expected_version": agreement["version"] + 5}, format="json")
    assert response.status_code == 409 and response.json()["code"] == "stale_version"


def test_patch_invalid_phone_and_issued_agreement(head, version, company, document_storage):
    agreement = _from_quotation(head, version).json()
    response = head.patch(f"{BASE}{agreement['uid']}/", {"registered_phone": "12"}, format="json")
    assert response.status_code == 400 and "registered_phone" in response.json()["errors"]
    assert head.post(f"{BASE}{agreement['uid']}/issue/", {}, format="json").status_code == 200
    response = head.patch(f"{BASE}{agreement['uid']}/", {"ladder_required": True}, format="json")
    assert response.status_code == 409 and response.json()["code"] == "agreement_not_draft"


# ── blank agreements ────────────────────────────────────────────────────────────────────────────────────────────────


def test_blank_sale_order(executive, executive_user, world):
    customer = issued_version().quotation.customer
    data = {
        "kind": "SALE_ORDER",
        "customer_uid": str(customer.uid),
        "capacity_kw": "5",
        "phase": "1P",
        "variant": "PREMIUM",
        "panel_uid": str(world["panel"].uid),
        "inverter_uid": str(world["inverter"].uid),
        "battery_uid": str(world["battery"].uid),
        "original_price": "335000",
        "extra_cost": "19500",
        "extra_description": "Raised GI structure",
    }
    response = executive.post(BASE, data, format="json")
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["owner"]["uid"] == str(executive_user.uid) and body["quotation"] is None
    assert body["size_label"] == "5 KW" and body["panel_label"].startswith("Brand") and body["battery_label"] == "5 kWh"
    assert body["final_price"] == "354500.00" and body["statutory_fee_amount"] == "7800.00"
    response = executive.patch(f"{BASE}{body['uid']}/", {"capacity_kw": "7", "original_price": "400000"}, format="json")
    assert response.status_code == 200
    assert response.json()["statutory_fee_amount"] == "11240.00" and response.json()["final_price"] == "419500.00"


def test_blank_extra_structure_from_a_site_inspection(head, world):
    base = AgreementFactory(kind="PURCHASE_AGREEMENT", panel=world["panel"], inverter=world["inverter"], final_price=Decimal("300000.00"))
    data = {
        "kind": "EXTRA_STRUCTURE",
        "customer_uid": str(base.customer.uid),
        "source_type": "SITE_INSPECTION",
        "source_uid": "8b1a3c55-2a4b-4b8e-9d6f-0a1b2c3d4e5f",
        "base_agreement_uid": str(base.uid),
        "lines": [
            {"description": "Walkway", "quantity": "4", "unit": "m", "unit_price": "1500", "additional_work_item_uid": "1b1a3c55-2a4b-4b8e-9d6f-0a1b2c3d4e5f"},
            {"description": "Elevated structure", "quantity": "1", "unit_price": "12500.50"},
        ],
    }
    response = head.post(BASE, data, format="json")
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["source_type"] == "SITE_INSPECTION" and body["original_price"] == "300000.00" and body["panel"]["uid"] == str(world["panel"].uid)
    assert body["extra_cost"] == "18500.50" and body["final_price"] == "318500.50" and len(body["lines"]) == 2
    assert head.get(BASE, {"source_uid": data["source_uid"]}).json()["count"] == 1
    response = head.patch(f"{BASE}{body['uid']}/", {"extra_cost": "10"}, format="json")
    assert response.status_code == 400 and "extra_cost" in response.json()["errors"]
    response = head.patch(f"{BASE}{body['uid']}/", {"lines": [{"description": "Ladder", "quantity": "1", "unit_price": "9000"}]}, format="json")
    assert response.json()["extra_cost"] == "9000.00" and response.json()["final_price"] == "309000.00"


@pytest.mark.parametrize(
    "overrides,field",
    [
        ({"kind": "PURCHASE_AGREEMENT"}, "kind"),
        ({"kind": "EXTRA_STRUCTURE"}, "lines"),
        ({"source_type": "SITE_INSPECTION"}, "source_uid"),
        ({"panel_uid": "8b1a3c55-2a4b-4b8e-9d6f-0a1b2c3d4e5f"}, "panel_uid"),
        ({"lines": [{"description": "x", "quantity": "1", "unit_price": "1"}]}, "lines"),
        ({"customer_uid": "8b1a3c55-2a4b-4b8e-9d6f-0a1b2c3d4e5f"}, "customer_uid"),
    ],
)
def test_blank_validation(head, world, overrides, field):
    customer = issued_version().quotation.customer
    data = {"kind": "SALE_ORDER", "customer_uid": str(customer.uid), **overrides}
    response = head.post(BASE, data, format="json")
    assert response.status_code == 400 and field in response.json()["errors"], response.json()


def test_blank_draft_equipment_edits_and_revision_copies_lines(head, world, company, document_storage):
    agreement = AgreementFactory(kind="EXTRA_STRUCTURE", panel=world["panel"], extra_cost=Decimal("0"))
    path = f"{BASE}{agreement.uid}/"
    response = head.patch(path, {"structure_template_uid": "8b1a3c55-2a4b-4b8e-9d6f-0a1b2c3d4e5f"}, format="json")
    assert response.status_code == 400 and "structure_template_uid" in response.json()["errors"]
    response = head.patch(path, {"inverter_uid": str(world["panel"].uid)}, format="json")
    assert response.status_code == 400 and "inverter_uid" in response.json()["errors"]
    response = head.patch(path, {"lines": []}, format="json")
    assert response.status_code == 400 and "lines" in response.json()["errors"]
    lines = [{"description": "Walkway", "quantity": "2.5", "unit": "m", "unit_price": "1000"}]
    body = {"structure_template_uid": str(world["structure"].uid), "panel_uid": None, "panel_capacity_label": "", "lines": lines}
    response = head.patch(path, body, format="json")
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["structure_template_uid"] == str(world["structure"].uid) and body["panel"] is None and body["panel_label"] == "" and body["extra_cost"] == "2500.00"
    assert body["final_price"] == "302500.00"
    assert head.patch(path, {"panel_uid": str(world["panel"].uid)}, format="json").status_code == 200
    issued = head.post(f"{path}issue/", {}, format="json")
    assert issued.status_code == 200, issued.json()
    assert issued.json()["payload"]["prices"]["lines"][0]["amount"] == "2500.00"
    revision = head.post(f"{path}supersede/", {}, format="json").json()
    assert [line["description"] for line in revision["lines"]] == ["Walkway"] and revision["extra_cost"] == "2500.00"
    response = head.post(f"{path}supersede/", {"quotation_version_uid": "8b1a3c55-2a4b-4b8e-9d6f-0a1b2c3d4e5f"}, format="json")
    assert response.status_code == 409
