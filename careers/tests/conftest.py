"""Careers tests write media to a per-test directory, never to var/."""

import pytest


@pytest.fixture(autouse=True)
def media_roots(settings, tmp_path):
    settings.MEDIA_PUBLIC_BACKEND = "local"
    settings.PUBLIC_MEDIA_ROOT = tmp_path / "public"
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    settings.PUBLIC_MEDIA_URL = "/media/public/"
    settings.USE_X_ACCEL = False
    return tmp_path


@pytest.fixture
def hr(make_user):
    """The seeded HR grants for careers: positions, applications and departments in full."""
    return make_user(grants={"job_positions": "*", "applications": "*", "departments": "*"})


@pytest.fixture
def hr_client(auth_client, hr):
    return auth_client(hr)
