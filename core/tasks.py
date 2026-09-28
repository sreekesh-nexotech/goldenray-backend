"""Celery tasks owned by core."""

from smtplib import SMTPException

from celery import shared_task

from core import outbox


@shared_task(name="core.tasks.drain_outbox", ignore_result=True)
def drain_outbox(batch_size: int | None = None) -> dict:
    """Drain one batch of the transactional outbox (Beat every 5 s, plus on commit after every emit)."""
    return outbox.drain(batch_size)


@shared_task(
    name="core.tasks.send_email",
    ignore_result=True,
    autoretry_for=(SMTPException, ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=5,
)
def send_email(to: list[str], subject: str, text: str, html: str | None = None, category: str = "") -> int:
    """Deliver one e-mail (enqueued on commit by ``core.notifications.queue_email``); retried on SMTP/network errors."""
    from core.notifications import send_email as deliver

    return deliver(to=to, subject=subject, text=text, html=html, category=category)


@shared_task(name="core.tasks.send_ops_report", ignore_result=True)
def send_ops_report(days: int = 7) -> int:
    """Beat, Monday 08:05 IST: e-mail the ops report to ``OPS_EMAILS`` (no-op when none are configured)."""
    from core import ops_report

    return ops_report.send(ops_report.build(days=days))
