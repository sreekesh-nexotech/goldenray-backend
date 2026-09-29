"""Celery tasks owned by attendance (routed to the ``ingest`` queue; enqueued only inside ``transaction.on_commit``).

* ``run_due_recomputes`` — the debounced recompute (A8): after each burst of events, and every minute from Beat as a
  safety net for a lost message.
* ``finalise_days`` — Beat every 15 minutes: each office's newly final day is recomputed once after 00:30 on the
  office's own clock (A7); never a date ≥ today.
* ``maintain_punch_partitions`` — Beat monthly: the next months' ``attendance_raw_punch`` partitions (when the worker
  owns the table; ``manage.py maintain_attendance_punches`` does it on every deploy as the owner).
"""

import logging

from celery import shared_task

logger = logging.getLogger("flarize.attendance")


@shared_task(name="attendance.tasks.run_due_recomputes", ignore_result=True)
def run_due_recomputes() -> dict:
    from attendance.services import recompute

    return recompute.run_due()


@shared_task(name="attendance.tasks.finalise_days", ignore_result=True)
def finalise_days() -> list:
    from attendance.services import recompute

    results = recompute.finalise_due()
    for item in results:
        logger.info("attendance day finalised", extra=item)
    return results


@shared_task(name="attendance.tasks.maintain_punch_partitions", ignore_result=True)
def maintain_punch_partitions(months: int = 3) -> dict:
    from attendance.services import partitions

    result = partitions.maintain(months)
    if not result["owner"]:
        logger.warning("attendance_raw_punch partitions not maintained: the worker is not the table owner (run maintain_attendance_punches as the owner)")
    return result
