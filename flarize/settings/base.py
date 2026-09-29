"""Settings shared by every environment.

All configuration is read from the environment through ``decouple.config()``. Environment modules (dev, test,
staging, prod) import this module and override only what differs. ``prod.py`` validates the result and refuses to
start on unsafe values.
"""

from datetime import timedelta
from pathlib import Path

from celery.schedules import crontab
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
    # Opens the audit context (request id, trusted client IP); the authentication classes attach the actor.
    "audit.middleware.AuditMiddleware",
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
# The role the application connects as when it differs from the migration/owner role (prod). The audit migration and
# `ensure_audit_partitions` revoke UPDATE/DELETE/TRUNCATE on audit_log from it (docs/ops/audit-log.md). Empty = one role.
DB_APP_ROLE = config("DB_APP_ROLE", default="")

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
    "accounts.hashers.LegacyBCryptPasswordHasher",  # eSSL bcrypt hashes (72-byte truncation), upgraded on login
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
# Media (media.services.storage). Public files go to Bunny (CDN) in staging/prod; the "local" public backend writes
# under PUBLIC_MEDIA_ROOT and is served by Django only when DEBUG. Private files never leave PRIVATE_MEDIA_ROOT: they
# are served after a signed-URL check, by nginx (X-Accel-Redirect to the internal /media/private/ location) when
# USE_X_ACCEL, else streamed by Django.
# --------------------------------------------------------------------------------------------------------------------
MEDIA_PUBLIC_BACKEND = config("MEDIA_PUBLIC_BACKEND", default="bunny")
PUBLIC_MEDIA_ROOT = Path(config("PUBLIC_MEDIA_ROOT", default=str(MEDIA_ROOT / "public")))
PUBLIC_MEDIA_URL = config("PUBLIC_MEDIA_URL", default="/media/public/")
PRIVATE_MEDIA_ROOT = Path(config("PRIVATE_MEDIA_ROOT", default=str(MEDIA_ROOT / "private")))
USE_X_ACCEL = config("USE_X_ACCEL", default=False, cast=bool)
X_ACCEL_PRIVATE_PREFIX = "/media/private/"
MEDIA_SIGNED_URL_TTL_SECONDS = 600
MEDIA_THUMBNAIL_MAX_PX = 480
# Pillow refuses images above this many pixels (decompression bombs); 50 MP covers every phone camera.
MEDIA_MAX_IMAGE_PIXELS = 50_000_000
# Bunny storage (env fallback; an enabled BUNNY row in company_integration takes precedence).
BUNNY_STORAGE_ZONE = config("BUNNY_STORAGE_ZONE", default="")
BUNNY_STORAGE_ENDPOINT = config("BUNNY_STORAGE_ENDPOINT", default="storage.bunnycdn.com")
BUNNY_STORAGE_ACCESS_KEY = config("BUNNY_STORAGE_ACCESS_KEY", default="")
BUNNY_CDN_BASE_URL = config("BUNNY_CDN_BASE_URL", default="")
BUNNY_TIMEOUT_SECONDS = 30

# --------------------------------------------------------------------------------------------------------------------
# Documents (documents.services): one HTML → PDF pipeline. "playwright" renders with headless Chromium in the
# documents worker; "stub" produces deterministic minimal PDFs (tests, laptops without Chromium).
# --------------------------------------------------------------------------------------------------------------------
DOCUMENTS_RENDERER = config("DOCUMENTS_RENDERER", default="playwright")
# Chromium binary for Playwright; empty = the browser Playwright installed for its own version.
DOCUMENTS_CHROMIUM_EXECUTABLE = config("DOCUMENTS_CHROMIUM_EXECUTABLE", default="")
DOCUMENTS_RENDER_TIMEOUT_SECONDS = 60
DOCUMENTS_DOWNLOAD_TTL_SECONDS = 600
# Hosts a document template may load images/fonts from (https only); everything else is blocked while rendering.
DOCUMENTS_ALLOWED_ASSET_HOSTS = config("DOCUMENTS_ALLOWED_ASSET_HOSTS", default="", cast=Csv())
# /healthz reports "degraded" when the oldest QUEUED render job is older than this.
DOCUMENTS_QUEUE_ALERT_SECONDS = 600
# RUNNING jobs older than this are treated as lost (worker crash) and failed by the sweeper.
DOCUMENTS_STALE_RUNNING_SECONDS = 900

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
# Server-side (Redis) default TTL of public GET payloads; bump() invalidates them on every write.
PUBLIC_CACHE_TTL_SECONDS = 60
# HTTP Cache-Control max-age budget for public GETs (PLAN §3.1): browsers and CDNs cannot be invalidated by bump().
PUBLIC_CACHE_MAX_AGE_SECONDS = 60
# Origin of the public website (Next.js), no trailing slash: absolute URLs in JSON-LD, previews and sitemap entries.
# Same name and default as the legacy CMS setting, so generated documents stay identical across the cutover.
FRONTEND_BASE_URL = config("FRONTEND_BASE_URL", default="http://localhost:3000").rstrip("/")

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
    # otp/send and otp/verify also per client IP: one address cycling through numbers cannot pump SMS (a CGNAT
    # address is shared by many phones, hence a larger budget than the per-phone one).
    "otp_ip": "20/10min",
    "login": "10/15min",
    # auth/refresh/ and auth/logout/ (DV-8): every active Studio user refreshes every 15 minutes, often from one
    # office NAT or the BFF host, so they cannot share the 10/15min login budget.
    "token_refresh": "300/15min",
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
# Accounts: lockout, sessions, password reset (accounts.services)
# --------------------------------------------------------------------------------------------------------------------
# DB-counted lockout (standard §3.1): failed logins per e-mail and per client IP within the window. Offices behind one
# NAT address share the per-IP budget; raise ACCOUNTS_LOGIN_MAX_FAILURES_PER_IP there if needed.
ACCOUNTS_LOGIN_LOCKOUT_WINDOW = timedelta(minutes=config("ACCOUNTS_LOGIN_LOCKOUT_MINUTES", default=15, cast=int))
ACCOUNTS_LOGIN_MAX_FAILURES_PER_EMAIL = config("ACCOUNTS_LOGIN_MAX_FAILURES_PER_EMAIL", default=5, cast=int)
ACCOUNTS_LOGIN_MAX_FAILURES_PER_IP = config("ACCOUNTS_LOGIN_MAX_FAILURES_PER_IP", default=5, cast=int)
ACCOUNTS_LOGIN_ATTEMPT_RETENTION = timedelta(days=90)
# A session (refresh-token family) slides with every refresh but never outlives this absolute cap.
ACCOUNTS_SESSION_MAX_AGE = timedelta(days=config("ACCOUNTS_SESSION_MAX_AGE_DAYS", default=30, cast=int))
# Session liveness and grants are cached briefly; revocation bumps a version key, so it takes effect immediately.
ACCOUNTS_SESSION_LIVENESS_CACHE_SECONDS = 30
ACCOUNTS_GRANTS_CACHE_SECONDS = 300
# A superseded refresh token replayed within this window (a racing tab/BFF) is refused without ending the session;
# after it, the replay is treated as theft and the whole session is revoked.
ACCOUNTS_REFRESH_REUSE_GRACE_SECONDS = 30
ACCOUNTS_PASSWORD_RESET_TTL = timedelta(hours=1)
ACCOUNTS_INVITE_TTL = timedelta(hours=72)
ACCOUNTS_BOOTSTRAP_TTL = timedelta(hours=24)
ACCOUNTS_RESET_REQUESTS_PER_HOUR = 3
# Studio page that completes a reset; the token travels in the URL fragment (never sent to a server or logged).
ACCOUNTS_PASSWORD_RESET_URL = config("PASSWORD_RESET_URL", default="http://localhost:3000/studio/reset-password")

# --------------------------------------------------------------------------------------------------------------------
# E-mail (core.notifications). SMTP in staging/prod, console in dev, in-memory in tests.
# --------------------------------------------------------------------------------------------------------------------
EMAIL_BACKEND = config("EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = config("EMAIL_HOST", default="localhost")
EMAIL_PORT = config("EMAIL_PORT", default=587, cast=int)
EMAIL_HOST_USER = config("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = config("EMAIL_USE_TLS", default=True, cast=bool)
EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL", default="Flarize <no-reply@flarize.com>")

# --------------------------------------------------------------------------------------------------------------------
# Encryption at rest (Fernet; fail-closed — see flarize.crypto)
# --------------------------------------------------------------------------------------------------------------------
FERNET_KEYS = config("FERNET_KEYS", default="", cast=Csv())

# --------------------------------------------------------------------------------------------------------------------
# OpenAPI (drf-spectacular). One schema per API version: /api/schema/<version>/.
# --------------------------------------------------------------------------------------------------------------------
# /api/docs/ and /api/schema/<version>/ (core.permissions.ApiDocsAccess): open when API_DOCS_PUBLIC, otherwise only
# from API_DOCS_ALLOWED_NETWORKS (CIDRs; the office/VPN networks of deploy/nginx/snippets/docs-allow.conf).
API_DOCS_PUBLIC = config("API_DOCS_PUBLIC", default=True, cast=bool)
API_DOCS_ALLOWED_NETWORKS = config("API_DOCS_ALLOWED_NETWORKS", default="", cast=Csv())
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
    "SERVE_PERMISSIONS": ["core.permissions.ApiDocsAccess"],
    "SERVE_AUTHENTICATION": [],
    "SWAGGER_UI_SETTINGS": {"deepLinking": True, "displayOperationId": True},
    # Choice sets that share a field name across apps get explicit, stable component names.
    "ENUM_NAME_OVERRIDES": {
        "MediaKindEnum": "media.models.asset.MediaAsset.Kind",
        "MediaVisibilityEnum": "media.models.asset.MediaAsset.Visibility",
        "RenderJobKindEnum": "documents.models.render_job.RenderJob.Kind",
        "RenderJobStatusEnum": "documents.models.render_job.RenderJob.Status",
        "IntegrationKeyEnum": "company.models.integration.Integration.Key",
        "CatalogComponentStatusEnum": "catalog.models.component.ComponentStatus",
        "CatalogProfileStatusEnum": "catalog.models.profile.ProfileStatus",
        "CatalogUnitEnum": "catalog.models.category.Unit",
        "BlogEntryStatusEnum": "blog.models.entry.Entry.Status",
        "BlogContentBlockKindEnum": "blog.models.entry.ContentBlock.Kind",
        # DRAFT / PUBLISHED / ARCHIVED — shared by sitepages.Page.Status and faqs.Faq.Status.
        "ContentStatusEnum": "sitepages.models.page.Page.Status",
        "JobPositionStatusEnum": "careers.models.position.JobPosition.Status",
        "JobPositionEmploymentTypeEnum": "careers.models.position.JobPosition.EmploymentType",
        "JobApplicationStatusEnum": "careers.models.application.JobApplication.Status",
        "JobApplicationSourceEnum": "careers.models.application.JobApplication.Source",
        "JobApplicationExperienceEnum": "careers.models.application.JobApplication.Experience",
        "JobApplicationSalaryEnum": "careers.models.application.JobApplication.Salary",
        "JobApplicationEventKindEnum": "careers.models.application.JobApplicationEvent.Kind",
        "KsebTariffPhaseEnum": "reference.models.tariff.KsebTariff.Phase",
        "EmployeeIdentityMethodEnum": "hr.models.employee.Employee.IdentityMethod",
        "LeaveRecordStatusEnum": "hr.models.calendar.LeaveRecord.Status",
        "AttendanceRuleScopeEnum": "hr.models.calendar.AttendanceRule.Scope",
        "ShiftHalfDayAfterSourceEnum": "hr.models.shift.Shift.HalfDayAfterSource",
        "CustomerSourceEnum": "customers.models.customer.Customer.Source",
        "CustomerBillCycleEnum": "customers.models.customer.Customer.BillCycle",
        "LeadKindEnum": "leads.models.lead.Lead.Kind",
        "LeadStatusEnum": "leads.models.lead.Lead.Status",
        "LeadOpenStatusEnum": "leads.models.lead.OPEN_STATUS_CHOICES",
        "LeadFormEnum": "leads.models.lead.Lead.Form",
        "AffiliateApplicationStatusEnum": "leads.models.forms.AffiliateApplication.Status",
        "AffiliateProfessionEnum": "leads.models.forms.Profession",
        "KeralaDistrictEnum": "leads.models.choices.KeralaDistrict",
        "WarrantyRequestStatusEnum": "leads.models.forms.WarrantyRequest.Status",
        "WarrantyIssueTypeEnum": "leads.models.forms.IssueType",
        "InstallationStatusEnum": "leads.models.installation.CustomerInstallation.Status",
        "InstallationSystemTypeEnum": "leads.models.installation.CustomerInstallation.SystemType",
        "DeviceHealthStatusEnum": "devices.services.health.DEVICE_STATUSES",
        "DeviceAgentStatusEnum": "devices.services.health.AGENT_STATUSES",
        "DeviceSyncLogStatusEnum": "devices.models.logs.SyncLog.Status",
    },
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
# Beat (DatabaseScheduler): these entries are synced into django_celery_beat on start; one beat replica only.
CELERY_BEAT_SCHEDULE = {
    "core.drain_outbox": {"task": "core.tasks.drain_outbox", "schedule": 5.0},
    "accounts.purge_auth_records": {"task": "accounts.tasks.purge_auth_records", "schedule": crontab(hour=3, minute=17)},
    # Monthly (and on every deploy from deploy/release.sh, as the owner role): next months' audit_log partitions.
    "audit.ensure_partitions": {"task": "audit.tasks.ensure_partitions", "schedule": crontab(day_of_month=1, hour=2, minute=7)},
    "documents.sweep_render_jobs": {"task": "documents.tasks.sweep_render_jobs", "schedule": crontab(minute="*/5")},
    "core.send_ops_report": {"task": "core.tasks.send_ops_report", "schedule": crontab(day_of_week="mon", hour=8, minute=5)},
    # Scheduled blog publications (blog.services.workflow.publish_due_entries).
    "blog.publish_due_entries": {"task": "blog.tasks.publish_due_entries", "schedule": crontab(minute="*")},
    # Daily: next months' devices_adms_request partitions (when the worker owns the table) and the 30-day ADMS evidence purge.
    "devices.purge_adms_evidence": {"task": "devices.tasks.purge_adms_evidence", "schedule": crontab(hour=3, minute=41)},
}
# Recipients of the weekly ops report (PLAN §5.6) and of the healthz cron alerts (deploy/scripts/healthz-check.sh).
OPS_EMAILS = config("OPS_EMAILS", default="", cast=Csv())

# --------------------------------------------------------------------------------------------------------------------
# Outbox
# --------------------------------------------------------------------------------------------------------------------
# A row is parked after this many failed or abandoned claims (DV-7); `drain_outbox --requeue-parked` re-drives it.
OUTBOX_MAX_ATTEMPTS = 5
OUTBOX_BATCH_SIZE = 100
# Claim lease: longer than the Celery hard time limit, so a live drainer never loses a row it is dispatching; a
# drainer that dies leaves a lease that expires after this long, and the row is claimed again.
OUTBOX_CLAIM_LEASE_SECONDS = CELERY_TASK_TIME_LIMIT + 60
# Retry backoff after a failed dispatch: base × 2^(attempt-1), capped (60 s, 2, 4, 8 min → parked after ~15 min).
OUTBOX_RETRY_BASE_SECONDS = 60
OUTBOX_RETRY_MAX_SECONDS = 3600
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
# Leads: website OTP (Twilio Verify) and the verification token POST leads requires
# --------------------------------------------------------------------------------------------------------------------
# "twilio" in staging/prod (prod.py refuses anything else); "fake" in dev/test (no SMS; the code is 000000).
LEADS_OTP_BACKEND = config("LEADS_OTP_BACKEND", default="twilio")
# Env fallback; an enabled TWILIO integration (Studio → Settings → Integrations) takes precedence.
TWILIO_ACCOUNT_SID = config("TWILIO_ACCOUNT_SID", default="")
TWILIO_AUTH_TOKEN = config("TWILIO_AUTH_TOKEN", default="")
TWILIO_VERIFY_SERVICE_SID = config("TWILIO_VERIFY_SERVICE_SID", default="")
TWILIO_TIMEOUT_SECONDS = 10
LEADS_OTP_TTL_SECONDS = 600  # matches Twilio Verify's default code lifetime
LEADS_OTP_MAX_ATTEMPTS = 5  # verification checks per sent code
LEADS_OTP_MAX_SENDS_PER_PHONE_PER_DAY = 10  # database backstop behind the (fail-open) otp throttle
LEADS_VERIFICATION_TOKEN_TTL_SECONDS = 1800

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
        # 5xx only. API errors are logged once by flarize.exceptions (traceback + request id; the response is marked
        # as logged so Django does not repeat it); plain views (/iclock/, /healthz) are logged here, with the request id.
        "django.request": {"handlers": ["stdout"], "level": "ERROR", "propagate": False},
        "celery": {"handlers": ["stdout"], "level": LOG_LEVEL, "propagate": False},
    },
}
