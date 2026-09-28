"""The weekly ops report (PLAN §5.6 "Operations without a monitoring stack").

Apps register sections at startup (like health checks)::

    @ops_report.register("render_jobs")
    def render_jobs(since: datetime) -> dict:
        return {"failed": ..., "done": ...}

:func:`build` runs every section for the window ``[since, now)``; a failing section is reported with its exception
class instead of breaking the report. ``manage.py ops_report`` prints it (``--email`` sends it to ``OPS_EMAILS``) and
Beat sends it every Monday morning. HR contexts add agent-offline hours and unknown ADMS devices the same way.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

logger = logging.getLogger("flarize.ops")

Section = Callable[[datetime], dict]
_SECTIONS: dict[str, Section] = {}


def register(name: str):
    def decorator(fn: Section) -> Section:
        _SECTIONS[name] = fn
        return fn

    return decorator


def unregister(name: str) -> None:
    _SECTIONS.pop(name, None)


def registered() -> dict[str, Section]:
    return dict(_SECTIONS)


def build(*, days: int = 7, now: datetime | None = None) -> dict:
    now = now or timezone.now()
    since = now - timedelta(days=days)
    sections: dict[str, dict] = {}
    for name, fn in sorted(_SECTIONS.items()):
        try:
            sections[name] = fn(since)
        except Exception as exc:  # noqa: BLE001 - one broken section must not hide the others
            logger.exception("ops report section failed", extra={"section": name})
            sections[name] = {"error": exc.__class__.__name__}
    return {"since": since, "until": now, "sections": sections}


def render_text(report: dict) -> str:
    lines = [f"Flarize ops report {report['since']:%Y-%m-%d %H:%M} → {report['until']:%Y-%m-%d %H:%M} ({settings.TIME_ZONE})", ""]
    for name, values in report["sections"].items():
        lines.append(f"[{name}]")
        for key, value in values.items():
            rendered = value if isinstance(value, (str, int, float)) or value is None else json.dumps(value, cls=DjangoJSONEncoder, ensure_ascii=False)
            lines.append(f"  {key}: {rendered}")
        lines.append("")
    return "\n".join(lines)


def send(report: dict) -> int:
    """E-mail the report to ``OPS_EMAILS`` (after commit). Returns the number of recipients (0 = not configured)."""
    recipients = [address for address in getattr(settings, "OPS_EMAILS", []) if address]
    if not recipients:
        return 0
    from core.notifications import queue_email

    queue_email(to=recipients, subject=f"Flarize ops report {report['until']:%Y-%m-%d}", text=render_text(report), category="ops_report")
    return len(recipients)


@register("outbox")
def outbox_section(since: datetime) -> dict:
    from core.models import OutboxEvent
    from core.outbox import backlog_stats

    window = OutboxEvent.objects.filter(created_at__gte=since)
    return {**backlog_stats(), "events_in_window": window.count(), "parked_in_window": window.filter(parked_at__isnull=False).count()}
