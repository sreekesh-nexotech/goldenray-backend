"""Flarize Platform project package.

Importing the Celery app here makes ``@shared_task`` bind to it whenever Django starts.
"""

from flarize.celery import app as celery_app

__all__ = ("celery_app",)
