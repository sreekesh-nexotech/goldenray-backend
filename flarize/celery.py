"""Celery application (PLAN §1.4 "Async", §5.2). Configuration comes from Django settings (``CELERY_*`` keys).

* Tasks are autodiscovered from every installed app's ``tasks.py``.
* Queues: ``default`` (outbox drain, e-mail, thumbnails, imports), ``documents`` (Playwright rendering; its own worker
  so PDF bursts never block ingestion) and ``ingest`` (attendance/devices). Every queue has a consumer in
  ``deploy/docker-compose.yml``: ``worker-default -Q default,ingest`` and ``worker-documents -Q documents``
  (``core/tests/test_deploy.py`` fails otherwise).
* Beat runs once (``beat`` service) with django-celery-beat's ``DatabaseScheduler``; ``CELERY_BEAT_SCHEDULE``
  (outbox drain every 5 s, monthly audit partitions, render-job sweeper, auth retention) is synced into it on start.
* Tasks are only ever enqueued inside ``transaction.on_commit``; ``CELERY_TASK_PUBLISH_RETRY`` retries a publish
  that races a broker blip after the commit.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "flarize.settings.dev")

app = Celery("flarize")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
