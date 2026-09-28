"""GET installations/ (showcase map) and installations/stats/ — shape, no personal data, cache, throttle, stats rules."""

import datetime as dt
from unittest import mock

import pytest

from leads.models import CustomerInstallation
from leads.services import installations, pincode_directory
from leads.tests.factories import CustomerInstallationFactory
from media.tests.factories import MediaAssetFactory

pytestmark = pytest.mark.django_db
LIST = "/api/public/v1/installations/"
STATS = "/api/public/v1/installations/stats/"


class TestShowcaseList:
    def test_only_completed_showcase_rows_without_personal_data(self, api_client, django_assert_max_num_queries):
        photo = MediaAssetFactory(alternative_text="Roof")
        shown = CustomerInstallationFactory(is_showcase=True, photo=photo, system_type="ON_GRID")
        CustomerInstallationFactory.create_batch(3, is_showcase=True)
        CustomerInstallationFactory(is_showcase=False)
        CustomerInstallationFactory(is_showcase=True, status=CustomerInstallation.Status.PLANNED)
        CustomerInstallationFactory(is_showcase=True).soft_delete()
        with django_assert_max_num_queries(3):
            response = api_client.get(LIST)
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 4 and set(body) == {"results", "count", "next", "previous"}
        item = next(row for row in body["results"] if row["uid"] == str(shown.uid))
        assert set(item) == {"uid", "pincode", "district", "capacity_kw", "system_type", "installed_on", "photo"}
        assert item["photo"] == {"url": photo.cdn_url, "alternative_text": "Roof", "width": photo.width, "height": photo.height}
        text = response.content.decode()
        assert shown.customer_name not in text and shown.phone_e164 not in text and shown.address not in text

    def test_filters(self, api_client):
        CustomerInstallationFactory(is_showcase=True, pincode="688008", district="ALAPPUZHA")
        CustomerInstallationFactory(is_showcase=True, pincode="682016", district="ERNAKULAM")
        assert api_client.get(LIST, {"pincode": "688008"}).json()["count"] == 1
        assert api_client.get(LIST, {"district": "ernakulam"}).json()["count"] == 1
        assert api_client.get(LIST, {"pincode": "1" * 13}).status_code == 400

    def test_private_photo_is_not_published(self, api_client):
        CustomerInstallationFactory(is_showcase=True, photo=MediaAssetFactory(visibility="PRIVATE", cdn_url=""))
        assert api_client.get(LIST).json()["results"][0]["photo"] is None

    def test_cache_headers_and_invalidation(self, api_client, auth_client, make_user):
        installation = CustomerInstallationFactory(is_showcase=True)
        first = api_client.get(LIST)
        assert first["Cache-Control"] == "public, max-age=60" and first["X-Cache"] == "MISS" and first["ETag"]
        assert api_client.get(LIST)["X-Cache"] == "HIT"
        assert api_client.get(LIST, HTTP_IF_NONE_MATCH=first["ETag"]).status_code == 304
        staff = auth_client(make_user(grants={"leads": "*"}, scopes={"leads": "all"}))
        assert staff.patch(f"/api/v1/leads/installations/{installation.uid}/", {"is_showcase": False}, format="json").status_code == 200
        after = api_client.get(LIST)
        assert after["X-Cache"] == "MISS" and after.json()["count"] == 0

    def test_throttled_as_public_read(self, api_client, settings):
        from leads.views.public import InstallationStatsView, PublicInstallationViewSet

        request = type("R", (), {"method": "GET"})()
        assert PublicInstallationViewSet().get_throttle_scope(request) == InstallationStatsView().get_throttle_scope(request) == "public_read"
        settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "public_read": "2/min"}}
        assert [api_client.get(STATS, {"pincode": "682016"}).status_code for _ in range(3)] == [200, 200, 429]


class TestStats:
    def test_counts_with_the_pincode_list(self, api_client, legacy_pincodes):
        CustomerInstallationFactory(pincode="688008", installed_on=dt.date(2026, 3, 1))
        CustomerInstallationFactory(pincode="688001", installed_on=dt.date(2025, 3, 1))
        CustomerInstallationFactory(pincode="688008", status=CustomerInstallation.Status.PLANNED, installed_on=dt.date(2026, 5, 1))
        CustomerInstallationFactory(pincode="682016")
        with mock.patch("django.utils.timezone.localdate", return_value=dt.date(2026, 9, 28)):
            body = api_client.get(STATS, {"pincode": "688008"}).json()
        assert body == {"pincode": "688008", "district": "ALAPPUZHA", "pincode_installations": 1, "district_installations": 2, "current_year_installations": 1, "year": 2026}

    def test_unknown_pincode_falls_back_like_before(self, api_client, legacy_pincodes):
        assert api_client.get(STATS, {"pincode": "682999"}).json()["district"] == "Ernakulam"
        assert api_client.get(STATS, {"pincode": "110001"}).json()["district"] == "Kerala"
        assert api_client.get(STATS, {"pincode": "68"}).json()["district"] == "Kerala"

    def test_without_a_pincode_list_districts_come_from_the_rows(self, api_client):
        CustomerInstallationFactory(pincode="682016", district="Ernakulam")
        CustomerInstallationFactory(pincode="682020", district="ERNAKULAM")
        body = api_client.get(STATS, {"pincode": "682016"}).json()
        assert (body["district"], body["pincode_installations"], body["district_installations"]) == ("Ernakulam", 1, 2)

    @pytest.mark.parametrize("query", [{}, {"pincode": ""}])
    def test_pincode_is_required(self, api_client, query):
        response = api_client.get(STATS, query)
        assert response.status_code == 400 and response.json()["errors"] == {"pincode": ["Pincode parameter is required"]}

    def test_cached_and_bumped_by_writes(self, api_client):
        assert api_client.get(STATS, {"pincode": "682016"}).json()["pincode_installations"] == 0
        assert api_client.get(STATS, {"pincode": "682016"})["X-Cache"] == "HIT"
        installations.create_installation(user=None, data={"customer_name": "X", "pincode": "682016", "capacity_kw": 3, "installed_on": dt.date(2025, 1, 1)})
        assert api_client.get(STATS, {"pincode": "682016"}).json()["pincode_installations"] == 1

    def test_one_aggregate_query(self, django_assert_num_queries):
        CustomerInstallationFactory.create_batch(3)
        with django_assert_num_queries(1):
            installations.installation_stats("682016")


class TestReferenceDirectory:
    def test_without_the_reference_model_it_has_no_data(self):
        directory = pincode_directory.ReferenceDirectory()
        with mock.patch("leads.services.pincode_directory.apps.get_model", side_effect=LookupError):
            assert directory.district_for("682016") is None and directory.pincodes_in("ERNAKULAM") is None

    def test_reads_the_plan_columns_of_reference_pincode(self):
        rows = mock.MagicMock()
        rows.filter.return_value.order_by.return_value.values_list.return_value.first.return_value = "ERNAKULAM"
        rows.filter.return_value.values_list.return_value = ["682016", "682020"]
        model = mock.MagicMock(_default_manager=rows)
        model._meta.get_fields.return_value = [type("F", (), {"name": "pincode"})(), type("F", (), {"name": "district"})()]
        with mock.patch("leads.services.pincode_directory.apps.get_model", return_value=model):
            directory = pincode_directory.ReferenceDirectory()
            assert directory.district_for("682016") == "ERNAKULAM"
            assert directory.pincodes_in("ERNAKULAM") == {"682016", "682020"}
        model._meta.get_fields.return_value = []
        with mock.patch("leads.services.pincode_directory.apps.get_model", return_value=model):
            assert pincode_directory.ReferenceDirectory().district_for("682016") is None
