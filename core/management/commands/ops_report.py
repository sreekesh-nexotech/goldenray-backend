"""``manage.py ops_report [--days 7] [--email] [--json]`` — the weekly operations summary (PLAN §5.6)."""

import json

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction

from core import ops_report


class Command(BaseCommand):
    help = "Print (and optionally e-mail) the ops report: outbox backlog, failed render jobs, audit volume, media, …"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7, help="Window in days (default 7).")
        parser.add_argument("--email", action="store_true", help="Also e-mail it to OPS_EMAILS.")
        parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")

    def handle(self, *args, days: int, email: bool, json: bool, **options):
        if not 1 <= days <= 90:
            raise CommandError("--days must be between 1 and 90.")
        report = ops_report.build(days=days)
        self.stdout.write(_json(report) if json else ops_report.render_text(report))
        if email:
            with transaction.atomic():
                sent = ops_report.send(report)
            if not sent:
                raise CommandError("OPS_EMAILS is not configured; nothing was sent.")
            self.stdout.write(self.style.SUCCESS(f"sent to {sent} recipient(s)"))


def _json(report) -> str:
    return json.dumps(report, cls=DjangoJSONEncoder, ensure_ascii=False, indent=2)
