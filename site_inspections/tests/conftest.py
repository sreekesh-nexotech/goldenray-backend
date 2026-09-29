"""Site-inspection fixtures: the seeded role grants (PLAN §3.2), media roots in a per-test directory, a clean fake
Twilio outbox."""

import pytest

from leads.services import twilio_verify

ENGINEER = {"grants": {"dashboard": ["view"], "site_inspections": ["view", "edit", "submit"], "media": ["create"], "customers": ["view"]}, "scopes": {"site_inspections": "assigned"}}
SALES = {
    "grants": {"customers": ["view", "create", "edit"], "site_inspections": ["view", "create"], "leads": ["view"]},
    "scopes": {"customers": "owned", "site_inspections": "owned", "leads": "owned"},
}
HEAD = {"grants": {"site_inspections": ["view", "assign", "approve", "release"], "customers": ["view"]}, "scopes": {"site_inspections": "all", "customers": "all"}}
ADMIN = {"grants": {"site_inspections": "*", "customers": "*", "documents": []}, "scopes": {"site_inspections": "all", "customers": "all"}}


@pytest.fixture(autouse=True)
def media_roots(settings, tmp_path):
    settings.MEDIA_PUBLIC_BACKEND = "local"
    settings.PUBLIC_MEDIA_ROOT = tmp_path / "public"
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    settings.PUBLIC_MEDIA_URL = "/media/public/"
    settings.USE_X_ACCEL = False
    return tmp_path


@pytest.fixture(autouse=True)
def _fake_twilio_outbox():
    twilio_verify.SENT.clear()
    yield
    twilio_verify.SENT.clear()


def _role(make_user, spec):
    grants = {module: actions for module, actions in spec["grants"].items() if actions}
    return make_user(grants=grants, scopes=spec["scopes"])


@pytest.fixture
def engineer(make_user):
    return _role(make_user, ENGINEER)


@pytest.fixture
def sales(make_user):
    return _role(make_user, SALES)


@pytest.fixture
def head(make_user):
    return _role(make_user, HEAD)


@pytest.fixture
def admin(make_user):
    return _role(make_user, ADMIN)
