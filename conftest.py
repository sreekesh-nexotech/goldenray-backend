"""Shared pytest fixtures for every app.

* ``api_client`` — an unauthenticated DRF ``APIClient``;
* ``make_user(grants=None, scopes=None, role=None, **fields)`` — a live, active user whose role holds ``grants``
  (``{module: [actions]}`` or ``{module: "*"}``) and ``scopes``;
* ``auth_client(user)`` — an ``APIClient`` carrying a real RS256 access token for ``user`` bound to a fresh session
  (``client.tokens`` holds the issued pair: ``access``, ``refresh``, ``session``);
* ``drain_outbox`` — runs the outbox drain synchronously and returns the outcome counts.

The cache is cleared around every test (LocMem in tests, so concurrent runs never share state).
"""

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient


@pytest.fixture(autouse=True)
def _isolated_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def make_user(db):
    from accounts.registry import MODULES
    from accounts.tests.factories import RoleFactory, UserFactory

    def _make_user(grants=None, scopes=None, role=None, **fields):
        if role is None:
            permissions = {module: (list(MODULES[module].actions) if actions == "*" else actions) for module, actions in (grants or {}).items()}
            role = RoleFactory(permissions=permissions, scopes=scopes or {})
        elif grants is not None or scopes is not None:
            raise ValueError("Pass either role= or grants=/scopes=, not both.")
        return UserFactory(role=role, **fields)

    return _make_user


@pytest.fixture
def auth_client(db):
    from accounts.services.sessions import start_session

    def _auth_client(user):
        client = APIClient()
        client.tokens = start_session(user, ip="127.0.0.1", user_agent="pytest")
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {client.tokens.access}")
        return client

    return _auth_client


@pytest.fixture
def drain_outbox(db):
    from core.outbox import drain_outbox_sync

    return drain_outbox_sync
