"""Single-use download links for rendered documents (PLAN §2.1: "Download only via documents.signed_url(job) —
HMAC, 10 min, single use recorded in documents_download").

:func:`signed_url` checks the caller may see the job (``documents.access``), records a ``documents_download`` row and
returns ``/api/<version>/documents/download/<token>/`` where the token is a ``TimestampSigner`` signature (HMAC with
``SECRET_KEY`` under a documents-specific salt) of that row's uid. :func:`redeem` verifies the signature and age,
then consumes the row with one conditional UPDATE (``used_at IS NULL``), so two concurrent requests can never both
download: the second — and any later one — gets 410 ``link_used``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from audit.services import record
from core.errors import Conflict, DomainError, NotFound, PermissionDenied
from core.services import stamp_create
from documents import access
from documents.models import DocumentDownload, RenderJob

SALT = "flarize.documents.download"


class LinkGone(DomainError):
    status = 410
    default_code = "link_expired"
    default_message = "This link has expired. Request a new one."


@dataclass(frozen=True)
class IssuedLink:
    url: str
    expires_at: datetime


def _ttl() -> int:
    return int(settings.DOCUMENTS_DOWNLOAD_TTL_SECONDS)


def _signer() -> signing.TimestampSigner:
    return signing.TimestampSigner(salt=SALT)


@transaction.atomic
def signed_url(job: RenderJob, *, user, version: str, absolute=None) -> IssuedLink:
    access.ensure_can_view(user, job)
    if job.status != RenderJob.Status.DONE or not job.file:
        raise Conflict("document_not_ready", "The document has not been rendered yet.", errors={"status": [job.status]})
    download = DocumentDownload(job=job, issued_to=user, expires_at=timezone.now() + timedelta(seconds=_ttl()))
    stamp_create(download, user)
    download.save()
    record("documents.download_issued", obj=job, actor=user, after={"download": str(download.uid), "expires_at": download.expires_at.isoformat()})
    token = _signer().sign_object({"d": str(download.uid)})
    path = reverse("staff:documents-download", kwargs={"version": version, "token": token})
    return IssuedLink(url=absolute(path) if absolute else path, expires_at=download.expires_at)


@transaction.atomic
def redeem(token: str, *, ip: str | None, user_agent: str) -> RenderJob:
    """Consume a link. 403 forged, 410 expired/used, 404 when the document is gone."""
    try:
        payload = _signer().unsign_object(token, max_age=_ttl())
    except signing.SignatureExpired:
        raise LinkGone() from None
    except signing.BadSignature:
        raise PermissionDenied("signature_invalid", "This link is not valid.") from None
    download = DocumentDownload.objects.select_related("job", "job__asset").filter(uid=(payload or {}).get("d")).first() if isinstance(payload, dict) else None
    if download is None:
        raise PermissionDenied("signature_invalid", "This link is not valid.")
    now = timezone.now()
    consumed = DocumentDownload.objects.filter(pk=download.pk, used_at__isnull=True, expires_at__gt=now).update(
        used_at=now, used_ip=ip or None, used_user_agent=(user_agent or "")[:255], updated_at=now
    )
    if not consumed:
        download.refresh_from_db(fields=["used_at"])
        if download.used_at is not None:
            raise LinkGone("link_used", "This link has already been used. Request a new one.")
        raise LinkGone()
    job = download.job
    if job.status != RenderJob.Status.DONE or not job.file:
        raise NotFound("not_found", "The document no longer exists.")
    record("documents.downloaded", obj=job, actor=download.issued_to, after={"download": str(download.uid)})
    return job
