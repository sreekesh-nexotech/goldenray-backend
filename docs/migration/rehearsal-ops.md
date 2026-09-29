# Rehearsal — operational sources (Flarize, Purchase Agreement, Site Inspection, eSSL)

Release artefact of PLAN §7.1 ("rehearsed at least twice per source; the rehearsal report is a release artefact") for
`import_flarize`, `import_pa`, `import_si`, `import_essl` and `verify_migration` #8, #9, #11. Two full rehearsals
(`rh1`, `rh2`) on 2026-09-29, each on a **fresh** private target database, each running every import **twice**; plus
one rehearsal of the seeded eSSL database. The staging rehearsal repeats the same commands against production copies
at the freeze windows (C5 Flarize, C6 eSSL, C8 PA/SI).

## Environment

| What | Value |
|---|---|
| Target | fresh `flarize_wp_migration_ops_rh1` / `_rh2`: `migrate`, `ensure_audit_partitions --months 3`, `seed_roles`; settings `flarize.settings.dev`, media under a scratch folder, Redis db 13; dropped afterwards |
| Website sources (first, so the Flarize import produces the B-2 list) | private restores of `/home/user/platform-reference/uat/legacy_{blog_cms,goldenapp}.dump` (`migration_ops_blog_cms`, `migration_ops_goldenapp`), read-only sessions; the shared `legacy_*` databases were never touched |
| Flarize | the real folder `/home/user/flarize-main/flarize/data` (read-only; 31 files, 675 page-designer asset files) |
| Purchase Agreement | `crs` = `agreements/tests/fixtures/pa/flarize_agr.json` (the page's own records, recorded by the agreements package), `admin` = `migrations_tools/tests/fixtures/ops/pa/admin-localstorage.json`, Upstash catalog = `agreements/tests/fixtures/pa/catalog.json` |
| Site Inspection | `migrations_tools/tests/fixtures/ops/si/flarize-site-inspection.db` — built by the SI app's own `lib/db.ts` + `scripts/seed.ts` (`build_si_sqlite.mjs`), opened `mode=ro` |
| eSSL | `essl_wp_migration_ops` (eSSL alembic head + `scripts/seed.py`) and `essl_wp_migration_ops_full` (alembic head + the attendance package's capture of a running eSSL: a month of punches, v3 days, leave, holidays, an ADMS/agent duplicate), both private, read-only sessions; dropped afterwards |
| Branch | `wp/migration-ops` |

## Commands, in order (per rehearsal)

```
import_cms --source-url $CMS ; import_backend --source-url $BACKEND
import_flarize --source-dir $FLARIZE --dry-run
import_flarize --source-dir $FLARIZE --json flarize-1.json
import_pa --export crs=… --export admin=… --catalog … --json pa-1.json
import_si --source-file flarize-site-inspection.db --json si-1.json
import_essl --source-url $ESSL_FULL --diff-report essl-diff-1.json --credentials-file essl-credentials.json
import_flarize … --send-reset-links ; import_pa … ; import_si … ; import_essl …       # second runs
verify_migration --source flarize --flarize-dir $FLARIZE --offline
verify_migration --source pa --pa-export … --source si --si-file … --offline
verify_migration --source essl --essl-url $ESSL_FULL --offline --attendance-report hr-report.json
verify_migration --source essl --essl-url $ESSL_FULL --offline --attendance-signoff <sha256 of hr-report.json>
```

Timings (rh1 / rh2): Flarize dry run 60 / 62 s, first run 64 / 66 s, second run 40 / 39 s; PA 3 s, SI 3–4 s, eSSL
3–4 s; each verification ≤ 6 s.

## 1. Dry run

`import_flarize --dry-run` reported the counts of the real run (1,278 created, 300 updated, 1,235 unchanged, 1,083
violations) and left nothing behind: no legacy map row, no audit row, no release, no file.

## 2. First runs (rh1 and rh2 identical)

### Flarize

| Step | Created | Updated | Unchanged | Violations (codes) |
|---|---|---|---|---|
| `flarize.users` | 5 | 0 | 0 | — |
| `flarize.cms_assets` | 341 | 0 | 330 | 330 `not_active`, 1 `page_designer_not_migrated` (1,321 pages, 331 campaigns) |
| `flarize.catalog_pricing` — catalog / LIST prices / pricing sections | 3 / 3 / 23 | 197 / 17 / 5 | 89 / 238 / 67 | 32 `d2_flarize_wins`, 9 `d2_tier_not_kept`, 1 `possible_duplicate_offer` |
| `flarize.documents` | 39 | 0 | 0 | — |
| `flarize.bom` (+ B-5 list) | 9 | 77 | 124 | 77 `d2_flarize_wins`; 14 `hybrid_slot_phase_unmatched` |
| `flarize.procurement` | 520 | 0 | 257 | — |
| `flarize.packs` | 17 | 2 | 0 | — |
| `flarize.company` | 0 | 1 | 0 | 1 `value_differs` (trade name from the CMS kept), 1 `enter_as_bank_account`, 3 `asset_not_linked`, 6 `listed_only` |
| `flarize.quotation_content` | 38 | 0 | 4 | 1 `photo_not_migrated`, 4 `demo_branding_not_migrated` |
| `flarize.customers` | 34 | 0 | 0 | 34 `unmapped_owner`, 1 `unparsable_phone` |
| `flarize.quotations` (counter + state) | 65 | 1 | 0 | 54 `unmapped_owner`, 2 `customer_created`, 20 `orphan_snapshot` |
| `flarize.projects` | 179 | 0 | 112 | 292 `unmapped_user`, 7 `customer_not_found`, 104 `open_workspace_not_migrated`, 1 `ui_state_not_migrated` |
| `flarize.releases` | 2 (PriceRelease #1, PackRelease #1) | 0 | 0 | 9 `pack_config_market_rate_wins`, 79 `pack_excluded` |
| **Total** | **1,278** | **300** | **1,235** | **1,083** |

Business-default lists printed in full: **B-2** 109 values the Flarize import changed over the main-backend import
(76 fixed BOM items, 17 LIST prices, 10 catalog items, 3 market-rate cells, 2 costs, 1 slot — e.g. `p2 LIST: FLARIZE
13585.00 replaces 14300.00`, `gi_rate_per_kg: Flarize 98.3 replaces the main backend's 130`); **B-3** 9 cells where the
approved pack configuration's market rate replaces the imported one (e.g. ongrid value 3 kW 228,000 → 229,000);
**B-5** 14 components stored as `1P`/`3P` in the ACDB/DCDB categories that the HYBRID template's HYB-filtered slots
draw from (listed by `import_backend` too). No B-12 row: the UAT EMI banks are well formed.

### Purchase Agreement → Site Inspection

| Import | Created | Unchanged | Violations |
|---|---|---|---|
| `import_pa` | 14 (6 KSEB fee bands, 7 `crs` agreements, 1 `admin` copy) | 55 | 10 `demo_record_not_migrated`, 6 `customer_without_phone`, 2 `customer_created`, 1 `duplicate_record_id`, 1 `trashed_record_not_migrated`, 36 catalog lines (1 `not_in_catalog`, 35 `listed_only`) |
| `import_si` | 19 (2 engineers, 2 customers, 3 inspections, 3 photos, 2 annotations, 3 equipment, 2 approvals, 2 observations) | 4 | 2 `email_placeholder`, 1 `matched_by_phone` (the PA customer), 1 `unparsable_phone`, 1 `commercial_not_imported`, 1 `invalid_enum`, 1 `out_of_range`, 1 `invalid_data_url`, 1 `status_recomputed`, 1 `approval_expired` |

The PA inspection's `agreement_uid` = `uuid5(SI_AGREEMENT_NAMESPACE, "PA:agr_1789902000000")` = the imported
agreement's uid (`agreement links`: 1 linked, 0 missing), and its customer is the one the PA import created (phone
9000000101).

### eSSL (captured database)

214 created: 1 user, 3 shifts, 3 offices, 5 employees, 1 holiday, 1 leave type, 2 leave records, 1 agent (new
credential written 0600, shown once), 2 devices, 6 PIN links, 34 raw punches (35 eSSL rows: 1 ADMS/agent duplicate
collapsed, listed `collapsed_duplicate`), 155 v4 days. Listed: the admin's missing e-mail, 2 shift half-day rules
(A5), PIN 4 linked on two terminals. **B-7**: 5 of 155 employee-days differ — E001 15 Aug LATE → HOLIDAY and 16 Aug
PRESENT → WEEKLY_OFF (A4), E002 3 Aug LATE → HALF_DAY (A5), E003 5 Aug PRESENT → ABSENT (A6), employee `5` 3 Aug
PRESENT → ABSENT (A1: the unlinked-PIN fallback is gone); totals ABSENT 109 → 111, HALF_DAY 2 → 3, HOLIDAY 4 → 5,
LATE 3 → 1, PRESENT 13 → 10, WEEKLY_OFF 24 → 25.

## 3. Second runs (idempotency)

| Import | Created | Updated | Note |
|---|---|---|---|
| `import_flarize` | 2 | 0 | PriceRelease #2 / PackRelease #2: `import_pa` had added the KSEB statutory fees to the pricing masters between the two runs, so the release content changed (the only payload difference is `statutory_fees`); a **third** run: 0 created, 0 updated, releases reused. The pricing importer lists `market_rate_set_locked` (the imported set is the ACTIVE one since PriceRelease #1 — never rewritten). `--send-reset-links`: 5 links issued. |
| `import_pa` | 0 | 0 | |
| `import_si` | 0 | 0 | |
| `import_essl` | 0 | 0 | no new credential |

## 4. Verification (rh1 and rh2)

| Source | #1 | #2 | #4 | #6 | #8 | #9 | #11 | #12 |
|---|---|---|---|---|---|---|---|---|
| Flarize | pass (27 files) | pass (33 tables) | pass (341 private copies checksummed) | pass (5 accounts, links issued) | **pass** | **pass** | n/a | pass (13 steps) |
| PA + SI | pass (50 rows) | pass | pass | pass | n/a | n/a | n/a | pass (7 steps) |
| eSSL | pass (194 rows) | pass | pass | pass | n/a | n/a | **skipped → pass** | pass (4 steps) |

* **#8** — PackRelease #1 (configuration v16, PriceRelease #1): the 11 packs Flarize can sell are released at exactly
  the JavaScript reference's `customer_price_incl_gst` (`packs/tests/golden/flarize_packs.json`, whose recorded data
  SHA-256s equal the rehearsed folder's files); 12 packs have a market rate, 1 of them is blocked by the checker and
  excluded with `PACK_ENGINEERING_BLOCKED` (B-4); 78 have no market rate; publish report BLOCK 0, WARN 108.
* **#9** — 64 frozen documents (the 65th quotation is a draft): stored, source and re-render payload hashes equal;
  128 renders (en + ml) deterministic. No archived PDFs exist here for the page-count/text comparison (pending DV).
* **#11** — 35 raw rows → 34 punches, 1 collapsed; the report's SHA-256 changes per target database (it carries the
  employees' platform uids), so HR signs the report of the cutover run: without `--attendance-signoff` the check is
  `skipped` and the command fails; with the report's SHA-256 it passes.

## 5. Seeded eSSL database

`essl_wp_migration_ops` (alembic head + `scripts/seed.py`) into a fresh target: 7 created (admin login, 2 shifts,
3 offices, device MARS-01); listed: the admin's missing e-mail, the seeded default password (not carried over: the
account sets a new one through a reset link), the NIGHT shift half-day rule. Second run 0 / 0. #1, #2, #4, #6, #12
pass; #11 skipped until signed off (0 punches, 0 days).

## Findings

1. **Pack pins re-counted on every run** — `import_flarize_pack_config` reported 2 "updated" pin sets on each re-run
   with nothing changed. Fixed (the count is the pins written); third runs are 0/0.
2. **Flarize owners never resolved in three importers** — customers, projects and quotations looked up the map table
   `users`, which nobody writes; the users import and catalog/pricing use `users.json`. Unified on `users.json`.
   The remaining `unmapped_owner`/`unmapped_user` rows name user ids (`user-…`) that `users.json` (5 accounts) does not
   hold — a business question before C5.
3. **Release order** — importing the PA KSEB fees after the Flarize import changes the pricing masters, so the next
   `import_flarize` publishes PriceRelease #2 / PackRelease #2. At the cutover run `import_pa` (fees only:
   `--only pa.kseb_fees`) before `import_flarize` if PriceRelease #1 must carry the statutory fees.
4. **#8 order** — the stored configuration (JSONB) does not keep Flarize's key order, so enumerating it gives another
   pack order than the reference; the check matches packs by their inputs.
5. Every scratch database (targets, website restores, eSSL fixtures) and scratch media folder was dropped after the
   rehearsals.
