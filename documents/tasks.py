"""Celery tasks owned by documents (routed to the ``documents`` queue; enqueued only inside ``transaction.on_commit``)."""

from celery import shared_task

from documents.services.renderers import TransientRenderError

MAX_RETRIES = 3


@shared_task(name="documents.tasks.render_job", bind=True, ignore_result=True, max_retries=MAX_RETRIES, acks_late=True)
def render_job(self, job_uid: str):
    """Render one job; a Chromium start failure is retried with backoff (the job goes back to QUEUED), then failed."""
    from documents.services import jobs

    try:
        return jobs.run_job(job_uid)
    except TransientRenderError as exc:
        if self.request.retries >= MAX_RETRIES:
            jobs.fail(job_uid, f"Renderer unavailable after {MAX_RETRIES} retries: {exc}")
            return "FAILED"
        raise self.retry(exc=exc, countdown=30 * (2**self.request.retries)) from exc


@shared_task(name="documents.tasks.sweep_render_jobs", ignore_result=True)
def sweep_render_jobs() -> dict:
    from documents.services import jobs

    return jobs.sweep()
