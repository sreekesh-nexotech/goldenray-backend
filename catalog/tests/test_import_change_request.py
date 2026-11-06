"""The component CSV import through a change request (DV-155): validated as a dry run on submit, imported on approval."""

import pytest

from approvals.models import ChangeRequest
from catalog.models import Component
from catalog.tests.factories import panel, panel_category

pytestmark = pytest.mark.django_db
CHANGES = "/api/v1/change-requests/"
REQUESTER = {"catalog": ["view"], "change_requests": ["view", "create"]}
APPROVER = {"catalog": "*", "change_requests": "*"}
CSV = "sku,category,name,spec.wattage_w\np1,panel,Renamed panel,545\np2,panel,New panel,550\n"


@pytest.fixture
def requester(auth_client, make_user):
    return auth_client(make_user(grants=REQUESTER, scopes={"change_requests": "owned"}))


@pytest.fixture
def approver(auth_client, make_user):
    return auth_client(make_user(grants=APPROVER, scopes={"change_requests": "all"}))


def ask(client, text=CSV):
    return client.post(CHANGES, {"kind": "catalog.component_import", "operation": "SET", "payload": {"csv": text}, "reason": "New range"}, format="json")


def test_import_happens_only_on_approval(requester, approver):
    panel_category()
    panel(sku="p1")
    response = ask(requester)
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["before"]["summary"] == {"rows": 2, "create": 1, "update": 1, "unchanged": 0, "error": 0}
    assert {row["sku"]: row["action"] for row in body["before"]["rows"]} == {"p1": "update", "p2": "create"}
    assert "2 row(s)" in body["target_label"] and not Component.objects.filter(sku="p2").exists()
    assert approver.post(f"{CHANGES}{body['uid']}/approve/", {}, format="json").status_code == 200
    assert Component.objects.get(sku="p1").name == "Renamed panel" and Component.objects.filter(sku="p2").exists()


def test_a_file_with_row_errors_is_refused_on_submit(requester):
    panel_category()
    response = ask(requester, "sku,category,name\nx1,no-such-category,Thing\n")
    assert response.status_code == 400 and response.json()["code"] == "import_has_errors"
    assert not ChangeRequest.objects.exists()


def test_the_requester_cannot_import_directly(requester):
    panel_category()
    from django.core.files.uploadedfile import SimpleUploadedFile

    upload = SimpleUploadedFile("c.csv", CSV.encode(), content_type="text/csv")
    assert requester.post("/api/v1/catalog/components/import/", {"file": upload}, format="multipart").status_code == 403


def test_empty_payload(requester):
    assert ask(requester, "").status_code == 400
