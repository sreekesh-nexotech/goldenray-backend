"""Documents tests store PDFs in a per-test directory and render with the stub renderer unless marked playwright."""

import pytest


@pytest.fixture(autouse=True)
def document_storage(settings, tmp_path):
    settings.MEDIA_PUBLIC_BACKEND = "local"
    settings.PUBLIC_MEDIA_ROOT = tmp_path / "public"
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    settings.USE_X_ACCEL = False
    settings.DOCUMENTS_RENDERER = "stub"
    return tmp_path
