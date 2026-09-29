"""Every attendance endpoint: anonymous 401, a caller without the registry action 403 (default deny), OpenAPI presence."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db

READS = [
    "/api/v1/attendance/days/",
    "/api/v1/attendance/raw/",
    "/api/v1/attendance/corrections/",
    "/api/v1/attendance/calendar/",
    "/api/v1/attendance/calendar/all/",
    "/api/v1/attendance/day/",
    "/api/v1/attendance/date-ranges/",
    "/api/v1/attendance/dashboard/summary/",
    "/api/v1/attendance/dashboard/recent-punches/",
    "/api/v1/attendance/dashboard/trend/",
]
MANAGE = ["/api/v1/attendance/process/", "/api/v1/attendance/process-all/", "/api/v1/attendance/recalculate/"]


@pytest.mark.parametrize("path", READS + [f"/api/v1/attendance/employees/{'0' * 8}-0000-0000-0000-{'0' * 12}/timeline/"])
def test_reads_need_a_session_and_attendance_view(api_client, auth_client, make_user, path):
    assert api_client.get(path).status_code == 401
    response = auth_client(make_user(grants={"employees": "*", "leave": "*"}, scopes={"employees": "all", "leave": "all"})).get(path)
    assert response.status_code == 403 and response.json()["code"] == "permission_denied"


@pytest.mark.parametrize("path", MANAGE)
def test_recomputes_need_manage(api_client, auth_client, make_user, path):
    assert api_client.post(path, {}, format="json").status_code == 401
    viewer = make_user(grants={"attendance": ["view", "edit", "export"]}, scopes={"attendance": "all"})
    assert auth_client(viewer).post(path, {}, format="json").status_code == 403


def test_corrections_need_edit(auth_client, make_user):
    viewer = make_user(grants={"attendance": ["view", "export", "manage"]}, scopes={"attendance": "all"})
    client = auth_client(viewer)
    assert client.post("/api/v1/attendance/corrections/", {}, format="json").status_code == 403
    assert client.post(f"/api/v1/attendance/corrections/{'0' * 8}-0000-0000-0000-{'0' * 12}/revoke/", {}, format="json").status_code == 403


def test_a_role_without_a_scope_sees_nothing(auth_client, make_user, world):
    """A grant without a stated scope gets the narrowest (``self``); no linked employee means nothing at all."""
    client = auth_client(make_user(grants={"attendance": ["view"]}))
    assert client.get("/api/v1/attendance/days/").json()["count"] == 0
    assert client.get("/api/v1/attendance/raw/").json()["results"] == []
    assert client.get("/api/v1/attendance/day/").json()["rows"] == []


def test_every_endpoint_is_in_the_schema(auth_client, make_user):
    from django.test import override_settings

    with override_settings(API_DOCS_PUBLIC=True):
        schema = auth_client(make_user()).get("/api/schema/v1/", {"format": "json"})
    assert schema.status_code == 200
    paths = schema.json()["paths"] if schema["Content-Type"].startswith("application/json") else None
    if paths is None:  # YAML
        import yaml

        paths = yaml.safe_load(schema.content)["paths"]
    for path in [*READS, *MANAGE, "/api/v1/attendance/reports/daily/", "/api/v1/attendance/reports/office/", "/api/v1/attendance/corrections/{uid}/revoke/"]:
        assert path in paths, path
