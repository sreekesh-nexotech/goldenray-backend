import hashlib

import factory
from django.core.files.uploadedfile import SimpleUploadedFile

from media.models import MediaAsset
from media.tests import files


class MediaAssetFactory(factory.django.DjangoModelFactory):
    """A row only (no stored object); use :func:`stored_asset` when the bytes must exist."""

    class Meta:
        model = MediaAsset

    visibility = MediaAsset.Visibility.PUBLIC
    kind = MediaAsset.Kind.IMAGE
    file = factory.Sequence(lambda n: f"library/2026/09/asset-{n}.jpg")
    cdn_url = factory.LazyAttribute(lambda asset: f"https://cdn.example.com/{asset.file}" if asset.visibility == MediaAsset.Visibility.PUBLIC else "")
    original_filename = "photo.jpg"
    mime_type = "image/jpeg"
    size_bytes = 1024
    width = 64
    height = 48
    checksum_sha256 = factory.Sequence(lambda n: hashlib.sha256(str(n).encode()).hexdigest())


def upload(name: str, data: bytes, content_type: str = "application/octet-stream") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, data, content_type=content_type)


def stored_asset(
    user=None, *, data: bytes | None = None, name: str = "photo.jpg", visibility: str = "PUBLIC", kind: str = "IMAGE", folder: str = "", allow_reserved: bool = False, **extra
) -> MediaAsset:
    """Upload real bytes through the service (the thumbnail task runs only if on-commit callbacks are executed)."""
    from media.services.assets import upload as upload_asset

    return upload_asset(
        user=user,
        file=upload(name, data if data is not None else files.jpeg()),
        visibility=visibility,
        kind=kind,
        folder=folder,
        allow_reserved=allow_reserved,
        **extra,
    )
