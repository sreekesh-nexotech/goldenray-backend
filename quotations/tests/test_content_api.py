"""``/api/v1/quotation-content/…`` (content versions, the four masters) and ``/api/public/v1/testimonials/``."""

from __future__ import annotations

import copy
import datetime as dt

import pytest

from core.errors import DomainError
from core.models import OutboxEvent
from quotations.models import Campaign, ContentStatus, ContentVersion, Inclusion
from quotations.models import Testimonial as HomeownerTestimonial
from quotations.models import TierDisplayName
from quotations.services import content
from quotations.tests import flarize
from quotations.views.public import PublicTestimonialListView

pytestmark = pytest.mark.django_db
BASE = "/api/v1/quotation-content/"
PUBLIC = "/api/public/v1/testimonials/"


@pytest.fixture
def imported(db):
    flarize.import_content()
    return ContentVersion.objects.get(status=ContentStatus.PUBLISHED)


@pytest.fixture
def editor(auth_client, make_user):
    return auth_client(make_user(grants={"quotation_content": ["view", "edit"]}))


@pytest.fixture
def publisher(auth_client, make_user):
    return auth_client(make_user(grants={"quotation_content": "*"}))


@pytest.fixture
def reader(auth_client, make_user):
    return auth_client(make_user(grants={"quotation_content": ["view"]}))


@pytest.fixture
def media_storage(settings, tmp_path):
    settings.MEDIA_PUBLIC_BACKEND = "local"
    settings.PUBLIC_MEDIA_ROOT = tmp_path / "public"
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    settings.USE_X_ACCEL = False
    return tmp_path


def _too_long(payload: dict) -> dict:
    candidate = copy.deepcopy(payload)
    candidate["terms"][0]["body"] = {"en": "word " * 4000, "ml": "വാക്ക് " * 4000}
    return candidate


# ── content versions ───────────────────────────────────────────────────────────────────────────────────────────────


def test_versions_list_and_detail(imported, reader, api_client, auth_client, make_user, django_assert_max_num_queries):
    assert api_client.get(f"{BASE}versions/").status_code == 401
    assert auth_client(make_user(grants={"quotations": ["view"]})).get(f"{BASE}versions/").status_code == 403
    with django_assert_max_num_queries(8):
        listed = reader.get(f"{BASE}versions/")
    assert listed.status_code == 200 and listed.json()["results"][0]["status"] == "PUBLISHED" and "language_payload" not in listed.json()["results"][0]
    assert listed.json()["results"][0]["fit_ok"] is True
    detail = reader.get(f"{BASE}versions/{imported.uid}/").json()
    assert detail["language_payload"] == imported.language_payload and detail["release_payload"]["inclusionMatrix"]
    assert reader.get(f"{BASE}versions/", {"status": "DRAFT"}).json()["count"] == 0
    assert reader.post(f"{BASE}versions/", {}, format="json").status_code == 403


def test_draft_edit_fit_and_publish(imported, editor, publisher):
    created = editor.post(f"{BASE}versions/", {"note": "monsoon copy"}, format="json")
    assert created.status_code == 201 and created.json()["status"] == "DRAFT" and created.json()["number"] == imported.number + 1
    assert created.json()["language_payload"] == imported.language_payload
    uid = created.json()["uid"]
    assert editor.post(f"{BASE}versions/", {}, format="json").json()["code"] == "draft_exists"
    bad = _too_long(imported.language_payload)
    refused = editor.patch(f"{BASE}versions/{uid}/", {"language_payload": bad}, format="json")
    assert refused.status_code == 422 and refused.json()["code"] == "content_invalid" and refused.json()["errors"]
    report = editor.post(f"{BASE}versions/{uid}/fit-check/", {"language_payload": bad}, format="json").json()
    assert report["ok"] is False and report["errors"]
    assert editor.post(f"{BASE}versions/{uid}/fit-check/", {}, format="json").json()["ok"] is True
    assert editor.patch(f"{BASE}versions/{uid}/", {"language_payload": []}, format="json").json()["code"] == "validation_error"
    version = created.json()["version"]
    edited = editor.patch(f"{BASE}versions/{uid}/", {"note": "edited", "expected_version": version}, format="json")
    assert edited.status_code == 200 and edited.json()["note"] == "edited"
    assert editor.patch(f"{BASE}versions/{uid}/", {"note": "x", "expected_version": version}, format="json").json()["code"] == "stale_version"
    assert editor.post(f"{BASE}versions/{uid}/publish/", {}, format="json").status_code == 403
    assert publisher.post(f"{BASE}versions/{uid}/publish/", {"expected_version": 1}, format="json").json()["code"] == "stale_version"
    published = publisher.post(f"{BASE}versions/{uid}/publish/", {"note": "go"}, format="json")
    assert published.status_code == 200 and published.json()["status"] == "PUBLISHED" and published.json()["release_sha256"]
    imported.refresh_from_db()
    assert imported.status == ContentStatus.SUPERSEDED and imported.superseded_at is not None
    assert OutboxEvent.objects.filter(event_type="quotation_content.published", aggregate_uid=uid).exists()
    assert publisher.patch(f"{BASE}versions/{uid}/", {"note": "late"}, format="json").json()["code"] == "version_not_draft"
    assert publisher.post(f"{BASE}versions/{uid}/publish/", {}, format="json").json()["code"] == "version_not_draft"


def test_create_draft_errors(db, editor, imported):
    ContentVersion.objects.all().delete()
    missing = editor.post(f"{BASE}versions/", {}, format="json")
    assert missing.status_code == 400 and "language_payload" in missing.json()["errors"]
    assert editor.post(f"{BASE}versions/", {"language_payload": {}}, format="json").json()["code"] == "validation_error"
    assert editor.post(f"{BASE}versions/", {"language_payload": _too_long(imported.language_payload)}, format="json").status_code == 422
    fresh = editor.post(f"{BASE}versions/", {"language_payload": imported.language_payload}, format="json")
    assert fresh.status_code == 201


def test_publish_refuses_content_that_no_longer_fits(imported, publisher, editor):
    uid = editor.post(f"{BASE}versions/", {}, format="json").json()["uid"]
    ContentVersion.objects.filter(uid=uid).update(language_payload=_too_long(imported.language_payload))
    assert publisher.post(f"{BASE}versions/{uid}/publish/", {}, format="json").json()["code"] == "content_invalid"
    with pytest.raises(Exception) as error:
        content.update_draft(ContentVersion(pk=10**9), user=None, data={})
    assert getattr(error.value, "code", "") == "not_found"


def test_daily_generation_falls_back_without_a_price_release(db):
    from engines.content_fit import FALLBACK_DAILY_GEN_PER_KW

    assert content.daily_gen_per_kw() == FALLBACK_DAILY_GEN_PER_KW


# ── masters ────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_inclusions_crud(imported, editor, reader, django_assert_max_num_queries):
    with django_assert_max_num_queries(8):
        listed = reader.get(f"{BASE}inclusions/", {"kind": "SERVICE"})
    assert listed.status_code == 200 and listed.json()["count"] == Inclusion.objects.filter(kind="SERVICE").count() > 0
    body = {"key": "freeCleaning", "kind": "COMPONENT", "label_en": "Free cleaning", "label_ml": "സൗജന്യ ക്ലീനിംഗ്", "applies_to": {"BASE": False, "VALUE": True, "PREMIUM": True}}
    created = editor.post(f"{BASE}inclusions/", body, format="json")
    assert created.status_code == 201, created.json()
    duplicate = editor.post(f"{BASE}inclusions/", body, format="json")
    assert duplicate.status_code == 400 and "key" in duplicate.json()["errors"]
    with pytest.raises(DomainError) as error:
        content.create_inclusion(user=None, data=body)
    assert error.value.code == "inclusion_key_taken"
    assert editor.post(f"{BASE}inclusions/", {**body, "key": "1bad"}, format="json").status_code == 400
    assert editor.post(f"{BASE}inclusions/", {**body, "key": "other", "applies_to": {"GOLD": True}}, format="json").json()["errors"]["applies_to"]
    assert editor.post(f"{BASE}inclusions/", {**body, "key": "other", "applies_to": {"BASE": 3}}, format="json").status_code == 400
    assert editor.post(f"{BASE}inclusions/", {**body, "key": "other", "applies_to": {"BASE": "Yes"}}, format="json").status_code == 400
    service = editor.post(f"{BASE}inclusions/", {**body, "key": "svc_other", "kind": "SERVICE", "applies_to": {"BASE": "2 visits"}}, format="json")
    assert service.status_code == 201
    uid, version = created.json()["uid"], created.json()["version"]
    assert reader.patch(f"{BASE}inclusions/{uid}/", {"label_en": "x"}, format="json").status_code == 403
    updated = editor.patch(f"{BASE}inclusions/{uid}/", {"label_en": "Free panel cleaning", "expected_version": version}, format="json")
    assert updated.status_code == 200 and updated.json()["label_en"] == "Free panel cleaning"
    assert editor.patch(f"{BASE}inclusions/{uid}/", {"label_en": "y", "expected_version": version}, format="json").json()["code"] == "stale_version"
    assert editor.patch(f"{BASE}inclusions/{uid}/", {"key": service.json()["key"]}, format="json").status_code == 400
    with pytest.raises(DomainError) as error:
        content.update_inclusion(Inclusion.objects.get(uid=uid), user=None, data={"key": service.json()["key"]})
    assert error.value.code == "inclusion_key_taken"
    assert editor.delete(f"{BASE}inclusions/{uid}/").status_code == 204
    assert editor.get(f"{BASE}inclusions/{uid}/").status_code == 404
    matrix = content.inclusion_matrix()
    assert "freeCleaning" not in matrix["Value"] and "svc_other" not in matrix["Value"]
    assert {"label": "Free cleaning", "labelMl": "സൗജന്യ ക്ലീനിംഗ്", "base": "2 visits", "value": None, "premium": None} in matrix["_serviceMatrix"]


def test_tier_names_keep_one_recommended(imported, editor):
    rows = editor.get(f"{BASE}tier-names/", {"system_type": "ONGRID"}).json()["results"]
    base = next(row for row in rows if row["tier"] == "BASE")
    promoted = editor.patch(f"{BASE}tier-names/{base['uid']}/", {"is_recommended": True}, format="json")
    assert promoted.status_code == 200
    assert list(TierDisplayName.objects.filter(system_type="ONGRID", is_recommended=True).values_list("tier", flat=True)) == ["BASE"]
    duplicate = editor.post(f"{BASE}tier-names/", {"system_type": "ONGRID", "tier": "BASE", "name_en": "Again", "name_ml": "വീണ്ടും"}, format="json")
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "tier_name_exists"
    assert editor.post(f"{BASE}tier-names/", {"system_type": "OFFGRID", "tier": "BASE", "name_en": "x"}, format="json").status_code == 400
    names = content.tier_display_names()
    assert names["ONGRID"]["en"]["recommendedTier"] == "base"


def test_testimonials_and_the_public_endpoint(imported, editor, api_client, django_assert_max_num_queries, media_storage):
    from media.tests.factories import stored_asset

    body = {
        "customer_name": "Homeowner 99",
        "location": "Kochi",
        "quote_en": "Great service.",
        "capacity_kw": "3.00",
        "bill_before": "4000",
        "bill_after": "300",
        "show_on_website": True,
        "sort_order": 0,
    }
    created = editor.post(f"{BASE}testimonials/", body, format="json")
    assert created.status_code == 201, created.json()
    assert editor.post(f"{BASE}testimonials/", {**body, "bill_after": "5000"}, format="json").json()["errors"]["bill_after"]
    private = stored_asset(visibility="PRIVATE")
    refused = editor.post(f"{BASE}testimonials/", {**body, "photo": str(private.uid)}, format="json")
    assert refused.status_code == 400 and "photo" in refused.json()["errors"]
    with django_assert_max_num_queries(6):
        first = api_client.get(PUBLIC)
    assert first.status_code == 200 and first["X-Cache"] == "MISS" and "max-age" in first["Cache-Control"]
    rows = first.json()["results"]
    assert [row["name"] for row in rows] == list(HomeownerTestimonial.objects.filter(is_active=True, show_on_website=True).order_by("sort_order", "id").values_list("customer_name", flat=True))
    row = next(row for row in rows if row["name"] == "Homeowner 99")
    assert set(row) == {"uid", "name", "location", "system_label", "capacity_kw", "installed_on", "quote", "quote_ml", "photo_src", "bill_before", "bill_after", "monthly_saving", "sort_order"}
    assert row["quote"] == "Great service." and row["monthly_saving"] in ("3700.00", "3700", 3700)
    assert api_client.get(PUBLIC)["X-Cache"] == "HIT"
    hidden = editor.patch(f"{BASE}testimonials/{created.json()['uid']}/", {"show_on_website": False}, format="json")
    assert hidden.status_code == 200
    fresh = api_client.get(PUBLIC)
    assert fresh["X-Cache"] == "MISS" and "Homeowner 99" not in [row["name"] for row in fresh.json()["results"]]
    assert editor.get(f"{BASE}testimonials/", {"show_on_website": "false"}).json()["count"] >= 1


def test_public_testimonials_throttle_scope():
    request = type("R", (), {"method": "GET"})()
    assert PublicTestimonialListView().get_throttle_scope(request) == "public_read"
    assert PublicTestimonialListView.authentication_classes == []


def test_campaigns(imported, editor):
    body = {"title": "Monsoon", "body_en": "Save more", "starts_on": "2026-06-01", "ends_on": "2026-05-01", "is_active": True}
    assert editor.post(f"{BASE}campaigns/", body, format="json").status_code == 400
    created = editor.post(f"{BASE}campaigns/", {**body, "ends_on": "2026-12-31"}, format="json")
    assert created.status_code == 201
    campaign = Campaign.objects.get(uid=created.json()["uid"])
    value = content.campaign_value(campaign)
    assert content.active_campaign([value], dt.date(2026, 7, 1)) is not None
    assert content.active_campaign([value], dt.date(2027, 1, 1)) is None
    assert editor.get(f"{BASE}campaigns/", {"is_active": "true"}).json()["count"] >= 1
    assert editor.delete(f"{BASE}campaigns/{campaign.uid}/").status_code == 204
    assert editor.delete(f"{BASE}campaigns/{campaign.uid}/").status_code == 404
