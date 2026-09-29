"""Factories and builders for site-inspection tests.

``InspectionFactory`` makes a bare row (DRAFT, PRE_SALE, UNDECIDED). The builders drive the real services so the
state they produce is the state the API would produce: :func:`add_photo` uploads a JPEG through the media pipeline,
:func:`completed_fields` fills everything field completion needs, :func:`ready_to_release` walks an inspection to
APPROVED with nothing blocking release.
"""

from __future__ import annotations

import datetime as dt
import io

import factory

from customers.tests.factories import CustomerFactory
from media.tests import files
from site_inspections.models import Inspection, LocationApproval
from site_inspections.models.choices import ApprovalStatus, Complexity, Status, Suitability, SystemType
from site_inspections.services import annotations, approvals, equipment, lifecycle, photos

PASS_ALL_OG = {
    check: "PASS"
    for check in (
        "direct_sunlight",
        "rain_exposure",
        "water_leakage",
        "ventilation",
        "clearance",
        "heat_source",
        "dust_chemical_exposure",
        "physical_protection",
        "maintenance_access",
        "mounting_surface",
        "cable_route",
        "cable_entry",
        "height",
        "flooding_water_risk",
    )
}


class InspectionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Inspection

    number = factory.Sequence(lambda n: f"SV-20260929-{n + 1:04d}")
    visit_date = dt.date(2026, 9, 29)
    customer = factory.SubFactory(CustomerFactory)
    address = "12 Temple Road, Kochi"
    pincode = "682001"
    district = "Ernakulam"


class NamedBytes(io.BytesIO):
    def __init__(self, data: bytes, name: str):
        super().__init__(data)
        self.name = name
        self.size = len(data)


def jpeg_file(name: str = "roof.jpg", **kwargs) -> NamedBytes:
    return NamedBytes(files.jpeg(**kwargs), name)


def add_photo(inspection: Inspection, user, photo_type: str = "PANEL_AREA", **kwargs):
    return photos.upload_photo(inspection, user=user, file=jpeg_file(), photo_type=photo_type, **kwargs)


def completed_fields(inspection: Inspection, user, *, system_type: str = SystemType.ON_GRID) -> Inspection:
    """An IN_PROGRESS inspection that passes field completion (photos, rectangles, verdict, complexity, checklist)."""
    Inspection.objects.filter(pk=inspection.pk).update(engineer=inspection.engineer or user, system_type=system_type)
    inspection.refresh_from_db()
    panel = add_photo(inspection, user, "PANEL_AREA")
    kit = add_photo(inspection, user, "EQUIPMENT_AREA")
    geometry = {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.4}
    annotations.save_annotation(inspection, user=user, annotation_type="PANEL_AREA", photo_uid=panel.uid, geometry=geometry, width_m=6, height_m=4)
    annotations.save_annotation(inspection, user=user, annotation_type="EQUIPMENT_AREA", photo_uid=kit.uid, geometry=geometry, width_m=2, height_m=1)
    if system_type == SystemType.ON_GRID:
        equipment.put_assessment(inspection, user=user, equipment_type="ON_GRID_INVERTER", data={"results": PASS_ALL_OG})
    Inspection.objects.filter(pk=inspection.pk).update(site_suitability=Suitability.SUITABLE, complexity_status=Complexity.ROUTINE)
    inspection.refresh_from_db()
    return inspection


def approved(inspection: Inspection, user) -> Inspection:
    """Completed, submitted, approval requested and answered (APPROVED) without the OTP round trip."""
    completed_fields(inspection, user)
    lifecycle.submit(inspection, user=user)
    approval, _ = approvals.request_approval(Inspection.objects.get(pk=inspection.pk), user=user)
    LocationApproval.objects.filter(pk=approval.pk).update(status=ApprovalStatus.APPROVED, otp_verified_at=dt.datetime(2026, 9, 29, tzinfo=dt.UTC))
    Inspection.objects.filter(pk=inspection.pk).update(status=Status.APPROVED)
    inspection.refresh_from_db()
    return inspection


def issued_extra_structure(inspection: Inspection):
    """A live ISSUED EXTRA_STRUCTURE agreement raised from ``inspection`` — what the agreements context's
    COST_CALCULATED validator (registered by ``AgreementsConfig.ready``) accepts."""
    from agreements.models import AgreementKind, AgreementStatus, SourceType
    from agreements.tests.factories import AgreementFactory
    from engines.frozen import sha256_hex

    payload = {"kind": "EXTRA_STRUCTURE", "source_uid": str(inspection.uid)}
    return AgreementFactory(
        kind=AgreementKind.EXTRA_STRUCTURE,
        customer=inspection.customer,
        status=AgreementStatus.ISSUED,
        source_type=SourceType.SITE_INSPECTION,
        source_uid=inspection.uid,
        number=f"AGR-X-{str(inspection.uid)[:8]}",
        payload=payload,
        payload_sha256=sha256_hex(payload),
        issued_at=dt.datetime(2026, 9, 29, tzinfo=dt.UTC),
    )
