"""Import the Purchase Agreement page's browser records (PLAN §7.5). Run before ``import_si``.

import_pa --export crs=crs.json --export admin=admin.json [--catalog products.json]
"""

import re

from django.core.management.base import CommandError

from migrations_tools.services import pa
from migrations_tools.services.cli import ImportCommand
from migrations_tools.services.source import TablesSource

EXPORT_RE = re.compile(r"^(?P<profile>[a-z0-9_-]+)=(?P<path>.+)$")


class Command(ImportCommand):
    help = "Import the Purchase Agreement localStorage exports (one per browser profile) and the KSEB fees, idempotently."
    plan = pa.PLAN
    source_name = "Purchase Agreement page exports"
    website_options = False

    def add_source_arguments(self, parser) -> None:
        parser.add_argument(
            "--export", action="append", default=[], metavar="PROFILE=FILE", help=f"one browser profile's export ({' / '.join(pa.PROFILES)}): the flarize_agr array or a localStorage dump"
        )
        parser.add_argument("--catalog", help="the Upstash catalog export (GET /api/products); default: the page's built-in KSEB fees, no catalog comparison")
        parser.add_argument("--source-fixture", help="JSON file {table: [rows]} with the tables the plan reads (tests)")

    def _exports(self, options) -> dict[str, str]:
        exports = {}
        for value in options["export"]:
            match = EXPORT_RE.match(value)
            if match is None or match.group("profile") not in pa.PROFILES:
                raise CommandError(f"--export {value!r}: use PROFILE=FILE with PROFILE one of {', '.join(pa.PROFILES)}.")
            exports[match.group("profile")] = match.group("path")
        return exports

    def source_given(self, options) -> bool:
        return bool(self._exports(options) or options["source_fixture"])

    def missing_source_message(self) -> str:
        return "give --export PROFILE=FILE (one per browser profile) or --source-fixture."

    def open_source(self, options):
        if options["source_fixture"]:
            return TablesSource.from_file(options["source_fixture"])
        exports = self._exports(options)
        for profile in pa.PROFILES:
            if profile not in exports:
                self.stdout.write(self.style.WARNING(f"no export for the {profile!r} profile: if that browser profile was cleared, its records are gone — tell the client before C8 (PLAN §7.5)."))
        return TablesSource(pa.export_tables(exports, options["catalog"]), label=", ".join(f"{profile}={path}" for profile, path in exports.items()))

    def verify_arguments(self, options) -> dict:
        if options["source_fixture"]:
            return {"pa_fixture": options["source_fixture"]}
        return {"pa_export": options["export"], "pa_catalog": options["catalog"]}
