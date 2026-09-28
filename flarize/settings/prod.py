"""Production settings. Importing this module validates the configuration and refuses to start on unsafe values."""

from decouple import config

from flarize.production import validate_production_settings
from flarize.settings.base import *  # noqa: F401,F403

DEBUG = config("DEBUG", default=False, cast=bool)
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

if not API_DOCS_PUBLIC:
    SPECTACULAR_SETTINGS["SERVE_PERMISSIONS"] = ["rest_framework.permissions.IsAuthenticated"]
    SPECTACULAR_SETTINGS["SERVE_AUTHENTICATION"] = ["accounts.authentication.SessionAwareJWTAuthentication"]


validate_production_settings(globals())
