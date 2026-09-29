# Wave-4c integration (agreements)

`wp/agreements` was merged into `claude/bold-goodall-lxgwep` with `git merge --no-ff`. After the merge I ran the fast
checks (`check --database default --fail-level WARNING`, `makemigrations --check`, `lint-imports`, the agreements
tests). After the integration wiring I ran every gate once: black, isort, flake8, `check --database default
--fail-level WARNING`, `makemigrations --check`, `lint-imports`, `spectacular --validate --fail-on-warn`, the view
budget, the on-commit enqueue check (f-fix #9), `bash -n` of the deploy scripts, and the full pytest suite.

## Merge conflicts, deviation numbers, requirements, migrations

* **DEVIATIONS.md.** The integration branch already used DV-122 … DV-134 (site-inspections DV-122 … DV-127, projects
  DV-128 … DV-129, legacy-shim DV-130 … DV-134). agreements' rows moved to the next free numbers (old → new):

  | Old | New | Subject |
  |---|---|---|
  | DV-122 | DV-135 | `owner_id` (owned scope anchor) |
  | DV-123 | DV-136 | `revision` column (PLAN `version`) |
  | DV-124 | DV-137 | component FKs beside the printed columns |
  | DV-125 | DV-138 | `number`, extras, partial uniques (one agreement in force) |
  | DV-126 | DV-139 | `agreements_line.additional_work_item_uid` |
  | DV-127 | DV-140 | action permissions |
  | DV-128 | DV-141 | agreements events |

  Citations updated in `docs/decisions/agreements.md`, `agreements/models/agreement.py` (comments and the `revision`
  help text) and the unreleased `agreements/migrations/0001_initial.py` (help text only; no schema change). Citations
  of site-inspections' own DV-122 … DV-127 (e.g. DV-127, "EXTRA_STRUCTURE events are ignored") are unchanged.
* **Other conflicts.** `flarize/settings/base.py`: both sides' SPECTACULAR `ENUM_NAME_OVERRIDES` kept.
  `customers/tests/test_timeline.py`: the integration side's subset check kept, with `agreements.agreements` added.
* **Requirements.** agreements changed no requirements file.
* **Migrations.** `agreements` gains `0001`/`0002` in its own app; no other app got migrations, so no merge migration.
* **engines.** No engines module was added or duplicated.

## Integration wiring

1. **COST_CALCULATED validator.** `AgreementsConfig.ready()` → `registrations.register()` now calls
   `site_inspections.services.work.register_agreement_validator(extra_structure_problem)`. An additional-work item
   reaches COST_CALCULATED only with the uid of a live ISSUED/ACCEPTED EXTRA_STRUCTURE agreement whose
   `source_type = SITE_INSPECTION` and `source_uid` = that inspection's uid; otherwise `agreement_invalid` naming
   the problem (unknown or malformed uid, wrong kind, DRAFT/SUPERSEDED/CANCELLED, raised from another inspection).
   The sales layer's siblings are `:`-separated (non-independent), so `lint-imports` keeps all contracts.
   `site_inspections/tests/test_lifecycle.py::TestAdditionalWork::test_lifecycle_is_enforced` used a random uid for
   COST_CALCULATED; it now also asserts that a random uid is refused and uses a real issued EXTRA_STRUCTURE agreement
   (`site_inspections.tests.factories.issued_extra_structure`).
2. **Legacy PA uid.** `agreements.services.legacy_import.import_pa_agreements(…, uid_for=None)` gives a newly imported
   agreement `uid_for(record id)`, defaulting to `site_inspections.services.legacy_import.agreement_uid` =
   `uuid5(SI_AGREEMENT_NAMESPACE, "PA:<record id>")`, the reference a legacy PA inspection carries. A record id already
   imported from the other profile keeps a random uid (`duplicate_record_id` violation; the inspections link to the
   first). The `uid_for` hook matches the one the concurrent migration-ops build passes explicitly.
3. **Event contracts, end to end** (`agreements/tests/test_site_inspection_wiring.py`, real outbox drain, real
   handlers): an issued Purchase Agreement's `agreements.issued` is consumed by `site_inspections.events` (a DRAFT
   AGREEMENT inspection with the agreement's uid, number, system type, quoted size, consumer number and phone; nothing
   parked); its revision's `agreements.superseded` re-points the same inspection; the released inspection's
   `site_inspections.released` is accepted by projects' handler (auto-create on) and the project carries the
   agreement, quotation version, size and phase. An issued EXTRA_STRUCTURE agreement creates no inspection.
4. **Quotations → agreements (DV-104).** Not added. DV-104 leaves `agreement_id` off `quotations_quotation` because
   agreements owns the link; it exists as `agreements_agreement.quotation_version` (PROTECT FK, DV-2), and a quotation
   column would point the dependency the wrong way. Whether the quotation detail should show its agreement is an open
   product question.

## Left open

* `migrations_tools` does not yet call `import_pa_agreements` (the migration-ops build is adding the `import_pa` step).
* The business questions listed by the agreements package (trash/restore, customer OTP acceptance, draft revision left
  behind by a cancel, re-pin conflict detected only at issue, `₹ 0/–` legacy cosmetics, Playwright rendering of the
  Malayalam/Hindi templates) are unchanged.
