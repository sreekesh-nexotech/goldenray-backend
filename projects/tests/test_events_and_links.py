"""``site_inspections.released`` auto-create (D-8, off by default), the customer timeline provider, customer merges,
and the lock against the real checker context."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from django.utils import timezone

from core.errors import Conflict, DomainError
from core.models import OutboxEvent
from core.outbox import emit
from customers.services import merge, timeline
from customers.tests.factories import CustomerFactory
from engineering.models import RuleSet
from projects.models import Project
from projects.services import bom_lock, projects
from projects.tests.conftest import ACKS
from projects.tests.factories import ProjectFactory

pytestmark = pytest.mark.django_db


def _released(customer, inspection=None, **overrides):
    payload = {
        "inspection_uid": str(inspection or uuid.uuid4()),
        "customer_uid": str(customer.uid),
        "lead_uid": None,
        "agreement_uid": str(uuid.uuid4()),
        "system_type": "HYBRID",
        "size_kw": 5,
        "phase": "1P",
        "released_at": "2026-09-29T10:00:00+05:30",
        **overrides,
    }
    emit("site_inspections.released", payload, aggregate_type="site_inspections.inspection")
    return payload


class TestAutoCreate:
    def test_off_by_default(self, drain_outbox):
        _released(CustomerFactory())
        drain_outbox()
        assert not Project.objects.exists()

    def test_creates_once(self, settings, drain_outbox):
        settings.PROJECTS_AUTO_CREATE_ON_RELEASE = True
        customer = CustomerFactory()
        inspection = uuid.uuid4()
        payload = _released(customer, inspection)
        _released(customer, inspection)  # redelivery / second release
        drain_outbox()
        project = Project.objects.get()
        assert project.site_inspection_uid == inspection and project.customer == customer and project.status == "PLANNED"
        assert project.system_type == "HYBRID" and str(project.size_kw) == "5.00" and project.phase == "1P" and str(project.agreement_uid) == payload["agreement_uid"]
        assert project.number.startswith("PROJ-") and project.created_by is None
        assert OutboxEvent.objects.filter(event_type="projects.created").count() == 1

    def test_follows_merged_customer_and_drops_bad_payloads(self, settings, drain_outbox):
        settings.PROJECTS_AUTO_CREATE_ON_RELEASE = True
        survivor = CustomerFactory()
        merged = CustomerFactory()
        merge.merge_customers(merged, into=survivor, user=None)
        _released(merged, system_type="SOLAR", phase="2P", size_kw=None)
        _released(CustomerFactory.build())  # unknown customer
        _released(survivor, inspection_uid="not-a-uuid")
        _released(survivor, size_kw=-1)
        drain_outbox()
        project = Project.objects.get()
        assert project.customer == survivor and project.system_type == "" and project.phase == "" and project.size_kw is None
        assert not OutboxEvent.objects.filter(event_type="site_inspections.released", parked_at__isnull=False).exists()

    def test_cancelled_project_allows_a_new_one(self, settings):
        customer = CustomerFactory()
        inspection = uuid.uuid4()
        first, created = projects.create_from_inspection(customer=customer, inspection_uid=inspection)
        again, created_again = projects.create_from_inspection(customer=customer, inspection_uid=inspection)
        assert created and not created_again and again.pk == first.pk
        projects.cancel(first, user=None, reason="duplicate")
        second, created = projects.create_from_inspection(customer=customer, inspection_uid=inspection)
        assert created and second.pk != first.pk


class TestCustomerLinks:
    def test_timeline_entries(self, make_user):
        customer = CustomerFactory()
        project = ProjectFactory(customer=customer, status="CANCELLED", cancelled_at=timezone.now() + dt.timedelta(minutes=5), cancel_reason="lost")
        ProjectFactory()  # another customer's
        viewer = make_user(grants={"customers": ["view"], "projects": ["view"]})
        entries, _ = timeline.build(customer, user=viewer)
        kinds = [entry.kind for entry in entries if entry.kind.startswith("projects.")]
        assert kinds == ["projects.cancelled", "projects.created"] and all(entry.object_uid == project.uid for entry in entries if entry.kind.startswith("projects."))
        blind = make_user(grants={"customers": ["view"]})
        assert not [entry for entry in timeline.build(customer, user=blind)[0] if entry.kind.startswith("projects.")]
        older, _ = timeline.build(customer, user=viewer, before=project.cancelled_at)
        assert [entry.kind for entry in older if entry.kind.startswith("projects.")] == ["projects.created"]

    def test_merge_repoints_and_blocks_delete(self):
        source, target = CustomerFactory(), CustomerFactory()
        project = ProjectFactory(customer=source)
        assert merge.blocking_references(target) == {}
        assert merge.blocking_references(source)
        merge.merge_customers(source, into=target, user=None)
        project.refresh_from_db()
        assert project.customer == target and project.version == 2


class TestLockService:
    def test_real_checker_context(self, make_user, full_lines):
        """Against the real Flarize-shaped catalog (no price release): the verdict is stored, the lock refused or made."""
        project = ProjectFactory()
        user = make_user(grants={"projects": "*"})
        try:
            locked = bom_lock.lock_bom(project, user=user, architecture="DEYE", lines=full_lines, acknowledgements=ACKS)
        except Conflict as exc:
            assert exc.code == "bom_lock_refused"
        else:
            assert locked.bom_lock["price_release_number"] is None
        assert project.uid in set(bom_lock.Run.objects.values_list("subject_uid", flat=True))

    def test_no_active_rule_set(self, make_user, checker, full_lines):
        RuleSet.objects.filter(engine="engineeringChecker").update(active=False)
        with pytest.raises(Conflict) as exc:
            bom_lock.lock_bom(ProjectFactory(), user=make_user(grants={"projects": "*"}), architecture="DEYE", lines=full_lines, acknowledgements=ACKS)
        assert exc.value.code == "no_active_rule_set"


class TestServiceErrors:
    def test_remaining_domain_errors(self, make_user, checker, full_lines):
        user = make_user(grants={"projects": "*"})
        cancelled = ProjectFactory(status="CANCELLED", cancelled_at=timezone.now(), cancel_reason="lost")
        with pytest.raises(Conflict) as exc:
            bom_lock.lock_bom(cancelled, user=user, architecture="DEYE", lines=full_lines)
        assert exc.value.code == "invalid_transition"
        project = ProjectFactory()
        with pytest.raises(DomainError) as exc:
            projects.cancel(project, user=user, reason="  ")
        assert exc.value.code == "validation_error"
        inspection = uuid.uuid4()
        ProjectFactory(site_inspection_uid=inspection)
        with pytest.raises(Conflict) as exc:
            projects.update_project(project, user=user, data={"site_inspection_uid": inspection})
        assert exc.value.code == "inspection_has_project"
        assert projects.update_project(project, user=user, data={"title": project.title}).version == 1  # no change, no write
        assert projects.set_cost_inputs(project, user=user, inputs={}).version == 1
