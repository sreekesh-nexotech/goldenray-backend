"""``/api/v1/projects/`` — CRUD and the lifecycle actions."""

from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from audit.models import AuditLog
from core.models import OutboxEvent
from customers.tests.factories import CustomerFactory
from engineering.models import Finding, Run
from engineering.services import runs
from projects.models import Project
from projects.tests.conftest import ACKS
from projects.tests.factories import ProjectFactory

pytestmark = pytest.mark.django_db
BASE = "/api/v1/projects/"


@pytest.fixture
def head_user(make_user):
    return make_user(grants={"projects": "*", "pricing_internal": ["view"]})


@pytest.fixture
def head(auth_client, head_user):
    return auth_client(head_user)


@pytest.fixture
def viewer(auth_client, make_user):
    return auth_client(make_user(grants={"projects": ["view"]}))


@pytest.fixture
def editor(auth_client, make_user):
    """projects.edit without pricing_internal."""
    return auth_client(make_user(grants={"projects": ["view", "edit"]}))


@pytest.fixture
def customer(db):
    return CustomerFactory()


def _lock(client, project, lines, acks=(), **extra):
    return client.post(f"{BASE}{project.uid}/lock-bom/", {"architecture": "DEYE", "lines": lines, "acknowledgements": list(acks), **extra}, format="json")


class TestAccess:
    def test_anonymous_401(self, api_client):
        assert api_client.get(BASE).status_code == 401
        assert api_client.post(BASE, {}, format="json").status_code == 401

    def test_missing_permission_403(self, auth_client, make_user, viewer, customer):
        other = auth_client(make_user(grants={"customers": ["view"]}))
        assert other.get(BASE).status_code == 403
        project = ProjectFactory()
        assert viewer.post(BASE, {"customer_uid": str(customer.uid)}, format="json").status_code == 403
        assert viewer.patch(f"{BASE}{project.uid}/", {"title": "x"}, format="json").status_code == 403
        assert viewer.post(f"{BASE}{project.uid}/lock-bom/", {}, format="json").status_code == 403
        assert viewer.post(f"{BASE}{project.uid}/cancel/", {"reason": "x"}, format="json").status_code == 403
        assert viewer.delete(f"{BASE}{project.uid}/").status_code == 403

    def test_scope_is_all(self, viewer):
        """``projects`` only allows the ``all`` scope (PLAN §3.2): every viewer sees every live project, never deleted ones."""
        ProjectFactory.create_batch(2)
        gone = ProjectFactory()
        gone.soft_delete()
        body = viewer.get(BASE).json()
        assert body["count"] == 2
        assert viewer.get(f"{BASE}{gone.uid}/").status_code == 404


class TestList:
    def test_list_filters_and_queries(self, viewer, django_assert_max_num_queries):
        planned = ProjectFactory.create_batch(3)
        cancelled = ProjectFactory(status="CANCELLED", cancelled_at=timezone.now(), cancel_reason="lost")
        with django_assert_max_num_queries(8):
            body = viewer.get(BASE).json()
        assert body["count"] == 4
        row = body["results"][0]
        assert "bom_lock" not in row and "cost_inputs" not in row and row["customer"]["code"]
        assert viewer.get(BASE, {"status": "CANCELLED"}).json()["results"][0]["uid"] == str(cancelled.uid)
        assert viewer.get(BASE, {"customer": str(planned[0].customer.uid)}).json()["count"] == 1
        assert viewer.get(BASE, {"bom_locked": "true"}).json()["count"] == 0
        assert viewer.get(BASE, {"search": planned[1].number}).json()["count"] == 1
        assert viewer.get(BASE, {"status": "DONE"}).status_code == 400


class TestCreateUpdateDelete:
    def test_create(self, head, head_user, customer):
        inspection = "11111111-2222-3333-4444-555555555555"
        response = head.post(
            BASE,
            {
                "customer_uid": str(customer.uid),
                "head_uid": str(head_user.uid),
                "title": "Roof A",
                "system_type": "ON_GRID",
                "tier": "VALUE",
                "size_kw": "5",
                "phase": "1P",
                "site_inspection_uid": inspection,
            },
            format="json",
        )
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["number"].startswith("PROJ-") and body["status"] == "PLANNED" and body["head"]["uid"] == str(head_user.uid)
        assert body["bom_locked"] is False and body["cost_inputs"] == {} and body["size_kw"] == "5.00"
        assert AuditLog.objects.filter(action="projects.project_created").exists()
        assert OutboxEvent.objects.filter(event_type="projects.created").exists()
        again = head.post(BASE, {"customer_uid": str(customer.uid), "site_inspection_uid": inspection}, format="json")
        assert again.status_code == 409 and again.json()["code"] == "inspection_has_project"

    def test_create_validation(self, head):
        response = head.post(BASE, {"customer_uid": "00000000-0000-0000-0000-000000000000", "phase": "2P"}, format="json")
        assert response.status_code == 400
        body = response.json()
        assert body["code"] == "validation_error" and {"customer_uid", "phase"} <= set(body["errors"])

    def test_update_and_stale(self, head):
        project = ProjectFactory()
        stale = head.patch(f"{BASE}{project.uid}/", {"title": "New", "expected_version": 9}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        response = head.patch(f"{BASE}{project.uid}/", {"title": "New", "kseb_status": "APPLIED", "expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["title"] == "New" and response.json()["version"] == 2

    def test_delete_rules(self, head):
        planned = ProjectFactory()
        assert head.delete(f"{BASE}{planned.uid}/").status_code == 204
        assert Project.all_objects.get(pk=planned.pk).deleted_at is not None
        started = ProjectFactory(status="IN_PROGRESS", bom_lock={"lines": []}, bom_locked_at=timezone.now())
        response = head.delete(f"{BASE}{started.uid}/")
        assert response.status_code == 409 and response.json()["code"] == "project_not_deletable"


class TestLockBom:
    def test_blocked_then_waived(self, head, checker, short_lines):
        project = ProjectFactory()
        refused = _lock(head, project, short_lines, ACKS)
        assert refused.status_code == 409
        body = refused.json()
        assert body["code"] == "bom_lock_refused" and len(body["errors"]["blocked"]) == 3 and "missing_acknowledgements" not in body["errors"]
        run = Run.objects.get(uid=body["errors"]["engineering_run"][0])
        assert run.subject_type == "PROJECT_BOM" and run.subject_uid == project.uid and run.result == "FAIL"
        project.refresh_from_db()
        assert project.status == "PLANNED" and project.bom_lock is None
        assert AuditLog.objects.filter(action="projects.bom_lock_refused", object_uid=project.uid).exists()
        for finding in Finding.objects.filter(run=run, severity="BLOCK"):
            runs.acknowledge(finding, user=None, reason="Structure and isolator supplied by the customer")
        locked = _lock(head, project, short_lines, ACKS)
        assert locked.status_code == 200, locked.json()
        snapshot = locked.json()["bom_lock"]
        assert locked.json()["status"] == "IN_PROGRESS"
        # PBC-K-001 fires twice with one identity (no components) but two messages: each waived finding is listed.
        assert sum(ack["waiver"] for ack in snapshot["acknowledgements"]) == 3 and len(snapshot["acknowledgements"]) == 6

    def test_missing_acknowledgements_then_locked(self, head, checker, full_lines, components):
        project = ProjectFactory()
        refused = _lock(head, project, full_lines)
        assert refused.status_code == 409 and len(refused.json()["errors"]["missing_acknowledgements"]) == 3
        response = _lock(head, project, full_lines, ACKS, expected_version=1)
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["status"] == "IN_PROGRESS" and body["bom_locked"] and body["engineering_run_uid"]
        snapshot = body["bom_lock"]
        assert snapshot["engineering"]["result"] == "WARN" and snapshot["price_release_number"] == 7
        panel = next(line for line in snapshot["lines"] if line["sku"] == "p1")
        assert panel["unit_list_price"] == "15000.00" and panel["unit_landed_cost"] == "11000.00" and panel["quantity"] == "6.000"
        assert {ack["rule_code"] for ack in snapshot["acknowledgements"]} == {"PBC-D-001", "PBC-D-003", "PBC-N-001"}
        assert OutboxEvent.objects.filter(event_type="projects.bom_locked").exists()
        again = _lock(head, project, full_lines, ACKS)
        assert again.status_code == 409 and again.json()["code"] == "bom_already_locked"

    def test_acknowledgements_carry_over(self, head, checker, full_lines):
        project = ProjectFactory()
        assert _lock(head, project, full_lines[:2], ACKS).status_code == 409  # blocked, but the warnings are not acknowledged yet
        run = Run.objects.get(subject_uid=project.uid)
        for finding in Finding.objects.filter(run=run):
            runs.acknowledge(finding, user=None, reason="engineering review")
        assert _lock(head, project, full_lines[:2]).status_code == 200

    def test_validation_and_stale(self, head, checker, full_lines):
        project = ProjectFactory()
        assert _lock(head, project, []).json()["code"] == "validation_error"
        bad = _lock(head, project, [{**full_lines[0], "component_uid": "00000000-0000-0000-0000-000000000000"}])
        assert bad.status_code == 400 and "lines[0].component_uid" in bad.json()["errors"]
        assert _lock(head, project, [{**full_lines[0], "role": "ROOF"}]).status_code == 400
        stale = _lock(head, project, full_lines, ACKS, expected_version=4)
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"

    def test_system_fields_frozen_after_lock(self, head, checker, full_lines):
        project = ProjectFactory()
        assert _lock(head, project, full_lines, ACKS).status_code == 200
        response = head.patch(f"{BASE}{project.uid}/", {"size_kw": "5"}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "bom_locked"
        assert head.patch(f"{BASE}{project.uid}/", {"scheduled_on": "2026-10-10"}, format="json").status_code == 200


class TestCostInputs:
    PAYLOAD = {"distance_km": "30", "installation_type": "FLAT", "vehicle_type": "ACE", "size_key": "5sp", "special_works": [{"label": "Earthing pit", "amount": "2500"}]}

    def test_needs_pricing_internal(self, editor):
        project = ProjectFactory()
        response = editor.post(f"{BASE}{project.uid}/cost-inputs/", self.PAYLOAD, format="json")
        assert response.status_code == 403

    def test_set_and_redacted(self, head, viewer):
        project = ProjectFactory(bom_lock=None)
        response = head.post(f"{BASE}{project.uid}/cost-inputs/", {**self.PAYLOAD, "expected_version": 1}, format="json")
        assert response.status_code == 200, response.json()
        inputs = response.json()["cost_inputs"]
        assert inputs["distance_km"] == "30.00" and inputs["special_works"][0]["amount"] == "2500.00" and inputs["rate_effective_at"] is None
        assert viewer.get(f"{BASE}{project.uid}/").json()["cost_inputs"] is None
        invalid = head.post(f"{BASE}{project.uid}/cost-inputs/", {"distance_km": "-1", "installation_type": "ROOF"}, format="json")
        assert invalid.status_code == 400 and {"distance_km", "installation_type"} <= set(invalid.json()["errors"])
        stale = head.post(f"{BASE}{project.uid}/cost-inputs/", {**self.PAYLOAD, "expected_version": 1}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"

    def test_closed_project_refused(self, head):
        project = ProjectFactory(status="CANCELLED", cancelled_at=timezone.now(), cancel_reason="lost")
        response = head.post(f"{BASE}{project.uid}/cost-inputs/", self.PAYLOAD, format="json")
        assert response.status_code == 409 and response.json()["code"] == "project_not_open"

    def test_landed_cost_redacted_for_viewer(self, head, viewer, checker, full_lines):
        project = ProjectFactory()
        assert _lock(head, project, full_lines, ACKS).status_code == 200
        lines = viewer.get(f"{BASE}{project.uid}/").json()["bom_lock"]["lines"]
        assert all(line["unit_landed_cost"] is None for line in lines) and lines[0]["unit_list_price"] == "15000.00"


class TestLifecycle:
    def test_commission_close(self, head, checker, full_lines):
        project = ProjectFactory()
        early = head.post(f"{BASE}{project.uid}/commission/", {}, format="json")
        assert early.status_code == 409 and early.json()["code"] == "invalid_transition"
        assert _lock(head, project, full_lines, ACKS).status_code == 200
        future = head.post(f"{BASE}{project.uid}/commission/", {"commissioned_on": (timezone.localdate() + dt.timedelta(days=2)).isoformat()}, format="json")
        assert future.status_code == 400 and "commissioned_on" in future.json()["errors"]
        before_lock = head.post(f"{BASE}{project.uid}/commission/", {"commissioned_on": "2020-01-01"}, format="json")
        assert before_lock.status_code == 400
        assert head.post(f"{BASE}{project.uid}/close/", {}, format="json").json()["code"] == "invalid_transition"
        done = head.post(f"{BASE}{project.uid}/commission/", {"kseb_status": "SYNCHRONISED", "expected_version": 2}, format="json")
        assert done.status_code == 200 and done.json()["status"] == "COMMISSIONED" and done.json()["kseb_status"] == "SYNCHRONISED"
        closed = head.post(f"{BASE}{project.uid}/close/", {"note": "Handed over"}, format="json")
        assert closed.status_code == 200 and closed.json()["status"] == "CLOSED" and closed.json()["closed_at"]
        finished = head.patch(f"{BASE}{project.uid}/", {"title": "x"}, format="json")
        assert finished.status_code == 409 and finished.json()["code"] == "project_finished"
        assert head.post(f"{BASE}{project.uid}/cancel/", {"reason": "x"}, format="json").json()["code"] == "invalid_transition"
        assert [e.event_type for e in OutboxEvent.objects.filter(aggregate_uid=project.uid).order_by("id")][-2:] == ["projects.commissioned", "projects.closed"]

    def test_cancel(self, head):
        project = ProjectFactory()
        assert head.post(f"{BASE}{project.uid}/cancel/", {}, format="json").status_code == 400
        stale = head.post(f"{BASE}{project.uid}/cancel/", {"reason": "Customer withdrew", "expected_version": 3}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        response = head.post(f"{BASE}{project.uid}/cancel/", {"reason": "Customer withdrew"}, format="json")
        assert response.status_code == 200 and response.json()["status"] == "CANCELLED" and response.json()["cancel_reason"] == "Customer withdrew"
        assert head.delete(f"{BASE}{project.uid}/").status_code == 204


class TestWaiverScope:
    """A waiver covers the finding engineering reviewed, not every later finding that shares its rule and components."""

    def test_waiver_of_one_missing_role_does_not_cover_another(self, head, checker, full_lines):
        project = ProjectFactory()
        no_structure = [line for line in full_lines if line["role"] != "STRUCTURE"]
        refused = _lock(head, project, no_structure, ACKS)
        assert refused.status_code == 409 and any("STRUCTURE" in item for item in refused.json()["errors"]["blocked"])
        run = Run.objects.get(uid=refused.json()["errors"]["engineering_run"][0])
        for finding in Finding.objects.filter(run=run, severity="BLOCK"):
            runs.acknowledge(finding, user=None, reason="Structure supplied by the customer")
        no_inverter = [line for line in full_lines if line["role"] != "INVERTER"]
        response = _lock(head, project, no_inverter, ACKS)
        assert response.status_code == 409, response.json()
        assert any("Required role INVERTER is missing" in item for item in response.json()["errors"]["blocked"])
        project.refresh_from_db()
        assert project.status == "PLANNED" and project.bom_lock is None
        assert _lock(head, project, no_structure, ACKS).status_code == 200  # the reviewed BOM still locks
