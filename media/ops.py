"""Operations hook: the ops report's media section (uploads in the window, stored volume by visibility)."""

from datetime import datetime

from django.db.models import Count, Sum

from core import ops_report


@ops_report.register("media")
def media_section(since: datetime) -> dict:
    from media.models import MediaAsset

    live = MediaAsset.objects.values("visibility").annotate(files=Count("id"), bytes=Sum("size_bytes"))
    window = MediaAsset.all_objects.filter(created_at__gte=since)
    return {
        "uploads_in_window": window.count(),
        "bytes_uploaded_in_window": window.aggregate(total=Sum("size_bytes"))["total"] or 0,
        "stored": {row["visibility"]: {"files": row["files"], "bytes": row["bytes"] or 0} for row in live},
    }
