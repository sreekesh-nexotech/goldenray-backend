# F1 — Foundation: infrastructure, settings, core platform, accounts models + registry, app skeletons

Work package F1 builds the part of PLAN §4.3 (Phase 1) every other package stands on. It follows PLAN §1, §2.1,
§2.10, §3.1, §3.2, §5.3 and the standard (§3, §5, §6.1, §7). Deviations are in `docs/DEVIATIONS.md` (DV-7).

## What exists after F1

| Area | Where | Notes |
|---|---|---|
| Dependencies | `requirements/base.txt`, `requirements/dev.txt` | exact pins; every package verified on Django 6.0.8 (`manage.py check`, full suite). simplejwt 5.5.1 has no Django 6.0 classifier but no upper bound and passes; no alternative was needed. `redis==5.3.1` is the newest the `celery[redis]` 5.4 extra accepts. |
| Settings | `flarize/settings/{base,dev,test,staging,prod}.py` | decouple `config()` only. `prod.py` calls `flarize.production.validate_production_settings` and refuses to start on a placeholder/short `SECRET_KEY`, `DEBUG`, empty `ALLOWED_HOSTS`, missing RS256 keys, missing/invalid `FERNET_KEYS`, empty/invalid `TRUSTED_PROXIES`. `staging.py` = prod + public API docs. |
| Keys | `flarize/keys.py` | dev/test generate one RSA-2048 private key in `var/keys/` (public key derived), created atomically (first writer wins) so parallel test processes never mismatch. Dev also generates a Fernet key there; tests use an in-memory key per process. |
| Error envelope | `flarize/exceptions.py` | `{code, message, errors, error_codes}` for DRF, Django and `core.errors.DomainError`; 500s are logged with the request id and written to `core_system_exception` by a never-raising writer; JSON `handler404/500/400/403` for non-DRF paths. |
| Versioning | `flarize/versioning.py`, `flarize/urls.py` | the four surfaces are built from `settings.API_VERSIONS`; every app's four lists are collected dynamically (no static import of app URL modules). |
| Cache | `flarize/cache_utils.py` | version-keyed namespaces; `bump()` increments now **and** after commit (closes the "reader caches pre-commit data under the new version" race); public GET cache with content ETag, `304`, `Cache-Control: public, max-age`. |
| Crypto | `flarize/crypto.py` | MultiFernet, fail-closed, `EncryptedTextField` / `EncryptedJSONField` (lookups other than `isnull` raise). |
| Client IP / throttles | `flarize/client_ip.py`, `flarize/throttles.py` | right-to-left XFF walk honouring only `TRUSTED_PROXIES`; scoped throttle with multi-unit rates (`5/10min`), per-request scope/ident hooks. |
| Core | `core/` | `BaseModel`, `SequenceCounter` (CompositePrimaryKey), `ServiceCredential`, `FeatureFlag`, `OutboxEvent`, `LegacyMap`, `SystemException`; outbox, sequences, flags, idempotency, service credentials, scopes, `HasModulePermission`, base views, `/healthz`, `settings/flags/`, `dashboard/`. |
| Accounts | `accounts/` | `User`, `Role`, `LoginAttempt`, `UserSession`, `PasswordReset`; the closed registry; `SessionAwareJWTAuthentication`; `services/authz.py`; `EmailBackend`. |
| Skeletons | every other app | package layout, AppConfig, four URL lists (+ `iclock_urlpatterns`, `legacy_urlpatterns`), `events.py`, `tasks.py`. `engines/` is a plain package. |
| Guard rails | `core/checks.py`, `.importlinter`, `core/tests/test_url_versioning.py`, `core/tests/test_app_layout.py`, `core/tests/test_schema.py` | see below. |

## Decisions not spelled out in the PLAN

1. **System checks enforce the API contract at startup** (`manage.py check`, so CI and every test run):
   `core.E001` HasModulePermission without module/action map · `core.E002` mapping outside the registry ·
   `core.E003` a `BaseViewSet` overriding `get_queryset()` · `core.E004` a DRF view outside the versioned surfaces ·
   `core.E005` a routed generic view still using DRF's default `perform_create/update/destroy` (ORM write or hard
   delete in a view). Use the `core.views` mixins (`ListModelMixin`, `CreateModelMixin`, … ) — they delegate writes to
   the view's `services = {"create": fn, "update": fn, "destroy": fn}` map.
2. **DRF views without a version kwarg answer 404.** That is DRF's `URLPathVersioning` with `ALLOWED_VERSIONS` and it
   is kept as a safety net; the Swagger UI (`/api/docs/`) and legacy adapters opt out with `versioning_class = None`.
3. **Default scope is the narrowest allowed one.** A role granted `customers.view` without a scope gets `owned`
   (`attendance` → `self`, `site_inspections` → `owned`). Seeds and importers pass `"all"` explicitly where PLAN §3.2
   / §7.2 say so. `core.scopes.apply` fails closed for anything unresolved; `all` is the identity filter.
4. **Grants are resolved from the role on every request** (memoised per user object). Service principals
   (`ServicePrincipal`, agents) never have staff grants. Django's `has_perm` API is disabled by `EmailBackend`.
5. **E-mail uniqueness is partial (live rows only)** via a `citext` column (`core.models.CIEmailField`; the extension is
   created in `accounts.0001`). Django's `auth.W004` is silenced for that reason, with the custom backend resolving
   logins through the live-rows manager.
6. **`User.role` is required** (PROTECT); `UserManager.create_user(email, password, role=...)`; there is no
   `create_superuser` and no `is_superuser` column. `last_login` is replaced by `last_login_at`.
7. **Role grants are normalised on every write path**: `Role.save()` and `Role.versioned_update()` normalise
   leniently; services should call `normalise_permissions(..., strict=True)` to report unknown entries.
8. **Optimistic locking is a compare-and-swap** (`BaseModel.versioned_update`, `core.services.save_versioned`): the
   UPDATE carries `WHERE version = <read version>`, so concurrent writers cannot both succeed even without
   `select_for_update`. `soft_delete`/`restore` use it too.
9. **`updated_at` is set in `BaseModel.save()`** (not `auto_now`) and `created_at` defaults to now (not
   `auto_now_add`), so importers can preserve source timestamps via `bulk_create`.
10. **Outbox** *(claiming superseded by F-FIX: a lease, completion after dispatch, retry backoff, `--requeue-parked`;
    see `docs/decisions/f-fix.md`)*: rows are claimed with `SKIP LOCKED` and marked processed before dispatch (standard §7.2); each handler
    runs in its own transaction; succeeded handlers are recorded in `delivered` and not re-run; after 5 attempts the
    row is parked and a `SystemException` is written. `OUTBOX_STRICT` (test settings) makes invalid payloads raise
    instead of being dropped fail-soft. Handlers receive an immutable `core.outbox.Event`.
11. **Sequence formats**: `QUO` → `GR-<n>` as PLAN §2.1 states (Flarize printed `GR-YYYYMMDD-NNNN` over the same global
    counter; the quotations package may pass `fmt=` if the business keeps the date segment). `AGR` →
    `AGR-<FY>-<nnnn>` with FY `2026-27` (April–March, period key = FY), `SV` → `SV-YYYYMMDD-NNNN` (period = day),
    `LEAD` → `L-<n>`, `PROJ` → `PROJ-<n>`. Other kinds pass `fmt`. Numbers must be taken inside the caller's
    transaction (enforced); `ensure_next_value_at_least()` lets importers continue legacy counters (QUO from 9730).
12. **Health**: `/healthz` returns `ok`, `degraded` (non-critical check failed — outbox lag/parked rows, later the
    render queue; HTTP 200 so a deploy is not blocked) or `fail` (database/cache; HTTP 503). Failing checks report
    only the exception class name.
13. **Throttles fail open on a cache outage** (logged): nginx `limit_req` is the backstop and the login lockout is
    DB-backed, so Redis trouble degrades rate limiting instead of taking the API down. Cache reads/writes in
    `cache_utils` and flags are fail-soft for the same reason.
14. **Filtering accepts `?field=` and `?filter[field]=`** (`flarize.filters.FilterBackend`) — PLAN §3.1 names the
    bracket form, §3.4 uses plain parameters.
15. **API docs**: schema per version at `/api/schema/<version>/`; in prod both schema and Swagger UI require a staff
    JWT unless `API_DOCS_PUBLIC=True` (staging default). *(Superseded by F-FIX: a browser cannot send the JWT, so prod
    gates both by `API_DOCS_ALLOWED_NETWORKS`, the same networks as nginx's allow-list.)* `core/tests/test_schema.py` fails on any drf-spectacular
    warning, so every later endpoint must be fully described.
16. **Request context**: `core.middleware.RequestIdMiddleware` accepts a UUID `X-Request-ID` from nginx or mints one,
    echoes it, and writes one JSON access-log line (method, path, status, duration, user uid).
17. **`settings/flags/`** lists the four known flags with their effective state; `PATCH` takes
    `{key, enabled, note?, expected_version?}`; changes emit `core.flag_changed` on the outbox.

## Hand-over notes

* Test fixtures: `api_client`, `make_user(grants={"catalog": ["view"]} | {"catalog": "*"}, scopes=...)`,
  `auth_client(user)` (real RS256 access token), `drain_outbox`. Use `DB_NAME=flarize_<wp>`.
* Register record-scope filters with `core.scopes.register(module, scope)`, dashboard counters with
  `core.dashboard.register(module)`, health checks with `core.health.register(name, critical=False)`, outbox handlers
  with `@core.outbox.handler("<context>.<event>")` in `<app>/events.py`.
* Audit: `audit.services.record(...)` does not exist yet. `core.services.flags.set_flag` (which also emits
  `core.flag_changed`) and `core.service_credentials.issue/rotate/revoke` write no audit row until the audit package
  lands; that package should add the `record(...)` calls there.
* The accounts package extends `SessionAwareJWTAuthentication` (session revocation), `accounts.services.authz`
  (cross-request cache) and `conftest.auth_client` if sessions become mandatory.
