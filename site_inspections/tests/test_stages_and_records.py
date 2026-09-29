"""Stage allow-lists, the engineer write window, photos, annotations, observations (API + services)."""

import pytest
from django.db import IntegrityError, transaction

from site_inspections.models import Annotation, EngineeringReview, Inspection, Photo
from site_inspections.models.choices import Status
from site_inspections.tests.factories import InspectionFactory, add_photo, jpeg_file

pytestmark = pytest.mark.django_db
BASE = "/api/v1/site-inspections/"


@pytest.fixture
def mine(engineer):
    return InspectionFactory(engineer=engineer)


class TestStages:
    def test_stage_save_starts_the_work_and_writes_only_its_columns(self, auth_client, engineer, mine):
        client = auth_client(engineer)
        response = client.patch(f"{BASE}{mine.uid}/stages/site-access/", {"latitude": "9.9312", "longitude": "76.2673", "road_access": "GOOD"}, format="json")
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["status"] == "IN_PROGRESS" and body["road_access"] == "GOOD"
        assert body["google_map_link"] == "https://www.google.com/maps?q=9.9312,76.2673"

    def test_fields_of_other_stages_and_protected_columns_are_refused(self, auth_client, engineer, mine):
        client = auth_client(engineer)
        for body in ({"status": "APPROVED"}, {"system_type": "HYBRID"}, {"roof_type": "TILE"}, {"quoted_size_kw": "5"}, {"engineer": None}):
            response = client.patch(f"{BASE}{mine.uid}/stages/site-access/", body, format="json")
            assert response.status_code == 400 and response.json()["errors"] == {next(iter(body)): ["Not writable in this stage."]}
        mine.refresh_from_db()
        assert mine.status == Status.DRAFT and mine.system_type == "UNDECIDED" and mine.roof_type == "" and mine.quoted_size_kw is None

    def test_service_refuses_foreign_fields(self, engineer, mine):
        from core.errors import DomainError
        from site_inspections.services import inspections

        with pytest.raises(DomainError) as error:
            inspections.update_stage(mine, user=engineer, stage_key="shading", data={"status": "APPROVED"})
        assert error.value.code == "field_not_allowed"
        with pytest.raises(DomainError) as error:
            inspections.update_stage(mine, user=engineer, stage_key="evidence", data={})
        assert error.value.code == "unknown_stage"

    def test_validation_and_stale_version(self, auth_client, engineer, mine):
        client = auth_client(engineer)
        response = client.patch(f"{BASE}{mine.uid}/stages/shading/", {"shading_pct": 140, "morning_shading": "BRIGHT"}, format="json")
        assert response.status_code == 400 and {"shading_pct", "morning_shading"} <= set(response.json()["errors"])
        response = client.patch(f"{BASE}{mine.uid}/stages/shading/", {"shading_pct": 10, "expected_version": 99}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"

    def test_phone_is_normalised(self, auth_client, engineer, mine):
        body = auth_client(engineer).patch(f"{BASE}{mine.uid}/stages/electrical/", {"registered_phone_e164": "98765 43210", "neutral_link": "NOT_AVAILABLE"}, format="json").json()
        assert body["registered_phone_e164"] == "+919876543210" and body["neutral_link"] == "NOT_AVAILABLE"

    def test_permissions_and_scope(self, auth_client, api_client, engineer, head):
        other = InspectionFactory()
        assert api_client.patch(f"{BASE}{other.uid}/stages/shading/", {}, format="json").status_code == 401
        assert auth_client(head).patch(f"{BASE}{other.uid}/stages/shading/", {}, format="json").status_code == 403
        assert auth_client(engineer).patch(f"{BASE}{other.uid}/stages/shading/", {}, format="json").status_code == 404

    def test_read_only_after_approval_and_locked_after_submit(self, auth_client, engineer):
        client = auth_client(engineer)
        for status, code in ((Status.APPROVED, "inspection_read_only"), (Status.COMPLETED, "invalid_status"), (Status.ON_HOLD, "invalid_status")):
            inspection = InspectionFactory(engineer=engineer, status=status, on_hold_reason="x" if status == Status.ON_HOLD else "", held_from_status="DRAFT" if status == Status.ON_HOLD else "")
            response = client.patch(f"{BASE}{inspection.uid}/stages/shading/", {"shading_pct": 5}, format="json")
            assert response.status_code == 409 and response.json()["code"] == code

    def test_start_needs_an_engineer(self, auth_client, admin):
        inspection = InspectionFactory()
        response = auth_client(admin).patch(f"{BASE}{inspection.uid}/stages/shading/", {"shading_pct": 5}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "engineer_required"

    def test_conditions_open_engineering_reviews(self, auth_client, engineer, mine):
        client = auth_client(engineer)
        client.patch(f"{BASE}{mine.uid}/stages/roof-structure/", {"roof_condition": "REVIEW_REQUIRED"}, format="json")
        client.patch(f"{BASE}{mine.uid}/stages/shading/", {"generation_impact": "REQUIRES_REVIEW"}, format="json")
        reviews = EngineeringReview.objects.filter(inspection=mine)
        assert reviews.count() == 1 and reviews.get().trigger == "ROOF" and "[GENERATION]" in reviews.get().reason

    def test_engineer_cannot_clear_a_pending_review_by_choosing_routine(self, auth_client, engineer, mine):
        client = auth_client(engineer)
        client.patch(f"{BASE}{mine.uid}/stages/additional-work/", {"complexity_status": "ENGINEERING_REVIEW_REQUIRED", "complexity_reason": "Old roof"}, format="json")
        assert EngineeringReview.objects.get(inspection=mine).trigger == "MANUAL"
        response = client.patch(f"{BASE}{mine.uid}/stages/additional-work/", {"complexity_status": "ROUTINE"}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "review_pending"

    def test_start_action(self, auth_client, engineer, mine):
        client = auth_client(engineer)
        assert client.post(f"{BASE}{mine.uid}/start/", {}, format="json").json()["status"] == "IN_PROGRESS"
        assert client.post(f"{BASE}{mine.uid}/start/", {}, format="json").json()["status"] == "IN_PROGRESS"


class TestPhotos:
    def test_upload_list_and_signed_urls(self, auth_client, engineer, mine, django_assert_max_num_queries):
        client = auth_client(engineer)
        response = client.post(f"{BASE}{mine.uid}/photos/", {"file": jpeg_file(), "photo_type": "ROOF", "stage": 2}, format="multipart")
        assert response.status_code == 201, response.json()
        photo = response.json()
        assert photo["file"]["url"].startswith("http://testserver/api/v1/media/download/") and photo["mime_type"] == "image/jpeg"
        asset = Photo.objects.get(uid=photo["uid"]).asset
        assert asset.visibility == "PRIVATE" and asset.folder == f"site-inspections/{mine.uid}"
        for _ in range(3):
            add_photo(mine, engineer, "SHADING")
        with django_assert_max_num_queries(10):
            assert client.get(f"{BASE}{mine.uid}/photos/").json()["count"] == 4

    def test_upload_refuses_a_non_image(self, auth_client, engineer, mine):
        from site_inspections.tests.factories import NamedBytes

        response = auth_client(engineer).post(f"{BASE}{mine.uid}/photos/", {"file": NamedBytes(b"%PDF-1.4 nope", "a.jpg"), "photo_type": "ROOF"}, format="multipart")
        assert response.status_code == 400
        assert not Photo.objects.exists()

    def test_delete_is_refused_while_referenced(self, auth_client, engineer, mine):
        client = auth_client(engineer)
        photo = add_photo(mine, engineer)
        client.post(f"{BASE}{mine.uid}/annotations/", {"annotation_type": "PANEL_AREA", "photo_uid": str(photo.uid), "geometry": {"x": 0, "y": 0, "w": 0.5, "h": 0.5}}, format="json")
        response = client.delete(f"{BASE}{mine.uid}/photos/{photo.uid}/")
        assert response.status_code == 409 and response.json()["code"] == "photo_in_use"
        spare = add_photo(mine, engineer, "OTHER")
        assert client.delete(f"{BASE}{mine.uid}/photos/{spare.uid}/").status_code == 204
        assert not Photo.objects.filter(pk=spare.pk).exists() and spare.asset.__class__.all_objects.get(pk=spare.asset_id).deleted_at is not None


class TestAnnotations:
    def test_history_current_and_layout_columns(self, auth_client, engineer, mine):
        client = auth_client(engineer)
        photo = add_photo(mine, engineer)
        url = f"{BASE}{mine.uid}/annotations/"
        first = client.post(url, {"annotation_type": "PANEL_AREA", "photo_uid": str(photo.uid), "geometry": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}, "width_m": "6", "height_m": "4"}, format="json")
        assert first.status_code == 201, first.json()
        second = client.post(
            url, {"annotation_type": "PANEL_AREA", "photo_uid": str(photo.uid), "geometry": {"x": 0.2, "y": 0.1, "w": 0.5, "h": 0.5}, "width_m": "5", "height_m": "4"}, format="json"
        ).json()
        assert second["number"] == 2 and second["area_m2"] == "20.00"
        assert client.get(url).json()["count"] == 2 and client.get(url, {"current": "true"}).json()["count"] == 1
        mine.refresh_from_db()
        assert mine.panel_photo == photo and str(mine.panel_area_m2) == "20.00"

    @pytest.mark.parametrize(
        "geometry",
        [
            {"x": 0.1, "y": 0.1, "w": 0.01, "h": 0.5},
            {"x": 0.8, "y": 0.1, "w": 0.5, "h": 0.5},
            {"x": -1, "y": 0, "w": 0.1, "h": 0.1},
            {"x": 0, "y": 0, "w": 0.5},
            {"x": "a", "y": 0, "w": 0.5, "h": 0.5},
        ],
    )
    def test_invalid_geometry(self, auth_client, engineer, mine, geometry):
        photo = add_photo(mine, engineer)
        response = auth_client(engineer).post(f"{BASE}{mine.uid}/annotations/", {"annotation_type": "PANEL_AREA", "photo_uid": str(photo.uid), "geometry": geometry}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "invalid_geometry"

    def test_photo_of_another_inspection_is_refused(self, auth_client, engineer, mine):
        other = InspectionFactory(engineer=engineer)
        foreign = add_photo(other, engineer)
        response = auth_client(engineer).post(
            f"{BASE}{mine.uid}/annotations/", {"annotation_type": "PANEL_AREA", "photo_uid": str(foreign.uid), "geometry": {"x": 0, "y": 0, "w": 0.5, "h": 0.5}}, format="json"
        )
        assert response.status_code == 400 and response.json()["code"] == "photo_not_in_inspection"

    def test_composite_foreign_key_holds_in_the_database(self, engineer, mine):
        other = InspectionFactory(engineer=engineer)
        foreign = add_photo(other, engineer)
        with pytest.raises(IntegrityError), transaction.atomic():
            Annotation.objects.create(inspection=mine, photo=foreign, annotation_type="PANEL_AREA", geometry={}, number=1)
            with transaction.get_connection().cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


class TestObservations:
    def test_add_and_list(self, auth_client, engineer, mine):
        client = auth_client(engineer)
        photo = add_photo(mine, engineer, "ELECTRICAL")
        response = client.post(f"{BASE}{mine.uid}/observations/", {"note": "Earthing pit missing", "category": "EARTHING", "stage": 5, "photo_uids": [str(photo.uid)]}, format="json")
        assert response.status_code == 201 and response.json()["photo_uids"] == [str(photo.uid)]
        assert client.get(f"{BASE}{mine.uid}/observations/").json()["count"] == 1
        assert client.post(f"{BASE}{mine.uid}/observations/", {"note": ""}, format="json").status_code == 400

    def test_foreign_photo_refused(self, auth_client, engineer, mine):
        foreign = add_photo(InspectionFactory(engineer=engineer), engineer)
        response = auth_client(engineer).post(f"{BASE}{mine.uid}/observations/", {"note": "x", "photo_uids": [str(foreign.uid)]}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "photo_not_in_inspection"
        assert Inspection.objects.get(pk=mine.pk).status == Status.DRAFT
