"""catalog/brands/ — CRUD, case-insensitive names, relabelling on rename, in-use guard."""

import pytest

from audit.models import AuditLog
from catalog.models import Brand, ComponentChange
from catalog.tests.factories import BrandFactory, ComponentFactory
from media.tests.factories import MediaAssetFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/catalog/brands/"


def detail(brand, suffix=""):
    return f"{URL}{brand.uid}/{suffix}"


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        brand = BrandFactory()
        assert api_client.get(URL).status_code == 401
        assert api_client.post(URL, {"name": "x"}, format="json").status_code == 401
        assert api_client.delete(detail(brand)).status_code == 401

    def test_missing_permission_is_403(self, outsider, viewer):
        brand = BrandFactory()
        assert outsider.get(URL).status_code == 403
        assert viewer.get(URL).status_code == 200
        assert viewer.post(URL, {"name": "Waaree"}, format="json").status_code == 403
        assert viewer.patch(detail(brand), {"country": "IN"}, format="json").status_code == 403
        assert viewer.delete(detail(brand)).status_code == 403

    def test_scope_all_sees_every_brand(self, auth_client, make_user):
        BrandFactory.create_batch(3)
        client = auth_client(make_user(grants={"catalog": ["view"]}, scopes={"catalog": "all"}))
        assert client.get(URL).json()["count"] == 3


class TestCrud:
    def test_create_list_and_detail(self, client, catalog_user):
        logo = MediaAssetFactory()
        response = client.post(URL, {"name": "  Waaree   Energies ", "country": "IN", "website": "https://waaree.com", "logo": str(logo.uid)}, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["name"] == "Waaree Energies" and body["slug"] == "waaree-energies" and body["logo"]["uid"] == str(logo.uid) and body["version"] == 1
        assert client.get(detail(Brand.objects.get())).json()["name"] == "Waaree Energies"
        assert AuditLog.objects.get(action="catalog.brand_created").actor == catalog_user

    def test_name_is_unique_case_insensitively(self, client):
        BrandFactory(name="Waaree")
        response = client.post(URL, {"name": "WAAREE"}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "brand_name_taken"

    def test_slug_taken_and_deleted_name_reusable(self, client):
        BrandFactory(name="Other", slug="waaree")
        assert client.post(URL, {"name": "Waaree", "slug": "waaree"}, format="json").json()["code"] == "brand_slug_taken"
        assert client.post(URL, {"name": "Waaree"}, format="json").json()["slug"] == "waaree-2"
        gone = BrandFactory(name="Gone")
        gone.soft_delete()
        assert client.post(URL, {"name": "Gone"}, format="json").status_code == 201

    @pytest.mark.parametrize("payload,field", [({"name": ""}, "name"), ({"name": "x", "website": "not-a-url"}, "website"), ({"name": "x", "slug": "bad slug"}, "slug")])
    def test_validation_envelope(self, client, payload, field):
        response = client.post(URL, payload, format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error" and field in response.json()["errors"]

    def test_private_logo_is_refused(self, client):
        response = client.post(URL, {"name": "x", "logo": str(MediaAssetFactory(visibility="PRIVATE", cdn_url="").uid)}, format="json")
        assert response.status_code == 400 and "logo" in response.json()["errors"]

    def test_rename_relabels_components_that_printed_the_old_name(self, client):
        brand = BrandFactory(name="Renewsys")
        plain = ComponentFactory(brand=brand, brand_label="Renewsys")
        custom = ComponentFactory(brand=brand, brand_label="RENEWSYS INDIA")
        response = client.patch(detail(brand), {"name": "RenewSys", "expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["version"] == 2
        plain.refresh_from_db()
        custom.refresh_from_db()
        assert plain.brand_label == "RenewSys" and plain.version == 2 and custom.brand_label == "RENEWSYS INDIA"
        assert ComponentChange.objects.get(component=plain, field="brand_label").new == "RenewSys"

    def test_update_noop_and_stale_version(self, client):
        brand = BrandFactory(name="Waaree", version=3)
        assert client.patch(detail(brand), {"name": "Waaree"}, format="json").json()["version"] == 3
        response = client.patch(detail(brand), {"country": "IN", "expected_version": 2}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"
        assert client.patch(detail(brand), {"name": ""}, format="json").status_code == 400

    def test_rename_to_taken_name_conflicts(self, client):
        BrandFactory(name="Adani")
        brand = BrandFactory(name="Adani Solar")
        assert client.patch(detail(brand), {"name": "adani"}, format="json").json()["code"] == "brand_name_taken"

    def test_delete_refused_while_in_use(self, client):
        brand = BrandFactory()
        component = ComponentFactory(brand=brand)
        response = client.delete(detail(brand))
        assert response.status_code == 409 and response.json()["code"] == "brand_in_use"
        component.soft_delete()
        assert client.delete(detail(brand)).status_code == 204
        assert not Brand.objects.exists() and AuditLog.objects.filter(action="catalog.brand_deleted").count() == 1

    def test_list_filters_search_and_query_budget(self, client, django_assert_max_num_queries):
        BrandFactory(name="Waaree", is_active=False)
        BrandFactory.create_batch(20, logo=MediaAssetFactory())
        assert [row["name"] for row in client.get(URL, {"search": "waar"}).json()["results"]] == ["Waaree"]
        assert client.get(URL, {"filter[is_active]": "false"}).json()["count"] == 1
        with django_assert_max_num_queries(12):
            assert client.get(URL, {"page_size": 50}).json()["count"] == 21
