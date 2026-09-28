# engines-rules — engineering rules, BOM domain, generation gate, quotation payload, content fit

Work package engines-rules ports the Flarize rule and document engines to pure Python (PLAN §1.5 `engineering_checker`,
`gate`; §2.5 `engineering_*`; §2.6 `quotations_version.gate_report` / `document_payload`, `quotations_bom_snapshot`,
`quotations_commercial_snapshot`, `quotations_content_version.fit_report`; engines spec §11, §12, §15, §19–§21;
workflows spec C.1, C.5–C.7, C.12, F.2, F.3, H.2). No Django, no app import (import-linter `engines-pure` plus
`engines/tests/test_rules_purity.py`). Deviations: DV-17, DV-18.

The package owns no table and no endpoint, so there is no `legacy_import.py`: the legacy *data* these engines produce
or read (rule register, stored verdicts, snapshots, frozen documents, content and branding stores) is mapped below for
the packages that own the tables (engineering, quotations, company).

## What exists

| Module | JavaScript source | Public API |
|---|---|---|
| `engines/engineering_checker.py` | `engineeringChecker.js` (`RULES_VERSION 'phase1e.1'`), `engineeringValidation.js`, `scripts/validate-bom.mjs validateBatteryConsistency`, `batteryMaster.js toBatteryMaster`, `batteryCompatibility.js`, `upgradeModel.js` | rule sets as data: `Rule`, `RuleSet` (`as_json`, `from_json`, `with_changes`), `RuleSetError`, `DEFAULT_RULE_SET` (35 PBC rules), `VALIDATION_RULE_SET` (30 ENG rules), `CATEGORY`; `check_project_bom` → `CheckResult` (`Finding`, `Counts`, `result`, `summary()`, `as_dict()`, `from_legacy`), `check_project_bom_js` (JavaScript calling convention), `registry_lines`; `validate_engineering` → `ValidationResult`, `validate_battery_consistency`; `to_battery_master`, `check_battery_compatibility`, `resolve_protection_requirement`; `APPROVED_UPGRADE_PATHS`, `build_upgrade_identity`, `upgrade_identity_key`, `check_upgrade_section_policy`; vocabulary `Severity` (BLOCK/WARN/INFO), `CheckStatus` (VALID/WARNING/BLOCKED), `RunResult` (PASS/WARN/FAIL) |
| `engines/bom_domain.py` | `bomRoles.js`, `componentIdentity.js`, `catalogLifecycle.js`, `projectBom.js`, `bomSnapshot.js`, `bomLock.js`, `bomStatus.js` | `Role`, `CATEGORY_TO_ROLE`, `ENPHASE_COMPONENT_ROLES`, `role_for_line`; `to_component`, `all_components`, `find_by_component_id`, `is_selectable`, `find_duplicate_identities`; `classify_catalog_item`, `classify_catalog`, `find_exclusion_gaps`; `create_project_bom`, `set_component`, `set_quantity`, `remove_role`, `effective_line(s)`, `diff_against_package`, `BomError`; `create_project_bom_snapshot`, `apply_procurement_price_change`, `price_for_new_quotation`, `diff_snapshot_against_current_prices`, `revise_snapshot`; `attempt_lock` → `LockOutcome`, `warnings_requiring_acknowledgement`, `lock_summary`; `BomStatus`, `quotation_gate`, `can_transition`, … |
| `engines/gate.py` | `quotationPayload.evaluateGenerationGate`, `quotationWorkspace.deriveSubsidyTreatment` / `resolveInputs` | `evaluate_generation_gate` → `GateReport` (`as_dict()` = `gate_report`, `raise_if_blocked()` → `GenerationBlocked`), `GateCheck` (8), `derive_subsidy_treatment`, `resolve_inputs` |
| `engines/quotation_payload.py` | `quotationPayload.js`, `quotationWorkspace.js` (pure parts), `commercialSnapshot.js`, `commercialFreeze.js`, `quotationPolicy.js` | `build_quotation_payload` (+ `_from` for the JavaScript argument object), `component_attributes_for`, `resolve_panel_dcr_type`, `panel_unresolved_subsidy_result`, `alternative_inputs`, `alternative_entry`, `with_alternatives`, `project_issued_payload_for_actor`, `project_issued_snapshot_for_actor`, `verify_immutability`, `normalize_renderer_pinning`, `issued_document`; `VERSION_KEYS` (13), `create_commercial_snapshot`, `create_pack_commercial_snapshot`, `apply_config_change`, `compare_recalculation`, `supersede`; `build_commercial_snapshot` (`FreezeError`); `resolve_effective_validity`, `resolve_active_policy`, `validate_policy_record`, `publish_policy`, `set_policy_status`, `set_default_validity_days`, `describe_policy_store` (`PolicyError`) |
| `engines/content_fit.py` | `quotationContent.js`, `quotationBranding.js` | `LIMITS`, `validate_content`, `fit_summary`, `ContentInvalid`; `pick`, `normalise_language`, `label`, `profile_key_for_kw`, `materialise_rows`, `total_units`, `default_rows_for_kw`, `daily_generation_for_kw`; store lifecycle `create_empty_store`, `save_draft`, `publish_draft`, `discard_draft`, `describe_store`; branding `freeze_branding_snapshot`, `demo_branding_kinds`, `list_accounts`, `kind_contains_demo`, `current_version_id`, `get_version`, `publish_branding_version`, `set_branding_account_status`, `set_primary_branding_account`, `describe_branding_store` (`BrandingError`) |
| `engines/jscompat.py` | — | JavaScript value semantics used by all five: `UNDEFINED`, `js_number` (`Number()`), `js_string` (`String()`/template literals), `js_truthy`, `js_keys` (`Object.keys` order), `js_round` (`Math.round`), `round_places`, `to_fixed`, `number_text`, `utf16_len`, `js_trim`, `parse_iso_ms`/`iso_from_ms` (`Date`), `format_en_in` (`toLocaleString('en-IN')`) |
| `engines/frozen.py` | `deepFreeze`, `JSON.parse(JSON.stringify(x))` | `FrozenDict`, `deep_freeze`, `is_frozen`, `thaw`, `jsonable`, `to_json`, `sha256_hex` |
| `engines/tests/golden/generate_rules.mjs` + `rules_*.json` | — | the capture (below) |
| `engines/tests/test_rules_{checker,bom,gate,payload,content}_parity.py`, `test_rules_unit.py`, `test_rules_purity.py`, `rules_golden.py` | — | 1,1xx engines-rules tests; 99 % statement/branch coverage of the seven modules |

## Decisions not spelled out in the PLAN

1. **Rules as data, logic as code.** A `RuleSet` holds, per rule: code, category, severity, description, inputs,
   source, `requiresAcknowledgement` (the six `bomLock.WARNINGS_REQUIRING_ACKNOWLEDGEMENT`) and typed `params` — the
   15 °C Kerala minimum cell temperature and 25 °C STC (D-002), the D11 isolator per phase (P-001), the required roles
   per architecture and the template-scope exemption (K-001), the Enphase-only roles (A-002), the required panel fields
   (C-001), the phase-agnostic marker `HYB` (J-001, O-001), the prohibited `DC_ISOLATOR` role (P-002), the V1 battery
   count (M-002), the system types needing an IQ System Controller (M-003) and the approved upgrade paths (R-001/R-002).
   `DEFAULT_RULE_SET.as_json()` is the `engineering_rule_set.rules` document for version `phase1e.1`;
   `RuleSet.from_json` validates a stored one (every code exactly once, known severities, the rule's category, typed
   parameters; missing members take the register's defaults, so adding a parameter later keeps old rule sets valid)
   and `RuleSetError.errors` lists every problem as `{path, message}`. A new version is `with_changes(version=…,
   severities=…, params=…)`. Severity words are the PLAN's (`BLOCK`/`WARN`/`INFO`); the JavaScript's
   `BLOCKED`/`WARNING` are accepted on input and emitted by `as_dict()`. The ENG register (`engineeringValidation.js`)
   is a second rule set (`engine: engineeringValidation`, version `phase1e.eng` — the JavaScript has no version);
   its four declared-only rules (PNL-002, STR-001, DEV-001, SYS-004) are never emitted, as in the JavaScript.
2. **Three vocabularies, one mapping.** A run's verdict is `CheckStatus` VALID/WARNING/BLOCKED (the quotation payload,
   gate and BOM snapshot contract — D-3 keeps the Flarize payload) and `RunResult` PASS/WARN/FAIL
   (`engineering_run.result`); `Finding.as_row()` is the `engineering_finding` row (`rule_code`, `severity`,
   `message`, `context` = category, component ids, reason, source, inputs). `CheckResult.summary()` is
   `engineering_run.summary` (counts, rules version, catalogue version, deterministic key).
3. **Deep-frozen documents.** Every document the engines build (payload, snapshots, freeze, lock record, stores,
   reports) is `deep_freeze`d: `FrozenDict` (a `dict` whose mutators raise `TypeError`, as a frozen object throws in
   strict-mode JavaScript) and tuples, copied from the inputs so no live reference to master data survives. `thaw` /
   `jsonable` give plain data for a JSONB column; `sha256_hex` is `document_payload_sha256` (canonical JSON: sorted
   keys, compact, JavaScript number text). `apply_procurement_price_change` returns a LOCKED snapshot *as the same
   object*, `apply_config_change` returns the issued snapshot untouched.
4. **JavaScript semantics where they show.** Inputs are the legacy JSON shapes; a missing member is `UNDEFINED`
   (dropped like `JSON.stringify` drops it), `None` is `null`; messages print values the JavaScript way
   (`null`, `undefined`, `1500`, `[object Object]`); `Object.keys` order (array-index keys first) is kept wherever
   the JavaScript iterates an object (error order in the fit guard, size lists in ENG-SYS-001); `.length` is UTF-16
   code units (a character outside the BMP counts twice in the fit guard); `String.prototype.trim` white space;
   regular expressions are ASCII-case-insensitive and end-anchored like JavaScript (`\Z`, not Python's `$`);
   digits are ASCII only (`Number('൩')`, `Number('３')` are `NaN`, a date in Malayalam or full-width digits is not
   ISO-8601), and `Number()` leaves the binary64 range the JavaScript way (`'1e400'` → `Infinity`, `'1e-400'` → `0`),
   so a quantity written that way is still refused by PBC-L-001 and the fit guard; a `NaN` never raises in a
   comparison (JavaScript compares it false, a `Decimal` NaN ordering comparison would raise).
5. **Decimal only; exact where binary64 shows (DV-18).** Every number is `Decimal`/`int` (a JSON float is read back as
   the decimal its text meant). The display figures the payload computes so the renderer does not (appliance
   units/day, surplus, KSEB refund `round(0.8 × 1000 × kW)`, cold Voc `toFixed(1)`) use `Math.round`/`toFixed`
   semantics on the exact value. Two JavaScript results are binary64 artefacts the platform does not reproduce: the
   freeze's `targetMarginPct = targetGrossMargin × 100` (0.07 → JavaScript `7.000000000000001`, Python `7.00`) and
   `compare_recalculation`'s deltas; the golden case is pinned as a *representation* divergence
   (`|js − py| ≤ |py|·10⁻¹²`, `test_the_pinned_margin_divergence_is_the_exact_percentage`).
6. **No clock, strict dates.** `at`/`issuedAt` always come from the caller (the content store's JavaScript defaulted
   to `new Date()`). Dates are ISO-8601 only (`parse_iso_ms`): a date is UTC midnight, a date-time without offset is
   read as UTC (the legacy server ran in UTC); `null`, numbers and free text (`'September 1, 2026'`) are
   `VALIDITY_INVALID`, where `new Date(null)` silently meant 1970.
7. **Pure stores.** `save_draft`, `publish_draft`, `discard_draft` and the branding/policy writers return a new store
   instead of mutating the one given. The branding and policy writers keep every data rule (immutable versions,
   accounts never deleted, one primary, append-only policies, duplicate refusal) but not the legacy
   `ADMIN`/`PROJECT_HEAD` role check: authorisation is the accounts registry's job before the engine is called
   (`actor_id` is recorded as `publishedBy`/`createdBy`).
8. **The gate refuses in one place.** `evaluate_generation_gate` reports all eight checks with their reasons (a blocked
   gate still lets the operator see the draft payload); `GateReport.raise_if_blocked()` is the issue path's hard
   refusal (`GenerationBlocked`, code `GENERATION_BLOCKED`, `failed_checks`, `reasons`) — the quotations service maps
   it to a 409/422 `DomainError` before writing anything. `derive_subsidy_treatment`: an explicit `NOT_QUOTED` wins,
   an engine result (eligible or not) gives `ENGINE_RESULT`, else the declared value (default `NOT_QUOTED`).
9. **Lock follows the rule set.** `attempt_lock` always runs a fresh checker pass (never a cached verdict) with the
   same rule set, so the acknowledgement list is the rule set's `requiresAcknowledgement` rules; the lock record pins
   the rule set version (`rulesVersion`). `attempt_lock` imports the checker at call time (the checker imports this
   module's role taxonomy).
10. **Undefined numbers stay JSON.** An upgrade identity of an unparsable size stores `null` where the JavaScript kept
    `NaN` (unrepresentable in JSON); its key is computed from the stored identity (`UNAPPROVED-NaN-5||5||value`).
11. **Package checks, the JavaScript way.** `check_project_bom_js(arguments)` takes the JavaScript argument object and
    returns the JavaScript result object, which is what `engines.package_registry.run_package_checker`
    (engines-commercial) injects; `CheckResult.from_legacy` parses a stored verdict (`lastValidation` of the 290
    workspace projects and 645 registry packages) for the engineering importer.

## Legacy → platform mapping (for the owning packages)

**Rule register → `engineering_rule_set`** (seed version `phase1e.1`, `active=true`):

| Legacy (`CHECKER_RULES[]`) | `rules` document (`DEFAULT_RULE_SET.as_json()["rules"][]`) |
|---|---|
| `id` | `code` |
| `category` (CATEGORY A–S) | `category` |
| `severity` BLOCKED / WARNING / INFO | `severity` BLOCK / WARN / INFO |
| `description`, `inputs`, `source` | same |
| membership of `bomLock.WARNINGS_REQUIRING_ACKNOWLEDGEMENT` | `requiresAcknowledgement` |
| literals inside `checkProjectBom` (15 °C, 25 °C, `is1`/`is2`, required roles, `HYB`, `DC_ISOLATOR`, 1 battery, `hybrid`) and `upgradeModel.APPROVED_UPGRADE_PATHS` | `params` (typed per rule) |
| `RULES_VERSION` | `engineering_rule_set.version` and the document's `version` |

**Stored verdicts → `engineering_run` / `engineering_finding`** (`CheckResult.from_legacy(lastValidation)`):

| Legacy | Platform |
|---|---|
| `status` VALID / WARNING / BLOCKED | `engineering_run.result` PASS / WARN / FAIL (`CheckResult.result`) |
| `counts`, `rulesVersion`, `catalogVersion`, `deterministicKey` | `engineering_run.summary` (`summary()`) |
| subject (project BOM / package / quotation draft) | `subject_type` PROJECT_BOM / PACK_CONFIG_VERSION / QUOTATION_DRAFT, `subject_uid` |
| `findings[]` `ruleId`, `severity`, `message`, `componentIds`, `reason`, `source`, `inputs`, `category` | `engineering_finding.rule_code`, `severity`, `message`, `context` (`Finding.as_row()`) |
| `acknowledgements[]` `{ruleId, by|acknowledgedBy, at, note|reason}` | `engineering_acknowledgement` (`finding_id`, `acknowledged_by`, `reason`, `at`) |

**Quotation store (`quotation-state.json`) → quotations** (PLAN §2.6, §7.4):

| Legacy | Platform | Engine |
|---|---|---|
| `bomSnapshots[id]` (lock record: `lines`, `acknowledgements`, `rulesVersion`, `validationStatus`) | `quotations_bom_snapshot.lines`, `.lock_acknowledgements` | `create_project_bom_snapshot`, `attempt_lock` |
| `commercialSnapshots[id]` `cost` / `pricing` / `versions` (13) / `pack` / `offer` | `quotations_commercial_snapshot.cost_lines` (`cost`, `pricing`, `pack`, `offer`), `.pins` (`versions`), `.margin_check` (`cost.landedCostCheck`, reference margin) | `create_pack_commercial_snapshot`, `create_commercial_snapshot` |
| `documents[qid][n].payload` | `quotations_version.document_payload` (+ `document_payload_sha256` = `sha256_hex`) | `build_quotation_payload` (+ alternatives) |
| `payload.generationGate` | `quotations_version.gate_report` | `evaluate_generation_gate(...).as_dict()` |
| `documents[qid][n].snapshot` | frozen with the version (inside `document_payload`'s document or beside it) | `build_commercial_snapshot` |
| `documents[qid][n].rendererPinning`, `cmsPageVersions`, `issuedBy/At`, `identityModel` | `quotations_version.issued_at/by`; pin kept in the document | `normalize_renderer_pinning`, `issued_document` |
| `quotation-content.json` `published`/`draft` | `quotations_content_version.language_payload` (`{en,ml}` content), `.fit_report` (`fit_summary`) | `validate_content`, store lifecycle |
| `quotation-policy.json` | validity at issue (`quotations_quotation.valid_until`) | `resolve_effective_validity` |
| `quotation-branding-state.json` | superseded by `company_bank_account` (D-9); the freeze keeps its `branding` domain for replay | `freeze_branding_snapshot` |

## Parity evidence

`TZ=UTC /opt/node22/bin/node engines/tests/golden/generate_rules.mjs` (`FLARIZE_ROOT`, default
`/home/user/flarize-main/flarize`; `GOLDEN_OUT` to write elsewhere) imports the real modules and data, masks personal
data (customer name, phone, e-mail, address, pincode, salutation; the salesperson's display name; bank account numbers)
**before** running them, and writes 798 cases (large shared values once, under `refs`):

| File | Sections (cases) |
|---|---|
| `rules_catalog.json` | the approved catalogue (catalog.json + approved pack-config v16 overlay; cost fields and change logs stripped — the generator proves the stripped catalogue gives identical verdicts) and `battery-master.json` |
| `rules_checker.json` | `pbc` 94 (every one of the 35 PBC rules with ≥ 1 failing and ≥ 1 passing fixture, checked by the generator), `packs` 54 (**every approved pack**: 2 system types × sizes × 3 tiers + the future-ready pairs, template scope, lines exactly as `packageApproval.runPackageChecker` builds them — the generator cross-checks the registry wiring; 12 VALID, 36 WARNING, 6 BLOCKED by PBC-H-002 = the known `bt1` rating blocker), `battery` 49, `upgrade` 19, `lock` 10 (every `attemptLock` outcome), `lockSummary` 4, `validation` 58 (every ENG rule failing and passing; declared-only rules never fire), `consistency` 7 |
| `rules_bom.json` | `roles` 13, `identity` 90, `lifecycle` 16 (incl. the whole catalogue classified), `projectBom` 5 (operation sequences with every error), `snapshot` 10, `status` 61 (every status × every transition) |
| `rules_gate.json` | `gate` 21 (all pass, all fail, **each of the 8 checks failing alone**, boundary values), `treatment` 7, `inputs` 3 |
| `rules_payload.json` | `replay` 5 representative issued documents (latest with bilingual content and two alternatives; 5 kW single phase; Enphase premium primary; one alternative; subsidy not quoted), `payload` 15 synthetic (every blocked section, no COST_VIEW, discounts and customer-side expenses, appliance-row edges, refused commercial attributes, testimonials/campaign/tier/inclusion shapes), `projection` 24, `commercialSnapshot` 17 (gross margin, pack priced by the real `pricePack` on SHEET/FLAT, errors, the real recalculation path's comparison), `freeze` 15 (every branch incl. both production guards), `policy` 47 |
| `rules_content.json` | `fit` 31 (the published content fits; en and ml limits at and one over the boundary; UTF-16 counting; every list and appliance rule), `helpers` 53, `store` 9, `branding` 60 |

**Frozen documents.** The generator replays **all 64 issued documents** of `quotation-state.json` with the current
JavaScript (`header.replaySummary`): payloads — 2 identical, 51 identical apart from members the assembler gained
after they were issued (`content`, `tierColumnNames`, `inclusionsByTier`), 11 differing only in the wording of the
empty-testimonials reason (changed after issue); commercial snapshots — 120 identical, 8 identical apart from the
later `extras[].detail`; commercial freeze 63/63 identical; renderer pin 63/63 identical. The five representative
documents are in the golden file with their reconstructed inputs; `test_rules_payload_parity.py` requires the Python
payload (primary + every alternative), every pack commercial snapshot, the freeze, the pin and the assembled document
to equal the JavaScript exactly. `test_golden_capture_is_reproducible` re-runs the generator (when node and the
sources are present) and requires byte-identical files; each header records the sha256 of every JavaScript source and
data file used.

## Hand-over notes

* **engineering** (tables): seed `engineering_rule_set` with `DEFAULT_RULE_SET.as_json()` (version `phase1e.1`);
  load the active one with `RuleSet.from_json(row.rules)` and pass it as `rule_set=`; store a run with
  `result.result`, `result.summary()` and one `Finding.as_row()` per finding. Import historic verdicts with
  `CheckResult.from_legacy`.
* **packs**: publish-time checks are `check_project_bom(bom={phase, architecture, sysType}, lines=registry_lines(...),
  template_scope=True, …)` (or inject `check_project_bom_js` into `engines.package_registry.run_package_checker`);
  D-8 "PBC-M-003 controller flagged in the publish report" is the checker's BLOCKED finding listed there.
* **quotations**: build inputs with `gate.resolve_inputs` / `alternative_inputs`, the subsidy with
  `resolve_panel_dcr_type` (+ `panel_unresolved_subsidy_result`), evaluate the gate and `raise_if_blocked()` before
  any write, build the payload with `capabilities=["COST_VIEW"]`, assemble alternatives with `alternative_entry` /
  `with_alternatives`, freeze with `build_commercial_snapshot`, and store `jsonable(payload)` +
  `sha256_hex(payload)`. Readers without `quotations` cost access get `project_issued_payload_for_actor(payload,
  False)`. Map `GenerationBlocked`, `FreezeError`, `PolicyError`, `SnapshotError`, `ContentInvalid`,
  `BrandingError`, `BomError` to `DomainError` codes in the service.
* **Consolidation after merge**: engines-commercial ports the same `batteryCompatibility.js`/`batteryMaster.js`
  (`engines.battery_compat`) and has its own `_jscompat.py`; engines-core has `money.js_round`/`js_text`. The golden
  files of all three pin the same JavaScript, so the integration can make one delegate to the other without losing
  parity evidence.

## Adversarial review (engines-rules RV)

Beyond re-running every golden comparison (the regenerated files are byte-identical), the review ran a differential
fuzzer against the real JavaScript: ~9,000 random `checkProjectBom` calls, ~6,000 fit-guard mutations of the published
content, ~2,300 payload mutations of the replayed and synthetic inputs, ~1,800 commercial snapshot / freeze mutations,
~4,000 policy calls, ~13,000 BOM-domain calls and ~1,600 ENG-validation / battery calls. Outside JavaScript crashes on
malformed input and the DV-18 representations, it found the defects below; each has a failing-first regression test in
`engines/tests/test_rules_review.py`.

| # | Defect | Fix |
|---|---|---|
| RV-1 | `js_number` read Unicode digits (`'൩'`, `'٣'`, `'３'`) as numbers and `parse_iso_ms` read them as dates, where JavaScript gives `NaN` / an invalid date: a BOM quantity typed in Malayalam digits skipped PBC-L-001 (fail-open on a BLOCK rule), the fit guard accepted such a quantity, and `'２０２６-09-10'` resolved a validity policy | every digit pattern in `jscompat` is `re.ASCII` |
| RV-2 | `js_number('1e400')` / `('1e-400')` stayed finite / positive, where `Number()` gives `Infinity` / `0`: such a quantity skipped PBC-L-001 | `js_number` applies the binary64 range exactly (overflow from 2^1024 − 2^970, underflow at 2^-1075), representable values stay exact |
| RV-3 | `validate_battery_consistency` (and so `validate_engineering`) raised `InvalidOperation` on a non-numeric profile `batteryQuantity` (a `Decimal` NaN ordering comparison), where JavaScript compares `NaN > 0` false | NaN guarded before the comparison |
| RV-4 | `RuleSet.from_json` raised `TypeError` on an unhashable rule `code`; `RuleSet.with_changes` silently ignored an unknown rule code (a typo produced a "new" version identical to the base) and raised `ValueError`/`TypeError` for a bad severity or non-object parameters; a NaN/∞ `Decimal` passed as a numeric parameter | every problem is a `RuleSetError` with `{path, message}`; numeric parameters must be finite |
| RV-5 | `set_policy_status` and the branding account writers printed Python `None` in "not found" messages (JavaScript prints `null`) | `js_string` |
| RV-6 | `create_pack_commercial_snapshot(material_list=None)` raised `TypeError`; the JavaScript keeps an explicit `null` (its `[]` default replaces only `undefined`) | `null` kept |
| RV-7 | `registry_lines` kept the components' order although the hand-over note tells **packs** to check `lines=registry_lines(...)`: `packageApproval.runPackageChecker` sorts the lines with `localeCompare` (the underscore before letters: `AC_CABLE` < `ACDB` < `DC_CABLE` < `DCDB`), and that order decides which of two findings tying on (rule, components) — e.g. several PBC-K-002 — comes first in a stored verdict | the lines are stably sorted by the `localeCompare` key (1,500 random packages now equal `runPackageChecker`) |
