"""Production settings. Importing this module validates the configuration and refuses to start on unsafe values."""

from decouple import config

from flarize.production import validate_production_settings
from flarize.settings.base import *  # noqa: F401,F403

DEBUG = config("DEBUG", default=False, cast=bool)
# Docs are private in prod: only API_DOCS_ALLOWED_NETWORKS (and nginx's docs-allow.conf) reach them (PLAN §5.4).
API_DOCS_PUBLIC = config("API_DOCS_PUBLIC", default=False, cast=bool)

# HTTPS everywhere except the terminal receiver (plain-HTTP listener on :8080) and the internal health probe.
SECURE_SSL_REDIRECT = True
SECURE_REDIRECT_EXEMPT = [r"^iclock/", r"^healthz$"]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = config("SECURE_HSTS_SECONDS", default=31536000, cast=int)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = False
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"

# `check --deploy` must be clean (final review). Two checks do not apply:
# * security.W003 (no CsrfViewMiddleware): nothing authenticates by cookie — no session middleware, every API reads a
#   bearer token (JWT, service token, signed link) from the request, so there is no ambient credential to forge with;
# * security.W021 (HSTS preload): submitting the domain to the browsers' preload list is an owner decision
#   (docs/reviews/final-review.md "Decisions for the owner"); SECURE_HSTS_PRELOAD stays False until then.
SILENCED_SYSTEM_CHECKS = [*SILENCED_SYSTEM_CHECKS, "security.W003", "security.W021"]  # noqa: F405


validate_production_settings(globals())
