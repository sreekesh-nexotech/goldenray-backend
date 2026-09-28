"""Celery wiring: autodiscovery, queues, routes and the Beat schedule (PLAN §1.4 "Async", §5.2)."""

from datetime import timedelta

from celery.schedules import crontab
from django.conf import settings

from flarize.celery import app


def test_tasks_are_autodiscovered():
    app.loader.import_default_modules()
    for name in (
        "core.tasks.drain_outbox",
        "core.tasks.send_email",
        "accounts.tasks.purge_auth_records",
        "audit.tasks.ensure_partitions",
        "media.tasks.generate_thumbnail",
        "media.tasks.delete_stored_files",
        "documents.tasks.render_job",
        "documents.tasks.sweep_render_jobs",
    ):
        assert name in app.tasks, name


def test_queues_and_routes():
    assert [queue.name for queue in settings.CELERY_TASK_QUEUES] == ["default", "documents", "ingest"]
    assert settings.CELERY_TASK_DEFAULT_QUEUE == "default"
    route = app.amqp.router.route
    assert route({}, "documents.tasks.render_job")["queue"].name == "documents"
    assert route({}, "devices.tasks.anything")["queue"].name == "ingest"
    assert route({}, "attendance.tasks.anything")["queue"].name == "ingest"
    assert route({}, "core.tasks.drain_outbox")["queue"].name == "default"


def test_beat_schedule():
    schedule = settings.CELERY_BEAT_SCHEDULE
    assert schedule["core.drain_outbox"] == {"task": "core.tasks.drain_outbox", "schedule": 5.0}
    partitions = schedule["audit.ensure_partitions"]
    assert partitions["task"] == "audit.tasks.ensure_partitions" and isinstance(partitions["schedule"], crontab)
    assert partitions["schedule"].day_of_month == {1}  # monthly
    assert schedule["documents.sweep_render_jobs"]["task"] == "documents.tasks.sweep_render_jobs"
    app.loader.import_default_modules()
    for entry in schedule.values():
        assert entry["task"] in app.tasks, entry["task"]
    assert settings.CELERY_BEAT_SCHEDULER == "django_celery_beat.schedulers:DatabaseScheduler"


def test_worker_discipline():
    assert settings.CELERY_ACCEPT_CONTENT == ["json"] and settings.CELERY_TASK_SERIALIZER == "json"
    assert settings.CELERY_TIMEZONE == "Asia/Kolkata" and settings.CELERY_TASK_PUBLISH_RETRY is True
    assert settings.CELERY_TASK_TIME_LIMIT == 120 and settings.CELERY_WORKER_PREFETCH_MULTIPLIER == 1
    assert timedelta(seconds=settings.CELERY_TASK_SOFT_TIME_LIMIT) < timedelta(seconds=settings.CELERY_TASK_TIME_LIMIT)
