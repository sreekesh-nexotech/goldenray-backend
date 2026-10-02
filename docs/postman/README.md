# Flarize Platform API: Postman collection

This collection covers **every HTTP endpoint** of the backend: 985 (method, path) pairs. `COVERAGE_REPORT.md` gives the current request count and shows how completeness is proven.

It is **generated from the code. Never edit the JSON by hand.** To change it, regenerate it (see the last section).

| File | What it is |
|---|---|
| `collection.postman_collection.json` | The collection (Postman Collection v2.1). |
| `local.postman_environment.json` | Environment for the local Docker stack (`http://127.0.0.1:8000`). |
| `staging.postman_environment.json`, `prod.postman_environment.json` | Templates. Set `base_url`; every secret is empty. |
| `COVERAGE_REPORT.md` | Inventory vs collection (0 missing), counts, exclusions, endpoints needing review. |
| `_work/` | The scripts and intermediate files that produce all of the above. |

## Import

1. In Postman, choose **Import** and select `collection.postman_collection.json` and the environment files.
   Newman works too: `npx newman run docs/postman/collection.postman_collection.json -e docs/postman/local.postman_environment.json`.
2. Select the **Flarize local** environment, or a copy of staging/prod with `base_url` set.
3. Fill in these environment values. They are *secret* type and are never committed with values.
   - `staff_email` and `staff_password`: a staff user. Locally, the dev stack creates `DEV_ADMIN_EMAIL` (default `admin@flarize.local`) on the first `make up` and prints a one-time set-password link in `make logs-setup`. There is no default password.
   - `staff_new_password`: only needed for *Change my password*. It really changes the password.
   - `password_reset_token`: only needed for *Set a new password with a reset token*. The token arrives by e-mail (`#token=` in the link).
   - `agent_token` is captured automatically by *Create an office agent* and *Rotate token*. Set it by hand to use an existing agent.

## Variables and auth

- **Auth** is set once at collection level: bearer `{{access_token}}` (staff JWT). Requests on other surfaces override it:
  - `/api/public/…`, login/refresh/logout and `/healthz`, schema and docs: no auth.
  - `/api/agent/…`: bearer `{{agent_token}}`.
  - Customer links, signed download links and `/iclock/…`: no auth. The credential is a token in the path, held in a variable.
- **00 Auth › Sign in** stores `access_token` and `refresh_token`. **Rotate the refresh token** refreshes both, since the refresh token travels in the JSON body.
- **ID chaining:** every create stores the new record's `uid` in a collection variable, which later requests use in their paths and bodies. For example *Create a component* sets `{{catalog_components_uid}}`, and *Get a component* uses it. Values the API never creates are captured from list responses instead (`results.0.uid`), such as sessions, slugs and slot keys. One-time tokens are pulled out of returned links with a regex: customer approval links, signed downloads, preview tokens. Path variables start with a realistic example value, so a single request works on its own if you paste in a real uid.
- `api_version` is `v1`. It is the only allowed version: `/api/v2/…` doesn't resolve.
- Captured values are written to the collection variables, and also to the active environment when one is selected.

## Run order

Folders are ordered to run top to bottom on a fresh database:

| Folder | Contents |
|---|---|
| `00 Auth` | Sign in, then me, sessions and password flows. |
| `01 Platform` → `06 HR, attendance & devices` | Staff API by domain, in dependency order: platform, product master, configuration, sales, website content, HR/devices. Inside each resource: list, create, retrieve, update, workflow actions. |
| `07 Public website API` · `08 Office agent API` · `09 Customer links` | The other versioned surfaces. They reuse ids and tokens created above. |
| `10 iclock terminals` | eSSL/ZKTeco push protocol, plain text. Needs flag **ADMS_RECEIVER** on, and a device token from *Devices › Enable ADMS push*. |
| `11 Legacy shim` | Old website contracts. Needs flag **LEGACY_API_SHIM** on (404 otherwise), and the legacy paths reached directly under `/legacy/`. |
| `12 Refused methods` | PUT routes the router maps but every caller is refused (403, or 405 for a caller who holds the permission). Each request asserts that status. |
| `13 Ops & docs` | `/healthz`, the OpenAPI schema, Swagger UI, and the DEBUG-only public media route. |
| `98 Cleanup` | Every staff DELETE, in reverse dependency order, with nested resources before their parents. |
| `99 Sign out` | Logout. |

**Feature flags.** The *10 iclock*, *11 Legacy shim* and *02 Product master › Inventory* folders each **start with a "Turn <FLAG> on (run this first)" request**: while a flag is off, every request behind it answers a plain 404, and its test then names the flag. You can also turn flags on with **01 Platform › Core › Turn a feature flag on or off** (`PATCH /api/v1/settings/flags/`, body `{"key": "ADMS_RECEIVER", "enabled": true}`; needs `settings.edit`). The inventory routes need `INVENTORY_STOCK`, and the agreement price override needs `AGREEMENTS_PRICE_OVERRIDE`.

Every request asserts its success status and the JSON shape: `count` and `results[]` for paginated lists, otherwise the key fields. Its description gives:
- the permission (`module.action`) and record scope;
- path params, body fields (required ones marked) and side effects (outbox events, Celery tasks, SMS/e-mail, document rendering);
- the possible responses, and the view and source file it came from.

Saved examples hold realistic success bodies.

**Expect some failures in a full unattended run.** Workflows have business preconditions that one linear pass can't always meet:
- Pack releases need an approved configuration and a current price release.
- A blank sale order needs both a panel and an inverter before it can be issued.
- A pending discount blocks issue, and the self-approval guard stops a single user from approving their own.
- `expected_version: 1` goes stale once an earlier step has changed the row.

The request descriptions call these out. The run order keeps them as few as possible, but run a domain folder at a time when you are testing a feature. **Don't run the collection against production.** It creates, publishes and deletes data, changes feature flags and settings, and sends real OTP SMS and e-mails, depending on the provider configuration.

## Regenerate (when endpoints change)

With the dev stack up (`make up`), run from the repo root:

```bash
bash docs/postman/_work/regenerate.sh
```

It runs, in order:

1. `dump_urls.py` (in the API container): the live URL resolver gives every (method, path), feature flag, and refused method.
2. `dump_schema.sh`: drf-spectacular writes the OpenAPI v1 schema.
3. `static_scan.py`: an ast and grep scan of every `urls.py` and views module. It fails if any declared route doesn't resolve.
4. `build_inventory.py`: merges the three sources into `inventory.json`. It fails on schema operations with no route, or on unexplained gaps.
5. `seed_fragments.py`, then `--add-missing`, then `--sync-fragments`:
   - builds seeds from the schema;
   - starts a fragment for any new endpoint;
   - copies the inventory-owned fields (view, flag, refusal) into the fragments.
6. `reconcile.py`: diffs the inventory against the fragments (missing, phantom, mismatch, malformed).
7. `build_collection.py`: writes the collection and environments. The output is deterministic, so an unchanged input gives byte-identical files.
8. `verify_coverage.py`: parses the **final collection** and diffs it against the inventory. It exits non-zero on any missing endpoint.
9. `validate_collection.py`: validates against the official Postman v2.1 JSON schema (vendored), and checks that no environment holds a secret value.
10. `write_report.py`: writes `COVERAGE_REPORT.md`.

The host needs only `python3` with `jsonschema`. Django runs in the container.

**What it can't do automatically:** describe a *new* endpoint from its code. A new endpoint first appears as a seed-only fragment, built from the OpenAPI schema and listed under "needs review" in `COVERAGE_REPORT.md`. Fill it in from the view and serializer, using the rules in `_work/AGENT_BRIEF.md`, edit `_work/fragments/<group>.json`, and rerun. The fragments are the hand-checked source of every description, example body and capture.
