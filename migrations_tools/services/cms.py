"""The CMS (``blog_cms``) import plan — PLAN §7.2, in FK order. Each step calls the owning apps' importers."""

from __future__ import annotations

from django.conf import settings

from accounts.services import legacy_import as accounts_import
from blog.services import legacy_import as blog_import
from careers.services import legacy_import as careers_import
from company.services import legacy_import as company_import
from core.models import LegacyMap
from faqs.services import legacy_import as faqs_import
from media.services import legacy_import as media_import
from migrations_tools.services.runner import Context, Plan, Step
from seo.services import legacy_import as seo_import
from sitepages.services import legacy_import as sitepages_import

CMS = LegacyMap.SourceSystem.CMS
BLOG_SCHEMA_TABLES = (
    "catalog_collection",
    "catalog_template",
    "catalog_template_image_group",
    "catalog_template_attribute_slot",
    "catalog_author",
    "catalog_category",
    "catalog_tag",
    "catalog_badge",
)
BLOG_CONTENT_TABLES = (
    "content_entry",
    "content_entry_categories",
    "content_entry_tags",
    "content_entry_badges",
    "content_entry_slug_history",
    "content_content_block",
    "content_entry_image",
    "content_entry_attribute_value",
    "content_seo",
)
SITEPAGES_TABLES = ("sitepages_page", "sitepages_page_seo", "sitepages_page_text_slot", "sitepages_page_image_slot")


def user_map() -> dict[str, object]:
    """CMS admin-user id → platform user (the users step's ``core_legacy_map`` rows)."""
    from accounts.models import User

    targets = dict(LegacyMap.objects.filter(source_system=CMS, source_table=accounts_import.CMS_USER_TABLE).values_list("target_id", "source_id"))
    return {targets[user.pk]: user for user in User.all_objects.filter(pk__in=targets)}


def _users(rows, ctx: Context) -> dict:
    return {
        "accounts_role": accounts_import.import_roles(rows["accounts_role"], user=ctx.user),
        "accounts_admin_user": accounts_import.import_cms_users(rows["accounts_admin_user"], user=ctx.user),
    }


def _media(rows, ctx: Context) -> dict:
    collections = {row["id"]: row.get("api_uid") for row in rows["catalog_collection"]}
    return {"media_asset": media_import.import_cms_assets(rows["media_asset"], collections=collections, read_file=ctx.read_file, user=ctx.user, dry_run=False)}


def _blog_schema(rows, ctx: Context) -> dict:
    return blog_import.import_all({table: rows[table] for table in BLOG_SCHEMA_TABLES}, user=ctx.user)


def _departments(rows, ctx: Context) -> dict:
    return {"careers_department": careers_import.import_departments(rows["careers_department"], user=ctx.user)}


def _faq_categories(rows, ctx: Context) -> dict:
    return {"faqs_category": faqs_import.import_categories(rows["faqs_category"], user=ctx.user)}


def _pages(rows, ctx: Context) -> dict:
    return sitepages_import.import_all({table: rows[table] for table in SITEPAGES_TABLES}, user=ctx.user)


def _faqs(rows, ctx: Context) -> dict:
    return {"faqs_faq": faqs_import.import_faqs(rows["faqs_faq"], user=ctx.user)}


def _blog_content(rows, ctx: Context) -> dict:
    return blog_import.import_all({table: rows[table] for table in BLOG_CONTENT_TABLES}, user_map=user_map(), user=ctx.user)


def _positions(rows, ctx: Context) -> dict:
    return {"careers_job_position": careers_import.import_positions(rows["careers_job_position"], user=ctx.user)}


def _site_settings(rows, ctx: Context) -> dict:
    settings_rows = rows["siteconfig_settings"]
    site_url = ctx.options.get("site_url") or settings.FRONTEND_BASE_URL
    results = {"siteconfig_settings": company_import.import_site_settings(settings_rows, site_url=site_url, user=ctx.user)}
    if settings_rows:
        results["siteconfig_settings.seo"] = seo_import.import_site_seo_defaults(settings_rows[0], user=ctx.user)
    return results


PLAN = Plan(
    source_system=CMS,
    steps=(
        Step("cms.users", "users", ("accounts_role", "accounts_admin_user"), _users, "roles (grants re-keyed) and admin users (forced reset)"),
        Step("cms.media", "media", ("media_asset", "catalog_collection"), _media, "media library (Bunny URLs kept, /uploads-only files re-uploaded)"),
        Step("cms.blog_schema", "masters", BLOG_SCHEMA_TABLES, _blog_schema, "collections, templates and taxonomy"),
        Step("cms.careers_departments", "masters", ("careers_department",), _departments, "departments"),
        Step("cms.faq_categories", "masters", ("faqs_category",), _faq_categories, "FAQ categories"),
        Step("cms.pages", "content", SITEPAGES_TABLES, _pages, "pages, SEO, text and image slots"),
        Step("cms.faqs", "content", ("faqs_faq",), _faqs, "FAQs (after the pages they belong to)"),
        Step("cms.blog_content", "content", BLOG_CONTENT_TABLES, _blog_content, "entries, links, slug history, blocks, images, attributes, SEO"),
        Step("cms.careers_positions", "content", ("careers_job_position",), _positions, "job positions"),
        Step("cms.site_settings", "content", ("siteconfig_settings",), _site_settings, "site settings → company profile and SEO defaults"),
    ),
    not_migrated={
        "django_admin_log": "PLAN §7.2 row 12 is optional; the Django admin history is not imported (the legacy database is archived)",
        "django_session": "PLAN §7.2: sessions are not migrated",
        "django_migrations": "framework table",
        "django_content_type": "framework table",
        "auth_permission": "framework table (permissions come from the registry)",
        "auth_group": "unused by the CMS (roles are accounts_role)",
        "auth_group_permissions": "unused by the CMS",
        "accounts_admin_user_groups": "unused by the CMS",
        "accounts_admin_user_user_permissions": "unused by the CMS",
        "token_blacklist_outstandingtoken": "PLAN §7.2: not migrated",
        "token_blacklist_blacklistedtoken": "PLAN §7.2: not migrated",
        "authtoken_token": "PLAN §7.2: not migrated",
    },
)
