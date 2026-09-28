# engines-core — money, energy, savings, subsidy, finance

Work package engines-core ports the five customer-facing Flarize engines of PLAN §1.5 (`engines.money`,
`engines.energy`, `engines.savings`, `engines.subsidy`, `engines.finance`) from
`/home/user/flarize-main/flarize/src/lib` to pure Python (engines spec §0–§5, protected formulas §19, reference
outputs §20, porting notes §21). No Django, no app import (import-linter contract `engines-pure` plus
`engines/tests/test_core_purity.py`). Decisions D-7 and D-8 (PLAN §9) are applied. Deviations: DV-19, DV-20.

The package owns no table, endpoint or legacy data source, so there is no `legacy_import.py`; the legacy
*configuration* files these engines read are mapped below for the pricing package's importer (PLAN §7.4).

## What exists

| Module | JavaScript source (version) | Public API |
|---|---|---|
| `engines/money.py` | `money.js` (`MONEY_RULE_VERSION 'money.1'`), `pricingEngine.resolveGstRegime`/`applyGst` | `CURRENCY`, `ROUNDING_MODE`, `ROUNDING_UNIT`, `MONEY_RULE_VERSION`, `round_money`, `js_round`, `round_places`, `sum_exact`, `mul_exact`, `is_money`, `js_number`, `publish_parts`, `to_decimal`, `optional_decimal`, `json_number`, `is_number`, `canonical`, `js_text`, `exact` (60-digit context), GST: `GstConfig` (+ `from_cost_config`, `from_json`), `resolve_gst_regime`, `apply_gst`, `GstRegime`, `GstComponent`, `GstApplication`, `AppliedGstComponent`, `GstConfigError`, `GstError`, `GstRegimeCode` |
| `engines/energy.py` | `energyEngine.js` (`ENERGY_ENGINE_VERSION 'energyEngine.2'`) | `calculate_energy_profile`, `units_to_bill`, `bill_to_units`, `resolve_phase`, `validate_energy_config`, `list_regions`; inputs `EnergyInputs`; config `EnergyConfig`, `RegionConfig`, `TariffSlab`, `RateBand`, `FixedChargeSlab`, `MeterRent`, `YieldAssumption`, `SizingRule`; results `EnergyProfile` (+ `from_payload`), `EnergyBlocked`, `BillBreakdown`; enums `EnergyError`, `Phase`, `BillingCycle`, `BillingCategory` |
| `engines/savings.py` | `savingsEngine.js` (`'2.0.0'`) | `calculate_savings`, `project_lifetime`, `normalize_cycle`, `validate_savings_config`; inputs `SavingsInputs`; `SavingsConfig`; results `SavingsResult`, `SavingsBlocked`, `MonthlyBillBreakdown`, `LifetimeProjection`, `GraphPoint`, `NO_PROJECTION`; `SavingsError`, `SelfConsumptionModel` |
| `engines/subsidy.py` | `subsidyEngine.js` (`'1.0.0'`, scheme `PMSG_2024_V1`) | `calculate_subsidy`, `validate_subsidy_config`; inputs `SubsidyInputs`; `SubsidyConfig`, `ResidentialRule`, `SubsidyTier`, `GhsRule`, `SchemeInfo`; results `SubsidyEligible`, `SubsidyIneligible`, `SubsidyGivenUp`, `SubsidyNotRequested`, `SubsidyBlocked` (`SubsidyResult`); `SubsidyError`, `EligibilityStatus`, `SubsidyType` |
| `engines/finance.py` | `financeEngine.js` (`'1.1.0'`), `quotationWorkspace.resolveFinanceResult` | `calculate_finance`, `resolve_indicative_rate`, `finance_basis`, `resolve_finance`, `validate_finance_config`; inputs `FinanceInputs`; `FinanceConfig`, `RateTier`, `IndicativeRate`, `FinanceBasis`; results `FinanceResult`, `FinanceBlocked`; `FinanceError`, `LEGACY_FALLBACK_ANNUAL_INTEREST_RATE` |
| `engines/tests/golden/generate_core.mjs` | — | the capture: imports the real modules from `FLARIZE_ROOT`, writes `core_*.json` |
| `engines/tests/golden/core_{money,energy,energy_tables,savings,subsidy,finance}.json` | — | golden outputs (below) |
| `engines/tests/golden/core_divergences.json` | — | the pinned, classified binary64 divergences (below) |
| `engines/tests/test_core_parity.py`, `parity_runners.py`, `golden_support.py` | — | replay + field-by-field comparison |
| `engines/tests/test_core_reference.py` | — | spec §20 reference outputs, D-7, D-8, every error code, config parsing/validation, frozen results, float refusal |
| `engines/tests/test_core_purity.py` | — | stdlib-only imports, no clock/environment, floats only in the search replica |

Every engine takes a frozen input dataclass (the JavaScript's parameter object) plus its configuration and returns a
frozen result dataclass with typed snake_case fields and an `as_dict()` that is the JavaScript result object **key for
key** (camelCase, same order) — the quotation payload contract (D-3 keeps the Flarize payload).
Coverage of the five modules: 100 % statements and branches (2,221 engines tests).

## Decisions not spelled out in the PLAN

1. **Decimal only; floats refused at the boundary.** Engine inputs are `Decimal`, `int` or numeric strings;
   `float` and `bool` raise `TypeError` (`money.to_decimal`, dataclass `__post_init__`, engine entry points).
   Configuration JSON may be parsed with or without `parse_float=Decimal`: `money.json_number` turns a parsed float
   back into the decimal its JSON text meant (`repr`: `3.35`, not `3.35000000000000008882…`). Every engine runs in a
   60-significant-digit context with traps on (`money.exact`), so sums and products are exact and divisions/powers
   (EMI) are carried ~45 digits beyond the rupee.
2. **Two rounding rules, as in the JavaScript.** `money.round_money` is `money.js roundMoney` (half away from zero,
   symmetric; used by the GST composition). The four customer engines predate `money.js` and use `Math.round`
   (nearest, ties toward +∞) — `money.js_round`/`round_places` — at exactly the places the JavaScript does
   (`Math.round(x × 100) / 100` for the effective rate, one decimal for kWh figures, whole rupees elsewhere).
3. **The bill → units search is a binary64 replica (DV-20).** `billToUnits` bisects [0, 3000] bi-monthly units 50
   times over IEEE-754 midpoints and rounds the last midpoint. Where the bill total jumps exactly at a half unit
   (duty ₹1207.5 at 1312.5 units at ₹9.20, 81 bills per phase below ₹30,000), the answer depends on binary rounding
   in the last steps: an exact-decimal bisection returns one unit more or less at 34 of the 60,000 whole-rupee bills
   (`test_the_binary64_search_is_needed_for_parity` shows it). Both answers are equally good estimates of the half
   unit, so parity wins: `bill_to_units` replicates the JavaScript operation for operation in binary64
   (`_Binary64Region`, `_binary64_energy`, `_binary64_bill_total`, `_binary64_math_round`). It returns a whole
   number of units; every published amount is then recomputed from those units in Decimal (`units_to_bill`), which
   equals the JavaScript for every whole unit 0–3000 in both phases (dense table test). The replica is the only
   float arithmetic in engines-core; besides it, floats appear only where `money` recognises them at the boundary
   (`test_floats_appear_only_in_the_bill_search_replica`).
4. **Exact decimals where binary64 shows (DV-19).** Elsewhere the Python engines publish the exact decimal result.
   Where the JavaScript's binary floating point makes a published figure differ, the difference is intentional,
   pinned case by case in `core_divergences.json` and verified by kind:
   * `representation` — the JavaScript printed a binary64 artefact of the same expression: `totalInterest`
     `63400.01000000001` for 263400 − 199999.99 (fractional principals), `annualGeneration` `4708.799999999999` for
     3.27 kW × 120 × 12. Python: `63400.01`, `4708.8`. The test checks |js − py| ≤ |py|·10⁻¹².
   * `half` — the exact value is k + ½ and the JavaScript's binary64 evaluation fell just below it, so `Math.round`
     went down; Python rounds the exact half up, as `money.js` states for money ("the epsilon nudge removes binary
     representation error … so the same inputs always produce the same rupee"). Where it occurs: the effective rate
     at 599 bi-monthly units single phase (₹4,899/600 units = 8.165 → JS 8.16, Python 8.17; bi-monthly bills
     ₹4,897–4,902 — the only whole-unit consumption in 0–3000 where it happens, both phases checked exhaustively);
     the generation value `monthlyKsebValueLow/High` for fractional sizes (2.5 kW: 330 kWh × ₹5.35 = ₹1765.5 → JS
     1765, Python 1766; never for whole kW); the V1 fallback savings `generation × rate` (V1 runs only without a
     region tariff — the live flow always has one); duty at a fractional bill of units (direct `units_to_bill` of
     1312.5 units: 1207.5 → JS 1207, Python 1208; the engines themselves bill whole units). The test recomputes the
     exact value (must be k + ½), the binary64 value (must be below it and round to the JavaScript's number) and
     checks Python = JavaScript + one unit.
   * `consequence` — a value that differs only because a `half` field did (KSEB value from the rate; annual savings,
     payback and post-solar bills from V1 savings).
   A characterisation run (not committed: 60,000 bills × 13 sizes, V2 savings with and without the 25-year
   projection, V1 savings) found no other kind of difference; V2 savings — the live path — matched everywhere apart
   from `annualOutputKwh`, the pass-through of the energy result's `annualGeneration` artefact for 3.27 kW.
5. **D-8 — the `'3P'` tariff fix is a parameter.** `calculate_energy_profile(..., fix_three_phase_tariff=True)`
   (default, PLAN §9 D-8) maps `3P`, `3p`, `3 ph`, `3-phase`, `3PH`, `3_phase` (and anything containing `three`) to the
   three-phase tariff; `False` reproduces the JavaScript, which bills `'3P'` as single phase. Savings inherits the
   resolved phase from the energy result. The golden files carry both expectations for every `'3P'`-style case
   (`output` = the JavaScript as is, for `False`; `outputWithFix` = the JavaScript run with `'three'`, for `True`): 51
   energy and 36 savings cases. Example, ₹3,000 monthly, `'3P'`: JavaScript 659 units, fixed ₹480, meter ₹12, rate
   9.09, savings ₹2,949/month, payback 78 months; fixed: 654 units, fixed ₹500, meter ₹30, rate 9.17, savings
   ₹2,854, payback 81 months. `'1P'` is single either way.
6. **D-8 — payback basis stays Flarize's**: `ceil(customerTotalIncludingGST / annual savings × 12)`, gross, subsidy
   not deducted (spec §19); the lifetime projection stays off until `tariffEscalationLow/High` and
   `degradationRatePerYear` are approved (it is implemented and parity-tested with approved-looking values).
7. **D-7 — interest tiers are data.** `FinanceConfig.rate_tiers` (`RateTier(annual_interest_rate,
   min_finance_amount, max_finance_amount, label)`), from `finance.*` configuration rows or the legacy file; no
   rate is written in `finance.py` (`test_no_rate_is_hard_coded` parses the module). The JavaScript's fallbacks for
   missing keys are named constants: `LEGACY_FALLBACK_ANNUAL_INTEREST_RATE` (7 %, only with neither tiers nor
   `defaults.annualInterestRate`), tenure 10, max rate 18, tenure 1–10. The Flarize tier gap (≤ 200,000 and
   ≥ 200,000.01: a principal in between matches no tier and takes the last one) is kept and golden-tested.
8. **Down payment and principal** (`quotationWorkspace.resolveFinanceResult`, protected formula) are
   `finance_basis`/`resolve_finance`: down payment `round(gross × dp% / 100)` on the **original gross, before
   subsidy**; `postSubsidyInvestment = max(0, gross − subsidy)`; principal `max(0, gross − dp − subsidy)`; only an
   *available* subsidy counts (GIVE_IT_UP and ineligible count 0). The JavaScript function is not exported; the
   generator lifts its source text verbatim (sha256 in the golden header) and runs it with the real
   `calculateFinance`.
9. **No clock, no I/O.** `calculated_at` is passed by the caller (subsidy, finance); the engines never read the time
   (the JavaScript stamps `new Date()`; the generator freezes the clock at `2026-09-28T00:00:00.000Z`). Configuration
   comes in as dataclasses; `*.from_json` parse the legacy document shapes.
10. **JavaScript `null`/`undefined`.** Python `None` stands for `null`; a missing JSON member (`undefined`, dropped by
    `JSON.stringify`) is compared as `None`. JavaScript defaults apply only to `undefined`, so `subsidy_type=None`
    is NO_SUBSIDY and `connection_type=None` is INELIGIBLE_CONNECTION, as in the JavaScript. Blocked `reason` texts
    print numbers the JavaScript way (`money.js_text`: `null`, `10.5`, `undefined` for a missing region).
11. **Typed inputs.** A bill amount may be a Decimal, an int or a numeric string (trimmed); `parseFloat`'s
    numeric-prefix reading (`"3000abc"` → 3000) is not reproduced — it would be a serializer bug upstream.
12. **GST composition lives in `money`** (C73): `GstConfig.from_cost_config(gst_goods_share=0.70, gst_goods_rate=0.05,
    gst_services_share=0.30, gst_services_rate=0.18)` takes the PLAN §2.3 keys (fractions, CLAUDE.md) and builds the
    percentage regime; `resolve_gst_regime` derives the effective rate (8.9, 6 places, a cross-check only) and
    refuses a split that does not total 100 % or a declared rate that disagrees; `apply_gst` computes each component
    from the pre-GST amount, publishes each rounded, and the published total is the sum of the published components
    (₹210,285 → goods 7,360 + service 11,355 = 18,715). Parity: `resolveGstRegime` directly, `applyGst` through
    `calculatePricing` (zero margin, and as an extra) over 12 amounts × 3 regimes. The pricing/pack engines
    (later packages) reuse it.
13. **Energy results keep the unrounded energy charge** (`BillBreakdown.energy_charge_exact`, not in `as_dict()`),
    as `money.js` asks exact values to be kept beside published ones for audit.

## Parity evidence

`/opt/node22/bin/node engines/tests/golden/generate_core.mjs` (`FLARIZE_ROOT`, default
`/home/user/flarize-main/flarize/src/lib`; `GOLDEN_OUT` to write elsewhere) imports the real modules and writes:

| File | Cases | Grid |
|---|---|---|
| `core_money.json` | 98 | `roundMoney` (halves both signs, 1.005, 2.675, strings, `''`, `null`), `isMoney`, `sumExact`, `mulExact`, `resolveGstRegime` (12 configurations incl. every error), GST applied to 12 amounts × 3 regimes |
| `core_energy.json` | 781 | 48 unit boundaries (0 … 3000, every slab edge, 312/313, 1312/1313) × {total − 1, total, total + 1} × 2 phases (bi-monthly); 24 monthly bills × 8 phase texts (`single`, `three`, `1P`, `3P`, `Three Phase`, `3-phase`, `null`, `''`) + 7 more phase texts; 25 sizes (none, 0, −1, 1–12 kW incl. 1.5, 2.2, 2.5, 2.75, 3.24, 3.25, 3.27, 3.3, 4.4, 5.45, 6.6, 7.5, 9.9) × 2 bills; decimal and invalid bills; 5 cycles; 9 configuration shapes (old shapes with every fallback, explicit old values, partial tables, other yield, short slabs, no yield, yield as text, no tariff, no default region) × 3 bills × 2 phases; 3 region ids; all 162 half-unit tie bills; 4 half-rupee cases |
| `core_energy_tables.json` | 6,002 + 92 bills; 66,070 searches | `unitsToBill` for every whole unit 0–3000 × 2 phases, plus 92 fractional-unit and other-configuration cases; `billToUnits` for every whole bi-monthly rupee 1–30,000 and every half rupee 0.5–2,999.5 × 2 phases, plus 70 under other configuration shapes |
| `core_savings.json` | 261 | V2: 12 bills × 3 phases (`3P` included) × 3 sizes; V1: 3 bills × 7 bill/cycle inputs × 2 investments; 9 savings configurations (off, missing, three approved projections, full degradation, escalation out of range, fractional/zero years) × 3 bills × 2 investments + V1; 8 investments; 9 sizes with projection; 5 energy configuration shapes × 2 phases; every blocked code (null/unavailable energy, zero/null tariff, zero generation, zero consumption); payloads; 3 V1 half-rupee cases |
| `core_subsidy.json` | 363 | 19 sizes (0.5 … 25, edges 1/2/2.5/3/10/10.01/11) × 5 panel texts; GHS 6 sizes × 8 house counts (0, 2.5, −1, null, text); 6 subsidy types × 2 panels; 4 connection texts × 2 types; invalid sizes; missing config; 10 configuration shapes (DCR waived, state top-up, missing sections/scheme/dcr, custom and misordered and empty tiers, defaults only) × 16 residential + 3 GHS |
| `core_finance.json` | 419 | 13 principals (tier edges 200,000 / 200,000.004 / 200,000.01) × 6 tenures; 12 explicit rates × 3 tenures × 2 principals; zero rate; invalid principal/rate/tenure (incl. 2.5 and 1.25 years); passthrough; 9 configurations (D-7 EMI-rules tiers 5.75/8, no tiers with/without default, tight/no validation, default tenure 5, three tiers, down payment 20 %/none); `resolveFinanceResult` 8 grosses × 5 subsidy results × 4 configurations |

`engines/tests/test_core_parity.py` replays every case (energy and savings twice: without and with the D-8 fix) and
compares every field as a decimal string; 1,922 cases + the dense tables. Result: equal everywhere except the 107
pinned field differences of 40 cases in `core_divergences.json` (29 `representation`, 35 `half`, 43 `consequence`;
7 of those cases were added on purpose to exhibit the rule, 25 are fractional-principal finance cases). The spec §20
reference outputs are asserted literally in `test_core_reference.py`. `test_golden_capture_is_reproducible` re-runs the generator (when node and
`FLARIZE_ROOT` are available) and requires byte-identical files; the headers record the sha256 of every JavaScript
source.

## Legacy configuration → engine configuration (for the pricing importer, PLAN §7.4)

The engines take their configuration as data. The pricing package stores each legacy document (validated) in
`pricing_cost_config`; the engines parse the stored value with `from_json`. Proposed keys (≤ 48 chars):

| Legacy file | `pricing_cost_config.key` | Engine parser | Notes |
|---|---|---|---|
| `data/energy-config.json` | `energy.config` | `EnergyConfig.from_json` (+ `validate_energy_config`) | regions by key; `defaultRegion` |
| `data/savings-config.json` | `savings.config` | `SavingsConfig.from_json` (+ `validate_savings_config`) | escalation/degradation stay `null` until approved |
| `data/subsidy-config.json` | `subsidy.config` | `SubsidyConfig.from_json` (+ `validate_subsidy_config`) | |
| `data/finance-config.json` | `finance.config` | `FinanceConfig.from_json` (+ `validate_finance_config`) | D-7: the business-confirmed tiers replace `rateTiers` (`dataclasses.replace(config, rate_tiers=...)`) |
| `data/cost-config.json` `gst` | `gst_goods_share`, `gst_services_share`, `gst_goods_rate`, `gst_services_rate` (PLAN §2.3, fractions) | `GstConfig.from_cost_config` | or `GstConfig.from_json` on the Flarize block |

Field mapping of the documents (JSON → dataclass field; `—` = the JavaScript fallback when missing):

| JSON | Field | Fallback |
|---|---|---|
| `regions.<id>.tariffSlabs[].upToUnits/ratePerUnit` | `RegionConfig.tariff_slabs[TariffSlab]` | empty → NO_TARIFF_CONFIG |
| `nonTelescopicThreshold` | `non_telescopic_threshold` | 500 |
| `nonTelescopicSlabs[]` / `nonTelescopicRate` | `non_telescopic_slabs[RateBand]` / `non_telescopic_rate` | 8.20 |
| `fixedChargeSlabs.singlePhase/threePhase[]` / `fixedChargeBiMonthly` | `fixed_charge_single_phase/three_phase[FixedChargeSlab]` / `fixed_charge_bi_monthly` | 80 |
| `meterRent.singlePhaseBiMonthly/threePhaseBiMonthly` / `meterRentBiMonthly` | `meter_rent: MeterRent` / `meter_rent_bi_monthly` | 12 / 30; flat 14 |
| `electricityDutyPct` | `electricity_duty_pct` | 10 |
| `yield.dailyGenPerKw/unit/source` | `yield_assumption: YieldAssumption` | not a number → NO_YIELD_CONFIG |
| `sizing.coverageTarget/note` | `sizing: SizingRule` | 0.85; generated note |
| `regionName`, `discom`, `tariffName`, `tariffVersion` | same names, snake_case | — |
| savings `selfConsumptionModel`, `lifetimeYears`, `degradationRatePerYear`, `tariffEscalationLow/High` | `SavingsConfig` fields | projection off |
| subsidy `scheme.{name,schemeVersion,effectivePeriod}` | `SchemeInfo` | "PM Surya Ghar: Muft Bijli Yojana", `PMSG_2024_V1` |
| subsidy `residential.{tiers[upToKw,ratePerKw],maxSubsidy,maxCapacityKw,minCapacityKw}` | `ResidentialRule` | 78,000; 10; 1 |
| subsidy `ghs.{ratePerKw,maxKwPerHouse,maxCommunityKw}` | `GhsRule` | 18,000; 3; 500 |
| subsidy `stateTopUp.kerala`, `dcr.required` | `state_top_up_kerala`, `dcr_required` (only `false` waives) | 0; true |
| finance `rateTiers[{minFinanceAmount,maxFinanceAmount,annualInterestRate,label}]` | `rate_tiers[RateTier]` | none → `defaults.annualInterestRate` → 7 |
| finance `defaults.{tenureYears,annualInterestRate}`, `validation.{maxAnnualInterestRate,minTenureYears,maxTenureYears}`, `downPayment.{percentage,note}`, `source.rateQualification` | `default_tenure_years`, `default_annual_interest_rate`, `max_annual_interest_rate`, `min_tenure_years`, `max_tenure_years`, `down_payment_percentage`, `down_payment_note`, `rate_qualification` | 10; —; 18; 1; 10; 0 (in `finance_basis`) |

## Hand-over notes

* **Calculators / quotations** call `calculate_energy_profile(EnergyInputs(bill, cycle, phase, size), config)` —
  keep the default `fix_three_phase_tariff=True`; the platform's phase vocabulary `'1P'`/`'3P'` is accepted as is.
  Then `calculate_savings(SavingsInputs(profile, customer_total_including_gst), region=config.regions[profile.region_id],
  config=…)` (pass the region: V1 is a fallback), `calculate_subsidy(SubsidyInputs(size, …), config,
  calculated_at=…)` with the panel type
  resolved from the locked BOM (mixed panels → `'NON_DCR'`, unresolvable → do not call), and
  `resolve_finance(gross, subsidy_result, finance_config, calculated_at=…)`. Freeze `result.as_dict()` into the
  payload (Decimals; serialise with a Decimal-aware encoder).
* To re-derive savings from a stored payload: `EnergyProfile.from_payload(payload["energy"])`.
* `emi` (website EMI rules) should build a `FinanceConfig` from its rule rows (D-7) and call
  `calculate_finance(FinanceInputs(principal, tenure_years=…), config, calculated_at=…)`.
* The pricing/pack engines reuse `money.round_money`, `publish_parts`, `GstConfig.from_cost_config`,
  `resolve_gst_regime`, `apply_gst`.
* Re-capture after a change of the legacy JavaScript: run the generator, then the parity test; a new difference
  fails with the exact `(case, variant, path, javascript, python)` to review and, if it is binary64 noise of a
  documented kind, pin in `core_divergences.json`.
