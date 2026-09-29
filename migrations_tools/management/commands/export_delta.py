"""Export the platform writes since a cutover for manual replay after a rollback (PLAN §7.7)."""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from migrations_tools.services import delta

SYSTEMS = {"cms": "CMS", "backend": "BACKEND"}


def _moment(value: str | None, name: str):
    if value is None:
        return None
    moment = parse_datetime(value)
    if moment is None:
        raise CommandError(f"--{name}: not an ISO date-time ({value!r}).")
    return moment if timezone.is_aware(moment) else timezone.make_aware(moment)


class Command(BaseCommand):
    help = "Export every row created, changed or deleted since --since in the target tables of a website source (JSON, mode 0600)."

    def add_arguments(self, parser):
        parser.add_argument("--source", required=True, choices=sorted(SYSTEMS))
        parser.add_argument("--since", required=True, help="cutover moment, ISO 8601 (e.g. 2026-10-01T06:00:00+05:30)")
        parser.add_argument("--until", help="end of the window (default: now)")
        parser.add_argument("--output", required=True, help="JSON file to write (created with mode 0600)")

    def handle(self, *args, **options):
        since, until = _moment(options["since"], "since"), _moment(options["until"], "until")
        if until is not None and until <= since:
            raise CommandError("--until must be after --since.")
        payload = delta.export(SYSTEMS[options["source"]], since=since, until=until)
        path = delta.write(payload, options["output"])
        total = sum(sum(counts.values()) for counts in payload["counts"].values())
        self.stdout.write(f"{total} changed rows in {len(payload['tables'])} tables written to {path}")
        for table, counts in payload["counts"].items():
            self.stdout.write(f"  {table}: {', '.join(f'{count} {change}' for change, count in sorted(counts.items()))}")
        if payload["unknown_tables"]:
            self.stdout.write(self.style.WARNING(f"tables not installed: {', '.join(payload['unknown_tables'])}"))
