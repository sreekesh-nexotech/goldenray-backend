"""GET /api/public/v1/job-positions/ (+ /<slug>/) — shape, visibility, caching, invalidation, throttle scope."""

import pytest

from careers.models import JobPosition
from careers.tests.factories import DepartmentFactory, JobPositionFactory
from company.tests.factories import CompanyProfileFactory

pytestmark = pytest.mark.django_db
LIST = "/api/public/v1/job-positions/"


def test_list_shape_and_visibility(api_client):
    CompanyProfileFactory(careers_intro="Join us.", careers_accepting_general_applications=False)
    sales = DepartmentFactory(name="Sales", slug="sales")
    open_position = JobPositionFactory(published=True, department=sales, title="Field Sales Executive")
    JobPositionFactory(closed=True, department=DepartmentFactory(name="Ops", slug="ops"))
    JobPositionFactory(title="Draft")
    JobPositionFactory(status=JobPosition.Status.ARCHIVED)
    JobPositionFactory(published=True).soft_delete()
    response = api_client.get(LIST)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"data", "meta"}
    assert [card["uid"] for card in body["data"]] == [str(open_position.uid)]
    assert set(body["data"][0]) == {"uid", "slug", "title", "department", "location", "employment_type", "experience_required", "application_deadline", "published_at", "is_open"}
    assert body["data"][0]["employment_type"] == "Full-time" and body["data"][0]["is_open"] is True
    assert body["meta"] == {"count": 1, "departments": [{"name": "Sales", "slug": "sales"}], "accepting_general_applications": False, "intro": "Join us."}
    assert "id" not in body["data"][0]


def test_department_filter(api_client):
    JobPositionFactory(published=True, department=DepartmentFactory(slug="sales"))
    JobPositionFactory(published=True, department=DepartmentFactory(slug="ops"))
    body = api_client.get(LIST, {"department": "sales"}).json()
    assert body["meta"]["count"] == 1 and len(body["meta"]["departments"]) == 2


def test_detail_serves_published_and_closed_only(api_client):
    JobPositionFactory(published=True, slug="open-role", responsibilities="a\n\n b \n")
    JobPositionFactory(closed=True, slug="closed-role")
    JobPositionFactory(slug="draft-role")
    JobPositionFactory(status=JobPosition.Status.ARCHIVED, slug="archived-role")
    open_role = api_client.get(f"{LIST}open-role/").json()
    assert open_role["data"]["is_open"] is True and open_role["data"]["responsibilities"] == ["a", "b"]
    assert open_role["meta"]["schema"]["@type"] == "JobPosting"
    assert api_client.get(f"{LIST}closed-role/").json()["data"]["is_open"] is False
    for slug in ("draft-role", "archived-role", "unknown"):
        response = api_client.get(f"{LIST}{slug}/")
        assert response.status_code == 404 and response.json()["code"] == "not_found"


def test_caching_headers_etag_and_304(api_client, django_assert_max_num_queries):
    for _ in range(5):
        JobPositionFactory(published=True)
    first = api_client.get(LIST)
    assert first["Cache-Control"] == "public, max-age=60" and first["X-Cache"] == "MISS" and first["ETag"]
    with django_assert_max_num_queries(0):
        second = api_client.get(LIST)
    assert second["X-Cache"] == "HIT" and second.json() == first.json()
    assert api_client.get(LIST, HTTP_IF_NONE_MATCH=first["ETag"]).status_code == 304


def test_list_query_count_is_flat(api_client, django_assert_max_num_queries):
    for _ in range(12):
        JobPositionFactory(published=True)
    with django_assert_max_num_queries(4):
        assert len(api_client.get(LIST).json()["data"]) == 12


def test_staff_writes_invalidate_the_public_cache(api_client, auth_client, make_user):
    position = JobPositionFactory(published=True, title="Old title")
    api_client.get(LIST)
    api_client.get(f"{LIST}{position.slug}/")
    staff = auth_client(make_user(grants={"job_positions": "*"}))
    staff.patch(f"/api/v1/careers/positions/{position.uid}/", {"title": "New title"}, format="json")
    assert api_client.get(LIST).json()["data"][0]["title"] == "New title"
    assert api_client.get(f"{LIST}{position.slug}/").json()["data"]["title"] == "New title"
    staff.post(f"/api/v1/careers/positions/{position.uid}/unpublish/")
    assert api_client.get(LIST).json()["data"] == []
    assert api_client.get(f"{LIST}{position.slug}/").status_code == 404


def test_company_edits_invalidate_the_public_cache(api_client, auth_client, make_user):
    CompanyProfileFactory(careers_intro="Before")
    assert api_client.get(LIST).json()["meta"]["intro"] == "Before"
    auth_client(make_user(grants={"company": ["view", "edit"]})).patch("/api/v1/company/profile/", {"careers_intro": "After"}, format="json")
    assert api_client.get(LIST).json()["meta"]["intro"] == "After"


def test_anonymous_and_throttled_as_public_read():
    from careers.views.public import PublicJobPositionDetailView, PublicJobPositionListView

    request = type("R", (), {"method": "GET"})()
    for view in (PublicJobPositionListView(), PublicJobPositionDetailView()):
        assert view.authentication_classes == [] and view.get_throttle_scope(request) == "public_read"


def test_sitemap_entries():
    from careers.services.sitemap import sitemap_entries

    live = JobPositionFactory(published=True, slug="live")
    JobPositionFactory(published=True, slug="hidden", noindex=True)
    JobPositionFactory(closed=True, slug="closed")
    JobPositionFactory(slug="draft")
    entries = sitemap_entries()
    assert entries == [{"path": "/career/live", "lastmod": live.updated_at.isoformat(), "kind": "job_position"}]
