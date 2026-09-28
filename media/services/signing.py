"""Signed URLs for private media (PLAN §2.1 "private (bucket + signed URL)").

``GET media/<uid>/signed-url/`` returns a URL to ``/api/<version>/media/download/<token>/`` valid for
``MEDIA_SIGNED_URL_TTL_SECONDS`` (10 minutes). The token is a ``django.core.signing.TimestampSigner`` object bound to
the asset uid, its checksum and the variant (original or thumbnail) under a media-specific salt: it cannot be forged
or re-targeted, it expires, and it stops working when the asset is deleted or replaced. It carries no user: the URL
itself is the capability (it is handed to an ``<img>``/download link, which cannot send a JWT), so it is short-lived
and never logged by the application.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.core import signing
from django.urls import reverse
from django.utils import timezone

from core.errors import DomainError, NotFound, PermissionDenied
from media.models import MediaAsset

SALT = "flarize.media.private-download"
ORIGINAL, THUMBNAIL = "o", "t"


class LinkExpired(DomainError):
    status = 410
    default_code = "link_expired"
    default_message = "This link has expired. Request a new one."


@dataclass(frozen=True)
class SignedUrl:
    url: str
    thumbnail_url: str | None
    expires_at: datetime | None


def _ttl() -> int:
    return int(settings.MEDIA_SIGNED_URL_TTL_SECONDS)


def _signer() -> signing.TimestampSigner:
    return signing.TimestampSigner(salt=SALT)


def make_token(asset: MediaAsset, variant: str = ORIGINAL) -> str:
    return _signer().sign_object({"a": str(asset.uid), "c": asset.checksum_sha256[:16], "v": variant})


def download_path(token: str, version: str) -> str:
    return reverse("staff:media-download", kwargs={"version": version, "token": token})


def signed_url(asset: MediaAsset, *, version: str, absolute=None) -> SignedUrl:
    """Public assets answer with their CDN URL (no expiry); private ones with fresh signed URLs."""
    build = absolute or (lambda path: path)
    if asset.is_public:
        from media.services.storage import sibling_public_url

        return SignedUrl(url=asset.cdn_url, thumbnail_url=sibling_public_url(asset.cdn_url, asset.file, asset.thumbnail_key), expires_at=None)
    expires_at = timezone.now() + timedelta(seconds=_ttl())
    url = build(download_path(make_token(asset, ORIGINAL), version))
    thumbnail = build(download_path(make_token(asset, THUMBNAIL), version)) if asset.thumbnail_key else None
    return SignedUrl(url=url, thumbnail_url=thumbnail, expires_at=expires_at)


def resolve_token(token: str) -> tuple[MediaAsset, str]:
    """``(asset, storage key)`` for a valid token; 410 when expired, 403 when forged/tampered, 404 when gone."""
    try:
        payload = _signer().unsign_object(token, max_age=_ttl())
    except signing.SignatureExpired:
        raise LinkExpired() from None
    except signing.BadSignature:
        raise PermissionDenied("signature_invalid", "This link is not valid.") from None
    if not isinstance(payload, dict) or payload.get("v") not in (ORIGINAL, THUMBNAIL):
        raise PermissionDenied("signature_invalid", "This link is not valid.")
    asset = MediaAsset.objects.filter(uid=payload.get("a"), visibility=MediaAsset.Visibility.PRIVATE).first()
    if asset is None or asset.checksum_sha256[:16] != payload.get("c"):
        raise NotFound("not_found", "The file no longer exists.")
    key = asset.file if payload["v"] == ORIGINAL else asset.thumbnail_key
    if not key:
        raise NotFound("not_found", "The file no longer exists.")
    return asset, key
