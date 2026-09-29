"""The catalog's OpenAPI contract declares exactly the query parameters each endpoint accepts (PLAN §3.1: every list
endpoint declares its filter set)."""

import pytest
from drf_spectacular.generators import SchemaGenerator

pytestmark = pytest.mark.django_db


@pytest.fixture(scope="module")
def paths(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        return SchemaGenerator(api_version="v1").get_schema(request=None, public=True)["paths"]


def params(paths, path, method="get") -> set[str]:
    return {parameter["name"] for parameter in paths[path][method].get("parameters", [])}


def test_history_declares_only_its_cursor(paths):
    assert params(paths, "/api/v1/catalog/components/{uid}/history/") == {"uid", "cursor", "page_size"}


def test_export_declares_every_list_filter(paths):
    listed = params(paths, "/api/v1/catalog/components/") - {"page", "page_size", "ordering"}
    assert listed <= params(paths, "/api/v1/catalog/components/export/")


def test_public_lists_declare_the_website_filters(paths):
    panels = params(paths, "/api/public/v1/products/panels/")
    assert {"slug", "min_efficiency", "max_efficiency", "ordering"} <= panels
    assert "slug" in params(paths, "/api/public/v1/products/inverters/") and "slug" in params(paths, "/api/public/v1/products/batteries/")
