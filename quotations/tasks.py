"""Celery tasks owned by quotations (enqueued only inside ``transaction.on_commit``).

* ``quotations.tasks.expire_quotations`` — Beat, daily (``CELERY_BEAT_SCHEDULE["quotations.expire_quotations"]``):
  ISSUED quotations whose validity (frozen at issue from the validity policy) has passed become EXPIRED.
* ``quotations.tasks.send_quotation_email`` — one queued ``quotations_email_log`` row: the PDF e-mailed to the
  customer (one retry after a minute on an SMTP failure).
"""

from __future__ import annotations

from celery import shared_task


@shared_task(name="quotations.tasks.expire_quotations", ignore_result=True)
def expire_quotations() -> list[str]:
    from quotations.services.lifecycle import expire_due

    return expire_due()


@shared_task(name="quotations.tasks.send_quotation_email", bind=True, max_retries=1, default_retry_delay=60)
def send_quotation_email(self, log_id: int) -> str:
    from quotations.services.sending import deliver

    try:
        return deliver(log_id)
    except Exception as exc:  # noqa: BLE001 - SMTP/storage failure: recorded on the row, retried once
        raise self.retry(exc=exc) from None
