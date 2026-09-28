"""Settings shared by every environment.

All configuration is read from the environment through ``decouple.config()``. Environment modules (dev, test,
staging, prod) import this module and override only what differs. ``prod.py`` validates the result and refuses to
start on unsafe values.
"""

from datetime import timedelta
from pathlib import Path

from decouple import Csv, config
from kombu import Queue

from flarize.keys import load_jwt_keys

BASE_DIR = Path(__file__).resolve().parent.parent.parent
VAR_DIR = BASE_DIR / "var"
KEY_DIR = VAR_DIR / "keys"

# --------------------------------------------------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------------------------------------------------
PLACEHOLDER_SECRET_KEY = "django-insecure-flarize-placeholder-never-use-outside-dev-and-test"
SECRET_KEY = config("SECRET_KEY", default=PLACEHOLDER_SECRET_KEY)
DEBUG = config("DEBUG", default=False, cast=bool)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="", cast=Csv())
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", default="", cast=Csv())

DJANGO_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.postgres",
    "django.contrib.staticfiles",
]
THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "django_filters",
    "corsheaders",
    "django_celery_beat",
]
# Order matters only for readability: platform first, then product master, configuration, sales, website, HR.
LOCAL_APPS = [
    "core",
    "accounts",
    "audit",
    "media",
    "documents",
    "company",
    "catalog",
    "pricing",
    "procurement",
    "inventory",
    "bom",
    "packs",
    "engineering",
    "customers",
    "leads",
    "quotations",
    "agreements",
    "site_inspections",
    "projects",
    "blog",
    "sitepages",
    "faqs",
    "careers",
    "seo",
    "reference",
    "calculators",
    "emi",
    "hr",
    "attendance",
    "devices",
    "legacy",
    "migrations_tools",
]
INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "core.middleware.RequestIdMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "flarize.urls"
WSGI_APPLICATION = "flarize.wsgi.application"
# Every versioned path carries its trailing slash explicitly; legacy adapters declare their exact old paths.
APPEND_SLASH = False

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    }
]

# --------------------------------------------------------------------------------------------------------------------
# Database (PgBouncer transaction pooling: no persistent connections, no server-side cursors)
# --------------------------------------------------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("DB_NAME", default="flarize"),
        "USER": config("DB_USER", default="postgres"),
        "PASSWORD": config("DB_PASSWORD", default=""),
        "HOST": config("DB_HOST", default="localhost"),
        "PORT": config("DB_PORT", default="5432"),
        "CONN_MAX_AGE": 0,
        "DISABLE_SERVER_SIDE_CURSORS": True,
        "OPTIONS": {"connect_timeout": 5, "application_name": "flarize"},
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["accounts.backends.EmailBackend"]
# accounts_user.email is unique among *live* users (partial unique constraint, PLAN §2.1); the custom backend above
# resolves logins through the live-rows manager, which is exactly the case auth.W004 warns about.
SILENCED_SYSTEM_CHECKS = ["auth.W004"]
# Argon2 for every new hash. PBKDF2 verifies migrated CMS hashes; bcrypt verifies imported eSSL hashes. Old hashes
# are upgraded to Argon2 on the next successful login.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
    "django.contrib.auth.hashers.BCryptPasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --------------------------------------------------------------------------------------------------------------------
# I18N / time
# --------------------------------------------------------------------------------------------------------------------
LANGUAGE_CODE = "en"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(config("MEDIA_ROOT", default=str(VAR_DIR / "media")))

# --------------------------------------------------------------------------------------------------------------------
# Cache (Redis; version-keyed invalidation lives in flarize.cache_utils)
# --------------------------------------------------------------------------------------------------------------------
REDIS_URL = config("REDIS_URL", default="redis://localhost:6379/1")
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": "flarize",
        "TIMEOUT": 300,
        "OPTIONS": {"socket_connect_timeout": 2, "socket_timeout": 2},
    }
}
CACHE_VERSION_TTL_SECONDS = 7 * 24 * 60 * 60
PUBLIC_CACHE_TTL_SECONDS = 60

# --------------------------------------------------------------------------------------------------------------------
# Client IP / proxies
# --------------------------------------------------------------------------------------------------------------------
# CIDR list of reverse proxies whose X-Forwarded-For we honour (nginx, docker network). Empty = trust nobody.
TRUSTED_PROXIES = config("TRUSTED_PROXIES", default="", cast=Csv())

# --------------------------------------------------------------------------------------------------------------------
# DRF
# --------------------------------------------------------------------------------------------------------------------
API_VERSIONS = ("v1",)
THROTTLE_RATES = {
    "public_read": "600/min",
    "public_write": "20/min",
    "otp": "5/10min",
    "login": "10/15min",
    "staff": "1200/min",
    "agent": "120/min",
    "iclock": "300/min",
    "customer": "60/min",
}
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["accounts.authentication.SessionAwareJWTAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.URLPathVersioning",
    "ALLOWED_VERSIONS": API_VERSIONS,
    "VERSION_PARAM": "version",
    "DEFAULT_VERSION": None,
    "DEFAULT_PAGINATION_CLASS": "flarize.pagination.StandardPagination",
    "PAGE_SIZE": 25,
    "EXCEPTION_HANDLER": "flarize.exceptions.exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_FILTER_BACKENDS": [
        "flarize.filters.FilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_THROTTLE_CLASSES": ["flarize.throttles.ScopedRateThrottle"],
    "DEFAULT_THROTTLE_RATES": THROTTLE_RATES,
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ],
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

# --------------------------------------------------------------------------------------------------------------------
# JWT (RS256; tokens carry identity only — permissions are resolved server-side on every request)
# --------------------------------------------------------------------------------------------------------------------
JWT_PRIVATE_KEY_PATH = config("JWT_PRIVATE_KEY_PATH", default="")
JWT_PUBLIC_KEY_PATH = config("JWT_PUBLIC_KEY_PATH", default="")
JWT_PRIVATE_KEY, JWT_PUBLIC_KEY = load_jwt_keys(JWT_PRIVATE_KEY_PATH, JWT_PUBLIC_KEY_PATH)
SIMPLE_JWT = {
    "ALGORITHM": "RS256",
    "SIGNING_KEY": JWT_PRIVATE_KEY,
    "VERIFYING_KEY": JWT_PUBLIC_KEY,
    "ISSUER": config("JWT_ISSUER", default="flarize"),
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "uid",
    "USER_ID_CLAIM": "sub",
    "TOKEN_TYPE_CLAIM": "token_type",
    "JTI_CLAIM": "jti",
    "LEEWAY": 5,
}

# --------------------------------------------------------------------------------------------------------------------
# Encryption at rest (Fernet; fail-closed — see flarize.crypto)
# --------------------------------------------------------------------------------------------------------------------
FERNET_KEYS = config("FERNET_KEYS", default="", cast=Csv())

# --------------------------------------------------------------------------------------------------------------------
# OpenAPI (drf-spectacular). One schema per API version: /api/schema/<version>/.
# --------------------------------------------------------------------------------------------------------------------
API_DOCS_PUBLIC = config("API_DOCS_PUBLIC", default=True, cast=bool)
SPECTACULAR_SETTINGS = {
    "TITLE": "Flarize Platform API",
    "DESCRIPTION": (
        "Surfaces: /api/<version>/ staff (JWT RS256), /api/public/<version>/ website, /api/agent/<version>/ office agents "
        "(service token), /api/customer/<version>/ customer signed links. Errors use the envelope {code, message, errors, error_codes}."
    ),
    "VERSION": "1",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": r"/api/(?:public/|agent/|customer/)?v[0-9]+",
    "COMPONENT_SPLIT_REQUEST": True,
    "SERVE_PERMISSIONS": ["rest_framework.permissions.AllowAny"],
    "SERVE_AUTHENTICATION": [],
    "SWAGGER_UI_SETTINGS": {"deepLinking": True, "displayOperationId": True},
}

# --------------------------------------------------------------------------------------------------------------------
# Celery (JSON only, IST wall-clock schedules, publish retry for on_commit enqueues)
# --------------------------------------------------------------------------------------------------------------------
CELERY_BROKER_URL = config("CELERY_BROKER_URL", default=REDIS_URL)
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default=CELERY_BROKER_URL)
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TIME_LIMIT = 120
CELERY_TASK_SOFT_TIME_LIMIT = 110
CELERY_TASK_PUBLISH_RETRY = True
CELERY_TASK_PUBLISH_RETRY_POLICY = {"max_retries": 3, "interval_start": 0, "interval_step": 0.5, "interval_max": 2}
CELERY_TASK_IGNORE_RESULT = True
CELERY_RESULT_EXPIRES = 24 * 60 * 60
CELERY_BROKER_CONNECTION_TIMEOUT = 3
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_TRANSPORT_OPTIONS = {"socket_timeout": 5, "socket_connect_timeout": 3, "visibility_timeout": 3600}
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_QUEUES = (Queue("default"), Queue("documents"), Queue("ingest"))
# Every queue must have a consumer: worker-default runs `-Q default,ingest`, worker-documents runs `-Q documents`.
CELERY_TASK_ROUTES = {
    "documents.tasks.*": {"queue": "documents"},
    "devices.tasks.*": {"queue": "ingest"},
    "attendance.tasks.*": {"queue": "ingest"},
}
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_BEAT_SCHEDULE = {
    "core.drain_outbox": {"task": "core.tasks.drain_outbox", "schedule": 5.0},
}

# --------------------------------------------------------------------------------------------------------------------
# Outbox
# --------------------------------------------------------------------------------------------------------------------
OUTBOX_MAX_ATTEMPTS = 5
OUTBOX_BATCH_SIZE = 100
OUTBOX_LAG_ALERT_SECONDS = 300
# When True a non-serialisable payload raises at emit() (tests); otherwise it is logged and dropped (fail-soft).
OUTBOX_STRICT = False

# --------------------------------------------------------------------------------------------------------------------
# Feature flags (defaults; core_feature_flag rows override them)
# --------------------------------------------------------------------------------------------------------------------
FEATURE_FLAG_DEFAULTS = {
    "ADMS_RECEIVER": False,
    "AGREEMENTS_PRICE_OVERRIDE": False,
    "LEGACY_API_SHIM": False,
    "INVENTORY_STOCK": False,
}

# --------------------------------------------------------------------------------------------------------------------
# Idempotency-Key replay store
# --------------------------------------------------------------------------------------------------------------------
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60

# --------------------------------------------------------------------------------------------------------------------
# HTTP hardening shared by every environment
# --------------------------------------------------------------------------------------------------------------------
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
DATA_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 1000

CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", default="", cast=Csv())
CORS_URLS_REGEX = r"^/api/.*$"
CORS_ALLOW_CREDENTIALS = False
CORS_EXPOSE_HEADERS = ["ETag", "Retry-After", "X-Request-ID"]

# --------------------------------------------------------------------------------------------------------------------
# Logging: JSON lines to stdout with the request id on every record
# --------------------------------------------------------------------------------------------------------------------
LOG_LEVEL = config("LOG_LEVEL", default="INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"request_context": {"()": "flarize.logging.RequestContextFilter"}},
    "formatters": {"json": {"()": "flarize.logging.JsonFormatter"}},
    "handlers": {"stdout": {"class": "logging.StreamHandler", "formatter": "json", "filters": ["request_context"]}},
    "root": {"handlers": ["stdout"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["stdout"], "level": LOG_LEVEL, "propagate": False},
        # Request errors are logged once by flarize.exceptions (with the request id); avoid duplicate records.
        "django.request": {"handlers": ["stdout"], "level": "ERROR", "propagate": False},
        "celery": {"handlers": ["stdout"], "level": LOG_LEVEL, "propagate": False},
    },
}
