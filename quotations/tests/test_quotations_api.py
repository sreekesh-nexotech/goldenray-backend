"""``/api/v1/quotations/…`` end to end over the real Flarize data (PackRelease #1): create, system options, draft edit,
preview, issue, documents, send, discounts, accept, cancel, revise, history — every status and error code."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core import mail
from django.utils import timezone

from audit.models import AuditLog
from core.models import OutboxEvent
from documents.models import RenderJob
from documents.services.jobs import run_job
from engines.frozen import sha256_hex
from quotations.models import BomSnapshot, CommercialSnapshot, DiscountRequest, EmailLog, EmailStatus, Quotation, QuotationStatus, Version, VersionStatus
from quotations.services import quotations as quotation_services
from quotations.services import sending
from quotations.tests.conftest import quotation_data

pytestmark = pytest.mark.django_db
BASE = "/api/v1/quotations/"


def _create(client, customer, **overrides):
    response = client.post(BASE, quotation_data(customer, **overrides), format="json")
    assert response.status_code == 201, response.json()
    return response.json()


def _issue(client, quotation, number=1):
    response = client.post(f"{BASE}{quotation['uid']}/versions/{number}/issue/", {}, format="json")
    assert response.status_code == 200, response.json()
    return response.json()


# ── create / options ───────────────────────────────────────────────────────────────────────────────────────────────


def test_create_pins_the_current_releases_and_emits(world, executive, executive_user, customer):
    body = _create(executive, customer)
    assert body["status"] == "DRAFT" and body["number"] == "" and body["owner"]["uid"] == str(executive_user.uid)
    assert [row["number"] for row in body["versions"]] == [1] and body["current_version"]["status"] == "DRAFT"
    version = Version.objects.get(uid=body["current_version"]["uid"])
    assert version.pack_release.number == 1 and version.pack_release.price_release.number == 1
    assert version.price_release_id == version.pack_release.price_release_id and version.content_release is not None
    assert (version.size_kw, version.phase, version.structure_type) == (Decimal("3"), "1P", "flatRoof")
    assert OutboxEvent.objects.filter(event_type="quotations.created", aggregate_uid=body["uid"]).exists()
    assert AuditLog.objects.filter(action="quotations.created", object_uid=body["uid"]).exists()


def test_create_requires_authentication_and_permission(world, api_client, outsider, customer):
    assert api_client.post(BASE, quotation_data(customer), format="json").status_code == 401
    assert outsider.post(BASE, quotation_data(customer), format="json").status_code == 403
    assert outsider.get(BASE).status_code == 403


@pytest.mark.parametrize(
    ("overrides", "field", "status", "code"),
    [
        ({"tier": "GOLD"}, "tier", 400, "validation_error"),
        ({"size_key": "99"}, "size_key", 400, "validation_error"),
        ({"phase": "3P"}, None, 409, "no_approved_package"),
        ({"battery_config": "1"}, "battery_config", 400, "validation_error"),
        ({"future_size_key": "77"}, "future_size_key", 400, "validation_error"),
        ({"vehicle_type": "ROCKET"}, "vehicle_type", 400, "validation_error"),
        ({"subsidy_type": "ghs"}, "ghs_houses", 400, "validation_error"),
        ({"distance_km": "6000"}, "distance_km", 400, "validation_error"),
        ({"source": "DISTRICT"}, "district", 400, "validation_error"),
        ({"affiliate_ref": "AFF-1"}, "affiliate_ref", 400, "validation_error"),
        ({"selections": {"validity_override_days": 10}}, None, 403, "validity_override_denied"),
        ({"selections": {"appliance_rows": [{"id": "BAD ID", "qty": 1, "hours": 1}]}}, "selections.appliance_rows[0].id", 400, "validation_error"),
        ({"selections": {"tier_selections": {"gold": {}}}}, "selections.tier_selections", 400, "validation_error"),
    ],
)
def test_create_validation(world, executive, customer, overrides, field, status, code):
    response = executive.post(BASE, quotation_data(customer, **overrides), format="json")
    assert response.status_code == status, response.json()
    assert response.json()["code"] == code
    if field:
        assert field in response.json()["errors"]


def test_create_unknown_customer(world, executive):
    import uuid
    from types import SimpleNamespace

    body = quotation_data(SimpleNamespace(uid=uuid.uuid4()))
    response = executive.post(BASE, body, format="json")
    assert response.status_code == 400 and "customer_uid" in response.json()["errors"]


def test_validity_override_is_allowed_to_approvers(world, head, customer):
    body = _create(head, customer, selections={"validity_override_days": 30})
    assert Version.objects.get(uid=body["current_version"]["uid"]).selections["validity_override_days"] == 30


def test_system_options(world, executive):
    response = executive.get(f"{BASE}system-options/", {"system_type": "ONGRID"})
    assert response.status_code == 200
    body = response.json()
    assert body["pack_release"] == 1 and set(body["options"]) == {"ONGRID"}
    ongrid = body["options"]["ONGRID"]
    assert "3" in {size["size_key"] for size in ongrid["sizes"]} and set(ongrid["tiers"]) == {"BASE", "VALUE", "PREMIUM"}
    assert all(pack["customer_price_incl_gst"] for pack in ongrid["packs"])
    assert body["subsidy_types"] and body["vehicles"] and body["tier_names"]["ONGRID"]["VALUE"]["recommended"] is True
    assert executive.get(f"{BASE}system-options/", {"phase": "3P"}).status_code == 200


# ── list / retrieve / scope ────────────────────────────────────────────────────────────────────────────────────────


def test_scope_owned_sees_own_quotations_only(world, executive, head, customer):
    mine = _create(executive, customer)
    theirs = _create(head, customer)
    listed = [row["uid"] for row in executive.get(BASE).json()["results"]]
    assert mine["uid"] in listed and theirs["uid"] not in listed
    assert executive.get(f"{BASE}{theirs['uid']}/").status_code == 404
    assert executive.get(f"{BASE}{mine['uid']}/").status_code == 200
    assert {row["uid"] for row in head.get(BASE).json()["results"]} >= {mine["uid"], theirs["uid"]}
    assert head.get(BASE, {"status": "DRAFT", "customer": str(customer.uid)}).json()["count"] >= 2


# ── the draft ─────────────────────────────────────────────────────────────────────────────────────────────────────


def test_draft_edit_with_stale_version_and_missing_version(world, executive, customer):
    quotation = _create(executive, customer)
    url = f"{BASE}{quotation['uid']}/versions/1/"
    detail = executive.get(url).json()
    assert detail["status"] == "DRAFT" and detail["document"] is None and detail["document_status"] == {"en": None, "ml": None}
    response = executive.patch(url, {"tier": "BASE", "expected_version": detail["version"]}, format="json")
    assert response.status_code == 200 and response.json()["tier"] == "BASE"
    stale = executive.patch(url, {"tier": "PREMIUM", "expected_version": detail["version"]}, format="json")
    assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
    assert executive.patch(url, {"size_key": "99"}, format="json").json()["code"] == "validation_error"
    assert executive.get(f"{BASE}{quotation['uid']}/versions/9/").status_code == 404


def test_preview_projects_internal_pricing(world, executive, head, customer):
    quotation = _create(executive, customer)
    url = f"{BASE}{quotation['uid']}/versions/1/preview/"
    body = executive.post(url, {}, format="json").json()
    assert len(body["gate_report"]["checks"]) == 8
    assert set(body["pricing"]["tiers"]) == {"BASE", "VALUE", "PREMIUM"}
    assert all("internal" not in tier for tier in body["pricing"]["tiers"].values())
    assert body["payload"]["pricing"]["internal"] is None and body["payload"]["pricing"]["internalWithheld"] is True
    internal = head.post(url, {}, format="json").json()
    assert all("internal" in tier for tier in internal["pricing"]["tiers"].values())
    assert internal["payload"]["pricing"]["internal"]["cost"]
    assert Version.objects.get(uid=quotation["current_version"]["uid"]).document_payload is None


# ── issue ─────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_issue_freezes_the_document_and_snapshots(world, executive, head, customer):
    quotation = _create(executive, customer)
    detail = _issue(executive, quotation)
    version = Version.objects.get(uid=detail["uid"])
    row = Quotation.objects.get(uid=quotation["uid"])
    assert row.status == QuotationStatus.ISSUED and row.number.startswith("GR-") and row.valid_until is not None and row.issued_at is not None
    assert version.status == VersionStatus.ISSUED and version.document_payload_sha256 == sha256_hex(version.document_payload)
    customer_total = Decimal(str(version.document_payload["payload"]["pricing"]["customer"]["customerTotalIncludingGST"]))
    assert version.gate_report["status"] == "PASS" and version.final_price == customer_total - version.offer_total
    assert BomSnapshot.objects.filter(quotation_version=version).count() == CommercialSnapshot.objects.filter(quotation_version=version).count() == 3
    assert BomSnapshot.objects.get(quotation_version=version, is_primary=True).tier == "VALUE"
    assert {version.document_job.language, version.document_job_ml.language} == {"en", "ml"}
    assert version.document_job.payload_sha256 == version.document_payload_sha256
    event = OutboxEvent.objects.get(event_type="quotations.issued", aggregate_uid=row.uid)
    assert event.payload["customer_uid"] == str(customer.uid) and event.payload["number"] == row.number
    # internal cost and margin only for pricing_internal readers
    assert detail["gross_margin_pct"] is None and all("cost" not in snap["cost_lines"] and snap["margin_check"] is None for snap in detail["commercial_snapshots"])
    assert detail["document"]["payload"]["pricing"]["internal"] is None and detail["document"]["payload"]["pricing"]["internalWithheld"] is True
    full = head.get(f"{BASE}{quotation['uid']}/versions/1/").json()
    assert full["gross_margin_pct"] is not None and all("cost" in snap["cost_lines"] for snap in full["commercial_snapshots"])
    # issued is final
    again = executive.post(f"{BASE}{quotation['uid']}/versions/1/issue/", {}, format="json")
    assert again.status_code == 409 and again.json()["code"] == "version_not_draft"
    assert executive.patch(f"{BASE}{quotation['uid']}/versions/1/", {"tier": "BASE"}, format="json").json()["code"] == "version_not_draft"
    assert executive.post(f"{BASE}{quotation['uid']}/versions/1/preview/", {}, format="json").json()["code"] == "version_not_draft"


def test_issue_stale_version(world, executive, customer):
    quotation = _create(executive, customer)
    response = executive.post(f"{BASE}{quotation['uid']}/versions/1/issue/", {"expected_version": 99}, format="json")
    assert response.status_code == 409 and response.json()["code"] == "stale_version"


def test_issue_blocked_by_the_gate(world, executive, customer, monkeypatch):
    from quotations.services import inputs

    real = inputs.customer_document
    monkeypatch.setattr(inputs, "customer_document", lambda row: {**real(row), "customerId": None})
    quotation = _create(executive, customer)
    preview = executive.post(f"{BASE}{quotation['uid']}/versions/1/preview/", {}, format="json").json()
    assert preview["gate_report"]["status"] == "BLOCKED"
    response = executive.post(f"{BASE}{quotation['uid']}/versions/1/issue/", {}, format="json")
    assert response.status_code == 422 and response.json()["code"] == "generation_blocked" and "CUSTOMER_DATA" in response.json()["message"]
    assert Quotation.objects.get(uid=quotation["uid"]).number == ""


def test_issue_refused_on_a_superseded_release(world, executive, customer, monkeypatch):
    from types import SimpleNamespace

    from quotations.services import inputs

    quotation = _create(executive, customer)
    monkeypatch.setattr(inputs, "current_pack_release", lambda: SimpleNamespace(pk=-1, number=99))
    response = executive.post(f"{BASE}{quotation['uid']}/versions/1/issue/", {}, format="json")
    assert response.status_code == 409 and response.json()["code"] == "release_superseded"


def test_issue_with_an_unknown_offer_and_a_refused_swap(world, executive, customer):
    offer = _create(executive, customer, selections={"offer_code": "NOPE"})
    response = executive.post(f"{BASE}{offer['uid']}/versions/1/preview/", {}, format="json")
    assert response.status_code == 400 and response.json()["code"] == "offer_not_active"
    swap = _create(executive, customer, selections={"tier_selections": {"value": {"panel": "no-such-panel"}}})
    response = executive.post(f"{BASE}{swap['uid']}/versions/1/issue/", {}, format="json")
    assert response.status_code == 409 and response.json()["code"]


# ── documents and sending ─────────────────────────────────────────────────────────────────────────────────────────


def test_document_render_and_send(world, executive, customer, document_storage, django_capture_on_commit_callbacks):
    quotation = _create(executive, customer)
    base = f"{BASE}{quotation['uid']}/versions/1/"
    assert executive.get(f"{base}document/").json()["code"] == "version_not_issued"
    assert executive.post(f"{base}send/", {"to": "a@example.com"}, format="json").json()["code"] == "version_not_issued"
    assert executive.post(f"{base}render/", {"language": "en"}, format="json").json()["code"] == "version_not_issued"
    detail = _issue(executive, quotation)
    assert detail["document_status"] == {"en": "QUEUED", "ml": "QUEUED"}
    assert executive.post(f"{base}send/", {"to": "a@example.com"}, format="json").json()["code"] == "document_not_ready"
    assert executive.post(f"{base}send/", {"to": "a@example.com", "channel": "WHATSAPP"}, format="json").json()["code"] in ("channel_unavailable", "validation_error")
    assert executive.post(f"{base}send/", {"to": "not-an-email"}, format="json").status_code == 400
    version = Version.objects.get(uid=detail["uid"])
    for job in (version.document_job, version.document_job_ml):
        assert run_job(str(job.uid)) == RenderJob.Status.DONE
    link = executive.get(f"{base}document/", {"language": "ml"})
    assert link.status_code == 200 and link.json()["language"] == "ml" and link.json()["url"]
    rerender = executive.post(f"{base}render/", {"language": "en"}, format="json")
    assert rerender.status_code == 200 and rerender.json()["payload_sha256"] == version.document_payload_sha256
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        sent = executive.post(f"{base}send/", {"to": "Customer@Example.com", "language": "ml"}, format="json")
    assert sent.status_code == 202 and sent.json()["status"] == "QUEUED"
    assert [callback.func.__name__ for callback in callbacks if getattr(callback, "func", None)].count("_enqueue") == 1
    assert "id" not in sent.json()  # integer ids never leave the service layer
    log = EmailLog.objects.get(version=version, to="customer@example.com")
    assert log.to == "customer@example.com" and log.version_id == version.pk
    assert sending.deliver(log.pk) == "sent"
    log.refresh_from_db()
    assert log.status == EmailStatus.SENT and log.sent_at is not None
    message = mail.outbox[-1]
    assert message.to == ["customer@example.com"] and message.attachments[0][0].endswith("-ml.pdf") and message.attachments[0][1][:4] == b"%PDF"
    assert sending.deliver(log.pk) == "skipped"
    assert executive.get(f"{base}").json()["email_logs"][0]["status"] == "SENT"


def test_deliver_failure_is_recorded(world, executive, customer, document_storage, monkeypatch):
    quotation = _create(executive, customer)
    version = Version.objects.get(uid=_issue(executive, quotation)["uid"])
    run_job(str(version.document_job.uid))
    log = sending.send(Version.objects.select_related("document_job").get(pk=version.pk), user=None, to="x@example.com")
    monkeypatch.setattr("quotations.services.sending.EmailMessage.send", lambda self, fail_silently=False: (_ for _ in ()).throw(OSError("smtp down")))
    with pytest.raises(OSError):
        sending.deliver(log.pk)
    log.refresh_from_db()
    assert log.status == EmailStatus.FAILED and "smtp down" in log.error
    assert sending.deliver(10**9) == "skipped"


# ── discounts ─────────────────────────────────────────────────────────────────────────────────────────────────────


def test_discount_requests(world, executive, head, head_user, customer):
    quotation = _create(executive, customer)
    url = f"{BASE}{quotation['uid']}/discount-requests/"
    assert executive.post(url, {"amount": "0", "reason": "x"}, format="json").status_code == 400
    assert executive.post(url, {"amount": "1000", "reason": " "}, format="json").status_code == 400
    created = executive.post(url, {"amount": "2500", "reason": "Neighbour referral"}, format="json")
    assert created.status_code == 201 and created.json()["status"] == "PENDING"
    duplicate = executive.post(url, {"amount": "100", "reason": "again"}, format="json")
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "discount_pending"
    blocked = executive.post(f"{BASE}{quotation['uid']}/versions/1/issue/", {}, format="json")
    assert blocked.status_code == 409 and blocked.json()["code"] == "discount_pending"
    request_uid = created.json()["uid"]
    assert executive.post(f"{url}{request_uid}/approve/", {}, format="json").status_code == 403
    assert head.post(f"{url}00000000-0000-0000-0000-000000000000/approve/", {}, format="json").status_code == 404
    assert head.post(f"{url}{request_uid}/approve/", {"expected_version": 99}, format="json").json()["code"] == "stale_version"
    approved = head.post(f"{url}{request_uid}/approve/", {"note": "ok"}, format="json")
    assert approved.status_code == 200 and approved.json()["status"] == "APPROVED"
    assert head.post(f"{url}{request_uid}/reject/", {}, format="json").json()["code"] == "discount_decided"
    assert executive.get(url).json()["count"] == 1
    # never your own request
    own = head.post(url, {"amount": "500", "reason": "mine"}, format="json").json()
    denied = head.post(f"{url}{own['uid']}/reject/", {}, format="json")
    assert denied.status_code == 403 and denied.json()["code"] == "self_action_denied"
    DiscountRequest.objects.filter(uid=own["uid"]).delete()
    version = Version.objects.get(uid=quotation["current_version"]["uid"])
    assert version.discount_total == Decimal("2500.00")
    detail = _issue(executive, quotation)
    issued = Version.objects.get(uid=detail["uid"])
    assert issued.final_price == Decimal(str(issued.document_payload["payload"]["pricing"]["customer"]["customerTotalIncludingGST"])) - issued.offer_total - Decimal("2500")
    assert Decimal(str(issued.document_payload["payload"]["pricing"]["customer"]["discount"]["value"]["totalReduction"])) == issued.offer_total + Decimal("2500")
    no_draft = executive.post(url, {"amount": "100", "reason": "late"}, format="json")
    assert no_draft.status_code == 409 and no_draft.json()["code"] == "no_draft"


def test_discount_decided_after_issue_and_reduction_exceeding_the_price(world, executive, head, head_user, customer):
    from quotations.services import lifecycle

    quotation = _create(executive, customer)
    row = Quotation.objects.get(uid=quotation["uid"])
    request = lifecycle.request_discount(row, user=head_user, amount=Decimal("99999999"), reason="too much")
    other = executive.post(f"{BASE}{quotation['uid']}/discount-requests/{request.uid}/approve/", {}, format="json")
    assert other.status_code == 403
    executive_request = DiscountRequest.objects.get(pk=request.pk)
    executive_request.requested_by = None
    executive_request.save(update_fields=["requested_by"])
    assert head.post(f"{BASE}{quotation['uid']}/discount-requests/{request.uid}/approve/", {}, format="json").status_code == 200
    response = executive.post(f"{BASE}{quotation['uid']}/versions/1/issue/", {}, format="json")
    assert response.status_code == 409 and response.json()["code"] == "reduction_exceeds_price"
    # a request left pending on a version that is no longer a draft cannot be decided
    Version.objects.filter(quotation=row).update(discount_total=0)
    pending = lifecycle.request_discount(row, user=None, amount=Decimal("10"), reason="late")
    DiscountRequest.objects.filter(pk=pending.pk).update(status="PENDING")
    Version.objects.filter(pk=pending.quotation_version_id).update(status=VersionStatus.SUPERSEDED, document_payload={"x": 1}, document_payload_sha256=sha256_hex({"x": 1}), issued_at=timezone.now())
    late = head.post(f"{BASE}{quotation['uid']}/discount-requests/{pending.uid}/reject/", {}, format="json")
    assert late.status_code == 409 and late.json()["code"] == "version_not_draft"


# ── accept / cancel / revise / history ────────────────────────────────────────────────────────────────────────────


def test_accept_cancel_revise_and_history(world, executive, customer):
    quotation = _create(executive, customer)
    url = f"{BASE}{quotation['uid']}/"
    draft_accept = executive.post(f"{url}accept/", {}, format="json")
    assert draft_accept.status_code == 409 and draft_accept.json()["code"] == "quotation_not_issued"
    assert executive.post(f"{url}revise/", {}, format="json").json()["code"] == "quotation_not_issued"
    _issue(executive, quotation)
    first_number = Quotation.objects.get(uid=quotation["uid"]).number
    revised = executive.post(f"{url}revise/", {"tier": "PREMIUM"}, format="json")
    assert revised.status_code == 201 and revised.json()["number"] == 2 and revised.json()["tier"] == "PREMIUM"
    assert Version.objects.get(quotation__uid=quotation["uid"], number=1).status == VersionStatus.SUPERSEDED
    assert Quotation.objects.get(uid=quotation["uid"]).status == QuotationStatus.DRAFT
    _issue(executive, quotation, number=2)
    row = Quotation.objects.get(uid=quotation["uid"])
    assert row.number == first_number  # the number is kept across versions
    assert executive.post(f"{url}accept/", {"expected_version": 999}, format="json").json()["code"] == "stale_version"
    accepted = executive.post(f"{url}accept/", {"note": "signed"}, format="json")
    assert accepted.status_code == 200 and accepted.json()["status"] == "ACCEPTED" and accepted.json()["accepted_at"]
    event = OutboxEvent.objects.get(event_type="quotations.accepted", aggregate_uid=row.uid)
    assert event.payload["version"] == 2 and event.payload["customer_uid"] == str(customer.uid) and event.payload["final_price"]
    closed = executive.post(f"{url}cancel/", {"reason": "changed mind"}, format="json")
    assert closed.status_code == 409 and closed.json()["code"] == "quotation_closed"
    history = executive.get(f"{url}history/").json()
    assert [v["number"] for v in history["versions"]] == [2, 1]
    actions = [event["action"] for event in history["events"]]
    assert {"quotations.created", "quotations.issued", "quotations.revised", "quotations.accepted"} <= set(actions)


def test_cancel_and_expiry(world, executive, customer):
    import datetime as dt

    from django.utils import timezone

    from quotations.services import lifecycle

    quotation = _create(executive, customer)
    url = f"{BASE}{quotation['uid']}/"
    assert executive.post(f"{url}cancel/", {}, format="json").status_code == 400
    assert executive.post(f"{url}cancel/", {"reason": "  "}, format="json").json()["code"] == "validation_error"
    cancelled = executive.post(f"{url}cancel/", {"reason": "went elsewhere"}, format="json")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED" and cancelled.json()["lost_reason"] == "went elsewhere"
    assert executive.post(f"{BASE}{quotation['uid']}/versions/1/issue/", {}, format="json").json()["code"] == "quotation_closed"

    second = _create(executive, customer)
    _issue(executive, second)
    Quotation.objects.filter(uid=second["uid"]).update(valid_until=timezone.localdate() - dt.timedelta(days=1))
    expired = executive.post(f"{BASE}{second['uid']}/accept/", {}, format="json")
    assert expired.status_code == 409 and expired.json()["code"] == "quotation_expired"
    assert lifecycle.expire_due() == [Quotation.objects.get(uid=second["uid"]).number]
    assert Quotation.objects.get(uid=second["uid"]).status == QuotationStatus.EXPIRED
    assert OutboxEvent.objects.filter(event_type="quotations.expired", aggregate_uid=second["uid"]).exists()
    assert lifecycle.expire_due() == []
    revised = executive.post(f"{BASE}{second['uid']}/revise/", {}, format="json")
    assert revised.status_code == 201
    assert Quotation.objects.get(uid=second["uid"]).expired_at is None


def test_release_published_warns_open_drafts(world, executive, customer, drain_outbox):
    from core.outbox import Event
    from quotations.events import warn_open_drafts

    quotation = _create(executive, customer)
    version = Version.objects.get(uid=quotation["current_version"]["uid"])
    number = version.pack_release.number + 1
    event = Event(id=1, event_type="packs.release_published", payload={"number": number}, aggregate_type="", aggregate_uid=None, attempts=0, created_at=None)
    warn_open_drafts(event)
    warn_open_drafts(event)
    version.refresh_from_db()
    assert [notice["release"] for notice in version.notices] == [number]
    warn_open_drafts(Event(id=2, event_type="packs.release_published", payload={}, aggregate_type="", aggregate_uid=None, attempts=0, created_at=None))
    refreshed = executive.patch(f"{BASE}{quotation['uid']}/versions/1/", {"refresh_release": True}, format="json")
    assert refreshed.status_code == 200 and refreshed.json()["notices"] == []


def test_services_not_found_paths(world, head_user, customer):
    from core.errors import DomainError
    from quotations.services import lifecycle

    quotation = quotation_services.create(user=head_user, data=quotation_data(customer, distance_km=Decimal("60")))
    version = quotation.current_version
    Version.objects.filter(pk=version.pk).delete()
    with pytest.raises(DomainError) as error:
        quotation_services.lock_version(version)
    assert error.value.code == "version_not_found"
    with pytest.raises(DomainError) as error:
        lifecycle.get_request(quotation, "00000000-0000-0000-0000-000000000000")
    assert error.value.code == "not_found"
    with pytest.raises(DomainError) as error:
        lifecycle.draft_version(quotation)
    assert error.value.code == "no_draft"
