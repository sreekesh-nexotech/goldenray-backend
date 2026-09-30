"""Production configuration guard used by ``flarize/settings/prod.py`` (and staging, which imports prod)."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

from flarize.client_ip import parse_networks
from flarize.keys import valid_fernet_key

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
NON_DELIVERING_EMAIL_BACKENDS = frozenset(
    {
        "django.core.mail.backends.console.EmailBackend",
        "django.core.mail.backends.locmem.EmailBackend",
        "django.core.mail.backends.dummy.EmailBackend",
        "django.core.mail.backends.filebased.EmailBackend",
    }
)


def validate_production_settings(settings: Mapping) -> None:
    """Raise ImproperlyConfigured on any unsafe production value (PLAN §5.3)."""
    problems = []
    secret = settings["SECRET_KEY"]
    if not secret or secret == settings["PLACEHOLDER_SECRET_KEY"] or secret.startswith("django-insecure") or len(secret) < 50:
        problems.append("SECRET_KEY is missing, a placeholder, or shorter than 50 characters")
    if settings["DEBUG"]:
        problems.append("DEBUG must be False")
    if not [host for host in settings["ALLOWED_HOSTS"] if host]:
        problems.append("ALLOWED_HOSTS is empty")
    if not settings["JWT_PRIVATE_KEY"] or not settings["JWT_PUBLIC_KEY"]:
        problems.append("RS256 keys are missing (JWT_PRIVATE_KEY_PATH / JWT_PUBLIC_KEY_PATH)")
    fernet_keys = [key for key in settings["FERNET_KEYS"] if key]
    if not fernet_keys:
        problems.append("FERNET_KEYS is empty")
    elif not all(valid_fernet_key(key) for key in fernet_keys):
        problems.append("FERNET_KEYS contains an invalid key")
    proxies = [cidr for cidr in settings["TRUSTED_PROXIES"] if cidr]
    if not proxies:
        problems.append("TRUSTED_PROXIES is empty")
    else:
        try:
            parse_networks(tuple(proxies))
        except ValueError as exc:
            problems.append(f"TRUSTED_PROXIES is invalid: {exc}")
    try:
        parse_networks(tuple(cidr for cidr in settings.get("API_DOCS_ALLOWED_NETWORKS", ()) if cidr))
    except ValueError as exc:
        problems.append(f"API_DOCS_ALLOWED_NETWORKS is invalid: {exc}")
    if not str(settings.get("ACCOUNTS_PASSWORD_RESET_URL", "")).startswith("https://"):
        problems.append("PASSWORD_RESET_URL must be an https:// URL (it carries one-time reset tokens)")
    frontend = urlsplit(str(settings.get("FRONTEND_BASE_URL", "")))
    if frontend.scheme != "https" or not frontend.hostname or frontend.hostname in LOOPBACK_HOSTS:
        problems.append("FRONTEND_BASE_URL must be the https:// origin of the public website (it is published in JSON-LD, previews and sitemaps)")
    if settings.get("EMAIL_BACKEND", "") in NON_DELIVERING_EMAIL_BACKENDS:
        problems.append("EMAIL_BACKEND does not deliver mail (console/locmem/dummy/filebased); password resets would be lost")
    if settings.get("MEDIA_PUBLIC_BACKEND") != "bunny":
        problems.append("MEDIA_PUBLIC_BACKEND must be 'bunny' (the local public backend is served only while DEBUG)")
    if settings.get("LEADS_OTP_BACKEND") != "twilio":
        problems.append("LEADS_OTP_BACKEND must be 'twilio' (the fake backend accepts the code 000000 for any number)")
    if settings.get("DOCUMENTS_RENDERER") != "playwright":
        problems.append("DOCUMENTS_RENDERER must be 'playwright' (the stub renderer produces placeholder PDFs)")
    # Without the app role the release's REVOKE steps (audit_log, inventory_movement, attendance_raw_punch) log "left
    # unchanged" and exit 0, so the append-only ledgers would silently accept UPDATE/DELETE (docs/ops/audit-log.md).
    if not str(settings.get("DB_APP_ROLE", "") or "").strip():
        problems.append("DB_APP_ROLE must name the application's database role (the append-only ledgers are enforced by REVOKE from it)")
    private_root = str(settings.get("PRIVATE_MEDIA_ROOT", ""))
    public_root = str(settings.get("PUBLIC_MEDIA_ROOT", ""))
    if not private_root or (public_root and (private_root == public_root or private_root.startswith(public_root.rstrip("/") + "/"))):
        problems.append("PRIVATE_MEDIA_ROOT must be set and must not live inside PUBLIC_MEDIA_ROOT")
    if problems:
        raise ImproperlyConfigured("Refusing to start with unsafe production settings: " + "; ".join(problems))
