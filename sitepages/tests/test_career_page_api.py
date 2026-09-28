"""career-page/ — the career page (slug ``career``) through the ``career_page`` grant, and nothing else."""

import pytest

from sitepages.services.registry import sync_registry
from sitepages.tests.factories import PageFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/career-page/"


@pytest.fixture
def career():
    sync_registry()
    from sitepages.models import Page

    return Page.objects.get(slug="career")


@pytest.fixture
def hr(auth_client, make_user):
    return auth_client(make_user(grants={"career_page": "*"}))


def test_anonymous_401_and_other_grants_403(api_client, auth_client, make_user, career):
    assert api_client.get(URL).status_code == 401
    pages_only = auth_client(make_user(grants={"pages": "*"}))
    assert pages_only.get(URL).status_code == 403
    assert pages_only.patch(f"{URL}{career.uid}/text-slots/hero_title/", {"value": "x"}, format="json").status_code == 403


@pytest.mark.parametrize(
    "method,suffix,needed",
    [("get", None, "view"), ("get", "", "view"), ("get", "seo/", "view"), ("patch", "seo/", "edit"), ("patch", "text-slots/hero_title/", "edit"), ("post", "publish/", "publish")],
)
def test_each_action_needs_its_career_page_permission(auth_client, make_user, career, method, suffix, needed):
    client = auth_client(make_user(grants={"career_page": [action for action in ("view", "edit", "publish") if action != needed], "pages": "*"}))
    target = URL if suffix is None else f"{URL}{career.uid}/{suffix}"
    assert getattr(client, method)(target, {}, format="json").status_code == 403


def test_only_the_career_page_is_reachable(hr, career):
    other = PageFactory()
    body = hr.get(URL).json()
    assert body["count"] == 1 and body["results"][0]["slug"] == "career"
    assert hr.get(f"{URL}{other.uid}/").status_code == 404
    assert hr.patch(f"{URL}{other.uid}/seo/", {"seo_title": "x"}, format="json").status_code == 404


def test_hr_maintains_the_career_page(hr, career, api_client):
    detail = hr.get(f"{URL}{career.uid}/").json()
    assert {slot["key"] for slot in detail["text_slots"]} == {"hero_title", "hero_subtitle"} and [slot["key"] for slot in detail["image_slots"]] == ["hero_background"]
    assert hr.patch(f"{URL}{career.uid}/text-slots/hero_title/", {"value": "Build Kerala's solar future"}, format="json").status_code == 200
    assert hr.patch(f"{URL}{career.uid}/seo/", {"seo_title": "Careers at Flarize"}, format="json").json()["version"] == 2
    assert hr.get(f"{URL}{career.uid}/preview/").json()["content"]["text"] == {"hero_title": "Build Kerala's solar future"}
    assert hr.post(f"{URL}{career.uid}/unpublish/").json()["status"] == "DRAFT"
    assert api_client.get("/api/public/v1/pages/career/").status_code == 404
    assert hr.post(f"{URL}{career.uid}/publish/").json()["status"] == "PUBLISHED"
    assert api_client.get("/api/public/v1/pages/career/").json()["data"]["text"] == {"hero_title": "Build Kerala's solar future"}


def test_archive_restore_verify_and_sort_order_are_not_offered(hr, career):
    for suffix in ("archive/", "restore/", "verify/"):
        assert hr.post(f"{URL}{career.uid}/{suffix}").status_code == 404
    assert hr.patch(f"{URL}{career.uid}/", {"sort_order": 1}, format="json").status_code == 403
