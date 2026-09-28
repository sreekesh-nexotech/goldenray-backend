from django.core.management.base import BaseCommand

from core.outbox import backlog_stats, drain_outbox_sync


class Command(BaseCommand):
    help = "Drain the transactional outbox synchronously (ops fallback when no worker is running)."

    def add_arguments(self, parser):
        parser.add_argument("--max-rounds", type=int, default=50, help="Maximum drain batches to run.")

    def handle(self, *args, **options):
        totals = drain_outbox_sync(max_rounds=options["max_rounds"])
        stats = backlog_stats()
        self.stdout.write(
            f"claimed={totals['claimed']} processed={totals['processed']} failed={totals['failed']} parked={totals['parked']} "
            f"pending={stats['pending']} oldest_age_seconds={stats['oldest_age_seconds']} parked_total={stats['parked']}"
        )
