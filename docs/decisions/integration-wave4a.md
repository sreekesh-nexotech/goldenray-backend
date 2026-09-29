# Wave-4a integration (quotations, migration-website)

`wp/quotations` and then `wp/migration-website` were merged into `claude/bold-goodall-lxgwep` with
`git merge --no-ff`. Both packages had merged the integration branch at wave 3c before they started. After each merge
I ran the fast checks: `check --database default --fail-level WARNING`, `makemigrations --check`, `lint-imports`, and
the merged package's own tests. After the integration fixes I ran every gate once: black, isort, flake8,
`check --database default --fail-level WARNING` (on a freshly migrated database), `makemigrations --check`,
`lint-imports`, `spectacular --validate --fail-on-warn`, the view budget, the on-commit enqueue check (f-fix #9),
`bash -n` of the deploy scripts, and the full pytest suite.

## Merge conflicts, deviation numbers, requirements, migrations

* **DEVIATIONS.md.** Both packages started numbering at DV-104. quotations keeps DV-104 … DV-116. migration-website's
  five rows were renumbered to the next free numbers (old → new):

  | Old | New |
  |---|---|
  | DV-104 | DV-117 |
  | DV-105 | DV-118 |
  | DV-106 | DV-119 |
  | DV-107 | DV-120 |
  | DV-108 | DV-121 |

  I updated every citation in `docs/decisions/migration-website.md` and `docs/migration/rehearsal-website.md`. The
  package's code and tests cite no DV number.
* **Requirements.** Neither package changed a requirements file.
* **Migrations.** Only quotations added one (`0001_initial`), so no merge migration was needed.
* **engines.** Neither package added or duplicated an engines module.

## Wiring the deferred migration items (DV-120)

Before this integration, `import_backend` listed `bom_quotationtestimonial` and `sent_quotes` as not migrated. Now a
new step, `backend.quotations`, imports both. It runs in the transactional phase, after careers, and calls the
quotations package's `import_backend_testimonials` and `import_sent_quotes`.

* **Testimonials.** They land in `quotations_testimonial` with `show_on_website = true`. An uploaded `photo` file is
  listed (`photo_file_not_migrated`) but not copied.
* **Sent quotes.** `import_sent_quotes` now also links each row in `core_legacy_map` (`BACKEND sent_quotes <id>`),
  besides the existing `legacy_ref = quote_id` idempotency key. Without that link, `verify_migration` #1 (row counts
  against `core_legacy_map`) could never account for the table. A row without a `quote_id` is listed as before.
* **Rollback export.** `export_delta` now includes `quotations_testimonial` and `quotations_email_log` in the backend
  targets, so rows changed after the cutover are exported for a rollback.
* **Verification.** Covered by `migrations_tools/tests/test_quotations_step.py`, using the UAT backend export plus
  synthetic, masked `sent_quotes` rows (the UAT dump has none):
  * both tables are imported and mapped;
  * a re-run creates nothing;
  * checks #1, #2 and #12 pass;
  * check #1 fails when a testimonial or sent-quote mapping is missing;
  * check #2 fails when an imported e-mail log row is deleted.

## Quotations importer: a re-run must not overwrite platform changes (review bug)

**Bug.** `import_flarize_quotations` upserted the Flarize state over an imported quotation whatever had happened to it
since. Suppose an imported quotation was then accepted on the platform. A re-run set its status back to ISSUED but kept
`accepted_at`, which violated `quotations_quotation_accepted_at_only_accepted`. The IntegrityError rolled back the
whole import. `test_reimport_keeps_a_quotation_accepted_on_the_platform` reproduced this before the fix.

**Fix.** A quotation that the platform changed after its last import is now skipped and reported as
`modified_on_platform`. The importer treats a quotation as changed when either of these holds:

* its `updated_at` is later than the legacy map's `imported_at`. Every platform write goes through `versioned_update`,
  which stamps now. The importer stamps the source's `updatedAt` and links the map afterwards, so an import never
  trips this test.
* it has a version that the import did not create, such as a revision's draft.

Unchanged quotations are still updated from the source. Tests: `quotations/tests/test_legacy_import.py::test_reimport_*`
(accepted, cancelled, revised, and untouched-still-updated).

## B-8: attendance `process/` and `recalculate/` apply the caller's record scope

Business default B-8 (`docs/decisions/business-defaults.md`) says `process/` and `recalculate/` must respect the
caller's attendance record scope. They now do:

* **Out-of-scope names.** If `employee_uids` names anyone outside the scope, the request answers 404 `not_found`,
  the same answer as the scoped timeline, and nothing is recomputed.
* **Unknown uids.** These still answer 400 `validation_error`.
* **No `employee_uids`.** Only the people in scope are recomputed. Under the `all` scope that is everyone. A caller
  with a narrow scope but no employee record recomputes nobody (fail closed).

Tests: `attendance/tests/test_process_scope.py`, with the existing `test_process_api.py` unchanged.

The recalculate device report and `process-all/` are left as they were. B-8 names only the two endpoints, and
`process-all/` stays a manage-only, whole-dataset queue.

## Business defaults

`/home/user/platform-reference/business-defaults.md` is copied to `docs/decisions/business-defaults.md`, with two
assumed decisions added from the quotations package's open questions:

* **B-13.** The D-4 savings, payback and EMI figures stay computed on the pre-discount price (Flarize parity).
* **B-14.** An explicit `offer_code` may apply any ACTIVE offer (Flarize parity).
