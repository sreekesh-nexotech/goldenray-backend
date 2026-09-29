# migration-ops — import and verification tooling for the operational sources

Work package migration-ops extends `migrations_tools` (built by migration-website for the CMS and the main backend)
with the four operational sources of PLAN §7: Flarize JSON files (§7.4), the Purchase Agreement browser exports, the
Site Inspection V2 SQLite file and the eSSL PostgreSQL database (§7.5), plus `verify_migration` checks #8, #9 and #11
(§7.6). It follows §7.1 (principles), §7.7 (rollback: every source is read, never written) and the business defaults
B-2, B-3, B-5, B-7, B-12. Rehearsal report: `docs/migration/rehearsal-ops.md`. Deviations: see *Pending DV numbers*.

## What exists

| Area | Where | Notes |
|---|---|---|
| Commands | `migrations_tools/management/commands/{import_flarize,import_pa,import_si,import_essl}.py` | thin; each names its plan and how its source is given |
| Shared command | `services/cli.py` (`ImportCommand`) | source hooks (`add_source_arguments`, `open_source`, `verify_arguments`, `after_run`); dry run, resume, `--only`, `--json`, reset links, `--verify` and the business-default lists are common to all six import commands |
| Sources | `services/source.py` | + `JsonFilesSource` (a folder of JSON documents, one table per file, `read_file` for files beside them), `SqliteSource` (read-only `mode=ro`), `document()` |
| Plans | `services/flarize.py`, `pa.py`, `si.py`, `essl.py` | which files/tables each step reads and which importers it calls, in FK order; `not_migrated` reasons; `row_keys` for check #1 |
| Runner | `services/runner.py` | + phase `releases` (PLAN §7.1 "… transactional rows → releases"); `Plan.row_keys` / `keys_of` |
| Business defaults | `services/business_defaults.py` | groups the importers' violations under B-2/B-3/B-5/B-12; `hybrid_phase_mismatches()` computes the B-5 list after every BOM step (main backend and Flarize) |
| Verification | `services/verify_ops.py`, `services/verify.py`, `commands/verify_migration.py` | #8, #9, #11; #1, #2, #4, #12 now cover every source (`--source flarize|pa|si|essl` and their source options) |
| Gap importers (shared) | `accounts…import_flarize_users`, `company…import_flarize_company_profile`, `media…import_flarize_cms_assets` | nothing imported `users.json`, `company-profile.json` or the page-designer assets before |
| Fixtures | `migrations_tools/tests/fixtures/ops/` | see *Fixtures* |

## Commands

```
seed_roles                                                    # once, before any import
import_flarize --source-dir /srv/flarize/data [--media-root DIR] …   # or --source-fixture FILE
import_pa      --export crs=crs.json --export admin=admin.json [--catalog products.json]
import_si      --source-file flarize-site-inspection.db       # after import_pa
import_essl    --source-url postgresql://… [--diff-report FILE] [--credentials-file FILE]
       common: [--dry-run] [--verify] [--resume [RUN_UID|latest]] [--only STEP]… [--list-steps] [--actor EMAIL]
               [--json FILE] [--send-reset-links]
verify_migration --source flarize --flarize-dir DIR [--flarize-reference FILE]
                 --source pa --pa-export PROFILE=FILE… [--pa-catalog FILE]
                 --source si --si-file FILE
                 --source essl --essl-url URL [--attendance-report FILE] [--attendance-signoff SHA256]
                 [--check N]… [--allow-skipped] [--offline] [--json FILE]
```

Order at the cutover: `import_cms` → `import_backend` (website, C3/C4) → `import_flarize` (C5) → `import_pa` →
`import_si` (C8) → `import_essl` (C6). `import_flarize` may run before the website imports too (D-2 and the catalog
de-duplication hold in either order), but running it after them is what produces the B-2 list for sales.

## Decisions

1. **One plan per source, the same runner.** Every command is the migration-website runner: one committed batch per
   step with its audit row (counts, per-table SHA-256, violation codes, listed ids — PLAN §7.6 #12), dry run as one
   rolled-back transaction with the media storages swapped, resume by run uid + source checksum. The Flarize folder is
   a source whose tables are its files (`catalog.json` → `[document]`); a JSON file is checksummed as parsed, so
   `--resume` and check #12 see a changed file.
2. **Flarize plan (every PLAN §7.4 row).** `flarize.users` (users) → `flarize.cms_assets` (media) →
   `catalog_pricing`, `documents`, `bom`, `procurement`, `packs`, `company` (masters) → `quotation_content` (content)
   → `customers`, `quotations`, `projects` (transactional) → `flarize.releases` (releases): PriceRelease #1 and
   PackRelease #1 through `packs.services.legacy_import.publish_initial_releases` (the services' `publish`). A BLOCK
   item stops the step and names it; excluded packs are listed `pack_excluded` with their reasons (B-4: hybrid packs
   stay blocked by the bt1 finding, no waiver). Re-running re-uses the releases (`RELEASE_UNCHANGED`).
3. **Users (`users.json`, gap importer).** The accounts rules of the website sources (e-mail login, unusable password,
   `must_reset_password`, adoption of an existing address, reset links at the cutover) with PLAN's role map; the
   sales roles keep their flavour in `title` (`Sales`, `Sales (CRS)`, `Field Sales`); `ENGINEER` (what `users.json`
   holds) maps like `ENGINEERING`; an unknown role skips the row (`unknown_role`). The map key is
   `FLARIZE users.json <userId>` — the key catalog/pricing/procurement/packs already resolved; customers, projects
   and quotations looked up `FLARIZE users` (never written by anyone) and now use `users.json` too (fix).
4. **Company profile (`company-profile.json`, gap importer): fill only.** A value the profile holds (CMS import,
   Studio) is kept and the difference listed (`value_differs`) — D-9 leaves the legal entity on documents to the
   business. Bank/UPI details are never written (`enter_as_bank_account`; the account number is not repeated), the
   signature/seal/UPI QR asset ids are listed (`asset_not_linked`), fields without a column `listed_only`.
5. **Page-designer assets (`cms-state.json` + `cms-assets/`, gap importer).** PLAN: "not migrated; assets copied to
   media for reference". ACTIVE asset records are copied through the upload pipeline as PRIVATE images in folder
   `flarize-cms`, mapped `FLARIZE cms-state.json:assets <assetId>`, uploaded once; non-ACTIVE records, missing files
   and checksum mismatches are listed; the pages/versions/campaigns are reported (`page_designer_not_migrated`).
6. **Purchase Agreement exports.** One file per browser profile (`crs`, `admin`): the `flarize_agr` array, or a
   localStorage dump (`{"flarize_agr": "<json>", "flarize_trash": "<json>", …}`). Steps: KSEB fee bands
   (`import_pa_kseb_fees`; from the Upstash catalog export when given, else the page's built-in `KSEB_FEES`) → the
   catalog comparison (`report_pa_catalog`, listed only) → one step per profile (`import_pa_agreements`). A profile
   without an export is announced ("if that browser profile was cleared, its records are gone — tell the client before
   C8"). Records in the page's bin are listed `trashed_record_not_migrated`.
7. **PA ↔ SI link.** `import_pa` passes `uid_for = site_inspections.services.legacy_import.agreement_uid`, so an
   imported agreement's uid is `uuid5(SI_AGREEMENT_NAMESPACE, "PA:<record id>")` — the uid a legacy PA inspection
   carries. agreements cannot import site_inspections (dependency direction), so `import_pa_agreements` gained an
   optional `uid_for` argument (backward compatible: random uids without it). A record id already imported from the
   other profile keeps its uid there; the second copy gets a random one (`duplicate_record_id`). `import_si` lists
   every PA inspection whose agreement is not on the platform (`agreement_not_imported`, step label
   `agreement links`) — run `import_pa` first.
8. **Site Inspection plan.** `si.engineers` (users) → `si.customers` (masters) → `si.inspections` (transactional:
   inspections, photos, the layout photo links, annotations, equipment, approvals, observations — one batch, children
   resolve their inspection through the map). The SQLite file is opened `mode=ro`; stop the app first so its WAL is
   checkpointed. Not migrated: `purchase_agreements` (the PA page is the source), `engineer_sessions`,
   `site_inspection_activity` (the audit log replaces it), `site_inspection_materials`/`_documents` (dead tables).
9. **eSSL plan.** `essl.users` → `essl.hr` (shifts, offices, employees, holidays, leave types from the distinct
   `leave_type` strings, leave records, rules) → `essl.devices` (agents with new credentials, devices, per-device PIN
   links with the multi-device PIN report, watermarks, mappings, ADMS quarantine, 30 days of ADMS evidence) →
   `essl.attendance` (raw punches with content keys — collapsed duplicates listed `collapsed_duplicate` —, the v4
   recompute and the v3/v4 diff). Two results leave the batch: the **agent tokens** (shown once) are written to a 0600
   file (`--credentials-file`, never in a dry run), and the **diff report** for HR (`--diff-report`, B-7).
10. **Business defaults in every report.** The import report (stdout and `--json` → `business_defaults`) lists, in
    full and never capped: B-2 (`d2_flarize_wins`: every value the Flarize import changed, with both values), B-3
    (`pack_config_market_rate_wins`), B-5 (components a `1P-HYB`/`3P-HYB` slot never matches: the legacy filter also
    skips ACDB/DCDB rows stored as `1P`/`3P`, so the list covers the whole category), B-12 (EMI banks skipped for a
    malformed slug or logo colour); `import_essl` prints the B-7 summary (per-status v3/v4 totals).
11. **Check #1 for document sources.** `Plan.row_keys` gives, per source table, the `core_legacy_map` keys that must
    be mapped or listed: Flarize `users.json` (userId), `customers.json` (customerId), `quotation-state.json`
    (quotation ids), `workspace-state.json` (projectIds; open workspaces are listed), `cms-state.json` (asset ids);
    PA records `<profile>/<id>` under `flarize_agr`; SI logins through their engineer; eSSL `roles` (mapped onto the
    seeds), `sync_logs` (only the watermark is kept), `attendance` (recomputed, compared by #11) need none and
    `adms_requests` only those inside the retention window. Orphans are computed per map table over all source tables.
12. **#8 packs.** PackRelease #1 against the Flarize reference outputs (`packs/tests/golden/flarize_packs.json`, made
    by the real JavaScript with `generate_packs.mjs`): every pack Flarize priced (a market rate) and did not block must
    be released at the same `customer_price_incl_gst`; a priced pack the checker blocks must be excluded with
    `PACK_ENGINEERING_BLOCKED`; nothing unpriced may be released; the publish report must hold no BLOCK item. Cases
    are matched by (system, size, tier, future size, battery) — JSONB does not keep the configuration's key order, so
    the enumeration order of the stored configuration is not Flarize's. With `--flarize-dir` the reference must have
    been generated from the same four data files (SHA-256), else the check fails and asks for a regenerated reference.
13. **#9 quotations.** For every issued legacy version: the stored `document_payload` still hashes to its
    `document_payload_sha256`; the canonical payload a render job gets (`documents.services.jobs.canonical_payload`,
    i.e. a re-render's `payload_sha256`) has the same hash; with the Flarize source, the source document of that
    version has it too; the payload renders through the quotation template in English and Malayalam, twice, with
    identical HTML. Nothing is written (no render job). PLAN's page count / text diff against archived PDFs needs the
    archive, which the reference estate does not hold (pending DV).
14. **#11 attendance.** Raw punch accounting (every `attendance_raw` row mapped or listed, mapped punches exist,
    mapped − distinct = collapsed) and the v3/v4 per-employee-month diff rebuilt **read-only**
    (`attendance.services.legacy_import.diff_report`, split out of `status_diff_report`). The report is written with
    its SHA-256 (`--attendance-report`); the check is `skipped` (a failure unless `--allow-skipped`) until run with
    `--attendance-signoff <that sha256>` — the HR sign-off PLAN asks for is bound to the exact report HR saw.
15. **Reset links reach the Site Inspection engineers; re-runs never reactivate.** PLAN §7.5 "engineers → users
    (Field Engineer, reset)": `accounts.services.legacy_import.USER_MAPS` names every (source, map table) whose rows
    became staff accounts — CMS, BACKEND, FLARIZE and SI `engineers` — so `issue_reset_links` (`--send-reset-links`)
    and check #6 cover the engineers. An imported placeholder address (any `*.invalid`: `migrated.invalid`,
    `site-engineers.invalid`) is never mailed and is not a #6 failure; once staff replaced it, the next
    `import_si --send-reset-links` sends the link. A re-run (resume, a second rehearsal pass) never reactivates an
    account deactivated on the platform (`kept_inactive`, listed) — in `_import_user` (every website/Flarize account)
    and in the SI engineer import — otherwise a departed person would be reactivated and mailed a link.
16. **Check #1 and aged ADMS evidence.** `adms_requests` expected by #1 are the rows inside the retention window **or
    already mapped**: evidence imported at the cutover is not an "orphan" when #1 runs after it aged out.
17. **Idempotency fixes found by the rehearsals.** `import_flarize_pack_config` counted the registry pins as
    "updated" on every run although nothing changed; it now counts only pins written (created, changed, removed).

## Legacy mapping

### Flarize (`import_flarize`, source system FLARIZE)

| Step (phase) | Files | Importers | Targets |
|---|---|---|---|
| `flarize.users` (users) | `users.json` | `accounts…import_flarize_users` | `accounts_user` |
| `flarize.cms_assets` (media) | `cms-state.json` (+ `cms-assets/`) | `media…import_flarize_cms_assets` | `media_asset` (PRIVATE, `flarize-cms`) |
| `flarize.catalog_pricing` (masters) | `catalog.json`, `battery-master.json` | `catalog…import_flarize_catalog`, `pricing…import_prices`, `…import_flarize_pricing` | `catalog_*`, `catalog_battery_*`, `pricing_price` (LIST), `pricing_cost_config`, `pricing_installation_matrix`, `pricing_market_rate(_set)`, `pricing_offer` |
| `flarize.documents` (masters) | `cost-config.json`, `project-rate-card.json`, `quotation-policy.json`, `energy/savings/subsidy/finance-config.json` | `pricing…import_flarize_documents` | `pricing_cost_config` documents, `pricing_validity_policy` |
| `flarize.bom` (masters) | `catalog.json` | `bom…import_flarize_bom` (+ B-5 list) | `bom_*` |
| `flarize.procurement` (masters) | `procurement-state.json`, `procurement-price-master.json`, `commercial-history.json` | `procurement…import_flarize_procurement` | `procurement_supplier`, `procurement_batch(+lines, charges)`, `pricing_price` (PURCHASE, LANDED) |
| `flarize.packs` (masters) | `pack-config.json`, `packages.proposed.json` | `packs…import_flarize_pack_config` | `packs_config_version` (+ pins, typed mirror) |
| `flarize.company` (masters) | `company-profile.json` | `company…import_flarize_company_profile` | `company_profile` (fill only) |
| `flarize.quotation_content` (content) | `quotation-inclusions.json`, `tier-display-names.json`, `quotation-testimonials.json`, `quotation-content.json`, `quotation-branding-state.json` | `quotations…import_flarize_inclusions`, `_tier_names`, `_testimonials`, `_content`, `_branding` | `quotations_inclusion`, `_tier_display_name`, `_testimonial`, `_content_version` (PUBLISHED #1); branding reported |
| `flarize.customers` (transactional) | `customers.json` | `customers…import_flarize_customers` | `customers_customer` |
| `flarize.quotations` (transactional) | `quotation-counter.json`, `quotation-state.json` | `quotations…import_flarize_counter`, `_quotations` | `core_sequence_counter` QUO, `quotations_quotation`, `_version`, `_bom_snapshot`, `_commercial_snapshot` |
| `flarize.projects` (transactional) | `workspace-state.json`, `bom-state.json` | `projects…import_flarize_workspace_projects`, `report_flarize_bom_state` | `projects_project` (+ `bom_lock`); open workspaces and the BOM tool state reported |
| `flarize.releases` (releases) | (`pack-config.json`) | `packs…publish_initial_releases` | `pricing_release` #1, `packs_release` #1 |
| not migrated | `catalog.json.bak` (backup copy), `cms-state.json` pages (page designer superseded) | | |

### Purchase Agreement (`import_pa`, PA)

| Step | Source | Importer | Target |
|---|---|---|---|
| `pa.kseb_fees` (masters) | Upstash catalog `kseb` or the page's `KSEB_FEES` | `pricing…import_pa_kseb_fees` | `pricing_statutory_fee` (KSEB_REGISTRATION) |
| `pa.catalog_report` (masters) | Upstash catalog export | `agreements…report_pa_catalog` | — (listed) |
| `pa.agreements_crs`, `pa.agreements_admin` (transactional) | each profile's `flarize_agr` (+ `flarize_trash` listed) | `agreements…import_pa_agreements(uid_for=agreement_uid)` | `agreements_agreement` (ISSUED, legacy) |

### Site Inspection (`import_si`, SI)

| Step | Tables | Importer | Target |
|---|---|---|---|
| `si.engineers` (users) | `engineers`, `engineer_users` | `site_inspections…import_engineers` | `accounts_user` (Field Engineer) |
| `si.customers` (masters) | `customers` | `…import_customers` | `customers_customer` (by phone) |
| `si.inspections` (transactional) | `site_inspections`, `site_inspection_photos`, `_annotations`, `_equipment_assessments`, `_approvals`, `_observations` | `…import_inspections`, `import_photos`, `link_layout`, `import_annotations`, `import_equipment`, `import_approvals`, `import_observations` + the agreement link report | `site_inspections_*`, private media |
| not migrated | `purchase_agreements`, `engineer_sessions`, `site_inspection_activity`, `site_inspection_materials`, `site_inspection_documents` | | |

### eSSL (`import_essl`, ESSL)

| Step | Tables | Importer | Target |
|---|---|---|---|
| `essl.users` (users) | `users`, `roles` | `hr…import_users` | `accounts_user` |
| `essl.hr` (masters) | `shifts`, `offices`, `employees`, `holidays`, `leave_records`, `attendance_rules` | `hr…import_shifts`, `_offices`, `_employees`, `_holidays`, `_leave_types`, `_leave_records`, `_attendance_rules` | `hr_*` |
| `essl.devices` (masters) | `agents`, `devices`, `device_users`, `sync_logs`, `protocol_mappings`, `adms_unknown_devices`, `adms_requests` | `devices…import_all` | `devices_*` (+ new agent credentials) |
| `essl.attendance` (transactional) | `attendance_raw`, `attendance` | `attendance…import_all` (raw punches, recompute, diff) | `attendance_raw_punch`, `attendance_day` (v4) |
| not migrated | `alembic_version` | | |

## Fixtures

All committed fixtures are read-only exports of the legacy sources, masked; migration-ops adds only what no package had:

| Fixture | Built by | Notes |
|---|---|---|
| `fixtures/ops/si/flarize-site-inspection.db` | `build_si_sqlite.mjs` | the SI app's **own** `lib/db.ts` (schema + every `ensureColumn`) and `scripts/seed.ts` run unchanged (TypeScript transpiled; `better-sqlite3` shimmed over Node's built-in `node:sqlite` — the native module is not installed in the reference tree), then the site-inspections package's masked rows (`legacy_si.json`) inserted through the same connection; the seeded ENG-001 replaces the fixture's; the PA inspection references PA record `agr_1789902000000` and shares its customer phone. Clock, UUIDs and salts frozen: byte-reproducible (SHA-256 checked twice). |
| `fixtures/ops/pa/admin-localstorage.json` | `build_admin_export.py` | the `admin` profile as a localStorage dump (values as JSON strings): the page's demo seed records, one record id the `crs` profile also holds, one record in the bin. The `crs` export is `agreements/tests/fixtures/pa/flarize_agr.json` (recorded from the page by the agreements package). |
| `fixtures/ops/essl/essl_seeded.json` | `build_essl_fixture.py --export` | `SELECT *` of a private database built by eSSL's alembic chain + `scripts/seed.py` (admin hash replaced by a hash of a random string). `--load` builds the private "full" database from the attendance package's capture (a month of punches driven through eSSL's own endpoints). |
| `fixtures/ops/flarize/{users,company-profile,cms-state}.json`, `cms-assets/` | `export_flarize_fixtures.py` | users masked (names, e-mails; hashes dropped), company phone/bank masked, two page-designer assets + one archived record. Every other Flarize file comes from the fixtures of catalog/pricing/procurement/packs/quotations/customers/projects (`tests/ops_fixtures.py`). |

## Parity evidence

* `migrations_tools/tests/test_import_flarize.py` — the whole Flarize plan on the committed fixtures: every step
  imports; PriceRelease #1 and PackRelease #1 published with the 11 packs Flarize can sell; B-3 lists 228,000 →
  229,000; B-5 listed; a second run creates and updates nothing; `verify_migration` #1, #2, #4, #8, #9, #12 pass
  (#8: 11 packs equal to the JavaScript reference, 1 priced pack excluded by the checker; #9: 6 frozen documents,
  12 deterministic renders); #8 fails on a changed release price and on a reference generated from other files; #9
  fails on a changed frozen document.
* `migrations_tools/tests/test_import_ops.py` — `import_pa` → `import_si`: 7 agreements (+1 copy from the second
  profile), uid = the SI key, the inspection linked to its agreement and to the same customer (phone), trash and demo
  records listed, second runs 0/0, #1/#12 pass and #1 fails on a record saved after the import; `import_si` before
  `import_pa` lists `agreement_not_imported`. `import_essl` on the eSSL capture: 35 raw rows → 34 punches (1
  collapsed), 5 of 155 employee-days differ (the attendance package's documented A-rows), the token file is 0600 and
  written once, #11 is skipped until signed off, passes with the report's SHA-256 and fails when a day changes after
  the sign-off or a raw row loses its map; the seeded eSSL database imports.
* Rehearsals (`docs/migration/rehearsal-ops.md`): every command twice on fresh private databases, the real Flarize
  folder, private restores of both website databases, the SI fixture file and both eSSL fixture databases.

## Shared changes

* `accounts/services/legacy_import.py`: new `import_flarize_users`, `FLARIZE_USER_TABLE`, `USER_TABLES`
  (`issue_reset_links` covers Flarize accounts). Tests in `accounts/tests/test_legacy_import.py`.
* `company/services/legacy_import.py`: new `import_flarize_company_profile`. Tests in `company/tests/…`.
* `media/services/legacy_import.py`: new `import_flarize_cms_assets`. Tests in `media/tests/…`.
* `accounts/services/legacy_import.py` (review): `USER_MAPS` (+ SI `engineers`) for `migrated_users`/
  `issue_reset_links`, `placeholder_address()`, and `_import_user` never reactivates a platform-deactivated account
  (`kept_inactive`). `site_inspections/services/legacy_import.py` (review): the engineer re-import keeps a
  platform deactivation (`kept_inactive`). Tests in `accounts/tests/`, `site_inspections/tests/test_legacy_import.py`,
  `migrations_tools/tests/test_import_ops.py`.
* `agreements/services/legacy_import.py`: `import_pa_agreements(…, uid_for=None)` (optional; default unchanged).
* `attendance/services/legacy_import.py`: `status_diff_report` split into `_v3_days` / `_compare` (unchanged result)
  and a read-only `diff_report`.
* `packs/services/legacy_import.py`: `_pins` returns the pins written, so an unchanged re-run counts them skipped.
* `customers/`, `projects/`, `quotations/services/legacy_import.py`: the Flarize user map table `users` → `users.json`
  (the key the users import and catalog/pricing use); their tests' map rows follow. Docs: leads-customers.md,
  quotations.md.
* `migrations_tools` (migration-website's files): `cli.py` hooks, `runner.py` phase `releases` + `row_keys`,
  `source.py` sources, `verify.py` plans/checks, `backend.py` B-5 list after the BOM step, `verify_migration`
  options (default `--source` unchanged: cms + backend).

## Pending DV numbers

For the integrator to number (not appended to docs/DEVIATIONS.md):

| Pending | PLAN ref | Deviation | Reason |
|---|---|---|---|
| ops-1 | §7.6 #9 "PDF page count equal and text diff empty against the archived PDFs" | #9 compares hashes (stored, source, re-render payload) and renders the HTML twice in both languages; no PDF page count / text diff | the reference estate holds no archived Flarize PDFs; the payload hash is what a re-render is bound to (quotations decision 10) |
| ops-2 | §7.6 #11 "diff report reviewed and signed by HR" | the sign-off is `--attendance-signoff <sha256 of the report>`; without it #11 is `skipped` | binds the sign-off to the exact report HR reviewed; no sign-off table exists |
| ops-3 | §7.4 `company-profile.json` → `company_profile`, `company_bank_account` | fill only (existing values kept, differences listed); bank/UPI details and asset links not written | D-9 leaves the entity and bank accounts to the business; the Flarize bank values are demo values |
| ops-4 | §7.4 `cms-assets/` "copied to media for reference" | PRIVATE images in the unreserved folder `flarize-cms`, ACTIVE records only | not public material; staff can find them in the library |
| ops-5 | §7.5 PA "JSON export of `flarize_agr`" | accepted shapes: the array, or a localStorage dump with `flarize_agr` (and `flarize_trash`, listed); KSEB fees from the Upstash catalog export or the page's built-in list | the page offers no export; operators copy localStorage; the fee list lives in the catalog or the page code |
| ops-6 | §7.1 "Order … transactional rows → releases" | runner phase `releases` after `transactional` | the release publish needs every master and is the last step |
| ops-7 | CLAUDE.md "Touch only your own app directories" | the Flarize user map table unified to `users.json` in customers/projects/quotations | three importers resolved a map table nobody wrote (`users`), so every Flarize owner was `unmapped` |

## Open issues

* No production copy of any operational source exists here: the SI file and the eSSL databases are fixtures built by
  the apps themselves; the PA exports are the page's own records. Rehearsal 2 on production snapshots is the first
  run with real volumes (C5/C6/C8).
* The Flarize `users.json` holds 5 accounts while `customers.json`/`workspace-state.json`/`quotation-state.json` name
  other user ids (`user-…`): their owners stay empty (`unmapped_owner`, `unmapped_user`) — the business must decide
  whether those people get accounts before C5.
* #8's reference must be regenerated (`generate_packs.mjs`, which also checks its fixture) whenever the production
  `pack-config.json`/`catalog.json`/`packages.proposed.json`/`battery-master.json` differ from the committed ones.
* The pricing importer reports `market_rate_set_locked` on every re-run after PriceRelease #1 (the imported set was
  activated by the release): by design (a published set is never rewritten), listed not failed.
