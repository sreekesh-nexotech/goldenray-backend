"""Celery tasks owned by blog (enqueued only inside ``transaction.on_commit``; the Beat entry lives in settings).

``blog.tasks.publish_due_entries`` runs every minute (``CELERY_BEAT_SCHEDULE["blog.publish_due_entries"]``) and
publishes live DRAFT/REVIEW entries whose ``scheduled_for`` has passed (``blog.services.workflow``).
"""

from celery import shared_task

from blog.services.workflow import publish_due_entries as publish_due


@shared_task(name="blog.tasks.publish_due_entries", ignore_result=True)
def publish_due_entries() -> dict:
    return publish_due()
