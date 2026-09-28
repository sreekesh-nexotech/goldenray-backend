# Backend Engineering Standard — derived from NexoCRM Django API

> **Purpose.** This document distills the architecture of the NexoCRM Django backend into a reusable engineering standard for all future backend projects. It is written to be *honest, not flattering*: the "Adopt" rules are patterns worth copying; the "Do NOT carry forward" section lists real defects and inconsistencies found in this very codebase that must not be propagated.
>
> **Scope of analysis.** ~213k Python LOC across 22 Django apps, 41 model modules, 73 migrations. Findings were verified against source (file\:line citations retained where they matter). Where the checked-in docs (`CLAUDE.md`, `README.md`) disagree with the code, **the code wins and the doc is flagged as aspirational.**
>
> **Blunt overall verdict.** The *hard architecture* — tenant isolation, async orchestration, webhook security, caching invalidation — is genuinely strong and above the median Django SaaS. The *consistency* is not: the documented layering is largely fictional, there are two PK strategies, three soft-delete mechanisms, an `is_staff` god-flag, and secrets committed to the repo. Adopt the architecture; do not adopt the sprawl.

---

## 1. Stack Information

| Layer | Technology | Notes |
|---|---|---|
| Language / Framework | **Python 3.12, Django 6.0, DRF 3.16** | |
| HTTP server | **Gunicorn** (`gthread`, WSGI), 4 workers × 4 threads | Absolute cap 5 workers; never scaled by CPU formula |
| WebSocket server | **Uvicorn** (ASGI) on a *separate* process, port 8001 | Serves `/ws/*` only; HTTP never routes through it |
| Real-time | **Django Channels + channels-redis** | Redis DB 1 as channel layer |
| Database | **PostgreSQL 16**, single shared instance | Hard ceiling `max_connections ≤ 150` |
| Connection pooling | **PgBouncer**, *transaction* pooling mode | Forces `CONN_MAX_AGE = 0` (see §7) |
| DB driver | `psycopg[binary,pool]` | |
| Cache / broker | **Redis 7** | DB 0 = Celery, DB 1 = cache + channels; `maxmemory 512mb`, `allkeys-lru` |
| Task queue | **Celery 5.4** + `django-celery-beat` (DB scheduler) | concurrency 1–2, dedicated queues |
| Auth | **SimpleJWT, RS256** (PEM keypair in `certs/`) | |
| Containerization | **Docker** multi-stage (builder + slim runtime), `docker-compose` | non-root `appuser`, wheels copied from builder |
| Reverse proxy | **Nginx** | |
| Observability | **Sentry, Prometheus, Grafana**, (Flower present but disabled) | |
| API docs | **drf-spectacular** — Swagger `/api/docs/`, Redoc `/api/redoc/` | |
| Testing | **pytest + pytest-django** | no factory layer (see §5) |
| Lint/format | **black + isort + flake8**, line length **200** (not 88), via pre-commit | |

**Deployment topology (containers):** `backend` (Gunicorn), `websocket` (Uvicorn), `pgbouncer`, `db`, `redis`, `nginx`, `celery-worker`, `celery-beat`, `prometheus`, `grafana`.

**Standard going forward**
- Split WSGI (HTTP) and ASGI (WebSocket) into separate processes. Do not serve both from one ASGI worker "for simplicity."
- Treat the **DB connection count as the primary scaling constraint**, not CPU or memory. Pool at PgBouncer, keep Django connections non-persistent.
- Multi-stage Docker builds, slim base, drop root, `--no-cache-dir`, compile deps only in the builder stage.

---

## 2. External APIs, Libraries & Frameworks (with purpose)

### 2.1 Third-party integrations

| Integration | Purpose | Client location | Sync/Async | Webhook auth |
|---|---|---|---|---|
| **Razorpay** (`razorpay==2.0.0`) | Recurring per-seat subscriptions, mandates, payment links | `billing/services/razorpay_client.py` | mandate init **sync**; webhook processing **Celery** (`webhooks` queue) | **HMAC-SHA256, constant-time**, unique `event_id` idempotency ✅ |
| **Gmail / Google Calendar** | Outbound send, calendar sync, **sent-mail-only** sync | `emails/services/google_oauth.py` (raw `requests`) | send **sync**; sync via Celery + Pub/Sub | `?token=` shared secret, **non-constant-time `!=`** ⚠️ |
| **WhatsApp Cloud API** (Meta) | Messaging, templates, team inbox, chatbot | `whatsapp/graph_client.py` | send **sync**; inbound **Celery** | `X-Hub-Signature-256` HMAC, constant-time ✅ |
| **Meta Ads / Graph API** | Lead-ads ingestion → CRM leads | `meta_ads/graph_client.py` (retry+jitter+circuit breaker) | all Graph calls **Celery** | `X-Hub-Signature-256` + **per-page cross-tenant guard** ✅ |
| **Firebase (FCM)** (`firebase-admin`) | Push notifications, call banners, badges | `crm/services/firebase.py` (singleton) | **Celery** | n/a |
| **AI providers** | Call transcription + summary, NL assistant | see §2.2 | transcription/summary **Celery**; chat **sync** | n/a |
| **Bunny CDN** | File/recording storage | `crm/services/bunny.py` | mixed | n/a |
| **Bunny Stream** | LMS video hosting (TUS upload, token CDN) | `lms/services/bunny_stream.py` | Celery | `?token=` + `constant_time_compare` + library-id check ✅ |
| **Resend** | Provider transactional email (SMTP 465/SSL) | Django SMTP backend | sync | n/a |
| **Exotel** | Telephony (click-to-call, call logs, recordings) | `crm/services/exotel.py` | call **sync**; recording→transcription Celery | `?key=` plaintext token, plain `==` ⚠️ (weakest) |
| **Sentry / Prometheus / Grafana** | Errors, metrics, dashboards | settings + compose | — | — |

### 2.2 AI provider abstraction (Dont use this unless specifically asked)
A single `AI_PROVIDER` switch (`gemini | ollama | groq`) routes chat and summarization:
- **Gemini** (`google-genai`, `gemini-2.5-flash`) — default; function-calling + summaries.
- **Groq** — via the **OpenAI SDK pointed at `api.groq.com/openai/v1`**.
- **Ollama** — self-hosted fallback.
- **OpenAI Whisper** & **Sarvam** (`saarika:v2.5`) — speech-to-text; Sarvam does dual-channel speaker-labeled transcription for Exotel stereo recordings (`pydub` splits channels).
- **Usage metering:** every AI call funnels through a best-effort chokepoint (`billing/services/ai_usage_service.py`) that writes an append-only `AIUsageRecord` + an `F()`-upserted monthly counter, and **never raises** (a metering failure cannot break the AI pipeline). Cost derives from a config rate table.

### 2.3 Notable libraries and their role
`drf-spectacular` (OpenAPI) · `django-filter` (queryset filtering) · `djangorestframework-simplejwt` (RS256 JWT) · `cryptography` (Fernet secret encryption) · `python-decouple` (all env config via `config()`) · `argon2-cffi` (password hashing) · `django-ratelimit` (rate limiting) · `django-cors-headers` · `django-mptt` (tree structures — project task hierarchy) · `weasyprint` (HTML→PDF invoices/quotations) · `pydub` (audio channel-split) · `pyotp` (TOTP 2FA) · `user-agents` (session UA parsing) · `channels`/`channels-redis` (WebSockets) · `django-prometheus` · `sentry-sdk` · `flower` (present, not deployed).

**Standard going forward**
- **One thin client wrapper per external service** in `services/`; never scatter `requests` calls through views.
- **All secrets through `config()`**; provider-wide keys as plaintext env, **per-tenant secrets Fernet-encrypted at rest** in the DB row.
- **Every inbound webhook must verify a cryptographic signature over the raw body with `hmac.compare_digest`.** The Razorpay/WhatsApp/Meta handlers are the reference; the Exotel/Gmail `?token=` approach is the anti-pattern to avoid.
- **Outbound provider calls belong in Celery** unless the user is genuinely blocked on the result. (This codebase does inbound well but leaves email/WhatsApp/mandate-init send synchronous — see §8.)
- Side-effects (metering, notifications, analytics) must be **fail-soft** — wrapped so they can never break the primary write.

---

## 3. Authentication & Authorization Design

### 3.1 Authentication
- **Custom `User`** (`AbstractUser`), `USERNAME_FIELD = "email"`. Roles are **not** a field on `User` — they are relational (`access_control.UserRole`).
- **JWT: RS256** asymmetric (private key signs, public key verifies). Access **30 min**, refresh **7 days**. Token carries **only `user_id` + `organization_id`** — **no roles/permissions/plan in the token**; all authorization is resolved server-side per request against DB+cache. This is correct: it keeps tokens small and makes permission revocation take effect immediately.
- **Password hashing: Argon2** (primary), PBKDF2/BCrypt fallbacks for migration.
- **2FA (TOTP, `pyotp`):** secrets **Fernet-encrypted**; replay protection via monotonic counter + `select_for_update`; single-use backup codes; login challenge tokens are `django.core.signing` tokens that are **structurally unusable as bearer JWTs**. Superusers do **not** bypass 2FA.
- **Session management:** every login creates a tracked `UserSession` (records access + refresh JTIs); `SessionInactivityMiddleware` enforces inactivity timeout, force-signout absolute cap, IP allow/deny, and `max_sessions_per_user`. Refresh endpoint **re-polices the session** even though it is inactivity-exempt (closes the "refresh mints an untracked token" bypass).
- **Brute-force defense:** DB-counted `LoginAttempt` lockout + per-IP `django-ratelimit`; login always runs the auth to normalize timing even when locked.
- **Client IP resolution is centralized** (`accounts/utils/client_ip.py`) with an explicit `TRUSTED_PROXY_IPS` allowlist — XFF from untrusted peers is never trusted.

### 3.2 Authorization — the RBAC engine (`access_control/`)
Four-layer, defense-in-depth model, checked in this order on a write:

1. **Tenant middleware gate** — resolves org from JWT, blocks inactive/deleted orgs, enforces the Razorpay mandate write-gate.
2. **Module-level RBAC** — `MapBasedPermission` reads `view.permission_map[action]`, splits `<action>_<module>`, checks `ModulePermission` CRUD flags for the user's roles. **Default-deny if the action is unmapped.**
3. **Plan-feature gate** — optional `required_feature`/`feature_map`, checked *after* RBAC so plan details aren't leaked to unauthorized roles.
4. **Record-level scoping** — `apply_record_permissions` narrows the queryset by precedence **`all > hierarchy > team > owned > assignee > filtered`**. Hierarchy = recursive CTE over `user_hierarchies`; team = `TeamMember`; plus `SharedRecord` and `UserDataFilter` overlays. **Fails closed** (`.none()`) when nothing matches.
5. **Postgres Row-Level Security** — the DB backstop (see §6.2). Even a viewset that forgets step 4 cannot cross a *tenant* boundary.

Permission results are cached with version-keyed invalidation on `user:{id}` / `org:{id}`.

**Audit logging:** `AuditMiddleware` logs all mutations, masks sensitive fields, captures before-state, and writes via Celery (sync fallback) deferred to `transaction.on_commit`. `audit_logs` is **append-only enforced at the DB privilege level** (UPDATE/DELETE/TRUNCATE revoked from the web role — not a trigger, so the owner role's retention job still works).

**Standard going forward**
- **Keep authorization data out of the JWT.** Put only identity + tenant in the token; resolve permissions server-side so revocation is instant.
- **One central record-permission resolver** used by every viewset. Never re-implement visibility per feature.
- **Default-deny** on unmapped actions.
- **Append-only ledgers via REVOKE, not application code.**
- Enforce 2FA challenge tokens as signed, purpose-namespaced, single-use — never reuse the bearer-token mechanism for intermediate auth states.

### 3.3 Auth/authz defects to fix (do NOT copy but not this done as a mistakes that should not be repeated)
- ⚠️ **`is_staff` is a full authorization bypass** at every layer, and *every tenant admin is `is_staff`*. The entire precedence/record engine is inert for them. A provider-only endpoint that carries just a `permission_map` is wide open to every tenant admin — the reason `IsProviderModule` had to exist. **A binary god-flag is a design smell; scope authority explicitly.**
- ⚠️ **`filtered` RecordPermission / UserDataFilter feed admin-editable JSON straight into `Q(**conds)`** — ORM-lookup injection / info-disclosure surface. Validate against a per-module field allowlist.
- ⚠️ **The team overlay silently widens `owned`** — a role granted only `owned` actually sees all teammates' records. This contradicts the documented precedence.
- ⚠️ **Refresh tokens are not rotated** (`ROTATE_REFRESH_TOKENS` unset) — a stolen refresh token is replayable for its full 7-day life.
- ⚠️ **Org password policy is only enforced on password reset**, not at registration/activation/`set_password` (the custom validator only receives the policy from one caller).
- ⚠️ **Two middlewares (`audit`, `SessionInactivity`) read raw XFF** without the trusted-proxy check — the IP allow/deny control is spoofable. Route everything through `get_client_ip`.
- ⚠️ **`TOKEN_OBTAIN_SERIALIZER` points at a non-existent module path** (`nexocrm.accounts...`); works only because `LoginView` sets `serializer_class` directly. Rotted config.

---

## 4. Project Structure

### 4.1 Actual layout
- **Domain-per-app monolith.** 22 apps, each owning one domain (`crm`, `billing`, `access_control`, `organizations`, `accounts`, `teams`, `projects`, `emails`, `whatsapp`, `meta_ads`, `lms`, `quotations`, `report`, `automation`, `milestones`, `chatbot`, `broadcast`, `public_webhooks`, `settings`, `management`, `support`, `exceptions`).
- **URL convention is clean and uniform:** every app mounts at `/api/v1/<app>/`; public webhooks at `/webhooks/*`. Routers + viewsets for CRUD, hand-written `APIView`s for dashboards/actions.
- **Project package `nexocrm/`** holds cross-cutting infra: `settings.py`, `urls.py`, `celery.py`, `asgi.py`/`wsgi.py`, `cache_utils.py`, `exceptions.py`, `pagination.py`, `crypto.py`, serializer mixins.

### 4.2 The layering rule vs. reality — **honest flag**
`CLAUDE.md` declares a **mandatory** `models/ · selectors/ · services/ · apis/` separation. In practice:
- **`apis/` does not exist anywhere** — views live in `views/` or `views.py`, serializers in `serializers/` or `serializers.py`.
- **`selectors/` exists in exactly one app (`lms`).** Everywhere else, read/query logic lives in `services/` or inline in the viewset.
- **Package-dir vs. flat-file varies app to app** with no enforced convention (`crm` uses `models/ services/ views/`; `automation`, `milestones`, `quotations` are flat single files).
- Views are **not thin** — `crm/views/lead.py` is ~1,400 lines of orchestration.

**Standard going forward** — pick one and *enforce it in CI*, don't just document it:
- `models/` — schema only, no business logic.
- `services/` — **all writes and non-trivial reads** (this codebase de facto folds selectors into services; that's a fine choice — then say so, and drop the fictional `selectors/`/`apis/` tiers).
- `views/` + `serializers/` — thin; validation and HTTP shape only. A hard line-count budget on view files keeps them honest.
- One consistent package-vs-file rule for all apps.

---

## 5. Best Practices (adopt as-is)

**ORM / query discipline**
- `select_related` / `prefetch_related` mandatory; `.only()`/`.defer()` to trim columns; `.iterator()` for large scans; **no N+1, no full-table scans in hot paths**.
- **Global pagination** (`StandardResultsSetPagination`, page 100, max 200) — no endpoint returns an unbounded queryset.
- **Bulk operations** (`bulk_create`/`bulk_update`/batch delete).

**Error handling**
- Single `custom_exception_handler` produces a consistent envelope `{code, message, errors, error_codes}` with **machine-readable error codes** harvested from DRF `ErrorDetail`.
- **Two error sinks:** a DB `SystemException` table (never raises) + Sentry, with `before_send` dropping noisy/expected exceptions (auth, validation, 404, throttle) so signal isn't buried.
- Custom `RatelimitResponseMiddleware` translates rate-limit hits to a real **429**, not a 500.

**Config & security hygiene**
- All config via `python-decouple`; feature flags default **off** and fail-safe (`EMAIL_SYNC_ENABLED`, `STORAGE_CAP_ENFORCED`, `RAZORPAY_MANUAL_ACTIVATE_ENABLED`, `INBOX_ACCESS_ENFORCED`).
- Dangerous routes (org-marks-itself-paid) are **not even registered** unless explicitly enabled.
- Argon2, RS256, HSTS/SSL-redirect/secure-cookies in prod, request-size caps, CORS allowlist.

**Testing**
- Root `conftest.py` resets tenant thread-local between tests, seeds global lookups once per session, and pins weekday so business-hours logic doesn't make the suite red on weekends.

**Best-practice gaps to fix**
- ⚠️ **No factory layer** (`factory_boy`/`model_bakery`) — fixtures are hand-rolled ORM `.create()`; `faker` is barely used. Standardize a factory layer.
- ⚠️ **`send_default_pii=True`** ships customer PII to Sentry. Turn off or scrub.
- ⚠️ **Committed secrets:** a Firebase service-account private-key JSON and `.env.backup` are in the repo. Rotate and purge; enforce secret scanning in CI.
- ⚠️ **`emails/encryption.py` fails *open* to a hardcoded public dev key** when `FERNET_ENCRYPTION_KEY` is unset — the rest of the codebase fails closed. **Unify on fail-closed encryption.**

---

## 6. Schema Design Technique

### 6.1 Model conventions
- **Dual-key pattern (dominant):** Django's integer `BigAutoField id` stays the physical PK; a separate **unique, indexed UUID business key** (`user_id`, `organization_id`, `lead_id`, …) is what every FK targets via `to_field="<x>_id"`. Rationale: narrow int PKs/joins + stable non-enumerable external identifier. `DEFAULT_AUTO_FIELD = BigAutoField` is set globally so a new app can't silently regress to int4.
- **Status-type architecture (strong, standardize this):** a **global fixed type** table (no org FK, `code` unique) + a **per-org customizable status** table FK'd to it by `code`. KPI semantics (`is_won`/`is_lost`) derive from the type, replacing brittle string matching.
- **Custom fields = hybrid, not EAV (strong):** a per-org `CustomFieldDefinition` metadata table + a `JSONField` blob for values + a fixed pool of **typed, indexed "slot" columns** (`c_date_1/2`, `c_num_1/2`) for the sortable/filterable subset, because JSON can't be efficiently sorted. GIN index (`jsonb_path_ops`) on every `custom_fields` column for containment filters.
- **Deliberate `on_delete` per relationship:** `CASCADE` for the tenant FK, `PROTECT`/`RESTRICT` for catastrophic-loss lookups (plans, live status), `SET_NULL` for audit/attribution. This intent is worth mandating.
- **Partial `UniqueConstraint` as the house idiom:** "one default status per org," "one won status per org," "at most one provider org," idempotent-conversion guards — all expressed as DB constraints with `condition=`. Excellent.
- **Query-shaped composite indexes**, named and commented to the query they serve (Kanban lane, list page, timeline). Partial indexes matching soft-delete read filters (index only live rows).

### 6.2 Multi-tenancy at the schema layer — **the crown jewel**
Two mandatory layers:
1. **Application layer** — `TenantAwareManager` auto-filters `objects` by a thread-local org set from the JWT; `all_objects` is the escape hatch.
2. **Postgres Row-Level Security** — the DB enforces isolation even if the app layer is bypassed:
   - A dedicated **non-superuser `nexocrm_app` role (`NOBYPASSRLS`)** serves web/ASGI requests; the owner role (migrations/Celery/commands) bypasses RLS for cross-org maintenance.
   - Policy: `organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid`, `USING` + `WITH CHECK`, **`ENABLE` not `FORCE`**, **fail-closed** (unset GUC → NULL → zero rows, zero inserts).
   - GUC set per request via `set_config(..., is_local=true)` (SET LOCAL), and the **entire request is wrapped in a transaction by the middleware** so the setting survives under PgBouncer transaction pooling and is discarded at transaction end (never leaks across pooled connections).
   - **Coverage derived from the model registry, not `information_schema`** (`organizations/rls_registry.py`) — so tables created by later migrations can't silently ship with no policy. Exclusion lists are documented with reasons (bootstrap/identity tables, GUC-less webhook lookups). An **asymmetric shared-content policy** lets tenants read provider-global (NULL-org) rows but never forge them.
   - **RLS knowledge lives in importable app code with a guarding test**, not inside migrations (migrations get squashed/renumbered/deleted).
- A `provider_org_context` context manager is the **one sanctioned way to cross a tenant boundary**, moving both the thread-local and the GUC together and restoring in `finally`.
- A `repair_cross_tenant_refs` command detects/repairs cross-tenant FK leaks via the `pg_constraint` catalog — proving that RLS filters *reads* but does not by itself stop a serializer from accepting another tenant's FK value.

**Standard going forward**
- **Two-layer tenant isolation is the standard for any multi-tenant DB:** app-layer manager filtering *and* Postgres RLS with a non-bypass role. Neither alone is sufficient.
- Derive RLS coverage from the model registry; keep policy definitions in app code with a coverage test.
- Every "one-per-tenant" or idempotency invariant belongs in a **partial DB constraint**, not application code.
- Global fixed-lookup + per-org-customizable split for any status/type taxonomy.

### 6.3 Schema defects to fix (do NOT copy)
- ⚠️ **Two contradictory PK strategies** — most apps use int-PK + UUID-business-key; the `management` app uses true UUID PKs. Pick one.
- ⚠️ **Two unrelated abstract bases** (`TenantAwareModel` vs `CRMBaseModel`) that duplicate the org FK and managers; `CRMBaseModel` doesn't even extend `TenantAwareModel`.
- ⚠️ **`TenantAwareManager` applied unevenly** — `Customer` and several billing/org models are plain `models.Model` with no tenant-filtering manager, leaning entirely on RLS. A missing GUC on a non-web path would leak.
- ⚠️ **Three overlapping soft-delete mechanisms** (`deleted_at`, `is_archived`/`is_archive`, a `DeletedRecord` tombstone) with naming drift and no shared base. There is no single `is_deleted` convention.
- ⚠️ **Inconsistent uniqueness scoping** — `uniq_lead_name_per_org` is unconditional (a soft-deleted lead reserves its name forever) while the sibling `uniq_customer_name_per_org` is scoped to live rows.
- ⚠️ **Org FK field-name split** (`organization` vs `org_id` + `db_column="organization_id"`) across six models.
- ⚠️ **Deprecated shadow billing columns** still on `Organization` (a dual source of truth that "never agreed").
- ⚠️ **14 legacy Frappe-style table names with spaces** (`tabCRM Call Log`, `tabProject`, …) — a migrated-from-Frappe underbelly that forces identifier-quoting everywhere. New tables must use clean snake_case names.
- ⚠️ **No trigram / full-text index support** — text search is `ILIKE`/`icontains` without `pg_trgm`, a scaling gap.

---

## 7. Critical API, Worker & Caching Design Decisions

### 7.1 Caching — version-keyed invalidation
- Cache keys **embed version numbers**, not just a TTL: `path + query + u:{id}:v{uv} + o:{id}:v{ov} + m:{model}:v{mv}`, SHA-256'd, prefixed `nexocrm:org:{org_id}:` so bulk pattern-delete works. Version keys fetched with a single `get_many`.
- A write calls `increment_cache_version("org:{id}:{model}")`, instantly orphaning every key that embedded the old version — **no key enumeration, no scan, O(1) invalidation.** `BaseCRMViewSet` does this automatically on create/update/destroy. Dashboards embed *multiple* model versions so an aggregate invalidates when any input changes.
- Version keys carry a 7-day TTL to bound Redis growth (expiry → reset to 1, which only invalidates ≥7-day-old caches).
- **This is the single best caching pattern in the codebase — adopt it wholesale.**

**Caching defects to fix (do NOT copy)**
- ⚠️ **Per-model versioning misses nested reads.** A `Lead` detail embedding notes stays cached up to 300s after a `Note` write, because the note bump touches `…:note`, not `…:lead`. Only dashboards use multi-model keys. For nested serializers, version on *every* embedded model.
- ⚠️ **TTLs exceed the documented 30–120s** — list/retrieve and several dashboards cache at 300s.
- ⚠️ **Writes outside a viewset must bump the version manually** — an easy invariant to break. Prefer a central write path.

### 7.2 Workers & async
- **Celery config discipline:** JSON-only, IST timezone (so all beat crontabs are wall-clock IST), 120s hard time limit, **`CELERY_TASK_PUBLISH_RETRY`** so a task enqueued via `transaction.on_commit` isn't silently lost after the DB commits, fast broker socket timeouts.
- **`transaction.on_commit` for every task enqueue** — near-universal and correct. Make it a lint rule.
- **Dedicated queues** (`default`, `webhooks`, `broadcast`) so a slow paced job can't starve inbound webhooks.
- **Transactional outbox pattern (standardize this):** the automation & milestone engines are textbook —
  - a **single writer** records domain events to an outbox table, best-effort and inside a savepoint so a failed emit can never roll back the real write;
  - a `rule_origin` contextvar prevents rule actions from emitting new events (no infinite loops);
  - the drain task **advances the cursor / `next_run_at` *before* dispatching** so a re-tick can't double-fire;
  - **idempotency via a unique constraint** (`rule, version, record_uid, dedup_key`) so re-delivered events are no-ops without row locks;
  - a poison-pill guard stops one bad row from blocking the drain.
- **Every Celery task that touches tenant data re-establishes tenant context** (`set_current_organization` + `set_rls_org`), because the worker runs as the RLS-bypassing owner role. This discipline is essential and worth mandating.
- **WebSockets:** separate Uvicorn process; JWT (RS256) validated from the `?token=` query param; consumers reject anonymous, join per-user groups, and use the socket only for server→client push + a `ping` keepalive — all real operations go through REST. Sound.

**Worker/WS defects to fix (do NOT copy)**
- ⚠️ **The `broadcast` queue has no consumer in the committed `docker-compose.yml`** (worker runs `-Q default,webhooks`). As checked in, scheduled broadcasts enqueue to a queue nobody drains — they never dispatch. Fix the compose or the routing.
- ⚠️ **WS JWT is double-decoded** and the token rides in the query string (leaks into proxy/access logs); no server-side idle timeout. Prefer a subprotocol/header for the token where possible.

### 7.3 API design decisions
- **`BaseCRMViewSet` centralizes the CRM contract:** dual tenant filtering (thread-local manager *and* explicit `organization=` filter — belt and suspenders), automatic record-permission application, auto-stamping of `organization/owner/created_by/modified_by`, cache-version bump on write, and automation-event emission.
- **Dynamic per-org field configuration (strong):** one resolver (`org_config_visible_fields`) drives both the `/schema/` endpoint and the data endpoints, so the columns a client is told about and the columns it receives can never diverge. `view_type` (list/detail/form/kanban/mobile) selects the field set; `mandatory_fields` are always forced in.
- **IETF `QUERY` HTTP method** support so a JSON-body search can share the GET collection URL — a clean, well-commented seam.
- **Rate limiting is deliberately narrow:** DRF `ScopedRateThrottle` is global-default but only bites views that declare a `throttle_scope` (currently one). Everything else relies on two Redis limiters (per-IP/per-key webhook counters, `django-ratelimit` on auth). **Consequence to note:** most authenticated CRM endpoints have *no* request-rate ceiling — add one for a public-facing deployment.

**API defects to fix (do NOT copy)**
- ⚠️ **`hasattr`-based auto-stamping** silently no-ops if a model names a field differently. Prefer explicit declaration.
- ⚠️ **`perform_destroy` hard-deletes** even though soft-delete infrastructure exists elsewhere — inconsistent delete semantics baked into the base class.
- ⚠️ **Object-level record enforcement isn't structurally guaranteed** — `has_object_permission` returns `True` for `list`, trusting each `get_queryset` to scope. A viewset that forgets `apply_record_permissions` leaks records intra-tenant (RLS only protects *cross*-tenant). Make record scoping enforced by the base class, not by convention.

---

## 8. Cross-cutting: the "Adopt / Reject" summary

### Adopt as standard (genuinely strong)
1. **Two-layer tenant isolation** — app manager + Postgres RLS (non-bypass role, fail-closed, registry-derived coverage, coverage test in app code).
2. **Version-keyed O(1) cache invalidation.**
3. **Transactional outbox + idempotency-key + advance-cursor-before-dispatch** for all async fan-out.
4. **`transaction.on_commit` enqueue + publish-retry**; re-establish tenant context inside every task.
5. **Signed-webhook reference pattern** — HMAC over raw body, constant-time, unique-event idempotency, `on_commit` before `.delay`, cross-tenant guard.
6. **Authorization out of the JWT; one central record-permission resolver; default-deny.**
7. **Partial DB constraints** for every per-tenant / idempotency invariant; deliberate `on_delete`.
8. **Global fixed-lookup + per-org-custom** status taxonomy; **hybrid custom fields** (definition + JSON + indexed slots).
9. **Append-only ledgers via privilege REVOKE.**
10. **Consistent error envelope with machine-readable codes; feature flags default-off and fail-safe.**

### Reject / fix (present in this codebase — do not propagate)
1. `is_staff` as a blanket authorization bypass.
2. Admin-editable JSON fed straight into ORM `Q()` (lookup injection).
3. Team overlay silently widening `owned`.
4. No refresh-token rotation; password policy enforced on only one path; spoofable XFF in two middlewares.
5. Committed secrets (Firebase key, `.env.backup`); `send_default_pii=True`; fail-*open* encryption in `emails/`.
6. Two PK strategies; two unrelated base models; uneven tenant manager; three soft-delete mechanisms; legacy `tab…` table names.
7. Per-model cache versioning that misses nested reads; TTLs above the documented budget.
8. `broadcast` queue with no consumer in the committed compose.
9. Weak `?token=` webhook auth (Exotel, Gmail Pub/Sub) vs. the signed pattern used elsewhere.
10. Documented `models/selectors/services/apis` layering that the code does not follow — either enforce it in CI or rewrite the standard to match reality (services-hold-reads).

### How to enforce (not just document)
- **CI secret scanning** + pre-commit hook to block key files and `.env*`.
- **A lint/CI check** that every tenant model is either `TenantAwareModel`-derived or RLS-covered (the coverage test already exists — extend it).
- **A CI grep** that `.delay()` is always inside `transaction.on_commit` in request paths.
- **A view line-count budget** to keep views thin.
- **Delete the aspirational layering language** from `CLAUDE.md` or make `apis/`/`selectors/` real — a standard nobody follows is worse than none.

---

*Analysis basis: direct read of `settings.py`, `docker-compose.yml`, `Dockerfile`, `urls.py`, `cache_utils.py`, `crm/views/base.py`, `organizations/rls_registry.py`, `organizations/tenant_context.py`, plus four targeted deep-dive passes over auth/authz, schema, external integrations, and caching/workers/structure. Where checked-in documentation disagreed with the source, the source was treated as authoritative.*
