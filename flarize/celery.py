"""Celery application. Configuration comes from Django settings (``CELERY_*`` keys)."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "flarize.settings.dev")

app = Celery("flarize")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
