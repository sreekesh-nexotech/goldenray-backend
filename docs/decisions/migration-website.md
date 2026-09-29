# migration-website — import, verification and rollback tooling for the website sources

Work package migration-website builds `migrations_tools` for the two website sources of PLAN §7: the CMS (`blog_cms`,
§7.2) and the main backend (`GoldenApp`, §7.3). It follows §7.1 (principles), §7.6 (checks 1–7, 10 and 12; 8, 9
and 11 belong to Flarize, quotations and eSSL) and §7.7 (`export_delta`). Deviations: DV-117 … DV-121. Rehearsal
report: `docs/migration/rehearsal-website.md`.

## What exists

| Area | Where | Notes |
|---|---|---|
| Commands | `migrations_tools/management/commands/{import_cms,import_backend,verify_migration,export_delta}.py` | thin; the logic is in `migrations_tools/services/` |
| Source | `services/source.py` | `PostgresSource` (read-only session), `TablesSource` (exported JSON), `checksum`, `redact_url` |
| Runner | `services/runner.py` | `Plan`/`Step` (FK order enforced), batch audit rows, dry run, resume, `FileReader` for the media volumes |
| Plans | `services/cms.py`, `services/backend.py` | which legacy tables each step reads and which importers it calls; `not_migrated` lists every other table with its reason |
| Verification | `services/verify.py`, `services/parity.py`, `services/http.py` | the checks; legacy-vs-new comparison; in-process or HTTP clients |
| Rollback | `services/delta.py` | `export_delta` |
| Gap importers (shared) | `accounts/services/legacy_import.py`, `media/services/legacy_import.py`, `company/services/legacy_import.py` | roles/users, CMS media library, site and quotation settings — nothing imported them before |
| Fixtures | `migrations_tools/tests/fixtures/{cms,backend}.json`, `export_fixtures.py` | every plan table exported read-only from the UAT dumps (pincodes cut to 30 codes + those in use; installation customers masked) |

## Commands

```
import_cms | import_backend  (--source-url postgresql://… | --source-fixture FILE)
    [--dry-run] [--verify [--legacy-api-url URL] [--offline]] [--resume [RUN_UID|latest]] [--only STEP]…
    [--list-steps] [--media-root DIR] [--actor EMAIL] [--json FILE] [--send-reset-links] [--site-url URL]
verify_migration [--source cms|backend]… [--cms-url URL|--cms-fixture FILE] [--backend-url URL|--backend-fixture FILE]
    [--legacy-cms-api URL] [--new-api-url URL] [--offline] [--corpus-dir KIND DIR]… [--check N]…
    [--list-prices-as-release] [--allow-skipped] [--json FILE]
export_delta --source cms|backend --since ISO [--until ISO] --output FILE
```

Run `import_cms` before `import_backend` (job applications resolve their posting through the CMS map). Exit status is
non-zero when a step fails, when a check fails, or when a required check was skipped (`--allow-skipped`; the media
HEAD check may be skipped with `--offline`).

## Decisions

1. **One batch = one step, committed on its own with its audit row.** `migrations_tools.batch_imported` (object
   `migrations_tools.importrun` = the run uid) carries the source system, step, phase, per-table row count and SHA-256,
   the importers' normalised counts, the violation codes and the source ids the violations name (`listed`, capped at
   5,000 per table). No new table: the audit log is the record PLAN §7.6 #12 asks for, and it is what `--resume`
   reads. A failing step rolls back alone; the error names the run uid to resume.
2. **Resume = skip a step already recorded for the run with the same source checksum.** Everything else re-runs; the
   importers are idempotent, so re-running is always safe. `--resume` without a uid takes the latest committed run.
   The catalog import and the price import are one step because the prices are the catalog importer's return value.
3. **Dry run = the whole run in one transaction, rolled back, with the media storages swapped for a discarding one.**
   Counts and violations are those of a real run (later steps see the earlier steps' rows); nothing is written — no
   rows, no audit rows, no files. The importers' own `dry_run` flags are never used by the runner, because a nested
   rollback would hide the earlier steps' rows from the later ones.
4. **Normalised results.** Every importer's result becomes `{created, updated, unchanged, violations}` (`skipped` and
   `unchanged` are merged: both mean "row not written by this run").
5. **"Listed" rows count as accounted for** in check #1: a source row is fine when `core_legacy_map` maps it (exact id,
   or `<id>:<part>` for importers that explode a row, e.g. `bom_globalcosts`, `bom_marketrate`) or when the latest
   batch of its step named it in a violation. A non-empty source table that no step imports and no `not_migrated`
   reason lists fails the check, and so does a mapped id that no longer exists in the source.
6. **Delivery parity compares the legacy CMS and the new public API request by request** (§7.6 #3): the collection
   list (`populate=*&pagination[pageSize]=100`) and every published slug (`populate=*&filters[slug][$eq]=`) of every
   collection, `page-content` for every page route, `faqs` for every route with FAQs and every (route, section),
   `job-positions` (list, per department, per posting). Only the documented normalisations apply: `updatedAt` dropped
   (collections), FAQ `id` → uid (§6.3), posting `id` → `uid` (DV-47). The new API is called in process (throttles
   off, request logging quiet) or over HTTP (`--new-api-url`).
7. **Calculator equality (§7.6 #10) replays the committed UAT corpora** of the calculators and EMI packages
   (`calculators/tests/parity/uat`, `emi/tests/parity/uat`; 1,228 requests) with their approved differences (legacy
   500 → 400 `invalid_input`, envelope `message`, EMI ids → uids). `--list-prices-as-release` is a rehearsal mode
   (DV-119).
8. **Pricing equality (§7.6 #7)** for these sources: every `bom_catalogitem` price/per watt and website battery price
   equals the component's current LIST row; components the Flarize catalog also maps are not compared (D-2: Flarize
   wins, verified with the Flarize import).
9. **Users (§7.6 #6)**: every migrated account has a live role, no usable password it did not set itself, and — when
   active with a real address and still `must_reset_password` — an issued reset link. Links are issued at the cutover
   by `--send-reset-links` (`accounts.services.legacy_import.issue_reset_links`: once per account, never while an
   unused link is open; `@migrated.invalid` and inactive accounts are listed, not sent).
10. **Media (§7.6 #4)**: HEAD 200 for every migrated public `cdn_url` (skippable with `--offline`), SHA-256 of every
    stored copy (CMS re-uploads, the offer image, job-application resumes and portfolios) against the row.
11. **Audit (§7.6 #12)** also compares each batch's table checksums with the source's current checksums: a source
    changed after its import (outside the freeze window) fails the check and names the step to re-run.
12. **Integrity (§7.6 #2)**: no `core_legacy_map` row points at a missing target, no foreign key is `NOT VALID`, and
    every foreign key declared without a database constraint (`db_constraint=False`) resolves.
13. **`export_delta`** exports, per target table of the source's plan, the rows created, updated or deleted in the
    window with their legacy origin (`core_legacy_map`, `null` for platform-born rows), foreign keys as uids, secrets
    and password hashes masked, plus the window's audit actions (import audit rows excluded). Mode `0600` — it holds
    personal data.
14. **Gap importers follow the legacy import contract** (plain rows in, `{created, updated, skipped, violations}` out,
    `core_legacy_map`, one audit row per call, `dry_run`):
    * **roles** (DV-117): CMS modules re-keyed onto the registry (`careers` → `job_positions` + `career_page`), unknown
      actions dropped and listed, scopes `all`; a seeded slug gets the CMS grants for the CMS-vocabulary modules and
      keeps the seed's grants elsewhere; `super-admin` keeps every grant; other CMS roles become custom roles;
    * **users**: e-mail login (lower-cased; `<username>@migrated.invalid` when empty/invalid), unusable password,
      `must_reset_password`; role = mapped `access_role_id`, else `admin`→Admin, `editor`→Content Manager,
      `author`→custom role `cms-author` (blogs only); `auth_user` → Admin; an existing live account with the same
      e-mail is adopted (mapped, role untouched); a re-run keeps following the source's role only until the person
      has set a password;
    * **media** (DV-118): Bunny `cdn_url` and storage key kept, `/uploads`-only files re-uploaded synchronously,
      checksum when the bytes are on the volume, folder = the collection's `api_uid`;
    * **company**: `siteconfig_settings` → profile (empty texts never blank a value; phone to E.164; lists split,
      lower-cased, de-duplicated; `website` from `--site-url` when empty, DV-121); `bom_quotationsettings` → the
      `quotation_offer_*` fields (uploaded `offer_image` through the media pipeline, mapped by content so it is
      uploaded once). Writes go through `update_profile` (validation, audit, outbox).

## Legacy mapping

### CMS (`import_cms`)

| Step | Legacy tables | Importer | Target |
|---|---|---|---|
| `cms.users` | `accounts_role`, `accounts_admin_user` | `accounts.services.legacy_import.import_roles`, `import_cms_users` | `accounts_role`, `accounts_user` |
| `cms.media` | `media_asset` (+ `catalog_collection` for folders) | `media.services.legacy_import.import_cms_assets` | `media_asset` |
| `cms.blog_schema` | `catalog_collection`, `catalog_template(+_image_group, _attribute_slot)`, `catalog_author`, `catalog_category`, `catalog_tag`, `catalog_badge` | `blog.services.legacy_import.import_all` | `blog_*` masters |
| `cms.careers_departments` | `careers_department` | `careers…import_departments` | `careers_department` |
| `cms.faq_categories` | `faqs_category` | `faqs…import_categories` | `faqs_category` |
| `cms.pages` | `sitepages_page(+_seo, _text_slot, _image_slot)` | `sitepages…import_all` | `sitepages_*` |
| `cms.faqs` | `faqs_faq` | `faqs…import_faqs` | `faqs_faq` |
| `cms.blog_content` | `content_entry`, its M2M tables, `content_entry_slug_history`, `content_content_block`, `content_entry_image`, `content_entry_attribute_value`, `content_seo` | `blog…import_all(user_map=…)` | `blog_entry` and children |
| `cms.careers_positions` | `careers_job_position` | `careers…import_positions` | `careers_job_position` |
| `cms.site_settings` | `siteconfig_settings` | `company…import_site_settings`, `seo…import_site_seo_defaults` | `company_profile` |
| not migrated | `django_admin_log` (§7.2 row 12 optional), `django_session`, `token_blacklist_*`, `authtoken_token`, `auth_*`, `accounts_admin_user_groups/_user_permissions`, framework tables | — | — |

### Main backend (`import_backend`)

| Step | Legacy tables | Importer | Target |
|---|---|---|---|
| `backend.users` | `auth_user` | `accounts…import_backend_users` | `accounts_user` (Admin) |
| `backend.reference` | `kseb_tariffs`, `device_types`, `wattages`, `room_size`, `ev_cars`, `ev_scooters`, `pincodes` | `reference.services.legacy_import.*` | `reference_*` |
| `backend.catalog_pricing` | `solar_panels`, `solar_inverters`, `batteries`, `bom_category`, `bom_catalogitem`, `bom_itemtier`, `bom_globalcosts`, `bom_marketrate`, `bom_offer` | `catalog…import_goldenray_products`, `import_bom_catalog`; `pricing…import_prices`, `import_bom_global_costs`, `import_bom_market_rates`, `import_bom_offers` | `catalog_*`, `pricing_price` (LIST), `pricing_cost_config`, `pricing_market_rate(_set)`, `pricing_offer` |
| `backend.bom` | `bom_bomtemplate`, `bom_bomslot`, `bom_bomfixeditem`, `bom_structuretemplate`, `bom_structuretemplateitem`, `bom_tubeweight` | `bom…import_goldenray_bom` | `bom_*` |
| `backend.calculators` | `solar_installations`, `solar_installation_new` | `calculators…import_all` | `calculators_capacity_size`, `calculators_bill_range_size` (DV-82) |
| `backend.emi` | `emi_bank`, `emi_interest_rate_rule`, `emi_subsidy_rule`, `emi_calculator_settings`, `emi_system_size` | `emi…import_all` | `emi_*` (DV-83) |
| `backend.company` | `bom_quotationsettings` | `company…import_quotation_settings` | `company_profile` |
| `backend.seo` | `goldenray_metadata` | `seo…import_page_metadata` | `seo_page_metadata` |
| `backend.leads` | `affiliate_application`, `warranty_service_request`, `customer_installations`, `lead_collection_home` | `leads…import_all` | `leads_*` |
| `backend.careers` | `job_application`, `job_application_note`, `job_application_event` | `careers…import_applications` (files from `--media-root`), `…_notes`, `…_events` | `careers_job_application(+note, event)` |
| not migrated | `bom_quotationtestimonial`, `sent_quotes` (DV-120), `send_quote_junk` (§7.3 dropped), `django_*`, `auth_*` except `auth_user` | — | — |

## Parity evidence

* Rehearsal (`docs/migration/rehearsal-website.md`): both sources imported from restored UAT dumps into a fresh
  migrated + seeded database; `verify_migration` — 71 CMS delivery requests with 0 differences against the running
  legacy CMS; 261 prices equal; 1,228 calculator requests with 0 differences in rehearsal mode (69 hybrid-battery
  differences without a PriceRelease, explained); second run of both imports: 0 created, 0 updated.
* Tests (`migrations_tools/tests/test_verify.py::test_a_clean_import_passes_every_check`): the committed exports are
  imported by the commands and every check passes — check #3 against the legacy CMS responses recorded by the blog,
  sitepages, faqs and careers packages (the same seeded CMS), check #10 on a slice of the corpora; each check has a
  test that fails it on the defect it exists for.

## Shared changes

* `accounts/services/legacy_import.py` (new), `accounts/tests/test_legacy_import.py`.
* `media/services/legacy_import.py` (new), `media/tests/test_legacy_import.py`.
* `company/services/legacy_import.py` (new), `company/tests/test_legacy_import.py`.
* No existing function, model, migration or setting changed.

## Open issues

* PriceRelease #1 must be published before the calculators cutover (rehearsal finding 1).
* `bom_quotationtestimonial` / `sent_quotes` wait for the quotations package (DV-120): add a `backend.quotations`
  step calling its importer when it is integrated.
* The UAT sources hold no admin users, media, leads or applications; rehearsal 2 on the production snapshot is the
  first run with real volumes (pass `--media-root`).
* `verify_migration` #3 covers the CMS delivery endpoints named in §7.6 #3; the main backend's public endpoints are
  covered by their packages' parity suites and the calculators replay (#10).

## Review findings (adversarial review of the package)

Each finding was reproduced by a failing test first; the tests stay in the suite.

| # | Finding | Fix | Tests |
|---|---|---|---|
| R1 | An account **adopted** by e-mail (the bootstrap Super Admin, or the same person in both sources) was rewritten by every re-run of the adopting row while it still had `must_reset_password`: role, names and active flag followed that row — the CMS and main-backend imports flipped a shared account between Content Manager and Admin on alternate runs, and a CMS author row could demote a Super Admin who had not set a password yet | adoption is audited (`accounts.legacy_user_adopted`, naming the source row); a re-run of an adopting row never writes the account — only the row that created an account keeps it in step with the source | `accounts/tests/test_legacy_import.py::TestUsers::test_adopted_account_is_never_rewritten_by_a_rerun`, `…::test_account_adopted_across_sources_keeps_the_creating_source` |
| R2 | `verify_migration --source cms|backend` (and therefore `import_cms/import_backend --verify`) always failed: the other source's checks (#3/#5 or #7/#10) were reported `skipped` and a skipped check fails the command | those checks are `n/a` (printed, never a failure); `skipped` stays for checks that miss an input | `migrations_tools/tests/test_verify.py::TestCommand::test_one_source_is_not_failed_by_the_other_sources_checks` |
| R3 | `--verify` after `--source-fixture` verified without the source (checks #1, #3, #5, #7 skipped → failure); `--verify` with `--dry-run` verified the state before the (rolled back) import | the fixture is passed on as `--cms-fixture/--backend-fixture`; `--dry-run --verify` is refused | `…::test_import_verify_verifies_the_imported_fixture`, `…::test_import_refuses_verify_with_dry_run` |
| R4 | The CMS media re-upload saved the bytes **before** the storage-key uniqueness was checked: a CMS `/uploads` row whose key another asset already used overwrote that asset's public file, then was refused | the key is checked before anything is written (`storage_key_taken`, skipped) | `media/tests/test_legacy_import.py::test_reupload_never_overwrites_a_file_another_asset_owns` |
| R5 | `export_delta --output` onto an existing file kept that file's mode (`O_CREAT` only applies the mode to new files): the personal-data export could stay world-readable | `fchmod(0600)` before writing | `migrations_tools/tests/test_export_delta.py::test_an_existing_output_file_is_made_private` |
| R6 | `export_delta` silently skipped the link tables without timestamps (`blog_entry_category/_tag/_badge`): an article's taxonomy changed after the cutover could not be replayed | link tables export the current links of every exported parent row (`change: current`) | `…::test_link_tables_without_timestamps_follow_their_changed_entry` |
| R7 | `--resume <uid>` accepted any text (every step then crashed on the audit row's UUID) and any run uid, including a run of the other source, whose steps it then extended | the uid must be a UUID with batch rows of this source | `migrations_tools/tests/test_import_commands.py::TestOptions::test_resume_needs_a_run_of_this_source` |

Re-run of the rehearsal on fresh private restores (`rv_migration_website_{cms,goldenapp}` → `flarize_rv_migration_website_target`):
the same counts as the committed report (CMS 231 created / 1 updated, main backend 6,061 / 1), every check passing with
`--list-prices-as-release` (#3: 71 requests against the legacy CMS, 0 differences; #10: 1,228 requests, 0 differences),
second run 0 created / 0 updated. With synthetic admin users, auth users and media rows added to the private sources
(what UAT lacks): users adopted across sources stay stable over repeated runs of both imports, `--send-reset-links`
issued 3 links (3 listed: no real address or inactive), and #1, #2, #4, #6 and #12 pass.
