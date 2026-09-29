"""Import the Site Inspection V2 SQLite database (PLAN §7.5). Run ``seed_roles`` and ``import_pa`` first."""

from migrations_tools.services.cli import ImportCommand
from migrations_tools.services.si import PLAN
from migrations_tools.services.source import SqliteSource, TablesSource


class Command(ImportCommand):
    help = "Import the Site Inspection V2 SQLite file (opened read-only): engineers, customers, inspections and their records, idempotently."
    plan = PLAN
    source_name = "Site Inspection V2 SQLite file"
    website_options = False

    def add_source_arguments(self, parser) -> None:
        source = parser.add_mutually_exclusive_group()
        source.add_argument("--source-file", help="the app's flarize-site-inspection.db (stop the app first: its WAL must be checkpointed)")
        source.add_argument("--source-fixture", help="JSON file {table: [rows]} instead of the SQLite file (tests)")

    def source_given(self, options) -> bool:
        return bool(options["source_file"] or options["source_fixture"])

    def missing_source_message(self) -> str:
        return "one of --source-file / --source-fixture is required."

    def open_source(self, options):
        return SqliteSource(options["source_file"]) if options["source_file"] else TablesSource.from_file(options["source_fixture"])

    def verify_arguments(self, options) -> dict:
        return {"si_file": options["source_file"]} if options["source_file"] else {"si_fixture": options["source_fixture"]}
