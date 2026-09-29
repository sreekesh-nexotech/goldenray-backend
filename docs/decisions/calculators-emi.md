# calculators-emi — the website calculators and the EMI calculator

Work package calculators-emi builds the `calculators` and `emi` apps: faithful ports of the three website calculators
(`calculate-solar/`, `calculate-solar-new/`, `calculate-solar-advanced/`) and of the EMI calculator (`emi-calculator/`,
`…/config/`, `…/quotation/`) as pure engines (`engines/website_calculators.py`, `engines/emi.py`), exposed on the
public surface (PLAN §3.3 `calculators/*`), their staff configuration (PLAN §3.4 `emi/*` + the sizing tables), the
tables of PLAN §2.8 `emi_*` and the legacy imports of §7.3 (`emi_*`, `solar_installations`, `solar_installation_new`).
Deviations: DV-74 … DV-79 in `docs/DEVIATIONS.md`.

## What exists

### Tables

| Table | Model | Notes |
|---|---|---|
| `calculators_capacity_size` | `calculators.CapacitySize` | ← legacy `solar_installations` (DV-75): `power_capacity_kw` numeric(7,3), `installation_days`, `total_cost`, `total_subsidy`, `area_required_sqft`, `is_active`. PU(`power_capacity_kw`) (the legacy `.get(power_capacity=…)` crashed on duplicates); checks: kW > 0, money ≥ 0. |
| `calculators_bill_range_size` | `calculators.BillRangeSize` | ← legacy `solar_installation_new` (DV-75): `bill_range`, `property_type` (RESIDENTIAL/COMMERCIAL; the label is what the responses print), `power_capacity_kw`, `installation_days_range` (text, "3-7"), `total_cost`, `total_subsidy`, `area_required_sqft`, `loan_available` (text), `per_kw_rate`, `final_cost`, `interest_rate` (fraction), `inverter_price`, `is_active`. PU(`bill_range`, `property_type`); checks: enum, bill range > 0, kW > 0, money ≥ 0, rate a fraction. Index (`property_type`, `bill_range`). |
| `emi_bank` | `emi.Bank` | PLAN + every legacy column (DV-76): `name`, `abbr`, `slug` (PU, slug format), `logo_bg` (hex check), `annual_rate` (fraction), `min_loan`/`max_loan` (ordered unless max 0), `upfront_requirement`, `eligibility`, `cibil_required`, `processing_fee_pct` (fraction), `processing_fee_note`, `approval_min_days`/`approval_max_days` (ordered), `max_tenure_years`, `features` (varchar[]), `best_for`, `is_recommended`, `sort_order`, `is_active`. |
| `emi_interest_rate_rule` | `emi.InterestRateRule` | `label`, `min_kw`/`max_kw`, `min_system_cost`/`max_system_cost`, `min_amount`/`max_amount` (the loan band: price − down payment), `annual_rate` + `min_annual_rate` (fractions; rate ≥ floor), `is_locked`, `priority`, `is_active`, `effective_from`/`effective_to` (PLAN). Every band ordered by a check. Ordering = the legacy query order (`-priority`, `min_kw`, `min_system_cost`, `min_amount`, NULLs last). |
| `emi_subsidy_rule` | `emi.SubsidyRule` | `scheme` (PM_SURYA_GHAR/OTHER), `label`, `kw_from`/`kw_to`, `amount` (flat), `amount_per_kw`, `cap_amount` (PLAN), `priority`, `is_active`, `effective_from`/`effective_to`. |
| `emi_settings` | `emi.EmiSettings` | singleton (unique index on a constant among live rows): tenure band/default (years), `daily_saving_divisor`, `price_step`, `down_payment_min_pct`/`max_pct`/`step_pct` (fractions), `down_payment_quick_adds` (numeric[]), `rate_max`, `default_annual_rate` (fractions), `panel_life_years`, `disclaimer_en`/`disclaimer_ml` (PLAN). Checks: tenure min ≤ default ≤ max, 0 < min ≤ max ≤ 1, steps > 0, rates fractions. |
| `emi_system_size` | `emi.SystemSize` | transitional price source (DV-76): `label`, `capacity_kw` (PU), `price_per_kw` (> 0), `price_min`/`price_max` (ordered), `monthly_bill_reference`, `sort_order`, `is_active`. |

### Endpoints

| Surface | Path | Permission / throttle / cache |
|---|---|---|
| public | `POST calculators/basic/` (legacy `calculate-solar/`) | `public_read`; `Cache-Control: no-store`; the table snapshot is cached (below) |
| public | `POST calculators/basic-v2/` (legacy `calculate-solar-new/`, what the website calls) | same |
| public | `POST calculators/advanced/` (legacy `calculate-solar-advanced/`) | same |
| public | `GET calculators/emi/config/` | `public_read`; cached 300 s server-side under `emi:config` (+ the price source's namespaces), ETag/304, `Cache-Control: public, max-age=60` |
| public | `POST calculators/emi/`, `POST calculators/emi/quotation/` | `public_read`; `no-store`; the configuration snapshot is cached |
| staff | `calculators/capacity-sizes/`, `calculators/bill-range-sizes/` list/detail/create/`PATCH`/`DELETE` (soft) | `reference_data` view/create/edit/archive |
| staff | `emi/banks/`, `emi/interest-rules/`, `emi/subsidy-rules/`, `emi/system-sizes/` list/detail/create/`PATCH`/`DELETE` (soft) | `emi` view (reads) / edit (every write — the registry has no other action) |
| staff | `emi/settings/` `GET`/`PATCH` | `emi` view / edit |

Every PATCH/DELETE takes `expected_version` (409 `stale_version`); live duplicates are 409 `<entity>_exists`
(`capacity_size`, `bill_range_size`, `bank`, `system_size`); cross-field rules are 400 `validation_error` naming the
field. Record scope: both modules allow only `all` (PLAN §3.2).

### Engines and services

* `engines/legacy_lookups.py` — how the legacy ORM prepared request values before comparing them (text `exact` =
  `str(v)`; PostgreSQL `iexact` = `UPPER(col) = UPPER(v)` for a text — PostgreSQL's one-for-one case mapping
  (`pg_upper`: `ﬆ`/`ß` stay, `ᾳ` → `ᾼ`), not Python's `str.upper()` — and a crash for anything else; integer `lte`
  truncates and `gte` rounds a float up, int32 overflow = every row/no row; `DecimalField` rounds a float to the
  column's `max_digits`; NUL or a lone surrogate in a text and non-finite decimals crash; `inf`/`nan` or a
  non-UTF-8 text in a response crash the renderer).
* `engines/website_calculators.py` — `basic`, `basic_v2`, `advanced` (+ `emi_with_interest` = legacy
  `utils/finance.emi_calc`, `emi_advanced` = the advanced view's own `emi_calc`), line-by-line ports over plain lookup
  tables (`CalculatorData`).
* `engines/emi.py` — `calculate` (view parsing + `utils/emi.calculate` + the flattened legacy keys), `quotation`
  (view + `quotation_breakdown`), `resolve_subsidy`, `resolve_interest_rule`, `resolve_rate`, `resolve_rate_unlock`,
  `find_system_size` over an `EmiConfig`.
* `calculators.services`: `sizing` (staff writes), `data` (the cached snapshot), `batteries` (DV-78), `calculate`
  (engine → `DomainError`), `import_support` + `legacy_import`.
* `emi.services`: `rows` (staff writes of the four lists), `settings` (singleton), `price_sources` (DV-76),
  `calculator` (snapshot, public config, calculate, quotation), `legacy_import`; `emi/checks.py` (`emi.E001`,
  `emi.W001`).

## Decisions not spelled out in the PLAN

1. **Faithful ports, not reinterpretations (DV-74).** Acceptance is "calculator outputs equal old endpoints for a
   recorded set of 200 inputs" (PLAN §4.5, §7.6 #10), so the engines keep the legacy binary64 arithmetic in the legacy
   operation order and the legacy value parsing (a JSON body is read raw: `int("5000")`, `float("30")`, `x or y`
   fallbacks, `str()` of a non-text pincode). Reuse of `engines.finance` / `engines.subsidy` / `engines.energy` was
   checked and rejected because the formulas are not identical: `engines.finance` rounds the EMI to whole rupees
   (`js_round`) in a 60-digit Decimal context where the legacy rounds a binary64 EMI to paise (`round(x, 2)`);
   `engines.subsidy` is the PM Surya Ghar per-kW tier scheme with DCR/connection rules where the legacy uses one flat
   amount per kW band from a table; `engines.energy` bills units with fixed charge, duty and meter rent by phase where
   the legacy basic calculator subtracts ₹197.08 and walks five slabs, and the advanced one divides by one slab rate.
2. **A legacy crash is a 400.** Whatever raised an unhandled exception in a legacy view (a non-object body, a text where
   a number is needed, `1e400`, a NUL character, a missing tariff table, `final_cost` NULL, a JSON `inf` result) is
   `400 invalid_input` ("The calculator cannot process these inputs." / "The EMI calculator cannot process these
   inputs."), with the cause logged at INFO (cut to 300 characters: the cause often quotes the visitor's value).
   A body nested too deeply for the JSON parser (legacy `RecursionError`, 500) is `400 parse_error` (shared
   `flarize.parsers.JSONParser`). Every legacy 4xx keeps its status and its text as `message`; codes:
   `missing_fields`, `invalid_monthly_bill`, `pincode_not_found` (404), `bill_out_of_range`, `no_sizing_row` (404),
   `unsupported_grid_type`, `no_matching_installation` (404), `invalid_number`, `size_required`, `invalid_request`
   (the legacy `ValueError` texts), `invalid_packages`, `too_many_packages`, `capacity_required`, `invalid_tenure`,
   `invalid_package`, `emi_prices_unavailable` (503).
3. **Cached snapshots, not per-request queries.** The legacy views queried the database per device, per slab, per
   rule. The calculators load every table they read into one `CalculatorData` / `EmiConfig` snapshot cached under the
   namespaces of everything in it (`calculators:sizes`, `reference:tariffs|device-types|ev-cars|ev-scooters|pincodes`,
   `catalog`, `pricing`; `emi:config` + the price source's), keyed by the day (tariff schedules and rule dates change at
   midnight). A warm calculation costs no query (tested); any staff write or import bumps a namespace and is visible
   at once. A cache outage falls back to the database.
4. **Throttle scope `public_read` for the calculator POSTs.** They compute and write nothing; the EMI page posts on
   every slider move, which `public_write` (20/min/IP) would cut off. Responses are `Cache-Control: no-store`.
5. **Reference data through `reference.services.lookups`.** Three whole-list reads were added there (shared change,
   additive): `active_pincode_codes`, `active_device_types`, `active_vehicles`. Only active rows are used (an
   inactive device type, EV, pincode or sizing row is as absent). The KSEB schedule is the one in force today for a
   single-phase connection (the imported legacy slabs are the every-phase schedule); `.first()` without ordering in
   the legacy (pk order) is reproduced with the reference `sort_order` (= legacy id).
6. **Percentages are fractions in the database, percent on the website.** Interest rates, processing fees and the
   down-payment band are stored as fractions (CLAUDE.md); the staff API speaks fractions (`"0.0575"`); the public
   payloads and the engines use the legacy percent values (`"5.75"`, `5.75`), converted exactly (a 2-place percent is
   a 4-place fraction).
7. **Batteries (DV-78).** The hybrid option picks from the website's published battery products (catalog profile
   PUBLISHED, public, ACTIVE/DEPRECATED, `battery_spec.capacity_kwh` set) priced by the price provider
   (`catalog.services.pricing_hooks`: the pricing package's current LIST price; `min_amount`, else `max_amount`). A
   battery without a price is not offered. Until the pricing package registers its provider, the hybrid option
   answers "No battery found …" as the legacy did with an empty table.
8. **EMI price source (DV-76).** `EMI_PRICE_SOURCE` (`flarize/settings/base.py`, env) is `MANUAL` (default: the
   `emi_system_size` rows) or `PACK_RELEASE` (the provider registered with
   `emi.services.price_sources.register(fn, cache_namespaces=(…))`, returning `SizeOption`s — a pack may give a
   lump-sum `system_cost`). `PACK_RELEASE` without a provider fails the deploy check (`emi.W001` under
   `--fail-level WARNING`) and answers 503 `emi_prices_unavailable`; it never falls back silently to manual prices.
9. **Settings singleton.** Reads serve the defaults without writing (`uid`/`updated_at` null, version 1); the first
   edit that changes something creates the row (audited `emi.settings_created`) and edits it (version 2), so a client
   holding the defaults' version 1 gets `stale_version` rather than overwriting a concurrent first edit. Quick-add
   amounts are de-duplicated and sorted (legacy `validate_down_payment_quick_adds`). The legacy Studio's validation
   rules are kept in `emi.services.settings.validate` and in DB checks.
10. **Rules have effective dates** (PLAN): a rule outside `effective_from`–`effective_to` on the day is ignored; the
    legacy rows import with no dates (always in force). A subsidy is `amount + amount_per_kw × kW`, capped at
    `cap_amount` — the legacy rows import with 0 and no cap, which is exactly the legacy amount.
11. **No outbox events.** Nothing outside these apps reacts to calculator configuration and the website reads the EMI
    config client-side; the public payloads are invalidated through cache namespaces. Writes are audited
    (`calculators.<entity>_created|updated|deleted`, `emi.<entity>_…`, `emi.settings_…`, `*.legacy_imported`).
12. **Import helpers are shared by the two apps** (`calculators.services.import_support`: `Report`, `upsert` through
    `core_legacy_map`, exact-decimal/whole-number readers, `run`): the EMI importer uses them (emi → calculators is a
    documented, one-way dependency between the two website-calculator apps).

## Legacy mapping

### `solar_installations` → `calculators_capacity_size` (BACKEND / `solar_installations`)

| Legacy | Platform | Rule |
|---|---|---|
| `id` | `core_legacy_map.source_id` | |
| `power_capacity` (float) | `power_capacity_kw` numeric(7,3) | > 0, at most 3 decimals (else violation) |
| `time_to_complete` (int, days) | `installation_days` | whole, ≥ 0 |
| `total_cost`, `total_subsidy` | same | numeric(14,2) exactly, ≥ 0 |
| `area_required` | `area_required_sqft` | whole, ≥ 0 |
| `created_at`, `updated_at` | same | preserved |
| — | `is_active` true | |

### `solar_installation_new` → `calculators_bill_range_size` (BACKEND / `solar_installation_new`)

| Legacy | Platform | Rule |
|---|---|---|
| `bill_range` | `bill_range` | whole, ≥ 1 |
| `type` (text, nullable) | `property_type` | Residential/Commercial any case (a case difference is reported); anything else is not imported |
| `power_capacity` (float) | `power_capacity_kw` | as above |
| `time_to_complete` (text) | `installation_days_range` | kept as text ("3-7") |
| `total_cost`, `total_subsidy`, `per_kw_rate`, `final_cost`, `inverter_price` | same | numeric(14,2) exactly; the last three nullable |
| `interest_rate` (%, numeric(5,2), nullable) | `interest_rate` (fraction, numeric(6,4)) | ÷ 100, exact |
| `loan_available` | `loan_available` | text; its first number is the graph's loan (legacy parsing) |
| `area_required` | `area_required_sqft` | |
| `created_at`, `updated_at` | same | preserved |

### `batteries` → catalog (imported by the catalog package; read by the advanced calculator)

`battery_capacity` → `catalog_battery_spec.capacity_kwh`; `battery_price` → the LIST price the catalog importer
returns for the pricing package, served by the price provider; `backup_hour` → `battery_spec.backup_hours` (not
read by the legacy calculator). See docs/decisions/catalog.md.

### Reference tables the calculators read (imported by the reference package)

`kseb_tariffs` (`min_units`, `max_units`, `rate`) → `reference_kseb_tariff` (`slab_from_units`, `slab_to_units`,
`rate_per_unit`); `device_types` (`name`, `watts`, `k_value`) → `reference_device_type`; `ev_cars`/`ev_scooters`
(`model`, `energy_consumption`, `k_value`) → `reference_ev_car`/`reference_ev_scooter`; `pincodes` (per office) →
`reference_pincode` (a pincode exists when any of its offices did). See docs/decisions/careers-reference.md.

### `emi_bank` → `emi_bank` (BACKEND / `emi_bank`)

| Legacy | Platform | Rule |
|---|---|---|
| `name`, `abbr`, `slug`, `logo_bg` | same | slug and hex colour validated |
| `interest_rate` (%) | `annual_rate` (fraction) | ÷ 100 exact |
| `min_loan`, `max_loan` | same | |
| `upfront_requirement`, `eligibility`, `processing_fee_note`, `best_for` | same | |
| `cibil_required`, `approval_min_days`, `approval_max_days`, `max_tenure_years`, `sort_order` | same | whole numbers |
| `processing_fee_percent` (%) | `processing_fee_pct` (fraction) | ÷ 100 exact |
| `features` (JSON list) | `features` varchar[] | a list of texts ≤ 255, else violation |
| `is_recommended`, `is_active`, `created_at`, `updated_at` | same | |

### `emi_interest_rate_rule` → `emi_interest_rate_rule`

| Legacy | Platform |
|---|---|
| `label`, `min_kw`, `max_kw`, `is_locked`, `priority`, `is_active`, timestamps | same |
| `min_cost`, `max_cost` | `min_system_cost`, `max_system_cost` |
| `min_loan`, `max_loan` | `min_amount`, `max_amount` (PLAN names; the loan band) |
| `rate`, `min_rate` (%) | `annual_rate`, `min_annual_rate` (fractions) |
| — | `effective_from`, `effective_to` null |

### `emi_subsidy_rule` → `emi_subsidy_rule`

`label`, `priority`, `is_active`, timestamps → same; `min_kw`/`max_kw` → `kw_from`/`kw_to`; `amount` → `amount`;
— → `scheme` PM_SURYA_GHAR, `amount_per_kw` 0, `cap_amount` null, effective dates null.

### `emi_calculator_settings` (row 1) → `emi_settings`

`tenure_min_years`, `tenure_max_years`, `tenure_default_years`, `daily_saving_divisor`, `price_step`,
`panel_life_years` → same; `down_payment_min_percent`/`max_percent`/`step_percent` (%) →
`down_payment_min_pct`/`max_pct`/`step_pct` (fractions); `down_payment_quick_adds` (JSON list) → numeric[];
`rate_max` (%) → `rate_max` (fraction); `default_interest_rate` (%) → `default_annual_rate` (fraction);
`updated_at` → `created_at` and `updated_at`; — → disclaimers empty. A platform settings row that did not come from
the import (a Studio edit before the first import) is kept and reported.

### `emi_system_size` → `emi_system_size` (DV-76: kept, not "not migrated")

Every column 1:1 (`label`, `capacity_kw`, `price_per_kw`, `price_min`, `price_max`, `monthly_bill_reference`,
`sort_order`, `is_active`, timestamps). The comparison "price it held vs the PackRelease price" of PLAN §7.3 is due
when packs publishes its first release and registers the `PACK_RELEASE` provider.

### Endpoints

| Legacy | Platform | Contract change |
|---|---|---|
| `POST /api/calculate-solar/` | `POST /api/public/v1/calculators/basic/` | errors in the envelope; 500 → 400 |
| `POST /api/calculate-solar-new/` | `POST …/calculators/basic-v2/` | same |
| `POST /api/calculate-solar-advanced/` | `POST …/calculators/advanced/` | same |
| `GET /api/emi-calculator/config/` | `GET …/calculators/emi/config/` | `uid` for `id` (sizes, banks); settings add `disclaimer_en`/`disclaimer_ml` |
| `POST /api/emi-calculator/` | `POST …/calculators/emi/` | request `size_uid` for `size_id`/`installation_id`/`id`; response `system.size_uid`, `interest.rule_uid`; the "Provide either …" text names `size_uid` |
| `POST /api/emi-calculator/quotation/` | `POST …/calculators/emi/quotation/` | none besides the envelope |
| `/api/emi-admin/{system-sizes,subsidies,interest-rates,banks,settings}/` | `/api/v1/emi/{system-sizes,subsidy-rules,interest-rules,banks,settings}/` | platform auth/RBAC, fractions, `uid`, soft delete, versions (no shim: Studio moves) |
| `/api/solar-installations[-new]/` (legacy CRUD) | `/api/v1/calculators/{capacity-sizes,bill-range-sizes}/` | same as above |

## Parity evidence

Captured by `calculators/tests/parity/capture_legacy.py` and `emi/tests/parity/capture_legacy.py` (committed) on
2026-09-29:

* **`uat`** — the shared UAT server `http://127.0.0.1:18012` (`legacy_goldenapp`; every request only reads).
* **`enriched`** — a private restored copy `legacy_goldenapp_calculators_emi` served on `:18161` with
  `calculators/tests/parity/enrich_private.sql`: NULL `final_cost` / `interest_rate` / `inverter_price`, fractional and
  extra sizes, three more batteries, devices without watts or with `k_value` 0, an EV without consumption, and an EMI
  policy where the kW, system-cost and loan bands, locked and floating rates, priorities and specificity compete (six
  distinct winning rules), an overlapping subsidy rule, sizes without slider room, an inactive size/bank/rule, and
  changed settings (15 % minimum down payment, divisor 31, fractional quick-add).

| Corpus | Inputs (uat / enriched) | Legacy statuses (uat) |
|---|---|---|
| `calculators/basic` | 213 / 213 | 169 × 200, 22 × 400, 16 × 404, 6 × 500 |
| `calculators/basic_v2` | 236 / 236 | 175 × 200, 29 × 400, 21 × 404, 11 × 500 |
| `calculators/advanced` | 269 / 269 | 227 × 200, 7 × 400, 35 × 500 |
| `emi/calculate` | 260 / 260 | 191 × 200, 56 × 400, 13 × 500 |
| `emi/quotation` | 249 / 249 | 218 × 200, 29 × 400, 2 × 500 |
| `emi/config` | 1 / 1 | 200 |

The recorded legacy 500s (the corpora keep the exception class the legacy debug answer named) cover every crash
family the ports reproduce as `invalid_input`: `AttributeError` (a body, section or preference that is not an object;
`None.lower()`; a missing tariff), `TypeError` (`float(None)` of a NULL `final_cost`, a list where a number is needed),
`ValueError` (`int("two")`, `float("nan")` into an integer lookup), `OverflowError` (`int(1e400)`), `ProgrammingError`
(`UPPER(5)`: a non-text property type in PostgreSQL `iexact`), `ValidationError` (a non-finite float in a
`DecimalField` lookup) and `InvalidOperation` (a 1e30 price quantized to paise).

The grids are deterministic: every pincode class (14 Kerala codes across districts, 5 unknown codes, number,
float, spaces, 5/7 digits, letters, booleans, null, list, object, NUL, full-width digits), property types (every case,
unknown, wildcards, the long ſ, non-texts), bill boundaries around every slab edge and every bill band ± 1 plus
sweeps, invalid bills (`1e400`, texts, `5_000`, Arabic-Indic digits, lists …), every device type and EV model plus
wrong-case/unknown/NUL names and value classes, device/EV mixes, grid/home types and bill frequencies, backup
preferences (hours classes, preference devices, battery thresholds including a fractional one), every EMI size id and
capacity (incl. 6-digit rounding), tenures, rate/price/down-payment adjustments and toggles, combinations across the
loan bands (rate-unlock suggestions), quotation packages (costs × subsidies, capacities × tenures, invalid packages,
limits), and malformed bodies.

`calculators/tests/test_parity.py` and `emi/tests/test_parity.py` import the same legacy rows (`legacy_rows.json`,
exported read-only) through the owners' importers — reference, calculators, catalog (batteries, with the prices the
catalog importer returns served by a stand-in price provider) and emi — replay every recorded request against the
canonical endpoint (EMI ids translated to uids through `core_legacy_map`, `emi/tests/parity/translate.py`) and compare:
**every 200 body identical** (integers and floats kept apart, `repr` of every float), every 4xx with the same status
and the legacy text as `message`, every legacy 500 a 400 `invalid_input`. Result: **2,456 of 2,456 recorded requests
answer as the legacy** (718 calculator and 509 EMI inputs per variant, plus the two configs).

Approved differences (the only ones): legacy 500 → 400 `invalid_input`; errors in the platform envelope; `uid`
instead of integer ids (`size_uid`, `rule_uid`, config `uid`), and "Provide either size_uid or capacity_kw";
the config's settings carry `disclaimer_en`/`disclaimer_ml`; the config prints a whole quick-add amount as an
integer (the legacy printed the JSON as stored: integers when seeded, floats after a Studio save).

Engine-level tests (`engines/tests/test_website_calculators.py`, `engines/tests/test_emi.py`) pin each legacy rule
separately, including those no recorded data can reach (empty tariff table, zero-rate slab, int32 overflow).

## Review fixes (calculators-emi review)

Each was reproduced first (against the legacy UAT server, the legacy database or the platform) and is pinned by a
test that failed before the fix: `engines/tests/test_calculators_review.py`, `calculators/tests/test_review.py`,
`emi/tests/test_review.py`.

| # | Finding | Fix |
|---|---|---|
| 1 | `iexact` used Python's `str.upper()` (full case mapping: `"Toaﬆer".upper() == "TOASTER"`); the legacy PostgreSQL `UPPER()` (C.UTF-8) maps one character to one, so a ligature/`ß` device or property-type name matched a row the legacy never matched (advanced: 1000 W of a "Toaﬆer" counted; legacy 0 W) | `legacy_lookups.pg_upper`: the simple mapping (a multi-letter upper case keeps the character, the iota-subscript letters take their titlecase form) — equal to `UPPER(chr(i))` of the legacy database for every code point (1,114,111 compared) |
| 2 | A lone UTF-16 surrogate (`"\ud800"`, valid JSON) in a pincode, property type, device name or EV model made the legacy crash in psycopg (500), and one echoed in a response (basic `property_type`, a quotation package key, a package-key error message) crashed the legacy renderer (500); the platform answered 404/200, or 500 from its own renderer | text parameters and responses (keys, values, EMI error messages) must be UTF-8 → `400 invalid_input` |
| 3 | A deeply nested JSON body (`[[[[…]]]]`) raised `RecursionError` inside DRF's JSON parser: 500 and a `SystemException` row per anonymous request on every JSON endpoint (the error sink F-FIX #2 protects) | shared `flarize/parsers.py` `JSONParser` (the default parser): `400 parse_error` |
| 4 | The refused-input INFO log line carried the exception text unbounded (`float("<2 MB text>")` echoes the text) | `legacy_lookups.crash_detail`: at most 300 characters, non-UTF-8 escaped |
| 5 | `emi_bank`/`emi_calculator_settings` legacy `integer` columns are `smallint` here; a larger legacy value raised `DataError`, which escaped the importer's per-row savepoint and aborted the whole import | `import_support.run` reports `DataError` as a row violation like `IntegrityError` |
| 6 | A PLAN per-kW subsidy (`amount_per_kw` × kW) carried fractions of a paisa into `subsidy.amount` / `net_cost_after_subsidy` | rounded to paise half-up like every other amount (legacy flat amounts unchanged) |
| 7 | `calculate-solar` echoes `property_type` unchecked; the legacy answered 200 for a value nested ~10,000 levels deep, the port's render check recursed in Python: from ~950 levels 500 + a `SystemException` per anonymous request | `legacy_lookups.check_renderable` walks the payload with an explicit stack |
| 8 | A list in an `iexact` lookup: psycopg2 sent `[]` / all-`None` lists as the text `'{}'` / `'{NULL,…}'` (no match: 404 / a 0 W device), any other list as `ARRAY[…]` (500); the port refused every list (400) | `legacy_lookups.iexact_param` reproduces the psycopg2 literal |
| 9 | The advanced calculator matched each requested device against every device row (`UPPER()` of every name, per device): ~1 s of CPU for a 2 MB anonymous body | name indexes built once per calculation (first row in legacy primary-key order wins) |
| 10 | A `PACK_RELEASE` tile could not be selected: `size_uid` had to be a UUID, the provider's tile ids are texts (and two packs of one size differ only by it) | `size_uid` also accepts the exact uid of a tile of the configuration |
| 11 | A bank slug or logo colour in the wrong format was refused by the database checks with a generic `non_field_errors` | serializer validators with the same patterns name the field |

Additional evidence: a seeded random fuzz (not committed; it needs the live legacy servers) sent the same random
requests to the legacy server and the platform. After fix 1: 7,500 against the UAT server with the UAT rows and 7,500
against a private enriched copy (`legacy_goldenapp_rv_calculators_emi`, `enrich_private.sql`, port 18163) with the
enriched rows, over all five POST endpoints (bills, pincode classes, property-type spellings, device/EV mixes with
case and ligature variants, backup preferences, EMI sizes/capacities/tenures/rates/prices/down payments/toggles,
quotation packages) — no difference. After fix 2: 5,000 calculator requests with lone-surrogate variants against the
UAT server — no difference (the matching EMI round did not finish: the disk filled up); fixes 7–9 were each recorded against the UAT server
first. Every recorded corpus case was also replayed against the legacy
servers and still answers as recorded (2,454 of 2,454).
A final replay of 125 random corpus cases (25 per endpoint) against the UAT server matched as recorded. The deviation
rows were renumbered DV-74 … DV-79 on merging the integration branch (DV-62 … DV-73 went to pricing-procurement and
inventory).

Known limit (not changed): the legacy Studio accepted any Django slug (`SBI_Home`) and any `logo_bg` text (`blue`) for a
bank; the platform checks a lowercase-hyphen slug and a hex colour, so the importer skips and reports such a bank (none
exists in the recorded data). Fix the row in the legacy Studio, or add it in `emi/banks/`, before the cut-over import.

## Hand-over notes

* **Legacy shim** (`/legacy/api/calculate-solar*`, `/legacy/api/emi-calculator*`): the canonical endpoints take the
  legacy bodies unchanged except EMI's size id → `size_uid` (`emi/tests/parity/translate.request` is the executable
  rule: `_to_int(size_id or installation_id or id)` → `core_legacy_map` BACKEND `emi_system_size` → uid; not a number
  → any non-uid text; unknown → a uid no size has) and the responses back (`size_uid`/`rule_uid`/`uid` → the legacy
  ids through the map); errors back to `{"error": message}` with the status (400 `invalid_input` stays 400 — the
  legacy's 500s were never a contract).
* **packs**: register the `PACK_RELEASE` provider (`emi.services.price_sources.register(fn, cache_namespaces=("packs",
  …))`) returning a `SizeOption` per offered pack size, then set `EMI_PRICE_SOURCE=PACK_RELEASE`; report the old
  `emi_system_size` prices against the release (PLAN §7.3). The calculators' sizing tables move to pack releases the
  same way when the business approves the release prices.
* **pricing**: the price provider registered with `catalog.services.pricing_hooks` prices the hybrid batteries; bump
  `pricing` on every release (the calculators' snapshot depends on it).
* **migrations_tools**: order reference (tariffs, device types, EVs, pincodes) → catalog website products (batteries)
  → pricing prices → `calculators.services.legacy_import.import_all(solar_installations=…, solar_installation_new=…)` →
  `emi.services.legacy_import.import_all(banks=…, interest_rules=…, subsidy_rules=…, settings=…, system_sizes=…)`.
* **Website rebinding**: `calculate-solar-new/` → `calculators/basic-v2/`, `calculate-solar-advanced/` →
  `calculators/advanced/`, `emi-calculator*` → `calculators/emi*` with `size_uid`; errors are the envelope's `message`.
* Recapturing: see the docstrings of the capture scripts; the enriched corpus must come from a private restored copy
  (the scripts refuse `legacy_goldenapp`).
