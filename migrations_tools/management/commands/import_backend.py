"""Import the legacy main backend (``GoldenApp``) into the platform (PLAN §7.3). Run ``import_cms`` first."""

from migrations_tools.services.backend import PLAN
from migrations_tools.services.cli import ImportCommand


class Command(ImportCommand):
    help = "Import the legacy main backend database (read-only source) through the apps' legacy importers, idempotently."
    plan = PLAN
    source_name = "main backend (GoldenApp)"
