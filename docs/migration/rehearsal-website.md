# Rehearsal — website sources (CMS `blog_cms`, main backend `GoldenApp`)

Release artefact of PLAN §7.1 ("rehearsed at least twice per source; the rehearsal report is a release artefact").
Rehearsal 1 of 2 for these sources, run on 2026-09-29 against the UAT snapshots. The staging rehearsal
(rehearsal 2) repeats the same commands against the production snapshots at the freeze window.

## Environment

| What | Value |
|---|---|
| Sources | `/home/user/platform-reference/uat/legacy_blog_cms.dump` and `legacy_goldenapp.dump`, restored into the private databases `migration_website_blog_cms` and `migration_website_goldenapp` (`createdb … && pg_restore --no-owner -d …`). The shared `legacy_*` databases were never written to. |
| Source access | `--source-url postgresql://…` — a read-only session (`default_transaction_read_only=on`, `read_only=True`; a write raises `ReadOnlySqlTransaction`, proven in `migrations_tools/tests/test_source.py`) |
| Target | fresh database `flarize_wp_migration_website_target`: `migrate`, `ensure_audit_partitions --months 3`, `seed_roles` (12 system roles) |
| Settings | `flarize.settings.dev` (local public media, Redis db 11), `FRONTEND_BASE_URL=http://localhost:3000` (the legacy CMS default the site URL comes from) |
| Legacy HTTP API for parity | the shared UAT CMS `http://127.0.0.1:18009` (read-only GETs: 71 requests per verification) |
| Branch | `wp/migration-website` |

## Commands, in order

```
python manage.py import_cms     --source-url $CMS_URL --dry-run
python manage.py import_backend --source-url $BACKEND_URL --dry-run
python manage.py import_cms     --source-url $CMS_URL     --json cms.json
python manage.py import_backend --source-url $BACKEND_URL --json backend.json
python manage.py verify_migration --cms-url $CMS_URL --backend-url $BACKEND_URL --legacy-cms-api http://127.0.0.1:18009
python manage.py verify_migration … --list-prices-as-release          # rehearsal mode for #10, see "Findings"
python manage.py import_cms     --source-url $CMS_URL                 # second run: idempotency
python manage.py import_backend --source-url $BACKEND_URL
python manage.py verify_migration … --list-prices-as-release
python manage.py export_delta --source backend --since <start of the run> --output delta.json
```

## 1. Dry runs

Both dry runs reported exactly the counts of the real run below (CMS: 231 created, 1 updated, 2 unchanged, 1
violation; main backend: 6,062 created, 3 unchanged, 3 violations — the main backend's dry run created the company
profile that the real run found already created by the CMS import). Afterwards the target still held only the seeded
roles: no legacy map row, no audit row, no file.

## 2. First import

### CMS (run `1e6a542f-69f6-4837-b989-3e9e23589ee3`)

| Step (phase) | Source rows | Created | Updated | Unchanged | Violations |
|---|---|---|---|---|---|
| `cms.users` (users) | `accounts_role` 4, `accounts_admin_user` 0 | 2 | 1 | 1 | 1 |
| `cms.media` (media) | `media_asset` 0 | 0 | 0 | 0 | 0 |
| `cms.blog_schema` (masters) | collection 1, template 1, image groups 4, attribute slot 1, authors 2, categories 3, tags 6, badge 1 | 19 | 0 | 0 | 0 |
| `cms.careers_departments` (masters) | 3 | 3 | 0 | 0 | 0 |
| `cms.faq_categories` (masters) | 0 | 0 | 0 | 0 | 0 |
| `cms.pages` (content) | pages 27, SEO 0, text slots 2, image slot 1 | 30 | 0 | 0 | 0 |
| `cms.faqs` (content) | 100 | 100 | 0 | 0 | 0 |
| `cms.blog_content` (content) | entries 7, categories 7, tags 10, badges 4, slug history 1, blocks 14, images 20, attribute values 0, SEO 7 | 70 | 0 | 0 | 0 |
| `cms.careers_positions` (content) | 6 | 6 | 0 | 0 | 0 |
| `cms.site_settings` (content) | 1 | 1 | 0 | 1 | 0 |
| **Total** | | **231** | **1** | **2** | **1** |

Roles: `super-admin` kept every grant (unchanged); `content-manager` (a seeded slug) had its CMS-vocabulary grants
replaced (pages view/edit only, no career page) — the 1 update; `careers-hr` and `sales-lead` were created as custom
roles. The one violation: `accounts_role` 1 `career_page.verify` is not a registry action (dropped, listed).

### Main backend (run `48a6ea67-c3ca-4e24-a65a-88a768cd5974`)

| Step (phase) | Source rows | Created | Updated | Unchanged | Violations |
|---|---|---|---|---|---|
| `backend.users` (users) | `auth_user` 0 | 0 | 0 | 0 | 0 |
| `backend.reference` (masters) | tariffs 5, device types 18, wattages 25, room sizes 7, EV cars 14, EV scooters 15, pincodes 5,057 | 5,141 | 0 | 0 | 0 |
| `backend.catalog_pricing` (masters) | panels 11, inverters 7, batteries 4, BOM categories 31, items 257, tiers 406, global costs 1, market rates 16, offers 3 | 642 | 0 | 3 | 3 |
| `backend.bom` (masters) | templates 3, slots 30, fixed items 109, structures 3 (+51 items), tube weights 5 | 201 | 0 | 0 | 0 |
| `backend.calculators` (masters) | `solar_installations` 10, `solar_installation_new` 18 | 28 | 0 | 0 | 0 |
| `backend.emi` (masters) | settings 1, banks 4, rate rules 6, subsidy rules 3, system sizes 4 | 18 | 0 | 0 | 0 |
| `backend.company` (masters) | `bom_quotationsettings` 1 | 0 | 1 | 0 | 0 |
| `backend.seo` (content) | `goldenray_metadata` 11 | 11 | 0 | 0 | 0 |
| `backend.leads` (transactional) | affiliate 0, warranty 0, installations 20, leads 0 | 20 | 0 | 0 | 0 |
| `backend.careers` (transactional) | applications 0, notes 0, events 0 | 0 | 0 | 0 | 0 |
| **Total** | | **6,061** | **1** | **3** | **3** |

Catalog/pricing: 22 website products (with public profiles), 285 BOM categories/components/tiers, 261 LIST prices
(257 BOM items + 4 website batteries), 13 cost-config keys, 58 market-rate cells in the DRAFT set "Imported <date>",
3 offers. The 3 violations are warnings on imported rows: `brand_near_match` for "Adani" / "Adani Solar", "Saatvik
Solar" / "Saatvik", "Goldi" / "Goldi Solar" (merge in Studio if the business confirms they are the same maker).

## 3. Verification (PLAN §7.6)

| # | Check | Result |
|---|---|---|
| 1 | Row counts vs `core_legacy_map` | PASS — 6,389 source rows; every row mapped or listed; every non-empty source table is imported or listed as not migrated |
| 2 | Referential integrity | PASS — 54 mapped target tables, no dangling map row, no `NOT VALID` foreign key, every unconstrained foreign key resolves |
| 3 | Delivery parity | PASS — 71 requests (collection list + 5 published articles, 27 `page-content` routes, the FAQ routes and sections, `job-positions` list, 3 departments, 6 postings): 70× 200 and 1× 404 on both sides, **0 differences** |
| 4 | Media | PASS — the UAT CMS has no media assets (nothing to HEAD or checksum); the check is exercised by the tests |
| 5 | Slugs | PASS — 37 requests: the active alias `net-metering-explained`, the 5 published articles, 27 published pages, 4 published postings |
| 6 | Users | PASS — no admin users in either UAT source; the check is exercised by the tests (role, forced reset, no usable password, reset link issued) |
| 7 | Pricing | PASS — 261 current LIST prices equal the BOM item / website battery prices (and per watt); no Flarize-owned component yet |
| 10 | Calculators | FAIL without a PriceRelease: 1,228 recorded requests replayed, 69 differ — every one an advanced-calculator case with a hybrid battery (`overall_setup_cost` 425,300 legacy vs 230,000). With `--list-prices-as-release` (the imported LIST prices standing in for PriceRelease #1): **PASS, 0 of 1,228 differ** |
| 12 | Audit | PASS — 20 batch rows (10 per source) with per-table row counts, SHA-256 checksums and the importers' counts; every table checksum equals the source's current checksum |

## 4. Second import (idempotency)

| Source | Run | Created | Updated | Unchanged | Violations |
|---|---|---|---|---|---|
| CMS | `6448d017-e031-41d1-bc52-5e6f7478529b` | 0 | 0 | 234 | 1 (the same dropped action, reported on every run) |
| Main backend | `300bbb3a-f260-42e6-8f1f-24e508ac7332` | 0 | 0 | 6,065 | 0 |

`core_legacy_map` held 6,705 rows after both runs (unchanged by the second run). `verify_migration` after the second
run: every check passed (with `--list-prices-as-release`).

## 5. Rollback artefact

`export_delta --source backend --since <start>` exported 977 rows in 14 tables (every row of the run, since the whole
import happened in the window) to a `0600` file — the same command lists the writes to replay after a rollback.

## Findings

1. **Website product prices need PriceRelease #1 before the calculators cutover.** The advanced calculator prices the
   hybrid battery from the current PriceRelease (DV-85). From the website sources alone a release cannot be
   published: the publish report blocks on `MARKET_RATE_SET_MISSING` (the imported market-rate set is DRAFT) and
   `GST_CONFIG_MISSING` (`gst_goods_share`, `gst_services_share`, `gst_goods_rate`, `gst_services_rate` come from the
   Flarize import). Cutover order: publish PriceRelease #1 (after the Flarize import, or after the business activates
   a market-rate set and enters the GST keys) before switching the calculators; `verify_migration` #10 then passes
   without the rehearsal flag. The public product pages (`products/*`) fall back to the profiles' price ranges until
   then.
2. **`company_profile.website` had no legacy column.** The CMS printed its `FRONTEND_BASE_URL` setting as the site
   URL of the job-posting schema (`hiringOrganization.sameAs`); the first verification showed 5 job-position
   differences. `import_cms` now fills an empty `website` from `--site-url` (default `FRONTEND_BASE_URL`) — DV-108.
3. **Fixture date-times must keep microseconds.** Exported with Django's JSON encoder they were cut to milliseconds
   and the articles' `publishedOn` / the EMI config `updated_at` no longer matched; the export script and the source
   checksum keep full precision.
4. The UAT sources hold no admin users, media assets, leads or job applications. Those importers are covered by the
   tests with synthetic, masked rows; rehearsal 2 (production snapshot) is the first run with real volumes and must
   pass `--media-root` (CMS uploads, backend resumes) and, at the cutover, `--send-reset-links`.
5. `bom_quotationtestimonial` (3 rows) and `sent_quotes` (0 rows) are listed as not migrated until the quotations
   package is integrated (DV-107).
