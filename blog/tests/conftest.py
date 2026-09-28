"""Blog tests: media under tmp_path, the revalidation client on its fake backend (never a real HTTP call)."""

import pytest

from blog.services import revalidation


@pytest.fixture(autouse=True)
def media_roots(settings, tmp_path):
    settings.MEDIA_PUBLIC_BACKEND = "local"
    settings.PUBLIC_MEDIA_ROOT = tmp_path / "public"
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    return tmp_path


@pytest.fixture(autouse=True)
def fake_revalidation(settings):
    settings.BLOG_REVALIDATE_BACKEND = "fake"
    revalidation.FAKE.sent.clear()
    revalidation.FAKE.status = 200
    revalidation.FAKE.fail_with = None
    yield revalidation.FAKE
    revalidation.FAKE.sent.clear()
