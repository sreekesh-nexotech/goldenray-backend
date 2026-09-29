# Wave-4b integration (site-inspections, projects, legacy-shim)

`wp/site-inspections`, `wp/projects` and then `wp/legacy-shim` were merged into `claude/bold-goodall-lxgwep` with
`git merge --no-ff`. After each merge I ran the fast checks: `check --database default --fail-level WARNING`,
`makemigrations --check`, `lint-imports`, and the merged package's own tests. After the integration fixes I ran every
gate once: black, isort, flake8, `check --database default --fail-level WARNING` (on a freshly migrated database),
`makemigrations --check`, `lint-imports`, `spectacular --validate --fail-on-warn`, the view budget, the on-commit
enqueue check (f-fix #9), `bash -n` of the deploy scripts, and the full pytest suite. The agreements package is being
built in parallel and is not part of this wave.

## Merge conflicts, deviation numbers, requirements, migrations

* **DEVIATIONS.md.** site-inspections keeps DV-122 … DV-127 (it had renumbered them after wave 4a). projects and
  legacy-shim had both used numbers that were already taken, so their rows moved to the next free numbers (old → new):

  | Package | Old | New |
  |---|---|---|
  | projects | DV-104 | DV-128 |
  | projects | DV-105 | DV-129 |
  | legacy-shim | DV-122 | DV-130 |
  | legacy-shim | DV-123 | DV-131 |
  | legacy-shim | DV-124 | DV-132 |
  | legacy-shim | DV-125 | DV-133 |
  | legacy-shim | DV-126 | DV-134 |

  I updated every citation. For projects that means `docs/decisions/projects.md`, `projects/models/project.py` (help
  texts) and the unreleased `projects/migrations/0001_initial.py`. For legacy-shim it means
  `docs/decisions/legacy-shim.md`, `docs/decisions/bom.md` (B-1 → DV-133), `docs/migration/parity-report.md` and
  `tools/parity/approved.json`.
* **Other conflicts.** In `flarize/settings/base.py` I kept both sides' SPECTACULAR `ENUM_NAME_OVERRIDES`. In
  `customers/tests/test_timeline.py` I kept the integration side's subset check, which already includes
  `quotations.quotations`. That check is still a subset check, so the timeline providers of site_inspections and
  projects are accepted.
* **Requirements.** None of the three packages changed a requirements file.
* **Migrations.** site_inspections (`0001`, `0002`) and projects (`0001`) are new apps, so no merge migration was needed.
* **engines.** None of the packages added or duplicated an engines module.

## Integration wiring

1. **Engineering waiver identity (safety).** `engineering.services.runs` now owns the carry-over key:
   `finding_key(identity, message)` and `acknowledged_keys(subject_type, subject_uid)`. These replace
   `acknowledged_identities`, which matched on the identity alone. That identity is `<scope>|<rule>|<components>`. A
   finding with no components, such as `<pack>|PBC-K-001|`, shares its identity with every other finding of that
   rule, so one waiver used to cover all of them.
   * **Pack release gate** (`packs/services/releases.py`): a BLOCK finding now counts as waived only when its identity
     **and** its message were acknowledged.
   * **Project BOM lock** (`projects/services/bom_lock.py`): now uses the shared implementation instead of its private
     `(identity, message)` query.
   * **Tests, written first:**
     * `packs/tests/test_releases_api.py::…::test_a_waiver_covers_only_the_component_less_finding_it_reviewed`: before
       the fix, a waiver for a missing MAIN_INVERTER let a pack with a missing AC_ISOLATOR into the release (`READY`).
     * `engineering/tests/test_api.py::…::test_carried_acknowledgements_are_keyed_by_identity_and_message`.
   * The two existing assertions on `acknowledged_identities` now assert the same thing through `acknowledged_keys`.
   * `packs/tests/test_release_parity.py` (release 1 from the real Flarize data) still passes.
2. **`site_inspections.released` → projects.** The producer's payload was missing keys that the projects contract
   expects. site_inspections now also sends:
   * `size_kw`: `quoted_size_kw`, as a decimal string;
   * `phase`: `1P` or `3P`, and `null` for NC;
   * `lead_uid`: always `null`, because an inspection is not linked to a lead.

   projects now stores the payload's `quotation_version_uid` on the auto-created project. This is tested end to end
   with `PROJECTS_AUTO_CREATE_ON_RELEASE` switched on, in
   `site_inspections/tests/test_lifecycle.py::TestRelease::test_release_creates_the_project_with_the_documented_payload`.
3. **Legacy shim, quotation testimonials.** This was already wired at 9e3f7d1 and I verified it: `import_backend`, step
   `backend.quotations`, calls `quotations.services.legacy_import.import_backend_testimonials`. The shim's "to wire"
   table now records it as done, and the parity report's note has been updated.
4. **Legacy shim, `POST /api/verify-otp/` repeat number.** This is a small read of existing data, so I implemented it.
   The legacy view checked `SentQuote.objects.filter(phone=…).exists()` and then answered
   `{status: approved, message: "We already have your details! Our team will contact you soon."}` without a
   `quote_id`. The shim now gives that answer when the number has either:
   * an imported `sent_quotes` row (`quotations_email_log`, LEGACY_LINK, `to` = `+91…` or 10 digits), or
   * an earlier `/advanced-calculator` quote enquiry.

   The enquiry is still recorded as a lead, because the legacy `record_lead` ran first. A first request still answers
   `quote_id = QUOTE_<8 hex of the lead uid>`, because quotations has no write service for a website quote request.
   Tests are in `legacy/tests/test_forms.py`.
5. **Business default B-1.** Verified, with the new test
   `legacy/tests/test_computations.py::test_business_default_b1_public_and_legacy_quotes_omit_cost_breakdown_and_totals`.
   It checks that `POST /api/public/v1/bom/quote/` and `/legacy/bom/api/calculate/` both answer exactly
   `bom_lines, pricing, meta, available_offers`, and that no internal cost key appears at any depth. The recorded
   engine answers did carry those keys.

## Integration failure fixed

* **OpenAPI component clash.** Both quotations and projects had a `CancelSerializer`. The full suite's
  `core/tests/test_schema.py` runs `spectacular --api-version v1 --fail-on-warn`, and it warned about two
  `CancelRequest` components. I renamed the projects serializer to `ProjectCancelSerializer`, so its component is now
  `ProjectCancelRequest`. The endpoint and its body are unchanged.

## Left open (not integration wiring)

* The agreements package has not been merged. Until it is, these stay open:
  * the COST_CALCULATED/EXTRA_STRUCTURE agreement validator (`site_inspections.services.work.register_agreement_validator`);
  * the imported PA agreement uid (`uuid5(SI_AGREEMENT_NAMESPACE, 'PA:<id>')`);
  * the agreements side of the projects links.
* These business questions are listed in the integration report: project stubs without a customer, the `kseb_status`
  enum, the `customer_uid` scope, KSEB after close, approval-link expiry and closed-notice behaviour, placeholder
  engineer e-mails, customer-impacting flags on imported work, and `SITE_INSPECTIONS_APPROVAL_LINK_BASE`.
