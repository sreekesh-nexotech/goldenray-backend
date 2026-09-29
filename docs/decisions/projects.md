# WP projects — minimal projects app (D-5)

Work package *projects* builds the `projects` app of PLAN §2.6 (`projects_project`), §3.4 (Projects: `projects/` CRUD +
`lock-bom/`, `cost-inputs/`, `commission/`, `close/`), §3.5 (`site_inspections.released` → optional auto-create, D-8),
the customer-timeline provider and the §7.4 import of Flarize `bom-state.json` / `workspace-state.json`. Legacy source:
Flarize `projectWorkspace.js`, `projectBom.js`, `bomLock.js`, `bomSnapshot.js` (workflows spec B.3, C.5, D.5).
Deviations: DV-104, DV-105. Business defaults: `/home/user/platform-reference/business-defaults.md` (none specific to
projects; the general rule "keep today's behaviour unless it leaks cost/margin" drives the `pricing_internal` redaction).

## What exists

| Area | Where | Notes |
|---|---|---|
| Table | `projects/models/project.py` | `projects_project` (BaseModel). `CHECK`s on every enum (status, system_type, phase, kseb_status), `size_kw > 0`, lifecycle invariants: IN_PROGRESS/COMMISSIONED/CLOSED ⇒ `bom_lock` + `bom_locked_at`; PLANNED ⇒ no `bom_lock`; COMMISSIONED/CLOSED ⇒ `commissioned_on`; CLOSED ⇒ `closed_at`; CANCELLED ⇒ `cancelled_at` + non-empty `cancel_reason`. Partial uniques: `number` (live), one live non-cancelled project per `site_inspection_uid`. Indexes (status, -created_at), (customer), (head). |
| Services | `projects/services/projects.py` | create/update/delete, `set_cost_inputs`, `commission`, `close`, `cancel`, `create_from_inspection`. |
| | `projects/services/bom_lock.py` | `lock_bom` (checker run + acknowledgements + snapshot). |
| | `projects/services/customer_timeline.py` | provider `projects.projects` for `customers/<uid>/timeline/`. |
| | `projects/services/legacy_import.py` | `import_flarize_workspace_projects(rows)`, `report_flarize_bom_state(state)`. |
| Events | `projects/events.py` | consumes `site_inspections.released` (behind `PROJECTS_AUTO_CREATE_ON_RELEASE`, default **off**). |
| Staff API | `projects/views/projects.py`, `projects/urls.py` | below. |
| Registrations | `ProjectsConfig.ready()` | `customers.services.merge.register_dependant(Project, "customer", blocks_delete=True)`; timeline provider. |
| Tests | `projects/tests/` | API (401/403/scope/validation/stale/happy/N+1 guard on the list), lock-bom (blocked → waived, missing acknowledgements, carried acknowledgements, real checker context), lifecycle, cost-inputs redaction, events, timeline, merge, legacy import (committed masked fixtures). Coverage of `projects/services/` ≥ 95 %. |

### Endpoints (staff, `/api/v1/`)

| Path | Permission | Notes |
|---|---|---|
| `GET projects/` (`status`, `system_type`, `kseb_status`, `customer`, `head`, `site_inspection_uid`, `bom_locked`, `created_from/to`, `search`, `ordering`) | `projects.view` | paginated; list rows carry neither `bom_lock` nor `cost_inputs` |
| `GET projects/<uid>/` | `projects.view` | with `bom_lock` (landed costs null without `pricing_internal.view`) and `cost_inputs` (null without it) |
| `POST projects/` | `projects.create` | `customer_uid` required; `head_uid`, `title`, system fields, link uids, `scheduled_on`, `kseb_status`, `note`; 409 `inspection_has_project` |
| `PATCH projects/<uid>/` (`expected_version`) | `projects.edit` | 409 `bom_locked` (customer/system fields after the lock), `project_finished` (CLOSED/CANCELLED), `inspection_has_project` |
| `DELETE projects/<uid>/` | `projects.archive` | soft; only PLANNED/CANCELLED (409 `project_not_deletable`) |
| `POST projects/<uid>/lock-bom/` (`architecture`, `lines[{component_uid, role, quantity, selection_method?, reason?}]`, `acknowledgements[{rule_code, reason}]`, `expected_version`) | `projects.lock` | PLANNED → IN_PROGRESS; 409 `bom_lock_refused`, `bom_already_locked`, `invalid_transition`, `no_active_rule_set` |
| `POST projects/<uid>/cost-inputs/` (`distance_km`, `installation_type` FLAT/SHEET/ELEVATED, `vehicle_type`, `structure_material_cost`, `special_works[{label, amount}]`, `rate_effective_at`, `size_key`, `expected_version`) | `projects.edit` + `pricing_internal.view` | 409 `project_not_open` |
| `POST projects/<uid>/commission/` (`commissioned_on` ≤ today and ≥ lock date, `kseb_status`, `note`) | `projects.edit` | IN_PROGRESS → COMMISSIONED |
| `POST projects/<uid>/close/` (`note`) | `projects.edit` | COMMISSIONED → CLOSED |
| `POST projects/<uid>/cancel/` (`reason`) | `projects.archive` | PLANNED/IN_PROGRESS → CANCELLED (DV-105) |

Record scope: the registry allows only `all` for `projects` (PLAN §3.2); the seeded Project Head holds `projects` full with
scope `all`, Engineering `projects.view`. The base views apply it.

## Decisions not spelled out in the PLAN

1. **References by uid (DV-104).** quotations, agreements and site_inspections are built in parallel and are sibling
   sales contexts; projects stores their uids (and the lead's) without FKs and never imports their code. The customer is
   a real FK (PROTECT); customer merges re-point projects and a customer with live projects cannot be deleted.
2. **Lifecycle.** `PLANNED ─lock-bom→ IN_PROGRESS ─commission→ COMMISSIONED ─close→ CLOSED`; `cancel` from PLANNED or
   IN_PROGRESS. Locking the BOM is what starts execution (Flarize: cost becomes available only after the lock), so it is
   the transition into IN_PROGRESS. There is no unlock (Flarize had none). Only PLANNED or CANCELLED projects can be
   archived.
3. **Lock = Flarize `attemptLock` with engineering's waivers.** The checker (`engines.engineering_checker.check_project_bom`)
   runs on the lines with the ACTIVE `engineeringChecker` rule set over the Flarize-shaped catalog priced by the current
   PriceRelease (`packs.services.context.engine_context`, a documented configuration read). The run is committed as an
   `engineering_run` (`PROJECT_BOM`, the project uid) whether the lock succeeds or not. The lock is refused while a BLOCK
   finding has no acknowledgement (engineering waives it with `engineering.approve` under
   `engineering/findings/<uid>/acknowledge/` — Flarize simply refused BLOCKED BOMs; the platform's waiver path is DV-101) or
   a WARN finding whose rule `requiresAcknowledgement` has neither an acknowledgement in the request nor one carried from
   an earlier run of the project (same identity **and** same message: the engineering identity
   `rule|components` is shared by every component-less finding of a rule — `|PBC-K-001|` is every missing role — so a
   waiver for "STRUCTURE missing" must not let a BOM missing its INVERTER lock). The request's acknowledgements become `engineering_acknowledgement`
   rows, so they carry over. Error `bom_lock_refused` lists `errors.blocked`, `errors.missing_acknowledgements` (with
   finding uids) and `errors.engineering_run`.
4. **Snapshot** (`bom_lock`, schema `projects.bom_lock/1`): `status`, `locked_at`, `locked_by` (user uid),
   `architecture`, `system_type`, `phase`, `price_release_number`, `engineering {run_uid, result, rules_version, counts}`,
   `lines[{component_uid, sku, name, role, quantity, selection_method, reason, unit_list_price, unit_landed_cost}]` (prices
   frozen from the PriceRelease payload — later price changes never touch it), `acknowledgements[{rule_code, severity,
   identity, reason, acknowledged_by, acknowledged_at, waiver}]`, `legacy`. After the lock the customer and the system
   fields (type, tier, size, phase) cannot change (409 `bom_locked`).
5. **Cost inputs are internal.** Flarize's `COST_INPUT_EDIT` was a Project Head capability and purchase costs never
   reached Sales; here `cost-inputs/` needs `pricing_internal.view` besides `projects.edit`, and responses hide
   `cost_inputs` and the snapshot's `unit_landed_cost` without it. The document is validated (the Flarize `costInputs`
   fields, snake_case) and stored as JSON. Cost/pricing panels (Flarize `/cost`, `/pricing`, `/discount`) are not part of
   the minimal app (D-5; margin approval lives with quotations).
6. **Auto-create (D-8 setting, off).** With `PROJECTS_AUTO_CREATE_ON_RELEASE=True` a released inspection creates one
   PLANNED project (customer, inspection/lead/agreement uids, system type, size, phase; actor = system). Idempotent per
   inspection (a cancelled project allows a new one). A merged customer is followed to the survivor; a payload that breaks
   the contract or names an unknown customer is logged and dropped (retrying cannot fix it).
7. **Timeline.** One entry per milestone of each project of the customer: `projects.created`, `projects.bom_locked`,
   `projects.commissioned`, `projects.closed`, `projects.cancelled`; shown only to holders of `projects.view`.
8. **Events emitted:** `projects.created` (`source` staff / `site_inspections.released`), `projects.updated`
   (`fields`), `projects.deleted`, `projects.cost_inputs_set`, `projects.bom_locked` (`engineering_run_uid`,
   `engineering_result`), `projects.commissioned` (`commissioned_on`), `projects.closed`, `projects.cancelled`
   (`reason`); every payload carries `project_uid`, `number`, `customer_uid`, `status`.

## Event contracts

**Consumed — `site_inspections.released`** (producer: site_inspections, built in parallel; projects only reads the
payload):

```json
{
  "inspection_uid": "<uuid>",        // required
  "customer_uid": "<uuid>",          // required; a merged customer is followed to the survivor
  "lead_uid": "<uuid>" | null,
  "agreement_uid": "<uuid>" | null,
  "system_type": "ON_GRID" | "HYBRID" | "UNDECIDED" | "OFF_GRID",   // anything else is stored empty
  "size_kw": 5 | "5.00" | null,      // > 0, stored numeric(6,2)
  "phase": "1P" | "3P" | null,
  "released_at": "<ISO date-time>"   // copied into the project note
}
```

Handled only when `PROJECTS_AUTO_CREATE_ON_RELEASE` is true (env, default false). Tests emit the event directly.

## Legacy mapping

### `workspace-state.json` → `projects_project` (`import_flarize_workspace_projects`)

Traceability: `core_legacy_map` `FLARIZE` / `workspace_projects` / `projectId`. Idempotent: a re-run updates changed
rows, never duplicates; the import never rewinds a project the platform moved on (commissioned, closed, cancelled).

| Flarize | Platform |
|---|---|
| `projectId` | legacy map; a new `number` (`PROJ-<n>`) |
| project without `lock` (`bom.status` DRAFT) | not migrated — `open_workspace_not_migrated` (PLAN §7.4 "open workspaces … report") |
| `customer.customerId` | `customer` via `FLARIZE/customers`, a merged customer followed to the survivor; else `customers_customer.code`; else `customer.phone` (E.164) — none → `customer_not_found`, skipped |
| `sysType` ongrid / hybrid | `system_type` ON_GRID / HYBRID (else UNDECIDED, `unknown_system_type`) |
| `tier`, `sizeKw`, `phase` | `tier` (upper case), `size_kw`, `phase` (`unknown_phase`, `invalid_size`) |
| `packageId` | `title` |
| `lock.lockedAt`, `lock.lockedBy` | `bom_locked_at`, `bom_locked_by` (via `FLARIZE/users`; `unmapped_user`) |
| `lock.lines[]` `{componentId, role, quantity, selectionMethod, unitSellingPrice, unitPurchaseCost}` | `bom_lock.lines[]` `{component_uid (by SKU; unknown_component), sku, name, role, quantity, selection_method, unit_list_price, unit_landed_cost}` |
| `lock.engineeringStatus` VALID/WARNING/BLOCKED, `rulesVersion`, `lastValidation.counts` | `bom_lock.engineering {result PASS/WARN/FAIL, rules_version, counts, run_uid null}` |
| `lock.acknowledgements[]` `{ruleId, acknowledgedBy, acknowledgedAt, reason}` | `bom_lock.acknowledgements[]` (`legacy_actor` keeps the Flarize id) |
| `lock.packageId`, `dataVersion`, `lockedBy` | `bom_lock.source` |
| `costInputs` | `cost_inputs` (snake_case keys; `installationType` upper-cased — 6 locked projects say `flat`) |
| status | IN_PROGRESS (locked BOM) |
| `createdAt`, `createdBy` | `created_at`, `created_by` |
| `bom.packageLines/overrides/removedRoles/history`, `lastValidation.findings`, `history`, `discount`, `lastCost`, `lastPricing` | not migrated (the lock snapshot is the record; discounts belong to quotations) |

### `bom-state.json`

The legacy React BOM tool's UI state (selections, overrides, customer fields, `savedAt`) — not a project and not a locked
BOM. `report_flarize_bom_state` reports it as `ui_state_not_migrated`; nothing is written.

## Parity / rehearsal evidence

* Fixtures `projects/tests/fixtures/flarize_workspace_state.json` (4 real projects: 3 locked, 1 open; customer names,
  phones and addresses masked) and `flarize_bom_state.json` (masked) exported read-only from
  `/home/user/flarize-main/flarize/data`; `test_legacy_import.py` checks every mapped column, the violations, idempotency,
  "never rewinds" and dry-run.
* Rehearsal on the full data (2026-09-29, rolled back; customers imported first through
  `customers.services.legacy_import`, catalog and users not imported): 290 workspace projects, 186 locked →
  **88 created**, 202 skipped (104 `open_workspace_not_migrated`, 98 `customer_not_found` — the Flarize E2E/UAT stubs
  "E2E Customer" and `UAT-*` ids that are not in `customers.json`); a second run: 0 created, 0 updated, 290 skipped.
  `unknown_component`/`unmapped_user` vanish once the catalog and users imports run first.
* The lock's checker is the engines port verified against Flarize in engines-rules / packs-engineering; the tests run it
  both over a hand-written catalog and over the platform's real catalog context.

## Shared changes

* `flarize/settings/base.py`: new setting `PROJECTS_AUTO_CREATE_ON_RELEASE` (env, default `False`);
  `SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]` gains `ProjectStatusEnum`, `ProjectSystemTypeEnum` (names only).
* `customers/tests/test_timeline.py`: the provider assertion checks a subset (new contexts register providers).
* `cost-inputs/` `installation_type` reuses `pricing.models.choices.InstallationType` (product-master read).

## For later packages

* **site_inspections**: emit `site_inspections.released` with the payload above.
* **quotations / agreements**: set `quotation_version_uid` / `agreement_uid` via `PATCH projects/<uid>/`, or emit an event
  projects can consume; a quotation's locked BOM can be passed as `lock-bom/` lines.
* **migrations_tools**: after users, catalog and customers: `import_flarize_workspace_projects(list(state["projects"].values()))`
  and `report_flarize_bom_state(bom_state)`; print the reports.

## Review (adversarial pass)

* **Waiver scope (fixed).** Carried acknowledgements matched on the engineering identity alone; component-less findings
  share it, so one waiver of PBC-K-001 "STRUCTURE missing" let a BOM with no INVERTER lock. Carry-over now needs the same
  identity and message (`projects/services/bom_lock.py::_key`); `TestWaiverScope`.
* **Lock gate parity.** All 186 Flarize lock records re-checked with the platform's checker call shape (architecture,
  phase, sysType, lines) over the Flarize catalog and battery master: 186/186 give the recorded `engineeringStatus` and
  exactly the recorded acknowledged rule set.
* **Import (fixed).** `installationType` `flat` → `FLAT`; a customer merged after the first import is followed to the
  survivor on a re-run.
