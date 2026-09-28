"""Media tests write to a per-test directory, never to var/."""

import pytest


@pytest.fixture(autouse=True)
def media_roots(settings, tmp_path):
    settings.MEDIA_PUBLIC_BACKEND = "local"
    settings.PUBLIC_MEDIA_ROOT = tmp_path / "public"
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    settings.PUBLIC_MEDIA_URL = "/media/public/"
    settings.USE_X_ACCEL = False
    return tmp_path
