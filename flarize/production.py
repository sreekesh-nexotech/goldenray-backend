"""Production configuration guard used by ``flarize/settings/prod.py`` (and staging, which imports prod)."""

from __future__ import annotations

from collections.abc import Mapping

from django.core.exceptions import ImproperlyConfigured

from flarize.client_ip import parse_networks
from flarize.keys import valid_fernet_key


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
    if problems:
        raise ImproperlyConfigured("Refusing to start with unsafe production settings: " + "; ".join(problems))
