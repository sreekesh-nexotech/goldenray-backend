# Phase-2 enrichment brief (one subagent per group)

You turn **seeds** (machine-generated from the OpenAPI schema + live URL resolver) into **fragments**: accurate, code-verified
descriptions of every endpoint in your group, from which `build_collection.py` generates the Postman collection.

Repo root: `/home/devneeraja/projects/flarize-backend/goldenray-backend` (Django 6 + DRF; read `CLAUDE.md` for conventions).

## Input / output
- Input: `docs/postman/_work/seeds/<GROUP>.json` — a JSON list, one object per (method, path).
- Output: `docs/postman/_work/fragments/<GROUP>.json` — the **same ids, every one of them**, enriched. Write valid JSON
  (generate it with a Python script — `json.dump(..., indent=1, ensure_ascii=False)` — never hand-edit giant JSON).
  Put any helper scripts in your scratchpad, NOT in the repo.
- Reference: `docs/postman/_work/variables.json` (every Postman variable: which request captures it, which use it),
  `docs/postman/_work/openapi.v1.json`, `docs/postman/_work/inventory.json`.
- Self-check (must print `RESULT: OK`): `python3 docs/postman/_work/reconcile.py --group <GROUP>`

## Hard rules
- Touch nothing except `docs/postman/_work/fragments/<GROUP>.json`. No commits, no migrations, no DB writes, no edits to app code.
- Every claim comes from code you actually read. If something cannot be determined, say so in `unresolved` (short).
- Django is NOT installed on the host. If you need introspection, it is read-only in the running container:
  `docker exec -w /app flarize-dev-api-1 python -c "import django,os;os.environ.setdefault('DJANGO_SETTINGS_MODULE','flarize.settings.dev');django.setup(); ..."`
  (never call anything that writes).

## For EVERY non-`auto` row, read the real code
`source_file` points at the view. Read: the view class and its bases/mixins (`core/views/`), `action_permissions` + `module`,
`base_queryset`, serializer(s) used for request and response (`get_serializer_class`, `extend_schema` decorators),
validators, `filterset_class` / `search_fields` / `ordering_fields`, pagination, `throttle_scope`, authentication, and the
service function(s) it calls (`<app>/services/`) for side effects. Then fill/correct:

| key | what to put |
|---|---|
| `summary` | ≤ 8 words, imperative, human: "List components", "Publish a pack release". |
| `description` | Markdown, 1–5 sentences: what it does, important business rules/validation, notable error codes (e.g. `409 stale_version`), flag gates. |
| `folder` | Resource path **inside the app** as human labels, e.g. `["Components"]`, `["Templates", "Image groups"]`. Same labels for every row of one resource. (The build adds surface/domain/app levels itself.) |
| `auth` | `scheme` ∈ `staff_jwt` · `none` · `agent_token` · `customer_link` (token in path, OTP) · `device_token` (iclock token in path) · `signed_link` (single-use download token in path) · `refresh_cookie`. `permission`: registry `"module.action"` from `action_permissions` (null if none). `scope`: record-scope note if `scope_queryset`/`core.scopes` applies. `notes`: anything else (throttle scope, flag). |
| `headers` | Only custom headers the code reads (e.g. idempotency keys, `X-…`). `Content-Type` is added by the build. |
| `path_params` | Keep `variable` names from the seed unless clearly wrong. Fix `type` (`uuid`, `str`, `slug`, `int`), `description`, `example`, `choices`. |
| `query_params` | Every filter/search/ordering/pagination param the view accepts: `{key, value (realistic example), description, required}`. Optional ones become disabled requests params. |
| `body` | `null` for GET/DELETE. POST/PUT/PATCH: `{mode:"raw", content_type:"application/json", raw:<object>}` or `{mode:"formdata", content_type:"multipart/form-data", formdata:[{key,type:"text"|"file",value|src,description}]}`; a non-JSON raw body (e.g. iclock text) is `{mode:"raw", content_type:"text/plain", raw:"<string>"}`. Example must be **valid against the serializer**: realistic Indian-solar-business values, valid enum choices, required fields present, optional fields included when useful. Where a field takes another resource's uid, use that resource's `{{variable}}` (see variables.json) so runs chain. PATCH on versioned rows includes `"expected_version": 1` when the serializer accepts it. If a POST/PUT/PATCH takes **no** body: `body: null` and `"no_body": true`. Keep `fields` (list of `{name,type,required,choices?}`) accurate. |
| `success_status` | The status the view really returns on success (201 create, 204 delete, 200, 202 async, 301/302 redirect, file download 200…). |
| `responses` | Success example (realistic, matches the response serializer; paginated lists = `{count,next,previous,results:[…]}` — check the pagination class) + the important error statuses `{status, description}` (400 validation, 401, 403, 404, 409 …). |
| `side_effects` | Outbox events emitted (`core.outbox.emit("...")`), Celery tasks enqueued, documents rendered, SMS/e-mail/OTP sent, cache bumps worth knowing, audit rows. Empty list if none. |
| `capture` | Creates: `[{"var": "<variable>", "from": "uid"}]` (seed pre-fills). If a variable in variables.json is **used but never captured** (no create endpoint: e.g. sessions, slugs, tokens), add a capture on the list/retrieve that exposes it: `{"var": ..., "from": "results.0.uid"}` (dotted JSON path). Tokens that only arrive out-of-band (e-mail/SMS) are not captured — mention in `description`. |
| `order` | Sort key inside the resource folder: list 10, create 20, retrieve 30, update 40, partial_update 50, custom actions 60–80 in a sensible workflow order (e.g. submit 61 → approve 62 → publish 63), destroy 90. |
| `unresolved` | Remove seed notes you resolved. Leave only genuine open questions. |

Do not change `id`, `method`, `path`, `app`, `surface`, `group`, `view`, `action`, `refused`, `flag`.

## `auto` rows
`"auto": true` rows (PUT routes that are always refused; legacy slash-less 301 redirects) are complete by construction.
Copy them unchanged — but spot-check a couple (e.g. confirm `update` really is absent from `action_permissions`); if one is
wrong, fix it, set `"auto": false`, and report it.

## Return (your final message)
`GROUP <name>: assigned <n>, documented <n>, auto <n>, reconcile <OK|FAIL>` then a short list of `unresolved` items and
any discrepancies you found between the OpenAPI schema and the code. The counts must match — keep working until they do.
