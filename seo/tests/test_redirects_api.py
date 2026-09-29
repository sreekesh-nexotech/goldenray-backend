"""seo/redirects/ (staff CRUD) and the public seo/redirects/ list for the Next.js build."""

import pytest

from audit.models import AuditLog
from seo.models import Redirect
from seo.tests.factories import RedirectFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/seo/redirects/"
PUBLIC = "/api/public/v1/seo/redirects/"


@pytest.fixture
def client(auth_client, make_user):
    return auth_client(make_user(grants={"seo": ["view", "edit"]}))


def test_permissions(api_client, auth_client, make_user):
    redirect = RedirectFactory()
    assert api_client.get(URL).status_code == 401
    viewer = auth_client(make_user(grants={"seo": ["view"]}))
    assert viewer.get(URL).status_code == 200
    assert viewer.post(URL, {"from_path": "/a", "to_path": "/b"}, format="json").status_code == 403
    assert viewer.delete(f"{URL}{redirect.uid}/").status_code == 403


def test_crud_and_stale_version(client):
    response = client.post(URL, {"from_path": "/old-blog", "to_path": "/blog", "status_code": 301}, format="json")
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["permanent"] is True and body["hits"] == 0
    assert AuditLog.objects.filter(action="seo.redirect_created").exists()
    url = f"{URL}{body['uid']}/"
    assert client.patch(url, {"status_code": 302, "expected_version": 1}, format="json").json()["permanent"] is False
    assert client.patch(url, {"note": "x", "expected_version": 1}, format="json").json()["code"] == "stale_version"
    assert client.delete(url).status_code == 204 and not Redirect.objects.exists()


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"from_path": "old", "to_path": "/b"}, "from_path"),
        ({"from_path": "//evil", "to_path": "/b"}, "from_path"),
        ({"from_path": "/a?x=1", "to_path": "/b"}, "from_path"),
        ({"from_path": "/a", "to_path": "javascript:alert(1)"}, "to_path"),
        ({"from_path": "/a", "to_path": "https://"}, "to_path"),
        ({"from_path": "/a", "to_path": "/b", "status_code": 303}, "status_code"),
    ],
)
def test_validation(client, payload, field):
    response = client.post(URL, payload, format="json")
    assert response.status_code == 400 and field in response.json()["errors"]


def test_duplicates_and_loops(client):
    RedirectFactory(from_path="/a", to_path="/b")
    RedirectFactory(from_path="/b", to_path="/c")
    assert client.post(URL, {"from_path": "/a", "to_path": "/z"}, format="json").json()["code"] == "redirect_exists"
    assert client.post(URL, {"from_path": "/x", "to_path": "/x"}, format="json").json()["code"] == "redirect_loop"
    assert client.post(URL, {"from_path": "/c", "to_path": "/a"}, format="json").json()["code"] == "redirect_loop"
    assert client.post(URL, {"from_path": "/c", "to_path": "https://flarize.com/new"}, format="json").status_code == 201
    chain = RedirectFactory(from_path="/d", to_path="/e")
    assert client.patch(f"{URL}{chain.uid}/", {"to_path": "/d"}, format="json").json()["code"] == "redirect_loop"


def test_public_list_shape_pagination_and_cache(api_client, client):
    RedirectFactory(from_path="/a", to_path="/b", status_code=301)
    RedirectFactory(from_path="/c", to_path="/d", status_code=307)
    deleted = RedirectFactory(from_path="/gone", to_path="/x")
    deleted.soft_delete()
    first = api_client.get(PUBLIC)
    assert first.status_code == 200 and first["Cache-Control"] == "public, max-age=60"
    assert first.json()["results"] == [{"from_path": "/a", "to_path": "/b", "status_code": 301, "permanent": True}, {"from_path": "/c", "to_path": "/d", "status_code": 307, "permanent": False}]
    assert api_client.get(PUBLIC)["X-Cache"] == "HIT"
    client.post(URL, {"from_path": "/e", "to_path": "/f"}, format="json")
    assert api_client.get(PUBLIC).json()["count"] == 3
    assert len(api_client.get(f"{PUBLIC}?page_size=2").json()["results"]) == 2


def test_public_list_has_no_n_plus_one(api_client, django_assert_max_num_queries):
    RedirectFactory.create_batch(3)
    with django_assert_max_num_queries(3):
        api_client.get(PUBLIC)
    RedirectFactory.create_batch(30)
    with django_assert_max_num_queries(3):
        assert api_client.get(f"{PUBLIC}?page_size=200").json()["count"] == 33


def test_public_throttle_scope():
    from seo.views.public import PublicRedirectViewSet, SitemapEntriesViewSet

    for view in (PublicRedirectViewSet(), SitemapEntriesViewSet()):
        assert view.authentication_classes == [] and view.get_throttle_scope(type("R", (), {"method": "GET"})()) == "public_read"
