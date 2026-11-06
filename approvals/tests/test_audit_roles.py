"""``audit_change_request_roles``: custom roles that bypass approval are found; Admin and requester roles are not."""

import pytest
from django.core.management import call_command

from accounts.tests.factories import RoleFactory
from approvals.management.commands.audit_change_request_roles import roles_bypassing_approval

pytestmark = pytest.mark.django_db


def test_lists_only_non_admin_roles_with_a_direct_write():
    RoleFactory(slug="admin", permissions={"bom": ["view", "edit"], "pricing": ["view", "edit"]})
    RoleFactory(slug="requester", permissions={"bom": ["view"], "catalog": ["view"], "change_requests": ["view", "create"]})
    RoleFactory(slug="custom-pricing", permissions={"pricing": ["view", "edit"], "change_requests": ["view", "create"]})
    RoleFactory(slug="custom-bom", permissions={"bom": ["view", "edit"]})
    RoleFactory(slug="unrelated", permissions={"leads": ["view", "edit"]})

    rows = {row["slug"]: row for row in roles_bypassing_approval()}
    assert set(rows) == {"custom-pricing", "custom-bom"}
    assert rows["custom-pricing"]["direct"] == ["pricing.edit"] and rows["custom-pricing"]["can_request"] is True
    assert rows["custom-bom"]["direct"] == ["bom.edit"] and rows["custom-bom"]["can_request"] is False


def test_command_output(capsys):
    RoleFactory(slug="custom-bom", permissions={"catalog": ["view", "create"]})
    call_command("audit_change_request_roles")
    out = capsys.readouterr().out
    assert "custom-bom" in out and "catalog.create" in out


def test_command_reports_clean(capsys):
    RoleFactory(slug="requester", permissions={"catalog": ["view"], "change_requests": ["view", "create"]})
    call_command("audit_change_request_roles")
    assert "No role other than Admin" in capsys.readouterr().out
