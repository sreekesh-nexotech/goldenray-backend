"""Pluggable health checks behind ``/healthz`` (PLAN §5.6).

Apps add checks at import time (e.g. documents registers "oldest queued render job")::

    @health.register("render_queue", critical=False)
    def render_queue() -> health.CheckResult:
        return health.CheckResult(ok=age < 600, details={"oldest_age_seconds": age})

Overall status: ``ok`` when every check passes; ``degraded`` when only non-critical checks fail (HTTP 200, the
instance still serves traffic — the ops cron alerts on it); ``fail`` when a critical check fails (HTTP 503, the
instance is taken out of rotation). A check that raises is reported as failed with the exception class name only.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from django.conf import settings
from django.core.cache import cache
from django.db import connection

logger = logging.getLogger("flarize.health")


@dataclass
class CheckResult:
    ok: bool
    details: dict = field(default_factory=dict)


Check = Callable[[], CheckResult]
_CHECKS: dict[str, tuple[Check, bool]] = {}

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_FAIL = "fail"


def register(name: str, *, critical: bool = True):
    def decorator(fn: Check) -> Check:
        _CHECKS[name] = (fn, critical)
        return fn

    return decorator


def unregister(name: str) -> None:
    _CHECKS.pop(name, None)


def registered_checks() -> dict[str, tuple[Check, bool]]:
    return dict(_CHECKS)


def run_checks() -> tuple[str, dict[str, dict]]:
    """Run every check. Returns ``(overall_status, {name: {ok, critical, duration_ms, ...details}})``."""
    results: dict[str, dict] = {}
    status = STATUS_OK
    for name, (check, critical) in sorted(_CHECKS.items()):
        started = time.monotonic()
        try:
            result = check()
            entry = {"ok": bool(result.ok), **result.details}
        except Exception as exc:  # noqa: BLE001 - a failing check is a result, not a crash
            logger.warning("health check failed", extra={"check": name}, exc_info=True)
            entry = {"ok": False, "error": exc.__class__.__name__}
        entry["critical"] = critical
        entry["duration_ms"] = int((time.monotonic() - started) * 1000)
        results[name] = entry
        if not entry["ok"]:
            status = STATUS_FAIL if critical or status == STATUS_FAIL else STATUS_DEGRADED
    return status, results


@register("database")
def database_check() -> CheckResult:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return CheckResult(ok=True)


@register("cache")
def cache_check() -> CheckResult:
    key = f"healthz:{uuid.uuid4().hex}"
    cache.set(key, "1", 10)
    ok = cache.get(key) == "1"
    cache.delete(key)
    return CheckResult(ok=ok)


@register("outbox", critical=False)
def outbox_check() -> CheckResult:
    from core.outbox import backlog_stats

    stats = backlog_stats()
    limit = int(getattr(settings, "OUTBOX_LAG_ALERT_SECONDS", 300))
    return CheckResult(ok=stats["oldest_age_seconds"] < limit and stats["parked"] == 0, details=stats)
