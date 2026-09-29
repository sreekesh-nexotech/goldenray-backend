"""CMS roles/admin users and main-backend ``auth_user`` → accounts (PLAN §7.2 rows 1–2, §7.3), with the committed CMS
role rows (``migrations_tools/tests/fixtures/cms.json``) and synthetic, masked user rows."""

import json
from pathlib import Path

import pytest
from django.core import mail

from accounts.models import PasswordReset, Role, User
from accounts.registry import MODULES, full_access
from accounts.services import legacy_import
from accounts.services.seeds import seed_roles
from accounts.tests.factories import UserFactory
from audit.models import AuditLog
from core.models import LegacyMap

pytestmark = pytest.mark.django_db
FIXTURE = Path(__file__).resolve().parents[2] / "migrations_tools/tests/fixtures/cms.json"
CMS_ROLES = json.loads(FIXTURE.read_text())["tables"]["accounts_role"]


def admin_user(**overrides):
    row = {
        "id": 1,
        "username": "editor.one",
        "email": "Editor.One@Example.com",
        "first_name": "Edi",
        "last_name": "Tor",
        "is_active": True,
        "is_superuser": True,
        "is_staff": True,
        "role": "editor",
        "access_role_id": 2,
        "password": "pbkdf2_sha256$870000$masked$masked=",
        "date_joined": "2025-01-02T03:04:05+00:00",
        "last_login": None,
    }
    return {**row, **overrides}


@pytest.fixture
def seeded():
    seed_roles()


def by_slug(slug):
    return Role.objects.get(slug=slug)


class TestRoles:
    def test_seeded_slugs_update_grants_only_and_new_roles_are_custom(self, seeded):
        content_before = by_slug("content-manager")
        result = legacy_import.import_roles(CMS_ROLES)
        assert (result["created"], result["updated"], result["skipped"]) == (2, 1, 1)
        content = by_slug("content-manager")
        # CMS vocabulary replaced (pages view/edit only, no career_page), platform-only modules kept (company, products_public).
        assert content.permissions["pages"] == ["view", "edit"] and "career_page" not in content.permissions
        assert content.permissions["company"] == ["view"] and content.permissions["products_public"] == list(MODULES["products_public"].actions)
        assert content.name == content_before.name and content.is_system
        assert set(content.scopes.values()) == {"all"}
        careers = by_slug("careers-hr")
        assert not careers.is_system and careers.name == "Career / HR" and careers.legacy_role == "editor"
        # career_page.verify (Super Admin row) is not in the registry: dropped and listed.
        assert careers.permissions["career_page"] == ["view", "edit"]
        assert {"source_table": "accounts_role", "source_id": "1", "code": "action_dropped", "message": "career_page.verify is not an action of career_page; dropped."} in result["violations"]
        sales = by_slug("sales-lead")
        assert sales.permissions["leads"] == ["view", "edit", "archive"] and sales.scopes["leads"] == "all"

    def test_super_admin_keeps_every_grant(self, seeded):
        role = by_slug("super-admin")
        Role.objects.filter(pk=role.pk).update(permissions={"blogs": ["view"]})
        result = legacy_import.import_roles(CMS_ROLES)
        assert by_slug("super-admin").permissions == full_access()[0]
        assert any(violation["code"] == "super_admin_kept" for violation in result["violations"])

    def test_idempotent_and_audited(self, seeded):
        legacy_import.import_roles(CMS_ROLES)
        again = legacy_import.import_roles(CMS_ROLES)
        assert again["created"] == again["updated"] == 0 and again["skipped"] == 4
        assert LegacyMap.objects.filter(source_system="CMS", source_table="accounts_role").count() == 4
        assert AuditLog.objects.filter(action="accounts.legacy_import").count() == 2

    def test_source_change_updates_a_custom_role(self, seeded):
        legacy_import.import_roles(CMS_ROLES)
        changed = [{**row, "name": "Sales (renamed)"} if row["slug"] == "sales-lead" else row for row in CMS_ROLES]
        assert legacy_import.import_roles(changed)["updated"] == 1
        assert by_slug("sales-lead").name == "Sales (renamed)"

    def test_invalid_rows_are_listed(self, seeded):
        result = legacy_import.import_roles(
            [{"id": 9, "slug": ""}, {"id": 10, "slug": "odd", "name": "Odd", "permissions": {"ghost": ["view"], "blogs": ["fly"]}}, {"id": 11, "slug": "x", "permissions": "[]"}]
        )
        codes = [violation["code"] for violation in result["violations"]]
        assert codes == ["slug_missing", "module_dropped", "action_dropped", "permissions_invalid"]
        assert by_slug("odd").permissions == {}

    def test_deleted_platform_role_stays_deleted(self, seeded):
        legacy_import.import_roles(CMS_ROLES)
        role = by_slug("sales-lead")
        Role.all_objects.filter(pk=role.pk).update(deleted_at=role.created_at)
        assert legacy_import.import_roles(CMS_ROLES)["skipped"] == 4

    def test_dry_run_writes_nothing(self, seeded):
        legacy_import.import_roles(CMS_ROLES, dry_run=True)
        assert not Role.objects.filter(slug="sales-lead").exists() and not LegacyMap.objects.exists()


class TestUsers:
    @pytest.fixture
    def roles(self, seeded):
        legacy_import.import_roles(CMS_ROLES)

    def test_forced_reset_no_password_role_from_access_role(self, roles):
        result = legacy_import.import_cms_users([admin_user()])
        assert result["created"] == 1 and result["violations"] == []
        user = User.objects.get(email="editor.one@example.com")
        assert user.role.slug == "content-manager" and user.must_reset_password and not user.has_usable_password()
        assert user.created_at.isoformat() == "2025-01-02T03:04:05+00:00"
        assert not user.check_password("anything")

    def test_role_from_legacy_role_when_access_role_is_unmapped(self, roles):
        result = legacy_import.import_cms_users([admin_user(id=2, email="a@example.com", access_role_id=77, role="admin"), admin_user(id=3, email="b@example.com", access_role_id=None, role="author")])
        assert result["created"] == 2
        assert User.objects.get(email="a@example.com").role.slug == "admin"
        author = User.objects.get(email="b@example.com").role
        assert author.slug == "cms-author" and set(author.permissions) == {"dashboard", "blogs"}
        assert [violation["code"] for violation in result["violations"]] == ["role_unmapped"]

    def test_missing_invalid_and_unknown_role(self, roles):
        result = legacy_import.import_cms_users(
            [admin_user(id=4, email="", username="No Mail"), admin_user(id=5, email="not-an-email", username="bad"), admin_user(id=6, email="c@example.com", access_role_id=None, role="ghost")]
        )
        assert User.objects.filter(email="no-mail@migrated.invalid").exists() and User.objects.filter(email="bad@migrated.invalid").exists()
        assert [violation["code"] for violation in result["violations"]] == ["email_missing", "email_invalid", "role_missing"]

    def test_existing_account_is_adopted(self, roles):
        existing = UserFactory(email="editor.one@example.com")
        result = legacy_import.import_cms_users([admin_user()])
        assert result["skipped"] == 1 and result["violations"][0]["code"] == "email_adopted"
        assert LegacyMap.objects.get(source_table="accounts_admin_user").target_id == existing.pk
        assert User.objects.get(pk=existing.pk).role_id == existing.role_id

    def test_adopted_account_is_never_rewritten_by_a_rerun(self, roles):
        # e.g. the bootstrap Super Admin, who has not set a password yet, shares the address of a CMS author.
        existing = UserFactory(email="editor.one@example.com", role=by_slug("super-admin"), must_reset_password=True, first_name="Boot")
        rows = [admin_user(access_role_id=None, role="author", first_name="Other", is_active=False)]
        assert legacy_import.import_cms_users(rows)["skipped"] == 1
        again = legacy_import.import_cms_users(rows)
        user = User.objects.get(pk=existing.pk)
        assert (again["created"], again["updated"], again["skipped"]) == (0, 0, 1)
        assert user.role.slug == "super-admin" and user.first_name == "Boot" and user.is_active

    def test_account_adopted_across_sources_keeps_the_creating_source(self, roles):
        legacy_import.import_cms_users([admin_user()])  # CMS editor → Content Manager
        backend = [admin_user(id=7, username="bom", first_name="Bom")]  # the same address among the /bom/ superusers
        assert legacy_import.import_backend_users(backend)["violations"][0]["code"] == "email_adopted"
        rerun = legacy_import.import_backend_users(backend)
        user = User.objects.get(email="editor.one@example.com")
        assert rerun["updated"] == 0 and user.role.slug == "content-manager" and user.first_name == "Edi"
        assert legacy_import.import_cms_users([admin_user()])["updated"] == 0

    def test_rerun_updates_names_and_role_until_taken_over(self, roles):
        legacy_import.import_cms_users([admin_user()])
        assert legacy_import.import_cms_users([admin_user()])["skipped"] == 1
        result = legacy_import.import_cms_users([admin_user(first_name="Edith", access_role_id=4, is_active=False)])
        user = User.all_objects.get(email="editor.one@example.com")
        assert result["updated"] == 1 and user.first_name == "Edith" and user.role.slug == "sales-lead" and not user.is_active

    def test_backend_superusers_become_admins(self, seeded):
        result = legacy_import.import_backend_users([admin_user(id=1, email="bom@example.com", username="bom")])
        user = User.objects.get(email="bom@example.com")
        assert result["created"] == 1 and user.role.slug == "admin" and user.must_reset_password
        assert LegacyMap.objects.filter(source_system="BACKEND", source_table="auth_user", target_id=user.pk).exists()

    def test_backend_without_admin_role(self):
        result = legacy_import.import_backend_users([admin_user(id=1, email="bom@example.com")])
        assert [violation["code"] for violation in result["violations"]] == ["admin_role_missing", "role_missing"]

    def test_database_refusal_is_listed(self, roles, monkeypatch):
        from django.db import IntegrityError

        def refuse(self, *args, **kwargs):
            raise IntegrityError("duplicate key value\nDETAIL: x")

        monkeypatch.setattr(User, "save", refuse)
        result = legacy_import.import_cms_users([admin_user()])
        assert result["skipped"] == 1 and result["violations"][0]["code"] == "rejected"


class TestResetLinks:
    def test_links_issued_once_to_real_active_addresses(self, seeded, django_capture_on_commit_callbacks):
        legacy_import.import_roles(CMS_ROLES)
        legacy_import.import_cms_users([admin_user(), admin_user(id=2, email="", username="ghost"), admin_user(id=3, email="off@example.com", is_active=False)])
        with django_capture_on_commit_callbacks(execute=True):
            first = legacy_import.issue_reset_links()
        assert first["created"] == 1 and len(first["violations"]) == 2
        assert len(mail.outbox) == 1 and mail.outbox[0].to == ["editor.one@example.com"]
        assert legacy_import.issue_reset_links()["created"] == 0  # the open link is not replaced
        user = User.objects.get(email="editor.one@example.com")
        assert PasswordReset.objects.filter(user=user).count() == 1
        User.objects.filter(pk=user.pk).update(must_reset_password=False)
        assert legacy_import.issue_reset_links(send=False)["created"] == 0
