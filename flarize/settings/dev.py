"""Local development settings: debug on, generated dev keys under var/keys/, console email."""

from decouple import Csv, config

from flarize.keys import ensure_dev_fernet_key, ensure_dev_jwt_keypair, load_jwt_keys
from flarize.settings.base import *  # noqa: F401,F403

DEBUG = config("DEBUG", default=True, cast=bool)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="localhost,127.0.0.1,[::1]", cast=Csv())
DATABASES["default"]["PASSWORD"] = config("DB_PASSWORD", default="postgres")
TRUSTED_PROXIES = config("TRUSTED_PROXIES", default="127.0.0.1/32,::1/128", cast=Csv())
CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", default="http://localhost:3000", cast=Csv())

if not JWT_PRIVATE_KEY:
    JWT_PRIVATE_KEY_PATH = str(ensure_dev_jwt_keypair(KEY_DIR))
    JWT_PRIVATE_KEY, JWT_PUBLIC_KEY = load_jwt_keys(JWT_PRIVATE_KEY_PATH, None)
    SIMPLE_JWT["SIGNING_KEY"] = JWT_PRIVATE_KEY
    SIMPLE_JWT["VERIFYING_KEY"] = JWT_PUBLIC_KEY

if not FERNET_KEYS:
    FERNET_KEYS = [ensure_dev_fernet_key(KEY_DIR)]

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Public media on the local filesystem (served by Django under PUBLIC_MEDIA_URL while DEBUG) unless a Bunny zone is set.
MEDIA_PUBLIC_BACKEND = config("MEDIA_PUBLIC_BACKEND", default="local")

# Website OTP without SMS: every code is 000000 (leads.services.twilio_verify.FakeVerifyClient) unless overridden.
LEADS_OTP_BACKEND = config("LEADS_OTP_BACKEND", default="fake")
