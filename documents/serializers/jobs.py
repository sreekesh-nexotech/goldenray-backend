"""Render job status and download-link shapes. The payload and the storage key are never serialised."""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from documents.models import RenderJob


class RenderJobSerializer(serializers.ModelSerializer):
    requested_by = serializers.SlugRelatedField(slug_field="uid", read_only=True, help_text="Requesting user's uid.")
    error = serializers.SerializerMethodField(help_text="Why rendering failed (FAILED jobs only).")
    download_available = serializers.SerializerMethodField()

    class Meta:
        model = RenderJob
        fields = [
            "uid",
            "kind",
            "object_type",
            "object_uid",
            "template",
            "language",
            "status",
            "page_count",
            "error",
            "download_available",
            "payload_sha256",
            "requested_by",
            "created_at",
            "started_at",
            "finished_at",
            "version",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_error(self, job: RenderJob) -> str | None:
        return (job.error or None) if job.status == RenderJob.Status.FAILED else None

    @extend_schema_field(serializers.BooleanField())
    def get_download_available(self, job: RenderJob) -> bool:
        return job.status == RenderJob.Status.DONE and bool(job.file)


class DownloadLinkSerializer(serializers.Serializer):
    url = serializers.CharField(help_text="Single-use download URL.")
    expires_at = serializers.DateTimeField()
