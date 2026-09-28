# Flarize Platform — Consolidated Architecture, Schema, API, Implementation, Deployment and Migration Plan

**Status:** final draft, self-verified (see §11). Supersedes the two earlier plan documents.
**Scope:** one backend that replaces the main Django backend (`goldenray/` + `bom/`), the separate CMS project (`cms/`), the Flarize Node utility, the Purchase Agreement page, Site Inspection V2 and the eSSL attendance app. One Postgres database, one user model, one permission registry, one product master, one pricing source consumed by the website, quotation/agreement/report documents, BOM and calculators.
**Standard:** `BACKEND_ENGINEERING_STANDARD.md` minus multi-tenancy, monitoring stack (Grafana/Prometheus/Sentry) and irrelevant external packages.
**Site:** the website is served at `flarize.com` (repo name is `goldenray`; the Next.js app already canonicalises to `https://flarize.com`).

---

## Table of contents
0. What the client asked for, restated as architecture goals
1. Architecture
2. Schema design (all apps)
3. API design
4. Implementation plan (series vs parallel, week by week)
5. Deployment plan
6. Replacing the existing APIs (strangler cutover)
7. Data migration plan (CMS in detail, then main backend, Flarize, utilities)
8. Testing and acceptance gates
9. Decisions still open (with defaults)
10. Risks
11. Verification record of this document

---

## 0. Goals, restated

The client wants **one central product inventory and one pricing source** that everything else reads:

| Consumer | What it reads | How it reads it today | How it reads it after |
|---|---|---|---|
| Website comparison pages (`/solar-comparison`, `/inverter-comparison`, …) | panel / inverter / battery specs, ratings, price range | `goldenray` tables `solar_panels`, `solar_inverters`, `batteries` (hand-maintained, disconnected from BOM prices) | `catalog` components with a public profile; one record per product |
| Website calculators (basic, advanced, EMI, quotation EMI) | system sizes, prices, subsidy, interest rules | hard-coded and duplicated across `calculate-solar*`, `emi_*`, `bom` market rates | published `PriceRelease` + `PackRelease` through `engines/` |
| Quotation documents | pack price, BOM, inclusions, content | Flarize JSON files; `bom` app; React renderer | `quotations` pins a `PackRelease` and a `PriceRelease`; one render pipeline |
| Purchase agreements / sale orders | quoted system + prices | typed by hand in a browser page | derived from the issued quotation |
| BOM / engineering | component prices, templates, structure costs | `bom` app (Django) and Flarize `catalog.json` (two authorities, already diverged) | `catalog` + `pricing` + `bom` + `packs`, single authority |
| Procurement | purchase and landed costs | Flarize JSON | `procurement` batches write append-only price rows |
| Site inspections | quoted system from the agreement | unauthenticated HTTP payload nobody sends | outbox event from `agreements` |
| Studio (staff UI) | everything above | two backends, two logins | one API, one login |

Design consequences, in order of importance:
1. **`catalog` + `pricing` are the core.** Every other app depends on them; they depend on nothing but the platform core.
2. **Published, immutable releases** (`PriceRelease`, `PackRelease`, `ContentRelease`) are what consumers read. Authoring tables are never read by the website or by document generation. That is what makes "central pricing" safe to change on a Tuesday afternoon.
3. **Documents pin releases.** A quotation, agreement or report stores the release ids it was built from and a frozen payload; re-rendering never changes numbers.
4. **The website keeps working unchanged** during the transition: the new backend serves the old public contracts (Strapi-shaped delivery, calculator payloads) from the new tables, behind the same URLs, until the frontend is rebound.

---

## 1. Architecture

### 1.1 Bounded contexts and dependency direction

```
                    ┌──────────────────────────── platform core ────────────────────────────┐
                    │ core · accounts · audit · media · documents · notifications(outbox)   │
                    └───────────────────────────────────────────────────────────────────────┘
                                                   ▲
        ┌──────────────────────────────────────────┼──────────────────────────────────────┐
        │                                          │                                      │
┌───────┴────────┐                       ┌─────────┴─────────┐                   ┌────────┴────────┐
│ PRODUCT MASTER │                       │ WEBSITE CONTENT   │                   │ HR              │
│ catalog        │◀──┐                   │ blog · sitepages  │                   │ hr · attendance │
│ pricing        │   │                   │ faqs · careers    │                   │ devices         │
│ procurement    │   │                   │ seo · company     │                   │ (essl-agent pkg)│
└───────┬────────┘   │                   │ reference         │                   └─────────────────┘
        │            │                   └─────────┬─────────┘
        ▼            │                             │
┌────────────────┐   │                             │
│ CONFIGURATION  │   │                             │
│ packs · bom    │   │                             │
│ engineering    │   │                             │
└───────┬────────┘   │                             │
        ▼            │                             ▼
┌────────────────┐   │                   ┌───────────────────┐
│ SALES          │   │                   │ PUBLIC CONSUMERS  │
│ customers      │───┘                   │ calculators · emi │
│ leads          │                       │ public API        │
│ quotations     │◀──────────────────────│ (reads releases)  │
│ agreements     │                       └───────────────────┘
│ site_inspections                       
│ projects       │
└────────────────┘
        engines/  (pure Python, imported by packs, quotations, calculators, attendance, site_inspections)
```

Rules enforced in CI (import-linter contracts):
- Arrows point down only. `catalog`/`pricing` import nothing from sales, content or HR.
- `engines/` imports nothing from Django.
- Cross-context side effects go through the outbox (`core.events`), never through direct imports of another context's services, except the documented "reads": sales reads product master and configuration; calculators read releases.

### 1.2 The release model (the heart of "central pricing")

| Release | Built from | Immutable payload | Consumers |
|---|---|---|---|
| `PriceRelease` | current `pricing_price` rows (LIST/LANDED per component), `MarketRate` set, `CostConfig`, `StatutoryFee`, GST config | JSONB snapshot + row-level `pricing_release_line` for querying | packs authoring, BOM costing, quotations, calculators, public product pages (list price / price range) |
| `PackRelease` | an APPROVED `PackConfigVersion` + a `PriceRelease` | per (system_type, tier, size): BOM lines, customer price incl. GST, pinned component uids | quotations, calculators, website pack prices, EMI |
| `ContentRelease` (= a PUBLISHED row of `quotations_content_version`) | bilingual quotation content, inclusions, tier display names, testimonials, campaign | JSONB | quotation documents |

Publishing is a service action with `publish` permission; it validates (publish report), writes the release inside one transaction, bumps the cache version key, and publishes an outbox event (`pricing.release_published`, `packs.release_published`). Releases are never edited; a correction is a new release. Public endpoints answer from the latest published release through Redis with version-keyed invalidation (standard: version-keyed cache invalidation).

### 1.3 Runtime topology

```
Internet ─▶ nginx (TLS, rate limits, static, /iclock plaintext listener on :8080)
              ├─ /                → Next.js (website + /studio)
              ├─ /api/public/v1/  → gunicorn (gthread)  ──┐
              ├─ /api/v1/         → gunicorn             │  Django 6.0 / DRF 3.16
              ├─ /api/agent/      → gunicorn             │
              ├─ /iclock/         → gunicorn (plain views)┘
              └─ /media/private/  → X-Accel-Redirect after signed-URL check
   gunicorn ─▶ PgBouncer ─▶ PostgreSQL 16          Redis 7 (cache, Celery broker, throttles)
   celery worker (default, documents, ingest queues) · celery beat · playwright (in the documents worker image)
   Object storage: Bunny CDN (public media) · local volume or S3-compatible bucket (private media, rendered documents)
   Office PCs: essl-agent (pyzk) ─▶ /api/agent/   ·   Terminals ─▶ /iclock/<token>/cdata (HTTP only)
```

### 1.4 Cross-cutting decisions (from the standard, with our deltas)

| Concern | Decision |
|---|---|
| Python / Django / DRF | 3.12 / 6.0 / 3.16; Gunicorn gthread; PgBouncer transaction pooling, `CONN_MAX_AGE=0` |
| Auth (staff) | SimpleJWT RS256, access 15 min, refresh 7 d with rotation + `token_blacklist`; Argon2; login lockout (5/15 min); Studio uses a BFF route in Next.js that stores tokens in HttpOnly cookies |
| Auth (machines) | `core.ServiceCredential` (sha256 token hash, prefix lookup) for office agents; per-device secret path segment for terminals; signed one-time URLs for documents; OTP-verified signed links for customers |
| RBAC | one closed module×action registry; `Role.permissions` JSON normalised on save; per-module record scope `all / owned / assigned / office / self`; no superuser bypass in API code; default deny |
| Models | `core.BaseModel`: `id` bigint PK, `uid` UUID unique (external id), `created_at/updated_at`, `created_by/updated_by` SET_NULL, `deleted_at` soft delete (`objects` live / `all_objects`), `version` int for optimistic locking. Lifecycle `status` fields separate from deletion. Partial `UniqueConstraint`s only; `unique_together` banned. `on_delete` chosen per FK (PROTECT for masters, SET_NULL for attribution, CASCADE for true children) |
| Layering | `models/` schema only; `services/` all writes (`@transaction.atomic`, `DomainError(code, status)`); `views/` + `serializers/` HTTP shape; `engines/` pure |
| Errors | `{code, message, errors, error_codes}` from one exception handler; 4xx codes are stable strings documented in OpenAPI |
| Async | Celery 5.4 + Beat; transactional outbox `core_outbox_event` drained by a worker; `transaction.on_commit` for enqueues |
| Cache | Redis; version-keyed namespaces (`v:<app>:<scope>`); bump on publish; public endpoints cached 60–300 s |
| Audit | `audit_log` append-only (`REVOKE UPDATE, DELETE`), middleware captures actor/IP/request id; every service write records old/new |
| Files | `media_asset` with `visibility` public (Bunny) / private (bucket + signed URL); thumbnails via Celery; upload limits per kind |
| Documents | one Playwright HTML→PDF pipeline (`documents.RenderJob`); templates per kind × language (en/ml/hi) |
| Encryption | Fernet field for Twilio/Bunny/SMTP secrets stored in `company_integration`; fail-closed |
| Client IP | one helper honouring `TRUSTED_PROXIES`; throttles and lockouts use it |
| API docs | drf-spectacular at `/api/docs/`; OpenAPI is the Studio contract |
| Tests / lint | pytest + factory_boy; black/isort/flake8 line length 200; coverage gate 85 % on services and engines; import-linter |
| Feature flags | `core_feature_flag` rows, default off: `ADMS_RECEIVER`, `AGREEMENTS_PRICE_OVERRIDE`, `LEGACY_API_SHIM`, `INVENTORY_STOCK` |
| Removed on purpose | Django admin for business data, Channels/WebSockets, second database, HS256, `is_superuser` checks, react-pdf/reportlab/jsPDF, SQLite, localStorage records, server-side terminal pulling |

### 1.5 Project layout

```
flarize/                  settings/{base,dev,staging,prod}.py · urls.py · celery.py · wsgi.py · exceptions.py · pagination.py · cache_utils.py · crypto.py · client_ip.py
core/                     BaseModel · SoftDeleteManager · DomainError · BaseViewSet · HasModulePermission · scope filters · SequenceCounter · ServiceCredential · FeatureFlag · OutboxEvent + drain task · LegacyMap
accounts/                 User · Role · registry.py · LoginAttempt · UserSession · password reset · seeds
audit/                    AuditLog · middleware
media/                    MediaAsset · bunny.py · tasks (thumbnails) · signed urls
documents/                RenderJob · templates/<kind>/<lang>.html · render task (Playwright) · signed download
catalog/  pricing/  procurement/  packs/  bom/  engineering/
customers/  leads/  quotations/  agreements/  site_inspections/  projects/
blog/  sitepages/  faqs/  careers/  seo/  company/  reference/  calculators/  emi/
hr/  attendance/  devices/
engines/                  money · energy · savings · subsidy · finance · bom_builder · device_allocation · pack_pricing · cost · pricing · engineering_checker · battery_compat · offers · gate · attendance · inspection_readiness · inspection_checks
legacy/                   read-only adapters that serve the old URL contracts from the new tables (deleted after cutover)
migrations_tools/         management commands: import_cms · import_backend · import_flarize · import_pa · import_si · import_essl · verify_migration
essl-agent/               separate Python package (office agent), own tests, not a Django app
```

---
## 2. Schema design

**Notation.** Every table inherits the `core.BaseModel` columns (`id`, `uid`, `created_at`, `updated_at`, `created_by`, `updated_by`, `deleted_at`, `version`) unless marked *(no base)*. Types are Postgres types. `PU(...)` = partial unique constraint `WHERE deleted_at IS NULL` unless stated. FK on_delete is given as P (PROTECT), S (SET_NULL), C (CASCADE). Enum columns are `varchar` with a DB `CHECK` and a `TextChoices` class. Money is `numeric(14,2)` in INR; rates `numeric(10,4)`; percentages stored as fractions (`0.18`).

### 2.1 Platform core

**`core_sequence_counter`** *(no base)* — `kind varchar(16)`, `period_key varchar(16)` ('' for global), `next_value bigint`; PK (kind, period_key). Read with `SELECT … FOR UPDATE`. Kinds: `QUO` (`GR-<n>`, continues from Flarize counter 9729), `AGR` (`AGR-<FY>-<nnnn>`), `SV` (`SV-YYYYMMDD-NNNN`), `LEAD` (`L-<n>`), `PROJ`.

**`core_service_credential`** — `kind varchar(16)` (AGENT), `name`, `token_prefix varchar(16)` (index), `token_hash char(64)`, `issued_at`, `revoked_at`, `last_used_at`, `bound_content_type/bound_object_id` (the Agent row). PU(token_prefix).

**`core_feature_flag`** — `key varchar(64)` unique, `enabled bool default false`, `note`.

**`core_outbox_event`** *(no base)* — `id bigserial`, `event_type varchar(64)`, `aggregate_type`, `aggregate_uid uuid`, `payload jsonb`, `created_at`, `processed_at`, `attempts int`, `last_error`. Index (processed_at) partial where null. Written inside the same transaction as the domain change; drained by `core.tasks.drain_outbox` every 5 s and on commit.

**`core_legacy_map`** *(no base)* — `source_system varchar(16)` (CMS, BACKEND, FLARIZE, PA, SI, ESSL), `source_table varchar(64)`, `source_id varchar(128)`, `target_table varchar(64)`, `target_id bigint`, `imported_at`. Unique (source_system, source_table, source_id). Makes every importer idempotent and every migrated row traceable.

**`accounts_user`** — `email citext` PU, `password` (Argon2), `first_name`, `last_name`, `phone_e164 varchar(16)`, `is_active`, `is_staff` (always true; kept for Django), `role_id` FK→accounts_role P, `title varchar(64)` (CRS / Field Sales / …), `employee_id` OneToOne→hr_employee S (nullable), `last_login_at`, `password_changed_at`, `must_reset_password bool`. **No `is_superuser` semantics in API code.**

**`accounts_role`** — `slug` PU, `name`, `description`, `is_system bool`, `permissions jsonb` (`{module: [actions]}`, normalised against the registry on save), `scopes jsonb` (`{module: 'all'|'owned'|'assigned'|'office'|'self'}`), `legacy_role varchar(16)` (nullable; CMS admin/editor/author for migration only).

**`accounts_login_attempt`** *(no base)* — `email`, `ip inet`, `succeeded bool`, `at timestamptz`; index (email, at). **`accounts_user_session`** — `user_id` C, `refresh_jti uuid` unique, `user_agent`, `ip inet`, `expires_at`, `revoked_at`. **`accounts_password_reset`** — `user_id` C, `token_hash char(64)`, `expires_at`, `used_at`.

**`audit_log`** *(no base; append-only, `REVOKE UPDATE, DELETE` from the app role)* — `id bigserial`, `at timestamptz`, `actor_id` S, `actor_kind varchar(8)` (USER, AGENT, DEVICE, CUSTOMER, SYSTEM), `request_id uuid`, `ip inet`, `action varchar(64)` (e.g. `quotations.issue`), `object_type varchar(64)`, `object_uid uuid`, `before jsonb`, `after jsonb`, `note text`. Indexes (object_type, object_uid, at), (actor_id, at), (action, at). Monthly range partitions by `at`.

**`media_asset`** — `visibility varchar(8)` (PUBLIC, PRIVATE), `kind varchar(16)` (IMAGE, DOCUMENT, SIGNATURE, RESUME, PHOTO), `file varchar(512)` (storage key), `cdn_url varchar(512)` (public only), `original_filename`, `mime_type varchar(120)`, `size_bytes bigint`, `width int`, `height int`, `captured_at timestamptz` (EXIF), `checksum_sha256 char(64)`, `alternative_text varchar(255)`, `caption`, `thumbnail_key varchar(512)`, `folder varchar(120)`, `uploaded_by` S. Index (visibility, kind, created_at). Usage is checked through a registry of referencing FKs before hard delete (never hard-deleted while referenced).

**`documents_render_job`** — `kind varchar(24)` (QUOTATION, AGREEMENT, INSPECTION_REPORT, ATTENDANCE_REPORT, PUBLISH_REPORT), `object_type`, `object_uid uuid`, `template varchar(64)`, `language char(2)`, `payload_sha256 char(64)`, `status varchar(12)` (QUEUED, RUNNING, DONE, FAILED), `file varchar(512)`, `page_count int`, `error text`, `started_at`, `finished_at`, `requested_by` S. Index (object_type, object_uid, created_at). Download only via `documents.signed_url(job)` (HMAC, 10 min, single use recorded in `documents_download`).

### 2.2 Product master — `catalog`

**`catalog_brand`** — `name` PU(lower(name)), `slug` PU, `country`, `website`, `logo_id` FK→media S, `is_active`.

**`catalog_category`** — `slug` PU (panel, inverter, battery, dcdb, acdb, meter, dc_cable, ac_cable, isolator, cb_rod, earth_cable, la_cable, structure, service, other), `name`, `bom_role varchar(24)` (how the BOM builder treats it: MAIN_PANEL, MAIN_INVERTER, BATTERY, PROTECTION, CABLE, EARTHING, STRUCTURE, SERVICE, MISC), `gst_rate numeric(5,4)` default, `hsn_code varchar(12)`, `unit varchar(12)` (NOS, M, KG, SET), `attribute_schema jsonb` (JSON Schema for `component.attributes`), `sort_order int`, `is_active`.

**`catalog_component`** — the single product record.
`sku varchar(32)` PU (keeps Flarize/BOM ids like `a1`, `en7`; new ones generated per category prefix), `category_id` P, `brand_id` P, `name varchar(255)`, `model varchar(120)`, `description text`, `attributes jsonb` (validated against category schema on save), `gst_rate_override numeric(5,4)` null, `hsn_code_override`, `unit_override`, `status varchar(12)` (DRAFT, ACTIVE, DEPRECATED, RETIRED), `deprecated_reason`, `replacement_id` FK→self S, `is_public bool` (visible on the website), `warranty_product_years smallint`, `warranty_performance_years smallint`, `datasheet_id` FK→media S, `primary_image_id` FK→media S, `search tsvector` (generated). Indexes: (category_id, status), GIN(search), GIN(attributes). Retired/deprecated components stay referenced by history; **`RETIRED` cannot be selected in a new pack or quotation** (service rule + publish check PBC-M-007).

**`catalog_component_tier`** — `component_id` C, `tier varchar(8)` (BASE, VALUE, PREMIUM). PU(component_id, tier). Replaces `bom.ItemTier` and Flarize tier lists.

**`catalog_panel_spec`** — OneToOne `component_id` C. `wattage_w int`, `panel_type varchar(24)` (MONO_PERC, TOPCON, HJT, BIFACIAL, POLY), `technology varchar(24)`, `cell_count smallint`, `efficiency_pct numeric(5,2)`, `temperature_coefficient numeric(5,3)`, `noct_c smallint`, `ip_rating varchar(8)`, `wind_load_pa int`, `snow_load_pa int`, `weight_kg numeric(6,2)`, `dimensions_mm varchar(32)`, `bifacial_gain_pct smallint`, `first_year_drop_pct numeric(4,2)`, `annual_degradation_pct numeric(4,2)`, `is_dcr bool`, `bis_certified bool`, `certifications jsonb`, `pvel_top_performer bool`, `manufacturing_capacity varchar(32)`.

**`catalog_inverter_spec`** — OneToOne. `kw numeric(6,2)`, `phase varchar(8)` (1P, 3P), `inverter_type varchar(8)` (ONGRID, HYBRID), `mppt_count smallint`, `max_dc_input_kw numeric(6,2)`, `max_pv_voltage_v int`, `battery_voltage_v int` (hybrid), `max_charge_current_a int`, `efficiency_pct numeric(5,2)`, `ip_rating`, `communication jsonb`, `compatible_battery_families jsonb` (array of `catalog_battery_family.slug`; source of the battery-compatibility engine), `system_controller_required bool` (PBC-M-003 data blocker made explicit).

**`catalog_battery_spec`** — OneToOne. `family_id` FK→`catalog_battery_family` P, `chemistry varchar(16)` (LFP, LEAD_ACID, NMC), `nominal_voltage_v numeric(6,2)`, `capacity_kwh numeric(6,2)`, `usable_kwh numeric(6,2)`, `max_c_rate numeric(4,2)`, `cycle_life int`, `dod_pct smallint`, `stackable bool`, `max_units_in_series smallint`, `max_units_in_parallel smallint`, `bms_included bool`, `protection_rating varchar(12)` (was the `bt1` data blocker).

**`catalog_battery_family`** — `slug` PU, `name`, `voltage_class varchar(8)` (48V, HV), `notes`. (Flarize `battery-master.json` → families + battery specs.)

**`catalog_structure_spec`** — OneToOne. `structure_type varchar(16)` (FLAT_ROOF, ELEVATED, SHEET_ROOF, GROUND), `material varchar(8)` (GP, GI, AL), `tube_size varchar(12)`, `weight_kg_per_m numeric(6,3)`.

**`catalog_component_public_profile`** — OneToOne `component_id` C. Website-facing marketing data that used to live in `solar_panels`/`solar_inverters`/`batteries`: `slug` PU, `headline`, `summary text`, `body text` (markdown), `price_range_label varchar(64)` (derived from the PriceRelease when null), `subsidy_eligible bool`, `kerala_climate_score smallint`, `ratings jsonb` (`{efficiency, heat_performance, warranty, kerala_climate, overall}`), `pros jsonb`, `cons jsonb`, `faq jsonb`, `gallery jsonb` (media uids), `seo_title`, `seo_description`, `published_at`, `status` (DRAFT, PUBLISHED). Served by `/api/public/v1/products/…`.

**`catalog_component_change`** *(no base)* — `component_id` C, `at`, `by_id` S, `field varchar(64)`, `old jsonb`, `new jsonb`, `reason`. (Flarize `ComponentChangeLog` semantics; also mirrored in `audit_log`, this table exists for the Items & Prices history screen without scanning the audit partition.)

### 2.3 Pricing — `pricing`

**`pricing_price`** *(append-only rows; `effective_to` is the only mutable column)* — `component_id` P, `kind varchar(12)` (PURCHASE, LANDED, LIST), `amount numeric(14,2)`, `currency char(3)` default INR, `gst_inclusive bool` default false, `effective_from date`, `effective_to date` null, `source varchar(12)` (BATCH, MANUAL, IMPORT, MARKUP), `source_ref varchar(64)` (batch line uid, import file), `supplier_id` FK→procurement_supplier S, `note`, `version_key varchar(64)` (e.g. `BATCH-2026-09-004::a1`). PU(component_id, kind) WHERE `effective_to IS NULL` — one current price per kind. Index (component_id, kind, effective_from desc). Setting a new current price closes the previous one in the same transaction. **`LIST` is never derived by markup in code** (Flarize rule: MARKUP is forbidden; gross-margin check lives in `engines.cost`).

**`pricing_current_price`** — Postgres view: latest open row per (component, kind). Used by authoring screens; releases copy from it.

**`pricing_market_rate_set`** — `name`, `status` (DRAFT, ACTIVE, RETIRED), `activated_at`. **`pricing_market_rate`** — `set_id` C, `system_type varchar(8)` (ONGRID, HYBRID, UPGRADE), `tier varchar(8)`, `battery_config varchar(4)` ('' , 0, 1, 2 — hybrid battery count band), `size_kw numeric(6,2)`, `customer_price_incl_gst numeric(14,2)`. PU(set_id, system_type, tier, battery_config, size_kw). Replaces `bom.MarketRate.size_rates` JSON and Flarize `marketRates` (`PACK_MARKET_RATE` pricing mode).

**`pricing_cost_config`** — `key varchar(48)` (install_rate, service_rate_year, service_years, transport_rate_per_km, transport_base_km, miscellaneous, office_expense_monthly, expected_projects_per_month, elevated_structure_rate, sheet_structure_rate, gp_rate_per_kg, gi_rate_per_kg, structure_labor, structure_repair_pct, gst_goods_share (0.70), gst_services_share (0.30), gst_goods_rate, gst_services_rate, target_gross_margin_by_tier), `value jsonb`, `effective_from date`, `effective_to date` null. PU(key) WHERE effective_to IS NULL. **`pricing_installation_matrix`** — `size_kw numeric(6,2)`, `phase varchar(4)`, `install_cost numeric(14,2)`, `labour_days numeric(5,2)`; PU(size_kw, phase).

**`pricing_swap_delta`** — `system_type`, `tier`, `slot varchar(24)` (panel, inverter, battery), `from_component_id` P, `to_component_id` P, `delta_incl_gst numeric(14,2)`, `set_id` FK→market_rate_set C. PU(set_id, system_type, tier, slot, from_component_id, to_component_id). (Flarize `Σ swapDelta`.)

**`pricing_roof_addon`** — `set_id` FK→market_rate_set C, `structure_type`, `size_kw`, `addon_incl_gst`; PU(set_id, structure_type, size_kw).

**`pricing_statutory_fee`** — `kind varchar(16)` (KSEB_REGISTRATION, KSEB_METER, NET_METER_TEST, OTHER), `label`, `phase varchar(4)` null, `capacity_kw_max numeric(6,2)` null, `amount numeric(12,2)`, `effective_from`, `effective_to`. PU(kind, phase, capacity_kw_max) WHERE effective_to IS NULL. (Purchase Agreement `kseb` list.)

**`pricing_offer`** — `code varchar(32)` PU, `name`, `type varchar(8)` (FLAT, PERCENT), `value numeric(12,2)`, `applies_to_system varchar(8)` (ALL/ONGRID/HYBRID), `applies_to_tier`, `applies_to_size_kw` null, `starts_on`, `ends_on`, `status varchar(10)` (DRAFT, ACTIVE, PAUSED, EXPIRED, ARCHIVED), `print_on_quotation bool`, `stackable bool`. **`pricing_offer_transition`** *(no base)* — `offer_id` C, `from_status`, `to_status`, `at`, `by_id`, `reason`.

**`pricing_validity_policy`** — `key` PU (QUOTATION), `days smallint`, `grace_days smallint`, `effective_from`.

**`pricing_release`** — `number int` PU, `status varchar(10)` (PUBLISHED, SUPERSEDED), `published_at`, `published_by` S, `market_rate_set_id` P, `note`, `payload jsonb` (full snapshot: component prices by sku, market rates, swap deltas, roof addons, cost config, statutory fees, gst), `payload_sha256`. **`pricing_release_line`** *(no base)* — `release_id` C, `component_id` P, `list_price`, `landed_cost`, `gst_rate`; PK (release_id, component_id). The **current release** is the highest PUBLISHED number; `SUPERSEDED` is set on the previous one in the same transaction.

**Optional stock ledger (feature flag `INVENTORY_STOCK`, D-6):** `inventory_location` (`code`, `name`, `office_id` S), `inventory_movement` *(append-only)* (`component_id` P, `location_id` P, `qty numeric(12,3)`, `direction` IN/OUT, `reason` (PURCHASE, ISSUE_TO_PROJECT, RETURN, ADJUST), `ref_type`, `ref_uid`, `at`, `by`), `inventory_balance` view. Not consumed by pricing; kept out of the critical path.

### 2.4 Procurement — `procurement`

**`procurement_supplier`** — `code` PU, `name`, `gstin varchar(15)`, `contact jsonb`, `address`, `is_active`.
**`procurement_batch`** — `number varchar(32)` PU (`BATCH-<YYYY>-<nnn>`), `supplier_id` P, `invoice_no`, `invoice_date`, `status varchar(10)` (DRAFT, COMMITTED, CANCELLED), `committed_at`, `committed_by` S, `subtotal`, `charges_total`, `total`, `allocation_method varchar(16)` = PURCHASE_VALUE_PROPORTION, `note`.
**`procurement_batch_line`** — `batch_id` C, `component_id` P, `qty numeric(12,3)`, `unit_purchase_price numeric(14,2)`, `line_value` (generated), `landed_unit_cost numeric(14,2)` (computed at commit), `price_row_id` FK→pricing_price S (the PURCHASE row written at commit), `landed_row_id` FK→pricing_price S. PU(batch_id, component_id).
**`procurement_batch_charge`** — `batch_id` C, `kind varchar(16)` (FREIGHT, INSURANCE, HANDLING, DUTY, OTHER), `amount`, `note`.
Commit is one transaction: allocate charges by purchase-value proportion (`engines.cost.allocate_landed`), write two `pricing_price` rows per line, close previous open rows, audit, event `procurement.batch_committed`. Committed batches are immutable; corrections are a reversing batch.

### 2.5 Configuration — `packs`, `bom`, `engineering`

**`bom_template`** — `system_type` PU (ONGRID, HYBRID, UPGRADE), `name`, `is_active`. **`bom_slot`** — `template_id` C, `key varchar(32)` (panel, inverter, dcdb, …), `category_id` P, `qty_rule jsonb` (`{"type":"per_kw"|"fixed"|"per_panel"|"by_phase", …}` — the `BomSlot.get_qty` logic as data), `required bool`, `sort_order`. PU(template_id, key). **`bom_fixed_item`** — `template_id` C, `component_id` P null, `category_id` P null, `qty numeric(12,3)`, `condition jsonb` (phase/size filters). **`bom_structure_template`** — `slug` PU (flat_roof, elevated, sheet_roof), `name`, `labour_rate_key`. **`bom_structure_template_item`** — `template_id` C, `tube_size`, `length_m_per_kw numeric(8,3)`, `qty_rule jsonb`. **`bom_tube_weight`** — `tube_size` PU, `kg_per_m numeric(6,3)`.

**`packs_config_version`** — `number int` PU, `status varchar(10)` (DRAFT, SUBMITTED, APPROVED, REJECTED, PUBLISHED), `based_on_id` FK→self S, `config jsonb` (validated by `engines.pack_config.schema`), `change_log jsonb`, `submitted_by/at`, `approved_by/at`, `rejection_reason`. PU(status) WHERE status IN ('DRAFT') — one open draft. Typed child tables mirror the JSON for querying: **`packs_config_pack`** (`version_id` C, `system_type`, `tier`, `size_kw`, `phase`, `battery_config`, `panel_id` P, `inverter_id` P, `battery_id` P null, `battery_qty`, `structure_template_id` P, `is_future_ready bool`, `pair_of_id` FK→self S) PU(version_id, system_type, tier, size_kw, phase, battery_config); **`packs_config_line`** (`pack_id` C, `slot_key`, `component_id` P, `qty numeric(12,3)`, `source varchar(8)` SLOT/FIXED/STRUCTURE/MANUAL).

**`packs_release`** — `number int` PU, `config_version_id` P, `price_release_id` P, `content_release_id` P null, `status` (PUBLISHED, SUPERSEDED), `published_at/by`, `publish_report jsonb` (readiness matrix: missing market rates, retired components, PBC failures), `payload jsonb`. **`packs_release_pack`** *(no base)* — `release_id` C, `system_type`, `tier`, `size_kw`, `phase`, `battery_config`, `customer_price_incl_gst`, `customer_price_excl_gst`, `gst_amount`, `landed_cost_total`, `gross_margin_pct`, `bom jsonb` (lines with sku, name, qty, unit list price), `display_name`; PK (release_id, system_type, tier, size_kw, phase, battery_config). This is the registry the website, calculators and quotations read.

**`engineering_rule_set`** — `version varchar(16)` PU, `rules jsonb` (the 33 PBC rules with severity and parameters), `active bool`. **`engineering_run`** — `rule_set_id` P, `subject_type` (PACK_CONFIG_VERSION, PROJECT_BOM, QUOTATION_DRAFT), `subject_uid`, `result varchar(8)` (PASS, WARN, FAIL), `summary jsonb`. **`engineering_finding`** — `run_id` C, `rule_code varchar(16)`, `severity varchar(8)` (BLOCK, WARN, INFO), `message`, `context jsonb`. **`engineering_acknowledgement`** — `finding_id` C, `acknowledged_by` S, `reason`, `at`; PU(finding_id).

### 2.6 Sales — `customers`, `leads`, `quotations`, `agreements`, `projects`

**`customers_customer`** — `code varchar(24)` PU (`CUST-…` kept for migrated rows), `name`, `phone_e164` PU, `alt_phone`, `email citext`, `address text`, `pincode char(6)`, `district`, `state`, `location varchar(120)`, `google_map_link`, `latitude/longitude numeric(9,6)`, `current_bill numeric(10,2)`, `source varchar(16)` (WEBSITE, SALES_ENTRY, REFERRAL, PA_IMPORT, SI_IMPORT), `owner_id` FK→user S (record-scope anchor), `lead_id` FK→leads_lead S, `merged_into_id` FK→self S. Index (owner_id), GIN(name trgm).
**`customers_note`** — `customer_id` C, `body`, `pinned bool`.

**`leads_lead`** — `number` (SequenceCounter LEAD), `kind varchar(16)` (HOME_ENQUIRY, ADVANCED_CALC, GROUP_PURCHASE, CONTACT, REFERRAL, QUOTE_REQUEST), `name`, `phone_e164`, `email`, `pincode`, `district`, `message`, `payload jsonb` (calculator inputs/outputs, page, utm), `otp_verified_at`, `status varchar(12)` (NEW, CONTACTED, QUALIFIED, CONVERTED, LOST, SPAM), `assignee_id` S, `customer_id` S, `lost_reason`, `source_url`, `ip inet`. Index (status, created_at), (assignee_id). **`leads_lead_note`**, **`leads_lead_event`** *(no base)* (`lead_id` C, `at`, `by`, `event`, `data jsonb`).
**`leads_otp_request`** *(no base)* — `phone_e164`, `purpose varchar(16)` (LEAD, APPROVAL), `provider_sid`, `attempts smallint`, `verified_at`, `expires_at`, `ip`. Index (phone_e164, created_at).
**`leads_affiliate_application`**, **`leads_warranty_request`** (`customer_id` S, `system_details jsonb`, `issue`, `status`, `assignee`), **`leads_customer_installation`** (public map / stats: `pincode`, `district`, `capacity_kw`, `system_type`, `installed_on`, `is_showcase bool`, `photo_id` S) — typed columns as in the current tables, plus status and assignee.

**`quotations_quotation`** — `number varchar(24)` PU (`GR-<n>`), `customer_id` P, `owner_id` FK→user S, `status varchar(12)` (DRAFT, ISSUED, ACCEPTED, EXPIRED, SUPERSEDED, CANCELLED), `current_version_id` FK→quotations_version S, `valid_until date`, `accepted_at`, `agreement_id` FK→agreements_agreement S (set when the PA is issued), `site_inspection_id` S (pre-sale inspection link), `lost_reason`.
**`quotations_version`** — `quotation_id` C, `number smallint` (1..n), `status varchar(10)` (DRAFT, ISSUED, SUPERSEDED), `pack_release_id` P, `price_release_id` P, `content_release_id` P, `system_type`, `tier`, `size_kw`, `phase`, `battery_config`, `structure_type`, `roof_type`, `distance_km`, `selections jsonb` (swaps, offers applied, inclusions toggles, language), `customer_price_incl_gst`, `transport_extra`, `offer_total`, `final_price`, `gross_margin_pct` (internal), `gate_report jsonb` (the 8 checks), `issued_at/by`, `document_job_id` FK→documents_render_job S, `document_payload jsonb` (deep-frozen page payload, 13 pinned versions), `document_payload_sha256`. PU(quotation_id, number).
**`quotations_bom_snapshot`** — `version_id` OneToOne C, `lines jsonb`, `lock_acknowledgements jsonb`, `engineering_run_id` S. **`quotations_commercial_snapshot`** — `version_id` OneToOne C, `cost_lines jsonb`, `pins jsonb` (release numbers, policy version, rate card version, content version, gst config version), `margin_check jsonb`.
**`quotations_discount_request`** — `version_id` C, `requested_by` S, `amount`, `reason`, `status` (PENDING, APPROVED, REJECTED), `decided_by/at`, `note`.
**`quotations_content_version`** — `number int` PU, `status` (DRAFT, PUBLISHED, SUPERSEDED), `language_payload jsonb` (`{en:{…}, ml:{…}}`), `fit_report jsonb` (bilingual fit guard), `published_at/by`. **`quotations_inclusion`** — `key` PU, `label_en`, `label_ml`, `default_on bool`, `applies_to jsonb`. **`quotations_tier_display_name`** — `system_type`, `tier`, `name_en`, `name_ml`; PU(system_type, tier). **`quotations_testimonial`** — `customer_name`, `location`, `capacity_kw`, `quote_en`, `quote_ml`, `photo_id` S, `is_active`, `sort_order`, `show_on_website bool` (replaces `bom.QuotationTestimonial`). **`quotations_campaign`** — `title`, `body_en/ml`, `image_id` S, `starts_on`, `ends_on`, `is_active` (page 2 of the document).
**`quotations_email_log`** *(no base)* — `version_id` C, `to`, `sent_at`, `provider_id`, `status` (replaces `sent_quotes`; `send_quote_junk` is not migrated).

**`agreements_agreement`** — `number` PU, `kind varchar(20)` (PURCHASE_AGREEMENT, SALE_ORDER, EXTRA_STRUCTURE), `customer_id` P, `quotation_version_id` P null, `site_inspection_id` S null, `status varchar(12)` (DRAFT, ISSUED, ACCEPTED, SUPERSEDED, CANCELLED), `version smallint`, `supersedes_id` S, `language char(2)`, `capacity_kw`, `phase`, `system_type`, `panel_id` P, `panel_qty`, `inverter_id` P, `battery_id` P null, `battery_qty`, `structure_template_id` P, `walkway_required bool`, `ladder_required bool`, `variant varchar(32)`, `original_price`, `extra_cost`, `discount`, `final_price`, `statutory_fee_id` S, `statutory_fee_amount`, `add_on_offer text`, `extra_description text`, `price_override_reason` null, `payload jsonb` (frozen at issue), `document_job_id` S, `issued_at/by`, `accepted_at`, `accepted_via varchar(8)` (OTP, PAPER), `acceptance_asset_id` S. PU(quotation_version_id, kind) WHERE status='ISSUED'.
**`agreements_line`** — `agreement_id` C, `description`, `qty`, `unit`, `unit_price`, `amount`, `additional_work_item_id` S.

**`projects_project`** — `number` PU (`PROJ-…`), `customer_id` P, `quotation_version_id` P, `agreement_id` P, `site_inspection_id` S, `status varchar(14)` (PLANNED, IN_PROGRESS, COMMISSIONED, CLOSED, CANCELLED), `head_id` FK→user S, `bom_lock jsonb` (snapshot), `cost_inputs jsonb`, `scheduled_on`, `commissioned_on`, `kseb_status varchar(16)`. Kept minimal (D-5): custom projects and margin approval live here; the Project Head workspace UI is out of the first release.

### 2.7 Site inspections — `site_inspections`

**`site_inspections_inspection`** — `number` PU, `visit_date date`, `customer_id` P, `engineer_id` FK→user S, `origin varchar(14)` (PRE_SALE, AGREEMENT), `system_type varchar(10)` (ON_GRID, HYBRID, UNDECIDED), `status varchar(28)` (DRAFT, IN_PROGRESS, COMPLETED, CUSTOMER_APPROVAL_PENDING, APPROVED, INSTALLATION_READY, REJECTED, REVISION_REQUIRED, ON_HOLD), `complexity_status varchar(28)` (NOT_ASSESSED, ROUTINE, ENGINEERING_REVIEW_REQUIRED), `complexity_reason`, `agreement_id` S, `agreement_version smallint`, `pre_sale_source_id` FK→self S, `quotation_version_id` S, then typed stage groups: **location** (`address`, `pincode`, `location`, `district`, `google_map_link`, `latitude`, `longitude`, `location_accuracy_m`, `location_captured_at`); **access** (`road_access varchar(16)`, `vehicle_type varchar(16)`, `access_remarks`); **building/roof** (`building_type`, `no_of_floors smallint`, `roof_type varchar(16)`, `roof_strength varchar(16)`, `roof_accessibility varchar(16)`, `roof_accessibility_reason`, `roof_condition varchar(16)`, `installation_difficulty varchar(8)`, `roof_length_m`, `roof_width_m`, `available_area_m2`, `usable_area_m2`, `roof_slope varchar(8)`, `direction_facing varchar(4)`); **shading** (`morning_shading varchar(8)`, `afternoon_shading varchar(8)`, `shade_source`, `shading_pct numeric(5,2)`, `generation_impact varchar(16)`, `shading_remarks`); **electrical** (`consumer_number varchar(20)`, `consumer_name`, `registered_phone_e164`, `phase varchar(4)`, `connected_load_kw numeric(6,2)`, `sanctioned_load_kw`, `meter_number`, `tariff varchar(12)`, `neutral_link varchar(20)` (AVAILABLE, NOT_AVAILABLE, NEEDS_MODIFICATION), `termination_point varchar(20)` (same enum), `distribution_board varchar(16)`, `earthing varchar(16)`, `electrical_remarks`, `wheeling_required bool`); **cables** (`ac_cable_m`, `dc_cable_m`, `la_cable_m`, `battery_cable_m`, `battery_cable_route`, `cable_routing_remarks`); **verdict** (`site_suitability varchar(16)` (SUITABLE, CONDITIONAL, NOT_SUITABLE), `suitability_pct`, `final_recommendation text`, `engineer_remarks`, `additional_requirements`, `approval_remarks`); **restrictions** (`has_location_restrictions bool`, `customer_restrictions`, `customer_location_remarks`); **quoted** (`quoted_size_kw`, `quoted_panel_id` S, `quoted_panel_capacity_w`, `quoted_inverter_id` S, `quoted_battery_id` S, `quoted_structure_type`, `quoted_structure_material`); **structure/access work** (`site_structure_type varchar(16)`, `walkway_required bool`, `walkway_length_m`, `walkway_width_m`, `ladder_required bool`, `ladder_length_m`, `sliding_door_required bool`, `sliding_door_width_m`, `sliding_door_height_m`, `elevated_height_m`); **layout** (`panel_photo_id` FK→inspection_photo S, `equipment_photo_id` S, `panel_width_m`, `panel_height_m`, `panel_area_m2`, `equipment_width_m`, `equipment_height_m`, `equipment_area_m2`); **workflow** (`released_at`, `released_by` S, `on_hold_reason`). **No commercial columns.** `installation_readiness` is derived by `engines.inspection_readiness`. Index (engineer_id, status), (customer_id), (status, visit_date).

**`site_inspections_snapshot`** — `inspection_id` C, `number smallint`, `source varchar(10)` (PRE_SALE, AGREEMENT), `agreement_version`, `data jsonb`; PU(inspection_id, number).
**`site_inspections_photo`** — `inspection_id` C, `asset_id` FK→media P, `photo_type varchar(24)` (PANEL_AREA, EQUIPMENT_AREA, ROOF, ACCESS, ELECTRICAL, CABLE_ROUTE, SHADING, ADDITIONAL_WORK, EVIDENCE, OTHER), `stage smallint`, `captured_at`, `caption`. Index (inspection_id, photo_type).
**`site_inspections_annotation`** — `inspection_id` C, `photo_id` FK→photo C, `annotation_type varchar(16)` (PANEL_AREA, EQUIPMENT_AREA), `geometry jsonb` (`{x,y,w,h}` in 0–1 image space), `geometry_space varchar(16)` (IMAGE, LEGACY_CONTAINER), `width_m`, `height_m`, `area_m2`, `number smallint`, `is_current bool`. PU(inspection_id, annotation_type) WHERE is_current. Composite FK `(photo_id, inspection_id)` → `photo(id, inspection_id)` added by migration.
**`site_inspections_equipment_assessment`** — `inspection_id` C, `equipment_type varchar(20)` (ON_GRID_INVERTER, HYBRID_INVERTER, HYBRID_BATTERY), `checks_version varchar(8)`, `results jsonb`, `status varchar(16)` (NOT_STARTED, IN_PROGRESS, PASS, FAIL, REQUIRES_REVIEW), `issue`, `corrective_action`, `engineer_remarks`, `evidence_photo_id` S, `review_status varchar(12)` (NOT_REQUIRED, PENDING, RESOLVED, WAIVED), `resolved_by` S, `resolved_at`, `resolution_note`. PU(inspection_id, equipment_type).
**`site_inspections_location_approval`** — `inspection_id` C, `number smallint`, `status varchar(10)` (PENDING, APPROVED, REJECTED, SUPERSEDED, EXPIRED), `location_snapshot jsonb`, `customer_name`, `customer_phone_e164`, `token_hash char(64)`, `expires_at`, `otp_verified_at`, `responded_at`, `responded_ip inet`, `customer_comment`, `signature_asset_id` S, `paper_scan_asset_id` S, `approved_by_staff_id` S (paper fallback). PU(inspection_id) WHERE status='PENDING'.
**`site_inspections_additional_work_item`** — `inspection_id` C, `work_type varchar(24)`, `required bool`, `quantity numeric(10,2)`, `unit varchar(8)`, `dimensions`, `reason`, `customer_impacting bool`, `status varchar(22)` (NONE, IDENTIFIED, ENGINEERING_REVIEW, COST_CALCULATED, CUSTOMER_QUOTE_SENT, APPROVED, REJECTED), `agreement_id` S, `decided_by` S, `decided_at`. PU(inspection_id, work_type).
**`site_inspections_observation`** — `inspection_id` C, `stage smallint`, `category varchar(24)`, `note`, `photos` M2M→photo.
**`site_inspections_engineering_review`** — `inspection_id` C, `trigger varchar(24)` (MANUAL, EQUIPMENT, STRUCTURE, ROOF, GENERATION), `requested_by` S, `reason`, `reviewer_id` S, `decision varchar(16)` (PENDING, ROUTINE, REQUIRES_CHANGES, RESOLVED), `notes`, `decided_at`. PU(inspection_id) WHERE decision='PENDING'.

### 2.8 Website content — `blog`, `sitepages`, `faqs`, `careers`, `seo`, `company`, `reference`, `emi`

These keep the CMS design (it is the best-built part of the estate) with the platform base model, soft delete, and the delivery contract unchanged.

**`blog_collection`** (`api_uid` PU, `name`, `singular_name`, `description`, `is_active`), **`blog_template`** (`slug` PU, `name`, `description`), **`blog_template_image_group`** (`template_id` C, `key`, `label`, `repeatable bool`, `position`; PU(template_id, key)), **`blog_template_attribute_slot`** (`template_id` C, `key`, `label`, `type` TEXT/NUMBER/ENUM/BOOL/DATE, `options jsonb`, `required`, `position`; PU(template_id, key)), **`blog_author`** (`name`, `slug` PU, `bio`, `avatar_id` S, `user_id` S), **`blog_category`** (`name`, `slug` PU), **`blog_tag`** (`name` PU, `slug` PU), **`blog_badge`** (`name`, `slug` PU, `color`).
**`blog_entry`** — `collection_id` P, `template_id` S, `title`, `slug`, `excerpt`, `author_id` S, `cover_image_id` S, `status varchar(12)` (DRAFT, REVIEW, PUBLISHED, ARCHIVED), `published_at`, `published_on date`, `scheduled_for timestamptz`, `read_time smallint`, `is_featured`, `sort_order`, `locale char(2)` default en, `verified_by` S, `verified_at`. PU(collection_id, slug). The CMS `document_id` UUID is imported into `uid` unchanged (the frontend keys on it). M2M `blog_entry_category`, `blog_entry_tag`, `blog_entry_badge`.
**`blog_entry_slug_history`** (`entry_id` C, `collection_id` P, `slug`, `active bool`; PU(collection_id, slug) WHERE active), **`blog_content_block`** (`entry_id` C, `position`, `kind` (RICH_TEXT, IMAGE, QUOTE, CTA, EMBED, TABLE), `data jsonb`), **`blog_entry_image`** (`entry_id` C, `group_key`, `position`, `media_asset_id` S, `external_url`, `alt`), **`blog_entry_attribute_value`** (`entry_id` C, `slot_key`, `value jsonb`; PU(entry_id, slot_key)), **`blog_entry_seo`** (OneToOne, SeoFields).
**`seo_fields`** is an abstract mixin: `seo_title varchar(70)`, `seo_description varchar(160)`, `canonical_url`, `robots varchar(32)`, `og_title`, `og_description`, `og_image_id` S, `schema_type varchar(32)`, `schema_extra jsonb`. **`seo_redirect`** (`from_path` PU, `to_path`, `status_code smallint`, `hits int`) — new: feeds `next.config` redirects export. **`seo_page_metadata`** (`page` PU, `title`, `description`, `keywords`, `og jsonb`) — replaces `goldenray.Metadata`.
**`sitepages_page`** (`slug` PU, `title`, `status`, `template varchar(32)`), **`sitepages_page_seo`** (OneToOne + SeoFields), **`sitepages_page_text_slot`** (`page_id` C, `key`, `kind` SHORT_TEXT/RICH_TEXT/MARKDOWN, `value text`, `label`; PU(page_id, key)), **`sitepages_page_image_slot`** (`page_id` C, `key`, `asset_id` S, `external_url`, `alt`; PU(page_id, key)). **`sitepages_career_page`** is a page with slug `career` (no separate table).
**`faqs_category`** (`name` PU, `slug` PU, `sort_order`), **`faqs_faq`** (`category_id` P, `page_id` S, `question`, `answer text`, `status`, `sort_order`, `published_at` + SeoFields).
**`careers_department`** (`name` PU, `slug` PU), **`careers_job_position`** (`slug` PU, `department_id` P, `title`, `location`, `employment_type`, `experience`, `description text`, `requirements text`, `status` DRAFT/PUBLISHED/CLOSED/ARCHIVED, `opens_on`, `closes_on`, `openings smallint` + SeoFields), **`careers_job_application`** (`position_id` P, `name`, `email`, `phone_e164`, `resume_id` FK→media(private) S, `portfolio_id` S, `cover_letter`, `status varchar(12)` (NEW, SCREENING, INTERVIEW, OFFERED, HIRED, REJECTED, WITHDRAWN), `assignee_id` S, `source`, `ip`), **`careers_job_application_note`**, **`careers_job_application_event`** *(no base)*.
**`company_profile`** (singleton: `legal_name`, `trade_name`, `gstin`, `pan`, `cin`, `address`, `phone_e164`, `email`, `website`, `logo_id` S, `letterhead_id` S, `seal_id` S, `signature_id` S, `upi_qr_id` S, `default_og_image_id` S, `trust_stats jsonb`, `social jsonb`, `notification_recipients jsonb`, `blog_revalidate_url`, `blog_revalidate_secret` (encrypted)), **`company_bank_account`** (`label`, `bank`, `account_name`, `account_number` (encrypted), `ifsc`, `branch`, `upi_id`, `is_primary bool`; PU(is_primary) WHERE is_primary), **`company_integration`** (`key` PU (TWILIO, BUNNY, SMTP), `config jsonb` (Fernet-encrypted values), `is_enabled`).
**`reference_pincode`** (`pincode` PU, `district`, `state`, `serviceable bool`, `distance_km_from_office`), **`reference_kseb_tariff`** (`slab_from_units`, `slab_to_units`, `phase`, `rate_per_unit`, `fixed_charge`, `effective_from`), **`reference_device_type`**, **`reference_wattage`**, **`reference_room_size`**, **`reference_ev_car`**, **`reference_ev_scooter`**, **`reference_appliance`** — typed as today with `is_active` and `sort_order`. (`solar_panels`, `solar_inverters`, `batteries` are **not** reference tables any more; they become `catalog` components with public profiles.)
**`emi_bank`** (`slug` PU, `name`, `logo_id` S, `is_active`, `sort_order`), **`emi_interest_rate_rule`** (`bank_id` S null, `min_amount`, `max_amount`, `tenure_months_min/max`, `annual_rate numeric(6,4)`, `processing_fee_pct`, `effective_from/to`), **`emi_subsidy_rule`** (`scheme varchar(24)` PM_SURYA_GHAR, `kw_from`, `kw_to`, `amount_per_kw`, `cap_amount`, `effective_from/to`), **`emi_settings`** (singleton: `default_down_payment_pct`, `default_tenure_months`, `disclaimer_en/ml`). `emi_system_size` is **removed**: sizes and prices come from `packs_release_pack`.

### 2.9 HR — `hr`, `attendance`, `devices`

**`hr_office`** (`code` PU, `name`, `address`, `timezone varchar(60)` validated against zoneinfo, `default_shift_id` S, `is_active`), **`hr_shift`** (`code` PU, `name`, `start_time`, `end_time`, `is_overnight`, `overnight_buffer_minutes` default 180, `grace_minutes` 10, `late_threshold_minutes` 0, `early_exit_threshold_minutes` 15, `full_day_minutes` 480, `half_day_minutes` 240, `half_day_after_minutes` 30, `break_minutes` 60, `auto_deduct_break`, `debounce_minutes` 2, `overtime_enabled`, `overtime_after_minutes` 480, `working_days jsonb`, `weekly_off_days jsonb`, `is_active`), **`hr_employee`** (`code` PU, `full_name`, `office_id` S, `shift_id` S, `department`, `designation`, `email`, `phone_e164`, `joined_on`, `left_on`, `identity_method varchar(12)`, `photo_id` FK→media(private) S, `is_active`), **`hr_holiday`** (`office_id` C null = all, `date`, `name`, `is_active`; PU(office_id, date) plus PU(date) WHERE office_id IS NULL), **`hr_leave_type`** (`code` PU, `name`, `paid bool`, `requires_approval bool`), **`hr_leave_record`** (`employee_id` C, `type_id` P, `date_from`, `date_to`, `is_half_day`, `status` PENDING/APPROVED/REJECTED/CANCELLED, `reason`, `decided_by` S, `decided_at`), **`hr_attendance_rule`** (`name` PU, `office_id` C null, `shift_id` C null, `rules jsonb` (half_day_after, half_day_after_minutes, half_day_under_minutes), `effective_from`, `is_active`, `notes`).

**`attendance_raw_punch`** *(no base; append-only, `REVOKE UPDATE, DELETE`)* — `id bigserial`, `device_id` FK→devices_device RESTRICT, `device_serial`, `device_record_uid int` null, `pin varchar(80)`, `device_time timestamp` (naive as reported), `punch_at timestamptz` (office tz applied), `status_code smallint`, `punch_code smallint`, `source varchar(12)` (AGENT_PUSH, ADMS_PUSH, IMPORT), `agent_id` S, `adms_request_id` S, `dedup_key char(64)` unique (sha256 of serial|pin|device_time|status|punch), `raw_payload jsonb`, `received_at`. Indexes (device_id, pin, device_time), (punch_at). Monthly partitions.
**`attendance_day`** — `employee_id` C, `work_date date`, `office_id` S, `shift_id` S, `first_device_id` S, `first_in timestamp`, `last_out timestamp`, `punch_count smallint`, `working_minutes int`, `break_minutes int`, `late_minutes int`, `early_exit_minutes int`, `overtime_minutes int`, `is_late`, `is_early_exit`, `status varchar(12)` (PRESENT, LATE, ABSENT, HALF_DAY, WEEKLY_OFF, HOLIDAY, ON_LEAVE), `worked_on_off_day bool`, `leave_conflict bool`, `missing_out bool`, `source_raw_ids jsonb`, `ignored_raw_ids jsonb`, `processing_version varchar(24)`, `computed_at`, `is_corrected bool`. PU(employee_id, work_date). Index (office_id, work_date), (work_date).
**`attendance_correction`** — `day_id` C, `field`, `old jsonb`, `new jsonb`, `reason`, `revoked_at`, `revoked_by` S.

**`devices_agent`** (`code` PU, `name`, `office_id` S, `credential_id` FK→core_service_credential S, `version`, `hostname`, `platform`, `local_ip`, `last_heartbeat_at`, `last_device_contact_at`, `last_sync_at`, `last_error`, `queued_records`, `failed_uploads`, `heartbeat_interval_seconds` 60, `sync_interval_seconds` 300, `offline_after_seconds` 300, `degraded_queue_threshold` 500, `settings jsonb`, `is_active`), **`devices_device`** (`name`, `serial_number` PU, `expected_serial`, `expected_mac`, `mac_address`, `office_id` S, `agent_id` S, `ip_address inet` null, `port` 4370, `comm_password int`, `timeout_seconds`, `model`, `firmware_version`, `platform`, `adms_enabled bool`, `adms_token_hash char(64)`, `adms_allowed_ips jsonb`, `adms_registration_state`, `adms_last_seen_at`, `adms_last_handshake_at`, `adms_last_push_at`, `adms_last_command_poll_at`, `adms_source_ip`, `adms_request_count`, `adms_options jsonb`, `identity_status varchar(20)` (VERIFIED, IDENTITY_MISMATCH, UNVERIFIED), `identity_message`, `identity_checked_at`, `last_seen_at`, `last_sync_at`, `last_punch_at`, `last_error`, `clock_offset_seconds int`, `user_count`, `attendance_count`, `device_info jsonb`, `notes`, `is_active`; health states are derived from timestamps, no `is_online` column), **`devices_device_user`** (`device_id` C, `pin` , `device_uid int`, `name`, `privilege smallint`, `card`, `group_id`, `has_password`, `employee_id` S, `raw_payload jsonb`, `first_seen_at`, `last_seen_at`; PU(device_id, pin)), **`devices_sync_log`** *(no base)*, **`devices_protocol_mapping`** (as today + PU(device_platform, firmware_version, field, raw_value)), **`devices_adms_request`** *(no base; body capped at 1 MiB; purged after 30 days; monthly partitions)*, **`devices_adms_unknown_device`**.

**Attendance engine v4 — behaviour changes against the eSSL v3 engine (each is a golden-test case; referenced as A1–A12 elsewhere):**

| # | eSSL v3 behaviour | v4 rule |
|---|---|---|
| A1 | PIN identity is global across devices | identity is `(device, pin)`; links and unmapped lists are per device |
| A2 | break deducted only when span > break (60 min → 60 working, 61 → 1) | `break_deducted = min(break_minutes, max(0, gross − half_day_minutes))` |
| A3 | no debounce (double scan = IN+OUT, 0 min, PRESENT) | punches within `debounce_minutes` of the previous accepted punch are ignored (raw rows kept, listed in `ignored_raw_ids`) |
| A4 | leave/holiday/weekly-off ignored when punches exist | worked holiday/weekly-off keep their status with `worked_on_off_day=true` and hours counted as overtime; full-day leave with punches → status from punches + `leave_conflict=true` |
| A5 | half-day deadline is 10:00 wall clock | `shift.start + half_day_after_minutes`, overridable by `hr_attendance_rule` |
| A6 | overnight shift has no end buffer | punches up to `end_time + overnight_buffer_minutes` belong to the previous work date |
| A7 | no future-date guard; first push of the day stores ABSENT for everyone | rows never written for dates ≥ today (office tz); `finalise_day` Beat task writes yesterday at 00:30 office time |
| A8 | recompute only on ADMS push, synchronously in the request | every ingestion publishes `attendance.punches_ingested`; debounced Celery recompute; rule/holiday/leave/link edits recompute the affected range |
| A9 | reports mix stored-only and calendar-filled sources; two office-% formulas | one `calendar_fill` service; `% = (present + 0.5 × half) / expected`; leave excluded from the denominator; unambiguous status codes |
| A10 | naive device time everywhere; "today" in server tz | `device_time` kept + `punch_at` from `office.timezone`; office tz for every "today"; `clock_offset_seconds` measured, never corrected |
| A11 | dedup by device record uid | content hash dedup across transports |
| A12 | manual override column exists but no API | `attendance_correction` with reason; corrected days skipped by recompute until revoked |

**Site-inspection readiness (`engines.inspection_readiness`) — the 19 blocker codes, evaluated in this order:** `CUSTOMER_REQUIRED`, `ENGINEER_REQUIRED`, `VISIT_DATE_REQUIRED`, `PANEL_PHOTO_REQUIRED`, `EQUIPMENT_PHOTO_REQUIRED`, `PANEL_WIDTH_REQUIRED`, `PANEL_HEIGHT_REQUIRED`, `EQUIPMENT_WIDTH_REQUIRED`, `EQUIPMENT_HEIGHT_REQUIRED`, `LOCATIONS_NOT_DOCUMENTED` (both current annotations), `RESTRICTIONS_NEED_REMARKS`, `SUITABILITY_UNCONFIRMED` / `SITE_NOT_SUITABLE` / `SUITABILITY_CONDITIONAL`, `CUSTOMER_APPROVAL_REQUIRED` / `CUSTOMER_APPROVAL_OUTDATED` (location snapshot differs), `ENGINEERING_REVIEW_PENDING`, `ADDITIONAL_WORK_REJECTED` (and, per D-12, `ADDITIONAL_WORK_UNAPPROVED`), `EQUIPMENT_<TYPE>_INCOMPLETE` / `EQUIPMENT_<TYPE>_NEEDS_RESOLUTION` (WAIVED counts as resolved), `WHEELING_CONSUMER_NUMBER_REQUIRED`, `WHEELING_PHONE_REQUIRED`, `NEUTRAL_OR_TERMINATION_DECISION_REQUIRED` (enum comparison), `LATEST_APPROVAL_NOT_APPROVED`. Field completion (`engines.inspection_readiness.field_completion`) is the subset enforced at submit.

### 2.10 Constraint and index policy (applies everywhere)
- Every lifecycle invariant is a partial unique index (one DRAFT, one PENDING, one current, one primary).
- Every enum column has a `CHECK`.
- Every FK has an index; composite indexes follow the list-screen filters (`status, created_at`; `owner_id`; `office_id, work_date`).
- JSONB used only for: validated documents (payloads, snapshots, configs with a schema), rule parameters, and free-form metadata. Never for anything that is filtered or joined in a list screen.
- Migrations follow expand → migrate data → contract; no migration both adds and drops a column used by running code.

---
## 3. API design

### 3.1 Conventions

| Item | Rule |
|---|---|
| Namespaces | `/api/public/v1/…` website (AllowAny, `authentication_classes=[]`, cached, throttled) · `/api/v1/…` staff (JWT) · `/api/agent/v1/…` office agents (service token) · `/api/customer/v1/…` customer self-service links (signed token + OTP) · `/iclock/<device_token>/…` terminals (plain Django) · `/api/docs/` OpenAPI · `/healthz` |
| Identifiers | `uid` (UUID) in every path and body. Integer ids never leave the service layer. |
| Envelope | Success: the resource or `{results, count, next, previous}` (page-number pagination, `page_size` default 25, max 200; cursor pagination on audit and raw punches). Error: `{code, message, errors:{field:[…]}, error_codes:[…]}`. |
| Verbs | CRUD via DRF viewsets; **workflow actions are POST sub-resources** (`…/issue/`, `…/publish/`, `…/approve/`) with a body carrying `reason`/`note` where the standard's audit rule needs it. Never PATCH a `status`. |
| Concurrency | Every PATCH/POST action on a versioned row accepts `expected_version`; mismatch → 409 `stale_version`. |
| Idempotency | `Idempotency-Key` header honoured on public POSTs (leads, OTP) and machine POSTs (agent uploads); stored 24 h in Redis. |
| Filtering | `?filter[field]=`, `?search=`, `?ordering=`; every list endpoint declares its filter set in OpenAPI; no free `?q=` SQL. |
| Caching | Public GETs: `Cache-Control: public, max-age=60`, `ETag` = release number + updated_at; server-side Redis keyed by version. |
| Throttles | scopes: `public_read` 600/min/IP, `public_write` 20/min/IP, `otp` 5/10min/phone, `login` 10/15min/IP, `staff` 1200/min/user, `agent` 120/min/token, `iclock` 300/min/device, `customer` 60/min/token. |
| Permissions | one `HasModulePermission(module, {GET:'view', POST:'create', …})` per view; actions declare their `(module, action)` explicitly; scope filter applied in `get_queryset()`; default deny on unmapped method. |
| Versioning | URL major only. Breaking change = `/v2/` beside `/v1/` for one release. |
| Legacy | Old website paths under `/api/…` and `/studio-api/api/…` are served by `legacy/` adapters while `LEGACY_API_SHIM` is on (§6). |

### 3.2 Permission registry (final, closed)

Verbs: `view create edit publish verify archive manage approve submit lock commit issue revise assign release export sync`.

| Group | Module | Actions |
|---|---|---|
| — | `dashboard` | view |
| PRODUCT | `catalog` | view, create, edit, approve (status changes), archive |
| | `pricing` | view, edit (manual price rows, cost config, statutory fees), publish (PriceRelease) |
| | `pricing_internal` | view (landed cost, margins) |
| | `market_rates` | view, edit, publish |
| | `offers` | view, create, edit, approve, publish, archive |
| | `procurement` | view, create, edit, commit |
| | `inventory` | view, edit (stock, flag-gated) |
| CONFIG | `bom` | view, edit |
| | `packs` | view, edit, submit, approve, publish |
| | `engineering` | view, verify (run checker), approve (acknowledge/waive) |
| SALES | `leads` | view, create, edit, archive, manage (assign) |
| | `customers` | view, create, edit, archive, manage (merge) |
| | `quotations` | view, create, edit, issue, revise, approve (discount), archive |
| | `quotation_content` | view, edit, publish |
| | `agreements` | view, create, edit, issue, manage |
| | `site_inspections` | view, create, edit, assign, submit, approve, release, archive |
| | `projects` | view, create, edit, lock, archive |
| WEBSITE | `pages` | view, edit, publish, verify |
| | `blogs` | view, create, edit, publish, verify, archive |
| | `faqs` | view, create, edit, publish, verify, archive |
| | `media` | view, create, edit, archive |
| | `seo` | view, edit, publish |
| | `products_public` | view, edit, publish (public profiles) |
| | `reference_data` | view, create, edit, archive |
| | `emi` | view, edit |
| CAREERS | `job_positions` | view, create, edit, publish, verify, archive |
| | `applications` | view, edit, archive |
| | `departments` | view, create, edit, archive |
| | `career_page` | view, edit, publish |
| HR | `employees` | view, create, edit, archive |
| | `hr_setup` | view, edit |
| | `attendance` | view, edit, export, manage |
| | `leave` | view, create, approve, archive |
| | `devices` | view, create, edit, sync, manage |
| ADMIN | `company` | view, edit |
| | `users` | view, create, edit, archive, manage |
| | `roles` | view, create, edit, manage |
| | `settings` | view, edit (feature flags, integrations) |
| | `audit` | view |

Scopes available per module: `customers`, `quotations`, `agreements`, `leads` → all/owned; `site_inspections` → all/owned/assigned; `employees`, `attendance`, `leave` → all/office/self; everything else → all. Seeded roles (`is_system=true`, created with `get_or_create`, editable by Admin afterwards):

| Role | Grants | Scopes |
|---|---|---|
| Super Admin | every module, every action | all |
| Admin | everything except `users.manage` on Super Admins; includes `pricing.publish`, `packs.publish`, `offers.*`, `settings`, `audit`, `devices.manage`, `quotations.create` | all |
| Content Manager | `dashboard`; `pages`, `blogs`, `faqs`, `media`, `seo`, `products_public`, `career_page` full; `company.view` | all |
| HR | `dashboard`; `job_positions`, `applications`, `departments` full; `media` view/create; `employees`, `hr_setup`, `attendance`, `leave` full; `devices` view/sync | all |
| Office Manager | `employees.view`; `attendance` view/export; `leave` view/approve | office |
| Staff | `dashboard`; `attendance.view`; `leave` view/create | self (assigned automatically when an employee is linked to a user) |
| Sales Executive | `dashboard`; `leads` view/create/edit; `customers` view/create/edit; `quotations` view/create/edit/issue/revise; `agreements` view/create/edit; `site_inspections` view/create; `emi.view`; `quotation_content.view`; `packs.view` | owned on leads, customers, quotations, agreements, site_inspections |
| Sales Head | Sales Executive + `quotations.approve` (discounts), `leads.manage`, `customers.manage`, `agreements` issue/manage, `site_inspections` assign | all |
| Project Head | `catalog` view/create/edit/approve; `pricing` view/edit; `pricing_internal.view`; `market_rates` view/edit; `procurement.view`; `bom` full; `packs` view/edit/submit; `engineering` view/verify; `projects` full; `quotation_content` view/edit/publish; `quotations.view`; `site_inspections` view/assign/approve/release; `agreements.view`; `company.view` | all |
| Engineering | `catalog.view`; `bom.view`; `packs.view`; `engineering` view/verify/approve; `projects.view`; `site_inspections` view/approve | all |
| Field Engineer | `dashboard`; `site_inspections` view/edit/submit; `media.create`; `customers.view` | assigned on site_inspections |
| Procurement | `catalog` view/create/edit; `procurement` full incl. commit; `pricing_internal.view`; `inventory` view/edit | all |

Permission-only modules (no dedicated endpoint): `pricing_internal` unlocks landed-cost and margin fields on pricing, packs and quotation responses; `dashboard` gates `/api/v1/dashboard/`. No role gets `attendance.edit` or `leave.approve` on its own record (`deny_self_action` guard).

### 3.3 Public API (website) — `/api/public/v1/`

| Method | Path | Serves | Notes |
|---|---|---|---|
| GET | `content/{collection}` | blog / case-studies / authors | **Strapi-v5-flat contract preserved**: `populate=*`, `filters[slug][$eq]`, `fields[n]`, `pagination[page]`, `pagination[pageSize]`, `sort[n]`; response `{data:[…], meta:{pagination:{page,pageSize,pageCount,total}}}` |
| GET | `content/{collection}/{slug}` | single entry | same payload as one-item list |
| GET | `pages/{slug}` | maintained page text/image slots + SEO | replaces `/api/page-content?page=` |
| GET | `faqs?page=&category=` | published FAQs | |
| GET | `job-positions`, `job-positions/{slug}` | published positions | |
| POST | `job-applications` | multipart resume | private media; throttled `public_write`; honeypot + Turnstile token optional |
| GET | `products/panels`, `products/inverters`, `products/batteries`, `products/{category}/{slug}` | catalog public profiles + spec + price range from the current PriceRelease | replaces `/api/solar-panels/`, `/api/solar-inverters/`, `/api/batteries/` |
| GET | `packs` , `packs/{system_type}/{tier}/{size_kw}` | current PackRelease prices and BOM summary (no cost/margin) | replaces `/bom/api/calculate/` reads for display |
| POST | `calculators/basic` | `engines.energy/subsidy` on published packs | replaces `calculate-solar/`, `calculate-solar-new/` |
| POST | `calculators/advanced` | appliance-based sizing | replaces `calculate-solar-advanced/` |
| GET | `calculators/emi/config` · POST `calculators/emi` · POST `calculators/emi/quotation` | banks, rules, computed schedule via `engines.finance` | replaces `emi-calculator*` |
| GET | `reference/pincodes/{pincode}`, `reference/tariffs`, `reference/device-types`, `reference/wattages`, `reference/room-sizes`, `reference/ev-cars`, `reference/ev-scooters` | reference lists | read-only; long cache |
| POST | `otp/send`, `otp/verify` | Twilio Verify | throttle `otp`; verification token returned for the lead POST |
| POST | `leads` | all website forms (`kind` discriminates) | requires OTP token for phone-bearing kinds; Idempotency-Key |
| POST | `affiliate-applications`, `warranty-requests` | forms | |
| GET | `installations?pincode=` , `installations/stats` | showcase map / stats | replaces `customer-installations`, `installation-stats` |
| GET | `testimonials` | website testimonials (`show_on_website`) | replaces `/bom/api/quotation-testimonials/` |
| GET | `company` | public company profile (phone, address, trust stats, social, quotation display settings) | replaces `/bom/api/quotation-settings/` |
| GET | `seo/metadata/{page}` , `seo/redirects` | page metadata; redirect list for `next.config` build step | replaces `/api/metadata/` |
| GET | `sitemap/entries` | slugs + lastmod for `next-sitemap` | |
| POST | `webhooks/revalidate-ack` | frontend acknowledges revalidation | optional |

### 3.4 Staff API — `/api/v1/` (grouped; every list has filters, search, ordering)

**Auth & account:** `POST auth/login` (email+password, returns access+refresh; Studio BFF stores them), `POST auth/refresh`, `POST auth/logout` (blacklists), `GET auth/me` (user, role, permissions, scopes), `POST auth/password/change`, `POST auth/password/reset-request`, `POST auth/password/reset`, `GET auth/sessions`, `DELETE auth/sessions/{uid}`.

**Admin:** `users/` CRUD + `…/deactivate/`, `…/reactivate/`, `…/force-reset/`; `roles/` CRUD + `GET roles/registry/`; `audit/?object_type=&object_uid=&actor=&action=&from=&to=` (cursor); `settings/flags/` GET/PATCH; `settings/integrations/` GET/PUT (secrets write-only); `company/profile/` GET/PATCH; `company/bank-accounts/` CRUD + `…/make-primary/`.

**Media:** `media/` list, `POST media/upload/` (multipart; `visibility`, `kind`, `folder`), `PATCH media/{uid}/` (alt/caption), `DELETE media/{uid}/` (refused 409 while referenced), `GET media/{uid}/signed-url/` (private).

**Catalog:** `catalog/brands/`, `catalog/categories/` CRUD; `catalog/components/` CRUD (+ nested spec by category), `POST …/{uid}/activate/`, `…/deprecate/` (`replacement_uid`, `reason`), `…/retire/`, `GET …/{uid}/history/`, `GET …/{uid}/usage/` (packs, quotations, agreements referencing it), `POST catalog/components/import/` (CSV, dry-run first), `GET catalog/components/export/`; `catalog/battery-families/` CRUD; `catalog/public-profiles/` CRUD + `…/publish/`, `…/unpublish/`.

**Pricing:** `pricing/prices/?component=&kind=` (history), `POST pricing/prices/` (manual LIST/LANDED row; closes previous), `pricing/current/` (view), `pricing/cost-config/` GET/PUT (writes new effective rows), `pricing/installation-matrix/` CRUD, `pricing/statutory-fees/` CRUD, `pricing/market-rate-sets/` CRUD + `…/rates/` bulk PUT + `…/swap-deltas/` + `…/roof-addons/` + `…/activate/`, `pricing/offers/` CRUD + `…/activate/`, `…/pause/`, `…/archive/`, `pricing/validity-policy/` GET/PUT, `GET pricing/releases/`, `GET pricing/releases/{number}/`, `POST pricing/releases/preview/` (publish report without writing), `POST pricing/releases/` (publish), `GET pricing/releases/current/diff/?against=`.

**Procurement:** `procurement/suppliers/` CRUD; `procurement/batches/` CRUD (DRAFT only editable) + `…/lines/` + `…/charges/` + `POST …/preview-allocation/` + `POST …/commit/` + `POST …/reverse/`; `GET procurement/price-master/` (current purchase/landed per component with batch refs).

**BOM & packs:** `bom/templates/`, `bom/slots/`, `bom/fixed-items/`, `bom/structure-templates/`, `bom/structure-items/`, `bom/tube-weights/` CRUD; `POST bom/build/` (compute BOM for size/phase/tier/structure — dry, no persistence); `packs/config-versions/` list/detail, `POST packs/config-versions/` (new draft from approved), `PATCH …/{uid}/` (config), `GET …/{uid}/packs/`, `PUT …/{uid}/packs/{key}/`, `POST …/{uid}/run-checker/`, `POST …/{uid}/submit/`, `…/approve/`, `…/reject/`, `POST packs/releases/preview/`, `POST packs/releases/` (publish; requires APPROVED version + current PriceRelease), `GET packs/releases/`, `GET packs/releases/current/`, `GET packs/releases/{n}/packs/`, `GET packs/compare/?a=&b=`.

**Engineering:** `engineering/rule-sets/` list + `POST …/activate/`; `engineering/runs/` list/detail; `POST engineering/findings/{uid}/acknowledge/`.

**Sales:** `leads/` CRUD + `…/assign/`, `…/convert/` (creates customer), `…/notes/`, `…/events/`; `customers/` CRUD + `…/merge/` (`into_uid`), `…/notes/`, `GET …/{uid}/timeline/` (quotations, agreements, inspections, projects); `quotations/` list/detail, `POST quotations/` (customer + system options), `GET quotations/system-options/?…` (valid sizes/tiers/phase for the customer's inputs from the current PackRelease), `PATCH quotations/{uid}/versions/{n}/` (draft selections), `POST …/versions/{n}/preview/` (payload + gate report, no write), `POST …/versions/{n}/issue/` (gate must pass; renders), `POST …/revise/` (new version from the current release), `POST …/discount-requests/`, `POST …/discount-requests/{uid}/approve|reject/`, `POST …/accept/`, `…/cancel/`, `GET …/versions/{n}/document/` (signed URL), `POST …/versions/{n}/send/` (email/WhatsApp), `GET quotations/{uid}/history/`; `quotation-content/versions/` list, `POST` draft, `PATCH`, `POST …/fit-check/`, `POST …/publish/`; `quotation-content/inclusions/`, `…/tier-names/`, `…/testimonials/`, `…/campaigns/` CRUD.

**Agreements:** `agreements/` list/detail, `POST agreements/from-quotation/` (`quotation_version_uid`, `kind`, `language`), `POST agreements/` (blank SALE_ORDER / EXTRA_STRUCTURE with `site_inspection_uid`), `PATCH …/{uid}/` (DRAFT), `POST …/issue/`, `…/supersede/`, `…/cancel/`, `…/record-acceptance/` (paper), `GET …/document/`, `POST …/render/?language=`, `POST …/price-override/` (flag-gated, `agreements.manage`, reason).

**Site inspections:** as designed in §2.7: `site-inspections/` list/detail (scoped, no prices), `POST` (PRE_SALE), `PATCH …/{uid}/stages/{stage}/` (allow-list per stage), `…/assign/`, `…/submit/`, `…/approval/request/`, `…/hold/`, `…/resume/`, `…/revision/`, `…/release/`, `…/photos/` (multipart) + delete, `…/annotations/`, `…/equipment/{type}/` GET/PUT, `…/equipment/{type}/review/`, `…/observations/`, `…/additional-work/` CRUD + `…/transition/`, `…/reviews/` (engineering review decide), `GET …/readiness/`, `GET …/activity/`, `GET …/report/?variant=customer|internal`, `GET engineer/site-inspections/` (queue). Customer side: `/api/customer/v1/inspection-approvals/{token}/` GET, `POST …/send-otp/`, `POST …/respond/`.

**Projects:** `projects/` CRUD + `…/lock-bom/`, `…/cost-inputs/`, `…/commission/`, `…/close/`.

**Website content:** `content/collections/`, `content/templates/` (+ image-groups, attribute-slots), `content/authors/`, `content/categories/`, `content/tags/`, `content/badges/`, `content/entries/` CRUD + `…/publish/`, `…/unpublish/`, `…/schedule/`, `…/archive/`, `…/restore/`, `…/verify/`, `…/preview/` (signed preview token), `…/slug-history/`; `pages/` + `…/text-slots/{key}/`, `…/image-slots/{key}/`, `…/seo/`, `…/publish/`; `faqs/`, `faq-categories/` + `faqs/reorder/`; `careers/departments/`, `careers/positions/` + `…/publish|unpublish|close|archive/`, `careers/applications/` + `…/status/`, `…/assign/`, `…/notes/`, `…/download/{kind}/` (signed); `seo/overview/`, `seo/metadata/`, `seo/redirects/`; `reference/*` CRUD (7 lists); `emi/banks/`, `emi/interest-rules/`, `emi/subsidy-rules/`, `emi/settings/`; `dashboard/` (counts per module the user can view).

**Inventory (flag `INVENTORY_STOCK`):** `inventory/locations/` CRUD; `inventory/movements/` list + `POST` (append-only); `GET inventory/balances/?component=&location=`.

**HR (`hr_setup`, `employees`, `leave`):** `hr/offices/` CRUD + `GET …/{uid}/summary/?day=`; `hr/shifts/` CRUD; `hr/employees/` CRUD + `…/deactivate/`, `…/activate/`, `…/link-user/`, `…/unlink-user/`, `…/photo/` (private upload), `GET …/device-mappings/`, `GET …/dependencies/`, `POST hr/employees/reconcile-devices/` (`read_devices`, `apply`, `confirm`); `hr/holidays/` CRUD (soft delete); `hr/leave-types/` CRUD; `hr/leave/` list (scoped) + `POST` (Staff self-service → PENDING; HR → APPROVED) + `…/approve/`, `…/reject/`, `…/cancel/`; `hr/attendance-rules/` CRUD.

**Attendance (`attendance`):** `attendance/days/` (scoped; filters `date_from`, `date_to`, `employee`, `office`, `status`, `search`; ordering), `attendance/raw/` (cursor; `device`, `pin`, `employee`, `date_from/to`), `GET attendance/employees/{uid}/timeline/?work_date=`, `attendance/calendar/?employee=&year=&month=`, `attendance/calendar/all/?year=&month=&office=`, `attendance/day/?day=&office=`, `attendance/date-ranges/?office=`; `POST attendance/process/` (`date_from`, `date_to`, `employee_uids`), `POST attendance/process-all/`, `POST attendance/recalculate/` (`manage`); `attendance/corrections/` list + `POST` (`day_uid`, `field`, `new`, `reason`) + `…/revoke/` (`edit`); `attendance/reports/{daily|weekly|monthly|monthly-detail|monthly-individual|individual|office}/?format=json|csv|xlsx|pdf&…` (`export`; > 5,000 rows → async RenderJob + signed URL); `attendance/dashboard/summary/?day=`, `…/recent-punches/`, `…/trend/?days=` (scoped).

**Devices (`devices`):** `devices/` CRUD (validated serializer) + `GET devices/mapping/`, `POST …/{uid}/refresh-employees/`, `GET …/employee-reconciliation/`, `GET …/user-reconciliation/`, `GET …/logs/`, `POST …/rehome/` (`manage`), `POST …/adms/enable/` (issues the device token, `manage`), `…/adms/disable/`; `devices/device-users/` list (`device`, `linked`, `search`, `device_state`, `software_state`) + `POST …/{uid}/link/` (`employee_uid` or null, **this device row only**), `POST devices/device-users/auto-link/?device=`, `POST …/{uid}/resolve/` (`LINK_EXISTING` | `CREATE_EMPLOYEE`, `confirm`), `GET devices/device-users/unmapped/?device=`, `POST devices/device-users/map-pin/` (`device_uid`, `pin`, `employee_uid`); `devices/agents/` CRUD + `POST …/rotate-token/` (token shown once), `…/revoke/`, `GET …/logs/`, `GET …/config-download/` (`agent.ini` with the one-time token); `devices/protocol-mappings/` CRUD + `GET …/observed/`; `GET devices/adms/status/`, `GET devices/adms/requests/?serial=&kind=` (cursor), `GET devices/adms/unknown-devices/`.

**Agent protocol** (`/api/agent/v1/`, service token, Idempotency-Key on uploads): `GET config`, `POST heartbeat`, `POST devices/announce` (409 `device_bound_elsewhere` when the serial belongs to another agent), `POST devices/identity-mismatch`, `POST devices/discovery`, `POST sync/users`, `POST sync/attendance` (batches ≤ 200, idempotent by dedup key), `GET sync-status`.

**Terminals** (`/iclock/<device_token>/{cdata,getrequest,devicecmd,registry,ping}`, plain HTTP, behind `ADMS_RECEIVER`): same pipeline as the eSSL receiver (evidence row, classify, resolve by serial **and** token, quarantine unknowns, ATTLOG ingest → `attendance.punches_ingested`), handshake `TransFlag=AttLog OpLog` only, every reply HTTP 200 `text/plain`.

### 3.5 Events (outbox) consumed across contexts

| Event | Producer | Consumers |
|---|---|---|
| `pricing.release_published` | pricing | cache bump; packs (mark stale drafts); website revalidation (`/solar-comparison`, `/emi-calculator`) |
| `packs.release_published` | packs | cache bump; calculators; website revalidation; quotations (warn open drafts) |
| `quotation_content.published` | quotations | cache bump |
| `quotations.issued` | quotations | notifications (email/WhatsApp), leads (mark CONVERTED), audit |
| `quotations.accepted` | quotations | agreements (draft PA created) |
| `agreements.issued` / `agreements.superseded` | agreements | site_inspections (create or link / refresh snapshot) |
| `site_inspections.released` | site_inspections | projects (optional auto-create, D-8) |
| `attendance.punches_ingested` | devices | attendance (debounced recompute) |
| `hr.employee_deactivated` | hr | accounts (deactivate user, blacklist sessions) |
| `blog.entry_published` etc. | blog/sitepages/faqs | website revalidation webhook (existing contract: POST `${FRONTEND_REVALIDATE_URL}` with shared secret and paths) |

---

## 4. Implementation plan

### 4.1 Team and tracks

Assumed team: **3 backend engineers (BE1, BE2, BE3), 2 frontend engineers (FE1, FE2), 1 QA/release engineer part-time.** With only 2 backend engineers, BE3's track (HR) starts after BE2 finishes the website track and the calendar stretches by ~8 weeks; nothing else changes.

Series backbone (cannot be parallelised): **Phase 0 → Phase 1 (core) → Product master & pricing → Packs/BOM → Quotations & documents → Agreements → Site inspections.** Everything else hangs off Phase 1 in parallel.

```
Week   1   2   3   4   5   6   7   8   9  10  11  12  13  14  15  16  17  18  19  20  21  22  23  24  25  26
BE1   [P0 ][  Phase 1 core  ][ catalog+pricing+procure ][packs/bom/eng][ quotations+docs ][agreem][ site inspections ][hard+M5]
BE2   [P0 ][  Phase 1 core  ][engines][  CMS content apps  ][website apps][legacy shim+parity][CMS+BE+Flarize mig][cutover M1-M3][SI help][hard]
BE3   [P0 ][  Phase 1 core  ][    hr + attendance + devices + agent     ][docs pipeline][attendance mig][M4 HR cutover][SI/agree help][hard]
FE1   [studio shell, BFF auth, RBAC nav][ catalog/pricing screens ][packs/BOM][   quotation wizard   ][agreements][ SI wizard port  ]
FE2   [website rebinding prep][content/pages/faq/careers screens][website public API rebinding][HR screens          ][customer approval][polish]
Milestones:            M0=W4        M1=W18 website+content cutover   M2=W19 Studio(content) cutover   M3=W20 pricing/quotations live (Flarize retired)
                                    M4=W21 HR live (eSSL retired)    M5=W25 agreements+SI live (PA/SI retired) · W26 decommission
```

### 4.2 Phase 0 — Stop the bleeding (Week 1, all engineers, series)
- Rotate Twilio, both Django secret keys, DB passwords, Bunny keys; purge `goldenray-backend/.env` and Flarize `catalog.json` `adminPassword` from git history; gitleaks in CI; pre-commit blocks `.env*`.
- Hot-fixes on the running systems (they stay in production ~15 more weeks): `GET /api/solar-installations/` 500; `/bom/` open redirect and GET logout; remove `seed_catalog` from `BomCalculateView`; throttle `send-otp` and `lead-collection-home`; `NUM_PROXIES`; CMS `BLACKLIST_AFTER_ROTATION` + `token_blacklist`; CMS user serializer rejects self-role changes.
- New repo `flarize-platform` (decision D-1: standalone repo). Skeleton from the CMS project layout; Docker multi-stage; compose with `db`, `pgbouncer`, `redis`, `api`, `worker`, `beat`, `nginx`; CI (lint, tests, `makemigrations --check`, import-linter, secret scan, coverage gate).
- Exit: CI green on an empty project; secrets rotated; hot-fixes deployed.

### 4.3 Phase 1 — Platform core (Weeks 2–4, all three backend engineers, series; split by file, merged daily)
BE1: settings package, `core.BaseModel`, soft delete, optimistic locking, `DomainError`, exception handler, `SequenceCounter`, `FeatureFlag`, outbox + drain task, Celery/Beat wiring, cache utils, client IP, throttles.
BE2: `accounts` (User, Role, registry, scopes, JWT RS256, blacklist, sessions, lockout, password reset), `HasModulePermission`, `BaseViewSet` with scope filters, seeds, `audit` (model, REVOKE migration, middleware), drf-spectacular, `/healthz`.
BE3: `media` (public/private, Bunny client, thumbnails, signed URLs, usage registry), `documents` (RenderJob, Playwright worker image, signed download), `ServiceCredential` + `ServiceTokenAuthentication`, `LegacyMap`, `migrations_tools` command skeleton with `--dry-run`.
FE1 in parallel: Studio shell against the OpenAPI stub (BFF login, HttpOnly cookies, RBAC-driven navigation, error envelope handling).
Exit (M0): 100 % tests on RBAC (registry normalisation, default deny, escalation guards, every scope), auth flows, audit append-only, outbox delivery, media signed URLs, document render of a hello-world template. Nothing app-specific exists yet.

### 4.4 Track A — Product master, pricing, configuration, sales (BE1, series)

| Weeks | Deliverable | Exit criteria |
|---|---|---|
| 5–8 | `catalog` (brands, categories with attribute schemas, components + 4 spec tables + tiers + public profiles + history + CSV import), `pricing` (price rows, current view, cost config, installation matrix, market rate sets, swap deltas, roof addons, statutory fees, offers, validity policy, **PriceRelease** with publish report), `procurement` (suppliers, batches, landed-cost allocation, commit), and the **catalog/pricing/bom part of `import_flarize` + `import_backend`** (the natural fixture for this work) | Publish a PriceRelease from imported Flarize + BOM data; publish report lists every data blocker (missing market rates, bt1 rating, PBC-M-003 controller); `engines.cost` golden tests pass |
| 9–11 | `bom` (templates, slots with qty rules, fixed items, structure templates, tube weights, `bom/build`), `packs` (config versions, typed packs/lines, submit/approve, **PackRelease** + compare), `engineering` (rule set with 33 PBC rules, runs, findings, acknowledgements) | Reproduce Flarize's approved pack config as a PackRelease whose per-pack prices equal Flarize's reference outputs (golden files, §8) |
| 12–15 | `customers`, `quotations` (orchestrator, system options, gate, snapshots, issue/revise/accept, discount requests, history, email log), `quotation_content` (bilingual versions, fit guard, inclusions, tier names, testimonials, campaign), quotation document template (goldenray React `QuotationV2` artwork ported to the HTML template, D-3), Sales/Admin dashboards | Issue a quotation end-to-end from Studio; frozen payload byte-identical on re-render; parity with 5 Flarize issued documents (numbers and inclusions) |
| 16–17 | `agreements` (three kinds, three languages, from-quotation, issue/supersede, acceptance, price override flag) | PA/SO/ES PDFs match the Purchase Agreement page text; `agreements.issued` is published (its consumer arrives with `site_inspections`) |
| 18–22 | `site_inspections` (models, per-stage serializers, lifecycle, readiness + checks engines, photos/annotations, approvals with OTP, additional work, engineering review, report), `projects` minimal | Golden tests for the 19 blockers; boundary test proves no commercial field reaches engineer serializers; release reachable from the UI |
| 23–25 | Hardening: pen-test checklist, load tests (public API 200 rps, PDF queue 50/min, ingestion), M5 cutover support | see §8 |

### 4.5 Track B — Engines, website content, website apps, legacy shim, migrations (BE2, parallel from Week 5)

| Weeks | Deliverable | Exit criteria |
|---|---|---|
| 5–6 | `engines/` port (money, energy, savings, subsidy, finance, bom_builder, device_allocation, pack_pricing, cost, pricing, engineering_checker, battery_compat, offers, gate) with golden-file parity tests from Flarize reference outputs; decisions D-7/D-8/D-9 applied as flags in the engines | parity suite green; no Django import in `engines/` |
| 7–10 | `blog`, `sitepages`, `faqs`, `careers` (positions/departments/career page), `seo`, `company`, `media` authoring views; public content delivery (Strapi-flat) | delivery JSON for every existing slug equals the old CMS output (recorded fixtures); all 26 CMS blueprint weaknesses closed |
| 11–13 | `leads` (all forms, OTP, workflow), `reference`, `calculators` (on PackRelease), `emi` (rules on `engines.finance`), `careers` applications with private resumes, public product endpoints, testimonials, installations | calculator outputs equal old endpoints for a recorded set of 200 inputs (differences listed and approved: the `'3P'` tariff fix, one EMI implementation) |
| 14–15 | `legacy/` shim: old paths → new services (`/api/articles`, `/api/page-content`, `/api/faqs`, `/api/job-positions`, `/api/calculate-solar*`, `/api/emi-calculator*`, `/api/lead-collection-home/`, `/api/send-otp/`, `/api/verify-otp/`, `/api/job-applications/`, `/api/metadata/`, `/api/solar-panels/` …, `/bom/api/quotation-testimonials/`, `/bom/api/quotation-settings/`); parity harness (replays production access-log samples against both stacks and diffs JSON) | diff report empty except approved differences |
| 16–18 | `import_cms`, `import_backend`, the remaining parts of `import_flarize` (customers, quotations, pack history, content) with dry-run, verification queries, two rehearsals on a production snapshot | §7 checklists pass on both rehearsals |
| 19–21 | M1/M2/M3 cutover execution (C1–C5, with BE1/BE3 on call), post-cutover fixes | old CMS and old backend receive no traffic (nginx access logs) |
| 22–25 | Site inspections help; `import_pa`, `import_si` | |

### 4.6 Track C — HR / attendance / devices (BE3, parallel from Week 5; independent of Track A)

| Weeks | Deliverable | Exit criteria |
|---|---|---|
| 5–6 | `hr` (offices, shifts, employees, holidays, leave types/records, rules) + APIs; Employee↔User link, Staff role automation | |
| 7–9 | `devices` (agents on service credentials, devices, device users, sync logs, protocol mappings, ADMS receiver behind flag, iclock views, agent protocol), `essl-agent` hardening (announce cadence, hash-gated user upload, cursor, purge, truthful reachability, persisted identity block) | agent end-to-end against a simulated terminal in CI (pyzk mock); iclock conformance tests from the recorded requests |
| 10–11 | `engines.attendance` port with golden tests + corrections A1–A12; recompute pipeline (outbox → Celery, debounce), `finalise_day` Beat task; calendar fill; reports; CSV/XLSX/PDF exporters; corrections API | old test expectations reproduced except the 12 documented behaviour changes |
| 12–13 | Documents pipeline for attendance PDFs; help BE1 with the quotation render pipeline (Playwright worker, fonts for Malayalam/Hindi) | |
| 14–15 | `import_essl` with per-device PIN link report and attendance status diff report | HR sign-off on the diff |
| 16–21 | M4 HR cutover (W21): agents reconfigured with new tokens; terminals untouched (agent-only, D-10) | eSSL Render/Vercel retired |
| 22–25 | Agreements/site-inspection help; hardening | |

### 4.7 Frontend tracks (FE1, FE2; parallel, gated by OpenAPI)
- FE1: Studio shell (W2–4) → catalog/pricing/procurement screens (W5–9) → BOM/packs/engineering (W10–12) → quotation wizard + documents + customers (W13–17) → agreements (W18) → site-inspection wizard port with autosave/offline draft (W19–24).
- FE2: website rebinding prep (`src/config.ts` single base URL, service files behind interfaces, W2–4) → Studio content screens: entries/pages/FAQs/careers/media/SEO/company (W5–11) → website rebinding to `/api/public/v1` + removal of `bomService`/`quotationSettingsService` (code W12–14, deployed at C7) → HR/attendance screens ported from the Vite pages (W15–20) → customer approval page (W21) → polish.

### 4.8 What is series and what is parallel (summary)

| Must wait for | Items |
|---|---|
| Phase 0 | everything |
| Phase 1 (M0) | all tracks |
| catalog + pricing (W8) | packs/bom, calculators on releases, public product pages, import_flarize pricing |
| packs release (W11) | quotations, EMI on packs, website pack prices, calculators cutover |
| quotations (W15) | agreements, quotation-content, M3 |
| agreements (W17) | site inspections (AGREEMENT origin), M5 |
| CMS apps + website apps + legacy shim + import_cms/import_backend rehearsed (W18) | M1 (W18), M2 (W19) |
| quotations + import_flarize complete (W18) | M3 (W20) |
| hr/attendance/devices + import_essl (W15) | M4 |
| Independent of each other | Track B vs Track C; engines port vs catalog; FE tracks vs each other; HR frontend vs sales frontend |

---
## 5. Deployment plan

### 5.1 Environments

| Env | Where | Data | Purpose |
|---|---|---|---|
| `dev` | docker compose on laptops | seeded fixtures + optional sanitised snapshot | development |
| `staging` | one VM (4 vCPU / 8 GB), same compose file as prod, `staging.flarize.com` | **weekly restore of a production snapshot, PII-masked** (phones, emails, resumes replaced) | migration rehearsals, parity runs, UAT |
| `prod` | the existing shared VM first (it already runs `db`, `backend`, `cms`, `frontend` on ports 8009/8012), then a dedicated VM (8 vCPU / 16 GB / 200 GB SSD) before M3 (D-16) | live | |

### 5.2 Services (compose, identical across envs; sizes are prod)

| Service | Image / role | Notes |
|---|---|---|
| `nginx` | TLS termination, routing (§5.4), static Next.js assets, rate limiting, `/media/private` X-Accel, **plaintext listener :8080 serving only `/iclock/`** (enabled at M4) | certbot sidecar |
| `frontend` | Next.js (existing image) | unchanged build; env `NEXT_PUBLIC_API_BASE_URL` etc. repointed at cutover |
| `api` | Django + gunicorn gthread (4 workers × 8 threads), `/healthz` readiness | 2 replicas behind nginx upstream for zero-downtime deploys |
| `worker-default` | Celery (outbox drain, notifications, recompute, imports) | concurrency 8 |
| `worker-documents` | Celery + Chromium/Playwright, Noto Sans Malayalam + Devanagari fonts | concurrency 2; separate queue so PDF bursts never block ingestion |
| `beat` | django-celery-beat | single replica |
| `pgbouncer` | transaction pooling, `max_client_conn` 500, pool 40 | |
| `db` | PostgreSQL 16, `shared_buffers` 2 GB, WAL archiving | volumes + nightly `pg_dump` + weekly base backup; 30-day retention; restore drill monthly |
| `redis` | 7.x, `maxmemory-policy allkeys-lru` for cache DB, separate DB index for Celery broker with `noeviction` | AOF on |
| `legacy-backend`, `legacy-cms` | the current containers, kept until W26 | traffic removed prefix by prefix (§6) |

### 5.3 Configuration and secrets
- All configuration via environment; `settings/prod.py` refuses to start on a placeholder `SECRET_KEY`, on `DEBUG=True`, without `ALLOWED_HOSTS`, without RS256 keys, or with `TRUSTED_PROXIES` empty.
- Secrets live in the VM's `/srv/flarize/.env` (root-only, 0600) injected by compose `env_file`; never in git; rotation runbook in `docs/ops/secrets.md`. Integration secrets (Twilio, Bunny, SMTP) are stored Fernet-encrypted in `company_integration` and editable by Admin in Studio.
- Migrations run as a one-off `api` command in the deploy script, never on container start (`RUN_MIGRATIONS_ON_START` pattern from eSSL is not repeated).

### 5.4 nginx routing (final state at W26; intermediate states in §6)

```
server flarize.com:443
  location /api/public/v1/  → api        (public, cached)
  location /api/v1/         → api        (staff JWT)
  location /api/agent/      → api        (service token)
  location /api/customer/   → api
  location /api/docs/       → api        (staff only, allow-list IPs)
  location /healthz         → api
  location /media/private/  → internal (X-Accel-Redirect from api after signed-URL check)
  location /studio          → frontend
  location /                → frontend
server flarize.com:8080 (plain HTTP)     → api, location ^~ /iclock/ only; everything else 404
```
Rate limits: `limit_req` zones per namespace (public 20 r/s burst 40 per IP; iclock 5 r/s per IP; agent 2 r/s per IP). `client_max_body_size` 20 MB on `/api/v1/media/upload/` and job applications, 2 MB elsewhere.

### 5.5 CI/CD
- GitHub Actions: lint → tests (Postgres + Redis services) → `makemigrations --check` → import-linter → coverage gate → gitleaks → build images → push to registry with the commit SHA.
- Deploy script (`deploy/release.sh <sha>`): pull images → `api migrate` (expand phase) → rolling restart `api` replicas one at a time behind nginx (health-checked) → restart workers/beat → smoke tests (`/healthz`, one public GET, one authenticated GET) → tag release. Rollback = redeploy previous SHA; schema changes are expand/contract so the previous code runs on the new schema.
- Environment promotion: every prod deploy has run on staging with the parity harness (§6.3) for at least one day during the cutover period.

### 5.6 Operations without a monitoring stack (per your constraint)
- Structured JSON logs (request id, user uid, duration, status) to stdout → docker log rotation → `journald`; `docs/ops/log-queries.md` with the `jq` one-liners for 5xx rate, slow queries, Celery failures.
- `/healthz` (DB, Redis, outbox lag, oldest queued render job) polled by the VM's cron; email to ops on failure.
- Weekly `ops report` management command: outbox backlog, failed render jobs, agent offline hours, ADMS unknown devices, audit volume.

### 5.7 Office deployment (HR)
- `essl-agent` shipped as a Windows service/`.exe` (PyInstaller) and a Linux systemd unit; `agent.ini` from Studio ("Download config" with the one-time token). One PC per office, always-on. Terminals stay pointed at nothing new until the ADMS controlled test (D-10).

---

## 6. Replacing the existing APIs (strangler cutover)

### 6.1 Principle
The website does not stop and the frontend is not rewritten to cut over. Traffic moves **prefix by prefix at nginx**, the new backend serves the **old contracts** through `legacy/` adapters until the frontend is rebound, and each move is reversible by one nginx reload.

### 6.2 Endpoint mapping (old → new)

| Old URL (host `flarize.com`) | Served today by | New canonical endpoint | Legacy shim path | Frontend file to rebind |
|---|---|---|---|---|
| `/studio-api/api/articles?populate=*…` (+ `case-studies`, `authors`) | CMS delivery | `/api/public/v1/content/{collection}` | `/studio-api/api/{collection}` | `src/services/blogApiService.ts`, `publicCmsService.ts` |
| `/studio-api/api/page-content?page=` | CMS | `/api/public/v1/pages/{slug}` | same path | `publicCmsService.ts` |
| `/studio-api/api/faqs`, `/job-positions[/slug]` | CMS | `/api/public/v1/faqs`, `/job-positions` | same paths | `publicCmsService.ts` |
| `/studio-api/admin-api/**` (Studio) | CMS authoring | `/api/v1/**` (§3.4) | **none** — Studio flips in one release (auth model differs) | `src/services/studioService.ts`, `src/config.ts`, `middleware.ts` |
| `/api/calculate-solar/`, `/calculate-solar-new/`, `/calculate-solar-advanced/` | main backend | `/api/public/v1/calculators/basic`, `/advanced` | same paths | `src/utils/fetchApi.ts` callers |
| `/api/emi-calculator/`, `/config/`, `/quotation/` | main backend | `/api/public/v1/calculators/emi*` | same paths | `emiConfigService.ts`, `quotationEmiService.ts` |
| `/api/emi-admin/**` | main backend (Studio) | `/api/v1/emi/*` | none | `studioService.ts` |
| `/api/send-otp/`, `/verify-otp/`, `/lead-collection-home/` | main backend | `/api/public/v1/otp/*`, `/leads` | same paths | `basicContactService.ts` |
| `/api/job-applications/**` | main backend | public POST `/api/public/v1/job-applications`; staff `/api/v1/careers/applications/**` | same paths | `careerApplicationService.ts`, `studioService.ts` |
| `/api/solar-panels/`, `/solar-inverters/`, `/batteries/` | main backend | `/api/public/v1/products/*` | same paths (read-only) | comparison page loaders |
| `/api/pincodes/`, `/tariffs/`, `/device-types/`, `/wattages/`, `/room-sizes/`, `/ev-cars/`, `/ev-scooters/` | main backend | `/api/public/v1/reference/*` | same paths (read-only) | `fetchApi` callers |
| `/api/customer-installations/`, `/installation-stats/` | main backend | `/api/public/v1/installations*` | same paths | |
| `/api/affiliate-applications/`, `/warranty-service-requests/` | main backend | `/api/public/v1/affiliate-applications`, `/warranty-requests` | same paths | |
| `/api/metadata/` | main backend | `/api/public/v1/seo/metadata/{page}` | same path | |
| `/bom/api/quotation-testimonials/`, `/bom/api/quotation-settings/` | main backend `bom` | `/api/public/v1/testimonials`, `/company` | same paths | `quotationTestimonialsService.ts`, `quotationSettingsService.ts` |
| `/bom/**` (Django templates, superuser login, calculate) | main backend `bom` | Studio screens on `/api/v1/packs`, `/bom`, `/pricing` | **none** (internal tool, users move to Studio) | — |
| Flarize Node servers (`localhost` utility) | Flarize | `/api/v1/**` | none | Studio replaces it |
| PA page, SI app, eSSL app | utilities | `/api/v1/agreements`, `/site-inspections`, `/hr`, `/attendance`, `/devices` | none | Studio replaces them |
| Write endpoints on reference tables (`POST/PUT/DELETE /api/solar-panels/` …) | main backend, public and unauthenticated | staff `/api/v1/catalog/*` and `/reference/*` | **not shimmed** (they were a defect) | |

### 6.3 Parity harness (built W14–15, run continuously until W26)
- Replays sampled production requests (from nginx access logs, PII-free GETs plus synthetic POSTs) against `legacy-*` and `api`, normalises volatile fields (`updatedAt`, ids) and diffs JSON.
- Approved differences are listed in `docs/cutover/approved-diffs.md` (e.g. calculator `'3P'` tariff fix, EMI rate tier D-7). Anything else blocks the cutover step.

### 6.4 Cutover steps

| Step | Week | Action | Verification | Rollback |
|---|---|---|---|---|
| C0 | 17 | Deploy `api` to prod with `LEGACY_API_SHIM=on`, no traffic; run `import_cms`/`import_backend` for real (sources stay live) | smoke tests; §7.6 checks | remove containers |
| C1 | 18 | nginx: `/studio-api/api/` (public content) → `api` | parity diff empty for 24 h on staging; blog pages render; sitemap unchanged | reload nginx to `legacy-cms` |
| C2 | 18 | nginx: public calculator/lead/reference/product prefixes under `/api/` → `api`; OTP provider switched (same Twilio service) | parity; lead rows appear in `leads_lead`; OTP works on a real phone | reload nginx |
| C3 | 19 | Studio flip: frontend build with `NEXT_PUBLIC_ADMIN_API_BASE_URL` → `/api/v1/`, BFF cookies; CMS users log in with reset passwords | every Studio screen smoke-tested by Content Manager | redeploy previous frontend image; old CMS still running |
| C4 | 19 | `legacy-cms` stopped (kept for 2 weeks, then removed); `blog_cms` DB dumped and archived | | start container |
| C5 | 20 | Pricing/quotations live (M3): Flarize users move to Studio; `bom` app frozen read-only; Flarize JSON archived | first real quotation issued from Studio | Flarize can still run locally |
| C6 | 21 | HR live (M4): agents reconfigured; eSSL Render service paused, then deleted after 2 weeks | attendance for the day matches HR expectation | re-point agents to the old URL |
| C7 | 22–24 | Frontend rebound to canonical `/api/public/v1/*` (FE2, code ready since W14); legacy shim paths still answer | | |
| C8 | 25 | Agreements + SI live (M5) | | |
| C9 | 26 | `LEGACY_API_SHIM=off` after 7 days of zero legacy hits in access logs; `legacy-backend` removed; `legacy/` package deleted; old DB archived | nginx logs | flag on again |

### 6.5 Frontend change list (small, deliberate)
1. `src/config.ts`: one `API_BASE_URL` (`https://flarize.com/api/public/v1/`) and one `STUDIO_API_BASE_URL` (`/api/v1/` through the BFF); delete `BLOG_API_BASE_URL` and the `bom/` URL derivation in `bomService.ts`, `quotationSettingsService.ts`, `quotationTestimonialsService.ts`.
2. `middleware.ts` + `studioService.ts`: cookie-based BFF (`/studio/api/auth/*` route handlers exchange JWTs and set HttpOnly cookies); refresh handled server-side.
3. `next.config.ts` `images.remotePatterns`: remove `localhost:8009`; keep Bunny hosts.
4. `/api/revalidate` route unchanged; secret moves to `company_profile`.
5. `next-sitemap.config.js`: source from `/api/public/v1/sitemap/entries`.
6. Studio screens: existing content screens rebound; new screens per §4.7.

---

## 7. Data migration plan

### 7.1 Principles
- **Importers are Django management commands** in `migrations_tools/`, read from a **read-only connection to the source** (`blog_cms`, `GoldenApp`, eSSL Postgres) or from files (Flarize JSON, PA JSON export, SI SQLite), write through the platform services where invariants matter (publish, prices) and through bulk inserts where they do not.
- **Idempotent** via `core_legacy_map`: re-running updates rather than duplicates; every target row is traceable to its source.
- `--dry-run` prints counts and violations without writing; `--verify` runs the checks in §7.6.
- Order respects FKs: users → media → masters → content → transactional rows → releases.
- **Freeze windows:** CMS authoring frozen 2 h before C3; Flarize frozen 24 h before C5; eSSL frozen at C6 (agents queue locally, nothing is lost).
- Rehearsed on staging at least twice per source; the rehearsal report is a release artefact.

### 7.2 CMS (`blog_cms` database) → platform — detailed

| # | Source table | Target | Transform / rules | Verify |
|---|---|---|---|---|
| 1 | `accounts_role` (`slug`, `name`, `permissions` JSON, `legacy_role`, `is_system`) | `accounts_role` | Keep slug/name. `permissions` re-keyed through a map: CMS modules → registry (`dashboard, pages, blogs, faqs, media, seo, leads→leads, emi, quotations, careers→job_positions/career_page, job_positions, applications, departments, career_page, users, roles, settings`); actions kept where allowed, unknown ones dropped and listed; `scopes` default all. System seeds are created first by `get_or_create`, then CMS roles with the same slug **update grants only**. | roles count; per-role diff report signed by Admin |
| 2 | `accounts_admin_user` (`username`, `email`, `password`, `role`, `access_role_id`, `is_active`, names) | `accounts_user` | Email is the login: if `email` empty → `<username>@migrated.invalid` and `must_reset_password=true`; **passwords are not migrated** (Django hasher may be PBKDF2 — importable, but we force reset for every account anyway and issue reset links at C3); `role_id` = mapped `access_role`, else by `legacy_role` (admin→Admin, editor→Content Manager, author→Content Manager with `blogs` only); `is_superuser` ignored. | user count; every user has a role; reset links sent |
| 3 | `media_asset` (`file`, `cdn_url`, `width`, `height`, `alternative_text`, `mime_type`, `size`, `collection`, `uploaded_by`) | `media_asset` (visibility PUBLIC) | Bunny URLs kept; assets that only exist under `/uploads` are re-uploaded to Bunny (task) and `cdn_url` set; checksum computed; `folder` = old collection. | all `cdn_url` return 200 (HEAD crawl) |
| 4 | `catalog_collection`, `catalog_template`, `catalog_template_image_group`, `catalog_template_attribute_slot`, `catalog_author`, `catalog_category`, `catalog_tag`, `catalog_badge` | `blog_*` masters | 1:1; `api_uid` preserved (it is the public route); slugs preserved. | counts equal |
| 5 | `content_entry` + M2M (`categories`, `tags`, `badges`) | `blog_entry` (+ M2M) | **`document_id` → `uid`** (frontend `documentId` unchanged); `status` map draft→DRAFT, published→PUBLISHED, archived→ARCHIVED (any review state → REVIEW); `published_at`, `published_on`, `read_time`, `is_featured`, `sort_order` copied; `created_by/updated_by` via user map; `collection`+`slug` uniqueness verified before insert. | counts; per-collection published counts equal; 50 random entries deep-compared |
| 6 | `content_entry_slug_history` | `blog_entry_slug_history` | copy; `active` preserved; partial unique verified. | old slugs still resolve (crawl) |
| 7 | `content_content_block`, `content_entry_image`, `content_entry_attribute_value`, `content_seo` | `blog_content_block`, `blog_entry_image`, `blog_entry_attribute_value`, `blog_entry_seo` | copy; `media_asset_id` via map; `group_key`/`slot_key` preserved; attribute values coerced to the slot type (report violations). | delivery JSON parity (§7.6) |
| 8 | `sitepages_page`, `sitepages_page_seo`, `sitepages_page_text_slot`, `sitepages_page_image_slot` | `sitepages_*` | copy; `career-page` becomes page slug `career`. | `pages/{slug}` payload parity for every page |
| 9 | `faqs_category`, `faqs_faq` | `faqs_*` | copy incl. SEO fields and `page_id` map. | `faqs` payload parity |
| 10 | `careers_department`, `careers_job_position` | `careers_*` | copy; status map (draft/published/closed/archived). | `job-positions` parity |
| 11 | `siteconfig_settings` (singleton) | `company_profile` + `seo_page_metadata` defaults + `company_integration` | phone/email/address/social/trust stats → profile; `default_og_image` → profile; revalidate URL/secret → profile (encrypted); Bunny keys → integration. | Studio company screen shows the same values |
| 12 | Django admin `LogEntry` (if kept) | `audit_log` | imported as `actor_kind=USER`, action `legacy.admin_change`. | optional |

Not migrated: `token_blacklist_*`, Django sessions, `authtoken`. Post-import: publish nothing new; the delivery API reads published entries directly.

### 7.3 Main backend (`GoldenApp` database) → platform

| Source | Target | Rules |
|---|---|---|
| `solar_panels`, `solar_inverters`, `batteries` | `catalog_component` + `catalog_panel_spec` / `inverter_spec` / `battery_spec` + `catalog_component_public_profile` | Brand → `catalog_brand` (created); component `status=ACTIVE`, `is_public=true`; marketing columns (description, ratings, kerala score, price_range) → public profile PUBLISHED; **matched to Flarize/BOM components by brand+model+wattage/kW** when possible (one record), else created as public-only components without prices (report). |
| `bom_category`, `bom_catalogitem`, `bom_itemtier` | `catalog_category`, `catalog_component`, `catalog_component_tier`, `pricing_price(LIST)` | `item_id` → `sku`; `price` → LIST price row `source=IMPORT`, effective from import date; `gst_override` → `gst_rate_override`; panels `watt/per_watt/dcr`, inverters `kw/phase/inverter_type/model` → specs. **Conflict rule (D-2): where the same `sku` exists in Flarize `catalog.json` with a different price or attributes, Flarize wins** (it is the maintained one); differences listed. |
| `bom_globalcosts` | `pricing_cost_config` | one row per key. |
| `bom_marketrate` (`size_rates` JSON) | `pricing_market_rate` in set "Imported <date>" (DRAFT) | exploded per size; battery config from `bat_config`. Flarize `marketRates` imported into the same set; conflicts → Flarize wins, listed. |
| `bom_bomtemplate`, `bom_bomslot`, `bom_bomfixeditem`, `bom_structuretemplate(+item)`, `bom_tubeweight` | `bom_*` | `BomSlot.get_qty` logic encoded as `qty_rule` JSON per slot (a table in the importer maps each slot to its rule; verified by `bom/build` parity on 20 configurations). |
| `bom_offer` | `pricing_offer` (ARCHIVED unless active) | |
| `bom_quotationsettings`, `bom_quotationtestimonial` | `company_profile` fields, `quotations_testimonial` (`show_on_website=true`) | |
| `emi_bank`, `emi_interest_rate_rule`, `emi_subsidy_rule`, `emi_calculator_settings` | `emi_*` | copy; `emi_system_size` → **not migrated** (report the price it held vs. the PackRelease price for the same size). |
| `pincodes`, `kseb_tariffs`, `device_types`, `wattages`, `room_size`, `ev_cars`, `ev_scooters` | `reference_*` | copy. |
| `goldenray_metadata` (model `Metadata`, default table name) | `seo_page_metadata` | copy. |
| `lead_collection_home` | `leads_lead` (`kind=HOME_ENQUIRY`) | phone normalised to E.164; `status=NEW` unless older than 90 days → `LOST(archived)`; OTP verified flag copied. |
| `affiliate_application`, `warranty_service_request`, `customer_installations`, `solar_installations`, `solar_installation_new` | `leads_affiliate_application`, `leads_warranty_request`, `leads_customer_installation` | `solar_installations` + `_new` merged into `customer_installation` (`is_showcase` from the new table). |
| `job_application` (+notes, events) | `careers_job_application` (+notes, events) | resumes/portfolios copied from `backend_media` volume into private media; `position` matched by slug from the CMS import; file checksum verified. |
| `sent_quotes` | `quotations_email_log` (legacy rows, no version FK) | `send_quote_junk` dropped. |
| `auth_user` (the main backend has no custom user model; these are the `/bom/` superusers) | `accounts_user` (Admin), forced reset | |

### 7.4 Flarize (JSON files) → platform

| File | Target | Rules |
|---|---|---|
| `users.json` (5) | `accounts_user` | roles mapped (ADMIN→Admin, PROJECT_HEAD→Project Head, ENGINEERING→Engineering, PROCUREMENT→Procurement, SALES/SALES_CRS/FIELD_SALES→Sales Executive with `title`); forced reset. |
| `catalog.json` `categories` (12) + components | `catalog_*`, `catalog_component_tier`, `pricing_price(LIST)` | authoritative over `bom_*` (D-2). |
| `catalog.json` `costs`, `installationMatrix`, `transportConfig`, `officeExpense`, `marketRates`, `structureTemplates`, `tubeWeights`, `bomTemplates`, `packageProfiles`, `offers` | `pricing_cost_config`, `pricing_installation_matrix`, `pricing_market_rate*`, `bom_*`, `pricing_offer` | legacy markers ignored. |
| `battery-master.json` | `catalog_battery_family`, `catalog_battery_spec` | |
| `procurement-price-master.json` (257) + `procurement-state.json` (batches) + `commercial-history.json` | `procurement_supplier`, `procurement_batch(+lines, charges)` COMMITTED, `pricing_price(PURCHASE, LANDED)` with `version_key` preserved | history order preserved; current prices equal the master's `landedUnitCost`. |
| `pack-config.json` (`approved`, `draft`, `history` 16) | `packs_config_version` (history as APPROVED/SUPERSEDED rows, `approved` → the one used for the first PackRelease, `draft` → DRAFT) | then **publish PriceRelease #1 and PackRelease #1 through the services**; publish report must be empty of BLOCK items or the business fills the data blockers first. |
| `project-rate-card.json`, `cost-config.json`, `quotation-policy.json` | `pricing_cost_config`, `pricing_validity_policy` | |
| `quotation-content.json`, `quotation-inclusions.json`, `tier-display-names.json`, `quotation-testimonials.json`, `quotation-branding-state.json`, `company-profile.json` | `quotations_content_version` (PUBLISHED #1), `quotations_inclusion`, `quotations_tier_display_name`, `quotations_testimonial`, `company_profile`, `company_bank_account` | |
| `customers.json` (34) | `customers_customer` | phone → E.164; `customerId` kept as `code`; `owner` from `createdBy`. |
| `quotation-state.json` (65 quotations, 139 BOM + 139 commercial snapshots, 65 documents, `versionStatus`) + `quotation-counter.json` | `quotations_quotation`, `quotations_version`, `quotations_bom_snapshot`, `quotations_commercial_snapshot`; `document_payload` = the frozen document **byte for byte**; `SequenceCounter QUO` set to the counter value | issued versions keep their pinned version numbers in `pins`; they reference `pack_release_id = NULL` with `legacy=true` (they predate releases). Re-render is allowed only from the frozen payload. |
| `bom-state.json`, `workspace-state.json` | `projects_project` (+ `bom_lock`) for locked BOMs; open workspaces not migrated (report) | |
| `energy-config.json`, `savings-config.json`, `subsidy-config.json`, `finance-config.json` | `engines` parameter rows (`pricing_cost_config` keys `energy.*`, `savings.*`, `subsidy.*`, `finance.*`) | |
| `cms-state.json`, `cms-assets/` | not migrated (Flarize page designer superseded by quotation content versions); assets copied to media for reference | |

### 7.5 Utilities → platform
- **Purchase Agreement:** JSON export of `flarize_agr` from the two browser profiles (`crs`, `admin`) → `agreements_agreement` (ISSUED, `legacy=true`, `payload` = raw record, typed columns parsed; `quotation_version_id` null); Upstash catalog compared with `catalog` and differences listed, nothing auto-created. If either profile was cleared, those records are gone; say so to the client before C8.
- **Site Inspection (SQLite):** customers by phone → `customers_customer` (unmatched created, `source=SI_IMPORT`); engineers → users (Field Engineer, reset); inspections column-by-column with type coercion (`"No"`→false, `"YES"`→true, `PARTIAL` kept, `system_type` recomputed from the snapshot); photos (data URLs → files → private media); annotations with `geometry_space=LEGACY_CONTAINER`; equipment assessments recomputed by the full-definition rule with a status diff report; approvals imported with `otp_verified_at=null`.
- **eSSL (Postgres):** users → `accounts_user` (bcrypt hashes import as `bcrypt$…`, verified by `BCryptPasswordHasher`, upgraded to Argon2 on next login); roles ADMIN→Admin, HR→HR, USER/VIEWER→Staff; offices/shifts/employees/holidays/leave/rules 1:1 (leave types created from distinct `leave_type` strings); devices/agents (new service credentials issued; agents reconfigured); `device_users` links kept **as per-device links** plus a report of PINs linked on more than one device; `attendance_raw` → `attendance_raw_punch` with new content dedup keys (duplicates across transports collapse; count reported); `attendance` history **recomputed** by `engines.attendance` v4 and a per-employee-month status diff produced for HR sign-off; `adms_requests` older than 30 days not migrated.

### 7.6 Verification checklist (run by `verify_migration`, all must pass before the corresponding cutover step)
1. Row counts per source table vs. `core_legacy_map` entries (100 % mapped or listed as intentionally skipped).
2. Referential integrity: no dangling `legacy_map` targets; every FK resolves.
3. **Delivery parity:** for every collection and every published slug, old `GET /api/<collection>?filters[slug][$eq]=` JSON == new JSON after normalising `updatedAt`; same for `page-content`, `faqs`, `job-positions`. Zero differences allowed.
4. Media: every `cdn_url` and every private file HEAD 200; checksums match for copied files.
5. Slugs: every historical slug resolves (301 or 200).
6. Users: every migrated user has a role and a reset link delivered; login with old passwords fails (by design).
7. Pricing: for every component, new current LIST price == Flarize `catalog.json` price (or BOM price where Flarize has none); every `landedUnitCost` in the price master == new current LANDED price.
8. Packs: PackRelease #1 per-pack `customer_price_incl_gst` == Flarize reference outputs for all packs with market rates; the publish report's BLOCK list is reviewed and either empty or explicitly accepted.
9. Quotations: 65 frozen payloads re-rendered → PDF page count equal and text diff empty against the archived PDFs where available.
10. Calculators: 200 recorded inputs produce identical outputs except approved diffs.
11. Attendance: diff report reviewed and signed by HR; row counts of raw punches equal after dedup accounting.
12. Audit: every import writes one `audit_log` row per batch with counts and the source snapshot checksum.

### 7.7 Rollback per source
CMS/backend: nginx back to legacy containers (both keep running read-write until C4/C9; writes made in the new system between cutover and rollback are exported by `export_delta` and replayed manually — the window is hours, not days). Flarize: the JSON files are untouched by the import; the utility still runs locally. eSSL: agents re-pointed; the Render service is paused, not deleted, for two weeks.

---
## 8. Testing and acceptance gates

| Layer | What | Gate |
|---|---|---|
| Unit (engines) | golden-file parity with Flarize reference outputs (pricing, cost, energy, subsidy, finance, BOM, checker); attendance v4 cases; readiness 19 blockers; inspection checks | 100 % of golden cases; no Django import |
| Unit (services) | every service function: happy path, each `DomainError`, permission denial, scope filter, audit row written, outbox event written | coverage ≥ 85 % on `*/services/` |
| RBAC | registry closed (unknown module/action rejected), normalisation, default deny per method, every scope, `deny_self_action`, no superuser bypass | 100 % |
| API contract | schemathesis against the OpenAPI schema for every endpoint (auth'd and anonymous) | no 5xx, no schema violation |
| Public contract | recorded fixtures of the old delivery/calculator responses replayed against the shim and the canonical endpoints | zero unapproved diffs |
| Engineer boundary | walks every serializer reachable by the Field Engineer role; fails on any commercial field name | pass |
| Concurrency | parallel issue/publish/commit on the same row → exactly one succeeds (`stale_version`, partial uniques) | pass |
| Migration | `verify_migration` checklist §7.6 on staging rehearsal | pass twice |
| Performance | k6: public GETs 200 rps p95 < 150 ms (cached), staff lists p95 < 400 ms, PDF render < 6 s p95, ingestion 10 devices × 2k punches < 60 s to recompute | pass on staging sizing |
| Security | OWASP ASVS L2 checklist: IDOR via uid enumeration, JWT alg confusion, upload types, signed-URL replay, throttle bypass via XFF, CSRF on BFF, SQLi on filters, path traversal on media | no high findings |
| Acceptance (business) | Content Manager: publish an article and see it on the site; Sales: issue a quotation and a PA; Field Engineer: complete an inspection to release on a phone; HR: month-end report equals expectation; Admin: create a role and see the navigation change | signed per milestone |

---

## 9. Decisions still open (defaults apply if unanswered)

| # | Decision | Default | Blocks |
|---|---|---|---|
| D-1 | Standalone repo for the platform (recommended) vs. inside `goldenray` | standalone `flarize-platform` | Phase 0 |
| D-2 | When Flarize `catalog.json` and `bom_*` disagree, which is authoritative | Flarize | import_flarize, W5 |
| D-3 | Quotation artwork: goldenray React `QuotationV2` vs Flarize `quotationDocument.js` | goldenray artwork on the Flarize payload | W12 |
| D-4 | Offers change the printed customer price | yes, printed | W8 |
| D-5 | Project Head workspace scope | minimal `projects` app; workspace UI later | W15 |
| D-6 | Stock quantities (real inventory ledger) in scope now | behind `INVENTORY_STOCK`, schema shipped, UI later | W8 |
| D-7 | Interest tiers 5.75 %/7.9 % (Flarize) vs 5.75 %/8 % (EMI rules) | business confirms; stored as rows | W11 |
| D-8 | Payback basis; `'3P'` tariff fix; PBC-M-003 controller | Flarize payback; fix `'3P'`; controller flagged in publish report | W6 |
| D-9 | Multiple bank accounts / legal entity on documents (Golden Ray vs Flarize) | `company_bank_account` with one primary; content editor sets entity text | W12 |
| D-10 | ADMS push path: prove on one terminal or agent-only | agent-only; receiver flag off | W9 |
| D-11 | Agreement numbering `AGR-<FY>-nnnn` vs continue `QUO-GR-AS-26-` | new sequence | W16 |
| D-12 | Additional work gates release until customer approves cost | yes | W20 |
| D-13 | Paper-signed customer approval fallback | allowed, audited | W20 |
| D-14 | Office Manager role now | seeded, unassigned | W4 |
| D-15 | Who repoints the three terminals away from `117.247.191.140:8080` | client IT, before any ADMS test | W9 |
| D-16 | Dedicated prod VM before M3 | yes | W18 |

---

## 10. Risks

| Risk | Impact | Mitigation in this plan |
|---|---|---|
| Data blockers, not code, stop the first PackRelease (missing market rates for most packs, `bt1` rating, on-grid Premium controller, bank details, real testimonials) | M3 slips | publish report at W8 makes the list visible; owners assigned per row; publishing is refused, never fudged |
| Two pricing authorities today (`bom_*` vs Flarize) already disagree | wrong prices imported | D-2 default + a signed diff report before W8 |
| CMS delivery parity | blog breaks silently | recorded fixtures + parity harness gate C1 |
| Studio flip is one release (auth model) | content team blocked for hours if it fails | old CMS kept running; rollback = redeploy previous frontend; rehearsed on staging with the content team |
| Attendance history changes after recompute (A1–A9) | HR disputes | diff report and sign-off before C6; corrections API for exceptions |
| Terminal reality unverified (offices 2, 3) | HR live only for office 1 | agent-only path; discovery command; ADMS behind flag |
| PDF rendering in Malayalam/Hindi | broken glyphs | Noto fonts baked into the documents image; golden PDFs in CI |
| Scope: this is ~26 weeks with 3+2 engineers | expectations | milestones ship value every 4–5 weeks; M1/M2 (website + content) at W18–19 do not depend on the sales work |
| Frontend is the long pole after W18 (SI wizard port, HR screens) | M5 slips | FE2 starts HR screens at W15; SI wizard is a port of existing React |

---

## 11. Verification record of this document

Checks run on the final draft before delivery (script `verify_plan.py`; all passed after two rounds of fixes — the first round found that HR endpoints and role grants were referenced from the superseded documents instead of being written here, that three source table names were wrong (`room_size`, `goldenray_metadata`, `auth_user`), that the milestone weeks in §4 were earlier than the migration commands they depend on, and that A1–A12 and the readiness blockers were referenced but not defined; all corrected above):
- every `§n` reference resolves to an existing heading;
- every module named in the API sections exists in the registry table (§3.2), and every registry module has at least one endpoint;
- every app named in §3–§7 exists in the project layout (§1.5);
- every table referenced in §7 exists in §2 (target) or in the source inventories (CMS `db_table`s, `goldenray`/`bom` `db_table`s, Flarize file list) — source lists were taken from the repositories, not from memory;
- milestone weeks (M1=W18, M2=W19, M3=W20, M4=W21, M5=W25) agree between §4.1, §4.8, §6.4 and §10;
- markdown tables have a consistent column count per row;
- the week ranges in §4.4–§4.6 do not overlap within an engineer's track.
