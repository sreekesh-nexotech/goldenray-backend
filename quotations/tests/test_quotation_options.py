"""``GET quotations/component-options/`` (panel and other Sales choices) and the history filters of ``GET quotations/``."""

from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from quotations.tests.conftest import quotation_data

pytestmark = pytest.mark.django_db
BASE = "/api/v1/quotations/"
OPTIONS = f"{BASE}component-options/"
CONFIG = {"system_type": "ONGRID", "tier": "VALUE", "size_key": "3"}


def _create(client, customer, **overrides):
    response = client.post(BASE, quotation_data(customer, **overrides), format="json")
    assert response.status_code == 201, response.json()
    return response.json()


# ── component options ──────────────────────────────────────────────────────────────────────────────────────────────


def test_component_options_lists_the_sales_choices_of_the_pack(world, executive):
    response = executive.get(OPTIONS, CONFIG)
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["pack_release"] == 1 and (body["system_type"], body["tier"], body["size_key"], body["phase"], body["battery_config"]) == ("ONGRID", "VALUE", "3", "1P", "")
    assert body["slots"], "the released on-grid pack offers Sales at least one swappable slot"
    for slot in body["slots"]:
        ids = [option["id"] for option in slot["options"]]
        assert slot["default_id"] in ids and sum(option["is_default"] for option in slot["options"]) <= 1


def test_a_listed_choice_is_accepted_by_a_quotation(world, executive, customer):
    slot = executive.get(OPTIONS, CONFIG).json()["slots"][0]
    choice = next((o["id"] for o in slot["options"] if not o["is_default"]), slot["default_id"])
    body = _create(executive, customer, selections={"tier_selections": {"value": {slot["category"]: choice}}})
    issued = executive.post(f"{BASE}{body['uid']}/versions/1/issue/", {}, format="json")
    assert issued.status_code == 200, issued.json()


@pytest.mark.parametrize(
    ("params", "status", "code"),
    [
        ({**CONFIG, "phase": "3P"}, 409, "no_approved_package"),
        ({**CONFIG, "tier": "GOLD"}, 400, "validation_error"),
        ({**CONFIG, "size_key": "99"}, 400, "validation_error"),
        ({"system_type": "ONGRID"}, 400, "validation_error"),
    ],
)
def test_component_options_errors(world, executive, params, status, code):
    response = executive.get(OPTIONS, params)
    assert response.status_code == status and response.json()["code"] == code


def test_component_options_needs_quotations_view(world, api_client, outsider):
    assert api_client.get(OPTIONS, CONFIG).status_code == 401
    assert outsider.get(OPTIONS, CONFIG).status_code == 403


# ── history filters ────────────────────────────────────────────────────────────────────────────────────────────────


def test_history_filters_by_system_tier_date_and_phone(world, executive, customer):
    quotation = _create(executive, customer)

    def uids(**params):
        return [row["uid"] for row in executive.get(BASE, params).json()["results"]]

    assert uids(system_type="ONGRID") == [quotation["uid"]] and uids(system_type="HYBRID") == []
    assert uids(tier="VALUE") == [quotation["uid"]] and uids(tier="BASE") == []
    today = timezone.localdate()
    assert uids(created_from=today.isoformat(), created_to=today.isoformat()) == [quotation["uid"]]
    assert uids(created_from=(today + dt.timedelta(days=1)).isoformat()) == []
    assert uids(search=customer.phone_e164[-6:]) == [quotation["uid"]]
    row = executive.get(BASE).json()["results"][0]
    assert row["current_version"]["battery_config"] == "" and row["current_version"]["size_key"] == "3"
