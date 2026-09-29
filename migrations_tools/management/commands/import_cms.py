"""Import the legacy CMS (``blog_cms``) into the platform (PLAN §7.2). See ``--help`` and docs/decisions/migration-website.md."""

from migrations_tools.services.cli import ImportCommand
from migrations_tools.services.cms import PLAN


class Command(ImportCommand):
    help = "Import the legacy CMS database (read-only source) through the apps' legacy importers, idempotently."
    plan = PLAN
    source_name = "CMS (blog_cms)"
