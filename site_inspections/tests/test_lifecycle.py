"""State machine: submit, hold/resume, revision, release (readiness on the current row), system type, reviews,
equipment checklists and additional work."""

import uuid

import pytest

from core.errors import Conflict, DomainError, PermissionDenied
from core.models import OutboxEvent
from site_inspections.models import AdditionalWorkItem, EngineeringReview, EquipmentAssessment, Inspection, LocationApproval
from site_inspections.models.choices import Status
from site_inspections.services import equipment, inspections, lifecycle, reviews, work
from site_inspections.tests.factories import PASS_ALL_OG, InspectionFactory, add_photo, approved, completed_fields, issued_extra_structure

pytestmark = pytest.mark.django_db
BASE = "/api/v1/site-inspections/"


@pytest.fixture
def working(engineer):
    return InspectionFactory(engineer=engineer, status=Status.IN_PROGRESS)


class TestSubmit:
    def test_blocked_until_field_completion(self, auth_client, engineer, working):
        response = auth_client(engineer).post(f"{BASE}{working.uid}/submit/", {}, format="json")
        body = response.json()
        assert response.status_code == 409 and body["code"] == "completion_blocked"
        assert {"PANEL_PHOTO_REQUIRED", "SUITABILITY_UNCONFIRMED", "COMPLEXITY_NOT_ASSESSED", "EQUIPMENT_SYSTEM_TYPE_UNDECIDED"} - {"EQUIPMENT_SYSTEM_TYPE_UNDECIDED"} <= set(
            body["errors"]["blockers"]
        )

    def test_submit_completes(self, auth_client, engineer, working):
        completed_fields(working, engineer)
        response = auth_client(engineer).post(f"{BASE}{working.uid}/submit/", {"expected_version": Inspection.objects.get(pk=working.pk).version}, format="json")
        assert response.status_code == 200 and response.json()["status"] == "COMPLETED"

    def test_wrong_status_and_permission(self, auth_client, engineer, head):
        draft = InspectionFactory(engineer=engineer)
        assert auth_client(engineer).post(f"{BASE}{draft.uid}/submit/", {}, format="json").json()["code"] == "invalid_status"
        assert auth_client(head).post(f"{BASE}{draft.uid}/submit/", {}, format="json").status_code == 403


class TestHoldResumeRevision:
    def test_hold_and_resume_return_to_the_held_status(self, auth_client, head, working):
        client = auth_client(head)
        assert client.post(f"{BASE}{working.uid}/hold/", {"reason": ""}, format="json").status_code == 400
        body = client.post(f"{BASE}{working.uid}/hold/", {"reason": "Customer travelling"}, format="json").json()
        assert body["status"] == "ON_HOLD" and body["held_from_status"] == "IN_PROGRESS"
        assert client.post(f"{BASE}{working.uid}/hold/", {"reason": "again"}, format="json").json()["code"] == "invalid_status"
        assert client.post(f"{BASE}{working.uid}/resume/", {}, format="json").json()["status"] == "IN_PROGRESS"
        assert client.post(f"{BASE}{working.uid}/resume/", {}, format="json").json()["code"] == "invalid_status"

    def test_revision_supersedes_approvals(self, auth_client, head, engineer, working):
        inspection = approved(working, engineer)
        client = auth_client(head)
        assert client.post(f"{BASE}{inspection.uid}/revision/", {"reason": " "}, format="json").status_code == 400
        body = client.post(f"{BASE}{inspection.uid}/revision/", {"reason": "Move the inverter"}, format="json").json()
        assert body["status"] == "REVISION_REQUIRED"
        assert set(LocationApproval.objects.filter(inspection=inspection).values_list("status", flat=True)) == {"SUPERSEDED"}
        assert client.post(f"{BASE}{InspectionFactory().uid}/revision/", {"reason": "x"}, format="json").json()["code"] == "invalid_status"


class TestRelease:
    def test_release_emits_event(self, auth_client, head, engineer, working):
        inspection = approved(working, engineer)
        response = auth_client(head).post(f"{BASE}{inspection.uid}/release/", {}, format="json")
        assert response.status_code == 200, response.json()
        assert response.json()["status"] == "INSTALLATION_READY" and response.json()["released_by"]["uid"] == str(head.uid)
        event = OutboxEvent.objects.get(event_type="site_inspections.released")
        assert event.payload["inspection_uid"] == str(inspection.uid) and event.payload["customer_uid"] == str(inspection.customer.uid)

    @pytest.mark.parametrize("phase,expected_phase", [("3P", "3P"), ("NC", "")])
    def test_release_creates_the_project_with_the_documented_payload(self, auth_client, head, engineer, working, settings, drain_outbox, phase, expected_phase):
        """End to end with projects (D-8, auto-create switched on): the released payload carries what projects
        consumes — customer, agreement, quotation version, system type, size and phase (NC is not a project phase)."""
        from projects.models import Project

        settings.PROJECTS_AUTO_CREATE_ON_RELEASE = True
        inspection = approved(working, engineer)
        agreement_uid, quotation_version_uid = uuid.uuid4(), uuid.uuid4()
        Inspection.objects.filter(pk=inspection.pk).update(agreement_uid=agreement_uid, quotation_version_uid=quotation_version_uid, quoted_size_kw="5.500", phase=phase)
        response = auth_client(head).post(f"{BASE}{inspection.uid}/release/", {}, format="json")
        assert response.status_code == 200, response.json()
        payload = OutboxEvent.objects.get(event_type="site_inspections.released").payload
        assert payload["size_kw"] == "5.500" and payload["phase"] == (expected_phase or None) and payload["lead_uid"] is None
        drain_outbox()
        project = Project.objects.get(site_inspection_uid=inspection.uid)
        assert project.customer_id == inspection.customer_id and project.agreement_uid == agreement_uid and project.quotation_version_uid == quotation_version_uid
        assert (project.system_type, str(project.size_kw), project.phase) == ("ON_GRID", "5.50", expected_phase)

    def test_release_is_judged_on_the_current_row(self, auth_client, head, engineer, working):
        inspection = approved(working, engineer)
        AdditionalWorkItem.objects.create(inspection=inspection, work_type="WALKWAY", customer_impacting=True)
        response = auth_client(head).post(f"{BASE}{inspection.uid}/release/", {}, format="json")
        assert response.status_code == 409 and response.json()["errors"]["blockers"] == ["ADDITIONAL_WORK_UNAPPROVED"]

    def test_release_needs_approved_and_permission(self, auth_client, head, engineer, working):
        assert auth_client(head).post(f"{BASE}{working.uid}/release/", {}, format="json").json()["code"] == "invalid_status"
        assert auth_client(engineer).post(f"{BASE}{working.uid}/release/", {}, format="json").status_code == 403

    def test_released_inspection_is_final(self, head, engineer, working):
        inspection = lifecycle.release(approved(working, engineer), user=head)
        with pytest.raises(Conflict) as error:
            lifecycle.hold(inspection, user=head, reason="x")
        assert error.value.code == "inspection_read_only"


class TestSystemType:
    def test_before_completion_resets_checklists(self, auth_client, head, engineer, working):
        Inspection.objects.filter(pk=working.pk).update(system_type="ON_GRID")
        equipment.put_assessment(working, user=engineer, equipment_type="ON_GRID_INVERTER", data={"results": {"direct_sunlight": "PASS"}})
        body = auth_client(head).post(f"{BASE}{working.uid}/system-type/", {"system_type": "HYBRID"}, format="json").json()
        assert body["system_type"] == "HYBRID" and body["status"] == "IN_PROGRESS"
        assert not EquipmentAssessment.objects.filter(inspection=working).exists()

    def test_after_completion_needs_reason_and_sends_back(self, head, engineer, working):
        inspection = approved(working, engineer)
        with pytest.raises(DomainError) as error:
            inspections.set_system_type(inspection, user=head, system_type="HYBRID")
        assert error.value.code == "reason_required"
        inspection = inspections.set_system_type(inspection, user=head, system_type="HYBRID", reason="Customer wants backup")
        assert inspection.status == Status.REVISION_REQUIRED
        assert inspections.set_system_type(inspection, user=head, system_type="HYBRID").version == inspection.version

    def test_not_while_on_hold(self, head, working):
        lifecycle.hold(working, user=head, reason="wait")
        with pytest.raises(Conflict):
            inspections.set_system_type(Inspection.objects.get(pk=working.pk), user=head, system_type="HYBRID")


class TestEquipment:
    URL = "equipment/ON_GRID_INVERTER/"

    def test_undecided_and_unrequired_types(self, auth_client, engineer, working):
        client = auth_client(engineer)
        assert client.put(f"{BASE}{working.uid}/{self.URL}", {"results": {}}, format="json").json()["code"] == "system_type_undecided"
        Inspection.objects.filter(pk=working.pk).update(system_type="ON_GRID")
        assert client.put(f"{BASE}{working.uid}/equipment/HYBRID_BATTERY/", {"results": {}}, format="json").json()["code"] == "equipment_not_required"
        assert client.get(f"{BASE}{working.uid}/equipment/HYBRID_BATTERY/").status_code == 404

    def test_full_definition_status_evidence_and_review(self, auth_client, engineer, head, working):
        Inspection.objects.filter(pk=working.pk).update(system_type="ON_GRID")
        client = auth_client(engineer)
        listed = client.get(f"{BASE}{working.uid}/equipment/").json()
        assert listed[0]["saved"] is False and len(listed[0]["results"]) == 14
        one_tap = client.put(f"{BASE}{working.uid}/{self.URL}", {"results": {"direct_sunlight": "PASS"}}, format="json").json()
        assert one_tap["status"] == "IN_PROGRESS" and len(one_tap["unanswered"]) == 13
        bad = client.put(f"{BASE}{working.uid}/{self.URL}", {"results": {"nope": "PASS", "height": "MAYBE"}}, format="json")
        assert bad.status_code == 400 and set(bad.json()["errors"]) == {"results.nope", "results.height"}
        failed = {**PASS_ALL_OG, "rain_exposure": "FAIL"}
        assert client.put(f"{BASE}{working.uid}/{self.URL}", {"results": failed, "issue": "Rain"}, format="json").json()["code"] == "evidence_required"
        photo = add_photo(working, engineer, "EVIDENCE")
        assert client.put(f"{BASE}{working.uid}/{self.URL}", {"results": failed, "evidence_photo_uid": str(photo.uid)}, format="json").json()["code"] == "issue_required"
        saved = client.put(f"{BASE}{working.uid}/{self.URL}", {"results": failed, "issue": "Rain", "evidence_photo_uid": str(photo.uid), "expected_version": 1}, format="json").json()
        assert saved["status"] == "FAIL" and saved["review_status"] == "PENDING"
        assert EngineeringReview.objects.get(inspection=working).trigger == "EQUIPMENT"
        stale = client.put(f"{BASE}{working.uid}/{self.URL}", {"results": failed, "issue": "Rain", "evidence_photo_uid": str(photo.uid), "expected_version": 1}, format="json")
        assert stale.json()["code"] == "stale_version"
        review = auth_client(head).post(f"{BASE}{working.uid}/{self.URL}review/", {"decision": "WAIVED", "note": "Canopy will be fitted"}, format="json").json()
        assert review["review_status"] == "WAIVED" and review["resolved_by"]["uid"] == str(head.uid)
        assert client.post(f"{BASE}{working.uid}/{self.URL}review/", {"decision": "WAIVED", "note": "x"}, format="json").status_code == 403

    def test_review_needs_a_critical_checklist_and_a_note(self, head, engineer, working):
        Inspection.objects.filter(pk=working.pk).update(system_type="ON_GRID")
        with pytest.raises(Conflict):
            equipment.review_assessment(working, user=head, equipment_type="ON_GRID_INVERTER", decision="RESOLVED", note="x")
        photo = add_photo(working, engineer, "EVIDENCE")
        equipment.put_assessment(working, user=engineer, equipment_type="ON_GRID_INVERTER", data={"results": {"height": "FAIL"}, "issue": "High", "evidence_photo_uid": photo.uid})
        with pytest.raises(DomainError) as error:
            equipment.review_assessment(working, user=head, equipment_type="ON_GRID_INVERTER", decision="RESOLVED", note=" ")
        assert error.value.code == "note_required"
        with pytest.raises(DomainError):
            equipment.review_assessment(working, user=head, equipment_type="ON_GRID_INVERTER", decision="PENDING", note="x")


class TestEngineeringReviews:
    def test_request_decide_and_routine_clears_complexity(self, auth_client, engineer, head, working):
        client = auth_client(engineer)
        created = client.post(f"{BASE}{working.uid}/reviews/", {"reason": "Old roof"}, format="json")
        assert created.status_code == 201
        assert client.post(f"{BASE}{working.uid}/reviews/", {"reason": "again"}, format="json").json()["code"] == "review_pending"
        Inspection.objects.filter(pk=working.pk).update(complexity_status="ENGINEERING_REVIEW_REQUIRED")
        url = f"{BASE}{working.uid}/reviews/{created.json()['uid']}/decide/"
        assert client.post(url, {"decision": "ROUTINE"}, format="json").status_code == 403
        head_client = auth_client(head)
        assert head_client.post(url, {"decision": "REQUIRES_CHANGES"}, format="json").json()["code"] == "notes_required"
        assert head_client.post(url, {"decision": "ROUTINE", "notes": "fine"}, format="json").json()["decision"] == "ROUTINE"
        assert Inspection.objects.get(pk=working.pk).complexity_status == "ROUTINE"
        assert head_client.post(url, {"decision": "RESOLVED"}, format="json").json()["code"] == "review_decided"
        assert head_client.get(f"{BASE}{working.uid}/reviews/").json()["count"] == 1
        assert head_client.post(f"{BASE}{working.uid}/reviews/{uuid.uuid4()}/decide/", {"decision": "RESOLVED"}, format="json").status_code == 404

    def test_requires_changes_keeps_the_review_pending_for_readiness(self, head, engineer, working):
        review = reviews.request_review(working, user=engineer, reason="Check the slab")
        reviews.decide_review(review, user=head, decision="REQUIRES_CHANGES", notes="Add a beam")
        assert reviews.review_pending(Inspection.objects.get(pk=working.pk))


class TestAdditionalWork:
    def test_lifecycle_is_enforced(self, auth_client, engineer, head, working):
        client = auth_client(engineer)
        created = client.post(f"{BASE}{working.uid}/additional-work/", {"work_type": "WALKWAY", "customer_impacting": True, "quantity": "6", "unit": "M"}, format="json")
        assert created.status_code == 201 and created.json()["status"] == "IDENTIFIED"
        assert client.post(f"{BASE}{working.uid}/additional-work/", {"work_type": "WALKWAY"}, format="json").json()["code"] == "work_type_exists"
        item = created.json()["uid"]
        assert client.patch(f"{BASE}{working.uid}/additional-work/{item}/", {"dimensions": "6 m x 1 m"}, format="json").json()["dimensions"] == "6 m x 1 m"
        assert client.post(f"{BASE}{working.uid}/additional-work/{item}/transition/", {"status": "ENGINEERING_REVIEW"}, format="json").json()["code"] == "approve_required"
        head_client = auth_client(head)
        move = lambda status, **extra: head_client.post(f"{BASE}{working.uid}/additional-work/{item}/transition/", {"status": status, **extra}, format="json")  # noqa: E731
        assert move("APPROVED").json()["code"] == "invalid_transition"
        assert move("ENGINEERING_REVIEW").json()["status"] == "ENGINEERING_REVIEW"
        assert client.patch(f"{BASE}{working.uid}/additional-work/{item}/", {"dimensions": "x"}, format="json").json()["code"] == "work_item_locked"
        assert move("COST_CALCULATED").json()["code"] == "agreement_required"
        # The agreements context's validator (AgreementsConfig.ready) needs a live ISSUED EXTRA_STRUCTURE agreement
        # raised from this inspection; an unknown uid is refused.
        assert move("COST_CALCULATED", agreement_uid=str(uuid.uuid4())).json()["code"] == "agreement_invalid"
        agreement = issued_extra_structure(working).uid
        assert move("COST_CALCULATED", agreement_uid=str(agreement)).json()["agreement_uid"] == str(agreement)
        assert move("CUSTOMER_QUOTE_SENT").json()["status"] == "CUSTOMER_QUOTE_SENT"
        decided = move("APPROVED").json()
        assert decided["status"] == "APPROVED" and decided["decided_by"]["uid"] == str(head.uid)
        assert client.delete(f"{BASE}{working.uid}/additional-work/{item}/").json()["code"] == "work_item_locked"
        assert move("NONE").json()["agreement_uid"] is None
        assert client.post(f"{BASE}{working.uid}/additional-work/{item}/transition/", {"status": "IDENTIFIED"}, format="json").json()["status"] == "IDENTIFIED"
        assert client.delete(f"{BASE}{working.uid}/additional-work/{item}/").status_code == 204
        assert client.get(f"{BASE}{working.uid}/additional-work/").json() == []

    def test_agreement_validator_and_permissions(self, head, engineer, working):
        item = work.create_item(working, user=engineer, data={"work_type": "LADDER"})
        with pytest.raises(PermissionDenied):
            work.transition_item(item, user=engineer, status="ENGINEERING_REVIEW")
        work.transition_item(item, user=head, status="ENGINEERING_REVIEW")
        previous = work.register_agreement_validator(lambda uid, inspection: "Not an EXTRA_STRUCTURE agreement.")
        try:
            with pytest.raises(DomainError) as error:
                work.transition_item(AdditionalWorkItem.objects.get(pk=item.pk), user=head, status="COST_CALCULATED", agreement_uid=uuid.uuid4())
            assert error.value.code == "agreement_invalid"
        finally:
            work.register_agreement_validator(previous)
