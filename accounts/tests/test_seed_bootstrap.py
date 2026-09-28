"""seed_roles (the exact PLAN §3.2 table, idempotent, never overwrites) and bootstrap_admin."""

import re
from io import StringIO

import pytest
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError

from accounts import registry
from accounts.models import PasswordReset, Role, User
from accounts.services.seeds import ROLE_SEEDS, seed_roles
from audit.models import AuditLog

pytestmark = pytest.mark.django_db

ALL = {module: list(spec.actions) for module, spec in registry.MODULES.items()}

# PLAN §3.2 "Seeded roles", written out literally (permissions in registry order; scopes only where not "all").
EXPECTED = {
    "super-admin": ("Super Admin", ALL, {}),
    "admin": ("Admin", ALL, {}),
    "content-manager": (
        "Content Manager",
        {
            "dashboard": ["view"],
            "pages": ["view", "edit", "publish", "verify"],
            "blogs": ["view", "create", "edit", "publish", "verify", "archive"],
            "faqs": ["view", "create", "edit", "publish", "verify", "archive"],
            "media": ["view", "create", "edit", "archive"],
            "seo": ["view", "edit", "publish"],
            "products_public": ["view", "edit", "publish"],
            "career_page": ["view", "edit", "publish"],
            "company": ["view"],
        },
        {},
    ),
    "hr": (
        "HR",
        {
            "dashboard": ["view"],
            "media": ["view", "create"],
            "job_positions": ["view", "create", "edit", "publish", "verify", "archive"],
            "applications": ["view", "edit", "archive"],
            "departments": ["view", "create", "edit", "archive"],
            "employees": ["view", "create", "edit", "archive"],
            "hr_setup": ["view", "edit"],
            "attendance": ["view", "edit", "export", "manage"],
            "leave": ["view", "create", "approve", "archive"],
            "devices": ["view", "sync"],
        },
        {},
    ),
    "office-manager": (
        "Office Manager",
        {"employees": ["view"], "attendance": ["view", "export"], "leave": ["view", "approve"]},
        {"employees": "office", "attendance": "office", "leave": "office"},
    ),
    "staff": ("Staff", {"dashboard": ["view"], "attendance": ["view"], "leave": ["view", "create"]}, {"attendance": "self", "leave": "self"}),
    "sales-executive": (
        "Sales Executive",
        {
            "dashboard": ["view"],
            "packs": ["view"],
            "leads": ["view", "create", "edit"],
            "customers": ["view", "create", "edit"],
            "quotations": ["view", "create", "edit", "issue", "revise"],
            "quotation_content": ["view"],
            "agreements": ["view", "create", "edit"],
            "site_inspections": ["view", "create"],
            "emi": ["view"],
        },
        {"leads": "owned", "customers": "owned", "quotations": "owned", "agreements": "owned", "site_inspections": "owned"},
    ),
    "sales-head": (
        "Sales Head",
        {
            "dashboard": ["view"],
            "packs": ["view"],
            "leads": ["view", "create", "edit", "manage"],
            "customers": ["view", "create", "edit", "manage"],
            "quotations": ["view", "create", "edit", "issue", "revise", "approve"],
            "quotation_content": ["view"],
            "agreements": ["view", "create", "edit", "issue", "manage"],
            "site_inspections": ["view", "create", "assign"],
            "emi": ["view"],
        },
        {},
    ),
    "project-head": (
        "Project Head",
        {
            "catalog": ["view", "create", "edit", "approve"],
            "pricing": ["view", "edit"],
            "pricing_internal": ["view"],
            "market_rates": ["view", "edit"],
            "procurement": ["view"],
            "bom": ["view", "edit"],
            "packs": ["view", "edit", "submit"],
            "engineering": ["view", "verify"],
            "quotations": ["view"],
            "quotation_content": ["view", "edit", "publish"],
            "agreements": ["view"],
            "site_inspections": ["view", "assign", "approve", "release"],
            "projects": ["view", "create", "edit", "lock", "archive"],
            "company": ["view"],
        },
        {},
    ),
    "engineering": (
        "Engineering",
        {
            "catalog": ["view"],
            "bom": ["view"],
            "packs": ["view"],
            "engineering": ["view", "verify", "approve"],
            "site_inspections": ["view", "approve"],
            "projects": ["view"],
        },
        {},
    ),
    "field-engineer": (
        "Field Engineer",
        {"dashboard": ["view"], "customers": ["view"], "site_inspections": ["view", "edit", "submit"], "media": ["create"]},
        {"site_inspections": "assigned"},
    ),
    "procurement": (
        "Procurement",
        {"catalog": ["view", "create", "edit"], "pricing_internal": ["view"], "procurement": ["view", "create", "edit", "commit"], "inventory": ["view", "edit"]},
        {},
    ),
}


def test_seeds_exactly_the_twelve_plan_roles():
    results = seed_roles()
    assert len(results) == 12 and all(created for _, created in results)
    assert {seed.slug for seed in ROLE_SEEDS} == set(EXPECTED)
    for slug, (name, permissions, narrow_scopes) in EXPECTED.items():
        role = Role.objects.get(slug=slug)
        assert role.name == name and role.is_system, slug
        assert role.permissions == registry.normalise_permissions(permissions), slug
        assert role.scopes == {module: narrow_scopes.get(module, "all") for module in role.permissions}, slug
        assert role.description
    assert AuditLog.objects.filter(action="accounts.role_seeded", actor_kind="SYSTEM").count() == 12


def test_running_twice_never_overwrites_edited_grants():
    seed_roles()
    hr = Role.objects.get(slug="hr")
    hr.permissions = {"employees": ["view"]}
    hr.name = "People team"
    hr.save()
    Role.objects.get(slug="staff").soft_delete()
    results = seed_roles()
    assert [role.slug for role, created in results if created] == ["staff"]  # only the missing role is recreated
    hr.refresh_from_db()
    assert hr.permissions == {"employees": ["view"]} and hr.name == "People team"
    assert Role.objects.filter(slug="staff").count() == 1


def test_command_output_is_idempotent():
    out = StringIO()
    call_command("seed_roles", stdout=out)
    assert "12 created, 0 already present" in out.getvalue()
    out = StringIO()
    call_command("seed_roles", stdout=out)
    assert "0 created, 12 already present" in out.getvalue()


class TestBootstrap:
    def test_creates_a_super_admin_without_a_password_and_prints_a_one_time_link(self, api_client, django_capture_on_commit_callbacks):
        out = StringIO()
        call_command("bootstrap_admin", "--email", "Founder@Flarize.com", "--first-name", "Asha", stdout=out)
        user = User.objects.get(email="founder@flarize.com")
        assert user.role.slug == "super-admin" and user.must_reset_password and not user.has_usable_password()
        assert Role.objects.filter(is_system=True).count() == 12
        token = re.search(r"#token=(\S+)", out.getvalue()).group(1)
        assert "24 hours" in out.getvalue()
        assert AuditLog.objects.get(action="accounts.user_bootstrapped").actor_kind == "SYSTEM"
        assert api_client.post("/api/v1/auth/login/", {"email": "founder@flarize.com", "password": "anything-at-all"}, format="json").status_code == 401
        with django_capture_on_commit_callbacks(execute=True):
            assert api_client.post("/api/v1/auth/password/reset/", {"token": token, "new_password": "Brand-New-Secret-77"}, format="json").status_code == 204
        login = api_client.post("/api/v1/auth/login/", {"email": "founder@flarize.com", "password": "Brand-New-Secret-77"}, format="json")
        assert login.status_code == 200
        assert PasswordReset.objects.get(user=user).used_at is not None
        assert mail.outbox  # the "password changed" notice

    def test_existing_email_is_refused(self, make_user):
        make_user(email="taken@example.com")
        with pytest.raises(CommandError, match="email_taken"):
            call_command("bootstrap_admin", "--email", "TAKEN@example.com", stdout=StringIO())

    def test_invalid_email_is_refused(self):
        with pytest.raises(CommandError, match="validation_error"):
            call_command("bootstrap_admin", "--email", "not-an-email", stdout=StringIO())
