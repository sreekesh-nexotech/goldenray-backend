"""Import the Flarize utility's JSON files into the platform (PLAN §7.4) and publish PriceRelease #1 / PackRelease #1.

See ``--help`` and docs/decisions/migration-ops.md. Run after ``seed_roles``; the files are read, never written.
"""

from migrations_tools.services.cli import ImportCommand
from migrations_tools.services.flarize import PLAN
from migrations_tools.services.source import JsonFilesSource, TablesSource


class Command(ImportCommand):
    help = "Import the Flarize data folder (users, catalog, pricing, procurement, packs, content, customers, quotations, projects) and publish the first releases, idempotently."
    plan = PLAN
    source_name = "Flarize data folder"
    website_options = False

    def add_source_arguments(self, parser) -> None:
        source = parser.add_mutually_exclusive_group()
        source.add_argument("--source-dir", help="the Flarize data/ folder (e.g. /srv/flarize/data); files are opened read-only")
        source.add_argument("--source-fixture", help='JSON file {"tables": {"<file>.json": [document]}} instead of the folder (tests)')

    def source_given(self, options) -> bool:
        return bool(options["source_dir"] or options["source_fixture"])

    def missing_source_message(self) -> str:
        return "one of --source-dir / --source-fixture is required."

    def open_source(self, options):
        return JsonFilesSource(options["source_dir"]) if options["source_dir"] else TablesSource.from_file(options["source_fixture"])

    def verify_arguments(self, options) -> dict:
        return {"flarize_dir": options["source_dir"]} if options["source_dir"] else {"flarize_fixture": options["source_fixture"]}
