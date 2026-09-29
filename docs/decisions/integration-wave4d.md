# Wave-4d integration (migration-ops)

`wp/migration-ops` was merged into `claude/bold-goodall-lxgwep` with `git merge --no-ff`. It had merged older copies
of `wp/site-inspections`, `wp/projects`, `wp/legacy-shim` and `wp/agreements`; the integration branch had since added
their integration commits (a4f6833 wave 4b, a5616f4/a59fab7 wave 4c). After the merge I ran the fast checks (`check
--database default --fail-level WARNING`, `makemigrations --check`, `lint-imports`, the migrations_tools tests and the
legacy-import tests of every app the package touched); after the wiring every gate once (black, isort, flake8, both
Django checks, `makemigrations --check`, `lint-imports`, `spectacular --validate --fail-on-warn`, the view budget, the
on-commit enqueue check of f-fix #9, `bash -n` of the deploy scripts) and the full pytest suite.

## Merge conflicts

* `docs/DEVIATIONS.md`: the package carried agreements' pre-renumbering rows DV-122 … DV-128; the integration side's
  DV-135 … DV-141 kept (wave 4c already renumbered them).
* `projects/serializers/projects.py`: the integration side's `ProjectCancelSerializer` kept (wave 4b's fix for the
  same `CancelSerializer` component-name clash the package fixed with `extend_schema_serializer`).
* `agreements/services/legacy_import.py`: the integration side's `uid_for` hook kept — it defaults to
  `site_inspections.services.legacy_import.agreement_uid`. `migrations_tools/services/pa.py` no longer passes (or
  imports) `agreement_uid`: it relies on the default, so the link rule exists in site_inspections only. The package's
  test of the hook is kept; its last assertion ("without `uid_for` the uid is random") now asserts the wave-4c default.
* Engineering waiver key: the package does not touch it; `projects/services/bom_lock.py` keeps the wave-4b version that
  uses `engineering.services.runs.acknowledged_keys` / `finding_key`.
* Requirements: unchanged by the package. Migrations: the package added none, so no merge migration.

## Deviations and business defaults

* migration-ops' pending `ops-1` … `ops-7` are **DV-142 … DV-148** (appended to docs/DEVIATIONS.md; citations in
  docs/decisions/migration-ops.md updated).
* business-defaults.md: **B-15** (`import_pa --only pa.kseb_fees` before `import_flarize` at cutover, so PriceRelease
  #1 carries the KSEB fees; also in docs/ops/runbook.md §7 "Data import order") and **B-16** (the Flarize owner/user
  ids missing from `users.json` stay unmapped and are listed in the import report).

## Rehearsal of the cutover order

One end-to-end run on fresh private databases (dropped afterwards): see docs/migration/rehearsal-ops.md "Integration
rehearsal (wave 4d)". It found one integration defect, fixed here: verify #1 for the main backend failed after the
Flarize import, because the D-2 `bom_itemtier` rows are listed by the Flarize run. `verify.cross_listed` counts rows
another source's committed runs listed under a table that is not one of that source's own
(`migrations_tools/tests/test_verify.py::test_rows_listed_by_another_sources_import_count_as_accounted`).

## Open (business)

* No production copy of the Flarize, PA, SI or eSSL sources exists here; the first real-volume run is on staging
  snapshots at C5, C6 and C8.
* #9 has no archived Flarize PDFs to compare page counts / text against (DV-142).
* B-5 lists only components with a phase set; `JsonFilesSource` hands the same parsed document to every step without
  copying (both left as they are by the package; no problem in the rehearsals).
