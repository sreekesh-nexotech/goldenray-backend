"""Staging settings: production configuration (same validation) with the API docs reachable for UAT."""

from decouple import config

from flarize.settings.prod import *  # noqa: F401,F403

API_DOCS_PUBLIC = config("API_DOCS_PUBLIC", default=True, cast=bool)
