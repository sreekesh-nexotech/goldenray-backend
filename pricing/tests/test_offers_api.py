"""pricing/offers/ — CRUD and the lifecycle (engines.offers transitions + PLAN's PAUSED), expiry, applicable offer."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from audit.models import AuditLog
from core.models import OutboxEvent
from engines import offers as offer_engine
from pricing.models import Offer, OfferStatus, OfferTransition
from pricing.services import offers
from pricing.tasks import expire_offers
from pricing.tests.factories import OfferFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/pricing/offers/"


def detail(offer, suffix=""):
    return f"{URL}{offer.uid}/{suffix}"


PAYLOAD = {"name": "Monsoon 2026 Discount", "type": "FLAT", "value": "5000", "applies_to_system": "ALL", "applies_to_tier": "ALL", "starts_on": "2026-01-01", "ends_on": "2026-12-31"}


class TestPermissions:
    def test_anonymous_outsider_and_actions(self, api_client, outsider, viewer, editor):
        offer = OfferFactory()
        assert api_client.get(URL).status_code == 401
        assert outsider.get(URL).status_code == 403
        assert viewer.get(URL).status_code == 200
        assert viewer.post(URL, PAYLOAD, format="json").status_code == 403
        assert editor.post(detail(offer, "approve/"), {}, format="json").status_code == 403
        assert editor.post(detail(offer, "activate/"), {}, format="json").status_code == 403
        assert editor.post(detail(offer, "archive/"), {}, format="json").status_code == 403
        assert editor.delete(detail(offer)).status_code == 403


class TestLifecycle:
    def test_create_approve_activate_pause_resume_archive(self, client, pricing_user):
        response = client.post(URL, PAYLOAD, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["status"] == "DRAFT" and body["code"].startswith("OFFER-") and body["transitions"][0]["to_status"] == "DRAFT"
        offer = Offer.objects.get(uid=body["uid"])
        for action, status in (("approve", "APPROVED"), ("activate", "ACTIVE"), ("pause", "PAUSED"), ("activate", "ACTIVE"), ("archive", "ARCHIVED")):
            result = client.post(detail(offer, f"{action}/"), {"reason": f"{action} it"}, format="json")
            assert result.status_code == 200, result.json()
            assert result.json()["status"] == status
        steps = list(OfferTransition.objects.filter(offer=offer).values_list("from_status", "to_status"))
        assert steps == [("", "DRAFT"), ("DRAFT", "APPROVED"), ("APPROVED", "ACTIVE"), ("ACTIVE", "PAUSED"), ("PAUSED", "ACTIVE"), ("ACTIVE", "ARCHIVED")]
        assert OutboxEvent.objects.filter(event_type="pricing.offer_status_changed").count() == 5
        assert AuditLog.objects.filter(action="pricing.offer_activated", actor=pricing_user).count() == 2
        again = client.post(detail(offer, "archive/"), {}, format="json")
        assert again.status_code == 409 and again.json()["code"] == "offer_already_archived"

    def test_invalid_transitions_follow_the_engine_table(self, client):
        offer = OfferFactory()
        response = client.post(detail(offer, "activate/"), {}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "invalid_transition"
        assert client.post(detail(offer, "pause/"), {}, format="json").json()["code"] == "invalid_transition"
        for status, targets in offer_engine.VALID_TRANSITIONS.items():
            for target in targets:
                assert offers.is_valid_transition(status, target)
        assert not offers.is_valid_transition("DRAFT", "ACTIVE")

    def test_edit_rules(self, client):
        offer = OfferFactory(status=OfferStatus.APPROVED)
        response = client.patch(detail(offer), {"value": "6000", "expected_version": 1}, format="json")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "DRAFT" and body["content_version"] == 2 and body["transitions"][-1]["reason"].startswith("Content changed")
        stale = client.patch(detail(offer), {"value": "1", "expected_version": 1}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        active = OfferFactory(status=OfferStatus.ACTIVE)
        refused = client.patch(detail(active), {"name": "x"}, format="json")
        assert refused.status_code == 409 and refused.json()["code"] == "offer_not_editable"
        assert client.patch(detail(OfferFactory(status=OfferStatus.DRAFT)), {"print_on_quotation": False}, format="json").json()["content_version"] == 1

    @pytest.mark.parametrize(
        "extra,field",
        [
            ({"type": "PERCENT", "value": "150"}, "value"),
            ({"name": ""}, "name"),
            ({"starts_on": "2026-12-31", "ends_on": "2026-01-01"}, "ends_on"),
            ({"applies_to_size_key": "abc"}, "applies_to_size_key"),
            ({"type": "BOGO"}, "type"),
        ],
    )
    def test_validation(self, client, extra, field):
        response = client.post(URL, {**PAYLOAD, **extra}, format="json")
        assert response.status_code == 400 and field in response.json()["errors"]

    def test_code_is_unique_and_delete_only_drafts(self, client):
        OfferFactory(code="MONSOON")
        taken = client.post(URL, {**PAYLOAD, "code": "monsoon"}, format="json")
        assert taken.status_code == 409 and taken.json()["code"] == "offer_code_taken"
        active = OfferFactory(status=OfferStatus.ACTIVE)
        refused = client.delete(detail(active))
        assert refused.status_code == 409 and refused.json()["code"] == "offer_not_draft"
        draft = OfferFactory()
        assert client.delete(detail(draft)).status_code == 204

    def test_activate_refused_after_end_date(self, client):
        offer = OfferFactory(status=OfferStatus.APPROVED, ends_on=timezone.localdate() - timedelta(days=1))
        response = client.post(detail(offer, "activate/"), {}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "offer_already_ended"

    def test_list_filters_and_query_budget(self, client, django_assert_max_num_queries):
        today = timezone.localdate()
        OfferFactory(status=OfferStatus.ACTIVE, starts_on=today - timedelta(days=1), ends_on=today + timedelta(days=1))
        OfferFactory(status=OfferStatus.ACTIVE, starts_on=today + timedelta(days=10))
        for _ in range(12):
            offers.create_offer(user=None, data={"name": "x", "type": "FLAT", "value": Decimal("1")})
        assert client.get(URL, {"active_on": str(today)}).json()["count"] == 1
        assert client.get(URL, {"status": "ACTIVE"}).json()["count"] == 2
        with django_assert_max_num_queries(12):
            assert client.get(URL, {"page_size": 50}).json()["count"] == 14


class TestExpiryAndApplication:
    def test_expire_task(self):
        today = timezone.localdate()
        ended = OfferFactory(status=OfferStatus.ACTIVE, ends_on=today - timedelta(days=1))
        paused = OfferFactory(status=OfferStatus.PAUSED, ends_on=today - timedelta(days=3))
        running = OfferFactory(status=OfferStatus.ACTIVE, ends_on=today)
        assert sorted(expire_offers()) == sorted([ended.code, paused.code])
        ended.refresh_from_db()
        running.refresh_from_db()
        assert ended.status == "EXPIRED" and running.status == "ACTIVE"
        assert OfferTransition.objects.get(offer=ended).by_label == "SYSTEM"

    def test_applicable_offer_and_amount(self):
        old = OfferFactory(code="OLD", status=OfferStatus.ACTIVE, value=Decimal("2500"))
        new = OfferFactory(code="NEW", status=OfferStatus.ACTIVE, applies_to_system="ONGRID", applies_to_size_key="5sp", applies_to_size_kw=Decimal("5"))
        OfferFactory(code="HYB", status=OfferStatus.ACTIVE, applies_to_system="HYBRID")
        assert offers.applicable_offer(system_type="ONGRID", tier="VALUE", size_key="5sp") == new
        assert offers.applicable_offer(system_type="ONGRID", tier="VALUE", size_key="5tp") == old
        assert offers.applicable_offer(system_type="ONGRID", tier="VALUE", size_key="3", on=date(2020, 1, 1)) == old
        percent = OfferFactory(type="PERCENT", value=Decimal("3"))
        assert offers.offer_amount(percent, Decimal("210285")) == Decimal("6308.55")
        assert offers.offer_amount(new, Decimal("1000")) == Decimal("1000.00")
        assert offers.offer_summary(new)["applies_to_size_key"] == "5sp"
