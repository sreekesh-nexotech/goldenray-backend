# Wave-1 integration (merge of the ten wave-1 work packages)

Merged into `claude/bold-goodall-lxgwep` in this order, each with `git merge --no-ff` and every gate green before
each commit (black, isort, flake8, `check --fail-level WARNING`, `makemigrations --check`, `lint-imports`,
`spectacular --validate --fail-on-warn` with and without `--api-version v1`, view budget, on-commit enqueue check,
`bash -n` of the deploy scripts, the full pytest suite): engines-core, engines-commercial, engines-rules, engines-ops,
catalog, content-blog, content-pages, careers-reference, hr, leads-customers.

`wp/engines-rules` was merged at its tip `b410f05` ("… (reviewed)"), which holds the review fixes and
`test_rules_review.py`; the uncommitted files left in that package's worktree are an older, pre-review copy.

## Deviation numbers

Every package appended from DV-17. The first package merged keeps its numbers; later collisions were renumbered in
`docs/DEVIATIONS.md` and in the renumbered package's decisions file and code comments/help texts (the two help texts
that cite a DV number changed in the model and its initial migration alike, so `makemigrations --check` stays clean).

| Package | Branch numbers | Integrated numbers |
|---|---|---|
| engines-core | DV-21, DV-22 | unchanged |
| engines-commercial | DV-17 … DV-20, DV-25, DV-26 | unchanged |
| engines-rules | DV-17, DV-18 | DV-23, DV-24 |
| engines-ops | DV-17, DV-18 | DV-27, DV-28 |
| catalog | DV-17 … DV-21 | DV-29 … DV-33 |
| content-blog | DV-17 … DV-22 | DV-34 … DV-39 |
| content-pages | DV-17 … DV-20 | DV-40 … DV-43 |
| careers-reference | DV-17 … DV-21 | DV-44 … DV-48 |
| hr | DV-17 … DV-21 | DV-49 … DV-53 |
| leads-customers | DV-17 … DV-24 | DV-54 … DV-61 |

## Shared files

* `flarize/settings/base.py` `SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]` and `flarize/settings/test.py`: unions.
* `requirements/base.txt`: catalog's `jsonschema==4.26.0` (no other package changed requirements). No two packages
  added migrations to the same app, so no merge migration was needed.
* OpenAPI component names that collided once the apps shared one schema (`spectacular --fail-on-warn`,
  `core/tests/test_schema.py`): blog's category serializers are `BlogCategory*`; faqs' `CategoryRef` is
  `FaqCategoryRef`; catalog's profile Q&A item is `CatalogProfileFaq`; careers' `UserRef` (`{uid, name}`) is
  `CareersUserRef`; customers/leads' `UserRef` (`{uid, full_name, email}`) is `SalesUserRef`; leads' staff warranty
  row is `WarrantyRequest`. sitepages and careers reuse seo's `SeoIssueSerializer` (the same shape). Wire formats are
  unchanged.

## Engines consolidated

See `docs/decisions/engines-commercial.md` ("Merge notes") and `engines-rules.md` ("Consolidation after merge"):
`engines/_money_compat.py` became the binary64 section of `engines/money.py`; `battery_compat`'s master /
compatibility / protection functions run `engineering_checker`'s port; `_jscompat` takes white space, number literals
and array indexes from `jscompat` (removing the Unicode-digit defect from the commercial copy);
`run_package_checker` + `check_project_bom_js` reproduce the JavaScript verdict of all 54 approved packs. Kept apart on
purpose: the two `UNDEFINED` sentinels (results cross only as JSON-like data) and engines-core's `money.js_round` /
`js_text` (their own `-0` and number-text contract, pinned by the core goldens).

## Wiring between packages

| Hand-over item | Done |
|---|---|
| leads: office-aware pincode directory once reference is merged | `leads.services.pincode_directory.ReferenceOfficeDirectory` is the installed directory (per-office rows; no live pincode = no list); `installations/stats/` also depends on `reference:pincodes` |
| content-pages / careers: the SEO/blog package consumes their revalidation events | `blog/events.py` subscribes `sitepages.page_*`, `faqs.*` (decision 10) and `careers.position_*` (`path` + `previous_path`) |
| content-pages / careers: sitemap aggregation | discovered by convention; both providers now declare `SITEMAP_CACHE_NAMESPACES` (`sitepages` + `faqs`, `careers:positions`), so a publish refreshes the cached `sitemap/entries/` at once |
| content-pages: "build any SEO overview" | `sitepages`, `faqs` and `careers` ship `services/seo_overview.py` (kinds `page`, `faq`, `job`) beside blog's |
| engines-ops → hr: validate rule minutes at `attendance-rules/` | hr's own validation is stricter (720 / 1440); a contract test pins that every document hr accepts is valid for `engines.attendance.validate_rules_payload` and vice versa; hr's working-day rule is now the engine's `is_working_day` |
| engines-commercial ↔ engines-rules: the checker behind `run_package_checker` | parity test on the 54 approved packs |

Tests: `blog/tests/test_revalidation_website_apps.py`, `seo/tests/test_overview_and_sitemap.py`,
`hr/tests/test_engine_contract.py`, `leads/tests/test_public_installations.py`,
`engines/tests/test_integration_package_checker.py`, `engines/tests/test_commercial_units.py`.

## Not wired (belongs to packages not built yet, or needs a decision)

* catalog → website revalidation: catalog events carry `slug`/`category` but no site path, and the website has no
  per-product page (comparison pages only); the product → page map is a frontend decision.
* hr: attendance/devices providers and the `hr.attendance_inputs_changed` consumer (attendance, devices packages).
* content-pages / careers / leads: the `/legacy/` shims, `migrations_tools` import order, website rebinding.
* engines-core config keys for the pricing importer; engines-rules DomainError mapping and rule-set seeding
  (engineering, quotations, company packages).
