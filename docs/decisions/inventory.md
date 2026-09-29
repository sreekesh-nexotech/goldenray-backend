# inventory — the optional stock ledger (flag `INVENTORY_STOCK`)

Work package inventory builds the `inventory` app (DV-6) behind the `INVENTORY_STOCK` feature flag (PLAN §1.4, D-6):
the tables of PLAN §2.3 "Optional stock ledger" (`inventory_location`, `inventory_movement` *(append-only)*, the
`inventory_balance` view), the staff endpoints of §3.4 "Inventory", the `inventory` registry module (§3.2: view,
edit; Procurement holds both), dashboard counters, and the optional booking of committed procurement batches.
Deviations: DV-62 … DV-64 in `docs/DEVIATIONS.md`.

## What exists

| Area | Where | Notes |
|---|---|---|
| Models | `inventory/models/{location,movement}.py` | `inventory_location` (`code`, `name`, `office` → `hr_office` SET_NULL); `inventory_movement` (`component` P, `location` P, `qty numeric(12,3)`, `direction` IN/OUT, `reason` PURCHASE/ISSUE_TO_PROJECT/RETURN/ADJUST, `ref_type`, `ref_uid`, `at`, `by` S, `note` — DV-62); `Balance` maps the `inventory_balance` VIEW (unmanaged, composite key component + location). |
| Constraints | migration `0001_initial` | location code unique among live rows, case-insensitive, format-checked; movement `qty > 0`, enum checks, reason ↔ direction check, `ref_type`/`ref_uid` both-or-neither with an `<app>.<model>` format, ADJUST ⇒ non-blank note, partial unique `(ref_type, ref_uid)` for `procurement.batch_line` (idempotent receipt). Indexes: (component, location) for the balance, (-at, -id) and (location, -at) for the lists, (ref_type, ref_uid). |
| Append-only | `inventory/models/movement.py`, `inventory/services/privileges.py`, migration, `manage.py ensure_inventory_append_only`, `deploy/release.sh` | like `audit_log`: the model and queryset refuse update/delete/soft-delete; the database revokes `UPDATE, DELETE, TRUNCATE` from `DB_APP_ROLE` (proven with a temporary role and `SET ROLE`). |
| Services | `inventory/services/{locations,movements,balances,receiving,dashboard,usage,privileges,common}.py` | every write `@transaction.atomic`, stamped, versioned where the row is (locations), audited (`inventory.*`), cache-bumped (`inventory:locations`, `inventory:stock`), movements emit `inventory.movement_recorded`. |
| Views | `inventory/views/{locations,stock}.py` | `FlagRequiredMixin` + `BaseViewSet`: 404 `not_found` for everyone (anonymous included, before authentication) while the flag is off. |
| Event handler | `inventory/events.py` | `procurement.batch_committed` → PURCHASE movements into the receiving location (off by default). |
| Dashboard | `inventory/services/dashboard.py` + `core/dashboard.py` (shared: `register(module, flag=…)`) | `locations`, `stocked_components`, `negative_balances`, `movements_last_7_days`; the module is absent while the flag is off. |
| Catalog usage | `inventory/services/usage.py` | provider `inventory.stock`: a component with a non-zero balance somewhere cannot be deleted (409 `component_in_use`, catalog's guard). |
| Settings | `flarize/settings/base.py` (shared) | `INVENTORY_RECEIVING_LOCATION` (env; blank = receiving off); system check `inventory.E001` on its format. |

## Endpoints (`/api/v1/`, all 404 while `INVENTORY_STOCK` is off)

| Path | Permission |
|---|---|
| `inventory/locations/` list (`office`, `has_office`, `search` code/name, `ordering` code/name/created_at) / detail | `inventory.view` |
| `POST inventory/locations/`, `PATCH …/<uid>/` (`expected_version`), `DELETE …/<uid>/` (soft; 409 `location_has_stock`) | `inventory.edit` |
| `inventory/movements/` list (`component`, `location`, `direction`, `reason` (multi), `ref_type`, `ref_uid`, `date_from`, `date_to`, `search`, `ordering` at/created_at/qty; newest first) | `inventory.view` |
| `POST inventory/movements/` → 201 with `balance_after`, `negative_override` | `inventory.edit` |
| `GET inventory/balances/` (`component`, `location`, `office`, `category`, `state` = in_stock/zero/negative, `search`, `ordering` qty/last_movement_at) | `inventory.view` |

There is no movement detail, PATCH, PUT or DELETE route: the ledger is corrected by new movements only. Unmapped
methods are refused by the default-deny permission (403). Every list is page-number paginated (PLAN §3.1: cursor
pagination is for audit and raw punches) and in the OpenAPI schema with its error envelopes; the two enums are
`InventoryMovementDirectionEnum` / `InventoryMovementReasonEnum`.

## Decisions not spelled out in the PLAN

1. **Flag gate.** `core.flags.FlagRequiredMixin` answers 404 in `initial()`, before authentication and permission
   checks, so a disabled ledger is indistinguishable from an absent one (no 401/403 leaks it). The dashboard skips the
   module (shared change: `core.dashboard.register(module, flag=…)`, backward compatible — counters without a flag
   behave as before), the catalog usage provider reports nothing, and received batches are not booked. The schema is
   shipped either way (D-6 "schema shipped, UI later").
2. **Reasons fix the direction.** PURCHASE is IN; ISSUE_TO_PROJECT is OUT; RETURN goes both ways (back from a
   project or site = IN, back to the supplier = OUT); ADJUST both ways. Enforced by the service (400) and a DB check.
3. **References.** `ref_type` (`<app>.<model>`, lower case) and `ref_uid` come together (400 otherwise, DB check
   too). ISSUE_TO_PROJECT must name what it was issued to (a project uid — the projects app does not exist yet, and
   product master may not import sales, so the reference is not resolved). `procurement.batch_line` is reserved for
   the receipt of committed batches (400 on the staff API), so a hand-typed movement can never collide with the
   receipt's idempotency index.
4. **Negative stock.** An OUT that would take the balance of the component at that location below zero is refused
   with 409 `insufficient_stock` (`errors.qty = ["Available: …"]`) unless the reason is ADJUST — every ADJUST needs
   a note (it has no external reference; the note is its justification, 400 otherwise) — and the user holds
   `inventory.edit` (the service re-checks it; a system caller never overrides). The override is booked, returned as
   `negative_override: true`, and audited with `negative_override` and a note prefixed `negative-stock override`.
5. **Concurrency.** Every movement takes its location's row lock (`SELECT … FOR UPDATE`) before reading the balance,
   so two concurrent OUTs of the last units cannot both pass (a threaded test proves exactly one succeeds). Deleting a
   location takes the same lock, so no movement lands in a location being deleted.
6. **Balances.** `inventory_balance` is `SUM(IN) − SUM(OUT)` per (component, location) plus `qty_in`, `qty_out`,
   `movement_count`, `last_movement_at` — a plain view (no materialisation: the ledger is small and the grouping
   predicate is pushed into the (component, location) index). Only pairs with a movement have a row; the endpoint
   leaves out soft-deleted locations (all zero by construction) and keeps soft-deleted components while they hold
   stock. `state=zero` lists pairs that were fully consumed.
7. **Locations.** Codes are unique among live rows case-insensitively (as hr's codes, DV-50), 1-30 of
   `[A-Za-z0-9_.-]`. A location with any non-zero balance (negative included) cannot be deleted (409
   `location_has_stock`); its movements stay in the ledger. The HR office link: DV-64.
8. **Time.** `at` defaults to now, may be backdated, never in the future (5-minute skew tolerance), must carry a
   time zone. `by` is the acting user, or for receipts the batch committer; `created_by` is who recorded the row
   (NULL for the system).
9. **Components.** Any live component can move (DRAFT/RETIRED included: a retired panel still sits on the shelf);
   the receipt also books components soft-deleted after their batch was committed.
10. **Append-only enforcement.** The migration applies the REVOKE when `DB_APP_ROLE` is set and differs from the
    migrating role (as `audit.0001`); `ensure_inventory_append_only` re-applies it on every deploy (a role configured
    after the first migration, or a broad `GRANT … ON ALL TABLES`, is narrowed again). It fails closed for a role that
    does not exist, owns the table or inherits the owner's rights. Foreign-key `SET NULL` actions run with the owner's
    rights and are unaffected. The table keeps the `BaseModel` columns (PLAN marks it *append-only*, not *no base*):
    `deleted_at` stays NULL and `version` 1.

## `procurement.batch_committed` → stock (hand-over to the procurement package)

Off unless the flag is on **and** `INVENTORY_RECEIVING_LOCATION` names a live location code. Payload contract:

```json
{"batch_uid": "<uuid>", "number": "BATCH-2026-004", "committed_at": "2026-09-20T10:15:00+05:30",
 "committed_by_uid": "<uuid or null>", "imported": false,
 "lines": [{"line_uid": "<uuid>", "component_uid": "<uuid>", "qty": "10.000"}]}
```

* each line with `qty > 0` → IN / PURCHASE at the receiving location, `ref_type = procurement.batch_line`,
  `ref_uid = line_uid` (`uid` is accepted as an alias), `at = committed_at` (else the event time), `by` = the
  committer, note `Received with <number>`; zero/negative lines are skipped (reversals are booked by staff);
* `imported: true` (historic batches written by the Flarize importer) books nothing — history predates the ledger;
* idempotent: lines already booked are skipped; a racing second delivery hits the partial unique index and is skipped;
* a missing `lines` list, a malformed uid/quantity, an unknown component or a configured location that does not exist
  raises `ReceiptError`: the outbox retries with backoff and parks the event (SystemException, `/healthz` degraded).
  Fix the cause, then `manage.py drain_outbox --requeue-parked`.

## Events emitted

`inventory.movement_recorded` (aggregate `inventory.movement`): `{movement_uid, component_uid, location_uid,
direction, reason, qty, balance_after, ref_type, ref_uid}` — for later consumers (projects), DV-63.

## Legacy sources and import contract

**No legacy source feeds these tables**, so there is no `inventory/services/legacy_import.py` and no committed
fixture. Checked read-only:

| Source | What was checked | Result |
|---|---|---|
| main backend `legacy_goldenapp` (UAT dump) | every table of `information_schema.tables`; columns named like stock/qty/quantity/warehouse | 51 tables, none holds stock; the `qty` columns are BOM template quantities (`bom_bomslot`, `bom_bomfixeditem`, `bom_structuretemplateitem`) owned by the bom package |
| CMS `legacy_blog_cms` | tables named like stock/inventory/warehouse/location | none |
| Flarize data files (`/home/user/flarize-main/flarize/data/*.json`) | `stock`/`inventory`/`warehouse` | only a cost-config comment ("supplier->warehouse only"); procurement batches carry purchase quantities, not stock (imported by the procurement package, flagged `imported`) |
| PLAN §7.1–§7.5 | migration tables | no row targets `inventory_*`; D-6: "schema shipped, UI later" |

Parity evidence: none applies — no legacy endpoint served stock (the website API inventory,
`/home/user/platform-reference/website-api-inventory.md`, has no stock call).

## Tests

`inventory/tests/`: `test_locations_api.py`, `test_movements_api.py`, `test_balances_api.py` (flag 404 for anonymous
and authorised callers, 401, 403, scope `all`, validation envelopes, `stale_version`, every 409, filters, ordering,
`django_assert_max_num_queries` on every list), `test_services.py` (every DomainError called directly, the override
permission, a threaded concurrency test, catalog usage guard, the settings check), `test_receiving.py` (contract,
idempotency, racing insert, switches, through the real outbox), `test_append_only.py` (model/queryset guards,
REVOKE under `SET ROLE`, migration step, command, deploy step), `test_dashboard.py`.

## Hand-over notes

* **procurement**: emit `procurement.batch_committed` with the payload above inside the commit transaction
  (`dedup_key` = batch uid); mark importer-written batches `imported: true`.
* **projects**: issue stock with `POST inventory/movements/` (`reason=ISSUE_TO_PROJECT`, `ref_type=projects.project`,
  `ref_uid=<project uid>`) or follow `inventory.movement_recorded`; never import inventory services.
* **ops**: set `INVENTORY_RECEIVING_LOCATION` only after creating that location; `ensure_inventory_append_only` runs
  in `deploy/release.sh` after `migrate` (owner role).
