# Flarize Platform backend — engineering contract

This file is binding for every contributor (human or agent). Source documents:
- `docs/PLAN.md` — the approved architecture, schema, API, implementation, deployment and migration plan. **Stick to it.** Any deviation goes in `docs/DEVIATIONS.md` with a one-line reason.
- `docs/BACKEND_ENGINEERING_STANDARD.md` — the house standard. Excluded on purpose: multi-tenancy/RLS, Grafana/Prometheus/Sentry/any monitoring stack, Channels/WebSockets, AI providers, irrelevant external APIs.
- Legacy analysis (read-only, outside git): `/home/user/platform-reference/*.md` (Flarize engines spec with reference outputs, Flarize workflows/API spec, goldenray backend+Studio audit, Site Inspection V2 spec, eSSL spec, Plan 2).
- Legacy sources (read-only): `/home/user/goldenray` (website repo: `frontend/`, `goldenray-backend/backend/` main Django + `bom`, `goldenray-backend/backend/cms/` CMS), `/home/user/flarize-main/flarize` (Flarize Node utility), `/tmp/claude-0/-home-user/2d0c9eb0-ce20-5c8e-9860-d18f6ad69685/scratchpad/utils/{agreement-goldenray-main,site-inspection-v2,essl-webap-main}`.

## Toolchain
- Python **3.12** venv: `/home/user/.venvs/platform` (use `/home/user/.venvs/platform/bin/python`, `.../pip`, `.../pytest`). Django 6.0, DRF 3.16.
- PostgreSQL 16 on `localhost:5432`, user `postgres`, password `postgres`. Redis 7 on `localhost:6379`.
- Settings module for tests: `flarize.settings.test`. **Every agent/worktree must export a unique `DB_NAME`** (e.g. `DB_NAME=flarize_wp_catalog`) so concurrent test runs never share `test_<DB_NAME>`.
- Run from the repo root: `DB_NAME=flarize_wp_x /home/user/.venvs/platform/bin/pytest <app> -q`.
- Format/lint before every commit: `black . && isort . && flake8` (line length 200), `python manage.py makemigrations --check --dry-run`, `lint-imports`.

## Layout (one rule for every app — package dirs, never flat files)
```
flarize/            project package: settings/{base,dev,test,staging,prod}.py, urls.py, celery.py, wsgi.py, exceptions.py,
                    pagination.py, cache_utils.py, crypto.py, client_ip.py, throttles.py, versioning.py
<app>/              apps.py, models/ (schema + constraints only), services/ (ALL writes and non-trivial reads),
                    serializers/, views/ (thin: HTTP shape only, ≤ 250 lines per file), urls.py, tasks.py (Celery),
                    events.py (outbox handlers), management/commands/, migrations/, tests/ (factories.py + test_*.py)
engines/            PURE PYTHON, imports nothing from Django or any app. Tests in engines/tests/.
legacy/             old-URL adapters, flag-gated by LEGACY_API_SHIM, mounted only under /legacy/
migrations_tools/   import_* and verify_migration management commands
essl-agent/         standalone office-agent package (not a Django app)
deploy/             Dockerfile, docker-compose.yml, nginx/, scripts
```
Apps (all exist as skeletons from the foundation): core, accounts, audit, media, documents, company, catalog, pricing, procurement, inventory, bom, packs, engineering, customers, leads, quotations, agreements, site_inspections, projects, blog, sitepages, faqs, careers, seo, reference, calculators, emi, hr, attendance, devices, legacy, migrations_tools.

Dependency direction (enforced by import-linter in `.importlinter`): core/accounts/audit/media/documents are the platform; product master (catalog, pricing, procurement, inventory) → configuration (bom, packs, engineering) → sales (customers, leads, quotations, agreements, site_inspections, projects). Website content and HR never import sales. Cross-context side effects go through the **outbox** (`core.outbox.emit` + `@core.outbox.handler("event.name")`), never by importing another context's services. Documented reads are allowed (sales reads product master/configuration; calculators read releases).

## Models
- Every table inherits `core.models.BaseModel`: `id` BigAutoField (never exposed), `uid` UUID (the external id), `created_at`, `updated_at`, `created_by`/`updated_by` (SET_NULL), `deleted_at` soft delete (`objects` = live rows, `all_objects` = everything), `version` (optimistic locking). Append-only/log tables marked *(no base)* in PLAN §2 use plain `models.Model` with `id` BigAutoField.
- Table names exactly as PLAN §2 (`db_table = "catalog_component"`). Snake_case only.
- Every lifecycle invariant is a **partial `UniqueConstraint`** (`condition=Q(deleted_at__isnull=True) & ...`). `unique_together` is banned.
- Every FK states `on_delete` with a comment: PROTECT for masters/lookups, SET_NULL for attribution, CASCADE only for true children.
- Enum columns: `models.TextChoices` + a `CheckConstraint`.
- Money `DecimalField(14, 2)`; rates `DecimalField(10, 4)`; percentages stored as fractions.
- JSONB only for validated documents/snapshots/rule params/free metadata — never for anything filtered or joined in a list screen.
- No Django admin registration for business models.

## Services
- All writes live in `<app>/services/*.py`, decorated `@transaction.atomic`, take the acting `user` explicitly, stamp `created_by/updated_by` explicitly (no `hasattr` magic), check `expected_version` via `core.services.check_version`, write an audit row via `audit.services.record(...)`, bump cache namespaces via `flarize.cache_utils.bump(...)`, and emit outbox events via `core.outbox.emit(...)`.
- Errors: raise `core.errors.DomainError(code, message, status=400, errors=None)` or its subclasses (`NotFound`, `Conflict`, `PermissionDenied`, `StaleVersion`). The global handler renders `{code, message, errors, error_codes}`.
- Celery tasks are enqueued only inside `transaction.on_commit`.
- External providers (Twilio Verify, Bunny, SMTP) go through one thin client per provider in the owning app's `services/`, with a `console`/`fake` backend used in dev and tests. Secrets come from env via `decouple.config()` or `company_integration` (Fernet, fail-closed).

## API — strict versioning
- Surfaces (URL major version only, DRF `URLPathVersioning`, `ALLOWED_VERSIONS=("v1",)`):
  - `/api/<version>/…` staff (JWT RS256), `/api/public/<version>/…` website (AllowAny, `authentication_classes=[]`, cached, throttled), `/api/agent/<version>/…` office agents (service token), `/api/customer/<version>/…` customer signed links (+OTP).
  - `/iclock/<device_token>/…` terminals (plain Django views, flag `ADMS_RECEIVER`), `/legacy/…` old contracts (flag `LEGACY_API_SHIM`), `/api/docs/`, `/api/schema/<version>/`, `/healthz`.
  - **Nothing else** may be routed. A test (`core/tests/test_url_versioning.py`) fails if any DRF view is reachable outside these prefixes.
- Each app's `urls.py` exports four lists — `staff_urlpatterns`, `public_urlpatterns`, `agent_urlpatterns`, `customer_urlpatterns` — which `flarize/urls.py` mounts under the version surfaces. Apps only use the path prefixes they own (see table in PLAN §3.3/§3.4; e.g. catalog owns `catalog/` staff and `products/` public).
- Trailing slashes on every versioned path. `uid` in every path and body (`lookup_field = "uid"`); integer ids never leave the service layer.
- Staff views extend `core.views.BaseViewSet`/`core.views.BaseAPIView`: declare `module = "<registry module>"` and an explicit `action_permissions = {drf_action: registry_action}`; unmapped actions are denied. Record scope is applied by the base class (`scope_queryset`) — override `base_queryset()`, never `get_queryset()`.
- Workflow transitions are POST sub-resources (`…/issue/`, `…/publish/`), never PATCH of `status`. PATCH/actions on versioned rows accept `expected_version` → 409 `stale_version`.
- Pagination: `flarize.pagination.StandardPagination` (page_size 25, max 200) or cursor pagination for logs. No unbounded list.
- Every endpoint appears in the OpenAPI schema (drf-spectacular) with request/response serializers.

## RBAC
- One closed registry in `accounts/registry.py` (PLAN §3.2): modules × actions + allowed scopes. Roles store `permissions` (`{module: [actions]}`) and `scopes` (`{module: scope}`), normalised against the registry on save. No `is_superuser`/`is_staff` bypass anywhere. Default deny. `deny_self_action` guard where PLAN says so.
- Scope filters are registered per module with `core.scopes.register(module, scope)(fn)`.

## Tests (pytest + pytest-django + factory_boy)
- Factories in `<app>/tests/factories.py`; shared fixtures in the root `conftest.py` (`api_client`, `make_user(grants=..., scopes=...)`, `auth_client`, `drain_outbox`).
- Every staff endpoint: anonymous 401, missing permission 403, scope filtering, happy path, validation error envelope, `stale_version` where applicable. Every public endpoint: shape, caching headers, throttle scope. Every service: happy path + every `DomainError`. Engines: golden parity cases.
- Tests call literal versioned paths (`/api/v1/catalog/components/`), never `reverse()` without a version.
- Coverage ≥ 85 % on `*/services/` and `engines/`.

## Git protocol for work packages (WPs)
- Integration branch: `claude/bold-goodall-lxgwep`. Each WP works on its own branch `wp/<name>` (in its own worktree when run in parallel).
- Start of a WP: `git merge --no-edit claude/bold-goodall-lxgwep` so you build on the latest integrated code.
- Touch only your own app directories, their tests, `requirements/*.txt` (append), `docs/decisions/<wp>.md`, `docs/DEVIATIONS.md` (append). If you must change shared code (`flarize/`, `core/`, `accounts/`), keep it minimal, backward compatible, and list it under "Shared changes" in your WP report.
- Commit when green (tests + lint + makemigrations check). Commit messages end with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Gi9mduW3rUQFGft9GwjEYq
  ```
- Never commit secrets, `.env` files, keys, or data dumps. Dev RSA keys are generated at runtime into `var/` (git-ignored).
