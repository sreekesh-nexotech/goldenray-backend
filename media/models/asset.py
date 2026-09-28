"""``media_asset`` (PLAN §2.1): every uploaded or generated file, public (Bunny CDN) or private (signed URL)."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import BaseModel


class MediaAsset(BaseModel):
    class Visibility(models.TextChoices):
        PUBLIC = "PUBLIC", "Public (CDN)"
        PRIVATE = "PRIVATE", "Private (signed URL)"

    class Kind(models.TextChoices):
        IMAGE = "IMAGE", "Image"
        DOCUMENT = "DOCUMENT", "Document"
        SIGNATURE = "SIGNATURE", "Signature"
        RESUME = "RESUME", "Resume"
        PHOTO = "PHOTO", "Photo"

    visibility = models.CharField(max_length=8, choices=Visibility.choices)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    # Storage key inside the visibility's backend (never a URL, never exposed for private assets).
    file = models.CharField(max_length=512)
    # Public CDN URL; always empty for private assets (DB check below).
    cdn_url = models.CharField(max_length=512, blank=True, default="")
    original_filename = models.CharField(max_length=255)
    # Sniffed from the content, never taken from the client's Content-Type header.
    mime_type = models.CharField(max_length=120)
    size_bytes = models.BigIntegerField()
    width = models.IntegerField(null=True, blank=True)
    height = models.IntegerField(null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)
    checksum_sha256 = models.CharField(max_length=64)
    alternative_text = models.CharField(max_length=255, blank=True, default="")
    caption = models.TextField(blank=True, default="")
    thumbnail_key = models.CharField(max_length=512, blank=True, default="")
    folder = models.CharField(max_length=120, blank=True, default="")
    # Attribution: SET_NULL so removing a user never removes files.
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "media_asset"
        ordering = ["-created_at", "-id"]
        indexes = [
            # Media library list: filter by visibility/kind, newest first.
            models.Index(fields=["visibility", "kind", "created_at"], name="media_asset_vis_kind_created"),
            # Folder filter of the media library.
            models.Index(fields=["folder", "created_at"], name="media_asset_folder_created"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(visibility__in=["PUBLIC", "PRIVATE"]), name="media_asset_visibility_valid"),
            models.CheckConstraint(condition=Q(kind__in=["IMAGE", "DOCUMENT", "SIGNATURE", "RESUME", "PHOTO"]), name="media_asset_kind_valid"),
            # A private asset never carries a public URL.
            models.CheckConstraint(condition=Q(visibility="PUBLIC") | Q(cdn_url=""), name="media_asset_cdn_url_public_only"),
            # Resumes and signatures are personal data: never on the CDN.
            models.CheckConstraint(condition=~Q(kind__in=["RESUME", "SIGNATURE"]) | Q(visibility="PRIVATE"), name="media_asset_sensitive_kinds_private"),
            models.CheckConstraint(condition=Q(size_bytes__gte=0), name="media_asset_size_non_negative"),
            # One row per storage object: deleting one row's file can never remove another row's file.
            models.UniqueConstraint(fields=["visibility", "file"], name="media_asset_storage_key_uniq"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.original_filename}"

    @property
    def is_public(self) -> bool:
        return self.visibility == self.Visibility.PUBLIC

    @property
    def is_image(self) -> bool:
        return self.mime_type.startswith("image/")
