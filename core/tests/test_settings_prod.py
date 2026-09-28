"""prod.py refuses to start on unsafe configuration (PLAN §5.3); dev/test key generation is race-free."""

import os
import subprocess
import sys

import pytest
from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from flarize.keys import ensure_dev_fernet_key, ensure_dev_jwt_keypair, generate_rsa_private_pem, load_jwt_keys, public_pem_from_private
from flarize.production import validate_production_settings

GOOD_SECRET = "k" * 64


def _valid(**overrides):
    base = {
        "SECRET_KEY": GOOD_SECRET,
        "PLACEHOLDER_SECRET_KEY": "django-insecure-placeholder",
        "DEBUG": False,
        "ALLOWED_HOSTS": ["flarize.com"],
        "JWT_PRIVATE_KEY": "private",
        "JWT_PUBLIC_KEY": "public",
        "FERNET_KEYS": [Fernet.generate_key().decode()],
        "TRUSTED_PROXIES": ["172.16.0.0/12"],
        "ACCOUNTS_PASSWORD_RESET_URL": "https://flarize.com/studio/reset-password",
        "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
        "MEDIA_PUBLIC_BACKEND": "bunny",
        "DOCUMENTS_RENDERER": "playwright",
        "PUBLIC_MEDIA_ROOT": "/srv/flarize/media/public",
        "PRIVATE_MEDIA_ROOT": "/srv/flarize/media/private",
        "FRONTEND_BASE_URL": "https://flarize.com",
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"SECRET_KEY": "django-insecure-placeholder"}, "SECRET_KEY"),
        ({"SECRET_KEY": "django-insecure-" + "x" * 60}, "SECRET_KEY"),
        ({"SECRET_KEY": "short"}, "SECRET_KEY"),
        ({"SECRET_KEY": ""}, "SECRET_KEY"),
        ({"DEBUG": True}, "DEBUG"),
        ({"ALLOWED_HOSTS": []}, "ALLOWED_HOSTS"),
        ({"ALLOWED_HOSTS": [""]}, "ALLOWED_HOSTS"),
        ({"JWT_PRIVATE_KEY": None}, "RS256"),
        ({"JWT_PUBLIC_KEY": None}, "RS256"),
        ({"FERNET_KEYS": []}, "FERNET_KEYS is empty"),
        ({"FERNET_KEYS": ["nope"]}, "invalid key"),
        ({"TRUSTED_PROXIES": []}, "TRUSTED_PROXIES is empty"),
        ({"TRUSTED_PROXIES": ["300.0.0.0/8"]}, "TRUSTED_PROXIES is invalid"),
        ({"API_DOCS_ALLOWED_NETWORKS": ["office"]}, "API_DOCS_ALLOWED_NETWORKS is invalid"),
        ({"ACCOUNTS_PASSWORD_RESET_URL": "http://flarize.com/studio/reset-password"}, "PASSWORD_RESET_URL"),
        ({"EMAIL_BACKEND": "django.core.mail.backends.console.EmailBackend"}, "EMAIL_BACKEND"),
        ({"MEDIA_PUBLIC_BACKEND": "local"}, "MEDIA_PUBLIC_BACKEND"),
        ({"DOCUMENTS_RENDERER": "stub"}, "DOCUMENTS_RENDERER"),
        ({"PRIVATE_MEDIA_ROOT": "/srv/flarize/media/public/private"}, "PRIVATE_MEDIA_ROOT"),
        ({"PRIVATE_MEDIA_ROOT": "/srv/flarize/media/public"}, "PRIVATE_MEDIA_ROOT"),
        ({"PRIVATE_MEDIA_ROOT": ""}, "PRIVATE_MEDIA_ROOT"),
        ({"FRONTEND_BASE_URL": "http://localhost:3000"}, "FRONTEND_BASE_URL"),
        ({"FRONTEND_BASE_URL": ""}, "FRONTEND_BASE_URL"),
        ({"FRONTEND_BASE_URL": "https://localhost:3000"}, "FRONTEND_BASE_URL"),
    ],
)
def test_validation_refuses_unsafe_values(overrides, message):
    with pytest.raises(ImproperlyConfigured, match=message):
        validate_production_settings(_valid(**overrides))


def test_validation_accepts_a_safe_configuration():
    validate_production_settings(_valid())


def _import_prod(env_overrides, tmp_path):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("DJANGO_", "SECRET_KEY", "JWT_", "FERNET", "TRUSTED", "ALLOWED", "DEBUG", "PASSWORD_RESET", "EMAIL_", "MEDIA_", "PUBLIC_MEDIA", "PRIVATE_MEDIA", "DOCUMENTS_", "FRONTEND_BASE_URL")
        )
    }
    env.update(env_overrides)
    return subprocess.run([sys.executable, "-c", "import flarize.settings.prod"], cwd=settings.BASE_DIR, env=env, capture_output=True, text=True, timeout=60)


def test_importing_prod_with_defaults_refuses_to_start(tmp_path):
    result = _import_prod({}, tmp_path)
    assert result.returncode != 0
    assert "Refusing to start with unsafe production settings" in result.stderr
    for fragment in ["SECRET_KEY", "ALLOWED_HOSTS", "RS256", "FERNET_KEYS", "TRUSTED_PROXIES", "PASSWORD_RESET_URL", "FRONTEND_BASE_URL"]:
        assert fragment in result.stderr


def test_importing_prod_with_a_complete_environment_succeeds(tmp_path):
    private = tmp_path / "jwt_private.pem"
    private.write_bytes(generate_rsa_private_pem())
    public = tmp_path / "jwt_public.pem"
    public.write_text(public_pem_from_private(private.read_bytes()))
    env = {
        "SECRET_KEY": GOOD_SECRET,
        "ALLOWED_HOSTS": "flarize.com,www.flarize.com",
        "JWT_PRIVATE_KEY_PATH": str(private),
        "JWT_PUBLIC_KEY_PATH": str(public),
        "FERNET_KEYS": Fernet.generate_key().decode(),
        "TRUSTED_PROXIES": "172.16.0.0/12",
        "PASSWORD_RESET_URL": "https://flarize.com/studio/reset-password",
        "FRONTEND_BASE_URL": "https://flarize.com/",
    }
    result = _import_prod(env, tmp_path)
    assert result.returncode == 0, result.stderr


def test_dev_keys_are_generated_once_and_consistent(tmp_path):
    first = ensure_dev_jwt_keypair(tmp_path)
    content = first.read_text()
    assert ensure_dev_jwt_keypair(tmp_path).read_text() == content
    private, public = load_jwt_keys(first, None)
    assert private == content and public.startswith("-----BEGIN PUBLIC KEY-----")
    assert oct(first.stat().st_mode & 0o777) == "0o600"
    assert load_jwt_keys(tmp_path / "missing.pem") == (None, None)
    key = ensure_dev_fernet_key(tmp_path)
    assert ensure_dev_fernet_key(tmp_path) == key
    Fernet(key.encode())
    assert [path.name for path in tmp_path.iterdir() if path.name.startswith(".tmp-")] == []


def test_test_settings_use_isolated_backends():
    assert settings.CACHES["default"]["BACKEND"] == "django.core.cache.backends.locmem.LocMemCache"
    assert settings.CELERY_TASK_ALWAYS_EAGER is True
    assert settings.SIMPLE_JWT["ALGORITHM"] == "RS256"
    assert settings.SIMPLE_JWT["SIGNING_KEY"] and settings.SIMPLE_JWT["VERIFYING_KEY"]
    assert settings.DATABASES["default"]["CONN_MAX_AGE"] == 0
    assert settings.DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] is True
    assert settings.PASSWORD_HASHERS[0] == "django.contrib.auth.hashers.Argon2PasswordHasher"
    assert "django.contrib.auth.hashers.BCryptPasswordHasher" in settings.PASSWORD_HASHERS
    assert settings.FEATURE_FLAG_DEFAULTS == {"ADMS_RECEIVER": False, "AGREEMENTS_PRICE_OVERRIDE": False, "LEGACY_API_SHIM": False, "INVENTORY_STOCK": False}
    assert settings.SIMPLE_JWT["ROTATE_REFRESH_TOKENS"] and settings.SIMPLE_JWT["BLACKLIST_AFTER_ROTATION"]
    assert settings.SIMPLE_JWT["USER_ID_FIELD"] == "uid" and settings.SIMPLE_JWT["USER_ID_CLAIM"] == "sub"
    assert settings.TIME_ZONE == "Asia/Kolkata" and settings.USE_TZ
    assert settings.AUTH_USER_MODEL == "accounts.User"
