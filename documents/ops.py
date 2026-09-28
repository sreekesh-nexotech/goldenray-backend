"""Operations hooks: the ``render_queue`` health check (PLAN §5.6 "oldest queued render job") and the ops report
section for render jobs. The health check is non-critical — a stuck documents worker degrades the instance (the ops
cron alerts) but never takes the API out of rotation."""

from datetime import datetime

from django.conf import settings
from django.db.models import Count

from core import health, ops_report


@health.register("render_queue", critical=False)
def render_queue_check() -> health.CheckResult:
    from documents.services.jobs import queue_stats

    stats = queue_stats()
    return health.CheckResult(ok=stats["oldest_queued_age_seconds"] < int(settings.DOCUMENTS_QUEUE_ALERT_SECONDS), details=stats)


@ops_report.register("render_jobs")
def render_jobs_section(since: datetime) -> dict:
    from documents.models import RenderJob

    window = RenderJob.objects.filter(created_at__gte=since)
    by_status = dict(window.values_list("status").annotate(count=Count("id")).values_list("status", "count"))
    failed = [
        {"job": str(uid), "kind": kind, "object": f"{object_type}:{object_uid}", "error": (error or "").splitlines()[0][:160] if error else ""}
        for uid, kind, object_type, object_uid, error in window.filter(status=RenderJob.Status.FAILED).order_by("-created_at").values_list("uid", "kind", "object_type", "object_uid", "error")[:10]
    ]
    return {"requested": window.count(), "by_status": by_status, "recent_failures": failed}
