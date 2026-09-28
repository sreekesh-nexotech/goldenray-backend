"""Outbound e-mail through Django's e-mail backend: SMTP in staging/prod, console in dev, in-memory in tests.

* :func:`queue_email` — what services call. The message is validated immediately but handed to the Celery task
  ``core.tasks.send_email`` only **after the transaction commits** (a rolled-back write sends nothing, and a slow
  SMTP server never holds a request or a transaction open). If the broker is unreachable the message is sent
  synchronously as a fallback so security mail (password resets) is not lost.
* :func:`send_email` — synchronous delivery used by the task and by management commands.

Logs carry the category and the recipient count, never addresses or bodies (bodies may contain one-time links).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from functools import partial

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.db import transaction

logger = logging.getLogger("flarize.notifications")

MAX_RECIPIENTS = 50


def _recipients(to: str | Iterable[str]) -> list[str]:
    recipients = [to] if isinstance(to, str) else list(to)
    cleaned = []
    for address in recipients:
        address = (address or "").strip()
        validate_email(address)
        cleaned.append(address)
    if not cleaned:
        raise ValueError("An e-mail needs at least one recipient.")
    if len(cleaned) > MAX_RECIPIENTS:
        raise ValueError(f"An e-mail may have at most {MAX_RECIPIENTS} recipients.")
    return cleaned


def _subject(subject: str) -> str:
    subject = (subject or "").strip()
    if not subject or "\n" in subject or "\r" in subject:
        raise ValueError("The subject must be a single non-empty line.")
    return subject


def send_email(*, to: str | Iterable[str], subject: str, text: str, html: str | None = None, category: str = "") -> int:
    """Send now. Returns the number of messages the backend accepted (0 or 1). Raises on SMTP failure."""
    recipients = _recipients(to)
    message = EmailMultiAlternatives(subject=_subject(subject), body=text, from_email=settings.DEFAULT_FROM_EMAIL, to=recipients)
    if html:
        message.attach_alternative(html, "text/html")
    sent = message.send(fail_silently=False)
    logger.info("email sent", extra={"category": category or "general", "recipients": len(recipients)})
    return sent


def _enqueue(payload: dict) -> None:
    try:
        from core.tasks import send_email as send_email_task

        send_email_task.delay(**payload)
    except Exception:  # noqa: BLE001 - broker outage: deliver synchronously rather than lose security mail
        logger.warning("could not enqueue email; sending synchronously", extra={"category": payload.get("category") or "general"}, exc_info=True)
        try:
            send_email(**payload)
        except Exception:  # noqa: BLE001 - the business write already committed; never surface to the caller
            logger.exception("email delivery failed", extra={"category": payload.get("category") or "general"})


def queue_email(*, to: str | Iterable[str], subject: str, text: str, html: str | None = None, category: str = "") -> None:
    """Validate now, deliver after commit (see module docstring)."""
    payload = {"to": _recipients(to), "subject": _subject(subject), "text": text, "html": html, "category": category}
    transaction.on_commit(partial(_enqueue, payload), robust=True)
