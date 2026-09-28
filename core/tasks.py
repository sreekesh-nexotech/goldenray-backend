"""Celery tasks owned by core."""

from celery import shared_task

from core import outbox


@shared_task(name="core.tasks.drain_outbox", ignore_result=True)
def drain_outbox(batch_size: int | None = None) -> dict:
    """Drain one batch of the transactional outbox (Beat every 5 s, plus on commit after every emit)."""
    return outbox.drain(batch_size)
