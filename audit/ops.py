"""Operations hooks: the ``audit_partitions`` health check (non-critical: this month's and next month's partitions
must exist, otherwise rows land in the default partition) and the ops report's audit-volume section."""

from datetime import datetime

from django.db.models import Count

from core import health, ops_report


@health.register("audit_partitions", critical=False)
def audit_partitions_check() -> health.CheckResult:
    from audit.services.partitions import missing_upcoming

    missing = missing_upcoming(2)
    return health.CheckResult(ok=not missing, details={"missing": missing})


@ops_report.register("audit")
def audit_section(since: datetime) -> dict:
    from audit.models import AuditLog

    window = AuditLog.objects.filter(at__gte=since)
    top = list(window.values("action").annotate(count=Count("id")).order_by("-count", "action").values_list("action", "count")[:10])
    return {"rows": window.count(), "top_actions": dict(top)}
