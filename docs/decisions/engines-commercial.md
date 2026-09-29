# engines-commercial — the Flarize commercial engines, ported to pure Python

Work package engines-commercial ports the commercial half of the Flarize Node utility into `engines/` (PLAN §1.5,
§4.5 "Track B — engines"; spec `/home/user/platform-reference/flarize-engines-spec.md` §6–§10, §13–§21). The engines
are pure functions: no Django, no app, no I/O (import-linter contract `engines-pure`, and
`engines/tests/test_commercial_units.py::test_engines_import_nothing_from_django_or_apps`). They are what the pricing,
procurement, packs, bom, quotations and calculators packages call; this package adds **no table, column, endpoint or
permission** — the PLAN tables they feed belong to those packages. Deviations: DV-17 … DV-20, DV-25, DV-26 (review).

## What exists

| Module | Port of (Flarize `/home/user/flarize-main/flarize`) | Public API |
|---|---|---|
| `engines/bom_builder.py` | `server-bom-builder.js` | `build_bom(config, *, catalog, registry, pack_config)`, `build_all_tier_boms`, `get_alternatives`, `load_catalog`, `get_profile_key`, `is_sales_role`, `strip_cost_fields_for_sales`, `BomBuildError` |
| `engines/device_allocation.py` | `src/lib/deviceAllocation.js` | `allocate_devices(*, panels, candidates)`, `ALLOCATION_CODE` |
| `engines/pack_pricing.py` | `src/lib/packPricing.js` (`pack-pricing.1`) | `price_pack(*, config, system_type, size, tier, roof_type, distance_km, vehicle_type, battery_config, future_system_size, lines, priced_at)`, `structure_qty`, `structure_material`, `tube_rate_for`, `kw_of` |
| `engines/cost.py` | `src/lib/costEngine.js` (`costEngine.3`), `procurementPriceMaster.js`, `procurementBatch.js` | `calculate_cost(*, snapshot, config, price_master, project, rate_card, …)`, `explain_cost`, `to_price_record`, `build_price_master`, `project_unit_cost`, `procurement_uplift`, **`allocate_landed(lines, charges, …)`**, `build_batch_landed_costs`, `to_price_master_entries` |
| `engines/rate_card.py` | `src/lib/commercialHistory.js`, `src/lib/projectRateCard.js` | `append_version`, `archive_version`, `get_history`, `get_current_version`, `get_version`, `get_version_effective_at`, `compare_versions`, `history_view`, `create_rate_card`, `set_rate`, `resolve_rate`, `installation_key`, `engineering_key`, `list_rates`, `seed_rate_card_from_config` |
| `engines/pricing.py` | `src/lib/pricingEngine.js` (`pricingEngine.3`) | `calculate_pricing(*, cost_result, margin, gst, extras, market_rate, priced_at, tier, discount, customer_side_expenses)`, `resolve_tier_margin`, `resolve_gst_regime`, `validate_gross_margin`, `explain_pricing`, **`gross_margin_list_price(cost, margin, *, margin_type)`**, `PricingError` |
| `engines/offers.py` | `src/lib/offerLifecycle.js` | `validate_offer`, `margin_safety_check`, `is_valid_transition`, `transition_offer(…, now=)`, `create_draft_offer`, `update_offer`, `auto_expire_offers`, `find_applicable_offer(*, system_type, tier, size, date)`, `calculate_offer_amount`, `offer_payload_shape`, `OfferError` |
| `engines/battery_compat.py` | `src/lib/batteryMaster.js`, `batteryCompatibility.js`, `resolveBattery.js` | `to_battery_master`, `all_batteries`, `is_selectable`, `is_permanently_unselectable`, `selectability_reason`, `check_battery_compatibility`, `resolve_protection_requirement`, `resolve_battery`, `approved_battery_shortlist`, `is_battery_alias`, `canonical_battery_id_for` — since the wave-1 integration `to_battery_master`, `check_battery_compatibility` and `resolve_protection_requirement` run engines-rules' port (`engines.engineering_checker`) through a number-type adapter; both golden sets replay through it |
| `engines/pack_config.py` | `src/lib/packConfig.js` (`flarize.pack-config/1`) | `validate_section`, **`validate_config`** (the `packs_config_version.config` schema), `seed_from_catalog`, `approved_config`, `draft_config`, `update_draft_section`, `update_draft_template`, `submit_draft`, `approve_draft`, `approve_draft_direct`, `reject_draft`, `reset_draft`, `describe_store`, `market_rate_key`, `approved_sizes`, `future_pairs_for`; re-exports the package-registry API below |
| `engines/package_registry.py` | `src/lib/packageApproval.js` (V4), `packageAuthority.js`, `packageProjection.js` | `approve_package`, `reject_package`, `reset_package`, `bulk_approve_packages`, `list_packages_for_role`, `approval_summary`, `assert_package_selectable_by_actor`, `create_package`, `duplicate_package`, `create_revision`, `edit_package`, `submit_package`, `archive_package`, `run_package_checker(…, check_project_bom=)`, `resolve_package_architecture`, `combo_key_for`, `PackageIdFactory`, `derive_package_components`, `is_approved_selection`, `is_sales_editable`, `hydrate_registry`, `visible_for_runtime` |
| `engines/flarize_rbac.py` | `src/lib/rbac.js` (`rbac.1`) | `assert_can` (the default `authorize` of the lifecycles), `allow_all`, `can`, `CAPABILITY_MATRIX`, `RbacError` |
| `engines/money.py` (binary64 section) | `src/lib/money.js` (`money.1`) | `round_money_binary64`, `sum_exact_binary64`, `mul_exact_binary64`, `is_money_binary64` on doubles, beside engines-core's Decimal API in the same module (the former `engines/_money_compat.py`, consolidated at the wave-1 integration; see "Merge notes") |
| `engines/_jscompat.py` | — | the JavaScript value rules the ports need (below), on JavaScript numbers; private to `engines`. White space, the ASCII number literals and array indexes come from `engines.jscompat` (one definition, wave-1 integration) |
| `engines/tests/golden/generate_commercial.mjs` | — | runs the REAL JS against the real data + synthetic edge cases; writes the golden files |
| `engines/tests/golden/commercial_*.json`, `fixtures/flarize_commercial.json` | — | 1 907 golden cases; the Flarize data the Python side replays them with |
| `engines/tests/golden_harness.py`, `test_commercial_golden.py`, `test_commercial_reference.py`, `test_commercial_units.py`, `test_commercial_review.py` | — | exact-equality replay of every case (documented deviations applied by `EXPECTED_DEVIATIONS`); §20 references asserted literally; Python-only rules; the review findings |

## Decisions not spelled out in the PLAN

1. **Parity first: the engines compute like the JavaScript (DV-17).** Money is IEEE-754 double arithmetic in the same
   operation order as the JS (`Math.round` = ties toward +∞, `roundMoney` = half away from zero with the
   `Number.EPSILON` nudge, `toFixed` on the exact binary value). Integers beyond 2⁵³ collapse to doubles like a JS
   number. Inputs are JSON-like (`dict`/`list`/`str`/`int`/`float`/`bool`/`None`); services pass `Decimal` columns
   through `engines._jscompat.json_numbers(...)` and quantize results to `DecimalField(14, 2)` / `(10, 4)` when they
   persist them. Nothing in `engines/` touches `Decimal` arithmetic.
2. **JavaScript value rules are explicit** (`engines/_jscompat.py`): `truthy` (`[]`/`{}` truthy), `js_or` (`||`),
   `nullish` (`??`), `js_number` (`Number()`), `js_add` (`'4' + 3 === '43'`), `js_str` (template literals and
   `String(n)`: `1e+21`, `1.5e-7`), `js_keys` (`Object.keys` puts integer-like keys first), `json_equal`
   (`JSON.stringify(a) === JSON.stringify(b)`, key order included — `NO_CHANGES` / `changed` in the pack-config log),
   `strict_equal`, `includes`, UTF-16 string order, and `locale_compare` (an ICU-root approximation: punctuation <
   digits < letters, then accents, then lower before upper — exact for the ASCII role names and ISO timestamps the
   engines sort). `UNDEFINED` models a missing property where the JS distinguished it from `null`
   (`Number(undefined)` is NaN but `Number(null)` is 0: `distanceKm: null` prices at 0 km, a missing one is
   `DISTANCE_INVALID`). Results never contain `UNDEFINED`: keys whose JS value was `undefined` are omitted, exactly
   as `JSON.stringify` drops them.
3. **Inputs instead of files.** `build_bom` takes `catalog` (the Flarize catalog shape), `pack_config` (a dict, or a
   provider `source -> config` — the JS `setPackConfigProvider`; `None` = catalog only, `configSource` becomes
   `CATALOG_ONLY`) and `registry` (`{"packages": [...]}`). Only the FIRST registry package per
   (systemType, size, tier, phase) is ever read (`findRegistryPackage`), so the committed fixture keeps exactly those
   records (their identity plus `role`, `componentId`, `pinnedComponentId`, `derivedBy`, `approvedAlternates`); the
   generator runs the JS against the full 5.5 MB `packages.proposed.json` and the Python replays against the reduced
   one — the parity of all BOM cases proves the reduction loses nothing.
4. **No clock, no counters in the engines.** Offers take `now` (ISO instant) / `date` (ISO day), defaulting to UTC
   now; package ids come from an injectable `PackageIdFactory(now_ms, seq)` (the JS used `Date.now()` and a
   module-level counter). The generator freezes the JS clock at `2026-09-20T10:00:00.000Z` and loads a fresh
   `packageApproval.js` per scenario so ids replay exactly.
5. **Authorisation hook (DV-20).** The pack-config and package-registry lifecycles keep Flarize's own capability check
   as their default `authorize` policy (`engines.flarize_rbac.assert_can`, the `rbac.1` matrix) — needed for parity
   and fail-closed: a platform role slug (`project-head`) is an `UNKNOWN_ROLE` to it. A platform service authorises
   through `accounts.registry` first (packs `edit`/`submit`/`approve`/`publish`) and then calls the engine with
   `authorize=engines.flarize_rbac.allow_all`.
6. **The engineering checker is injected.** `run_package_checker(…, check_project_bom=fn)` passes `fn` the JS argument
   object of `engineeringChecker.checkProjectBom` as one dict (the checker is its own engine, not in this package);
   the registry→checker role map (`bomRoles.js` `CATEGORY_TO_ROLE`, `ENPHASE_COMPONENT_ROLES`) is carried privately.
7. **Landed cost with several charges (DV-18).** `allocate_landed(lines, charges)` takes the batch's
   `procurement_batch_charge` rows (FREIGHT, INSURANCE, HANDLING, DUTY, OTHER — all supplier → warehouse), sums them
   exactly and allocates the sum by purchase-value proportion in ONE largest-remainder reconciliation — Flarize's
   `buildBatchLandedCosts` with `deliveryCost` = the sum, same result shape. Allocation by weight, volume, quantity or
   equal split is refused (`ALLOCATION_METHOD_INVALID`), as is project transport (`PROJECT_TRANSPORT_NOT_ALLOWED`); an
   unknown charge kind or a missing/negative amount refuses the batch with the new code `BATCH_CHARGE_INVALID`.
   `landedUnitCost` is whole rupees (the JS rule) and `landedUnitCostExact` the unrounded value; the procurement
   service stores the one its column needs.
8. **MARKUP is forbidden twice.** `calculate_pricing` returns `REJECTED` / `MARGIN_TYPE_FORBIDDEN` like the JS;
   `gross_margin_list_price(cost, margin, margin_type=…)` — the single list-price formula other contexts use
   (PLAN §2.3 "LIST is never derived by markup in code") — raises `PricingError` for MARKUP, an unknown type, or a
   margin outside [0, 1).
9. **Offers keep the Flarize lifecycle (DV-19).** DRAFT → APPROVED → ACTIVE → EXPIRED → ARCHIVED, types
   `flat`/`percentage`, transitions mutate the offer and append to `transitions[]`, like the JS. Decision D-4
   ("offers change the printed customer price") is applied by the quotations package with
   `calculate_offer_amount(offer, sellingPriceBeforeGST)`; Flarize only pinned the amount, so there is no engine
   formula to port for it.
10. **Errors.** Every JS throw is a `engines._jscompat.JsError` subclass carrying `js_name` (`Error`,
    `PackConfigError`, `RbacError`, `ApprovalError`, `OfferError`), `code` and `detail`. The commercial-history
    errors, which JS threw as plain `Error("CODE: …")`, carry the code on `code` too. Results that JS returned
    (`BLOCKED`, `REJECTED`, `INCOMPLETE`, `UNAVAILABLE`) are returned, never raised. Services map codes to
    `core.errors.DomainError`.
11. **Malformed data is not a crash contract.** Where the JS would throw a `TypeError` on malformed input (a missing
    `items` array, a `null` special work), the port treats the container as empty or raises a Python exception; the
    generator refuses to record any case that crashed the JS, so no golden depends on it. Where the JS did NOT throw
    — a division by zero is ±Infinity/NaN in JS — the port must not throw either: every JS `/` whose divisor can be
    zero goes through `_jscompat.js_div` (review R1).
12. **Golden files are minified JSON** (7.5 MB in all; git stores them zlib-compressed, ~0.5 MB). Each records the
    SHA-256 of every JS source and data file it was generated from (`sources`), the frozen clock and its synthetic
    fixtures.

## Legacy mapping

### Flarize functions → Python

| Flarize (JS) | Python |
|---|---|
| `buildBom`, `buildAllTierBoms`, `getAlternatives`, `loadCatalog` (+ `setPackConfigProvider`), `getProfileKey` | `bom_builder.build_bom`, `build_all_tier_boms`, `get_alternatives`, `load_catalog(catalog, pack_config, source)`, `get_profile_key` |
| `allocateDevices` | `device_allocation.allocate_devices` |
| `pricePack`, `structureQty`, `structureMaterial`, `tubeRateFor`, `kwOf` | `pack_pricing.price_pack`, `structure_qty`, `structure_material`, `tube_rate_for`, `kw_of` |
| `calculateCost`, `explainCost` | `cost.calculate_cost`, `cost.explain_cost` |
| `toPriceRecord`, `buildPriceMaster`, `projectUnitCost`, `procurementUplift` | `cost.to_price_record`, `build_price_master`, `project_unit_cost`, `procurement_uplift` |
| `buildBatchLandedCosts`, `toPriceMasterEntries` | `cost.build_batch_landed_costs` (and `allocate_landed`), `cost.to_price_master_entries` |
| `appendVersion` … `historyView`; `setRate`, `resolveRate`, `seedRateCardFromConfig`, `listRates` | `rate_card.append_version` … `history_view`; `set_rate`, `resolve_rate`, `seed_rate_card_from_config`, `list_rates` |
| `calculatePricing`, `resolveTierMargin`, `resolveGstRegime`, `validateGrossMargin`, `explainPricing` | `pricing.calculate_pricing`, `resolve_tier_margin`, `resolve_gst_regime`, `validate_gross_margin`, `explain_pricing` |
| `validateOffer` … `offerPayloadShape` | `offers.validate_offer` … `offer_payload_shape` |
| `toBatteryMaster` … `selectabilityReason`; `checkBatteryCompatibility`, `resolveProtectionRequirement`; `resolveBattery`, `approvedBatteryShortlist`, `isBatteryAlias`, `canonicalBatteryIdFor` | `battery_compat.*` (same names, snake case) |
| `seedFromCatalog` … `describeStore`, `marketRateKey`, `approvedSizes`, `futurePairsFor`, `validateSection` | `pack_config.*` (same names, snake case) + `validate_config` |
| `approvePackage` … `runPackageChecker`, `comboKeyFor`, `resolvePackageArchitecture`; `derivePackageComponents`, `isApprovedSelection`, `isSalesEditable`; `hydrateRegistry`, `visibleForRuntime` | `package_registry.*` (same names, snake case) |
| `roundMoney`, `sumExact`, `mulExact`, `isMoney` | `money.round_money_binary64`, `sum_exact_binary64`, `mul_exact_binary64`, `is_money_binary64` |

### Flarize data → fixture → the platform context that will own it

| Flarize file | Committed fixture key | Owner at import (PLAN §7.4) |
|---|---|---|
| `data/catalog.json` | `catalog` (whole) | catalog (components, battery rows), bom (templates), pricing (market rates, offers) |
| `data/pack-config.json` | `packStore` (whole store: approved v16, draft v17, history) | packs (`packs_config_version`) |
| `data/packages.proposed.json` | `registry` (first record per combo, fields listed in decision 3) | packs (release packs) |
| `data/battery-master.json` | `batteryMaster` (`batteries`) | catalog (battery specification) |
| `data/cost-config.json` | `costConfig` | pricing (`pricing_cost_config`, `pricing_installation_matrix`) |
| `data/procurement-price-master.json` | `priceMaster` | pricing (`pricing_price` PURCHASE/LANDED rows) |
| `data/project-rate-card.json` | `rateCard` | pricing (rates, versioned) |
| `data/procurement-state.json` batches 003/005 | `lines:BATCH-SEED-003/005` (lines only) in `commercial_landed` | procurement (`procurement_batch_line`) |

No personal data is in the fixtures (the generator refuses e-mail addresses); user references are Flarize's own
pseudonymous ids (`admin-001`). The legacy-import contract does not apply: this package owns no table.

### Flarize vocabulary → PLAN columns (for the consuming packages)

| Flarize | PLAN |
|---|---|
| offer type `flat` / `percentage` | `pricing_offer.type` FLAT / PERCENT (DV-19) |
| offer status DRAFT / APPROVED / ACTIVE / EXPIRED / ARCHIVED | `pricing_offer.status` DRAFT / ACTIVE / PAUSED / EXPIRED / ARCHIVED (DV-19) |
| pack-config draft `DRAFT` / `SUBMITTED`, `approved` copy, `history[]` | `packs_config_version.status` DRAFT / SUBMITTED / APPROVED (+ REJECTED, PUBLISHED); `rejectDraft` returns the draft to DRAFT with `rejectionReason` |
| `marketRateKey` `<sys>_<tier>[_<bat>][_up<size>]` × size | `pricing_market_rate` (system_type, tier, battery_config, size_kw) + the FR size |
| procurement `deliveryCost` + `otherProcurementCharges[]` | `procurement_batch_charge` rows (→ `allocate_landed` charges) |
| `costEngine` heads | `pricing_cost_config` keys (install/service/transport/…); installation exact `size × roof` |

## Parity evidence

* Generator: `node engines/tests/golden/generate_commercial.mjs [FLARIZE_ROOT]` (default
  `/home/user/flarize-main/flarize`, Node 22). It writes only inside `engines/tests/golden/`.
* **1 907 golden cases**, every one replayed with exact JSON equality (`engines/tests/test_commercial_golden.py`;
  the one documented deviation, DV-25, is applied to the recorded JS result by `golden_harness.EXPECTED_DEVIATIONS`):

  | Golden | Cases | Covers |
  |---|---|---|
  | `money` | 109 | `roundMoney`/`isMoney`/`mulExact`/`sumExact` incl. 1.005, ±x.5, 2⁵³+, strings, NaN |
  | `device_allocation` | 166 | exact cover, fewest units, tie bias, 8 candidate sets × 0–23 panels, invalid panels/candidates |
  | `bom` | 259 | **every pack of the approved pack config** (ongrid 6 sizes × 3 tiers + 6 future-ready pairs; hybrid 4 sizes × 3 tiers × battery null/0/1/2 + 2 FR pairs = 108 BOMs), Sales swaps and refusals, unsupported configurations, catalog-only and registry-less builds, a synthetic catalog for every slot/default/filter/allocation branch, all-tier builds, alternatives per role, a panel with `watt: '0'` (review R1) |
  | `pack_pricing` | 553 | every approved pack × FLAT/SHEET/ELEVATED with transport distances 0–1000 km, plus a full-market-rate config; swap deltas; every BLOCKED code; structure helpers for 12 kW values; zero-size structure keys and a −100 % GST rate (review R1) |
  | `pack_config` | 68 | every section valid/invalid, 6 lifecycle scenarios (submit/approve, reject/withdraw, direct approve, RBAC refusals, double submit, template patches), the real store, seeding |
  | `package_registry` | 74 | 7 registry scenarios (approve/supersede/archive, validation gates, component gate, reject/reset, authoring with generated ids, edit/submit, bulk/list/selectable), projection, authority derivation for every profile |
  | `cost` | 90 | full BOM snapshots of approved packs; 45 head-by-head variants (every error code, rate cards: seeded, real, banded, effective dates) |
  | `landed` | 27 | multi-charge allocation (incl. the 257-line seed batches), reconciliation, every refusal |
  | `pricing` | 95 | real cost results with discounts, tiers and market-rate references; every error code; GST regimes |
  | `offers` | 140 | full transition matrix (valid/invalid), validation, margin safety, edits, auto-expiry, applicable-offer ranking |
  | `battery` | 285 | compatibility matrix (7 batteries × 4 inverters × 3 architectures), master projection, resolution (9 profiles × master/legacy × project quantities × overrides), shortlists, aliases |
  | `rate_card` | 41 | seeding, resolution by date, supersede/archive, history, comparisons |

* §20 reference outputs asserted literally (`engines/tests/test_commercial_reference.py`): ongrid 3 kW Value BOM
  (p2 × 6 @ 13 585, i20 @ 18 250 …, matTotal 123 116.3, allGst 9 195, grand 132 311.3); pack price FLAT 229 000 /
  210 285 + 18 715, transport 1 050, total 230 050, reference total 168 588, grand 220 311, margin vs market 8 689
  (3.9 %); SHEET add-on 13 542 + 1 205 = 14 747 → 243 747 / 244 797; swap p2 → p7 −1 722 − 86 = −1 808 → 227 192;
  GST 70 × 5 % + 30 × 18 % = 8.9 %; site survey 130 km = 590; landed 5 000 over 70k/20k/10k = 3 500/1 000/500.
* Coverage of `engines/` by these tests: 98 % (branch coverage on).
* Sources the goldens were generated from (SHA-256 prefixes; full hashes in every golden file):

  | File | SHA-256 |
  |---|---|
  | `server-bom-builder.js` | `b48cade388bf2fe5` |
  | `src/lib/deviceAllocation.js` | `5f075a1eea7f3bf8` |
  | `src/lib/packPricing.js` | `4bfc4da38a8cf7ea` |
  | `src/lib/packConfig.js` | `651b12271b41aaa0` |
  | `src/lib/costEngine.js` | `b6ee89dcbf618f24` |
  | `src/lib/pricingEngine.js` | `8b9e5db2113df46a` |
  | `src/lib/offerLifecycle.js` | `876cb8d743f564bc` |
  | `src/lib/batteryCompatibility.js` / `resolveBattery.js` / `batteryMaster.js` | `76dfe69bfaab50ba` / `fbc9890d9d571ad8` / `e872ac9f7c038289` |
  | `src/lib/procurementBatch.js` / `procurementPriceMaster.js` | `3900adf7963bf333` / `f9d93971f3fb50c9` |
  | `src/lib/projectRateCard.js` / `commercialHistory.js` | `99d86edeaeecea46` / `8fc934922951e87c` |
  | `src/lib/packageApproval.js` / `packageAuthority.js` / `packageProjection.js` | `8fd5ca1ed979c702` / `3ec2b043782c9b38` / `941d3b30324010c4` |
  | `src/lib/money.js` / `rbac.js` | `40fdfa301e71ec4f` / `78e4bb6282ae0126` |
  | `data/catalog.json` / `pack-config.json` / `packages.proposed.json` | `f971226cf1765f66` / `0cab5965faa97acb` / `c2d7961b624148cb` |
  | `data/cost-config.json` / `procurement-price-master.json` / `project-rate-card.json` | `4a63d8e08a7bf0b9` / `f775957d8a2b1d56` / `a6315d96f24292cc` |
  | `data/battery-master.json` / `procurement-state.json` | `896aa0f5b506c7d3` / `4e46c60925078c3f` |

## Hand-over notes

* **pricing**: `LIST` prices come from `pricing.gross_margin_list_price` (also exposed as
  `cost.gross_margin_list_price`, where PLAN §2.3 places the gross-margin check); pack prices from
  `pack_pricing.price_pack(config=<PackConfig JSON>, lines=build_bom(...)["lines"], …)`; the publish report can reuse
  the `BLOCKED` error codes (`MARKET_RATE_NOT_SET`, `INSTALLATION_NOT_SET`, …). Offers: map PLAN FLAT/PERCENT to
  `flat`/`percentage` before calling `engines.offers` (DV-19); the engine matches `appliesToSize` against the size
  KEY (`'3'`, `'5sp'`, `'5tp'`), which PLAN's numeric `applies_to_size_kw` cannot tell apart for 5 kW 1P/3P. Every
  engine input is JSON-like: pass `Decimal` columns through `engines._jscompat.json_numbers` first (a `Decimal` is not
  a JS number to `is_num`, so e.g. a Decimal market rate prices as `MARKET_RATE_NOT_SET`).
* **procurement**: commit = `cost.allocate_landed(lines, charges, batch_id=…)`; write PURCHASE from
  `purchaseUnitPrice` and LANDED from `landedUnitCost` (or `landedUnitCostExact` quantized); refuse the commit when
  `ok` is false (`errors[0].code`). The charges are allocated in whole rupees (`roundMoney` of their sum, the JS
  rule): a batch whose charges carry paise allocates their rounded sum, so compare `allocation.allocatedTotal` with
  the rounded `charges_total`, not the paise figure. Quantities must be positive (`BATCH_LINE_INVALID`): a reversing
  batch (PLAN §2.4 corrections) cannot be allocated by this function and needs its own rule in the procurement package.
* **packs**: validate `packs_config_version.config` with `pack_config.validate_config` (exactly the ten sections, each
  valid: an unknown key is `INVALID_SECTION`, a missing section `INVALID_VALUE`); build the Flarize-shaped store
  (`{"schema","approved","draft","history"}`) from the approved and draft rows to reuse the lifecycle functions, pass
  `authorize=allow_all` after the registry check, persist what they return. `create_package` / `duplicate_package` /
  `create_revision` draw ids from `PackageIdFactory`; its sequence is thread-safe but per process, so a service with
  several worker processes passes `id_factory=` a factory that is unique across processes (e.g. one built on a uid).
* **bom / quotations**: `build_bom` needs the catalog in the Flarize shape (categories → items with `id`, `tiers`,
  `price`, `watt`, `kw`, `phase`, `type`, `status`, `deviceType`, `panelsPerDevice`, `microAccessoryRole`, …),
  `packageProfiles`, and the pack config; `BomBuildError.code` distinguishes Sales refusals from bad requests.
  `actorRole` is Flarize's `rbac.1` vocabulary: pass `SALES` (or `SALES_HEAD`/`SALES_CRS`/`FIELD_SALES`) for a caller
  held to the Project Head's swap rules and `PROJECT_HEAD`/`ADMIN` for an unrestricted one — any other string (a
  platform role slug) is treated as Sales (DV-26); omit it only for internal builds (publishing). Sales-facing lines go
  through `strip_cost_fields_for_sales`, which also removes `defaultUnitPrice` (DV-25); `totals` and the
  `pack_pricing` `internal` block must be dropped by the service for Sales. Results never alias the catalog, registry
  or pack config passed in.
* **calculators / quotations**: offers via `find_applicable_offer(offers, system_type=…, tier=…, size=…, date=…)`;
  D-4 printing via `calculate_offer_amount`.

## Merge notes

* **Done at the wave-1 integration:** the float rule moved into `engines/money.py` as its binary64 section
  (`round_money_binary64` …); `cost.py`, `pricing.py`, `tests/golden_harness.py` and `tests/test_commercial_units.py`
  import it under the old names and `engines/_money_compat.py` is gone. The commercial engines still never call the
  Decimal API (the note below explains why); all 1 907 goldens replay unchanged.
* (Branch note, superseded by the consolidation above.) **Keep `engines/_money_compat.py` when engines-core lands — do NOT switch these engines to `engines.money`.**
  engines-core's `engines.money` (branch `wp/engines-core`) is Decimal-only: `round_money`, `is_money`, `sum_exact`
  and `mul_exact` raise `TypeError` for a `float` and return `Decimal`. The commercial engines compute in doubles for
  golden parity (DV-17) and call the money rule with floats on every path, so switching the four import sites
  (`cost.py`, `pricing.py`, `tests/golden_harness.py`, `tests/test_commercial_units.py`) would break all of them (the
  1 907 golden cases fail at once). The two modules do not collide (different names) and implement the same
  `money.js` rule on different number types; they coexist after the merge. (The original note said to switch the
  imports and delete this module; the engines-core review showed its API refuses floats.)
* **Done at the wave-1 integration:** one port of `batteryMaster.js`/`batteryCompatibility.js` — `battery_compat`'s
  `to_battery_master`, `check_battery_compatibility` and `resolve_protection_requirement` delegate to
  `engines.engineering_checker` (engines-rules) through `_to_rules`/`_from_rules` (JavaScript numbers ↔ Decimal, the two
  `UNDEFINED`s); the 132 commercial battery goldens and the rules goldens replay through the same code. `_jscompat` takes
  `js_trim`, the number-literal patterns and `is_array_index` from `engines.jscompat`: the RV-1 class of defect found in
  engines-rules (Unicode digits read as numbers — `Number('൩')` gave 3, a Malayalam-digit battery voltage passed BC-C;
  `'1\n'` read as array index 1) is gone from the commercial copy too (`test_commercial_units.py::test_digits_are_ascii`,
  `::test_array_indexes_are_canonical_ascii_integers`, `TestOneBatteryPort`).
* `engines/_jscompat.py` is private to `engines`; engines-core ships its own Decimal helpers (`money.js_number`,
  `money.js_round`, `money.js_text`) with the Decimal contract — they are not interchangeable with these float ones.
* DEVIATIONS numbering: the sibling branches already use DV-17 … DV-24 (`wp/engines-ops` DV-17/18, `wp/engines-core`
  DV-21/22, `wp/hr`, `wp/leads-customers`, `wp/catalog`, `wp/content-*`, `wp/careers-reference`); this package's
  DV-17 … DV-20, DV-25, DV-26 are renumbered when the branches meet.
* No shared code (`flarize/`, `core/`, `accounts/`) was changed.

## Review (adversarial pass)

Each finding was reproduced by a failing test first (`engines/tests/test_commercial_review.py`, and new golden cases
recorded from the real JavaScript); the tests stay. A differential fuzzer (≈ 69 000 random cases through the real JS
and the port: `pricePack`, `structureQty`/`structureMaterial`, `buildBom`/`getAlternatives` on the real and synthetic
catalogs, `calculateCost`, `calculatePricing`, landed allocation, device allocation, offers, battery compatibility and
resolution, rate cards, pack-config validation and 300 random package-registry scenarios) found R1 and nothing else;
its only other divergences are the intended DV-26 ones.

| # | Finding | Fix |
|---|---|---|
| R1 | A JS division by zero (±Infinity / NaN) raised `ZeroDivisionError` in the port: a structure `qty` size key `"0"` (accepted by `validateSection`), a panel `watt` of `"0"`, a GST `ratePct` of −100 | `_jscompat.js_div` at every division whose divisor can be zero (`structure_qty`, panel count and `defaultQty`, the pre-GST split); golden cases `structureQty/zero-size/*`, `structureMaterial/zero-size`, `price/edge/gst-minus-100`, `bom/zero-watt/*` |
| R2 | `validate_config` — the `packs_config_version.config` schema (PLAN §2.5) — accepted unknown top-level keys (a `marketRate` typo was stored and priced as "market rate not set") and configs missing sections (the BOM builder then silently used the catalog's copy) | exactly the ten sections: unknown key `INVALID_SECTION`, missing section `INVALID_VALUE` (`detail.missing`) |
| R3 | The merge note told the integrator to replace `_money_compat` with engines-core's `engines.money`, which refuses floats — the switch would break every commercial engine | merge note corrected (above); `_money_compat` stays |
| R4 | `build_bom` results aliased the caller's inputs: every line's `alternatives` was the pack config's (or registry's) own list and `profile` the catalog's own record, so a service mutating a result corrupted its cached catalog/config for every later build (the JS re-read the catalog file per call) | deep copies at the output (`alternatives`, `profile`, the `SELECTION_NOT_APPROVED` detail) |
| R5 | `PackageIdFactory` read and bumped its sequence without a lock: 8 threads × 150 ids in one millisecond produced duplicate package ids (180 in 20 runs); `_find_package` then resolves a duplicate id to the wrong record | the sequence is taken under a lock; multi-process uniqueness is the service's `id_factory` (hand-over note) |
| R6 | PLAN §2.3 places the gross-margin check in `engines.cost`; it existed only as `engines.pricing.gross_margin_list_price` | re-exported from `engines.cost` (`gross_margin_list_price`, `validate_gross_margin`, `PricingError`) |
| R7 | `strip_cost_fields_for_sales` removed `unitPrice` but left `defaultUnitPrice`, which equals it on every unswapped line: Sales read every component price (a JS bug, ported faithfully) | `defaultUnitPrice` is stripped too (DV-25); the golden harness applies the deviation to the recorded JS result (`EXPECTED_DEVIATIONS`) |
| R8 | `build_bom` / `get_alternatives` gave any `actorRole` outside Flarize's vocabulary Project Head powers: a platform role slug (`sales-executive`) could swap any component past the approved list and the locked slots, and read reference prices — contrary to spec §6 ("a Sales caller, or an unknown role, sees only swappable slots") | unknown roles are held to the Sales rules (`held_to_sales_rules`, DV-26); a missing role keeps the JS behaviour |

