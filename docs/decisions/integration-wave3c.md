# Wave-3c integration (packs-engineering)

`wp/packs-engineering` (tip `7ab2635`) was merged into `claude/bold-goodall-lxgwep` with `git merge --no-ff`. The
package had already merged the integration branch after attendance (`fa3a3b9`), so the merge had no conflicts. After
the merge I ran the fast checks: `check --database default --fail-level WARNING`, `makemigrations --check`,
`lint-imports` and the packs/engineering/emi tests. I then ran every gate once on the result: black, isort, flake8,
`check --database default --fail-level WARNING`, `makemigrations --check`, `lint-imports`,
`spectacular --validate --fail-on-warn`, the view budget, the on-commit enqueue check (f-fix #9), `bash -n` of the
deploy scripts, and the full pytest suite.

## Deviation numbers, requirements, migrations

* packs renumbered its rows DV-96 … DV-103 before the merge (attendance holds DV-91 … DV-95). No number is duplicated.
* No requirements file changed. packs and engineering are the only apps that got migrations, so no merge migration was
  needed. No engines module is duplicated.

## Fresh database (the database-check gate the package predates)

I created a brand-new empty database (`flarize_merge_w3c`) and ran a full `migrate` on it. It applies cleanly, and
`check --database default --fail-level WARNING` then reports no issues.

* **Index names:** none of the new packs/engineering index names is longer than 30 characters.
* **Explicit constraint names:** the longest is 43 characters (`engineering_acknowledgement_one_per_finding`).
* **63-character names in `pg_class`/`pg_constraint`:** the only ones are Django's own hash-suffixed FK and unique
  names, which Django caps at 63 by construction. Nothing is truncated.

No identifier needed fixing.

## EMI `PACK_RELEASE` wiring (DV-103)

**Before:** packs registered the EMI price provider from `PacksConfig.ready` with
`importlib.import_module("emi.services.price_sources")` and a duck-typed copy of `SizeOption`. That hid a
configuration → content dependency from import-linter, the direction the contract `product-master-ignores-content-and-hr`
forbids. The packs tests reached `emi` the same way.

**After:** the dependency points the way PLAN §1.2 documents ("calculators read releases": content reads
configuration). It is a static import, so import-linter checks it.

* **The read stays in packs.** `packs.services.public.emi_size_packs()` is the packs-owned read: the standard, not
  future-ready, on-grid packs of the current release, smallest first.
* **EMI builds the tiles.** `emi.services.pack_release.release_sizes()` turns those packs into
  `price_sources.SizeOption` tiles: `uid` is the pack key, `system_cost` is the customer price incl. GST, and
  `price_per_kw` is derived from it. Cache namespace: `packs`. `EmiConfig.ready()` installs it through the unchanged
  `price_sources.register`, so the registry stays the seam fake providers use in tests. `emi.W001` still fires if
  something removes it.
* **packs no longer knows EMI.** `registrations.py` lost the `importlib` call and `PackSizeOption`, and the packs tests
  lost their `importlib` import.
* **The contract is unchanged.** packs → emi stays forbidden and emi → packs is allowed.
* **Tests.**
  * `emi/tests/test_pack_release.py` publishes a release and proves that `EMI_PRICE_SOURCE=PACK_RELEASE` returns its
    price, at the service level and through `GET calculators/emi/config/` and `POST calculators/emi/`: `ongrid-value-3`
    at 229,000.00, 76,333.33 per kW.
  * Under `MANUAL` the release is ignored.
  * The startup provider is the release reader.
  * A grimp graph check confirms that `emi.services.pack_release` imports `packs.services.public` and that no chain
    runs from packs to emi.
  * The packs service test that used to cover the provider now covers `emi_size_packs()` directly. The end-to-end
    assertion moved to emi, since packs tests may not import emi.
  * `emi/tests/conftest.py` puts the startup provider back after each emi test, because those tests reset or replace
    it.

## PLAN §7.3 report

`docs/reports/emi-vs-packrelease.md` compares the legacy `emi_system_size` prices with PackRelease #1 from the parity
data.

## Open business questions carried forward

* The pack-config market rate wins over `catalog.json`, e.g. `ongrid_value/3` becomes 229,000 instead of 228,000. The
  business needs to confirm this.
* The staff `pricing` block shows installation and structure figures to `packs.view`. Should it be tightened?
* EMI under `PACK_RELEASE` raises three questions:
  * there are no 8/10 kW packs in PackRelease #1;
  * the calculator would show several tiers per size instead of one tile;
  * there is no monthly-bill reference.

  See the report.
* `packs_release.content_release_uid` waits for quotations' content-version table.
* packs does not react to `catalog.component_updated`.
