# Wave-2a integration (pricing-procurement, inventory)

Merged into `claude/bold-goodall-lxgwep` in this order, each with `git merge --no-ff` and the fast checks green after
each merge (`check --fail-level WARNING`, `makemigrations --check`, `lint-imports`, the merged package's tests), then
every gate once on the result (black, isort, flake8, `check --fail-level WARNING`, `makemigrations --check`,
`lint-imports`, `spectacular --validate --fail-on-warn` with and without `--api-version v1`, view budget, on-commit
enqueue check, `bash -n` of the deploy scripts, the full pytest suite): `wp/pricing-procurement` (tip `fe94c19`),
`wp/inventory` (tip `8c95508`).

## Deviation numbers

Both packages appended from DV-62. The first package merged keeps its numbers; inventory's rows were renumbered in
`docs/DEVIATIONS.md` and in `docs/decisions/inventory.md` (no code comment, help text or migration cites them).

| Package | Branch numbers | Integrated numbers |
|---|---|---|
| pricing-procurement | DV-62 … DV-70 | unchanged |
| inventory | DV-62 … DV-64 | DV-71 … DV-73 |

## Shared files

* `flarize/settings/base.py` `SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]`: union (pricing/procurement enums, the
  shared `TierEnum` and the pinned `TypeEnum`; inventory's `InventoryMovementDirectionEnum` /
  `InventoryMovementReasonEnum`). pricing's `CELERY_BEAT_SCHEDULE["pricing.expire_offers"]` and inventory's
  `INVENTORY_RECEIVING_LOCATION` merged cleanly.
* `core/dashboard.py` (`register(module, flag=…)`), `deploy/release.sh` (`ensure_inventory_append_only`),
  `.env.example`, `docs/ops/audit-log.md`: inventory only, merged cleanly.
* No requirements changed; no two packages added migrations to the same app (pricing, procurement and inventory each
  own theirs), so no merge migration; no engines module was duplicated.

## Wiring between packages

| Hand-over item | Done |
|---|---|
| inventory ↔ procurement: the `procurement.batch_committed` payload (procurement emitted `lines` as a count and no `committed_at` / `committed_by_uid` / `imported` / line objects, so with `INVENTORY_STOCK` on and a receiving location set every committed batch would have raised `ReceiptError` and been parked) | `procurement.services.allocation.committed_payload` emits inventory's contract (`batch_uid`, `number`, `committed_at`, `committed_by_uid`, `imported: false`, `lines: [{line_uid, component_uid, qty}]`) and keeps procurement's `supplier_uid` / `effective_from`. `imported` is always false: only `commit()` emits, the Flarize importer writes historic batches without an event. `procurement/tests/test_batches_api.py` asserts the new shape (`len(lines) == 3` where it asserted the count 3) |
| pricing → blog: `pricing.release_published` revalidates `/solar-comparison` and `/emi-calculator` (PLAN §3.5; "the blog package owns that handler") | `blog.events.FIXED_PATH_EVENTS`: events whose payload names no site path revalidate the fixed pages PLAN §3.5 lists for them |

Tests: `inventory/tests/test_procurement_contract.py` (a batch committed through the real procurement service is
booked line by line — quantity, location, `ref_uid` = line uid, `by` = committer, `at` = commit time; a reversal books
nothing; receiving off books nothing), `blog/tests/test_revalidation_website_apps.py::test_a_published_price_release_revalidates_the_priced_pages`.
Both fail on the merged packages without the wiring.

## Not wired (belongs to packages not built yet, or needs a decision)

* `procurement.batch_reversed` is not consumed by inventory: stock from a reversed batch stays booked until staff
  record a RETURN or ADJUST (a reversal may correct a price rather than return goods, and received stock may already
  have been issued). Whether inventory books reversals automatically is a business decision.
* The PriceRelease publish report does not list the PLAN §7 / D-8 W8 data blockers (`bt1` protection rating,
  PBC-M-003 controller); `docs/decisions/engines-rules.md` places them in the PackRelease report (packs package).
* hr's office delete guard does not count inventory locations (product master may not import hr, DV-73).
* `ISSUE_TO_PROJECT` references are only format-checked (projects package); pricing/procurement `legacy_import`
  functions are not yet called by `migrations_tools` (call order in `docs/decisions/pricing-procurement.md`).
* Packs (mark stale drafts on `pricing.release_published`), bom, quotations and calculators consumers of pricing:
  later packages.
