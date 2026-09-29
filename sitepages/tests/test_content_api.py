"""pages/<uid>/text-slots/<key>/, image-slots/<key>/, seo/ and preview/ — what maintainers may change, and nothing else."""

import pytest

from audit.models import AuditLog
from core.errors import DomainError
from core.models import OutboxEvent
from media.tests.factories import MediaAssetFactory
from sitepages.models import PageImageSlot, PageSeo, PageTextSlot
from sitepages.tests.factories import PageFactory, PageImageSlotFactory, PageSeoFactory, PageTextSlotFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/pages/"


@pytest.fixture
def client(auth_client, make_user):
    return auth_client(make_user(grants={"pages": "*"}))


def slot_url(page, kind, key):
    return f"{URL}{page.uid}/{kind}-slots/{key}/"


class TestTextSlots:
    def test_only_value_is_editable_and_the_page_is_touched(self, client):
        slot = PageTextSlotFactory(key="headline", label="Headline", max_length=20)
        response = client.patch(slot_url(slot.page, "text", "headline"), {"value": "Go solar", "label": "Hacked", "key": "other", "max_length": 999, "expected_version": 1}, format="json")
        assert response.status_code == 200
        body = response.json()
        assert (body["value"], body["label"], body["key"], body["max_length"], body["version"]) == ("Go solar", "Headline", "headline", 20, 2)
        slot.page.refresh_from_db()
        assert slot.page.version == 2
        entry = AuditLog.objects.get(action="sitepages.text_slot_updated")
        assert entry.before == {"value": ""} and entry.after == {"value": "Go solar"} and entry.note == f"{slot.page.slug}:headline"
        event = OutboxEvent.objects.get(event_type="sitepages.page_updated")
        assert event.payload["paths"] == [slot.page.route] and event.payload["slug"] == slot.page.slug

    def test_draft_page_edit_emits_nothing_and_same_value_is_a_no_op(self, client):
        slot = PageTextSlotFactory(page=PageFactory(status="DRAFT"), key="headline", value="Same")
        client.patch(slot_url(slot.page, "text", "headline"), {"value": "Changed"}, format="json")
        assert not OutboxEvent.objects.exists()
        again = client.patch(slot_url(slot.page, "text", "headline"), {"value": "Changed"}, format="json")
        assert again.json()["version"] == 2

    def test_cap_is_enforced_with_the_numbers(self, client):
        slot = PageTextSlotFactory(key="headline", max_length=10)
        response = client.patch(slot_url(slot.page, "text", "headline"), {"value": "x" * 11}, format="json")
        assert response.status_code == 400 and response.json()["errors"]["value"] == ["This field holds up to 10 characters — you have 11."]

    @pytest.mark.parametrize(
        "kind,good,bad",
        [
            ("URL", "https://flarize.com/subsidy", "not a url"),
            ("URL", "/subsidy", "//evil.example.com"),
            ("URL", "/subsidy", "/with space"),
            ("EMAIL", "hello@flarize.com", "hello@"),
            ("PHONE", "+91 62829 22988", "call us"),
            ("SHORT_TEXT", "One line", "two\nlines"),
            ("LONG_TEXT", "two\nlines", None),
        ],
    )
    def test_kind_validation(self, client, kind, good, bad):
        slot = PageTextSlotFactory(key="field", kind=kind, max_length=None)
        assert client.patch(slot_url(slot.page, "text", "field"), {"value": good}, format="json").status_code == 200
        if bad is not None:
            response = client.patch(slot_url(slot.page, "text", "field"), {"value": bad}, format="json")
            assert response.status_code == 400 and response.json()["code"] == "validation_error" and "value" in response.json()["errors"]
        assert client.patch(slot_url(slot.page, "text", "field"), {"value": ""}, format="json").status_code == 200  # back to the built-in text

    def test_unknown_or_deleted_slot_is_404_and_stale_version_409(self, client):
        slot = PageTextSlotFactory(key="headline", version=3)
        assert client.patch(slot_url(slot.page, "text", "nope"), {"value": "x"}, format="json").json()["code"] == "slot_not_found"
        stale = client.patch(slot_url(slot.page, "text", "headline"), {"value": "x", "expected_version": 2}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        PageTextSlotFactory(page=slot.page, key="gone").soft_delete()
        assert client.patch(slot_url(slot.page, "text", "gone"), {"value": "x"}, format="json").status_code == 404

    def test_value_is_trimmed_like_the_legacy_editor(self, client, api_client):
        """Legacy CMS parity: the value is trimmed before the cap/kind checks, and a blank value clears the slot."""
        page = PageFactory(slug="trim-test", route="/trim-test")
        PageTextSlotFactory(page=page, key="headline", max_length=10, value="Old")
        PageTextSlotFactory(page=page, key="phone", kind="PHONE", max_length=None)
        padded = client.patch(slot_url(page, "text", "headline"), {"value": "  Join us!  \n"}, format="json")
        assert padded.status_code == 200 and padded.json()["value"] == "Join us!"  # 8 characters once trimmed
        assert client.patch(slot_url(page, "text", "phone"), {"value": " +91 62829 22988 "}, format="json").json()["value"] == "+91 62829 22988"
        blank = client.patch(slot_url(page, "text", "headline"), {"value": "   \n"}, format="json")
        assert blank.status_code == 200 and blank.json()["value"] == ""
        assert api_client.get("/api/public/v1/pages/trim-test/").json()["data"]["text"] == {"phone": "+91 62829 22988"}  # shipped headline applies again

    def test_the_service_trims_too(self):
        from sitepages.services.content import update_text_slot

        slot = PageTextSlotFactory(key="headline", value="Old")
        assert update_text_slot(slot.page, "headline", user=None, data={"value": "  \t "}).value == ""

    def test_missing_value_is_a_validation_error(self, client):
        slot = PageTextSlotFactory(key="headline")
        response = client.patch(slot_url(slot.page, "text", "headline"), {}, format="json")
        assert response.status_code == 400 and "value" in response.json()["errors"]


class TestImageSlots:
    def test_replace_with_a_public_image_then_reset(self, client):
        slot = PageImageSlotFactory(key="hero")
        asset = MediaAssetFactory(alternative_text="Rooftop")
        response = client.patch(slot_url(slot.page, "image", "hero"), {"asset": str(asset.uid), "alt": "Installers", "expected_version": 1}, format="json")
        assert response.status_code == 200
        body = response.json()
        assert body["asset"]["uid"] == str(asset.uid) and body["effective_alt"] == "Installers" and body["version"] == 2
        entry = AuditLog.objects.get(action="sitepages.image_slot_updated")
        assert entry.after == {"asset": str(asset.uid), "alt": "Installers"}
        reset = client.patch(slot_url(slot.page, "image", "hero"), {"asset": None, "alt": ""}, format="json").json()
        assert reset["asset"] is None and reset["effective_alt"] == ""

    def test_external_url(self, client):
        slot = PageImageSlotFactory(key="hero")
        body = client.patch(slot_url(slot.page, "image", "hero"), {"external_url": "https://golden-ray.b-cdn.net/x.png"}, format="json").json()
        assert body["external_url"] == "https://golden-ray.b-cdn.net/x.png"
        assert client.patch(slot_url(slot.page, "image", "hero"), {"external_url": "ftp://x"}, format="json").status_code == 400
        # A well-formed URL in a scheme the website cannot load as an <img> is refused too (not only a malformed one).
        refused = client.patch(slot_url(slot.page, "image", "hero"), {"external_url": "ftp://files.example.com/x.png"}, format="json")
        assert refused.status_code == 400 and refused.json()["errors"]["external_url"]
        from sitepages.services.content import update_image_slot

        with pytest.raises(DomainError) as exc:
            update_image_slot(slot.page, "hero", user=None, data={"external_url": "ftps://files.example.com/x.png"})
        assert set(exc.value.errors) == {"external_url"}

    @pytest.mark.parametrize(
        "asset_kwargs,message",
        [
            ({"visibility": "PRIVATE", "cdn_url": ""}, "Must be a public file (it is shown on the website)."),
            ({"kind": "DOCUMENT", "mime_type": "application/pdf"}, "Must be an image."),
            ({"cdn_url": ""}, "Must be a public file (it is shown on the website)."),
        ],
    )
    def test_refuses_files_the_website_cannot_show(self, client, asset_kwargs, message):
        slot = PageImageSlotFactory(key="hero")
        asset = MediaAssetFactory(**asset_kwargs)
        response = client.patch(slot_url(slot.page, "image", "hero"), {"asset": str(asset.uid)}, format="json")
        assert response.status_code == 400 and response.json()["errors"]["asset"] == [message]

    def test_deleted_asset_is_rejected_and_used_asset_cannot_be_deleted(self, client, auth_client, make_user):
        slot = PageImageSlotFactory(key="hero")
        gone = MediaAssetFactory()
        gone.soft_delete()
        assert client.patch(slot_url(slot.page, "image", "hero"), {"asset": str(gone.uid)}, format="json").status_code == 400
        used = MediaAssetFactory()
        client.patch(slot_url(slot.page, "image", "hero"), {"asset": str(used.uid)}, format="json")
        media_admin = auth_client(make_user(grants={"media": "*"}))
        response = media_admin.delete(f"/api/v1/media/{used.uid}/")
        assert response.status_code == 409 and response.json()["code"] == "media_in_use"

    def test_stale_version(self, client):
        slot = PageImageSlotFactory(key="hero", version=2)
        assert client.patch(slot_url(slot.page, "image", "hero"), {"alt": "x", "expected_version": 1}, format="json").status_code == 409
        assert client.patch(slot_url(slot.page, "image", "nope"), {"alt": "x"}, format="json").status_code == 404


class TestSeo:
    def test_defaults_until_the_first_edit_which_creates_version_2(self, client):
        page = PageFactory(title="Careers")
        body = client.get(f"{URL}{page.uid}/seo/").json()
        assert body["uid"] is None and body["version"] == 1 and body["schema_type"] == "none" and body["seo_status"] == "error"
        assert not PageSeo.objects.exists()
        og = MediaAssetFactory()
        response = client.patch(
            f"{URL}{page.uid}/seo/",
            {"seo_title": "Careers at Flarize", "meta_description": "d" * 100, "og_image": str(og.uid), "schema_type": "WebPage", "schema_extra": {"inLanguage": "en-IN"}, "expected_version": 1},
            format="json",
        )
        assert response.status_code == 200
        body = response.json()
        assert body["uid"] and body["version"] == 2 and body["og_image"]["uid"] == str(og.uid) and body["seo_status"] == "ok"
        assert AuditLog.objects.filter(action__in=["sitepages.page_seo_created", "sitepages.page_seo_updated"]).count() == 2
        assert OutboxEvent.objects.filter(event_type="sitepages.page_updated").count() == 1

    def test_no_change_on_defaults_writes_nothing(self, client):
        page = PageFactory()
        assert client.patch(f"{URL}{page.uid}/seo/", {"schema_type": "none", "noindex": False}, format="json").json()["uid"] is None
        assert not PageSeo.objects.exists()

    def test_stale_version_and_validation(self, client):
        seo = PageSeoFactory(version=3)
        url = f"{URL}{seo.page.uid}/seo/"
        assert client.patch(url, {"seo_title": "x", "expected_version": 2}, format="json").status_code == 409
        assert client.patch(url, {"schema_type": "Recipe"}, format="json").status_code == 400
        assert client.patch(url, {"schema_extra": ["not", "an", "object"]}, format="json").status_code == 400
        assert client.patch(url, {"schema_extra": {"blob": "x" * 20000}}, format="json").json()["errors"]["schema_extra"]
        private = MediaAssetFactory(visibility="PRIVATE", cdn_url="")
        assert client.patch(url, {"og_image": str(private.uid)}, format="json").json()["errors"]["og_image"]
        assert client.patch(url, {"canonical_url": "not a url"}, format="json").status_code == 400

    def test_unknown_field_is_refused_by_the_service(self):
        from core.errors import DomainError
        from sitepages.services.content import update_seo

        with pytest.raises(DomainError) as exc:
            update_seo(PageFactory(), user=None, data={"route": "/x"})
        assert exc.value.errors == {"route": ["Not an editable field."]}


class TestPreview:
    def test_preview_shows_the_payload_whatever_the_status(self, client, settings):
        settings.FRONTEND_BASE_URL = "https://flarize.com"
        page = PageFactory(status="DRAFT", title="Careers", route="/career-test")
        PageTextSlotFactory(page=page, key="headline", value="Hi")
        body = client.get(f"{URL}{page.uid}/preview/").json()
        assert body["url"] == "https://flarize.com/career-test" and body["status"] == "DRAFT" and body["title"] == "Careers"
        assert body["content"]["text"] == {"headline": "Hi"} and body["content"]["seo"] is None and body["schema"] is None
        assert body["seo_status"] == "error" and body["seo_issues"]
        PageSeoFactory(page=page, schema_type="WebPage", seo_title="Join us")
        body = client.get(f"{URL}{page.uid}/preview/").json()
        assert body["title"] == "Join us" and body["schema"]["@type"] == "WebPage" and body["schema"]["url"] == "https://flarize.com/career-test"


def test_slot_models_constraints():
    from django.db import IntegrityError, transaction

    page = PageFactory()
    PageTextSlotFactory(page=page, key="dup")
    for model, values in ((PageTextSlot, {"key": "dup", "label": "x"}), (PageTextSlot, {"key": "1bad", "label": "x"}), (PageTextSlot, {"key": "ok", "label": "x", "kind": "HTML"})):
        with pytest.raises(IntegrityError), transaction.atomic():
            model.objects.create(page=page, **values)
    with pytest.raises(IntegrityError), transaction.atomic():
        PageTextSlot.objects.create(page=page, key="zero", label="x", max_length=0)
    PageImageSlotFactory(page=page, key="dup")
    with pytest.raises(IntegrityError), transaction.atomic():
        PageImageSlot.objects.create(page=page, key="dup", label="x")
    with pytest.raises(IntegrityError), transaction.atomic():
        PageSeo.objects.create(page=PageFactory(), schema_type="Recipe")
