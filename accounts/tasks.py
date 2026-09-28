"""Celery tasks owned by accounts."""

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from accounts.services import retention


@shared_task(name="accounts.tasks.purge_auth_records", ignore_result=True)
def purge_auth_records() -> dict:
    """Daily retention: old login attempts, expired outstanding/blacklisted refresh tokens, spent reset tokens."""
    return retention.purge(now=timezone.now(), attempt_retention=settings.ACCOUNTS_LOGIN_ATTEMPT_RETENTION)
