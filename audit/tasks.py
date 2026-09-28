"""Celery tasks owned by audit (enqueued only inside ``transaction.on_commit``)."""

import logging

from celery import shared_task
from django.conf import settings

logger = logging.getLogger("flarize.audit")


@shared_task(name="audit.tasks.ensure_partitions", ignore_result=True)
def ensure_partitions(months: int = 3) -> dict:
    """Beat, monthly: create the upcoming ``audit_log`` partitions and keep them closed to ``DB_APP_ROLE``.

    Creating partitions needs the table owner. Where the workers connect as a separate application role (prod with
    ``DB_APP_ROLE``), this task cannot do it and says so in the log; ``deploy/release.sh`` and the monthly cron run
    ``manage.py ensure_audit_partitions`` as the owner, and the ``audit_partitions`` health check degrades
    ``/healthz`` when next month's partition is missing (docs/ops/audit-log.md).
    """
    from audit.services import partitions

    if not partitions.connected_as_owner():
        logger.warning("audit partitions not maintained: the worker is not connected as the audit_log owner (run ensure_audit_partitions as the owner)")
        return {"skipped": "not_owner", "created": []}
    role = (getattr(settings, "DB_APP_ROLE", "") or "").strip() or None
    if role and role == partitions.table_owner():
        role = None
    results = partitions.ensure_partitions(months, app_role=role)
    if role:
        partitions.apply_append_only_privileges(role)
    created = [result.name for result in results if result.created]
    if created:
        logger.info("audit partitions created", extra={"partitions": created})
    return {"skipped": None, "created": created}
