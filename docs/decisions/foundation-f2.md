# F2 — Accounts (auth, sessions, users, roles, seeds, authorization caching) and audit

Work package F2 completes the platform identity and audit layer of PLAN §4.3 on top of F1. It follows PLAN §1.4
(Auth, RBAC, Audit), §2.1 (`accounts_*`, `audit_log`), §3.1, §3.2, §3.4 ("Auth & account", "Admin") and the standard
§3.1–§3.3 (adopting the good parts, avoiding every listed defect). Deviations: DV-8, DV-9 in `docs/DEVIATIONS.md`.

## What exists after F2

| Area | Where | Notes |
|---|---|---|
| Staff endpoints | `accounts/views/{auth,users,roles}.py`, `accounts/urls.py` | `auth/login|refresh|logout|me|password/change|password/reset-request|password/reset|sessions[/<uid>]/`, `users/` CRUD + `deactivate/`, `reactivate/`, `force-reset/`, `roles/` CRUD + `registry/`. Every path ends with `/`, lives under `/api/v1/`, is in the OpenAPI schema. |
| Auth services | `accounts/services/{auth,sessions,lockout,passwords,emails}.py` | login/refresh/logout, password change/reset, sessions, DB-counted lockout, password policy + reset tokens, account e-mails. |
| Authorization | `accounts/services/authz.py` | `get_grants`/`can`/`scope_for` with a version-keyed cross-request cache, Super Admin identity, escalation guards, `deny_self_action`. `core.permissions`, `core.scopes` and `core.dashboard` call `can`/`scope_for` (the F1 names `has_permission`/`get_scope` remain as aliases). |
| Admin services | `accounts/services/{users,roles,seeds}.py` | user/role CRUD with guards; the 12 seeded roles; bootstrap; HR-driven deactivation. |
| Commands | `seed_roles`, `bootstrap_admin --email`, `ensure_audit_partitions --months N` | |
| Audit | `audit/` | partitioned `audit_log` (raw-SQL migration, unmanaged model), `audit.context`, `AuditMiddleware`, `audit.services.record`, `GET audit/`. |
| Notifications | `core/notifications.py`, `core.tasks.send_email` | Django e-mail backend (SMTP / console in dev / locmem in tests), enqueued on commit. |
| Ops | `docs/ops/audit-log.md` | owner/app roles, partition schedule, checks. |

## Decisions not spelled out in the PLAN

1. **Tokens carry identity only: `sub` (user uid), `sid` (session uid), `jti`** plus `token_type/exp/iat/iss`. A token
   without `sid` is refused, so SimpleJWT's own `for_user()` can never mint a usable token; only
   `accounts.services.sessions.start_session` (login) does. `conftest.auth_client` uses it too (`client.tokens`).
2. **Every access token is bound to a live session.** `SessionAwareJWTAuthentication` checks the session on every
   request through a 30-second liveness cache keyed by the version namespaces `accounts:session:<sid>` and
   `accounts:user:<uid>`; every revocation bumps them, so logout, "sign out that device", deactivation, forced reset
   and password change invalidate access tokens on the very next request. The cached value is the session's expiry,
   so an expired session is refused even on a cache hit. Users with `must_reset_password` are refused too.
3. **Refresh rotation with reuse detection.** The session row stores the *current* refresh `jti`. A refresh
   blacklists the old token, rotates the `jti`, slides the expiry and re-polices the session (revoked, expired,
   user inactive/deleted, `must_reset_password` → the session is ended). A superseded refresh token replayed within
   `ACCOUNTS_REFRESH_REUSE_GRACE_SECONDS` (30 s — a racing tab or BFF retry) is refused with
   `refresh_token_rotated`; after that it is treated as theft: the whole session is ended (`refresh_token_reused`)
   and audited. Sessions slide with each refresh but never outlive `ACCOUNTS_SESSION_MAX_AGE` (30 days) from login.
4. **Lockout** counts `accounts_login_attempt` rows (5 failures per 15 minutes per e-mail and per client IP, all
   env-tunable). A success clears the e-mail's count, never an IP's. Refused-while-locked attempts are audited but
   not counted, so a lockout expires 15 minutes after the failure that triggered it. A wrong *current* password on
   `password/change/` counts too (a stolen access token is not a password oracle). The hasher runs on every path —
   unknown e-mail, unusable password, locked out, reset required — against a dummy Argon2 hash when needed; tests
   prove it by spying on `Argon2PasswordHasher.verify`.
5. **Error codes** the BFF can branch on: `invalid_credentials` (401, identical for unknown/wrong/inactive),
   `login_locked` (429 + `Retry-After`), `password_reset_required` (403), `token_invalid`, `session_revoked`,
   `session_expired`, `refresh_token_rotated`, `refresh_token_reused`, `user_inactive` (401),
   `invalid_current_password`, `reset_token_invalid`, `reset_token_expired` (400). Access-token failures stay
   `not_authenticated`; `flarize.exceptions` now appends SimpleJWT's reason to `error_codes`
   (e.g. `["not_authenticated", "session_revoked"]`) and uses its message. `DomainError.retry_after` becomes a
   `Retry-After` header.
6. **No passwords anywhere in admin flows.** A created user gets an unusable password, `must_reset_password` and a
   72-hour single-use invitation link; `force-reset/` does the same for an existing account (and ends its
   sessions); `bootstrap_admin` prints a 24-hour link. A login with the right password while `must_reset_password`
   is set gets `password_reset_required` and no tokens. The password policy (`AUTH_PASSWORD_VALIDATORS`, with the
   user for similarity) runs on every path that sets a password: change, reset/invitation and
   `UserManager.create_user`. Reset tokens: 256-bit, sha256 stored, single use, a new one voids the previous ones,
   at most 3 per account per hour, and the link carries the token in the URL **fragment** (never sent to a server
   or written to access logs). `reset-request/` always answers 200.
7. **Permissions of the admin endpoints.** `users`: `view` list/detail, `create`, `edit` profile fields, `manage`
   additionally for e-mail and role changes (an e-mail change followed by a reset is an account takeover) and for
   `force-reset/`, `archive` for deactivate/reactivate/delete. `roles`: `view` (also `registry/`), `create`,
   `edit`, `manage` for delete. The `auth/*` self-service endpoints need only a valid session: they touch nothing
   but the caller's own account (`me`, password, own sessions).
8. **Escalation guards** (no god-flag, standard §3.3): assigning a role, creating/editing a role, or changing a
   user requires holding every grant — and at least as wide a scope (`all` covers any scope; narrower scopes cover
   only themselves) — involved; nobody changes their own role, the grants of the role they hold, or deactivates /
   deletes / force-resets themselves; only a Super Admin changes a Super Admin, assigns the Super Admin role or
   touches that role. The Super Admin role is the *system* role with slug `super-admin`; seeded slugs are reserved.
   Admin holds every grant (PLAN "everything except `users.manage` on Super Admins") — the exception is enforced by
   the Super Admin rule, not by a missing grant.
9. **Roles** are normalised strictly: unknown modules/actions/scopes, or a scope for a module without permissions,
   are a 400 listing each entry (never silently dropped). System roles are editable, never deletable, slug fixed;
   a role held by a live user cannot be deleted (409 `role_in_use`). A role write bumps `accounts:role:<uid>`; the
   grants cache key also embeds the role row's `version` and `updated_at`, so even writes that bypass the services
   (importers, shell) cannot serve stale grants.
10. **Seeds** (`seed_roles`) implement the PLAN §3.2 table literally (`accounts/tests/test_seed_bootstrap.py` holds
    the table written out); every permitted module gets scope `all` unless the table names a narrower one. The
    command is get-or-create by slug and never modifies an existing role. A registry change must ship with a data
    migration that updates the system roles that need the new module.
11. **Audit** rows are written synchronously in the caller's transaction by `audit.services.record` (never from
    request bodies), with recursive masking of keys whose *words* include `password`, `token`, `secret`, `otp`,
    `account_number`, `api_key`, `authorization` (camelCase/kebab/plural aware; `footprint` is not an `otp`). The
    actor comes from the explicit argument, else the request's audit context (set by the JWT and service-token
    authentication classes), else `SYSTEM`. Every accounts write is audited (login success/failure/locked/blocked,
    logout, session ends/revocations, refresh reuse, password change/reset/request, user and role changes, seeds,
    bootstrap), and so are the F1 writes that were waiting for this package: `core.flag_set`,
    `core.service_credential_issued|rotated|revoked` (the token prefix only, never the token or its hash).
12. **`audit_log`** is monthly range-partitioned with PK `(id, at)` (Postgres requires the partition column in the
    key), a `DEFAULT` partition, and the PLAN's three indexes; Django maps it with an unmanaged model
    (`CompositePrimaryKey("id", "at")`, `id` read back through `RETURNING`). The application refuses updates and
    deletes (model and queryset raise `AppendOnlyError`); the database refuses them for `DB_APP_ROLE` — proven in
    tests with a temporary role and `SET ROLE`. `actor_id` has no FK constraint (DV-9). `GET audit/` is cursor
    paginated (`-at, -id`, 50 per page) with `object_type`, `object_uid`, `actor` (user uid), `action` (exact, or a
    prefix ending in `*`), `from`, `to` (ISO date — `to` includes the whole day — or date-time).
13. **Request ids**: `AuditMiddleware` runs after `RequestIdMiddleware` and reuses its id (a well-formed
    `X-Request-ID` is honoured, anything else replaced by a new `uuid4`), records the trusted client IP and echoes
    the header, so access log, error log and audit rows of one request share one id.
14. **Throttles**: `login` (10/15 min per trusted client IP) on login and both password-reset endpoints; a new
    `token_refresh` scope (300/15 min per IP) on refresh and logout (DV-8); `staff` on the self-service endpoints.
15. **Events**: accounts consumes `hr.employee_deactivated` — payload contract for the HR package:
    `{"employee_uid": "<uuid>", "user_uid": "<uuid>" | null}`; the handler deactivates the user and ends every
    session (idempotent). Accounts emits `accounts.user_deactivated` / `accounts.user_reactivated`
    (`{"user_uid", "reason"?}`) for any context that wants them.
16. **Retention**: `accounts.tasks.purge_auth_records` (Beat, daily 03:17 IST) deletes login attempts older than 90
    days, expired SimpleJWT outstanding/blacklisted tokens and reset rows expired for 30 days. Sessions are kept.
17. **Production guard**: `prod.py` also refuses a non-https `PASSWORD_RESET_URL` and a non-delivering
    `EMAIL_BACKEND` (console/locmem/dummy/filebased).

## Hand-over notes

* Record-owning services call `audit.services.record(action, obj=..., actor=user, before=..., after=...)`; build
  the dicts with `audit.services.snapshot(instance, fields)` and `audit.services.changes(before, after)`.
* Use `accounts.services.authz.deny_self_action(user, record, module="attendance", action="edit")` (and
  `leave.approve`) where PLAN §3.2 requires it; `record` may be the user, a uid, or anything with
  `user`/`user_id`/`owner`/`owner_id`/`employee` (followed up to three hops, e.g. attendance day → employee → user).
  A record it cannot attribute raises `ValueError` (fail closed); an employee without a login is nobody's own record.
* Celery tasks that act for someone wrap their work in `audit.context.bind(actor=..., actor_kind=...)`.
* Services that write roles or users outside `accounts.services` must call `authz.invalidate_role(uid)` /
  `authz.invalidate_user(uid)` (importers: the key also embeds `version`/`updated_at`, so correctness does not
  depend on it).
* Importers that create users should call `accounts.services.passwords.issue_reset(user, ttl=...)` and send the
  link (PLAN §7.2 "reset links sent").
* Deploy: run `ensure_audit_partitions --months 3` after `migrate` and monthly (docs/ops/audit-log.md); set
  `PASSWORD_RESET_URL`, SMTP settings and, with separate roles, `DB_APP_ROLE`.
