"""careers/applications/ — queue, detail, status workflow, assign, notes, events, archive/restore, signed downloads."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from audit.models import AuditLog
from careers.models import JobApplication, JobApplicationEvent
from careers.tests.factories import JobApplicationFactory, JobApplicationNoteFactory, JobPositionFactory
from core.models import OutboxEvent
from media.tests import files

pytestmark = pytest.mark.django_db
URL = "/api/v1/careers/applications/"
Status = JobApplication.Status


def detail(application, suffix=""):
    return f"{URL}{application.uid}/{suffix}"


def submitted(api_client, **extra):
    """An application submitted through the public form (real private files)."""
    data = {
        "full_name": "Anu Thomas",
        "email": "anu@example.com",
        "phone": "9847012345",
        "location": "Alappuzha",
        "linkedin": "linkedin.com/in/anu",
        "declaration_accepted": "true",
        "resume": SimpleUploadedFile("cv.pdf", files.pdf()),
        **extra,
    }
    response = api_client.post("/api/public/v1/job-applications/", data, format="multipart")
    assert response.status_code == 201, response.json()
    return JobApplication.objects.get(uid=response.json()["uid"])


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        application = JobApplicationFactory()
        assert api_client.get(URL).status_code == 401
        assert api_client.get(detail(application)).status_code == 401
        assert api_client.get(detail(application, "download/resume/")).status_code == 401

    def test_each_action_needs_its_grant(self, auth_client, make_user):
        application = JobApplicationFactory()
        viewer = auth_client(make_user(grants={"applications": ["view"]}))
        assert viewer.get(URL).status_code == 200 and viewer.get(detail(application)).status_code == 200
        assert viewer.get(detail(application, "notes/")).status_code == 200 and viewer.get(detail(application, "events/")).status_code == 200
        assert viewer.post(detail(application, "status/"), {"status": "SCREENING"}, format="json").status_code == 403
        assert viewer.post(detail(application, "notes/"), {"body": "x"}, format="json").status_code == 403
        assert viewer.post(detail(application, "assign/"), {"position": str(application.position.uid)}, format="json").status_code == 403
        assert viewer.delete(detail(application)).status_code == 403
        assert viewer.post(detail(application, "restore/")).status_code == 403
        editor = auth_client(make_user(grants={"applications": ["view", "edit"]}))
        assert editor.post(detail(application, "notes/"), {"body": "x"}, format="json").status_code == 201
        assert editor.delete(detail(application)).status_code == 403
        # Holding job_positions (or media) never exposes candidates.
        assert auth_client(make_user(grants={"job_positions": "*", "media": "*"})).get(URL).status_code == 403

    @pytest.mark.parametrize("suffix", ["", "notes/", "events/", "download/resume/"])
    def test_sub_resources_need_applications_view(self, auth_client, make_user, suffix):
        application = JobApplicationFactory()
        outsider = auth_client(make_user(grants={"job_positions": "*", "departments": "*"}))
        assert outsider.get(detail(application, suffix)).status_code == 403

    def test_scope_is_all(self, auth_client, make_user):
        JobApplicationFactory.create_batch(2)
        assert auth_client(make_user(grants={"applications": ["view"]})).get(URL).json()["count"] == 2


class TestQueue:
    def test_list_shape_filters_and_no_n_plus_one(self, hr_client, hr, django_assert_max_num_queries):
        position = JobPositionFactory(published=True)
        JobApplicationFactory(position=position, name="Anu Thomas", assignee=hr)
        JobApplicationFactory(position=None, status=Status.SCREENING, name="Biju")
        JobApplicationFactory(name="Archived Person").soft_delete()
        for _ in range(6):
            JobApplicationFactory()
        with django_assert_max_num_queries(8):
            body = hr_client.get(URL).json()
        assert body["count"] == 8 and "Archived Person" not in {row["name"] for row in body["results"]}
        row = next(row for row in body["results"] if row["name"] == "Anu Thomas")
        assert row["position"] == str(position.uid) and row["assignee"]["uid"] == str(hr.uid) and row["archived"] is False

        def names(**params):
            return {row["name"] for row in hr_client.get(URL, params).json()["results"]}

        assert names(status="SCREENING") == {"Biju"}
        assert names(position=str(position.uid)) == {"Anu Thomas"}
        assert names(assignee=str(hr.uid)) == {"Anu Thomas"}
        assert "Archived Person" in names(include_archived="true")
        assert names(search="biju") == {"Biju"}

    def test_detail_embeds_notes_events_and_file_metadata(self, api_client, hr_client):
        application = submitted(api_client)
        JobApplicationNoteFactory(application=application, body="Call back Monday")
        body = hr_client.get(detail(application)).json()
        assert body["resume"] == {"filename": "Anu_Thomas_Resume.pdf", "mime_type": "application/pdf", "size_bytes": application.resume.size_bytes}
        assert body["portfolio"] is None and body["allowed_transitions"] == ["SCREENING", "REJECTED", "WITHDRAWN"]
        assert [note["body"] for note in body["notes"]] == ["Call back Monday"]
        assert [event["kind"] for event in body["events"]] == ["RECEIVED"]
        assert "ip" not in body


class TestWorkflow:
    def test_status_moves_record_the_timeline(self, hr_client, hr):
        application = JobApplicationFactory()
        response = hr_client.post(detail(application, "status/"), {"status": "SCREENING", "note": "Good CV", "expected_version": 1}, format="json")
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["status"] == "SCREENING" and body["version"] == 2 and body["status_changed_at"]
        event = body["events"][0]
        assert event["kind"] == "STATUS" and event["from_status"] == "NEW" and event["to_status"] == "SCREENING" and event["detail"] == "Good CV"
        assert event["actor"] == str(hr.uid) and event["actor_name"] == hr.get_full_name()
        assert AuditLog.objects.get(action="careers.application_status_changed").note == "Good CV"
        assert OutboxEvent.objects.get(event_type="careers.application_status_changed").payload["to_status"] == "SCREENING"
        again = hr_client.post(detail(application, "status/"), {"status": "SCREENING"}, format="json")
        assert again.json()["version"] == 2  # same status: no-op

    def test_invalid_transition(self, hr_client):
        application = JobApplicationFactory(status=Status.NEW)
        response = hr_client.post(detail(application, "status/"), {"status": "HIRED"}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "invalid_status_transition"

    def test_status_validation_and_stale_version(self, hr_client):
        application = JobApplicationFactory(version=3)
        bad = hr_client.post(detail(application, "status/"), {"status": "selected"}, format="json")
        assert bad.status_code == 400 and "status" in bad.json()["errors"]
        stale = hr_client.post(detail(application, "status/"), {"status": "SCREENING", "expected_version": 2}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"

    def test_assign_links_a_posting_and_an_assignee(self, hr_client, make_user):
        application = JobApplicationFactory(position=None, position_label="General application", position_title="")
        position = JobPositionFactory(closed=True, title="Store Keeper", department__name="Operations")
        colleague = make_user(grants={"applications": ["view"]})
        response = hr_client.post(detail(application, "assign/"), {"position": str(position.uid), "assignee": str(colleague.uid), "expected_version": 1}, format="json")
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["position"] == str(position.uid) and body["position_title"] == "Store Keeper" and body["department_name"] == "Operations"
        assert body["position_label"] == "General application" and body["display_position"] == "Store Keeper"
        assert body["assignee"]["uid"] == str(colleague.uid)
        assert {event["kind"] for event in body["events"]} >= {"ASSIGNED", "ASSIGNEE"}
        cleared = hr_client.post(detail(application, "assign/"), {"assignee": None}, format="json").json()
        assert cleared["assignee"] is None

    def test_assign_validation(self, hr_client, make_user):
        application = JobApplicationFactory()
        assert hr_client.post(detail(application, "assign/"), {}, format="json").status_code == 400
        outsider = make_user(grants={"media": ["view"]})
        response = hr_client.post(detail(application, "assign/"), {"assignee": str(outsider.uid)}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "invalid_assignee"
        unknown = hr_client.post(detail(application, "assign/"), {"position": "00000000-0000-0000-0000-000000000000"}, format="json")
        assert unknown.status_code == 400 and "position" in unknown.json()["errors"]

    def test_notes(self, hr_client, hr):
        application = JobApplicationFactory()
        created = hr_client.post(detail(application, "notes/"), {"body": "  Strong candidate  "}, format="json")
        assert created.status_code == 201 and created.json()["body"] == "Strong candidate" and created.json()["author"] == str(hr.uid)
        blank = hr_client.post(detail(application, "notes/"), {"body": "   "}, format="json")
        assert blank.status_code == 400
        listing = hr_client.get(detail(application, "notes/")).json()
        assert listing["count"] == 1 and listing["results"][0]["author_name"] == hr.get_full_name()
        assert JobApplicationEvent.objects.filter(application=application, kind="NOTE").count() == 1

    @pytest.mark.parametrize("suffix", ["notes/", "events/"])
    def test_sub_resource_lists_ignore_the_queue_filters(self, hr_client, suffix):
        """The queue's ?status/?search/?ordering were documented on notes/ and events/ and silently 404'd the parent."""
        application = JobApplicationFactory(status=Status.NEW)
        JobApplicationNoteFactory(application=application)
        JobApplicationEvent.objects.create(application=application, kind=JobApplicationEvent.Kind.RECEIVED)
        response = hr_client.get(detail(application, suffix), {"status": "REJECTED", "search": "nobody", "position": "00000000-0000-0000-0000-000000000000"})
        assert response.status_code == 200, response.json()
        assert len(response.json()["results"]) == 1

    def test_sub_resource_schemas_do_not_advertise_the_queue_filters(self):
        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator(api_version="v1").get_schema(request=None, public=True)
        for suffix in ("notes/", "events/"):
            operation = schema["paths"][f"/api/v1/careers/applications/{{uid}}/{suffix}"]["get"]
            names = {parameter["name"] for parameter in operation.get("parameters", [])}
            assert not names & {"status", "position", "assignee", "source", "created_from", "created_to", "search", "ordering"}, (suffix, names)

    def test_events_are_cursor_paginated(self, hr_client):
        application = JobApplicationFactory()
        for status in ("SCREENING", "INTERVIEW", "OFFERED"):
            hr_client.post(detail(application, "status/"), {"status": status}, format="json")
        body = hr_client.get(detail(application, "events/")).json()
        assert set(body) == {"results", "next", "previous"} and [event["to_status"] for event in body["results"]] == ["OFFERED", "INTERVIEW", "SCREENING"]

    def test_archive_and_restore(self, hr_client):
        application = JobApplicationFactory()
        assert hr_client.delete(detail(application)).status_code == 204
        assert hr_client.delete(detail(application)).status_code == 204  # idempotent
        assert hr_client.get(URL).json()["count"] == 0
        archived = hr_client.get(detail(application)).json()
        assert archived["archived"] is True and archived["allowed_transitions"] == []
        refused = hr_client.post(detail(application, "status/"), {"status": "SCREENING"}, format="json")
        assert refused.status_code == 409 and refused.json()["code"] == "application_archived"
        assert hr_client.post(detail(application, "notes/"), {"body": "x"}, format="json").status_code == 409
        restored = hr_client.post(detail(application, "restore/")).json()
        assert restored["archived"] is False and {event["kind"] for event in restored["events"]} >= {"ARCHIVED", "RESTORED"}
        assert list(AuditLog.objects.filter(action__in=["careers.application_archived", "careers.application_restored"]).values_list("action", flat=True).order_by("id")) == [
            "careers.application_archived",
            "careers.application_restored",
        ]


class TestDownloads:
    def test_signed_download_serves_the_private_file_with_the_candidate_name(self, api_client, hr_client):
        application = submitted(api_client, portfolio_file=SimpleUploadedFile("work.docx", files.docx()))
        link = hr_client.get(detail(application, "download/resume/")).json()
        assert link["filename"] == "Anu_Thomas_Resume.pdf" and link["mime_type"] == "application/pdf" and link["expires_at"]
        response = api_client.get(link["url"])
        assert response.status_code == 200 and b"".join(response.streaming_content) == files.pdf()
        assert "Anu_Thomas_Resume.pdf" in response["Content-Disposition"] and response["Cache-Control"] == "private, no-store"
        portfolio = hr_client.get(detail(application, "download/portfolio/")).json()
        assert portfolio["filename"] == "Anu_Thomas_Portfolio.docx"

    def test_missing_file_and_unknown_kind(self, hr_client):
        application = JobApplicationFactory()
        response = hr_client.get(detail(application, "download/resume/"))
        assert response.status_code == 404 and response.json()["code"] == "file_not_found"
        assert hr_client.get(detail(application, "download/passport/")).status_code == 404

    def test_resume_cannot_be_deleted_from_the_media_library(self, api_client, auth_client, make_user):
        from media.services.assets import delete_asset

        application = submitted(api_client)
        from core.errors import Conflict

        with pytest.raises(Conflict):
            delete_asset(application.resume, user=None)
