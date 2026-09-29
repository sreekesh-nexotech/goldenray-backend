"""Every app follows the one package layout (CLAUDE.md) and exports the four URL lists."""

import importlib
from pathlib import Path

import pytest
from django.apps import apps
from django.conf import settings

PACKAGES = ["models", "services", "serializers", "views", "tests", "migrations"]
MODULES = ["apps", "urls", "events", "tasks"]
URL_LISTS = ["staff_urlpatterns", "public_urlpatterns", "agent_urlpatterns", "customer_urlpatterns"]


@pytest.mark.parametrize("label", settings.LOCAL_APPS)
def test_app_layout(label):
    config = apps.get_app_config(label)
    assert config.name == label and config.label == label
    root = Path(config.path)
    for package in PACKAGES:
        assert (root / package / "__init__.py").is_file(), f"{label}/{package}/ must be a package"
    for module in MODULES:
        importlib.import_module(f"{label}.{module}")
    urls = importlib.import_module(f"{label}.urls")
    for name in URL_LISTS:
        assert isinstance(getattr(urls, name), list), f"{label}.urls.{name}"
    assert not (root / "models.py").exists() and not (root / "views.py").exists() and not (root / "serializers.py").exists()


def test_surface_specific_lists():
    assert isinstance(importlib.import_module("devices.urls").iclock_urlpatterns, list)  # the ADMS receiver (devices WP)
    assert isinstance(importlib.import_module("legacy.urls").legacy_urlpatterns, list)  # the old contracts (legacy-shim WP)


def test_installed_apps_order_and_no_admin():
    assert "django.contrib.admin" not in settings.INSTALLED_APPS
    assert settings.LOCAL_APPS[:2] == ["core", "accounts"]
    assert len(settings.LOCAL_APPS) == 32
    assert "engines" not in settings.INSTALLED_APPS


def test_engines_is_a_plain_package():
    engines = importlib.import_module("engines")
    assert Path(engines.__file__).parent.joinpath("tests", "__init__.py").is_file()
