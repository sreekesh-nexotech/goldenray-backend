# WP packs-engineering — pack configuration, PackReleases, engineering checker runs

Work package *packs-engineering* builds the `packs` and `engineering` apps of PLAN §2.5 (`packs_*`, `engineering_*`),
§3.3 (`packs`), §3.4 (BOM & packs: `packs/*`; Engineering), §3.5 (`packs.release_published`) and the §7.4 import of
Flarize `pack-config.json` with the first releases. Legacy sources: Flarize `packConfig.js`, `packageApproval.js`,
`packageAuthoring.js`, `packageAuthority.js`, `packageProjection.js`, `server-pack-publish.js` and the pack routes of
`server.js` (workflows spec B.8, B.11, C.3, C.4; engines spec §8, §9, §11, §17). Deviations: DV-96 … DV-103.

## What exists

| Area | Where | Notes |
|---|---|---|
| Tables | `packs/models/`, `engineering/models/` | `packs_config_version`, `packs_config_pack`, `packs_config_line`, `packs_config_pin` (DV-98), `packs_release`, `packs_release_pack` *(no base, composite PK)*; `engineering_rule_set`, `engineering_run`, `engineering_finding`, `engineering_acknowledgement`. Every enum has a `CHECK`; lifecycle invariants are partial unique indexes: one open draft (DRAFT/SUBMITTED), one current version (APPROVED/PUBLISHED), one PUBLISHED release, one ACTIVE rule set per engine, one acknowledgement per finding, live pack keys / natural keys / pins per slot. |
| Seed | `engineering/migrations/0002_seed_rule_sets.py` | `phase1e.1` (35 PBC rules, `engines.engineering_checker.DEFAULT_RULE_SET`) and `phase1e.eng` (30 ENG rules), both ACTIVE. |
| Services | `packs/services/` | `catalog_adapter` (platform catalog → Flarize catalog shape), `context`, `engine` (glue to `bom_builder`, `pack_pricing`, `pack_config`, `engineering_checker`, `package_registry`), `mirror` (typed mirror), `versions` (lifecycle, pins, checker), `releases` (preview/publish/compare), `public`, `maintenance` (event reactions), `registrations`, `legacy_import`, `common`. |
| | `engineering/services/` | `rule_sets` (active, activate, parse), `runs` (record a run, acknowledge, carried acknowledgements). |
| Staff API | `packs/views/versions.py`, `releases.py`; `engineering/views/engineering.py` | table below. |
| Public API | `packs/views/public.py` | `GET packs/`, `GET packs/<system_type>/<tier>/<size_kw>/`. |
| Registrations | `packs/services/registrations.py` | catalog usage providers `packs.config_versions`, `packs.current_release`; the EMI `PACK_RELEASE` price provider; dashboard counters `packs`. |
| Events | `packs/events.py` | consumes `pricing.release_published`, `catalog.component_status_changed`, `catalog.component_deleted`. |
| Golden | `packs/tests/golden/generate_packs.mjs` → `flarize_packs.json` | the real Flarize JavaScript over the real data: BOM, FLAT price and checker verdict of all 90 packs. |
| Tests | `packs/tests/`, `engineering/tests/` | API (401/403/scope/validation/stale/happy/N+1), public (shape/no cost fields/cache/throttle), services, events, providers, legacy import, parity. |

### Endpoints

| Surface | Path | Permission |
|---|---|---|
| staff | `GET packs/config-versions/` (`status`, `search`, `ordering`), `GET …/<uid>/` (with `config`, `change_log`) | `packs.view`; the `costs` and `pricing` (margin) sections of `config` are `null` without `pricing_internal.view` (also in write responses) |
| staff | `POST packs/config-versions/` (`based_on_uid?`, `config?`, `note?`) — new DRAFT | `packs.edit` |
| staff | `PATCH packs/config-versions/<uid>/` (`config` or `sections`, `note`, `expected_version`) | `packs.edit` |
| staff | `GET …/<uid>/packs/` (`system_type`, `tier`; packs with pins and lines), `GET …/<uid>/packs/<key>/` | `packs.view` |
| staff | `PUT …/<uid>/packs/<key>/` (`pins: {slot: component uid}`, `expected_version`) | `packs.edit` |
| staff | `POST …/<uid>/run-checker/` → the stored engineering run | `engineering.verify` |
| staff | `POST …/<uid>/submit/` · `…/approve/` (`note`, `direct`) · `…/reject/` (`reason`) — all with `expected_version` | `packs.submit` · `packs.approve` · `packs.approve` |
| staff | `POST packs/releases/preview/` (publish report + readiness matrix), `POST packs/releases/` (`note`, `expected_current_number`) | `packs.view` · `packs.publish` |
| staff | `GET packs/releases/`, `…/<n>/`, `…/current/`, `…/<n>/packs/` (`system_type`, `tier`), `GET packs/compare/?a=&b=` | `packs.view`; landed cost, margin and the pricing `internal` block need `pricing_internal.view` |
| staff | `GET engineering/rule-sets/` (`engine`, `active`), `…/<uid>/`, `POST …/<uid>/activate/` | `engineering.view` · `engineering.approve` |
| staff | `GET engineering/runs/` (`subject_type`, `subject_uid`, `result`), `…/<uid>/` (with findings and acknowledgements) | `engineering.view` |
| staff | `POST engineering/findings/<uid>/acknowledge/` (`reason`, `expected_version`) | `engineering.approve` |
| public | `GET packs/` (`system_type`, `tier`, `phase`, `battery_config`, `future_ready`; paginated), `GET packs/<system_type>/<tier>/<size_kw>/` | anonymous, `public_read`, cached 300 s under `packs` (`Cache-Control` ≤ 60 s) |

Record scope: `packs` and `engineering` allow only `all` (PLAN §3.2), applied by the base views.

## Decisions not spelled out in the PLAN

1. **The configuration JSON is the authoring document; the typed tables are its mirror.** `config` holds exactly the
   ten `flarize.pack-config/1` sections (`engines.pack_config.validate_config`, 400 `invalid_config`). After every write
   the mirror enumerates the packs — template system types except `upgrade` × sizes × tiers × {standard, the
   future-ready pairs of the same phase (`futureUpgrade.pairs`)} × the hybrid template's battery configurations (0/1/2,
   each has its own market-rate key) — the enumeration of `server-pack-publish.js` — builds each BOM with
   `engines.bom_builder.build_bom` (as the Project Head) and writes `packs_config_pack` (resolved panel / inverter /
   battery, battery quantity, profile, market-rate key, flat-roof structure template, `pair_of` for future-ready packs)
   and `packs_config_line` (SLOT, MANUAL, FIXED, STRUCTURE). Packs are upserted by key (pins survive), packs the
   configuration no longer offers are soft-deleted, lines are derived rows and are replaced.
2. **Pins replace the Flarize package registry (DV-98).** `build_bom` resolves a slot default from the pack config
   (`defaults`), then from the registry package of (system type, system size, tier, phase), then nearest kW / first
   eligible item. In the real data 1,037 of the approved BOM lines come from the registry, so the platform keeps them as
   per-pack pins; each pack's pins are handed to the engine as a one-package registry for exactly that pack.
3. **Catalog in the Flarize shape** (`catalog_adapter`): items rebuilt from components, specs, attributes and tiers
   (`status` ACTIVE for ACTIVE/DEPRECATED — the engines select only ACTIVE), prices from the PriceRelease payload,
   package profiles from `bom_package_profile`, the battery-master overlay from `catalog_battery_spec`. Item order is
   the Flarize file order (legacy-map order, then `created_at`): the engines' "first eligible item" depends on it.
4. **Lifecycle** (DV-96): DRAFT → SUBMITTED → APPROVED (or `direct` approve of a DRAFT, recorded as submitted and
   approved by the approver — Flarize's Admin path) → PUBLISHED (first release built from it) → SUPERSEDED (another
   version approved); SUBMITTED → REJECTED (reason). Editing a SUBMITTED draft withdraws it (Flarize). Submit /
   direct approve refuse a draft identical to the current approved version (configuration and pins: 409 `no_changes`, Flarize `NO_CHANGES`).
   Error codes: `draft_exists`, `based_on_has_no_config`, `version_not_editable`, `version_not_draft`,
   `already_submitted`, `version_not_submitted`, `approve_conflict`, `pack_not_found`, `version_has_no_config`,
   `invalid_config`, `validation_error`, `stale_version`.
5. **Checker = Flarize's publish check.** `engines.package_registry.run_package_checker` on the package
   `componentsFromBom` derives from the BOM, template scope, the architecture from the profile's inverter type, with the
   ACTIVE rule set parsed from `engineering_rule_set.rules`. One `engineering_run` per checker pass over a version
   (`summary.scopes.<pack>` holds each pack's status, counts and deterministic key; findings carry `context.pack`).
6. **Acknowledgements carry over** (DV-101): a finding's `identity` is `pack|rule|component ids`; an acknowledgement on
   any run of a subject counts in later runs of it. Acknowledging a BLOCK finding is a waiver (`engineering.finding_waived`
   audit action) and lets the pack into the next release (`PACK_ENGINEERING_WAIVED` INFO).
7. **Release pricing** (DV-100): component prices and market rates from the current PriceRelease, the pack formula
   from the approved configuration; the released price is `price_pack` for the FLAT roof at 0 km (swaps, roof add-ons
   and transport are quotation-time extras; the full pricing result is kept in `packs_release_pack.pricing`).
   `landed_cost_total` = the pack-pricing reference cost with the material at landed cost where the PriceRelease has
   one (reference price otherwise); `gross_margin_pct` = (price excl. GST − landed) / price excl. GST (fraction).
8. **Publish report** (`releases.preview()`): BLOCK — `NO_APPROVED_VERSION`, `NO_PRICE_RELEASE`, `NO_ACTIVE_RULE_SET`,
   `NO_PUBLISHABLE_PACKS`, `RELEASE_UNCHANGED`; WARN (pack left out) — `MARKET_RATE_NOT_SET`,
   `PACK_COMPONENT_NOT_SELECTABLE` (`catalog.services.assert_selectable`), `PACK_ENGINEERING_BLOCKED` (the PBC BLOCK
   findings listed), `PACK_PRICING_BLOCKED`, `PACK_BUILD_REFUSED`, `PACK_ENGINEERING_UNAVAILABLE`; WARN
   `PIN_COMPONENT_NOT_SELECTABLE`, `CONFIG_GST_DIFFERS`; INFO `PACK_ENGINEERING_WAIVED`, `CONFIG_MARKET_RATES_DIFFER`.
   `matrix` is the readiness matrix (one row per pack: READY/EXCLUDED, reasons, price, engineering verdict). PLAN D-8's
   "PBC-M-003 controller flagged in the publish report" and the `bt1` rating blocker appear as
   `PACK_ENGINEERING_BLOCKED` items.
9. **Publish** (one transaction): stale guard on `expected_current_number`, the report re-built inside the transaction
   (409 `publish_conflict` when the version it was built from is no longer the approved one once locked),
   the checker run stored, previous release SUPERSEDED, version PUBLISHED, number from `core.sequences` (`PACK_RELEASE`),
   payload (canonical, without number and time) + SHA-256 + report stored, cache namespace `packs` bumped,
   `packs.release_published` `{release_uid, number, previous_number, config_version_number, price_release_number,
   packs, payload_sha256}` emitted (dedup per number).
10. **Public payload**: customer price incl./excl. GST, GST, and a BOM summary (category, name, qty) — never landed cost,
    margin, component unit prices or the pricing `internal` block. `size_kw` accepts `3`, `5.00` (both 5 kW packs) or
    a size key (`5sp`).
11. **EMI provider** (DV-103): one size tile per standard on-grid pack of the current PackRelease (`uid` = pack key,
    `system_cost` = customer price, `price_per_kw` derived), cache namespace `packs`; active with
    `EMI_PRICE_SOURCE=PACK_RELEASE`.
12. **Events**: `pricing.release_published` marks the open draft stale (a `change_log` entry, once per release number)
    and re-mirrors it; component status changes and deletions re-mirror the open draft. Writes emit
    `packs.config_draft_created|draft_updated|pins_updated|submitted|approved|rejected`, `packs.release_published`,
    `engineering.run_recorded|rule_set_activated|finding_acknowledged`.

## Legacy mapping

### `pack-config.json` → `packs_config_version` (`packs.services.legacy_import.import_flarize_pack_config`)

| Flarize | Platform |
|---|---|
| `history[]` entry (not the approved version) `version` | `number`, status SUPERSEDED, `config` NULL |
| `history[].approvedBy` / `approvedAt` / `note` / `changes` | `approved_by` (the imported user, else `legacy_actor`) / `approved_at` / `note` / `change_log` `[{…, "note": "Flarize history: N change-log entries", "legacy": true}]` |
| `approved.version` | `number`, status APPROVED (kept PUBLISHED/SUPERSEDED when the platform moved on) |
| `approved.config` | `config` (validated) |
| `approved.approvedBy/At`, `submittedBy/At`, `note` | `approved_by/at`, `submitted_by/at`, `note` |
| `draft.version`, `status` DRAFT/SUBMITTED, `basedOn` | `number`, `status`, `based_on` (through the legacy map) |
| `draft.config`, `changeLog[]` | `config`, `change_log` (verbatim) |
| `draft.submittedBy/At`, `rejectedBy/At`, `rejectionReason`, `updatedAt` | `submitted_*`, `rejected_*`, `rejection_reason`, `created_at`/`updated_at` |
| `schema` | implied (`engines.pack_config.PACK_CONFIG_SCHEMA`) |

Traceability: `core_legacy_map` `FLARIZE pack-config.json:versions <number>`. Violations: `number_taken`,
`invalid_config`, `invalid_value`, `target_deleted`, `target_moved_on`, `unknown_component`.

### `packages.proposed.json` → `packs_config_pin`

For every pack of an imported configuration the package `buildBom` reads (the first with the same `systemType`,
`size` = the pack's system size, `tier`, `phase`) gives one pin per component whose `role` is a variable slot of the
template: `componentId` (else `pinnedComponentId`) → `component` (by SKU), `derivedBy == "explicit"` → `authoritative`,
`approvedAlternates` → `alternates`. The registry's lifecycle fields (`packageState`, `approvalStatus`, revisions,
`lastValidation`, `blockers`, …) have no platform home: the typed packs of each version and the stored engineering runs
replace the registry (packages are not published into a registry any more; the PackRelease is the published record).

### Flarize functions → platform

| Flarize | Platform |
|---|---|
| `packConfig.updateDraftSection` / `updateDraftTemplate` | `PATCH packs/config-versions/<uid>/` (`sections`) |
| `submitDraft` / `approveDraft` / `approveDraftDirect` / `rejectDraft` | `submit/` / `approve/` / `approve/` with `direct` / `reject/` |
| `resetDraft` | `POST packs/config-versions/` (`based_on_uid`) after closing the draft |
| `describeStore` | `GET packs/config-versions/` + detail |
| `server-pack-publish.publishApprovedPacks` | `POST packs/releases/` (report: `published[]` → released packs, `blocked[]` → `PACK_ENGINEERING_BLOCKED`) |
| `packageApproval.runPackageChecker` | `POST packs/config-versions/<uid>/run-checker/` |
| `packageApproval.approve/reject/reset/archive/…` (registry lifecycle) | not ported: pins on the version + the version lifecycle |
| `GET /api/packages/compare` | `GET packs/compare/?a=&b=` (releases) |
| `approvedTemplate.js` (renderer pin) | quotations package (document rendering), not packs |

## Parity evidence

* `node packs/tests/golden/generate_packs.mjs` (`FLARIZE_ROOT`, default `/home/user/flarize-main/flarize`) runs the real
  `buildBom`, `pricePack` (FLAT, 0 km) and `runPackageChecker` (on `componentsFromBom`) for all **90 packs** of the
  approved configuration v16 and records the SHA-256 of every source; it refuses data other than the
  `engines/tests/golden/fixtures/flarize_commercial.json` fixture the Python side imports.
  `test_golden_capture_is_reproducible` re-runs it and requires a byte-identical file.
* `packs/tests/test_release_parity.py` imports the Flarize catalog, battery master, prices, pricing sections, cost
  configuration, BOM configuration and pack configuration (+ registry pins) through the platform importers and calls
  `publish_initial_releases()` (PriceRelease #1 and PackRelease #1 through the services). Result:
  * the released packs are exactly the **11** Flarize could sell (priced COMPLETE, not BLOCKED): on-grid base/value/
    premium 3 / 5sp / 5tp and the two 3 → 5sp future-ready packs; every price (incl./excl. GST, GST), BOM line (SKU,
    name, category, qty, unit price, amount, GST %, GST amount), structure line and the pricing `internal` block equal
    the JavaScript's;
  * the other 79 are reported with Flarize's reasons: `MARKET_RATE_NOT_SET` (e.g. every hybrid pack but value/2/3), and
    `PACK_ENGINEERING_BLOCKED` with exactly the JavaScript's BLOCK findings (PBC-H-002 on `bt1`: the known battery
    protection-rating data blocker); the report has no BLOCK item;
  * the stored engineering run holds, for every pack, the JavaScript status, deterministic key and findings;
  * the typed mirror of v16 holds the same components and quantities as the JavaScript BOM for all 90 packs; the draft
    v17 and the 15 history versions are imported.
  * The approved configuration's market rates replace the imported `catalog.json` cells they differ from
    (`pack_config_market_rate_wins`, e.g. `ongrid_value/3` 229 000 over 228 000): Flarize priced with the pack
    configuration's.

## For later packages

* **quotations**: read the current release (`packs.services.releases.current_release()`, `release_packs(release)`); a
  pack's `pricing` holds the base `price_pack` result; recompute roof/transport/swap extras with
  `engines.pack_pricing.price_pack(config=engine.pricing_config(version.config, price_release.payload["market_rates_by_key"]), …)`
  on the release's config version; listen to `packs.release_published` to warn open drafts; set
  `packs_release.content_release_uid` once `quotations_content_version` exists.
* **calculators**: `packs.services.public.public_queryset()` is the published pack set; the EMI provider is registered.
* **migrations_tools**: after the catalog, pricing (`import_prices`, `import_flarize_pricing`,
  `import_flarize_documents`) and bom (`import_flarize_bom`) imports, and the users import (so `admin-001` resolves),
  call `import_flarize_pack_config(pack_config_json, packages_proposed_json)` then `publish_initial_releases()`; print
  the returned report (PLAN §7.6 #8).
* **projects / quotations drafts**: record their checker runs with `engineering.services.runs.record_run(subject_type=
  "PROJECT_BOM" | "QUOTATION_DRAFT", checks=[("", result)])`; import historic verdicts with
  `engines.engineering_checker.CheckResult.from_legacy`.

## Shared changes

* `flarize/settings/base.py` `SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]`: `PackConfigStatusEnum`,
  `PackLineSourceEnum`, `EngineeringEngineEnum`, `EngineeringSubjectTypeEnum`, `EngineeringRunResultEnum`,
  `EngineeringSeverityEnum` (names only; no existing enum renamed).
