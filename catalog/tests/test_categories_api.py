"""catalog/categories/ — CRUD, JSON Schema for attributes, spec-kind and in-use guards."""

from decimal import Decimal

import pytest

from catalog.models import Category
from catalog.tests.factories import CategoryFactory, ComponentFactory, panel, panel_category

pytestmark = pytest.mark.django_db
URL = "/api/v1/catalog/categories/"
NEW = {"slug": "dcdb", "name": "DCDB", "bom_role": "PROTECTION", "gst_rate": "0.18", "unit": "NOS", "sku_prefix": "dcdb", "hsn_code": "85371000"}
SCHEMA = {"type": "object", "properties": {"phase": {"type": "string", "enum": ["1P", "3P"]}}, "required": ["phase"]}


def detail(category):
    return f"{URL}{category.uid}/"


class TestPermissions:
    def test_anonymous_and_forbidden(self, api_client, outsider, viewer):
        category = CategoryFactory()
        assert api_client.get(URL).status_code == 401
        assert outsider.get(URL).status_code == 403
        assert viewer.get(URL).status_code == 200
        assert viewer.post(URL, NEW, format="json").status_code == 403
        assert viewer.patch(detail(category), {"name": "x"}, format="json").status_code == 403
        assert viewer.delete(detail(category)).status_code == 403


class TestCrud:
    def test_create_and_read(self, client):
        response = client.post(URL, {**NEW, "attribute_schema": SCHEMA}, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["sku_prefix"] == "DCDB" and body["gst_rate"] == "0.1800" and body["spec_kind"] is None and body["attribute_schema"] == SCHEMA
        assert client.get(URL, {"bom_role": "PROTECTION"}).json()["count"] == 1
        panel_category()
        assert client.get(detail(Category.objects.get(slug="panel"))).json()["spec_kind"] == "panel"

    @pytest.mark.parametrize(
        "payload,field",
        [
            ({"gst_rate": "18"}, "gst_rate"),
            ({"sku_prefix": "1AB"}, "sku_prefix"),
            ({"bom_role": "ROOF"}, "bom_role"),
            ({"attribute_schema": {"type": "array"}}, "attribute_schema"),
            ({"attribute_schema": {"type": "object", "properties": {"x": {"type": "nonsense"}}}}, "attribute_schema"),
            ({"attribute_schema": []}, "attribute_schema"),
        ],
    )
    def test_validation(self, client, payload, field):
        response = client.post(URL, {**NEW, **payload}, format="json")
        assert response.status_code == 400 and field in response.json()["errors"], response.json()

    def test_conflicts(self, client):
        CategoryFactory(slug="dcdb")
        assert client.post(URL, NEW, format="json").json()["code"] == "category_slug_taken"
        CategoryFactory(slug="other", sku_prefix="ACDB")
        assert client.post(URL, {**NEW, "slug": "acdb", "sku_prefix": "acdb"}, format="json").json()["code"] == "category_sku_prefix_taken"

    def test_schema_change_revalidates_components(self, client):
        category = CategoryFactory()
        ComponentFactory(category=category, sku="d1", attributes={"phase": "2P"})
        response = client.patch(detail(category), {"attribute_schema": SCHEMA}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "attribute_schema_conflict" and "d1" in response.json()["errors"]
        ComponentFactory(category=category, sku="d2", attributes={"phase": "1P"})
        Category.objects.get(pk=category.pk)
        from catalog.models import Component

        Component.objects.filter(sku="d1").update(attributes={"phase": "3P"})
        assert client.patch(detail(category), {"attribute_schema": SCHEMA, "expected_version": 1}, format="json").json()["version"] == 2

    def test_role_change_to_another_spec_table_is_refused_while_specs_exist(self, client):
        component = panel()
        category = component.category
        response = client.patch(detail(category), {"bom_role": "MAIN_INVERTER"}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "spec_kind_change"
        assert client.patch(detail(category), {"gst_rate": "0.12"}, format="json").json()["gst_rate"] == "0.1200"
        empty = CategoryFactory()
        assert client.patch(detail(empty), {"bom_role": "MAIN_INVERTER"}, format="json").status_code == 200

    def test_stale_version_and_noop(self, client):
        category = CategoryFactory(version=2, gst_rate=Decimal("0.18"))
        assert client.patch(detail(category), {"gst_rate": "0.18"}, format="json").json()["version"] == 2
        response = client.patch(detail(category), {"name": "x", "expected_version": 1}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"

    def test_delete_guard(self, client):
        category = CategoryFactory()
        component = ComponentFactory(category=category)
        assert client.delete(detail(category)).json()["code"] == "category_in_use"
        component.soft_delete()
        assert client.delete(detail(category)).status_code == 204 and not Category.objects.filter(pk=category.pk).exists()

    def test_list_query_budget(self, client, django_assert_max_num_queries):
        CategoryFactory.create_batch(30)
        with django_assert_max_num_queries(10):
            assert client.get(URL, {"page_size": 100}).json()["count"] == 30
