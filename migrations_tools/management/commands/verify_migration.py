"""Verify the website migration (PLAN §7.6 checks 1–7, 10, 12). Exit code 1 when a check fails."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder

from migrations_tools.services.http import InProcessClient, RemoteClient, unthrottled
from migrations_tools.services.source import SourceError, open_source
from migrations_tools.services.verify import Verifier

SYSTEMS = {"cms": "CMS", "backend": "BACKEND"}


class Command(BaseCommand):
    help = "Run the PLAN §7.6 verification for the CMS and/or main backend migration."

    def add_arguments(self, parser):
        parser.add_argument("--source", action="append", choices=sorted(SYSTEMS), help="which source(s) to verify (default: both)")
        parser.add_argument("--cms-url", help="postgresql:// URL of the legacy CMS database (read-only)")
        parser.add_argument("--backend-url", help="postgresql:// URL of the legacy main backend database (read-only)")
        parser.add_argument("--cms-fixture", help="exported CMS tables (JSON) instead of --cms-url")
        parser.add_argument("--backend-fixture", help="exported main backend tables (JSON) instead of --backend-url")
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

    def handle(self, *args, **options):
        systems = [SYSTEMS[name] for name in (options["source"] or sorted(SYSTEMS))]
        sources = {}
        try:
            for system in systems:
                name = system.lower()
                url, fixture = options[f"{name}_url"], options[f"{name}_fixture"]
                sources[system] = open_source(url, fixture=fixture) if (url or fixture) else None
        except SourceError as exc:
            raise CommandError(str(exc)) from exc
        corpus_dirs = None
        if options["corpus_dir"]:
            from migrations_tools.services.parity import default_corpus_dirs

            corpus_dirs = {**default_corpus_dirs(), **{kind: Path(path) for kind, path in options["corpus_dir"]}}
        new = RemoteClient(options["new_api_url"]) if options["new_api_url"] else InProcessClient()
        legacy = RemoteClient(options["legacy_api_url"]) if options["legacy_api_url"] else None
        verifier = Verifier(sources=sources, new=new, legacy_cms=legacy, offline=options["offline"], corpus_dirs=corpus_dirs, list_prices_as_release=options["list_prices_as_release"])
        try:
            with unthrottled():
                results = verifier.run(set(options["check"]) or None)
        finally:
            for source in sources.values():
                if source is not None:
                    source.close()
        for result in results:
            style = {"pass": self.style.SUCCESS, "fail": self.style.ERROR, "skipped": self.style.WARNING}[result.status]
            self.stdout.write(style(f"#{result.number:<2} {result.status.upper():7} {result.name}") + f" — {result.summary}")
            for detail in result.details:
                self.stdout.write(f"      {detail}")
        if options["json_path"]:
            Path(options["json_path"]).write_text(json.dumps([result.as_dict() for result in results], cls=DjangoJSONEncoder, indent=1, ensure_ascii=False), encoding="utf-8")
        failed = [result for result in results if result.status == "fail" or (result.status == "skipped" and not options["allow_skipped"] and not (result.number == 4 and options["offline"]))]
        if failed:
            raise CommandError(f"verification failed: {', '.join(f'#{result.number}' for result in failed)}")
        self.stdout.write(self.style.SUCCESS("verification passed"))
