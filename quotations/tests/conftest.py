"""Quotations test fixtures.

``flarize_world`` imports the real Flarize catalog, prices, pack configuration, engine configurations and quotation
content, and publishes PriceRelease #1 / PackRelease #1 — once per test module, inside an outer transaction that is
rolled back when the module ends (every test runs in a savepoint of it, as pytest-django's ``db`` does).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import transaction

from quotations.tests import flarize

SALES_HEAD = {"quotations": "*", "customers": "*", "pricing_internal": ["view"], "quotation_content": "*"}
EXECUTIVE = {"quotations": ["view", "create", "edit", "issue", "revise"], "customers": ["view", "create", "edit"]}


@pytest.fixture(scope="module")
def flarize_world(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        atomic = transaction.atomic()
        atomic.__enter__()
        try:
            yield flarize.import_world()
        finally:
            transaction.set_rollback(True)
            atomic.__exit__(None, None, None)


@pytest.fixture
def world(flarize_world, db):
    return flarize_world


@pytest.fixture
def document_storage(settings, tmp_path):
    settings.MEDIA_PUBLIC_BACKEND = "local"
    settings.PUBLIC_MEDIA_ROOT = tmp_path / "public"
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    settings.USE_X_ACCEL = False
    settings.DOCUMENTS_RENDERER = "stub"
    return tmp_path


@pytest.fixture
def head_user(make_user):
    return make_user(grants=SALES_HEAD, scopes={"quotations": "all", "customers": "all"})


@pytest.fixture
def executive_user(make_user):
    return make_user(grants=EXECUTIVE, scopes={"quotations": "owned", "customers": "all"})


@pytest.fixture
def head(auth_client, head_user):
    return auth_client(head_user)


@pytest.fixture
def executive(auth_client, executive_user):
    return auth_client(executive_user)


@pytest.fixture
def outsider(auth_client, make_user):
    return auth_client(make_user(grants={"customers": ["view"]}, scopes={"customers": "all"}))


@pytest.fixture
def customer(db):
    from customers.tests.factories import CustomerFactory

    return CustomerFactory(name="Test Customer One", address="Test Street 1", pincode="688001", district="Alappuzha", current_bill=Decimal("6000"), bill_cycle="BIMONTHLY")


def quotation_data(customer, **overrides) -> dict:
    """The first golden case (on-grid 3 kW VALUE, flat roof, residential subsidy) as a create body."""
    data = {
        "customer_uid": str(customer.uid),
        "system_type": "ONGRID",
        "tier": "VALUE",
        "size_key": "3",
        "roof_type": "FLAT",
        "distance_km": "60",
        "vehicle_type": "ACE",
        "subsidy_type": "residential",
        "language": "en",
    }
    data.update(overrides)
    return data
