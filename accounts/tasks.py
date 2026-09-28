"""Celery tasks owned by accounts."""

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from accounts.services import retention


@shared_task(name="accounts.tasks.purge_auth_records", ignore_result=True)
def purge_auth_records() -> dict:
    """Daily retention: old login attempts, expired outstanding/blacklisted refresh tokens, spent reset tokens."""
    return retention.purge(now=timezone.now(), attempt_retention=settings.ACCOUNTS_LOGIN_ATTEMPT_RETENTION)


@shared_task(name="accounts.tasks.send_password_reset", ignore_result=True)
def send_password_reset(email: str, request_id: str | None = None, ip: str | None = None) -> None:
    """``auth/password/reset-request/`` (enqueued on commit): look the address up and e-mail a link when it belongs to
    an active account. Runs outside the request so its cost never reveals whether the account exists."""
    from accounts.services.auth import process_password_reset_request
    from audit import context as audit_context

    with audit_context.bind(request_id=request_id, ip=ip):
        process_password_reset_request(email)
