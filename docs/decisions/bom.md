# WP bom — BOM configuration and the website quote engine

Work package *bom* builds the `bom` app of PLAN §2.5 (`bom_*`), §3.4 (BOM & packs: `bom/*`), the importers of §7.3
(`bom_bomtemplate`, `bom_bomslot`, `bom_bomfixeditem`, `bom_structuretemplate(+item)`, `bom_tubeweight`) and §7.4
(Flarize `bomTemplates`, `structureTemplates`, `tubeWeights`, `packageProfiles`), and the website quote engine that
replaces the legacy `POST /bom/api/calculate/` (DV-4) on the platform tables. Deviations: DV-87 … DV-90.

## What exists

| Area | Where | Notes |
|---|---|---|
| Tables | `bom/models/` | `bom_template`, `bom_slot`, `bom_fixed_item`, `bom_structure_template`, `bom_structure_template_item`, `bom_tube_weight`, `bom_package_profile` (DV-87). Every enum has a `CHECK`; live-row partial unique indexes: template `system_type`, slot `(template, key)`, structure template `slug`, tube weight `tube_size`, profile `key`. Checks: fractions in `[0, 1]`, non-negative prices/weights/quantities, a fixed item has a quantity (`qty` or `qty_rule`) and a price source (`unit_price` or a component). |
| Documents | `bom/schemas.py` | JSON Schema 2020-12: `qty_rule` (8 rule types), fixed-item `condition`, template `sizes` / `three_phase_sizes` / `tiers` / `battery_configs`. Validated in the services (400 `validation_error` on the field, one message per violation). |
| Services | `bom/services/` | `masters` (all CRUD writes), `qty_rules` (pure evaluation), `website_quote` (the quote engine), `build` (`bom/build/` through `engines.bom_builder`), `legacy_import`, `registrations` (catalog usage providers), `common`. |
| Staff API | `bom/views/masters.py`, `bom/views/quote.py` | table below. |
| Public API | `bom/views/quote.py` | `POST /api/public/v1/bom/quote/`. |
| Tests | `bom/tests/` | CRUD (401/403/scope/validation/stale/happy/N+1 for every resource), rules, legacy import, `qty_rule` equivalence, quote calculator branches, endpoint shape/throttle/no-store, build, the 1,077-case golden replay. |

### Endpoints

| Surface | Path | Permission |
|---|---|---|
| staff | `bom/templates/`, `bom/slots/`, `bom/fixed-items/`, `bom/structure-templates/`, `bom/structure-items/`, `bom/tube-weights/`, `bom/package-profiles/` — list/detail · create · PATCH (`expected_version`) · DELETE (soft) | `bom.view` · `bom.edit` (create, edit, delete) |
| staff | `POST bom/build/` (`system_type` ONGRID/HYBRID, `size`, `tier`, `phase?`, `battery_quantity?`, `future_system_size?`, `structure?`, `selections?`) | `bom.view` (a dry build writes nothing) |
| public | `POST bom/quote/` — the legacy `/bom/api/calculate/` body and response | anonymous, throttle `public_write`, `Cache-Control: no-store` |

List filters: slots `template` (uid), `category` (slug); fixed items `template`, `section`; structure items `template`,
`item_type`; templates `system_type`, `is_active`; `search` and `ordering` on every list.

## Decisions not spelled out in the PLAN

1. **`qty_rule` is the legacy quantity logic as data.** Rule types: `size_table` (`qty`, `bat_qty`, `premium_qty`,
   `premium_bat_qty` keyed by size key / battery band; `bat_lookup` `if_battery_config` for slots — the legacy
   `get_qty` read band tables only with a band — or `always` for fixed items; an *empty* `premium_qty` still means
   "0 for premium" because `get_qty` tested `is not None`), `fixed`, `new_panels`, `upgrade_path` (path keys `3_5`,
   scaled from the closest known path otherwise), `kw_interpolated` (`getStructureQty`), and the PLAN's `per_kw`,
   `per_panel`, `by_phase`. `bom/tests/test_qty_rules.py` proves, for every UAT slot, fixed item and structure item,
   that the imported rule gives exactly (value **and** number type) what the legacy code computed over 11 size keys
   (offered and not offered), 4 tiers (incl. blank) and 6 battery bands (incl. blank, `9`, `None`), and 9 upgrade paths.
2. **Fixed items carry their own reference price** (`unit_price`); a fixed item linked to a component without one is
   priced at the component's current LIST price. The legacy standard calculator always used the row's price; the
   upgrade calculator always used the linked catalog item's price (its rows hold `0.00`), so the importer stores
   `unit_price = NULL` for linked upgrade rows. A fixed item's `condition` (sizes, phases, tiers, bands, kW range)
   filters it; imported rows have `{}` (always).
3. **Slots are keyed by the category slug** (every legacy and Flarize template has one slot per category); `sort_order`
   is the legacy `pos` (the line's `pos` in the quote).
4. **Structure templates use the PLAN slugs** `flat_roof`, `elevated`, `sheet_roof` (legacy `flatRoof`, `sheetRoof`);
   the quote maps the request's `structure_type` onto them. `labour_rate_key` names the cost-config key of the roof
   type (`elevated_structure_rate`, `sheet_structure_rate`). A tube is priced by its `weight_kg` (one tube length) ×
   the tier's rate per kg (GP for base, GI otherwise), a fixed structure item by `unit_price`.
5. **The website quote engine** (`bom/services/website_quote.py`) is the legacy `BomCalculator` + `BomCalculateView`
   operation for operation on these reads:

   | Legacy read | Platform read |
   |---|---|
   | `GlobalCosts.objects.first()` | current `pricing_cost_config` rows (`install_rate`, `service_rate_year`, `service_years`, `transport_rate_per_km`, `transport_base_km`, `miscellaneous`, `office_per_project`, `gp_rate_per_kg`, `gi_rate_per_kg`, `structure_labor`, `structure_repair_pct` × 100); the constant 35 ₹/extra km = `transport_extra_rate_per_km` (35 when unset) |
   | `BomTemplate` of the system type | the live, active `bom_template` |
   | `slot.get_qty(...)` | `qty_rules.evaluate(slot.qty_rule, …)` |
   | `category.items.all()` (heap order) + `item_tiers` | live ACTIVE/DEPRECATED components of the slot's category, `created_at, id` order (the importers keep the source timestamps, so this is the legacy id order), with their tiers; components without a current LIST price are left out |
   | `item.price` / `brand` / `phase` / `inverter_type` / `kw` | current LIST `pricing_price.amount` / `brand_label` / `attributes.phase` else `inverter_spec.phase` / `inverter_spec` type (`micro` for MICRO topology) else `attributes.type` / `inverter_spec.kw` |
   | `slot.category.slug == "inverter"` (nearest kW) | category `bom_role` MAIN_INVERTER |
   | unit `m` for the four cable slugs | category `unit` M (the catalog importer's rule for exactly those slugs) |
   | `slot.gst`, `fi.gst`, `cat.gst_default` | `gst_rate` fractions as percentages (`percent_of`: 0.18 → `18`, an `int` as before) |
   | `StructureTemplate` / items | `bom_structure_template` / items |
   | `MarketRate.objects.filter(system_type=…)` | cells of the ACTIVE `pricing_market_rate_set` (no future-ready/variant cells) |
   | `Offer.objects.filter(active=True, dates)` | ACTIVE `pricing_offer` rows by `created_at, id`, date window on `today`; `applies_to_system`/`_tier` ALL or blank = all; a Flarize `applies_to_size_key` restricts the size |

   `today` is an explicit argument (the view passes `timezone.localdate()`); arithmetic stays in binary floating
   point in the legacy order (DV-90). The response is the legacy body — `bom_lines`, `cost_breakdown`, `totals`,
   `pricing`, `meta`, `available_offers` — keys, order and number types included.
6. **Validation.** `parse_request` keeps the legacy `_validate` messages and order (400 `validation_error`; the
   messages joined in `message`, per field in `errors`; `QuoteInvalid.legacy_errors` is the old `{"errors": [...]}`
   list for the `/legacy/` shim). Everything the legacy view crashed on is a 400 too (DV-90). Missing cost
   configuration is 503 `quote_not_configured`; no active template for the system type is 400 `bom_template_missing`
   (legacy: a 400 `{"error": …}`).
7. **`bom/build/`** builds a Flarize-shaped catalog from the tables (slot rules as `qty`/`premiumQty`/`batQty`/
   `premiumBatQty`, other rule types evaluated per size key; fixed items whose condition holds; components with tiers,
   legacy codes, LIST prices; package profiles) and runs `engines.bom_builder.build_bom` (catalog only, no registry
   pins — pack configuration is the packs package's). Engine refusals keep their code (`unsupported_configuration`,
   `selection_not_permitted`, else `bom_build_refused`). Required slots that need a component and got none are listed
   under `warnings` (`slot_unfilled`). The optional structure template adds its lines and tube kg.
8. **Writes** (`bom.services.masters`): one transaction, compare-and-swap on `version`, audit
   `bom.<noun>_created|updated|deleted`, cache namespace `bom` bumped, `bom.configuration_changed`
   `{object_type, object_uid, action}` emitted (DV-89). Deleting a template or structure template soft-deletes its
   children. Components put into a fixed item or a profile must be selectable (`catalog.assert_selectable`:
   `component_retired`, `component_deleted`); slots need a live, active category.
9. **Catalog usage.** `bom.fixed_items` and `bom.package_profiles` are registered with `catalog.services.usage`: a
   component a fixed item or profile names cannot be deleted (409 `component_in_use`).

## Legacy mapping

### Main backend (`GoldenApp`) — every column has a home

`bom_bomtemplate` → `bom_template`: `id` → `core_legacy_map` · `system_type` ongrid/hybrid/upgrade → `system_type`
ONGRID/HYBRID/UPGRADE · `label` → `name` · `description` · `sizes` (object) → `sizes` `[{key, label}]` in source
order · `three_phase_sizes` · `available_tiers` → `tiers` · `battery_configs`.

`bom_bomslot` → `bom_slot`:

| Legacy column | Home |
|---|---|
| `id` | `core_legacy_map` |
| `template_id` | `template` (via the map) |
| `category_id` | `category` (via the catalog import's `bom_category` map); `key` = its slug |
| `pos` | `sort_order` |
| `label` | `label` |
| `variable` | `is_variable` |
| `gst` (%) | `gst_rate` (fraction) |
| `qty`, `premium_qty`, `bat_qty`, `premium_bat_qty` | `qty_rule` `size_table` (`qty` always; `premium_qty` when not null; band tables when not empty) |
| `qty_mode` newPanels / fixed + `fixed_qty` (upgrade template) | `qty_rule` `new_panels` / `fixed` (`qty` = `fixed_qty or 0`); another mode on a non-upgrade slot is reported `qty_mode_ignored` |
| `filter_type` ongrid/hybrid | `filter_type` ONGRID/HYBRID (anything else: blank + `unknown_filter_type`) |
| `filter_phase` HYB | `filter_phase` HYB (else blank + `unknown_filter_phase`) |

`bom_bomfixeditem` → `bom_fixed_item`: `id` → map · `template_id` · `name` · `gst` → `gst_rate` · `price` →
`unit_price` (NULL for linked upgrade rows, decision 2) · `catalog_item_id` → `component` (via the `bom_catalogitem`
map) · `category_id` → `category` · `qty`/`bat_qty`/`premium_qty` → `qty_rule` (`size_table` with `bat_lookup:
always`; `upgrade_path` on the upgrade template) · `section` · `is_tube` · id order → `sort_order`.

`bom_structuretemplate` → `bom_structure_template`: `slug` → PLAN slug (decision 4) · `label` → `name`.
`bom_structuretemplateitem` → `bom_structure_template_item`: `name` · `item_type` tube/fixed → TUBE/FIXED ·
`tube_size` · `weight_kg` · `price` → `unit_price` · `unit` · `qty` → `qty_rule` `kw_interpolated` · id order →
`sort_order`. `bom_tubeweight` → `bom_tube_weight` (`tube_size`, `weight_kg`).

### Flarize `catalog.json`

| Source | Home |
|---|---|
| `bomTemplates.<sys>` `label`, `description`, `sizes`, `tiers`, `threePhase`, `batteryConfigs` | `bom_template` (name defaults: On-Grid, Hybrid, On-Grid Upgrade) |
| `bomTemplates.<sys>.slots[]` `pos`, `category`, `label`, `variable`, `gst`, `qty`, `premiumQty`, `batQty`, `premiumBatQty`, `filterType`, `filterPhase`, `qtyMode`, `fixedQty`, `hybridQty` | `bom_slot` (same rules as the main backend; `hybridQty` kept in the `fixed` rule as `hybrid_qty`) |
| `bomTemplates.<sys>.fixedItems[]` `id`, `name`, `gst`, `price`, `qty`, `premiumQty`, `batQty`, `category`, `itemId`, `_section`, `isTube`, `unit` | `bom_fixed_item` `code`, `name`, `gst_rate`, `unit_price`, `qty_rule`, `category`, `component` (by SKU), `section`, `is_tube`, `unit` |
| `structureTemplates.<slug>` `label`, `items[]` `name`, `type`, `tubeSize`, `weightKg`, `price`, `unit`, `qty` | `bom_structure_template` + items |
| `tubeWeights.<size>` | `bom_tube_weight` |
| `packageProfiles.<key>` `label`, `packageKey`, `structureType`, `inverterType`, `batteryIncluded`, `batteryBrand`, `batteryModel`, `batteryCapacity`, `batteryQuantity`, `batteryComponentId`, `structureLaborOverride`, `repairMarginOverride`, `notes` | `bom_package_profile` (`repairMarginOverride` % → fraction) |

**D-2 (Flarize wins).** A template / structure template / tube weight that the Flarize import wrote is not changed by
the main-backend import (its rows are mapped onto it; each difference is a `d2_flarize_wins` warning). When Flarize
runs second it replaces the content — its slots and fixed items (matched by key / name) win, differences are reported
`d2_flarize_wins`, main-backend-only children are soft-deleted and reported `d2_removed`. Both orders end with the same
live rows (`test_d2_flarize_wins_in_either_order`); UAT example: `SS Terminal Strip 4P` 150 (catalog.json) over 140.
In production the website quote therefore quotes the Flarize configuration; the golden parity below loads only the
legacy database, which is what the captured responses were computed from.

Import contract: `import_goldenray_bom(templates, slots, fixed_items, structure_templates, structure_items,
tube_weights, *, user=None, dry_run=False)` and `import_flarize_bom(catalog_json, *, user=None, dry_run=False)` return
`{created, updated, unchanged, skipped, violations, counts}`; every source row runs in its own savepoint
(`invalid_value`, `unknown_template`, `unknown_category`, `unknown_component`, `target_deleted` …); one
`bom.legacy_import` audit row per call; re-runs change nothing (`unchanged`). Run after the catalog import (and the
pricing imports for the quote). Fixtures: `bom/tests/fixtures/legacy/` (exported read-only from a private restore of
`legacy_goldenapp.dump`; no personal data in these tables).

## Parity evidence

`bom/tests/test_quote_parity.py` loads the UAT legacy database through `catalog.legacy_import.import_bom_catalog`,
`pricing.legacy_import.import_prices` / `import_bom_global_costs` / `import_bom_market_rates` / `import_bom_offers`
(set activated) and `bom.legacy_import.import_goldenray_bom`, then replays **all 1,077** requests of
`bom/tests/golden/bom_calculate.json.gz` (a gzipped copy of `platform-reference/uat/golden/bom_calculate.json`,
captured 2026-09-28) with `today = 2026-09-28`, comparing `json.dumps` of the result with the captured body:

* 1,059 × HTTP 200 — **identical** (keys, key order, values, `int` vs `float`);
* 9 × HTTP 400 — identical `{"errors": [...]}` lists (`QuoteInvalid.legacy_errors`);
* 9 × HTTP 500 — approved differences (below).

A sample (every 40th 200 case and every 400 case) is replayed through `POST /api/public/v1/bom/quote/` under
`freeze_time(2026-09-28)`: the JSON bodies are identical, the 400s are `validation_error` envelopes carrying the legacy
messages.

### Approved differences from the legacy endpoint (DV-90)

| Legacy | Platform |
|---|---|
| `custom_discount` sent as a number (`7500`) → HTTP 500 `AttributeError` (the 9 golden cases: on-grid 3/5sp/10 × base/value/premium) | 400 `validation_error` on `custom_discount` |
| `custom_discount.value`, `ghs_houses`, `upgrade_from_kw`/`upgrade_to_kw`, `dist_km` not convertible (`"12.5"`, `"many"`), `margin_val` NaN/∞, a standard-quote `size` whose kW is NaN/∞ or beyond 10¹² (`"nan"`, `"inf"`, `"1e400"`), numbers beyond 10¹², a list as `sys_type`, a non-object `upgrade_sections` (upgrades), a non-object body → HTTP 500 | 400 `validation_error` on the field |
| no `GlobalCosts` row → seeds itself from a JSON file | 503 `quote_not_configured` |
| no template → 400 `{"error": …}` | 400 `bom_template_missing` (platform envelope) |
| DRAFT/RETIRED items could be quoted; an item without a price crashed | only ACTIVE/DEPRECATED components with a current LIST price are candidates |

The legacy `mode` field is accepted and ignored, as before. A `selected_offer_id` that is not a string (an object, a
list, a number) matches no offer and the quote falls through to the custom discount, as in the legacy view (HTTP 200).
An upgrade fixed item's GST keeps the legacy fallback `fi.gst or (category rate if a slot uses the category else 18)`.

A differential corpus of 436 extra requests (sizes off the grid, battery bands, upgrade paths and sections, offer /
discount / margin / distance / GHS variants, malformed values) was replayed against a private legacy server on the
UAT dump during the review: every 200 and 400 body is identical; the remaining 22 are legacy HTTP 500s answered 400.

## For later packages

* **legacy shim** (`/legacy/bom/api/calculate/`): call `website_quote.quote(request.data, today=timezone.localdate())`;
  on `QuoteInvalid` answer `400 {"errors": exc.legacy_errors}`; other `DomainError`s as the legacy view did
  (`{"error": message}`).
* **packs**: read templates/slots/fixed items/profiles through `bom.services.build.flarize_catalog(template, ctx)` (the
  engine's catalog shape) or the tables; listen to `bom.configuration_changed` to mark drafts stale.
* **migrations_tools**: after the catalog and pricing imports call `import_goldenray_bom(...)` with the six tables'
  rows (JSON columns as parsed JSON) and `import_flarize_bom(catalog_json)`; print counts + violations.
* The public quote response still carries `cost_breakdown` and `totals.margin` as the legacy one did (the website
  reads them); dropping them is a frontend decision (PLAN §6.5).
