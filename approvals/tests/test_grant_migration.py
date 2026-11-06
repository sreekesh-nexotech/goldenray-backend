"""``approvals.0002_change_request_grants``: existing system roles gain ``change_requests``; Project Head and
Procurement lose their direct catalog/pricing/BOM writes (and get them back on reverse)."""

import importlib

import pytest
from django.apps import apps

from accounts.models import Role
from accounts.tests.factories import RoleFactory

pytestmark = pytest.mark.django_db
migration = importlib.import_module("approvals.migrations.0002_change_request_grants")


def test_forward_and_backward():
    admin = RoleFactory(slug="admin", permissions={"bom": ["view", "edit"]}, scopes={"bom": "all"})
    head = RoleFactory(
        slug="project-head",
        permissions={"catalog": ["view", "create", "edit", "approve"], "pricing": ["view", "edit"], "bom": ["view", "edit"], "packs": ["view", "edit"]},
        scopes={"catalog": "all", "pricing": "all", "bom": "all", "packs": "all"},
    )
    procurement = RoleFactory(
        slug="procurement", permissions={"catalog": ["view", "create", "edit"], "procurement": ["view", "create", "edit", "commit"]}, scopes={"catalog": "all", "procurement": "all"}
    )
    other = RoleFactory(slug="sales-head", permissions={"leads": ["view"]})

    migration.forward(apps, None)
    admin.refresh_from_db(), head.refresh_from_db(), procurement.refresh_from_db(), other.refresh_from_db()
    assert admin.permissions["change_requests"] == ["view", "create", "approve"] and admin.scopes["change_requests"] == "all"
    assert head.permissions["catalog"] == ["view"] and head.permissions["pricing"] == ["view"] and head.permissions["bom"] == ["view"]
    assert head.permissions["packs"] == ["view", "edit"], "only the modules that now need approval change"
    assert head.permissions["change_requests"] == ["view", "create"] and head.scopes["change_requests"] == "owned"
    assert procurement.permissions["catalog"] == ["view"] and procurement.permissions["change_requests"] == ["view", "create"]
    assert procurement.permissions["procurement"] == ["view", "create", "edit"], "committing a batch sets prices: it is requested"
    assert "change_requests" not in other.permissions

    migration.backward(apps, None)
    head.refresh_from_db(), procurement.refresh_from_db(), admin.refresh_from_db()
    assert head.permissions["catalog"] == ["view", "create", "edit", "approve"] and "change_requests" not in head.permissions
    assert procurement.permissions["catalog"] == ["view", "create", "edit"] and "change_requests" not in admin.permissions
    assert procurement.permissions["procurement"] == ["view", "create", "edit", "commit"]
    assert Role.objects.count() == 4
