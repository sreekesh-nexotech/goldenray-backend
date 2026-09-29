"""Regression tests for defects found in the adversarial review of the site-inspections package."""

import copy
import json
import uuid
from pathlib import Path

import pytest

from accounts.services.seeds import seed_roles
from core.errors import Conflict, PermissionDenied
from media.models import MediaAsset
from site_inspections.models import Inspection, LocationApproval, Photo
from site_inspections.models.choices import ApprovalStatus, Status
from site_inspections.services import annotations, approvals, inspections, legacy_import, lifecycle, linking, photos, report
from site_inspections.tests.factories import InspectionFactory, add_photo, approved, completed_fields

pytestmark = pytest.mark.django_db
BASE = "/api/v1/site-inspections/"
TABLES = json.loads((Path(__file__).parent / "fixtures" / "legacy_si.json").read_text())


@pytest.fixture
def completed(engineer):
    inspection = InspectionFactory(engineer=engineer, status=Status.IN_PROGRESS)
    completed_fields(inspection, engineer)
    return lifecycle.submit(inspection, user=engineer)


class TestApprovalPhoneIsTheCustomers:
    """The one-time code proves the *customer*: an engineer must not be able to route it to a phone of their choice."""

    def test_engineer_cannot_send_the_code_to_another_phone(self, auth_client, engineer, completed):
        response = auth_client(engineer).post(f"{BASE}{completed.uid}/approval/request/", {"customer_phone": "+919812345678"}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "phone_not_customer"
        assert not LocationApproval.objects.filter(inspection=completed).exists()

    def test_engineer_cannot_fall_back_to_the_phone_they_typed_in(self, engineer, completed):
        completed.customer.__class__.objects.filter(pk=completed.customer_id).update(phone_e164="")
        Inspection.objects.filter(pk=completed.pk).update(registered_phone_e164="+919812345678")
        with pytest.raises(Exception) as error:
            approvals.request_approval(Inspection.objects.get(pk=completed.pk), user=engineer)
        assert error.value.code == "phone_required"

    def test_the_customers_second_number_is_allowed(self, engineer, completed):
        completed.customer.__class__.objects.filter(pk=completed.customer_id).update(alt_phone="98123 45678")
        approval, _ = approvals.request_approval(Inspection.objects.get(pk=completed.pk), user=engineer, customer_phone_e164="+919812345678")
        assert approval.customer_phone_e164 == "+919812345678"

    def test_project_head_may_choose_another_phone(self, head, completed):
        approval, _ = approvals.request_approval(completed, user=head, customer_phone_e164="+919812345678")
        assert approval.customer_phone_e164 == "+919812345678"

    def test_default_is_the_customers_phone(self, engineer, completed):
        approval, _ = approvals.request_approval(completed, user=engineer)
        assert approval.customer_phone_e164 == completed.customer.phone_e164


class TestOnHoldKeepsTheApprovedProtection:
    def test_an_approved_inspection_on_hold_cannot_be_archived(self, engineer, head, admin):
        inspection = approved(InspectionFactory(engineer=engineer, status=Status.IN_PROGRESS), engineer)
        held = lifecycle.hold(inspection, user=head, reason="waiting for the customer")
        with pytest.raises(Conflict) as error:
            inspections.archive(held, user=admin)
        assert error.value.code == "inspection_read_only"
        assert Inspection.objects.filter(pk=inspection.pk).exists()

    def test_agreement_does_not_convert_an_approved_inspection_on_hold(self, engineer, head):
        inspection = approved(InspectionFactory(engineer=engineer, status=Status.IN_PROGRESS), engineer)
        lifecycle.hold(inspection, user=head, reason="waiting")
        body = {"agreement_uid": str(uuid.uuid4()), "number": "AGR-1", "customer_uid": str(inspection.customer.uid), "system_type": "ON_GRID", "fields": {}}
        created = linking.apply_agreement(body)
        assert created.pk != inspection.pk
        assert Inspection.objects.get(pk=inspection.pk).agreement_uid is None

    def test_system_type_change_on_hold_after_completion_sends_back_on_resume(self, engineer, head):
        inspection = approved(InspectionFactory(engineer=engineer, status=Status.IN_PROGRESS), engineer)
        first = {"agreement_uid": str(uuid.uuid4()), "number": "AGR-1", "customer_uid": str(inspection.customer.uid), "system_type": "ON_GRID", "fields": {}}
        Inspection.objects.filter(pk=inspection.pk).update(origin="AGREEMENT", agreement_uid=first["agreement_uid"], agreement_version=1)
        inspection.refresh_from_db()
        lifecycle.hold(inspection, user=head, reason="waiting")
        second = {**first, "agreement_uid": str(uuid.uuid4()), "number": "AGR-2", "system_type": "HYBRID", "supersedes_uid": first["agreement_uid"]}
        linking.apply_agreement(second)
        row = Inspection.objects.get(pk=inspection.pk)
        assert row.status == Status.ON_HOLD and row.held_from_status == Status.REVISION_REQUIRED
        assert not LocationApproval.objects.filter(inspection=row, status=ApprovalStatus.APPROVED).exists()
        assert lifecycle.resume(row, user=head).status == Status.REVISION_REQUIRED


class TestApprovalEvidenceIsKept:
    def test_deleting_a_photo_the_customer_approved_keeps_its_file(self, engineer, head):
        inspection = approved(InspectionFactory(engineer=engineer, status=Status.IN_PROGRESS), engineer)
        old = Inspection.objects.get(pk=inspection.pk).panel_photo
        lifecycle.revision(inspection, user=head, reason="move the panels")
        inspection = Inspection.objects.get(pk=inspection.pk)
        new = add_photo(inspection, engineer, "PANEL_AREA")
        annotations.save_annotation(inspection, user=engineer, annotation_type="PANEL_AREA", photo_uid=new.uid, geometry={"x": 0.2, "y": 0.2, "w": 0.3, "h": 0.3}, width_m=5, height_m=3)
        photos.delete_photo(Photo.objects.get(pk=old.pk), user=engineer)
        assert Photo.all_objects.get(pk=old.pk).deleted_at is not None
        assert MediaAsset.objects.filter(pk=old.asset_id).exists(), "the photo in an approval's location snapshot must stay on file"

    def test_an_unreferenced_photo_still_loses_its_file(self, engineer):
        inspection = InspectionFactory(engineer=engineer, status=Status.IN_PROGRESS)
        photo = add_photo(inspection, engineer, "ROOF")
        photos.delete_photo(photo, user=engineer)
        assert not MediaAsset.objects.filter(pk=photo.asset_id).exists()


class TestLegacyImportRobustness:
    def test_non_numeric_coordinates_do_not_abort_the_import(self):
        seed_roles()
        tables = copy.deepcopy(TABLES)
        row = tables["site_inspections"][0]
        row["latitude"], row["longitude"] = "", "n/a"
        result = legacy_import.import_all(tables)
        assert result["site_inspections"]["created"] >= 1
        inspection = Inspection.objects.get(number=row["site_visit_no"])
        assert inspection.latitude is None and inspection.longitude is None


class TestReportImageBudget:
    def test_budget_counts_the_embedded_base64_size(self, monkeypatch):
        """The documents payload cap is 1 MB of JSON: the budget must count the base64 text, not the raw bytes."""

        class Storage:
            def read(self, key):
                return b"x" * 600_000

        monkeypatch.setattr(report, "storage_for", lambda visibility: Storage())

        class Asset:
            visibility = "PRIVATE"
            thumbnail_key = "t.webp"

        class FakePhoto:
            asset = Asset()

        images = report._Images()
        uri = images.data_uri(FakePhoto())
        assert uri is None or len(uri) <= report.IMAGE_BUDGET


def test_engineer_cannot_self_approve_end_to_end(auth_client, api_client, engineer, completed):
    """Regression for the phone override: the engineer's link can only reach the customer's phone."""
    with pytest.raises(PermissionDenied):
        approvals.request_approval(completed, user=engineer, customer_phone_e164="+919800000001")
