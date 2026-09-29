# WP pricing-procurement — prices, commercial configuration, releases, procurement batches

Work package *pricing-procurement* builds the `pricing` and `procurement` apps of PLAN §2.3 / §2.4 / §3.4 (Pricing,
Procurement) on top of the foundation and the wave-1 packages (catalog, engines), the release model of PLAN §1.2
(`PriceRelease`), the event of §3.5 (`pricing.release_published`) and the importers of §7.3 (`bom_catalogitem` prices,
`bom_globalcosts`, `bom_marketrate`, `bom_offer`) and §7.4 (Flarize `catalog.json` costs/installation matrix/transport
/office expense/market rates/offers, `procurement-price-master.json`, `procurement-state.json`, `commercial-history.json`,
`project-rate-card.json`, `cost-config.json`, `quotation-policy.json`, plus the four engine configurations and the
Purchase Agreement `kseb` list). Deviations: DV-62 … DV-70.

## What exists

| Area | Where | Notes |
|---|---|---|
| Tables | `pricing/models/`, `procurement/models/` | `pricing_price` (append-only, DB-enforced), `pricing_current_price` (Postgres VIEW, unmanaged model), `pricing_market_rate_set`, `pricing_market_rate`, `pricing_swap_delta`, `pricing_roof_addon`, `pricing_cost_config`, `pricing_installation_matrix`, `pricing_statutory_fee`, `pricing_offer`, `pricing_offer_transition` *(no base)*, `pricing_validity_policy`, `pricing_release`, `pricing_release_line` *(no base, PK (release, component))*; `procurement_supplier`, `procurement_batch`, `procurement_batch_line` (`line_value` generated), `procurement_batch_charge`. Every enum has a `CHECK`; every lifecycle invariant is a partial unique index (below). |
| Migrations | `pricing/migrations/0001…0003`, `procurement/migrations/0001` | `pricing` ↔ `procurement` reference each other (`pricing_price.supplier`, `procurement_batch_line.price_row`/`landed_row`), so pricing's FKs come in `0002_relations`; `0003` creates the view and the append-only trigger with `RunSQL`. |
| Staff API | `pricing/views/`, `procurement/views/`, `*/urls.py` | PLAN §3.4 Pricing and Procurement (table below). No public endpoints: the website reads prices through the catalog's `products/` payloads (price provider). |
| Services | `pricing/services/`: `prices`, `cost_config`, `masters` (installation matrix, statutory fees), `market_rates`, `offers`, `validity`, `releases`, `provider`, `registrations`, `import_support`, `legacy_import`; `procurement/services/`: `suppliers`, `batches`, `allocation` (preview/commit/reverse), `price_master`, `registrations`, `legacy_import` | all writes `@transaction.atomic`, stamped, versioned, audited, cache/outbox where they matter. |
| Engines used | `engines.offers` (lifecycle, validation, applicable offer, offer amount), `engines.cost.allocate_landed` (landed cost), `engines.money` (GST regime check, `round_money`), `engines.rate_card` (rate-card validation), `engines.energy/savings/subsidy/finance` validators | nothing re-implemented. |
| Registrations | `pricing/services/registrations.py`, `procurement/services/registrations.py` | catalog price provider; catalog usage providers `pricing.swap_deltas`, `pricing.current_release`, `procurement.draft_batches`; dashboard counters `pricing`, `offers`, `procurement`. |
| Beat | `flarize/settings/base.py` `CELERY_BEAT_SCHEDULE["pricing.expire_offers"]` (daily 00:11 IST) | `pricing.tasks.expire_offers`. |
| Tests | `pricing/tests/`, `procurement/tests/` | API (401/403/scope/validation/stale/happy/N+1), services, DB invariants, legacy import, parity; fixtures in `*/tests/fixtures/legacy/`. |

### Endpoints

| Surface | Path | Permission |
|---|---|---|
| staff | `GET pricing/prices/` (filters `component`, `sku`, `kind`, `source`, `current`, `effective_on`, `supplier`, `version_key`; search), `GET pricing/prices/<uid>/` | `pricing.view`; PURCHASE/LANDED rows only with `pricing_internal.view` |
| staff | `POST pricing/prices/` — manual LIST or LANDED row, closes the current one (`expected_current_uid` → 409 `stale_version`) | `pricing.edit` (+ `pricing_internal.view` for LANDED) |
| staff | `GET pricing/current/` (the view; `component`, `sku`, `kind`, `category`) | `pricing.view` (internal kinds as above) |
| staff | `GET/PUT pricing/cost-config/` (key registry + current rows / entries `{key, value, current_uid?}`, `effective_from`, `note`), `GET pricing/cost-config/history/?key=` | `pricing.view` / `pricing.edit` (+ `pricing_internal.view` to write margin data or the cost-engine document) |
| staff | `pricing/installation-matrix/`, `pricing/statutory-fees/` CRUD | `pricing.view` · `pricing.edit` (create/edit/delete) |
| staff | `pricing/market-rate-sets/` CRUD; `GET/PUT …/<uid>/rates/`, `…/swap-deltas/`, `…/roof-addons/` (bulk replace, DRAFT only); `POST …/<uid>/activate/` | `market_rates.view` · `market_rates.edit` · `market_rates.publish` (activate) |
| staff | `pricing/offers/` CRUD; `POST …/approve/`, `…/activate/`, `…/pause/`, `…/archive/` | `offers.view` · `create` · `edit` · `approve` · `publish` (activate, pause) · `archive` (archive, DELETE of a DRAFT) |
| staff | `GET/PUT pricing/validity-policy/` | `pricing.view` / `pricing.edit` |
| staff | `GET pricing/releases/`, `GET pricing/releases/<number>/`, `GET pricing/releases/current/`, `POST pricing/releases/preview/`, `POST pricing/releases/` (publish), `GET pricing/releases/current/diff/?against=` | `pricing.view` (list, detail, preview, diff) · `pricing.publish` (publish); landed/margin data only with `pricing_internal.view` |
| staff | `procurement/suppliers/` CRUD | `procurement.view` · `create` · `edit` (edit and delete) |
| staff | `procurement/batches/` CRUD (DRAFT only; DELETE cancels); `GET/PUT …/lines/`, `…/charges/`; `POST …/preview-allocation/`; `POST …/commit/`; `POST …/reverse/` | `procurement.view` (reads, preview) · `create` · `edit` · `commit` (commit, reverse); landed fields only with `pricing_internal.view` |
| staff | `GET procurement/price-master/` (`search`, `category`, `supplier_uid`) | `procurement.view`; landed fields only with `pricing_internal.view` |
| staff | `GET dashboard/` | counters for `pricing`, `offers`, `procurement` |

Market rate sets and batches accept PUT only on their child collections: PUT on the set or batch itself is 405 (PATCH
edits it) and is not in the schema. The enum components the two apps add are named in
`SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]` (`PriceKindEnum`, `PricingSystemTypeEnum`, `ProcurementBatchStatusEnum`, …;
`TierEnum` is the BASE/VALUE/PREMIUM set shared with catalog, `TypeEnum` stays the blog attribute-slot type).

## Decisions not spelled out in the PLAN

1. **Append-only prices, enforced twice.** `pricing.services.prices.write_price` is the only writer: in the caller's
   transaction it locks the open row of the same (component, kind), refuses a newer row that starts earlier
   (`effective_from_before_current`) or in the future (`effective_from_in_future`), closes it (`effective_to` = the new
   `effective_from`) and inserts the new one. The partial unique index `(component, kind) WHERE effective_to IS NULL AND
   deleted_at IS NULL` keeps one current row per kind (a racing writer gets 409 `price_conflict`); `(kind, version_key)`
   is unique where set, so a batch line can never be committed twice (`price_version_exists`). A trigger
   (`pricing_price_append_only`) refuses every DELETE and every UPDATE except closing an open row, stamping
   `updated_*`/`version`, or nulling an attribution FK. `LIST` never comes from `MARKUP` (DB check + `list_markup_forbidden`).
2. **`pricing_current_price` is a view** (`DISTINCT ON (component_id, kind)` over open rows, newest `effective_from`
   first) mapped by the unmanaged `CurrentPrice`; authoring screens and releases read it.
3. **`pricing_internal`.** Callers without `pricing_internal.view` never see PURCHASE/LANDED rows (filtered out of
   `prices/` and `current/`), landed fields of batch lines, allocation previews, the price master and release payloads
   (`landed_cost`, `landed_effective_from`, `landed_version_key`), nor margin configuration (`target_gross_margin_by_tier`
   and the `margin` section of `cost_engine.config`, shown as `hidden`). Writing a LANDED row or margin data needs the
   permission as well (403 `pricing_internal_required`): nobody writes a cost they may not read.
4. **Cost configuration is a closed key registry** (`pricing.services.cost_config.KEYS`): each key has a type
   (money ≥ 0 with ≤ 2 decimals, whole number, fraction 0–1, or a validated document) and a group. Unknown keys → 400
   `unknown_config_key`. A PUT appends a row only for changed values and closes the previous one; `current_uid`
   guards concurrent edits. Percentages are fractions (`structure_repair_pct` 10 % → `0.1`, GST 70/30 → `0.7`/`0.3`).
   The Flarize cost engine and rate card are stored as whole documents (`cost_engine.config`, `rate_card`): the engines
   read them as they are and their own versioning (rate-card record histories) stays inside.
5. **Market rates** keep the source's size keys: `5sp`/`5tp` are two rows (5 kW, phase 1P/3P); upgrade paths carry
   `from_size_key`; Flarize future-ready keys (`_up<size>`) and extra suffixes (`_diffBase`) have columns; `sort_order`
   keeps the key order of `size_rates`; `0` means "not set" (both sources read 0 as missing; the publish report lists
   it). `rate_key()` rebuilds the Flarize key (`ongrid_value_up5sp`, `upgrade_3_5`); the release payload carries both the
   rows and `market_rates_by_key` (the `marketRates` shape `engines.pack_pricing` reads).
6. **Market rate sets**: created DRAFT (optionally `copy_from` another set), edited only while DRAFT
   (`market_rate_set_not_draft`), children replaced by bulk PUT (upsert by natural key, left-out rows soft-deleted),
   every child write bumps the set's `version`. `activate/` (DRAFT or RETIRED → ACTIVE, the previous ACTIVE → RETIRED in
   the same transaction; `market_rate_set_empty` refuses a set without rates); exactly one ACTIVE set is a partial
   unique index. The ACTIVE set and sets a release references cannot be deleted.
7. **Offers** use `engines.offers` (DV-19): validation, the transition table, the edit rule (DRAFT/APPROVED only; a
   content change bumps `content_version` and sends APPROVED back to DRAFT), auto-expiry and the applicable-offer
   choice. PLAN's PAUSED is added (ACTIVE ⇄ PAUSED; PAUSED → EXPIRED/ARCHIVED). Activation is refused after `ends_on`
   (`offer_already_ended`); only DRAFT offers are deleted. Every step: a transition row, an audit row,
   `pricing.offer_status_changed`. `pricing.tasks.expire_offers` (Beat, daily) expires ACTIVE/PAUSED offers past
   `ends_on`. `applicable_offer()` / `offer_amount()` are the quotation-side entry points (D-4).
8. **Validity** (`pricing_validity_policy`): one DEFAULT row per key plus Flarize's effective windows (ACTIVE window
   containing the instant, latest `effective_from` wins, ties by `policy_id` descending — `resolveActivePolicy`), else
   the default days; nothing configured → 409 `policy_not_configured`. A PUT upserts windows by `policy_id` and
   deactivates left-out ones (never deletes: issued quotations may cite them).
9. **Publish report.** `preview()` builds the release exactly as `publish()` would. BLOCK: no ACTIVE market rate set,
   GST keys missing or inconsistent (`engines.money.resolve_gst_regime`), nothing changed since the current release
   (`RELEASE_UNCHANGED`, by payload hash). WARN: ACTIVE/DEPRECATED components without a LIST price or with a zero LIST
   price, ACTIVE components without a landed cost, market-rate cells not set, swap deltas naming retired/deleted
   components, website-BOM cost keys missing, empty installation matrix, no validity policy. INFO: retired components
   left out. The report also counts added/removed/changed entries per payload section against the current release.
10. **Publish** (one transaction): `expected_current_number` (409 `stale_version` when another release was published),
    the report must have no BLOCK (409 `publish_blocked`, the BLOCK items in `errors.report`), the previous release
    becomes SUPERSEDED, the next number comes from `core.sequences` (`PRICE_RELEASE`), lines are bulk-inserted, the
    payload (canonical JSON, Decimals as strings) and its SHA-256 stored with the report, the `pricing` cache namespace
    bumped, `pricing.release_published` emitted (`{release_uid, number, previous_number, market_rate_set_uid,
    payload_sha256}`, dedup key per number). Lines cover every non-retired component with a current LIST or LANDED
    price; `gst_rate` is the component's effective rate.
11. **Website prices** (catalog price provider, one query per response): the current release line's list price made
    customer-facing — GST added unless the list price includes it, whole rupees (`money.round_money`) — as both
    `min`/`max` and the label (`₹14,264`). No positive list price → no price (the profile's own label shows). Catalog's
    public responses are cached under `pricing`, which publishing bumps.
12. **Batches.** Numbers `BATCH-<YYYY>-<nnn>` (`core.sequences`, per year). DRAFT only is editable; lines/charges by bulk
    PUT (selectable components only — `catalog.assert_selectable` —, one line per component, qty > 0, price ≥ 0; charge
    amounts ≥ 0), totals recomputed on every change. DELETE cancels a DRAFT (status CANCELLED; the number stays).
13. **Preview/commit** port Flarize's allocation review (`procurementWorkspace.allocationPreview`): blockers
    `batch_has_no_lines`, `charges_not_entered` (an absent delivery charge is not zero — enter FREIGHT 0),
    `other_charges_not_reconciled`, `component_retired`, `allocation_invalid` (engine refusal), `allocation_not_reconciled`.
    Commit also needs an invoice/PO number (`invoice_reference_missing`), a reason (`reason_required`) and a past or
    present `effective_from`. Per line: PURCHASE = unit purchase price, LANDED = `landedUnitCost` (whole rupees, the
    Flarize rule), `version_key` `<number>::<sku>` (Flarize `versionIdFor`), note `Landed = <formula>`, the supplier on
    both rows; the line keeps `landed_unit_cost`, `allocated_charges`, `allocation_pct` and links both rows.
    `procurement.batch_committed` is emitted.
14. **Reversal** (PLAN "corrections are a reversing batch"; the engine cannot allocate negative quantities): a new batch
    (COMMITTED at once, `reverses` → the original; one reversal per batch — partial unique index) mirrors lines and
    charges with negative quantities/amounts. Where the original's PURCHASE/LANDED row is still current it is replaced
    by a new row with the amount of the row it had superseded (`version_key` `<reversal>::<sku>`), or just closed when
    there was none; prices a later batch replaced are left alone (`superseded_since`). `procurement.batch_reversed`.
15. **Price master** (`procurement/price-master/`) is derived from the current rows (Flarize `priceMasterView`: no second
    table; a component without a committed version is absent, never a zero), with the batch that wrote each row.
16. **Usage guard.** A component referenced by a live swap delta of a non-retired set, the current release, or a DRAFT
    batch cannot be deleted (catalog's 409 `component_in_use`); price history alone never blocks (retire instead).

## Legacy mapping

### Main backend (`GoldenApp`) — every column has a home

`bom_globalcosts` (singleton; the calculator reads `.first()`, extra rows are reported `extra_global_costs_row`) →
`pricing_cost_config`, one row per column, `source_ref` `BACKEND:bom_globalcosts`, `created_at` = the row's `updated_at`:

| Legacy column | Cost config key | Conversion |
|---|---|---|
| `id` | `core_legacy_map` (`bom_globalcosts`, `<id>:<column>`) | |
| `install_rate` | `install_rate` | money |
| `service_rate_year` | `service_rate_year` | money |
| `service_years` | `service_years` | integer |
| `transport_rate` | `transport_rate_per_km` | money (₹/km over the whole distance) |
| `default_dist_km` | `transport_base_km` | integer |
| *(calculator constant 35 ₹/extra km)* | `transport_extra_rate_per_km` | from Flarize `transportConfig.costPerKm` (35) |
| `miscellaneous` | `miscellaneous` | money |
| `office` | `office_per_project` | money |
| `elevated_structure_rate` | `elevated_structure_rate` | money |
| `sheet_structure_rate` | `sheet_structure_rate` | money |
| `gp_rate_per_kg` | `gp_rate_per_kg` | money |
| `gi_rate_per_kg` | `gi_rate_per_kg` | money |
| `structure_labor` | `structure_labor` | money |
| `structure_repair_pct` (10.0000 = 10 %) | `structure_repair_pct` | fraction (÷ 100: `0.1`) |
| `updated_at` | `created_at`/`updated_at` of the rows | |

`bom_marketrate` → `pricing_market_rate` in the DRAFT set "Imported <date>" (shared with the Flarize import):

| Legacy column | Home |
|---|---|
| `id` | `core_legacy_map` (`bom_marketrate`, `<id>:<size key>`) — one entry per exploded cell |
| `system_type` ongrid/hybrid/upgrade | `system_type` ONGRID/HYBRID/UPGRADE |
| `tier` base/value/premium, `''` (upgrade) | `tier` BASE/VALUE/PREMIUM, `''` |
| `bat_config` `''`/`0`/`1`/`2` | `battery_config` (same values) |
| `from_size` (upgrade) | `from_size_key` (+ `from_size_kw`) |
| `to_size` (upgrade) | `size_key` (+ `size_kw`) of the upgrade row |
| `size_rates` keys (`3`, `5sp`, `5tp`, `6`, `8`, `10`; upgrade `rate`) | one row each: `size_key` exactly, `size_kw` (5), `phase` (`sp` → 1P, `tp` → 3P) |
| `size_rates` values | `customer_price_incl_gst` (0 = not set; a null value is stored as 0 and reported `null_rate`) |
| key order of `size_rates` | `sort_order` |
| `updated_at` | `created_at`/`updated_at` of the cells |

An empty `size_rates` has no cell to store (reported `empty_size_rates`; the calculator reads 0 for every size either way).

`bom_offer` → `pricing_offer` (+ one transition "Imported from BACKEND bom_offer"):

| Legacy column | Home |
|---|---|
| `id` | `core_legacy_map` (`bom_offer`) |
| `offer_id` (≤ 50) | `code` (≤ 50, DV-66) |
| `name` | `name` |
| `offer_type` flat/percent | `type` FLAT/PERCENT |
| `value` | `value` |
| `applies_to` all/ongrid/hybrid/upgrade | `applies_to_system` ALL/ONGRID/HYBRID/UPGRADE |
| `applies_to_tier` all/base/value/premium | `applies_to_tier` ALL/BASE/VALUE/PREMIUM |
| `start_date`, `end_date` | `starts_on`, `ends_on` |
| `active` | `status` ACTIVE (true) / ARCHIVED (false) — PLAN §7.3; the calculator's date filter still applies to ACTIVE |
| `created_at` | `created_at` |

`bom_catalogitem.price` / `per_watt` (via the catalog importer's returned price rows and `core_legacy_map` `BACKEND
bom_catalogitem → catalog_component`) → the component's LIST `pricing_price` row: `amount`, `per_watt` (DV-62),
`source` IMPORT, `source_ref` `BACKEND:bom_catalogitem#<id>`, `effective_from` = import date. Website
`batteries.battery_price` → LIST with `gst_inclusive` (a customer price).

**The website BOM quote engine** (bom package) reads these rows as the legacy calculator read its models:
`GlobalCosts` columns from the cost-config keys above, `MarketRate` lookups by (system, tier, battery band, size key)
or (from, to) for upgrades, active offers by status + dates, item prices from the current LIST rows.
`pricing/tests/legacy_rebuild.py` is that mapping in executable form (the `/legacy/bom/...` shim can reuse it).

### Flarize

| Source | Home |
|---|---|
| `catalog.json` `costs.*` | cost-config keys (same table as above; `office` → `office_per_project`, `structureRepairPct` ÷ 100) |
| `catalog.json` `transportConfig.baseDistanceKm` / `costPerKm` | `transport_base_km` / `transport_extra_rate_per_km` (a disagreement with `costs.defaultDistKm` is reported `flarize_conflict`, the first value kept) |
| `catalog.json` `officeExpense.monthlyExpense` / `expectedProjectsPerMonth` | `office_expense_monthly` / `expected_projects_per_month` |
| `catalog.json` `installationMatrix.<size>.<flat|sheet|elevated>` | `pricing_installation_matrix` (`size_kw`, `phase` from the key, `installation_type`) |
| `catalog.json` `marketRates.<key>.<size>` | `pricing_market_rate` in the imported set; key `<sys>_<tier>[_<bat>][_up<size>][_<variant>]` or `upgrade_<from>_<to>` parsed into the columns |
| `catalog.json` `offers[]` | `pricing_offer`: `id` → `code`, `type` flat/percentage → FLAT/PERCENT, `appliesTo`/`appliesToTier`/`appliesToSize` (`all` → blank), `startDate`/`endDate`, `description`, `status`, `offerVersion` → `content_version`, `createdAt`; `transitions[]` → `pricing_offer_transition` (`changedBy` → the imported user, else `by_label`) |
| `catalog.json` `_legacyMarkers` | ignored (PLAN §7.4) |
| `cost-config.json` | `cost_engine.config` (whole document); `gst.goodsValuationPct/goodsRatePct/serviceValuationPct/serviceRatePct` → `gst_goods_share`/`gst_goods_rate`/`gst_services_share`/`gst_services_rate` (fractions); `margin.tiers.<TIER>.targetMarginPct/minimumMarginPct` → `target_gross_margin_by_tier` (fractions) |
| `project-rate-card.json` | `rate_card` (whole document; its record histories stay inside) |
| `quotation-policy.json` | `validity.defaultDays/version/updatedAt/updatedBy/note` → the QUOTATION DEFAULT row; `policies[]` → WINDOW rows (`policyId`, `validityDays`, `effectiveFrom/To`, `status`, `version`, `createdAt/By`, `note`) |
| `energy-`, `savings-`, `subsidy-`, `finance-config.json` | `energy.config`, `savings.config`, `subsidy.config`, `finance.config` (validated by the engines' validators) |
| `procurement-state.json` `suppliers` | `procurement_supplier` (`supplierId` → `code`, `name`, `reference`, `createdAt/By`) |
| `procurement-state.json` `batches` | `procurement_batch`: `batchId` → `number`, `supplierId`, `batchReference` → `invoice_no`, `batchDate` → `invoice_date`, `status`, `deliveryCost` → FREIGHT charge (null = not entered: no charge), `otherChargesDeclared` → `other_charges_declared`, `isSeed`/`seedNote` → `is_seed`/`note`, `effectiveFrom`, `changeReason` → `commit_reason`, `committedAt/By`, `createdAt/By`; `lines[]` → `procurement_batch_line` (`componentId` via the catalog map, `quantity`, `purchaseUnitPrice`; line-level `otherProcurementCharges` have no home and are reported `line_charges_not_supported` — none in the data) |
| `procurement-state.json` `historyStore` + `commercial-history.json` (identical today; compared version by version, `history_differs`) | per `PROCUREMENT_PRICE` version: a PURCHASE and a LANDED `pricing_price` row, `version_key` = `versionId`, `effective_from` = `effectiveFrom` (a superseded version is closed at its successor's date), `created_at`/`created_by` = `changedAt`/`changedBy`, supplier, note `Landed = <formula>`, linked to the batch line; `allocatedDeliveryCost`/`allocationPct` → the line |
| `procurement-price-master.json` | verification (PLAN §7.6 #7): `price_master_differs` when a record disagrees with the current rows; a record without history is imported from the master (`master_without_history`) |
| `procurement-state.json` `audit`, `_phaseDMetadata` | not migrated (the platform audit log starts at the import) |
| Purchase Agreement `kseb` list (`KSEB_FEES` options / Upstash `"label|fee"`) | `pricing_statutory_fee` KSEB_REGISTRATION, `capacity_kw_max` from the label (`3 KW`) |

**D-2 (Flarize wins, every difference reported).** Both sources map onto the row they describe through
`core_legacy_map`; the importers read that ownership, so in either order the Flarize value stays current and each
difference is a `d2_flarize_wins` warning with both values: LIST prices (`import_prices`; PLAN §7.6 #7 — the
`catalog.json` price, or the BOM price where Flarize has none), global costs (UAT: `gi_rate_per_kg` 130 vs 98.3,
`structure_labor` 1500 vs 3000), market-rate cells (UAT: `ongrid_base/3` 202539.3 vs 0, `ongrid_value/3` 230000 vs
228000, `hybrid_value_2/3` 0 vs 549901.8) and offers with the same code (different codes with the same name are both
kept and reported `possible_duplicate_offer` — UAT: "Monsoon 2026 Discount"). A current MANUAL/BATCH price (platform
data) is never replaced by an import (`platform_price_kept`). When the main-backend import ran first, the value
Flarize replaces stays in history (closed row); the current state is the same in both orders (tested).

## Parity evidence

* `pricing/tests/test_legacy_parity.py`: the UAT `bom_globalcosts`, `bom_marketrate` (16 rows, 58 cells) and
  `bom_offer` rows (committed fixtures exported read-only from `legacy_goldenapp`) are imported and rebuilt from the
  platform tables by `pricing/tests/legacy_rebuild.py`: **every column equal** (decimals, `size_rates` values and key
  order incl. `5sp`/`5tp`, `bat_config`, upgrade `from_size`/`to_size`, dates, `active`, timestamps). The legacy
  `BomCalculator._lookup_market_rate` / `_lookup_upgrade_market_rate` and the view's offer filter +
  `_applicable_offers` are replayed on the rebuilt and the original rows for 132 system/tier/battery/size combinations,
  5 upgrade paths and 27 system/tier/day combinations: identical. All 257 `bom_catalogitem` prices and per-watt values
  come back from the current LIST rows.
* `pricing/tests/test_legacy_import.py`: D-2 in both orders (same current LIST prices, cost config, market-rate cells;
  the same differences reported), idempotent re-runs, dry runs.
* `procurement/tests/test_legacy_import.py`: after importing the Flarize procurement state, commercial history and
  price master, every one of the 257 `landedUnitCost`/`purchasePrice` equals the current LANDED/PURCHASE row with the
  same version key (PLAN §7.6 #7); history order is kept (SEED-004 closed at SEED-005's date). Committing a platform
  batch with the SEED-005 lines and its ₹15,000 delivery charge through `engines.cost.allocate_landed` reproduces all
  257 landed unit costs and allocated delivery amounts Flarize recorded (sum ₹15,000).
* `pricing/tests/test_releases_api.py::TestWebsitePrices`: a published release changes the public product payload
  (`price`, `price_range_label`) at once (cache namespace bump).

## For later packages

* **bom** (website quote engine, `/legacy/bom/api/calculate/`): read the cost-config keys, market rates of the ACTIVE set
  (or of a release payload), ACTIVE offers and current LIST prices as mapped above; `legacy_rebuild.py` shows the
  exact legacy shapes. `transport_extra_rate_per_km` replaces the calculator's constant 35.
* **packs**: build prices from the current release (`pricing.services.releases.current_release()`: payload
  `components`, `market_rates_by_key`, `swap_deltas`, `roof_addons`, `cost_config`, `installation_matrix`, `gst`);
  react to `pricing.release_published` (mark stale drafts). The other W8 data blockers of PLAN §7 / D-8 (`bt1`
  battery rating, the PBC-M-003 controller) are pack/engineering findings and belong in the PackRelease publish
  report; the PriceRelease report covers what pricing owns (market rates not set, LIST/landed gaps, GST, …).
* **quotations / calculators**: `pricing.services.offers.applicable_offer(system_type=…, tier=…, size_key=…, on=…)` +
  `offer_amount(offer, list_price)`; `pricing.services.validity.resolve(at)` freezes the validity days.
* **blog/website revalidation**: `pricing.release_published` should revalidate `/solar-comparison` and
  `/emi-calculator` (PLAN §3.5) — the blog package owns that handler.
* **migrations_tools**: call, in this order after the catalog import, `import_prices(catalog result prices)` (every
  catalog import), `import_bom_global_costs`, `import_bom_market_rates`, `import_bom_offers`, `import_flarize_pricing`,
  `import_flarize_documents`, `import_pa_kseb_fees`, then `procurement.services.legacy_import.import_flarize_procurement`
  (after the users import so `createdBy`/`changedBy` resolve); each takes `dry_run=` and returns counts + violations.
