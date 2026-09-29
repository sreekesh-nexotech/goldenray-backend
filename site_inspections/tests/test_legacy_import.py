"""Site Inspection V2 SQLite rows (``fixtures/legacy_si.json``, built from the legacy schema) → platform tables."""

import json
from pathlib import Path

import pytest

from accounts.services.seeds import seed_roles
from customers.models import Customer
from customers.tests.factories import CustomerFactory
from engines import inspection_readiness as ir
from site_inspections.models import AdditionalWorkItem, Annotation, EquipmentAssessment, Inspection, LocationApproval, Observation, Photo, Snapshot
from site_inspections.services import common, legacy_import

pytestmark = pytest.mark.django_db
TABLES = json.loads((Path(__file__).parent / "fixtures" / "legacy_si.json").read_text())


@pytest.fixture
def imported():
    seed_roles()
    CustomerFactory(phone_e164="+919847000303", name="Chitra M (existing)")
    return legacy_import.import_all(TABLES)


def codes(report, table):
    return [v["code"] for v in report[table]["violations"]]


def test_counts_and_violations(imported):
    assert imported["engineers"]["created"] == 2 and codes(imported, "engineers") == ["email_placeholder", "email_placeholder"]
    assert imported["customers"]["created"] == 2 and set(codes(imported, "customers")) == {"matched_by_phone", "unparsable_phone"}
    assert imported["site_inspections"]["created"] == 3
    assert {"commercial_not_imported", "unparsable_phone", "invalid_enum", "out_of_range"} <= set(codes(imported, "site_inspections"))
    assert imported["site_inspection_photos"]["created"] == 3 and codes(imported, "site_inspection_photos") == ["invalid_data_url"]
    assert imported["site_inspection_annotations"]["created"] == 2
    recomputed = [v["message"] for v in imported["site_inspection_equipment_assessments"]["violations"] if v["code"] == "status_recomputed"]
    assert recomputed == ["ON_GRID_INVERTER: legacy PASS → IN_PROGRESS (full check definition)."]
    assert codes(imported, "site_inspection_approvals") == ["approval_expired"]


def test_inspection_columns_are_coerced(imported):
    a = Inspection.objects.get(number="SV-20260610-0001")
    assert a.origin == "AGREEMENT" and a.agreement_uid == legacy_import.agreement_uid("agr-1001") and a.agreement_version == 2
    assert a.status == "INSTALLATION_READY" and a.released_at is not None and a.system_type == "HYBRID"
    assert a.vehicle_type == "TRUCK" and a.phase == "3P" and str(a.connected_load_kw) == "7.50" and a.site_suitability == "SUITABLE"
    assert a.walkway_required is False and a.wheeling_required is False and a.registered_phone_e164 == "+919847000101"
    assert a.panel_photo.photo_type == "PANEL_AREA" and a.equipment_photo.photo_type == "EQUIPMENT_AREA"
    assert set(AdditionalWorkItem.objects.filter(inspection=a).values_list("work_type", "status")) == {
        ("UNDERGROUND_CABLING", "APPROVED"),
        ("EXTRA_DC_CABLE", "APPROVED"),
        ("ADDITIONAL_EARTHING", "APPROVED"),
    }
    assert Snapshot.objects.get(inspection=a).data["legacy"]["system"]["capacityKw"] == 5
    b = Inspection.objects.get(number="SV-20260611-0001")
    assert b.site_suitability == "CONDITIONAL" and b.neutral_link == "NEEDS_MODIFICATION" and b.termination_point == "NEEDS_MODIFICATION"
    assert b.vehicle_type == "PICKUP_VAN" and b.phase == "1P" and b.wheeling_required is True and b.registered_phone_e164 == "" and b.has_location_restrictions is True
    assert b.system_type == "UNDECIDED" and b.site_structure_type == "REQUIRES_ENGINEERING_REVIEW"
    c = Inspection.objects.get(number="SV-20260612-0001")
    assert c.status == "ON_HOLD" and c.held_from_status == "DRAFT" and c.roof_type == "" and c.shading_pct is None
    assert c.customer.name == "Chitra M (existing)" and c.engineer.is_active is False
    assert set(AdditionalWorkItem.objects.filter(inspection=c).values_list("work_type", flat=True)) == {"WALKWAY", "LADDER", "OTHER"}


def test_media_annotations_equipment_approvals(imported):
    a = Inspection.objects.get(number="SV-20260610-0001")
    assert {p.asset.visibility for p in Photo.objects.filter(inspection=a)} == {"PRIVATE"}
    assert set(Annotation.objects.filter(inspection=a).values_list("geometry_space", flat=True)) == {"LEGACY_CONTAINER"}
    battery = EquipmentAssessment.objects.get(inspection=a, equipment_type="HYBRID_BATTERY")
    assert battery.status == "FAIL" and battery.review_status == "WAIVED" and battery.evidence_photo.photo_type == "CABLE_ROUTE"
    approval = LocationApproval.objects.get(inspection=a)
    assert approval.status == "APPROVED" and approval.otp_verified_at is None and approval.location_snapshot is None
    assert list(Observation.objects.get(inspection=a).photos.values_list("photo_type", flat=True)) == ["CABLE_ROUTE"]
    readiness = ir.evaluate(common.build_state(a))
    assert {"LOCATIONS_NOT_DOCUMENTED", "CUSTOMER_APPROVAL_OUTDATED"} <= set(readiness.codes)


def test_rerun_is_idempotent_and_sequence_continues(imported):
    again = legacy_import.import_all(TABLES)
    assert all(report["created"] == 0 for report in again.values())
    assert Inspection.objects.count() == 3 and Photo.objects.count() == 3 and Customer.objects.filter(source="SI_IMPORT").count() == 2
    from django.db import transaction

    from core.sequences import next_number

    with transaction.atomic():
        assert next_number("SV", period_key="20260610") == "SV-20260610-0002"


def test_dry_run_writes_nothing():
    seed_roles()
    report = legacy_import.import_all(TABLES, dry_run=True)
    assert report["customers"]["created"] == 3 and not Customer.objects.filter(source="SI_IMPORT").exists()


def test_rows_without_their_parents_are_reported():
    report = legacy_import.import_inspections(TABLES["site_inspections"][:1])
    assert report["skipped"] == 1 and report["violations"][0]["code"] == "customer_unmapped"
    assert legacy_import.import_photos(TABLES["site_inspection_photos"][:1])["violations"][0]["code"] == "inspection_unmapped"
    assert legacy_import.import_engineers(TABLES["engineers"][:1])["violations"][0]["code"] == "role_missing"
