"""``EMI_PRICE_SOURCE = PACK_RELEASE`` serves the prices of the current PackRelease (DV-83, DV-103).

EMI reads the release through the documented packs read ``packs.services.public.emi_size_packs``; the wiring is a
static import that import-linter checks (website content may read configuration; configuration never imports EMI).
"""

from __future__ import annotations

from decimal import Decimal

import grimp
import pytest
from django.test import override_settings

from emi import checks
from emi.services import calculator, pack_release, price_sources
from packs.models import ReleasePack
from packs.services import releases
from packs.tests import factories
from pricing.services import provider

pytestmark = pytest.mark.django_db
CONFIG = "/api/public/v1/calculators/emi/config/"
CALCULATE = "/api/public/v1/calculators/emi/"


@pytest.fixture
def world(db):
    provider.register()  # the pricing price provider packs builds releases with (packs/tests/conftest.py)
    return factories.world()


def test_the_startup_provider_is_the_pack_release_reader():
    pack_release.install()
    assert price_sources.has_provider() and price_sources._provider.function is pack_release.release_sizes
    assert price_sources._provider.cache_namespaces == ("packs",)


@override_settings(EMI_PRICE_SOURCE="PACK_RELEASE")
def test_pack_release_source_returns_the_release_prices(world, api_client):
    pack_release.install()
    assert checks.check_price_source() == []
    assert price_sources.active_sizes() == []  # nothing published yet
    release = releases.publish(user=None)
    (option,) = price_sources.active_sizes()
    (pack,) = ReleasePack.objects.filter(release=release)
    assert option.uid == pack.key == "ongrid-value-3" and option.label == pack.display_name
    assert option.system_cost == pack.customer_price_incl_gst == Decimal("229000.00") and option.capacity_kw == Decimal("3.00")
    assert option.price_per_kw == Decimal("76333.33") and option.created_at == release.published_at
    assert price_sources.cache_namespaces() == ("packs",) and calculator.cache_namespaces() == ["emi:config", "packs"]

    tile = api_client.get(CONFIG).json()["system_sizes"][0]
    assert tile["uid"] == "ongrid-value-3" and tile["system_cost"] == 229000.0 and tile["capacity_kw"] == "3.00"
    body = api_client.post(CALCULATE, {"size_uid": "ongrid-value-3", "tenure_years": 5}, format="json").json()
    assert body["system"]["size_uid"] == "ongrid-value-3" and body["system"]["system_cost"] == 229000.0


@override_settings(EMI_PRICE_SOURCE="MANUAL")
def test_manual_source_ignores_the_release(world):
    pack_release.install()
    releases.publish(user=None)
    assert price_sources.active_sizes() == [] and price_sources.cache_namespaces() == ()


def test_the_dependency_is_a_static_import_from_emi_to_packs_only():
    graph = grimp.build_graph("emi", "packs")
    assert graph.direct_import_exists(importer="emi.services.pack_release", imported="packs.services.public")
    assert not graph.chain_exists(importer="packs", imported="emi", as_packages=True)
