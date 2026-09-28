"""WebP thumbnails for raster images (run by ``media.tasks.generate_thumbnail`` after the upload commits).

The thumbnail lives beside the original in the same backend (public thumbnails on the CDN, private ones private),
fits in ``MEDIA_THUMBNAIL_MAX_PX`` on the long edge, has EXIF orientation applied and carries no metadata. HEIC
originals (which browsers cannot display) therefore get a displayable preview.
"""

from __future__ import annotations

import io
import logging

from django.conf import settings
from PIL import Image, ImageOps

from media.models import MediaAsset
from media.services.assets import set_thumbnail, thumbnail_key_for
from media.services.policies import IMAGE_TYPES, THUMBNAIL_KINDS
from media.services.sniffing import open_image
from media.services.storage import storage_for

logger = logging.getLogger("flarize.media")


def render_thumbnail(data: bytes, max_px: int) -> bytes:
    image = ImageOps.exif_transpose(open_image(io.BytesIO(data)))
    image.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
    image = image.convert("RGBA" if has_alpha else "RGB")
    output = io.BytesIO()
    image.save(output, "WEBP", quality=80, method=4)
    return output.getvalue()


def generate(asset_uid: str) -> str | None:
    """Create the thumbnail for ``asset_uid``; returns its key, or ``None`` when there is nothing to do.

    Idempotent: an asset that already has a thumbnail, was deleted, or is not a raster image is skipped. Storage
    errors propagate (the task retries them); undecodable images are logged and skipped (retrying cannot help).
    """
    asset = MediaAsset.objects.filter(uid=asset_uid).first()
    if asset is None or asset.thumbnail_key or asset.kind not in THUMBNAIL_KINDS or asset.mime_type not in IMAGE_TYPES:
        return None
    storage = storage_for(asset.visibility)
    original = storage.read(asset.file)
    try:
        thumbnail = render_thumbnail(original, int(settings.MEDIA_THUMBNAIL_MAX_PX))
    except (OSError, ValueError, Image.DecompressionBombError):
        logger.warning("thumbnail skipped: image cannot be decoded", extra={"asset_uid": asset_uid}, exc_info=True)
        return None
    key = thumbnail_key_for(asset)
    storage.save(key, thumbnail)
    if not set_thumbnail(asset, key):
        # Deleted or thumbnailed concurrently: drop the object we just wrote.
        storage.delete(key)
        return None
    return key
