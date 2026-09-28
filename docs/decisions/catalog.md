# WP catalog — the product master

Work package *catalog* builds the `catalog` app of PLAN §2.2 / §3.3 (`products/*`) / §3.4 (Catalog) on top of the
foundation (F1–F3, F-FIX), and its legacy importers for PLAN §7.3 (`solar_panels`, `solar_inverters`, `batteries`,
`bom_*`) and §7.4 (Flarize `catalog.json` categories/items, `battery-master.json`). Deviations: DV-16 … DV-20.

## What exists

| Area | Where | Notes |
|---|---|---|
| Tables | `catalog/models/` | `catalog_brand`, `catalog_category`, `catalog_component` (+ generated `search` tsvector), `catalog_component_tier`, `catalog_panel_spec`, `catalog_inverter_spec`, `catalog_battery_spec`, `catalog_battery_family`, `catalog_structure_spec`, `catalog_component_public_profile`, `catalog_component_change` *(no base)*. Every enum has a `CHECK`; every invariant is a partial unique index on live rows (brand `lower(name)` and slug, category slug and `lower(sku_prefix)`, component `lower(sku)`, tier per component, profile slug, family slug). Indexes: `(category_id, status)`, GIN(`search`), GIN(`attributes`), `(status, -kerala_climate_score)` on profiles, `(component_id, -at)` on the change log. |
| Staff API | `catalog/views/`, `catalog/urls.py` | exactly PLAN §3.4 Catalog (table below). |
| Public API | `catalog/views/public.py` | `products/panels/`, `products/inverters/`, `products/batteries/`, `products/<category>/<slug>/`. |
| Services | `catalog/services/` | `brands`, `categories`, `components`, `specs`, `lifecycle` (+ `assert_selectable`), `families`, `profiles`, `public`, `usage` (registry), `pricing_hooks` (registry), `history`, `csv_io`, `dashboard`, `legacy_import` + `legacy_support`. |
| Registries | `catalog.services.usage`, `catalog.services.pricing_hooks`, `core.dashboard`, `media.usage` | see "For later packages". |
| Tests | `catalog/tests/` | 165 tests; fixtures exported read-only from the UAT legacy sources in `catalog/tests/fixtures/legacy/`. |

### Endpoints

| Surface | Path | Permission |
|---|---|---|
| staff | `catalog/brands/` list/detail · create · PATCH · DELETE | `catalog` view · create · edit · archive (409 `brand_in_use`) |
| staff | `catalog/categories/` idem | idem (409 `category_in_use`, `attribute_schema_conflict`, `spec_kind_change`) |
| staff | `catalog/components/` list/detail · create · PATCH (nested `panel_spec` / `inverter_spec` / `battery_spec` / `structure_spec`, `tiers`) · DELETE | view · create · edit · archive (409 `component_in_use`) |
| staff | `POST catalog/components/<uid>/activate/`, `…/deprecate/` (`reason`, `replacement_uid`), `…/retire/` (`reason`) | `catalog.approve` |
| staff | `GET catalog/components/<uid>/history/` (cursor), `GET …/usage/` | `catalog.view` |
| staff | `POST catalog/components/import/` (multipart CSV; `dry_run`, `import_token`), `GET catalog/components/export/` | `catalog.create` (+ `catalog.edit` per updating row) · `catalog.view` |
| staff | `catalog/battery-families/` CRUD | `catalog` view · create · edit · archive (409 `battery_family_in_use`) |
| staff | `catalog/public-profiles/` CRUD + `…/publish/`, `…/unpublish/` | `products_public` view · edit (create/edit/delete) · publish |
| staff | `GET dashboard/` | counters for `catalog` and `products_public` |
| public | `GET products/panels/`, `products/inverters/`, `products/batteries/` (paginated; filters below), `GET products/<category>/<slug>/` | anonymous, throttle `public_read`, cached 300 s (Cache-Control ≤ 60 s) under `catalog` + `pricing` + `media` |

Public list filters: all lists `search`, `brand` (slugs, comma-separated), `overall_rating`, `min_kerala_score`,
`min_product_warranty`, `ordering` (`kerala_climate_score`, `published_at`, `slug`, warranties); panels add
`panel_type`, `technology`, `subsidy_eligible`, `min_performance_warranty`, `min_efficiency`; inverters add
`inverter_type`, `topology`, `rating_tier`, `min_extendable_warranty`; batteries add `min_capacity_kwh`. Without
`ordering` the list is Kerala score descending, unscored products last, then slug.

## Decisions not spelled out in the PLAN

1. **Spec table by BOM role.** A category's `bom_role` decides its components' spec table: MAIN_PANEL → panel,
   MAIN_INVERTER → inverter, BATTERY → battery, STRUCTURE → structure; other roles keep their technical data in
   `component.attributes`, validated by the category's `attribute_schema` (JSON Schema 2020-12, root `type: object`,
   `jsonschema` pinned in `requirements/base.txt`). Changing a schema re-validates every live component of the category;
   changing a role to another spec table is refused while specs exist; moving a component to a category with another
   spec table is refused. The write API names the spec explicitly (`panel_spec` …) so the OpenAPI contract is exact;
   sending the wrong one is a 400.
2. **Lifecycle.** New components are DRAFT. `activate` (DRAFT/DEPRECATED → ACTIVE) needs an active category and, for
   panels/inverters, the spec (the BOM builder sizes by wattage / kW); re-activation clears reason and replacement.
   `deprecate` (ACTIVE → DEPRECATED) needs a reason; the replacement must be live, not retired, in the same category,
   not the component itself and must not lead back to it. `retire` (DRAFT/ACTIVE/DEPRECATED → RETIRED) is terminal.
   Every transition is versioned, audited (`catalog.component_activated|deprecated|retired`), logged in the change
   table and emits `catalog.component_status_changed` `{component_uid, sku, from, to, replacement_uid?}`.
3. **Selection rule.** `catalog.services.assert_selectable(component, field="component")` raises
   `ComponentNotSelectable` (400): `component_retired` (message names the replacement), `component_deleted`,
   `component_not_found`. DRAFT/ACTIVE/DEPRECATED pass; `selection_warning()` returns the deprecation warning.
4. **SKUs.** Imported ids are kept (`p1`, `en7`, `pa_…`). Generated SKUs are `<category.sku_prefix>-<nnnn>` (next
   number over all rows including deleted, the category row locked) — upper-case with a dash, so they can never collide
   with the lower-case Flarize ids that keep arriving until the cutover (DV-19). SKUs are unique case-insensitively
   among live components and fixed once a component leaves DRAFT (409 `sku_locked`).
5. **Brands.** Unique by `lower(name)` among live rows; names are trimmed and inner spaces collapsed. A component
   prints `brand_label` (the source spelling: legacy data spells one maker `DEYE`/`Deye`, `RenewSys`/`Renewsys`); it
   follows the brand name unless set explicitly, and renaming a brand relabels the components that printed the old
   name. `brand` is nullable: many BOM consumables have none and none is invented (DV-16).
6. **Change log.** `catalog_component_change` gets one row per changed field (`name`, `spec.wattage_w`, `tiers`,
   `status`, `created`, `deleted`, imported `source.<action>` rows from Flarize `changeLog`), JSON-safe old/new and the
   reason; `history/` is cursor-paginated newest first. Audit rows are written as well.
7. **Usage registry and delete guard.** `catalog.services.usage.register("<context>.<what>")(fn)`; `fn(component)`
   returns live references `{object_type, object_uid, label?, status?}`. `usage/` lists every provider (≤ 100
   references each). Delete is refused (409 `component_in_use`) while any provider reports a reference **or fails**
   (fail closed). The catalog registers `catalog.replacements` (components naming this one as replacement).
8. **Public products.** A product is shown while its profile is PUBLISHED, the component live, `is_public` and
   ACTIVE/DEPRECATED, and the category live and active. `publish/` checks the same (409 `component_not_publishable`).
   The payload is profile + spec (internal battery/inverter engineering fields omitted) + `price` + `price_range_label`.
   `products/<category>/<slug>/` accepts a category slug or the aliases `panels`/`inverters`/`batteries`.
9. **Prices.** `catalog.services.pricing_hooks.register(fn)` installs the provider; `fn(components)` is called once per
   response with the page's components and returns `{component.pk: PriceInfo(min_amount, max_amount, currency,
   gst_inclusive, label, release_number)}`. The default provider knows no prices; a failing provider is logged and
   treated as "no price". `price_range_label` = provider label, else its range formatted in Indian grouping
   (`₹28,000 - ₹32,000`), else the profile's label.
10. **Caching.** Every catalog write bumps `catalog`; public product responses are keyed on `catalog`, `pricing` and
    `media` (images and gallery come from the media library).
11. **CSV import/export.** Export writes the list filters' components (≤ 10,000) with `spec.<column>` columns for the
    spec tables present; JSON cells for attributes and list-valued spec columns; the battery family as its slug; cells
    that a spreadsheet would run as formulas get a leading `'` (stripped on import). Import is enforced two-step: a dry
    run executes every row through the same serializers and services inside a rolled-back transaction and returns a
    per-row report plus an `import_token` (signed: file SHA-256 + user, 1 hour); commit needs that token for the same
    bytes and is all-or-nothing (400 `import_has_errors`). Limits 2 MB (413 `file_too_large`), 5,000 rows. One audit row
    `catalog.components_imported` per commit. Existing SKUs update (needs `catalog.edit`); new rows create DRAFTs;
    unknown brands are created; `status` is informational.
12. **Events.** `catalog.component_created|updated|deleted`, `catalog.component_status_changed`,
    `catalog.brand_renamed`, `catalog.profile_published|unpublished|updated` (website revalidation) — no handlers yet.

## Legacy import (`catalog/services/legacy_import.py`)

`import_goldenray_products(panels, inverters, batteries)`, `import_bom_catalog(categories, items, tiers)`,
`import_flarize_catalog(catalog_json, battery_master_json)` take plain row dicts (DB rows; parsed JSON) and a
keyword `user=None`, `dry_run=False`. Each returns `{created, updated, unchanged, skipped, violations, prices, counts}`
(`counts` per source table); `violations` carry `source_table`, `source_id`, `code`, `severity` (`error`: row not
imported; `warning`: imported, needs review), `message` and context. One `catalog.legacy_import` audit row per call
records the counts and the SHA-256 of the input. Each source row runs in its own savepoint.

* **Traceability.** `core_legacy_map`: `BACKEND solar_panels|solar_inverters|batteries|bom_catalogitem → catalog_component`,
  `BACKEND bom_category → catalog_category`, `BACKEND bom_itemtier → catalog_component_tier`,
  `FLARIZE catalog.json:categories (slug) → catalog_category`, `FLARIZE catalog.json:items (id) → catalog_component`.
  Re-runs update through the services (only changed fields are written; unchanged rows count as `unchanged`), a mapped
  row deleted in the platform since is reported (`target_deleted`) and never re-created.
* **Brands** match case-insensitively after trimming; a new brand whose name is a whole-word prefix of an existing one
  or vice versa is reported (`brand_near_match`: `Adani`/`Adani Solar`, `Saatvik`/`Saatvik Solar`,
  `Goldi`/`Goldi Solar` in the UAT data) — never merged.
* **Website ↔ catalog de-duplication**: same category, brand, size (panel W / inverter kW / battery kWh) and model
  compared on letters and digits only. One candidate → one record (website fills empty fields, differing values are
  kept and reported as `value_conflict`, `brand_label_differs`); several → `ambiguous_match` (not imported); none →
  a new public component (`possible_match` warnings list same brand+size candidates with another model). Rows of one
  source table never merge into each other. In the UAT data no website product matches a BOM/Flarize component (the
  Flarize panels carry no model designation; the inverter models differ), so all 22 become public components.
* **D-2.** Where `catalog.json` and `bom_*` describe the same SKU, Flarize wins in either import order; every field on
  which they disagree (of the fields BOM carries: name, brand, model, GST override, wattage, DCR, kW, phase, type,
  tiers, category) is reported as `d2_flarize_wins` with both values. UAT: 10 SKUs (`p1`, `pa_9e35d4adf2a3`, `m2`,
  `dc3`, `is1`, `cb4`, `cb5`, `cb6`, `ec1`, `la1`). Prices: `merge_prices(flarize, bom)` applies D-2 to the returned
  price lists and lists the differing amounts (UAT: `p2`, `p3`, `p8`, … — pricing imports the merged list).
* **Status.** BOM rows and approved Flarize items → ACTIVE; Flarize `status` TEST / `engineeringStatus`
  TEST_PLACEHOLDER*, `status` INACTIVE, `approvalStatus` REJECTED, battery-master engineering REJECTED/WITHDRAWN or
  procurement INACTIVE/DISCONTINUED → RETIRED (reason kept in `retired_reason`); other approval states → DRAFT; an
  ACTIVE panel/inverter without spec data → DRAFT (`spec_missing`). The source's lifecycle wins on re-runs until the
  cutover.
* **Values** are never invented or rounded: numbers must fit their column's scale (`invalid_value` otherwise), unknown
  enum values reject the row, `null` stays `null`; unknown source keys go to `component.attributes` and the category's
  schema is extended with their JSON types. Source timestamps (`created_at`/`updated_at`, Flarize
  `createdAt`/`updatedAt`) are preserved; Flarize `createdBy`/`updatedBy` resolve through the map of the (later) user
  import (`FLARIZE users.json`), else stay empty.
* **Prices are returned**, never written: `{sku, kind (LIST|PURCHASE), amount, per_watt, source_system,
  source_table, source_id}` (Flarize `price`/`perWatt`, battery-master `purchasePrice`/`sellingPrice`, BOM
  `price`/`per_watt`, website `battery_price`).

### Category defaults (importer-created categories)

`bom_role`: panel MAIN_PANEL · inverter MAIN_INVERTER · battery BATTERY · dcdb, acdb, isolator, mccb_box, change_over
PROTECTION · dc_cable, ac_cable, armoured_cable, ug_cable, battery_cable CABLE · earth_cable, la_cable, cb_rod EARTHING
· structure, structure_material, solar_clamp STRUCTURE · service SERVICE · everything else MISC (an unknown slug is
reported `unknown_category_role`). `unit` M for dc_cable, ac_cable, armoured_cable, ug_cable (the legacy
`BomCalculator` rule), else NOS; `gst_rate` = source percentage / 100; `sort_order` = source order; `sku_prefix` from a
fixed table (PNL, INV, BAT, DCDB, ACDB, …) or derived from the slug.

### Legacy column mapping

`solar_panels` (website; one row → component + panel spec + PUBLISHED public profile):

| Legacy column | Home | Legacy column | Home |
|---|---|---|---|
| `id` | `core_legacy_map.source_id` | `bifacial_gain` | `panel_spec.bifacial_gain_pct` |
| `brand` | `component.brand` (matched/created) + `component.brand_label` (exact) | `product_warranty` | `component.warranty_product_years` |
| `name` | `profile.headline`; `component.model` | `performance_warranty` | `component.warranty_performance_years` |
| `wattage` | `panel_spec.wattage_w` | `first_year_power_drop` | `panel_spec.first_year_drop_pct` |
| `panel_type` (monocrystalline/polycrystalline/bifacial) | `panel_spec.panel_type` (MONOCRYSTALLINE/POLYCRYSTALLINE/BIFACIAL) | `annual_degradation` | `panel_spec.annual_degradation_pct` |
| `technology` (n-type-topcon/p-type-perc/hjt/ibc) | `panel_spec.technology` (N_TYPE_TOPCON/P_TYPE_PERC/HJT/IBC) | `output_at_year_25` | `panel_spec.output_at_year_25_pct` |
| `image_url` | `profile.image_url` | `manufacturing_capacity` | `panel_spec.manufacturing_capacity` |
| `description` | `profile.summary` | `bloomberg_tier1`, `pvel_top_performer`, `bis_certified`, `independent_audit` | `panel_spec.*` (same names) |
| `efficiency` | `panel_spec.efficiency_pct` | `certifications` | `panel_spec.certifications` |
| `temperature_coefficient` | `panel_spec.temperature_coefficient` | `price_range` | `profile.price_range_label` |
| `noct` | `panel_spec.noct_c` | `subsidy_eligible` | `profile.subsidy_eligible` |
| `real_output_at_60c` | `panel_spec.real_output_at_60c_pct` | `kerala_climate_score` | `profile.kerala_climate_score` |
| `ip_rating` | `panel_spec.ip_rating` | `efficiency_rating`, `heat_performance_rating`, `warranty_rating`, `kerala_climate_rating` | `profile.ratings` `{efficiency, heat_performance, warranty, kerala_climate}` |
| `wind_load` | `panel_spec.wind_load_pa` | `overall_rating` (excellent/very-good/good) | `profile.overall_rating` (EXCELLENT/VERY_GOOD/GOOD) |
| `moisture_protection` | `panel_spec.moisture_protection` | `created_at`, `updated_at` | `profile.created_at/updated_at` (= `published_at`/component when not shared) |
| `weight` | `panel_spec.weight_kg` | (`__str__`) | `component.name` = `"<brand> <name> - <wattage>Wp"` |

`solar_inverters` (website):

| Legacy column | Home | Legacy column | Home |
|---|---|---|---|
| `id` | `core_legacy_map.source_id` | `iv_curve_scanning`, `corrosion_protection`, `operating_temperature`, `cooling`, `noise_level` | `inverter_spec.*` (same names) |
| `brand` | `component.brand` + `brand_label` | `dc_surge_protection`, `ac_surge_protection`, `arc_fault_detection`, `grid_protection` | `inverter_spec.*` |
| `name` | `profile.headline`; `component.model` | `monitoring_app`, `real_time_monitoring`, `remote_diagnostics`, `firmware_updates`, `connectivity` | `inverter_spec.*` |
| `inverter_type` string / hybrid / microinverter / optimized-string | `inverter_spec.inverter_type` + `topology`: ONGRID+STRING / HYBRID / ONGRID+MICRO / ONGRID+OPTIMIZED_STRING | `warranty_years` | `component.warranty_product_years` |
| `rating_tier` (premium/mid-range/value) | `profile.rating_tier` | `extendable_warranty_years` | `component.warranty_extendable_years` |
| `image_url`, `description` | `profile.image_url`, `profile.summary` | `certifications` | `inverter_spec.certifications` |
| `rated_output_power` (W) | `inverter_spec.kw` = W / 1000 (numeric(7,3), exact) | `brand_trust`, `year_founded`, `countries_served`, `global_installations` | `inverter_spec.*` |
| `maximum_dc_input` (W) | `inverter_spec.max_dc_input_kw` = W / 1000 | `price_range` | `profile.price_range_label` |
| `mppt_trackers` | `inverter_spec.mppt_count` | `kerala_climate_score` | `profile.kerala_climate_score` |
| `maximum_dc_voltage` | `inverter_spec.max_pv_voltage_v` | `efficiency_rating`, `reliability_rating`, `warranty_rating`, `kerala_climate_rating` | `profile.ratings` `{efficiency, reliability, warranty, kerala_climate}` |
| `maximum_input_current` (text) | `inverter_spec.max_input_current_text` | `overall_rating` | `profile.overall_rating` |
| `weight`, `display`, `suitable_system_size` | `inverter_spec.weight_kg`, `.display`, `.suitable_system_size` | `created_at`, `updated_at` | `profile.created_at/updated_at` |
| `maximum_efficiency`, `european_efficiency`, `mppt_efficiency` | `inverter_spec.efficiency_pct`, `.european_efficiency_pct`, `.mppt_efficiency_pct` | (`__str__`) | `component.name` = `"<brand> <name>"` |
| `dc_oversizing`, `ac_overloading`, `pid_protection` | `inverter_spec.dc_oversizing_pct`, `.ac_overloading_pct`, `.pid_protection` | | |

`batteries` (website): `id` → legacy map · `battery_capacity` → `battery_spec.capacity_kwh` · `backup_hour` →
`battery_spec.backup_hours` · `battery_price` → returned LIST price · `created_at`/`updated_at` → component and profile
timestamps · (`__str__`) → `component.name` = `"Battery <capacity>kWh - <backup>h backup"`; no brand (none recorded).

`bom_category`: `id` → legacy map · `slug` → `category.slug` · `label` → `category.name` · `gst_default` (%) →
`category.gst_rate` (fraction).

`bom_catalogitem`: `id` → legacy map · `item_id` → `component.sku` · `category_id` → `component.category` (via the
category map) · `name` · `brand` → `brand` + `brand_label` · `price`, `per_watt` → returned LIST price ·
`gst_override` (%) → `gst_rate_override` (fraction) · `watt`, `dcr` → `panel_spec.wattage_w`, `.is_dcr` (panel
categories only) · `kw`, `phase`, `inverter_type` (ongrid/hybrid/micro) → `inverter_spec.kw`, `.phase`,
`.inverter_type` (+ `topology` MICRO) for inverters; `phase` (1P, 3P, 1P-HYB, 3P-HYB) and `inverter_type` of other
categories (DCDB/ACDB variants) → `attributes.phase`, `attributes.type` · `model` → `component.model` ·
`created_at`/`updated_at` → component timestamps.

`bom_itemtier`: `id` → legacy map (→ `catalog_component_tier`) · `item_id` → component · `tier` → `tier` (upper case).

Flarize `catalog.json` `categories.<slug>`: `label` → `name`, `gstDefault` → `gst_rate`, order → `sort_order`;
`items[]` (all categories): `id` → `sku` · `name` · `brand` → `brand`/`brand_label` · `tiers` → tiers · `price`,
`perWatt` → returned prices · `isPremium` → `is_premium` · `approvalStatus`, `status`, `engineeringStatus` → status
(rules above) + `engineering_status` · `note` → `notes` · `warranty` → `warranty_text` · `gstOverride` →
`gst_rate_override` · `unit` → `unit_override` (when it differs from the category unit) · `createdAt`/`updatedAt`/
`createdBy`/`updatedBy` → timestamps/attribution · `changeLog[]` → `catalog_component_change` rows `source.<action>`.
Panels: `watt`, `dcr` (`panelType` DCR/NON_DCR is checked against it, `panel_type_mismatch`), `voc`, `vmp`, `isc`,
`imp`, `tempCoeffVoc`, `tempCoeffPmax`, `cells`, `maxSysVoltage` → `panel_spec.wattage_w`, `is_dcr`, `voc_v`,
`vmp_v`, `isc_a`, `imp_a`, `temperature_coefficient_voc`, `temperature_coefficient`, `cell_count`,
`max_system_voltage_v`. Inverters: `phase`, `kw`, `model`, `type` (ongrid/hybrid/micro), `mpptCount`,
`mpptVoltageMin`/`Max`, `maxInputVoltage`, `startVoltage`, `maxInputCurrent`, `maxStringsPerMppt`, `maxIsc`,
`deviceType`, `panelsPerDevice`, `optimizerBased`/`isMicro` → `inverter_spec.phase`, `kw`, `component.model`,
`inverter_type`, `mppt_count`, `mppt_voltage_min_v`/`max_v`, `max_pv_voltage_v`, `start_voltage_v`,
`max_input_current_a`, `max_strings_per_mppt`, `max_isc_a`, `device_type`, `panels_per_device`, `topology`.
Structure categories: `tubeSize`, `structureRole`, `specification` → `structure_spec.tube_size`, `structure_role`,
`specification`. Every other key (`phase`, `sqmm`, `core`, `modules`, `deviceType`, `panelsPerDevice`,
`microAccessoryRole`, `type`, …) → `component.attributes.<snake_case>` with the category schema extended.

Flarize `battery-master.json` `batteries.<componentId>` (overlay on the battery item of the same id; unknown ids are
`unknown_component` violations): `brand`/`displayName` compared with the catalog item (`brand_differs`,
`display_name_differs`) · `model` → `component.model` when the item has none · `datasheetUrl` →
`component.datasheet_url` · `batteryType`, `chemistry`, `nominalVoltage`, `minVoltage`, `maxVoltage`, `capacityKwh`,
`usableCapacityKwh`, `continuous/maximumCharge/DischargeCurrent`, `peakCurrent`, `integratedProtection`,
`protectionType`, `protectionRating`, `externalProtectionRequired`, `communicationProtocol`, `communicationRequired`,
`compatibleInverters`, `compatibleSystemTypes`, `compatiblePhases` (null kept: "not recorded"), `architecture`,
`engineeringStatus`, `procurementStatus`, `engineeringNotes`, `openItems`, `statusHistory`, `supplier`,
`supplierReference` → the `battery_spec` columns of the same meaning · `purchasePrice`/`sellingPrice` → returned
PURCHASE/LIST prices · `changeLog` → change rows · any other key → `attributes.master_<key>`. The file names no
battery families, so `battery_spec.family` stays empty (DV-17).

## Parity evidence

`catalog/tests/test_legacy_parity.py` imports the committed fixtures (exported read-only from the UAT
`legacy_goldenapp` database and the Flarize data files) in three orders (website only; Flarize → BOM → website;
website → BOM → Flarize) and rebuilds the legacy `GET /api/solar-panels/`, `/api/solar-inverters/` and
`/api/batteries/` responses with `catalog/tests/legacy_rebuild.py` (the executable form of the tables above, for the
legacy shim to reuse). They are compared with the responses captured from the UAT legacy server
(`fixtures/legacy/response_*.json`): every row is **equal field by field** (all columns, decimal scales, IST
timestamps), `meta.total` equal, and the list order by Kerala score equal. The only freedom is the order of rows with
equal scores, which the legacy server leaves to the heap (it returned ids 1, 7, 10, 9, 2 for five panels scored 96);
the rebuild orders ties by legacy id. `battery_price` comes from the importer's returned price rows (pricing imports
them). `test_every_legacy_column_has_a_home` asserts that every legacy column appears in the rebuilt rows.

## For later packages

* **Selecting components** (packs, BOM, quotations, projects): `from catalog.services import assert_selectable`; call
  it for every component you put into something new; surface `selection_warning(component)` for DEPRECATED ones.
* **Usage**: register in your `AppConfig.ready()`: `@usage.register("packs.config_lines")` returning live references;
  it drives `usage/` and blocks deletion.
* **Pricing**: register the current-release provider with `pricing_hooks.register(fn)` (batch, `{pk: PriceInfo}`)
  and bump the `pricing` namespace on every release; import the `prices` the catalog importers return (use
  `merge_prices` for D-2).
* **Legacy shim**: rebuild the old product endpoints from `core_legacy_map` + the columns above
  (`catalog/tests/legacy_rebuild.py`); legacy filters map to typed columns (`panel_type`, `overall_rating`,
  `efficiency_pct`, warranty years, `kerala_climate_score`, `rating_tier`, `topology`).
* **migrations_tools**: call the three importers with the source rows (psycopg rows or `json.load` output) and print
  `counts` + `violations`; `dry_run=True` for `--dry-run`. Run order does not matter (D-2 holds either way); the
  accounts import should run first so Flarize `createdBy` resolves (`FLARIZE users.json` map).
