from django.core.management.base import BaseCommand, CommandError

from core.outbox import backlog_stats, drain_outbox_sync, requeue_parked


class Command(BaseCommand):
    help = "Drain the transactional outbox synchronously (ops fallback when no worker is running); --requeue-parked re-drives parked events first."

    def add_arguments(self, parser):
        parser.add_argument("--max-rounds", type=int, default=50, help="Maximum drain batches to run.")
        parser.add_argument("--requeue-parked", action="store_true", help="Reset parked events (attempts restart at 0) before draining; fix the failing handler first.")
        parser.add_argument("--id", type=int, action="append", dest="ids", metavar="EVENT_ID", help="With --requeue-parked: only this parked event (repeatable).")

    def handle(self, *args, **options):
        ids = options["ids"]
        if ids and not options["requeue_parked"]:
            raise CommandError("--id only applies together with --requeue-parked.")
        if options["requeue_parked"]:
            requeued = requeue_parked(ids=ids)
            missing = sorted(set(ids or ()) - set(requeued))
            if missing:
                raise CommandError(f"Not parked (nothing requeued): {', '.join(str(event_id) for event_id in missing)}.")
            self.stdout.write(f"requeued={len(requeued)} ids={','.join(str(event_id) for event_id in requeued) or '-'}")
        totals = drain_outbox_sync(max_rounds=options["max_rounds"])
        stats = backlog_stats()
        self.stdout.write(
            f"claimed={totals['claimed']} processed={totals['processed']} failed={totals['failed']} parked={totals['parked']} lost={totals['lost']} "
            f"pending={stats['pending']} oldest_age_seconds={stats['oldest_age_seconds']} stale_claims={stats['stale_claims']} "
            f"retrying={stats['retrying']} parked_total={stats['parked']}"
        )
