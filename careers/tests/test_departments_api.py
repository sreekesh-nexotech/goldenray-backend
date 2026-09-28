"""careers/departments/ — CRUD, live uniqueness, job counts without N+1, delete refused while positions depend on it."""

import pytest

from audit.models import AuditLog
from careers.models import Department, JobPosition
from careers.tests.factories import DepartmentFactory, JobPositionFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/careers/departments/"


def detail(department, suffix=""):
    return f"{URL}{department.uid}/{suffix}"


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        department = DepartmentFactory()
        assert api_client.get(URL).status_code == 401
        assert api_client.post(URL, {"name": "x"}, format="json").status_code == 401
        assert api_client.delete(detail(department)).status_code == 401

    def test_each_action_needs_its_grant(self, auth_client, make_user):
        department = DepartmentFactory()
        viewer = auth_client(make_user(grants={"departments": ["view"]}))
        assert viewer.get(URL).status_code == 200
        assert viewer.get(detail(department)).status_code == 200
        assert viewer.post(URL, {"name": "Sales"}, format="json").status_code == 403
        assert viewer.patch(detail(department), {"name": "x"}, format="json").status_code == 403
        assert viewer.delete(detail(department)).status_code == 403
        assert auth_client(make_user(grants={"job_positions": "*"})).get(URL).status_code == 403
        editor = auth_client(make_user(grants={"departments": ["view", "edit"]}))
        assert editor.patch(detail(department), {"description": "x"}, format="json").status_code == 200
        assert editor.delete(detail(department)).status_code == 403

    def test_scope_is_all_for_every_holder(self, auth_client, make_user):
        """departments only allows the ``all`` scope: every holder of the grant sees every department."""
        DepartmentFactory.create_batch(3)
        client = auth_client(make_user(grants={"departments": ["view"]}))
        assert client.get(URL).json()["count"] == 3


class TestCreateAndList:
    def test_create_derives_the_slug_and_audits(self, hr_client, hr):
        response = hr_client.post(URL, {"name": "Field Operations", "description": "Installers"}, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["slug"] == "field-operations" and body["job_count"] == 0 and body["version"] == 1
        assert AuditLog.objects.get(action="careers.department_created").actor == hr

    def test_validation_envelope(self, hr_client):
        response = hr_client.post(URL, {"name": "", "slug": "Bad Slug"}, format="json")
        assert response.status_code == 400
        body = response.json()
        assert body["code"] == "validation_error" and {"name", "slug"} <= set(body["errors"])

    def test_names_and_slugs_are_unique_among_live_rows(self, hr_client):
        DepartmentFactory(name="Sales", slug="sales")
        clash = hr_client.post(URL, {"name": "SALES", "slug": "sales-2"}, format="json")
        assert clash.status_code == 409 and clash.json()["code"] == "department_name_taken"
        clash = hr_client.post(URL, {"name": "Sales Team", "slug": "sales"}, format="json")
        assert clash.status_code == 409 and clash.json()["code"] == "department_slug_taken"
        Department.objects.get(slug="sales").soft_delete()
        assert hr_client.post(URL, {"name": "Sales", "slug": "sales"}, format="json").status_code == 201

    def test_list_counts_jobs_and_filters(self, hr_client, django_assert_max_num_queries):
        engineering = DepartmentFactory(name="Engineering", sort_order=1)
        DepartmentFactory(name="Dormant", is_active=False, sort_order=2)
        JobPositionFactory(department=engineering, published=True)
        JobPositionFactory(department=engineering, status=JobPosition.Status.ARCHIVED)
        JobPositionFactory(department=engineering).soft_delete()
        for _ in range(5):
            JobPositionFactory(department=DepartmentFactory(sort_order=9))
        with django_assert_max_num_queries(8):
            rows = hr_client.get(URL).json()["results"]
        first = rows[0]
        assert first["name"] == "Engineering" and first["job_count"] == 2 and first["open_job_count"] == 1
        assert [row["name"] for row in hr_client.get(URL, {"is_active": "false"}).json()["results"]] == ["Dormant"]
        assert [row["name"] for row in hr_client.get(URL, {"search": "engin"}).json()["results"]] == ["Engineering"]


class TestUpdateAndDelete:
    def test_update_with_expected_version(self, hr_client):
        department = DepartmentFactory(name="Sales")
        response = hr_client.patch(detail(department), {"name": "Sales & Marketing", "expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["version"] == 2
        entry = AuditLog.objects.get(action="careers.department_updated")
        assert entry.before == {"name": "Sales"} and entry.after == {"name": "Sales & Marketing"}

    def test_stale_version(self, hr_client):
        department = DepartmentFactory(version=3)
        response = hr_client.patch(detail(department), {"name": "x", "expected_version": 2}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"
        assert hr_client.delete(f"{detail(department)}?expected_version=2").status_code == 409

    def test_update_to_a_taken_name(self, hr_client):
        DepartmentFactory(name="Sales")
        other = DepartmentFactory(name="Ops")
        response = hr_client.patch(detail(other), {"name": "sales"}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "department_name_taken"

    def test_noop_update_keeps_the_version(self, hr_client):
        department = DepartmentFactory(name="Sales")
        assert hr_client.patch(detail(department), {"name": "Sales"}, format="json").json()["version"] == 1

    def test_delete_is_refused_while_positions_depend_on_it(self, hr_client):
        department = DepartmentFactory()
        JobPositionFactory(department=department, status=JobPosition.Status.ARCHIVED)
        response = hr_client.delete(detail(department))
        assert response.status_code == 409 and response.json()["code"] == "department_in_use"
        assert Department.objects.filter(pk=department.pk).exists()

    def test_delete_soft_deletes_an_empty_department(self, hr_client):
        department = DepartmentFactory()
        assert hr_client.delete(detail(department)).status_code == 204
        assert not Department.objects.filter(pk=department.pk).exists() and Department.all_objects.get(pk=department.pk).deleted_at
        assert AuditLog.objects.filter(action="careers.department_deleted").count() == 1
        assert hr_client.get(detail(department)).status_code == 404
