"""request_render → QUEUED → (task on the documents queue, on commit) → RUNNING → DONE | FAILED."""

import uuid
from datetime import timedelta
from unittest import mock

import pytest
from celery.exceptions import Retry
from django.utils import timezone
from freezegun import freeze_time

from audit.models import AuditLog
from core.errors import DomainError
from core.models import OutboxEvent
from documents.models import RenderJob
from documents.services import jobs, renderers
from documents.tasks import render_job, sweep_render_jobs
from documents.tests.factories import REPORT_PAYLOAD, render
from media.models import MediaAsset

pytestmark = pytest.mark.django_db


def test_request_queues_and_renders_on_commit(make_user, document_storage, django_capture_on_commit_callbacks):
    user = make_user()
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        job = render(user)
    assert job.status == RenderJob.Status.QUEUED and job.requested_by == user and job.created_by == user
    assert job.payload == REPORT_PAYLOAD and len(job.payload_sha256) == 64
    assert AuditLog.objects.filter(action="documents.render_requested", object_uid=job.uid, actor=user).exists()
    assert RenderJob.objects.get(pk=job.pk).status == "QUEUED"  # nothing ran before commit
    for callback in callbacks:
        callback()
    job.refresh_from_db()
    assert job.status == RenderJob.Status.DONE and job.page_count == 1 and job.started_at and job.finished_at and job.error == ""
    asset = job.asset
    assert asset.visibility == MediaAsset.Visibility.PRIVATE and asset.kind == MediaAsset.Kind.DOCUMENT and asset.mime_type == "application/pdf"
    assert asset.folder == "documents/publish-report" and job.file == asset.file and asset.uploaded_by == user
    assert (document_storage / "private" / asset.file).read_bytes().startswith(b"%PDF-")
    event = OutboxEvent.objects.get(event_type="documents.render_completed")
    assert event.payload == {
        "job_uid": str(job.uid),
        "kind": "PUBLISH_REPORT",
        "object_type": "pricing.pricerelease",
        "object_uid": str(job.object_uid),
        "language": "en",
        "status": "DONE",
        "page_count": 1,
    }


def test_task_is_routed_to_the_documents_queue(settings):
    from flarize.celery import app

    assert app.amqp.router.route({}, "documents.tasks.render_job")["queue"].name == "documents"
    assert app.amqp.router.route({}, "media.tasks.generate_thumbnail")["queue"].name == "default"


def test_payload_hash_is_canonical():
    first, digest_a = jobs.canonical_payload({"b": 1, "a": "മലയാളം"})
    _, digest_b = jobs.canonical_payload({"a": "മലയാളം", "b": 1})
    assert digest_a == digest_b and first == {"a": "മലയാളം", "b": 1}


@pytest.mark.parametrize(
    "overrides,code,field",
    [
        ({"kind": "INVOICE"}, "validation_error", "kind"),
        ({"language": "fr"}, "validation_error", "language"),
        ({"object_type": "PricingRelease"}, "validation_error", "object_type"),
        ({"template": "../../etc"}, "validation_error", "template"),
        ({"template": "fancy"}, "template_not_found", "template"),
        ({"kind": "QUOTATION"}, "template_not_found", "template"),  # quotations ship their own templates
        ({"payload": ["not", "an", "object"]}, "validation_error", "payload"),
        ({"payload": {"blob": "x" * (1024 * 1024)}}, "payload_too_large", "payload"),
        ({"payload": {"when": object()}}, "validation_error", "payload"),
    ],
)
def test_request_validation(make_user, overrides, code, field):
    with pytest.raises(DomainError) as excinfo:
        render(make_user(), **overrides)
    assert excinfo.value.code == code and field in excinfo.value.errors
    assert not RenderJob.objects.exists()


def test_reuse_returns_the_existing_job(make_user):
    user, object_uid = make_user(), uuid.uuid4()
    first = render(user, object_uid=object_uid, reuse=True)
    assert render(user, object_uid=object_uid, reuse=True).pk == first.pk
    assert render(user, object_uid=object_uid, payload={**REPORT_PAYLOAD, "title": "Other"}, reuse=True).pk != first.pk
    assert render(user, object_uid=object_uid).pk != first.pk  # reuse is opt-in
    RenderJob.objects.filter(pk=first.pk).update(status="FAILED")
    assert render(user, object_uid=object_uid, reuse=True).pk != first.pk


def test_render_failure_marks_the_job_failed(make_user, django_capture_on_commit_callbacks):
    with mock.patch.object(renderers.StubRenderer, "render", side_effect=renderers.RenderError("bad markup")):
        with django_capture_on_commit_callbacks(execute=True):
            job = render(make_user())
    job.refresh_from_db()
    assert job.status == RenderJob.Status.FAILED and job.error == "RenderError: bad markup" and job.asset is None
    assert OutboxEvent.objects.filter(event_type="documents.render_failed").exists()
    assert AuditLog.objects.filter(action="documents.render_failed", object_uid=job.uid).exists()


def test_a_non_pdf_result_fails_the_job(make_user):
    job = render(make_user())
    with mock.patch.object(renderers.StubRenderer, "render", return_value=b"<html>oops</html>"):
        assert jobs.run_job(str(job.uid)) == RenderJob.Status.FAILED
    assert "valid PDF" in RenderJob.objects.get(pk=job.pk).error


def test_transient_failure_requeues_then_fails_after_retries(make_user):
    job = render(make_user())
    with mock.patch.object(renderers.StubRenderer, "render", side_effect=renderers.TransientRenderError("chromium died")):
        with pytest.raises(renderers.TransientRenderError):
            jobs.run_job(str(job.uid))
        assert RenderJob.objects.get(pk=job.pk).status == RenderJob.Status.QUEUED  # back in the queue for the retry
        with pytest.raises(Retry):  # first delivery: a retry is scheduled
            render_job.apply(args=[str(job.uid)], throw=True)
        assert render_job.apply(args=[str(job.uid)], retries=3).get() == "FAILED"  # last retry: give up
    job.refresh_from_db()
    assert job.status == RenderJob.Status.FAILED and "after 3 retries" in job.error


def test_storage_outage_is_transient(make_user):
    job = render(make_user())
    with mock.patch("media.services.storage.LocalStorage.save", side_effect=__import__("media.services.storage", fromlist=["StorageError"]).StorageError("disk")):
        with pytest.raises(renderers.TransientRenderError):
            jobs.run_job(str(job.uid))
    assert RenderJob.objects.get(pk=job.pk).status == RenderJob.Status.QUEUED


def test_a_job_runs_only_once(make_user):
    job = render(make_user())
    assert jobs.run_job(str(job.uid)) == RenderJob.Status.DONE
    assert jobs.run_job(str(job.uid)) is None  # duplicate delivery
    assert jobs.run_job(str(uuid.uuid4())) is None
    assert MediaAsset.objects.count() == 1


def test_fail_never_overrides_done(make_user):
    job = render(make_user())
    jobs.run_job(str(job.uid))
    assert jobs.fail(job.uid, "late") is None
    assert RenderJob.objects.get(pk=job.pk).status == RenderJob.Status.DONE


def test_sweeper(make_user):
    user = make_user()
    stuck = render(user)
    running = render(user)
    RenderJob.objects.filter(pk=running.pk).update(status="RUNNING", started_at=timezone.now())
    fresh = render(user)
    later = timezone.now() + timedelta(minutes=20)
    RenderJob.objects.filter(pk=fresh.pk).update(updated_at=later)
    with freeze_time(later), mock.patch.object(jobs, "_enqueue") as enqueue:
        from django.db import transaction

        with transaction.atomic():
            result = sweep_render_jobs.apply().get()
    assert result == {"requeued": 1, "failed": 1}
    assert RenderJob.objects.get(pk=running.pk).status == RenderJob.Status.FAILED
    assert "worker lost" in RenderJob.objects.get(pk=running.pk).error
    assert RenderJob.objects.get(pk=stuck.pk).status == RenderJob.Status.QUEUED
    assert enqueue.call_count in (0, 1)  # on_commit inside the test transaction


def test_queue_stats_and_health(make_user, client, settings):
    job = render(make_user())
    RenderJob.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(minutes=30))
    stats = jobs.queue_stats()
    assert stats["queued"] == 1 and stats["oldest_queued_age_seconds"] >= 1800
    body = client.get("/healthz").json()
    assert body["status"] == "degraded" and body["checks"]["render_queue"]["ok"] is False and body["checks"]["render_queue"]["critical"] is False


def test_jobs_for_an_object(make_user):
    object_uid = uuid.uuid4()
    render(make_user(), object_uid=object_uid)
    render(make_user(), object_uid=object_uid, language="ml")
    assert [job.language for job in jobs.jobs_for("pricing.pricerelease", object_uid)] == ["ml", "en"]
