"""Signed preview links for unpublished entries (staff ``…/preview/`` → public ``content/preview/<token>/``).

A preview token is a ``TimestampSigner`` signature over the entry uid with its own salt, valid for
``BLOG_PREVIEW_TTL_SECONDS`` (default one hour) and reusable until then (a preview page reloads). It shows the entry's
**current** state whatever its status, so an editor can iterate on a draft with one link. It dies with the entry
(soft delete) and is useless for any other entry. Issuing one needs ``blogs.view`` and is audited; the public
response is ``private, no-store`` and ``noindex``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.conf import settings
from django.core import signing
from django.utils import timezone

from audit.services import record
from blog.models import Entry
from core.errors import DomainError, NotFound

SALT = "flarize.blog.entry-preview"


class LinkExpired(DomainError):
    status = 410
    default_code = "link_expired"
    default_message = "This preview link has expired; open a new one from the Studio."


@dataclass(frozen=True)
class PreviewLink:
    token: str
    path: str
    expires_at: dt.datetime


def ttl_seconds() -> int:
    return int(getattr(settings, "BLOG_PREVIEW_TTL_SECONDS", 3600))


def _signer() -> signing.TimestampSigner:
    return signing.TimestampSigner(salt=SALT)


def issue_preview(entry: Entry, *, user, version: str) -> PreviewLink:
    token = _signer().sign(str(entry.uid))
    record("blog.entry_preview_issued", obj=entry, actor=user, after={"status": entry.status, "ttl_seconds": ttl_seconds()})
    return PreviewLink(token=token, path=f"/api/public/{version}/content/preview/{token}/", expires_at=timezone.now() + dt.timedelta(seconds=ttl_seconds()))


def resolve_preview(token: str) -> Entry:
    try:
        uid = _signer().unsign(token, max_age=ttl_seconds())
    except signing.SignatureExpired:
        raise LinkExpired() from None
    except signing.BadSignature:
        raise DomainError("signature_invalid", "This preview link is not valid.", status=403) from None
    entry = Entry.objects.filter(uid=uid).select_related("collection").first()
    if entry is None:
        raise NotFound("entry_not_found", "The previewed entry no longer exists.")
    return entry
