"""Issue, supersede, cancel, paper acceptance, documents, render and the flag-gated price override."""

from __future__ import annotations

import re

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from agreements.models import Agreement, AgreementStatus
from agreements.tests.factories import AgreementFactory
from audit.models import AuditLog
from core.models import OutboxEvent
from documents.models import RenderJob
from documents.services.jobs import run_job
from media.tests import files

BASE = "/api/v1/agreements/"


@pytest.fixture
def draft(head, version, company, document_storage):
    return head.post(f"{BASE}from-quotation/", {"quotation_version_uid": str(version.uid)}, format="json").json()


def _issue(client, uid, **body):
    return client.post(f"{BASE}{uid}/issue/", body, format="json")


def test_issue_freezes_numbers_renders_and_emits(head, head_user, draft, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        response = _issue(head, draft["uid"], expected_version=draft["version"])
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["status"] == "ISSUED" and re.fullmatch(r"AGR-\d{4}-\d{2}-0001", body["number"]) and body["issued_by"]["uid"] == str(head_user.uid)
    assert re.fullmatch(r"[0-9a-f]{64}", body["payload_sha256"])
    payload = body["payload"]
    assert payload["agreement"]["number"] == body["number"] and payload["prices"]["final_price"] == "222000.00"
    assert payload["company"]["payee"] == "M/s Golden Ray Renewable Energy LLP" and payload["company"]["bank"]["ifsc"] == "SBIN0001234"
    assert payload["system"]["plant_description"] == "3 KW (Single Phase) On-Grid Solar Power Plant"
    job = RenderJob.objects.get(object_uid=body["uid"])
    assert job.kind == "AGREEMENT" and job.language == "en" and job.payload_sha256 == body["payload_sha256"] and job.status == RenderJob.Status.DONE
    event = OutboxEvent.objects.get(event_type="agreements.issued")
    assert event.payload["agreement_uid"] == body["uid"] and event.payload["supersedes_uid"] is None
    assert not OutboxEvent.objects.filter(event_type="agreements.superseded").exists()
    assert AuditLog.objects.filter(action="agreements.issued", object_uid=body["uid"]).exists()
    response = head.get(f"{BASE}{body['uid']}/document/")
    assert response.status_code == 200 and response.json()["language"] == "en" and "/documents/download/" in response.json()["url"]


def test_issue_stale_version_and_twice(head, draft):
    assert _issue(head, draft["uid"], expected_version=draft["version"] + 1).json()["code"] == "stale_version"
    assert _issue(head, draft["uid"]).status_code == 200
    response = _issue(head, draft["uid"])
    assert response.status_code == 409 and response.json()["code"] == "agreement_not_draft"


def test_issue_incomplete_agreement(head, world, company, document_storage):
    agreement = AgreementFactory(kind="PURCHASE_AGREEMENT", panel_label="", structure_material="")
    agreement.customer.phone_e164 = ""
    agreement.customer.save()
    response = _issue(head, agreement.uid)
    assert response.status_code == 422 and response.json()["code"] == "agreement_incomplete"
    assert {"panel_label", "structure_material", "customer.phone_e164"} <= set(response.json()["errors"])
    assert Agreement.objects.get(pk=agreement.pk).status == AgreementStatus.DRAFT


def test_issue_refused_once_the_quotation_moved_on(head, draft, version):
    version.quotation.versions.filter(pk=version.pk).update(status="SUPERSEDED")
    response = _issue(head, draft["uid"])
    assert response.status_code == 409 and response.json()["code"] == "quotation_version_superseded"
    version.quotation.versions.filter(pk=version.pk).update(status="ISSUED")
    type(version.quotation).objects.filter(pk=version.quotation.pk).update(status="CANCELLED", cancelled_at=version.issued_at)
    response = _issue(head, draft["uid"])
    assert response.status_code == 409 and response.json()["code"] == "quotation_closed"


def test_supersede_then_issue_replaces_the_agreement(head, draft):
    issued = _issue(head, draft["uid"]).json()
    response = head.post(f"{BASE}{issued['uid']}/supersede/", {"language": "hi", "expected_version": issued["version"]}, format="json")
    assert response.status_code == 201, response.json()
    revision = response.json()
    assert revision["status"] == "DRAFT" and revision["revision"] == 2 and revision["supersedes"]["uid"] == issued["uid"] and revision["language"] == "hi"
    assert revision["final_price"] == issued["final_price"] and revision["panel"] == issued["panel"]
    again = head.post(f"{BASE}{issued['uid']}/supersede/", {}, format="json")
    assert again.status_code == 409 and again.json()["code"] == "already_superseded"
    response = _issue(head, revision["uid"])
    assert response.status_code == 200, response.json()
    assert head.get(f"{BASE}{issued['uid']}/").json()["status"] == "SUPERSEDED"
    event = OutboxEvent.objects.get(event_type="agreements.superseded")
    assert event.payload["agreement_uid"] == revision["uid"] and event.payload["supersedes_uid"] == issued["uid"] and event.payload["version"] == 2
    assert response.json()["number"] != issued["number"]


def test_supersede_needs_an_agreement_in_force(head, draft):
    response = head.post(f"{BASE}{draft['uid']}/supersede/", {}, format="json")
    assert response.status_code == 409 and response.json()["code"] == "agreement_not_in_force"


def test_issue_revision_of_a_cancelled_agreement(head, draft):
    issued = _issue(head, draft["uid"]).json()
    revision = head.post(f"{BASE}{issued['uid']}/supersede/", {}, format="json").json()
    assert head.post(f"{BASE}{issued['uid']}/cancel/", {"reason": "Customer withdrew"}, format="json").status_code == 200
    response = _issue(head, revision["uid"])
    assert response.status_code == 409 and response.json()["code"] == "supersedes_not_in_force"


def test_cancel(head, executive, executive_user, draft, world):
    mine = AgreementFactory(owner=executive_user)
    response = executive.post(f"{BASE}{mine.uid}/cancel/", {"reason": ""}, format="json")
    assert response.status_code == 400 and "reason" in response.json()["errors"]
    response = executive.post(f"{BASE}{mine.uid}/cancel/", {"reason": "Duplicate"}, format="json")
    assert response.status_code == 200 and response.json()["status"] == "CANCELLED"
    response = executive.post(f"{BASE}{mine.uid}/cancel/", {"reason": "Again"}, format="json")
    assert response.status_code == 409 and response.json()["code"] == "agreement_closed"
    issued = AgreementFactory(owner=executive_user)
    issued_uid = _issue(head, issued.uid).json()["uid"]
    response = executive.post(f"{BASE}{issued_uid}/cancel/", {"reason": "Lost"}, format="json")
    assert response.status_code == 403 and response.json()["code"] == "cancel_issued_forbidden"
    assert head.post(f"{BASE}{issued_uid}/cancel/", {"reason": "Lost"}, format="json").json()["status"] == "CANCELLED"
    assert OutboxEvent.objects.filter(event_type="agreements.cancelled").count() == 2


def test_record_acceptance_on_paper(head, draft):
    upload = SimpleUploadedFile("signed.pdf", files.pdf(), content_type="application/pdf")
    response = head.post(f"{BASE}{draft['uid']}/record-acceptance/", {"file": upload}, format="multipart")
    assert response.status_code == 409 and response.json()["code"] == "agreement_not_issued"
    issued = _issue(head, draft["uid"]).json()
    upload = SimpleUploadedFile("signed.pdf", files.pdf(), content_type="application/pdf")
    response = head.post(f"{BASE}{issued['uid']}/record-acceptance/", {"file": upload, "note": "Signed at office", "expected_version": issued["version"]}, format="multipart")
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["status"] == "ACCEPTED" and body["accepted_via"] == "PAPER" and body["acceptance_note"] == "Signed at office"
    assert body["acceptance_scan"]["url"] and "/media/download/" in body["acceptance_scan"]["url"]
    from media.models import MediaAsset

    asset = MediaAsset.objects.get(uid=body["acceptance_scan"]["uid"])
    assert asset.visibility == "PRIVATE" and asset.folder == "agreements/acceptance" and asset.kind == "DOCUMENT"
    assert OutboxEvent.objects.filter(event_type="agreements.accepted").exists()


def test_record_acceptance_rejects_bad_files_and_discards_on_conflict(head, draft):
    issued = _issue(head, draft["uid"]).json()
    upload = SimpleUploadedFile("signed.txt", b"hello", content_type="text/plain")
    response = head.post(f"{BASE}{issued['uid']}/record-acceptance/", {"file": upload}, format="multipart")
    assert response.status_code in (400, 415) and "file" in response.json()["errors"]
    from media.models import MediaAsset

    upload = SimpleUploadedFile("signed.jpg", files.jpeg(), content_type="image/jpeg")
    response = head.post(f"{BASE}{issued['uid']}/record-acceptance/", {"file": upload, "expected_version": issued["version"] + 3}, format="multipart")
    assert response.status_code == 409 and response.json()["code"] == "stale_version"
    assert not MediaAsset.objects.filter(folder="agreements/acceptance").exists()


def test_render_and_document_links(head, draft, django_capture_on_commit_callbacks):
    response = head.get(f"{BASE}{draft['uid']}/document/")
    assert response.status_code == 409 and response.json()["code"] == "document_not_ready"
    response = head.post(f"{BASE}{draft['uid']}/render/?language=ml", {}, format="json")
    assert response.status_code == 200 and response.json()["language"] == "ml"
    issued = _issue(head, draft["uid"]).json()
    with django_capture_on_commit_callbacks(execute=True):
        response = head.post(f"{BASE}{issued['uid']}/render/", {"language": "hi"}, format="json")
    job = response.json()
    assert job["payload_sha256"] == issued["payload_sha256"] and job["language"] == "hi"
    assert run_job(job["uid"]) is None  # already rendered on commit
    assert head.get(f"{BASE}{issued['uid']}/document/", {"language": "hi"}).status_code == 200
    again = head.post(f"{BASE}{issued['uid']}/render/", {"language": "hi"}, format="json").json()
    assert again["uid"] == job["uid"]
    assert head.post(f"{BASE}{issued['uid']}/render/", {"language": "fr"}, format="json").status_code == 400
    assert head.get(f"{BASE}{issued['uid']}/").json()["payload_sha256"] == issued["payload_sha256"]


def test_sale_order_from_a_quotation_prints_the_price_after_the_offer(head, version, company, document_storage):
    from django.template.loader import get_template

    created = head.post(f"{BASE}from-quotation/", {"quotation_version_uid": str(version.uid), "kind": "SALE_ORDER", "language": "ml"}, format="json").json()
    issued = _issue(head, created["uid"]).json()
    assert issued["payload"]["prices"]["net_price"] == "222000.00"
    for language in ("en", "ml", "hi"):
        html = get_template(f"documents/agreement/{language}.html").render({"payload": issued["payload"], "language": language})
        assert "₹ 2,22,000 (Value)" in html and "₹ 5,400" in html and "M/s Golden Ray Renewable Energy LLP" in html


def test_render_of_a_cancelled_draft(head, draft):
    head.post(f"{BASE}{draft['uid']}/cancel/", {"reason": "Wrong customer"}, format="json")
    response = head.post(f"{BASE}{draft['uid']}/render/", {"language": "en"}, format="json")
    assert response.status_code == 409 and response.json()["code"] == "agreement_not_issued"


# ── price override ──────────────────────────────────────────────────────────────────────────────────────────────────


def test_price_override_is_404_while_the_flag_is_off(head, draft):
    response = head.post(f"{BASE}{draft['uid']}/price-override/", {"discount": "10000", "reason": "Loyal customer"}, format="json")
    assert response.status_code == 404


def test_price_override(head, head_user, executive, executive_user, draft, price_override, api_client):
    path = f"{BASE}{draft['uid']}/price-override/"
    assert api_client.post(path, {}, format="json").status_code == 401
    assert executive.post(path, {"discount": "1", "reason": "x"}, format="json").status_code == 403
    response = head.post(path, {"discount": "10000"}, format="json")
    assert response.status_code == 400 and "reason" in response.json()["errors"]
    response = head.post(path, {"discount": "1", "final_price": "2", "reason": "x"}, format="json")
    assert response.status_code == 400 and response.json()["code"] == "validation_error"
    response = head.post(path, {"final_price": "300000", "reason": "x"}, format="json")
    assert response.status_code == 400
    response = head.post(path, {"discount": "10000", "reason": "Loyal customer", "expected_version": draft["version"] + 9}, format="json")
    assert response.json()["code"] == "stale_version"
    response = head.post(path, {"discount": "10000", "reason": "Loyal customer", "expected_version": draft["version"]}, format="json")
    assert response.status_code == 200, response.json()
    body = response.json()
    assert (body["discount"], body["final_price"], body["price_override_reason"]) == ("10000.00", "219000.00", "Loyal customer")
    response = head.post(path, {"final_price": "220000", "reason": "Rounded"}, format="json")
    assert response.json()["discount"] == "9000.00"
    log = AuditLog.objects.filter(action="agreements.price_overridden").order_by("-at").first()
    assert log.before["final_price"] == "219000.00" and log.after["price_override_reason"] == "Rounded"
    mine = AgreementFactory(owner=executive_user)
    assert head.post(f"{BASE}{mine.uid}/price-override/", {"discount": "1", "reason": "x"}, format="json").status_code == 200
    _issue(head, draft["uid"])
    response = head.post(path, {"discount": "1", "reason": "x"}, format="json")
    assert response.status_code == 409 and response.json()["code"] == "agreement_not_draft"
    assert head.post(f"{BASE}8b1a3c55-2a4b-4b8e-9d6f-0a1b2c3d4e5f/price-override/", {"discount": "1", "reason": "x"}, format="json").status_code == 404
