"""Render job lifecycle (PLAN §1.4 "Documents"): ``QUEUED → RUNNING → DONE | FAILED``.

* :func:`request_render` — what owning contexts call inside their transaction. Validates the kind, language,
  template and payload, writes the QUEUED job (with the frozen payload and its SHA-256) and enqueues
  ``documents.tasks.render_job`` on the ``documents`` queue **on commit**.
* :func:`run_job` — the task body: claims the job (only a QUEUED job is claimed, so a duplicate delivery is a
  no-op), renders the template, converts it to PDF, stores the PDF as a PRIVATE media asset and marks the job DONE
  (or FAILED with the error). Emits ``documents.render_completed`` / ``documents.render_failed`` on the outbox.
* :func:`sweep` — Beat, every 5 minutes: re-enqueues QUEUED jobs whose message was lost and fails RUNNING jobs whose
  worker died.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta
from functools import partial

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone

from audit.services import record
from core.errors import DomainError, NotFound
from core.outbox import emit
from core.services import stamp_create
from documents.models import LANGUAGES, RenderJob
from documents.services import renderers, templates
from media.models import MediaAsset
from media.services.assets import store_bytes

logger = logging.getLogger("flarize.documents")

MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_ERROR_CHARS = 2000
OBJECT_TYPE_MAX = 64
DOCUMENTS_FOLDER = "documents"


def canonical_payload(payload) -> tuple[dict, str]:
    """``(json-safe payload, sha256)`` of the canonical JSON (sorted keys, compact separators, UTF-8)."""
    if not isinstance(payload, dict):
        raise DomainError("validation_error", "The document payload must be a JSON object.", errors={"payload": ["Must be an object."]})
    try:
        text = json.dumps(payload, cls=DjangoJSONEncoder, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        raise DomainError("validation_error", "The document payload is not JSON-serialisable.", errors={"payload": ["Not JSON-serialisable."]}) from None
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise DomainError("payload_too_large", "The document payload exceeds 1 MB.", errors={"payload": ["Larger than 1 MB."]})
    return json.loads(text), hashlib.sha256(encoded).hexdigest()


def _validate(kind: str, object_type: str, language: str) -> None:
    errors = {}
    if kind not in RenderJob.Kind.values:
        errors["kind"] = [f"Use one of {', '.join(RenderJob.Kind.values)}."]
    if not object_type or len(object_type) > OBJECT_TYPE_MAX or object_type.count(".") != 1 or object_type != object_type.lower():
        errors["object_type"] = ["Use '<app_label>.<model_name>'."]
    if language not in LANGUAGES:
        errors["language"] = [f"Use one of {', '.join(LANGUAGES)}."]
    if errors:
        raise DomainError("validation_error", "Invalid render request.", errors=errors)


def _enqueue(job_uid: str) -> None:
    from documents.tasks import render_job

    render_job.delay(job_uid)


@transaction.atomic
def request_render(kind: str, object_type: str, object_uid, template: str, language: str, payload: dict, user, *, reuse: bool = False) -> RenderJob:
    """Queue a rendering of ``payload`` with ``documents/<kind>/[<template>/]<language>.html``.

    With ``reuse=True`` an unfailed job for the same object, template, language and identical payload is returned
    instead of rendering again (documents pin their payload, so the PDF would be byte-for-byte the same content).
    """
    _validate(kind, object_type, language)
    template = template or templates.DEFAULT_TEMPLATE
    templates.ensure_template(kind, template, language)
    clean, digest = canonical_payload(payload)
    if reuse:
        existing = (
            RenderJob.objects.filter(kind=kind, object_type=object_type, object_uid=object_uid, template=template, language=language, payload_sha256=digest)
            .exclude(status=RenderJob.Status.FAILED)
            .order_by("-created_at")
            .first()
        )
        if existing is not None:
            return existing
    job = RenderJob(
        kind=kind, object_type=object_type, object_uid=object_uid, template=template, language=language, payload=clean, payload_sha256=digest, requested_by=user if getattr(user, "pk", None) else None
    )
    stamp_create(job, user)
    job.save()
    record(
        "documents.render_requested",
        obj=job,
        actor=user,
        after={"kind": kind, "object_type": object_type, "object_uid": str(object_uid), "template": template, "language": language, "payload_sha256": digest},
    )
    transaction.on_commit(partial(_enqueue, str(job.uid)), robust=True)
    return job


def get_job(uid) -> RenderJob:
    job = RenderJob.objects.select_related("requested_by", "asset").filter(uid=uid).first()
    if job is None:
        raise NotFound("not_found", "Render job not found.")
    return job


def jobs_for(object_type: str, object_uid) -> list[RenderJob]:
    return list(RenderJob.objects.filter(object_type=object_type, object_uid=object_uid).order_by("-created_at"))


# --------------------------------------------------------------------------------------------------------------------
# Execution (Celery, documents queue)
# --------------------------------------------------------------------------------------------------------------------
@transaction.atomic
def _claim(job_uid: str) -> RenderJob | None:
    job = RenderJob.objects.select_for_update(skip_locked=True).filter(uid=job_uid, status=RenderJob.Status.QUEUED).first()
    if job is None:
        return None
    job.versioned_update(None, status=RenderJob.Status.RUNNING, started_at=timezone.now(), error="")
    return job


def _event_payload(job: RenderJob) -> dict:
    return {"job_uid": str(job.uid), "kind": job.kind, "object_type": job.object_type, "object_uid": str(job.object_uid), "language": job.language, "status": job.status}


@transaction.atomic
def _complete(job: RenderJob, asset: MediaAsset, pages: int) -> None:
    job = RenderJob.objects.select_for_update().get(pk=job.pk)
    job.versioned_update(None, status=RenderJob.Status.DONE, file=asset.file, asset=asset, page_count=pages, error="", finished_at=timezone.now())
    record("documents.render_completed", obj=job, actor_kind="SYSTEM", after={"status": job.status, "page_count": pages, "asset": str(asset.uid)})
    emit("documents.render_completed", {**_event_payload(job), "page_count": pages}, aggregate_type="documents.renderjob", aggregate_uid=job.uid)


@transaction.atomic
def fail(job_uid, message: str) -> RenderJob | None:
    job = RenderJob.objects.select_for_update().filter(uid=job_uid).exclude(status=RenderJob.Status.DONE).first()
    if job is None:
        return None
    job.versioned_update(None, status=RenderJob.Status.FAILED, error=(message or "Rendering failed.")[:MAX_ERROR_CHARS], finished_at=timezone.now())
    record("documents.render_failed", obj=job, actor_kind="SYSTEM", after={"status": job.status, "error": job.error})
    emit("documents.render_failed", {**_event_payload(job), "error": job.error}, aggregate_type="documents.renderjob", aggregate_uid=job.uid)
    return job


@transaction.atomic
def _requeue(job: RenderJob) -> None:
    RenderJob.objects.filter(pk=job.pk, status=RenderJob.Status.RUNNING).update(status=RenderJob.Status.QUEUED, started_at=None, updated_at=timezone.now())


def run_job(job_uid: str) -> str | None:
    """Render one job. Returns the final status, or ``None`` when the job was not QUEUED (already handled).

    Raises :class:`~documents.services.renderers.TransientRenderError` after putting the job back to QUEUED, so the
    task can retry it; every other failure marks the job FAILED.
    """
    job = _claim(job_uid)
    if job is None:
        return None
    try:
        html = templates.render_html(job)
        pdf = renderers.get_renderer().render(html)
        pages = renderers.count_pages(pdf)
    except renderers.TransientRenderError:
        _requeue(job)
        raise
    except Exception as exc:  # noqa: BLE001 - any template/render failure is recorded on the job
        logger.warning("render job failed", extra={"job_uid": job_uid, "kind": job.kind}, exc_info=True)
        fail(job_uid, f"{exc.__class__.__name__}: {exc}")
        return RenderJob.Status.FAILED
    try:
        asset = store_bytes(
            user=job.requested_by,
            data=pdf,
            filename=f"{job.kind.lower()}-{job.object_uid}-{job.language}.pdf",
            kind=MediaAsset.Kind.DOCUMENT,
            visibility=MediaAsset.Visibility.PRIVATE,
            folder=f"{DOCUMENTS_FOLDER}/{job.kind.lower().replace('_', '-')}",
        )
    except DomainError as exc:
        if exc.code == "storage_unavailable":
            _requeue(job)
            raise renderers.TransientRenderError("Private storage is unavailable.") from exc
        fail(job_uid, f"{exc.code}: {exc.message}")
        return RenderJob.Status.FAILED
    _complete(job, asset, pages)
    return RenderJob.Status.DONE


# --------------------------------------------------------------------------------------------------------------------
# Sweeper and health
# --------------------------------------------------------------------------------------------------------------------
def sweep(*, now=None) -> dict[str, int]:
    """Re-enqueue QUEUED jobs untouched for ``DOCUMENTS_QUEUE_ALERT_SECONDS``; fail RUNNING jobs older than
    ``DOCUMENTS_STALE_RUNNING_SECONDS`` (their worker was lost)."""
    now = now or timezone.now()
    stale_running = RenderJob.objects.filter(status=RenderJob.Status.RUNNING, started_at__lt=now - timedelta(seconds=settings.DOCUMENTS_STALE_RUNNING_SECONDS))
    failed = 0
    for uid in list(stale_running.values_list("uid", flat=True)[:500]):
        if fail(uid, "The renderer stopped responding (worker lost or timed out)."):
            failed += 1
    cutoff = now - timedelta(seconds=settings.DOCUMENTS_QUEUE_ALERT_SECONDS)
    requeued = 0
    with transaction.atomic():
        stuck = list(RenderJob.objects.select_for_update(skip_locked=True).filter(status=RenderJob.Status.QUEUED, updated_at__lt=cutoff).values_list("pk", "uid")[:500])
        if stuck:
            RenderJob.objects.filter(pk__in=[pk for pk, _ in stuck]).update(updated_at=now)
            for _, uid in stuck:
                transaction.on_commit(partial(_enqueue, str(uid)), robust=True)
            requeued = len(stuck)
    return {"requeued": requeued, "failed": failed}


def queue_stats(*, now=None) -> dict[str, int]:
    now = now or timezone.now()
    queued = RenderJob.objects.filter(status=RenderJob.Status.QUEUED)
    oldest = queued.order_by("created_at").values_list("created_at", flat=True).first()
    return {
        "queued": queued.count(),
        "running": RenderJob.objects.filter(status=RenderJob.Status.RUNNING).count(),
        "oldest_queued_age_seconds": int((now - oldest).total_seconds()) if oldest else 0,
        "failed_last_24h": RenderJob.objects.filter(status=RenderJob.Status.FAILED, finished_at__gte=now - timedelta(hours=24)).count(),
    }
