"""Readiness, activity, snapshots, report (render job + documents access), customer timeline, the engineer boundary."""

import re

import pytest
from rest_framework import serializers as drf

from customers.tests.factories import CustomerFactory
from documents.models import RenderJob
from site_inspections.models import Inspection
from site_inspections.models.choices import Status
from site_inspections.services import lifecycle, report
from site_inspections.tests.factories import InspectionFactory, add_photo, approved

pytestmark = pytest.mark.django_db
BASE = "/api/v1/site-inspections/"


class TestReadinessActivitySnapshots:
    def test_readiness_codes(self, auth_client, engineer):
        inspection = InspectionFactory(engineer=engineer)
        body = auth_client(engineer).get(f"{BASE}{inspection.uid}/readiness/").json()
        codes = [blocker["code"] for blocker in body["blockers"]]
        assert body["ready"] is False and codes[:2] == ["PANEL_PHOTO_REQUIRED", "EQUIPMENT_PHOTO_REQUIRED"]
        assert "EQUIPMENT_SYSTEM_TYPE_UNDECIDED" in codes and body["field_completion"]["ready"] is False

    def test_ready_after_approval(self, auth_client, head, engineer):
        inspection = approved(InspectionFactory(engineer=engineer, status=Status.IN_PROGRESS), engineer)
        assert auth_client(head).get(f"{BASE}{inspection.uid}/readiness/").json()["ready"] is True

    def test_activity_is_the_audit_trail(self, auth_client, engineer, head):
        inspection = InspectionFactory(engineer=engineer)
        client = auth_client(engineer)
        client.patch(f"{BASE}{inspection.uid}/stages/shading/", {"shading_pct": 5}, format="json")
        add_photo(inspection, engineer, "ROOF")
        actions = [row["action"] for row in auth_client(head).get(f"{BASE}{inspection.uid}/activity/").json()["results"]]
        assert actions[:3] == ["site_inspections.photo_added", "site_inspections.stage_saved", "site_inspections.status_changed"]

    def test_snapshots(self, auth_client, sales):
        customer = CustomerFactory(owner=sales)
        uid = auth_client(sales).post(BASE, {"customer_uid": str(customer.uid)}, format="json").json()["uid"]
        body = auth_client(sales).get(f"{BASE}{uid}/snapshots/").json()
        assert body["count"] == 1 and body["results"][0]["data"]["customer"]["uid"] == str(customer.uid)


class TestReport:
    def test_customer_and_internal_variants(self, auth_client, head, engineer):
        inspection = approved(InspectionFactory(engineer=engineer, status=Status.IN_PROGRESS), engineer)
        client = auth_client(head)
        response = client.get(f"{BASE}{inspection.uid}/report/", {"variant": "customer"})
        assert response.status_code == 202, response.json()
        job = RenderJob.objects.get(uid=response.json()["job_uid"])
        assert job.kind == "INSPECTION_REPORT" and job.template == "customer" and job.object_type == "site_inspections.inspection"
        assert "internal" not in job.payload and job.payload["locations"][0]["image"].startswith("data:image/webp;base64,")
        assert job.payload["filename"].endswith("Site Inspection Report - 2026-09-29.pdf")
        again = client.get(f"{BASE}{inspection.uid}/report/", {"variant": "customer"}).json()
        assert again["job_uid"] == str(job.uid)
        internal = RenderJob.objects.get(uid=client.get(f"{BASE}{inspection.uid}/report/", {"variant": "internal"}).json()["job_uid"])
        assert internal.payload["internal"]["blockers"] == [] and internal.payload["equipment"][0]["checks"]
        assert client.get(f"{BASE}{inspection.uid}/report/", {"variant": "secret"}).json()["code"] == "validation_error"

    def test_templates_render(self, engineer):
        from documents.services.templates import render_html

        inspection = approved(InspectionFactory(engineer=engineer, status=Status.IN_PROGRESS), engineer)
        for variant in report.VARIANTS:
            job = RenderJob(kind="INSPECTION_REPORT", template=variant, language="en", object_uid=inspection.uid, payload=report.payload(inspection, variant))
            html = render_html(job)
            assert inspection.number in html and "Proposed Installation Layout" in html and ("Readiness blockers" in html) == (variant == "internal")

    def test_job_follows_the_inspection_scope(self, auth_client, head, engineer, make_user):
        from documents.access import ensure_can_view

        inspection = approved(InspectionFactory(engineer=engineer, status=Status.IN_PROGRESS), engineer)
        job = report.request_report(inspection, user=head, variant="customer")
        other = make_user(grants={"site_inspections": ["view"]}, scopes={"site_inspections": "assigned"})
        from core.errors import NotFound

        ensure_can_view(engineer, job)
        with pytest.raises(NotFound):
            ensure_can_view(other, job)


class TestTimeline:
    def test_entries_follow_scope(self, auth_client, admin, engineer):
        inspection = approved(InspectionFactory(engineer=engineer, status=Status.IN_PROGRESS), engineer)
        lifecycle.release(Inspection.objects.get(pk=inspection.pk), user=admin)
        body = auth_client(admin).get(f"/api/v1/customers/{inspection.customer.uid}/timeline/").json()
        kinds = {entry["kind"] for entry in body["results"]}
        assert {"site_inspections.created", "site_inspections.released"} <= kinds


COMMERCIAL = re.compile(r"price|discount|cost|margin|amount|gst|payment|tariff_rate", re.IGNORECASE)


def _field_names(serializer_class, seen=None) -> set[str]:
    seen = seen if seen is not None else set()
    if serializer_class in seen:
        return set()
    seen.add(serializer_class)
    names = set()
    instance = serializer_class()
    for name, field in instance.fields.items():
        names.add(name)
        child = getattr(field, "child", None) or field
        if isinstance(child, drf.BaseSerializer):
            names |= _field_names(type(child), seen)
    return names


def test_engineer_boundary_no_commercial_field_is_reachable():
    """Plan 2 §3.2: the port of scripts/audit-v2-engineer-boundary.mjs — walks every serializer an engineer can reach."""
    from site_inspections.serializers import common, customer, inspections, records

    classes = [
        obj
        for module in (common, customer, inspections, records)
        for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, drf.BaseSerializer) and obj.__module__.startswith("site_inspections") and not obj.__name__.startswith("_Stage")
    ]
    classes += list(inspections.STAGE_SERIALIZERS.values())
    assert len(classes) > 25
    leaks = {cls.__name__: sorted(name for name in _field_names(cls) if COMMERCIAL.search(name)) for cls in classes}
    assert {name: fields for name, fields in leaks.items() if fields} == {}
    model_fields = {field.name for field in Inspection._meta.get_fields()}
    assert not [name for name in model_fields if COMMERCIAL.search(name)]
