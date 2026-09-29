"""Verify a migration (PLAN §7.6): website sources (checks 1–7, 10, 12), Flarize (#8 packs, #9 quotations), eSSL
(#11 attendance), and #1/#2/#4/#12 for every source. Exit code 1 when a check fails."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder

from migrations_tools.services import pa
from migrations_tools.services.http import InProcessClient, RemoteClient, unthrottled
from migrations_tools.services.source import JsonFilesSource, SourceError, SqliteSource, TablesSource, open_source
from migrations_tools.services.verify import Verifier

SYSTEMS = {"cms": "CMS", "backend": "BACKEND", "flarize": "FLARIZE", "pa": "PA", "si": "SI", "essl": "ESSL"}
DEFAULT_SYSTEMS = ("backend", "cms")  # the website cutover (C3/C4); the operational sources are verified by name


class Command(BaseCommand):
    help = "Run the PLAN §7.6 verification for one or more migrated sources."

    def add_arguments(self, parser):
        parser.add_argument("--source", action="append", choices=sorted(SYSTEMS), help=f"which source(s) to verify (default: {' and '.join(DEFAULT_SYSTEMS)})")
        parser.add_argument("--cms-url", help="postgresql:// URL of the legacy CMS database (read-only)")
        parser.add_argument("--backend-url", help="postgresql:// URL of the legacy main backend database (read-only)")
        parser.add_argument("--cms-fixture", help="exported CMS tables (JSON) instead of --cms-url")
        parser.add_argument("--backend-fixture", help="exported main backend tables (JSON) instead of --backend-url")
        parser.add_argument("--flarize-dir", help="the Flarize data/ folder the import read")
        parser.add_argument("--flarize-fixture", help="Flarize tables (JSON) instead of --flarize-dir")
        parser.add_argument("--flarize-reference", help="#8: the Flarize pack reference outputs (default: packs/tests/golden/flarize_packs.json, from generate_packs.mjs)")
        parser.add_argument("--pa-export", action="append", default=[], metavar="PROFILE=FILE", help="the Purchase Agreement exports the import read (as for import_pa)")
        parser.add_argument("--pa-catalog", help="the Upstash catalog export the import read")
        parser.add_argument("--pa-fixture", help="PA tables (JSON) instead of --pa-export")
        parser.add_argument("--si-file", help="the Site Inspection SQLite file the import read")
        parser.add_argument("--si-fixture", help="SI tables (JSON) instead of --si-file")
        parser.add_argument("--essl-url", help="postgresql:// URL of the eSSL database (read-only)")
        parser.add_argument("--essl-fixture", help="eSSL tables (JSON) instead of --essl-url")
        parser.add_argument("--attendance-report", help="#11: write the v3/v4 attendance diff for HR to this file")
        parser.add_argument("--attendance-signoff", help="#11: the sha256 of the diff report HR signed off")
        parser.add_argument("--legacy-cms-api", "--legacy-api-url", dest="legacy_api_url", help="base URL of the legacy CMS HTTP API (e.g. http://127.0.0.1:18009)")
        parser.add_argument("--new-api-url", help="base URL of the new platform (default: in process)")
        parser.add_argument("--offline", action="store_true", help="skip the media HEAD requests")
        parser.add_argument("--corpus-dir", nargs=2, action="append", metavar=("KIND", "DIR"), default=[], help="calculators|emi corpus directory (default: the committed UAT corpora)")
        parser.add_argument(
            "--list-prices-as-release",
            action="store_true",
            help="rehearsal only: #10 prices website products from the imported LIST rows while no PriceRelease can be published (reported)",
        )
        parser.add_argument("--check", action="append", type=int, default=[], help="run only this check number (repeatable)")
        parser.add_argument("--allow-skipped", action="store_true", help="do not fail on skipped checks")
        parser.add_argument("--json", dest="json_path", help="write the report to this file")

    def _source(self, name: str, options):
        if name in ("cms", "backend", "essl"):
            url, fixture = options[f"{name}_url"], options[f"{name}_fixture"]
            return open_source(url, fixture=fixture) if (url or fixture) else None
        if name == "flarize":
            if options["flarize_dir"]:
                return JsonFilesSource(options["flarize_dir"])
            return TablesSource.from_file(options["flarize_fixture"]) if options["flarize_fixture"] else None
        if name == "si":
            if options["si_file"]:
                return SqliteSource(options["si_file"])
            return TablesSource.from_file(options["si_fixture"]) if options["si_fixture"] else None
        if options["pa_fixture"]:
            return TablesSource.from_file(options["pa_fixture"])
        if options["pa_export"]:
            exports = {}
            for value in options["pa_export"]:
                profile, _, path = value.partition("=")
                if profile not in pa.PROFILES or not path:
                    raise CommandError(f"--pa-export {value!r}: use PROFILE=FILE with PROFILE one of {', '.join(pa.PROFILES)}.")
                exports[profile] = path
            return TablesSource(pa.export_tables(exports, options["pa_catalog"]), label="PA exports")
        return None

    def handle(self, *args, **options):
        systems = options["source"] or list(DEFAULT_SYSTEMS)
        sources = {}
        try:
            for name in sorted(set(systems)):
                sources[SYSTEMS[name]] = self._source(name, options)
        except SourceError as exc:
            raise CommandError(str(exc)) from exc
        corpus_dirs = None
        if options["corpus_dir"]:
            from migrations_tools.services.parity import default_corpus_dirs

            corpus_dirs = {**default_corpus_dirs(), **{kind: Path(path) for kind, path in options["corpus_dir"]}}
        new = RemoteClient(options["new_api_url"]) if options["new_api_url"] else InProcessClient()
        legacy = RemoteClient(options["legacy_api_url"]) if options["legacy_api_url"] else None
        verifier = Verifier(
            sources=sources,
            new=new,
            legacy_cms=legacy,
            offline=options["offline"],
            corpus_dirs=corpus_dirs,
            list_prices_as_release=options["list_prices_as_release"],
            flarize_reference=Path(options["flarize_reference"]) if options["flarize_reference"] else None,
            attendance_report=options["attendance_report"],
            attendance_signoff=options["attendance_signoff"],
        )
        try:
            with unthrottled():
                results = verifier.run(set(options["check"]) or None)
        finally:
            for source in sources.values():
                if source is not None:
                    source.close()
        for result in results:
            style = {"pass": self.style.SUCCESS, "fail": self.style.ERROR, "skipped": self.style.WARNING, "n/a": self.style.NOTICE}[result.status]
            self.stdout.write(style(f"#{result.number:<2} {result.status.upper():7} {result.name}") + f" — {result.summary}")
            for detail in result.details:
                self.stdout.write(f"      {detail}")
        if options["json_path"]:
            Path(options["json_path"]).write_text(json.dumps([result.as_dict() for result in results], cls=DjangoJSONEncoder, indent=1, ensure_ascii=False), encoding="utf-8")
        failed = [result for result in results if result.status == "fail" or (result.status == "skipped" and not options["allow_skipped"] and not (result.number == 4 and options["offline"]))]
        if failed:
            raise CommandError(f"verification failed: {', '.join(f'#{result.number}' for result in failed)}")
        self.stdout.write(self.style.SUCCESS("verification passed"))
