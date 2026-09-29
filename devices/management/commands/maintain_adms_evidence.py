"""``manage.py maintain_adms_evidence [--months N] [--no-purge]`` — create the upcoming monthly partitions of
``devices_adms_request`` and apply the evidence retention (run as the table owner; deploy/release.sh does)."""

from django.core.management.base import BaseCommand

from devices.services import adms_evidence


class Command(BaseCommand):
    help = "Create upcoming devices_adms_request partitions and purge ADMS evidence older than the retention window."

    def add_arguments(self, parser):
        parser.add_argument("--months", type=int, default=3, help="Partitions for the current month and the next N-1 (default 3).")
        parser.add_argument("--no-purge", action="store_true", help="Only create partitions.")

    def handle(self, *args, **options):
        created = adms_evidence.ensure_partitions(options["months"])
        self.stdout.write(f"partitions created: {', '.join(created) or 'none'}")
        if not options["no_purge"]:
            result = adms_evidence.purge()
            self.stdout.write(f"purged before {result['cutoff']:%Y-%m-%d %H:%M}: {result['deleted']} row(s); dropped {', '.join(result['dropped']) or 'no partition'}")
