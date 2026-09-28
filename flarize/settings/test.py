"""Test settings.

* Each agent/worktree exports a unique DB_NAME so concurrent runs never share ``test_<DB_NAME>``.
* LocMem cache so concurrent test runs never share Redis keys.
* Celery runs eagerly; the outbox drain is exercised synchronously.
* Throttle rates are raised so only tests that override them hit a limit.
"""

from cryptography.fernet import Fernet
from decouple import config

from flarize.keys import ensure_dev_jwt_keypair, load_jwt_keys
from flarize.settings.base import *  # noqa: F401,F403

DEBUG = False
SECRET_KEY = "test-secret-key-not-for-production-" + "x" * 32
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
DATABASES["default"]["PASSWORD"] = config("DB_PASSWORD", default="postgres")
TRUSTED_PROXIES = ["10.0.0.0/8"]

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "flarize-test", "KEY_PREFIX": "flarize", "TIMEOUT": 300}}

JWT_PRIVATE_KEY_PATH = str(ensure_dev_jwt_keypair(KEY_DIR))
JWT_PRIVATE_KEY, JWT_PUBLIC_KEY = load_jwt_keys(JWT_PRIVATE_KEY_PATH, None)
SIMPLE_JWT["SIGNING_KEY"] = JWT_PRIVATE_KEY
SIMPLE_JWT["VERIFYING_KEY"] = JWT_PUBLIC_KEY

FERNET_KEYS = [Fernet.generate_key().decode()]

REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {scope: "100000/min" for scope in THROTTLE_RATES}

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"

OUTBOX_STRICT = True
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
MEDIA_ROOT = VAR_DIR / "test-media"
# Media/document tests point these at a per-test tmp_path (media/tests/conftest.py, documents/tests/conftest.py).
MEDIA_PUBLIC_BACKEND = "local"
PUBLIC_MEDIA_ROOT = MEDIA_ROOT / "public"
PRIVATE_MEDIA_ROOT = MEDIA_ROOT / "private"
USE_X_ACCEL = False
DOCUMENTS_RENDERER = "stub"

LOGGING["root"]["level"] = "WARNING"
LOGGING["loggers"]["django"]["level"] = "WARNING"
LOGGING["loggers"]["celery"]["level"] = "WARNING"
