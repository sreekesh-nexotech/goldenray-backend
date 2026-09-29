"""Sending an issued quotation to the customer (PLAN §3.4 ``…/versions/{n}/send/``) — e-mail with the PDF attached.

``send`` checks the version is issued and its PDF (in the requested language) is rendered, writes a QUEUED
``quotations_email_log`` row and enqueues ``quotations.tasks.send_quotation_email`` on commit. The task builds the
message through the platform's SMTP settings (``core.notifications.smtp_override``: the SMTP integration when the
environment delivers over SMTP, the console/in-memory backend in dev and tests), attaches the PDF read from private
storage, and marks the row SENT or FAILED (the error kept, no retry storm: one retry after a minute). WhatsApp has no
provider in the platform (standard: no irrelevant external APIs) and is refused with ``channel_unavailable``.
"""

from __future__ import annotations

import logging
from functools import partial

from django.conf import settings
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from audit.services import record
from core.errors import Conflict, DomainError
from core.notifications import smtp_override
from documents.models import RenderJob
from flarize.cache_utils import bump
from quotations.models import EmailChannel, EmailLog, EmailStatus, Version, VersionStatus
from quotations.services.common import CACHE_NAMESPACE

logger = logging.getLogger("flarize.quotations.sending")
SUBJECTS = {"en": "Your solar quotation {number}", "ml": "നിങ്ങളുടെ സോളാർ ക്വട്ടേഷൻ {number}"}
BODIES = {
    "en": "Dear {name},\n\nPlease find attached your solar quotation {number} (valid until {valid_until}).\n\nThank you,\n{company}",
    "ml": "പ്രിയ {name},\n\nനിങ്ങളുടെ സോളാർ ക്വട്ടേഷൻ {number} ഇതോടൊപ്പം ചേർക്കുന്നു ({valid_until} വരെ സാധുത).\n\nനന്ദി,\n{company}",
}


def _enqueue(log_id: int) -> None:
    from quotations.tasks import send_quotation_email

    send_quotation_email.delay(log_id)


@transaction.atomic
def send(version: Version, *, user, to: str, channel: str = "EMAIL", language: str | None = None) -> EmailLog:
    if channel != EmailChannel.EMAIL:
        raise DomainError("channel_unavailable", "Only e-mail is available; the platform has no WhatsApp provider.", errors={"channel": ["Use EMAIL."]})
    try:
        validate_email(to or "")
    except Exception:  # noqa: BLE001 - Django's ValidationError
        raise DomainError("validation_error", "Not an e-mail address.", errors={"to": ["Enter a valid e-mail address."]}) from None
    if version.status == VersionStatus.DRAFT:
        raise Conflict("version_not_issued", "Only an issued version can be sent.")
    language = language or version.language
    job = version.document_job_ml if language == "ml" else version.document_job
    if job is None or job.status != RenderJob.Status.DONE or job.asset_id is None:
        raise Conflict("document_not_ready", "The PDF has not been rendered yet.", errors={"status": [job.status if job else "MISSING"]})
    log = EmailLog.objects.create(
        version=version, channel=EmailChannel.EMAIL, to=to.strip().lower(), name=version.quotation.customer.name[:100], language=language, sent_by=user if getattr(user, "pk", None) else None
    )
    record("quotations.send_requested", obj=version.quotation, actor=user, after={"version": version.number, "language": language, "log": log.pk})
    transaction.on_commit(partial(_enqueue, log.pk), robust=True)
    bump(CACHE_NAMESPACE)
    return log


def deliver(log_id: int) -> str:
    """The task body: send the e-mail with the PDF; ``SENT`` / ``FAILED`` recorded on the log row."""
    from company.services.profile import current_profile
    from media.services.storage import private_storage

    log = EmailLog.objects.select_related("version__quotation__customer", "version__document_job__asset", "version__document_job_ml__asset").filter(pk=log_id).first()
    if log is None or log.status == EmailStatus.SENT:
        return "skipped"
    version = log.version
    job = version.document_job_ml if log.language == "ml" else version.document_job
    quotation = version.quotation
    company = current_profile().display_name or "Flarize"
    context = {"name": quotation.customer.name, "number": quotation.number, "valid_until": quotation.valid_until.isoformat() if quotation.valid_until else "-", "company": company}
    try:
        content = private_storage().read(job.asset.file)
        connection, from_email = smtp_override()
        message = EmailMessage(subject=SUBJECTS[log.language].format(**context), body=BODIES[log.language].format(**context), from_email=from_email, to=[log.to], connection=connection)
        message.attach(f"{quotation.number}-v{version.number}-{log.language}.pdf", content, "application/pdf")
        message.send(fail_silently=False)
    except Exception as exc:  # noqa: BLE001 - recorded on the row; the caller decides about retries
        EmailLog.objects.filter(pk=log.pk).update(status=EmailStatus.FAILED, error=f"{type(exc).__name__}: {exc}"[:500])
        logger.warning("quotation e-mail failed", extra={"log": log.pk, "error": type(exc).__name__})
        raise
    EmailLog.objects.filter(pk=log.pk).update(status=EmailStatus.SENT, sent_at=timezone.now(), error="", provider_id=getattr(settings, "EMAIL_BACKEND", "")[:128])
    return "sent"
