"""Celery tasks owned by pricing (enqueued only inside ``transaction.on_commit``; the Beat entry lives in settings).

``pricing.tasks.expire_offers`` runs daily (``CELERY_BEAT_SCHEDULE["pricing.expire_offers"]``): ACTIVE and PAUSED
offers whose ``ends_on`` has passed become EXPIRED (``engines.offers.auto_expire_offers`` semantics).
"""

from celery import shared_task

from pricing.services.offers import expire_due_offers


@shared_task(name="pricing.tasks.expire_offers", ignore_result=True)
def expire_offers() -> list[str]:
    return expire_due_offers()
