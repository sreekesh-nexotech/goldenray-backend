"""Agreements test fixtures: users per role, the catalog/fees/quotation world, company master, document storage."""

from __future__ import annotations

import pytest

from agreements.tests import factories

SALES_HEAD = {"agreements": "*", "quotations": ["view"], "customers": ["view"]}
EXECUTIVE = {"agreements": ["view", "create", "edit"], "quotations": ["view"], "customers": ["view"]}
ALL = {"agreements": "all", "quotations": "all", "customers": "all"}
OWNED = {"agreements": "owned", "quotations": "owned", "customers": "all"}


@pytest.fixture
def world(db):
    return {**factories.catalog(), "fees": factories.kseb_fees()}


@pytest.fixture
def company(db):
    from company.tests.factories import BankAccountFactory, CompanyProfileFactory

    profile = CompanyProfileFactory(legal_name="Golden Ray Renewable Energy LLP", trade_name="Golden Ray Renewable Energy", website="https://www.goldenray.co.in", phone_e164="+916282922988")
    account = BankAccountFactory(is_primary=True, account_name="Golden Ray Renewable Energy LLP", account_number="123456789012", ifsc="SBIN0001234")
    return profile, account


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
    return make_user(grants=SALES_HEAD, scopes=ALL)


@pytest.fixture
def executive_user(make_user):
    return make_user(grants=EXECUTIVE, scopes=OWNED)


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
def version(world, head_user):
    return factories.issued_version(owner=head_user)


@pytest.fixture
def price_override(db):
    from core.models import FeatureFlag

    FeatureFlag.objects.update_or_create(key="AGREEMENTS_PRICE_OVERRIDE", defaults={"enabled": True})
    from flarize.cache_utils import bump

    bump("core:flags")
    return True
