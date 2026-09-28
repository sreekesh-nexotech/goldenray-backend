"""careers/positions/ — editor CRUD, publish workflow, preview, overview; permissions, versions, audit, outbox."""

import datetime as dt

import pytest

from audit.models import AuditLog
from careers.models import JobPosition
from careers.tests.factories import DepartmentFactory, JobApplicationFactory, JobPositionFactory
from core.models import OutboxEvent
from media.tests.factories import MediaAssetFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/careers/positions/"
Status = JobPosition.Status


def detail(position, suffix=""):
    return f"{URL}{position.uid}/{suffix}"


def new_position(dept, **extra):
    return {"title": "Solar Installation Engineer", "department": str(dept.uid), "location": "Alappuzha", "description": "Install rooftop systems.", **extra}


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        position = JobPositionFactory()
        assert api_client.get(URL).status_code == 401
        assert api_client.post(detail(position, "publish/")).status_code == 401
        assert api_client.get("/api/v1/careers/overview/").status_code == 401

    def test_actions_map_to_their_registry_grants(self, auth_client, make_user):
        position = JobPositionFactory(published=True)
        viewer = auth_client(make_user(grants={"job_positions": ["view"]}))
        assert viewer.get(URL).status_code == 200 and viewer.get(detail(position, "preview/")).status_code == 200
        for method, path in (
            ("post", URL),
            ("patch", detail(position)),
            ("post", detail(position, "close/")),
            ("post", detail(position, "unpublish/")),
            ("post", detail(position, "archive/")),
            ("delete", detail(position)),
        ):
            assert getattr(viewer, method)(path, {}, format="json").status_code == 403, path
        editor = auth_client(make_user(grants={"job_positions": ["view", "edit"]}))
        assert editor.post(detail(position, "close/")).status_code == 200
        assert editor.post(detail(position, "publish/")).status_code == 403
        publisher = auth_client(make_user(grants={"job_positions": ["view", "publish"]}))
        assert publisher.post(detail(position, "publish/")).status_code == 200
        assert publisher.post(detail(position, "archive/")).status_code == 403
        outsider = auth_client(make_user(grants={"applications": "*"}))
        assert outsider.get(URL).status_code == 403 and outsider.get("/api/v1/careers/overview/").status_code == 403

    def test_scope_is_all(self, auth_client, make_user):
        JobPositionFactory.create_batch(2)
        assert auth_client(make_user(grants={"job_positions": ["view"]})).get(URL).json()["count"] == 2


class TestEditor:
    def test_create_draft_with_derived_slug_and_seo(self, hr_client, hr):
        department = DepartmentFactory(name="Engineering")
        image = MediaAssetFactory()
        response = hr_client.post(URL, new_position(department, seo_title="Hiring now", og_image=str(image.uid), openings=2, opens_on="2026-10-01", application_deadline="2026-12-31"), format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["slug"] == "solar-installation-engineer" and body["status"] == "DRAFT" and body["department_name"] == "Engineering"
        assert body["og_image"]["uid"] == str(image.uid) and body["public_url"] == "/career/solar-installation-engineer"
        assert body["publish_errors"] == [] and body["employment_type_label"] == "Full-time" and body["application_count"] == 0
        assert {issue["field"] for issue in body["seo_issues"]} == {"meta_description"}
        assert AuditLog.objects.get(action="careers.position_created").actor == hr

    @pytest.mark.parametrize(
        "field,value",
        [("title", ""), ("department", "00000000-0000-0000-0000-000000000000"), ("employment_type", "freelance"), ("slug", "Not A Slug"), ("openings", 0), ("canonical_url", "nope")],
    )
    def test_validation_envelope(self, hr_client, field, value):
        response = hr_client.post(URL, new_position(DepartmentFactory(), **{field: value}), format="json")
        assert response.status_code == 400 and field in response.json()["errors"], response.json()

    def test_og_image_must_be_a_public_image(self, hr_client):
        resume = MediaAssetFactory(visibility="PRIVATE", kind="RESUME", cdn_url="")
        response = hr_client.post(URL, new_position(DepartmentFactory(), og_image=str(resume.uid)), format="json")
        assert response.status_code == 400 and "og_image" in response.json()["errors"]

    def test_deadline_before_opening(self, hr_client):
        response = hr_client.post(URL, new_position(DepartmentFactory(), opens_on="2026-12-01", application_deadline="2026-11-01"), format="json")
        assert response.status_code == 400 and "application_deadline" in response.json()["errors"]
        position = JobPositionFactory(opens_on=dt.date(2026, 12, 1))
        response = hr_client.patch(detail(position), {"application_deadline": "2026-11-01"}, format="json")
        assert response.status_code == 400

    def test_slug_is_unique_among_live_positions(self, hr_client):
        JobPositionFactory(slug="solar-installation-engineer")
        response = hr_client.post(URL, new_position(DepartmentFactory()), format="json")
        assert response.status_code == 409 and response.json()["code"] == "position_slug_taken"

    def test_update_and_stale_version(self, hr_client):
        position = JobPositionFactory(title="Old")
        response = hr_client.patch(detail(position), {"title": "New title", "expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["version"] == 2
        assert AuditLog.objects.get(action="careers.position_updated").after == {"title": "New title"}
        stale = hr_client.patch(detail(position), {"title": "Other", "expected_version": 1}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"

    def test_editing_a_live_posting_emits_for_revalidation(self, hr_client):
        position = JobPositionFactory(published=True)
        hr_client.patch(detail(position), {"location": "Kottayam"}, format="json")
        assert OutboxEvent.objects.filter(event_type="careers.position_updated").count() == 1

    def test_list_hides_archived_and_filters(self, hr_client, django_assert_max_num_queries):
        engineering = DepartmentFactory()
        open_position = JobPositionFactory(department=engineering, published=True, title="Open", location="Kochi")
        JobPositionFactory(status=Status.ARCHIVED, title="Old")
        JobPositionFactory(title="Intern", employment_type="internship", location="Remote")
        JobApplicationFactory.create_batch(3, position=open_position)
        JobApplicationFactory(position=open_position).soft_delete()
        for _ in range(5):
            JobApplicationFactory(position=JobPositionFactory(published=True))
        with django_assert_max_num_queries(8):
            rows = hr_client.get(URL).json()["results"]
        assert "Old" not in {row["title"] for row in rows}
        assert next(row for row in rows if row["title"] == "Open")["application_count"] == 3

        def titles(**params):
            return {row["title"] for row in hr_client.get(URL, params).json()["results"]}

        assert titles(status="ARCHIVED") == {"Old"} and "Old" in titles(include_archived="true")
        assert titles(department=str(engineering.uid)) == {"Open"}
        assert titles(employment_type="internship") == {"Intern"}
        assert titles(location="remo") == {"Intern"} and titles(search="inter") == {"Intern"}


class TestWorkflow:
    def test_publish_close_unpublish_archive(self, hr_client):
        position = JobPositionFactory()
        published = hr_client.post(detail(position, "publish/"), {"expected_version": 1}, format="json")
        assert published.status_code == 200 and published.json()["status"] == "PUBLISHED" and published.json()["published_at"]
        first_published_at = published.json()["published_at"]
        assert hr_client.post(detail(position, "publish/")).json()["version"] == published.json()["version"]  # no-op
        closed = hr_client.post(detail(position, "close/")).json()
        assert closed["status"] == "CLOSED" and closed["closed_at"]
        republished = hr_client.post(detail(position, "publish/")).json()
        assert republished["closed_at"] is None and republished["published_at"] == first_published_at
        assert hr_client.post(detail(position, "unpublish/")).json()["status"] == "DRAFT"
        assert hr_client.post(detail(position, "archive/")).json()["status"] == "ARCHIVED"
        actions = list(AuditLog.objects.filter(action__startswith="careers.position_").order_by("id").values_list("action", flat=True))
        assert actions == ["careers.position_published", "careers.position_closed", "careers.position_published", "careers.position_unpublished", "careers.position_archived"]
        events = list(OutboxEvent.objects.order_by("id").values_list("event_type", flat=True))
        assert events == ["careers.position_published", "careers.position_closed", "careers.position_published", "careers.position_unpublished", "careers.position_archived"]
        assert OutboxEvent.objects.first().payload["path"] == position.seo_path()

    def test_invalid_transitions(self, hr_client):
        draft = JobPositionFactory()
        response = hr_client.post(detail(draft, "close/"))
        assert response.status_code == 409 and response.json()["code"] == "invalid_status_transition"
        archived = JobPositionFactory(status=Status.ARCHIVED)
        assert hr_client.post(detail(archived, "unpublish/")).status_code == 409

    def test_publish_gate(self, hr_client):
        department = DepartmentFactory(is_active=False, name="Dormant")
        position = JobPositionFactory(department=department, description="")
        response = hr_client.post(detail(position, "publish/"))
        assert response.status_code == 400 and response.json()["code"] == "position_not_ready"
        problems = response.json()["errors"]["publish_errors"]
        assert "A job description is required." in problems and any("inactive" in problem for problem in problems)
        position.refresh_from_db()
        assert position.status == Status.DRAFT

    def test_stale_version_on_actions(self, hr_client):
        position = JobPositionFactory(version=5)
        assert hr_client.post(detail(position, "publish/"), {"expected_version": 4}, format="json").status_code == 409

    def test_delete_only_without_applications(self, hr_client):
        applied = JobPositionFactory(published=True)
        JobApplicationFactory(position=applied).soft_delete()
        response = hr_client.delete(detail(applied))
        assert response.status_code == 409 and response.json()["code"] == "position_has_applications"
        position = JobPositionFactory(published=True)
        assert hr_client.delete(detail(position)).status_code == 204
        assert not JobPosition.objects.filter(pk=position.pk).exists()
        assert OutboxEvent.objects.filter(event_type="careers.position_deleted").exists()


class TestPreviewAndOverview:
    def test_preview_builds_the_public_view(self, hr_client):
        from company.tests.factories import CompanyProfileFactory

        CompanyProfileFactory(website="https://flarize.com", trade_name="Flarize")
        position = JobPositionFactory(slug="site-engineer", title="Site Engineer", application_deadline=dt.date(2026, 12, 31), published=True)
        body = hr_client.get(detail(position, "preview/")).json()
        assert body["url"] == "https://flarize.com/career/site-engineer" and body["publish_errors"] == []
        assert body["schema"]["@type"] == "JobPosting" and body["schema"]["validThrough"] == "2026-12-31"
        assert body["schema"]["hiringOrganization"]["name"] == "Flarize"

    def test_overview_counts_and_application_counts_need_the_grant(self, auth_client, make_user, hr_client):
        JobPositionFactory(published=True, title="Open")
        JobPositionFactory(title="Draft")
        JobPositionFactory(closed=True)
        JobApplicationFactory()
        body = hr_client.get("/api/v1/careers/overview/").json()
        assert body["counts"]["active_positions"] == 2 and body["counts"]["draft_positions"] == 1 and body["counts"]["closed_positions"] == 1
        assert body["counts"]["applications_total"] == 1 and body["counts"]["applications_new"] == 1
        assert {row["title"] for row in body["open_positions"]} >= {"Open"}
        viewer = auth_client(make_user(grants={"job_positions": ["view"]})).get("/api/v1/careers/overview/").json()
        assert "applications_total" not in viewer["counts"]
