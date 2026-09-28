#!/usr/bin/env node
// Golden parity capture for engines-core (money, energy, savings, subsidy, finance).
//
// Imports the REAL Flarize engines from FLARIZE_ROOT (default /home/user/flarize-main/flarize/src/lib) and their
// configuration from FLARIZE_DATA (default <FLARIZE_ROOT>/../../data), runs them over a deterministic grid and writes
// engines/tests/golden/core_*.json next to this file. The Python tests (engines/tests/test_core_parity.py) replay every
// case against engines/{money,energy,savings,subsidy,finance}.py and compare every output field exactly.
//
//   /opt/node22/bin/node engines/tests/golden/generate_core.mjs
//
// Deterministic: no randomness, the clock is frozen (the subsidy and finance engines stamp `new Date()`), and each
// file records the sha256 of the JavaScript sources it was captured from.

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = path.resolve(process.env.FLARIZE_ROOT || '/home/user/flarize-main/flarize/src/lib');
const DATA = path.resolve(process.env.FLARIZE_DATA || path.join(ROOT, '..', '..', 'data'));
const OUT = path.resolve(process.env.GOLDEN_OUT || path.dirname(fileURLToPath(import.meta.url)));
const FIXED_NOW = '2026-09-28T00:00:00.000Z';
const SCHEMA = 'engines-core.golden/1';

// ---- frozen clock ------------------------------------------------------------------------------------------------
const RealDate = Date;
globalThis.Date = class FrozenDate extends RealDate {
  constructor(...args) {
    if (args.length === 0) super(FIXED_NOW);
    else super(...args);
  }
  static now() {
    return new RealDate(FIXED_NOW).getTime();
  }
};

const load = (name) => import(pathToFileURL(path.join(ROOT, name)).href);
const money = await load('money.js');
const energy = await load('energyEngine.js');
const savings = await load('savingsEngine.js');
const subsidy = await load('subsidyEngine.js');
const finance = await load('financeEngine.js');
const pricing = await load('pricingEngine.js');
const cost = await load('costEngine.js');

// `resolveFinanceResult` (down payment on the gross before subsidy, then the principal) is not exported by
// quotationWorkspace.js; its source text is lifted verbatim and evaluated with the real calculateFinance.
const workspaceSource = fs.readFileSync(path.join(ROOT, 'quotationWorkspace.js'), 'utf8');
function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`${name} not found`);
  let depth = 0;
  for (let i = source.indexOf('{', start); i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`${name} is not closed`);
}
const commercialSnapshot = await load('commercialSnapshot.js');
const resolveFinanceSource = extractFunction(workspaceSource, 'resolveFinanceResult');
// eslint-disable-next-line no-new-func
const resolveFinanceResult = new Function('COMMERCIAL_SNAPSHOT_STATUS', 'calculateFinance', `${resolveFinanceSource}\nreturn resolveFinanceResult;`)(
  commercialSnapshot.COMMERCIAL_SNAPSHOT_STATUS,
  finance.calculateFinance,
);

const readJson = (name) => JSON.parse(fs.readFileSync(path.join(DATA, name), 'utf8'));
const clone = (value) => JSON.parse(JSON.stringify(value));
const sha256 = (name) => crypto.createHash('sha256').update(fs.readFileSync(path.join(ROOT, name))).digest('hex');
const plain = (value) => JSON.parse(JSON.stringify(value)); // drops undefined, -0 → 0, freezes nothing

function write(name, header, cases, extra = {}) {
  const head = JSON.stringify({ schema: SCHEMA, ...header, fixedNow: FIXED_NOW, ...extra, caseCount: cases.length });
  const body = cases.map((c) => JSON.stringify(c)).join(',\n');
  fs.writeFileSync(path.join(OUT, name), `{"header": ${head},\n"cases": [\n${body}\n]}\n`);
  console.log(`${name}: ${cases.length} cases`);
}

// ---- configurations ----------------------------------------------------------------------------------------------
const ENERGY_CONFIG = readJson('energy-config.json');
const SAVINGS_CONFIG = readJson('savings-config.json');
const SUBSIDY_CONFIG = readJson('subsidy-config.json');
const FINANCE_CONFIG = readJson('finance-config.json');
const GST_CONFIG = readJson('cost-config.json').gst;
const KERALA = ENERGY_CONFIG.regions.kerala;

function energyVariant(mutate) {
  const config = clone(ENERGY_CONFIG);
  mutate(config.regions.kerala, config);
  return config;
}

const ENERGY_CONFIGS = {
  real: ENERGY_CONFIG,
  // old shapes, every fallback: nonTelescopicRate ?? 8.20, fixedChargeBiMonthly ?? 80, meterRentBiMonthly ?? 14,
  // threshold ?? 500, duty ?? 10, coverage ?? 0.85 with the generated note
  legacyDefaults: energyVariant((r) => {
    for (const key of ['nonTelescopicSlabs', 'fixedChargeSlabs', 'meterRent', 'nonTelescopicThreshold', 'electricityDutyPct', 'sizing']) delete r[key];
  }),
  // old shapes with explicit values
  legacyExplicit: energyVariant((r) => {
    for (const key of ['nonTelescopicSlabs', 'fixedChargeSlabs', 'meterRent']) delete r[key];
    Object.assign(r, { nonTelescopicRate: 7.5, fixedChargeBiMonthly: 90, meterRentBiMonthly: 20, electricityDutyPct: 12, nonTelescopicThreshold: 400, sizing: { coverageTarget: 1 } });
  }),
  // meterRent object without keys (→ 12 / 30), an empty three-phase fixed table (→ flat fallback), duty 0
  partialTables: energyVariant((r) => {
    r.meterRent = {};
    r.fixedChargeSlabs = { singlePhase: r.fixedChargeSlabs.singlePhase, threePhase: [] };
    r.fixedChargeBiMonthly = 75;
    r.electricityDutyPct = 0;
  }),
  altYield: energyVariant((r) => {
    r.yield = { dailyGenPerKw: 4.2, unit: 'kWh/kW/day' };
    r.sizing = { coverageTarget: 0.9 };
  }),
  // telescopic slabs that stop short of the threshold (units beyond the last slab are not charged), a null band
  shortSlabs: energyVariant((r) => {
    r.tariffSlabs = r.tariffSlabs.slice(0, 3);
    r.nonTelescopicSlabs = [{ upToUnits: 700, ratePerUnit: 7 }, { upToUnits: 900, ratePerUnit: 8 }];
  }),
  noYield: energyVariant((r) => delete r.yield),
  yieldNotNumber: energyVariant((r) => (r.yield = { dailyGenPerKw: '4.0' })),
  noTariff: energyVariant((r) => (r.tariffSlabs = [])),
  noDefaultRegion: energyVariant((r, c) => delete c.defaultRegion),
};

const SAVINGS_CONFIGS = {
  real: SAVINGS_CONFIG,
  none: null,
  projected: { ...SAVINGS_CONFIG, tariffEscalationLow: 3, tariffEscalationHigh: 5, degradationRatePerYear: 0.5, lifetimeYears: 25 },
  projectedShort: { ...SAVINGS_CONFIG, tariffEscalationLow: 2, tariffEscalationHigh: 2, degradationRatePerYear: 0.7, lifetimeYears: 10 },
  projectedFlat: { ...SAVINGS_CONFIG, tariffEscalationLow: 0, tariffEscalationHigh: 0, degradationRatePerYear: 0, lifetimeYears: 3 },
  projectedFullDegradation: { ...SAVINGS_CONFIG, tariffEscalationLow: 4, tariffEscalationHigh: 6, degradationRatePerYear: 40, lifetimeYears: 5 },
  escalationOutOfRange: { ...SAVINGS_CONFIG, tariffEscalationLow: 100, tariffEscalationHigh: 5, degradationRatePerYear: 0.5, lifetimeYears: 25 },
  fractionalYears: { ...SAVINGS_CONFIG, tariffEscalationLow: 3, tariffEscalationHigh: 5, degradationRatePerYear: 0.5, lifetimeYears: 2.5 },
  zeroYears: { ...SAVINGS_CONFIG, tariffEscalationLow: 3, tariffEscalationHigh: 5, degradationRatePerYear: 0.5, lifetimeYears: 0 },
};

function subsidyVariant(mutate) {
  const config = clone(SUBSIDY_CONFIG);
  mutate(config);
  return config;
}
const SUBSIDY_CONFIGS = {
  real: SUBSIDY_CONFIG,
  dcrWaived: subsidyVariant((c) => (c.dcr.required = false)),
  stateTopUp: subsidyVariant((c) => (c.stateTopUp.kerala = 5000)),
  noResidential: subsidyVariant((c) => delete c.residential),
  noGhs: subsidyVariant((c) => delete c.ghs),
  noScheme: subsidyVariant((c) => delete c.scheme),
  noDcrSection: subsidyVariant((c) => delete c.dcr),
  customTiers: subsidyVariant((c) => {
    c.residential = { tiers: [{ upToKw: 1, ratePerKw: 40000 }, { upToKw: 2, ratePerKw: 20000 }, { upToKw: 3, ratePerKw: 10000 }], maxSubsidy: 65000, maxCapacityKw: 5, minCapacityKw: 0.5 };
    c.ghs = { ratePerKw: 15000, maxKwPerHouse: 2, maxCommunityKw: 100 };
  }),
  defaultsOnly: subsidyVariant((c) => {
    c.residential = { tiers: c.residential.tiers };
    c.ghs = {};
  }),
  emptyTiers: subsidyVariant((c) => (c.residential.tiers = [])),
  misorderedTiers: subsidyVariant((c) => (c.residential.tiers = [{ upToKw: 3, ratePerKw: 18000 }, { upToKw: 2, ratePerKw: 30000 }])),
};

function financeVariant(mutate) {
  const config = clone(FINANCE_CONFIG);
  mutate(config);
  return config;
}
const FINANCE_CONFIGS = {
  real: FINANCE_CONFIG,
  // D-7: the website EMI rules' tiers (5.75 % / 8 %)
  emiRules: financeVariant((c) => (c.rateTiers = [{ maxFinanceAmount: 200000, annualInterestRate: 5.75, label: 'EMI rules' }, { minFinanceAmount: 200000.01, annualInterestRate: 8, label: 'EMI rules' }])),
  noTiersDefaultRate: financeVariant((c) => {
    delete c.rateTiers;
    c.defaults.annualInterestRate = 6.5;
  }),
  noTiersNoDefault: financeVariant((c) => (c.rateTiers = [])),
  tightValidation: financeVariant((c) => (c.validation = { maxAnnualInterestRate: 12, minTenureYears: 2, maxTenureYears: 7 })),
  noValidation: financeVariant((c) => {
    delete c.validation;
    delete c.defaults;
    delete c.source;
    delete c.downPayment;
    delete c.configVersion;
  }),
  shortDefaultTenure: financeVariant((c) => (c.defaults.tenureYears = 5)),
  threeTiers: financeVariant(
    (c) =>
      (c.rateTiers = [
        { minFinanceAmount: 0, maxFinanceAmount: 100000, annualInterestRate: 4.5, label: 'small' },
        { minFinanceAmount: 100000.01, maxFinanceAmount: 300000, annualInterestRate: 6.25 },
        { minFinanceAmount: 300000.01, annualInterestRate: 9.1, label: 'large' },
      ]),
  ),
  downPayment20: financeVariant((c) => (c.downPayment.percentage = 20)),
  noDownPayment: financeVariant((c) => delete c.downPayment),
};

const HEADER_SOURCES = (...names) => Object.fromEntries(names.map((n) => [n, sha256(n)]));

// ---- money --------------------------------------------------------------------------------------------------------
{
  const cases = [];
  const roundInputs = [0, 0.4, 0.5, 0.6, 1.5, 2.5, 3.5, -0.4, -0.5, -1.5, -2.5, 1.005, 2.675, 1234.4999, 1234.5, -1234.5, 99999999.5, 12345.678, 0.49999999999999, 7359.975, 210285.5, null, 'abc', '12.5', '', ' 7 ', '-3.5'];
  for (const value of roundInputs) cases.push({ id: `roundMoney:${JSON.stringify(value)}`, fn: 'roundMoney', input: { value }, output: money.roundMoney(value) });
  const moneyInputs = [0, 12, -3.5, null, '', 'abc', '12.5', ' 7 ', true];
  for (const value of moneyInputs) cases.push({ id: `isMoney:${JSON.stringify(value)}`, fn: 'isMoney', input: { value }, output: money.isMoney(value) });
  const sums = [[], [1, 2, 3], [100, 200.5, null, 'x', '12.25'], [0.5, 0.25, 0.125], [-5, 5], [1e6, 0.5], null];
  for (const values of sums) cases.push({ id: `sumExact:${JSON.stringify(values)}`, fn: 'sumExact', input: { values }, output: money.sumExact(values) });
  const products = [[2, 3], [1.5, 4], [null, 5], ['2.5', '4'], ['x', 3], [-2.25, 8], [123456, 0.5]];
  for (const [a, b] of products) cases.push({ id: `mulExact:${JSON.stringify([a, b])}`, fn: 'mulExact', input: { a, b }, output: money.mulExact(a, b) });
  const regimes = {
    composite: GST_CONFIG,
    compositeNoDeclared: { regime: 'SOLAR_70_30_COMPOSITE', goodsValuationPct: 70, goodsRatePct: 5, serviceValuationPct: 30, serviceRatePct: 18 },
    compositeOtherSplit: { regime: 'SOLAR_70_30_COMPOSITE', goodsValuationPct: 60, goodsRatePct: 12, serviceValuationPct: 40, serviceRatePct: 18 },
    compositeBadSplit: { regime: 'SOLAR_70_30_COMPOSITE', goodsValuationPct: 70, goodsRatePct: 5, serviceValuationPct: 20, serviceRatePct: 18 },
    compositeMissing: { regime: 'SOLAR_70_30_COMPOSITE', goodsValuationPct: 70, goodsRatePct: 5, serviceValuationPct: 30 },
    compositeMismatch: { ...GST_CONFIG, effectiveRatePct: 9 },
    compositeCloseEnough: { ...GST_CONFIG, effectiveRatePct: 8.90005 },
    flat: { regime: 'FLAT', ratePct: 9 },
    flatImplicit: { ratePct: 18 },
    flatMissingRate: { regime: 'FLAT' },
    none: {},
    unknown: { regime: 'VAT' },
  };
  for (const [name, gst] of Object.entries(regimes)) {
    const resolved = pricing.resolveGstRegime(gst);
    cases.push({ id: `resolveGstRegime:${name}`, fn: 'resolveGstRegime', input: { gst }, output: plain({ ok: resolved.ok, code: resolved.code ?? null, regime: resolved.regime ?? null, effectiveRatePct: resolved.effectiveRatePct ?? null, components: resolved.components ?? null }) });
  }
  // GST applied to a pre-GST amount, captured through calculatePricing (margin 0: selling price = cost, so the GST
  // components are applyGst(base)) and through an extra of the same amount.
  const bases = [0, 1, 100, 1000, 12345, 12345.67, 99999.5, 210285, 229000, 199.99, 7.14, 1000000];
  for (const regimeName of ['composite', 'compositeOtherSplit', 'flat']) {
    for (const base of bases) {
      const result = pricing.calculatePricing({
        costResult: { status: cost.COST_STATUS.COMPLETE, totalActualProjectCostExact: base, totalActualProjectCost: base },
        margin: { targetGrossMargin: 0 },
        gst: regimes[regimeName],
        extras: [{ extraId: 'x', customerPrice: base }],
        pricedAt: FIXED_NOW,
      });
      cases.push({
        id: `applyGst:${regimeName}:${base}`,
        fn: 'applyGst',
        input: { gst: regimes[regimeName], base },
        output: plain({
          components: result.gstComponents.map((c) => ({ label: c.label, valuationPct: c.valuationPct, ratePct: c.ratePct, taxableValue: c.taxableValue, taxAmount: c.taxAmount })),
          totalPublished: result.gstAmount,
          priceExcludingGST: result.priceExcludingGST,
          priceIncludingGST: result.priceIncludingGST,
          extraGstAmount: result.extras[0].gstAmount,
          extraCustomerPriceIncludingGST: result.extras[0].customerPriceIncludingGST,
        }),
      });
    }
  }
  write('core_money.json', { engine: 'money', version: money.MONEY_RULE_VERSION, sources: HEADER_SOURCES('money.js', 'pricingEngine.js') }, cases, {
    constants: { CURRENCY: money.CURRENCY, ROUNDING_MODE: money.ROUNDING_MODE, ROUNDING_UNIT: money.ROUNDING_UNIT, MONEY_RULE_VERSION: money.MONEY_RULE_VERSION },
  });
}

// ---- energy -------------------------------------------------------------------------------------------------------
// D-8: phases whose tariff the platform fixes ('3P' is billed as single phase by the JavaScript). For these the
// expectation with the fix is the JavaScript run with phase 'three'.
const D8_PHASES = new Set(['3P', '3p', '3-phase', '3 PH', '3ph']);
const phaseTotal = (units, phase, region = KERALA) => energy.unitsToBill(units, region, phase).total;

function energyCase(id, inputs, configName = 'real', regionId = undefined) {
  const config = ENERGY_CONFIGS[configName];
  const output = plain(energy.calculateEnergyProfile(inputs, config, regionId));
  const item = { id, config: configName, regionId: regionId ?? null, input: plain(inputs), output };
  if (D8_PHASES.has(inputs.phase)) item.outputWithFix = plain(energy.calculateEnergyProfile({ ...inputs, phase: 'three' }, config, regionId));
  return item;
}

// bills where the total jumps exactly at a half unit: the bisection result depends on IEEE-754 details
function halfUnitTieBills(phase) {
  const bills = [];
  for (let m = 0; m < 3000; m += 1) {
    const at = phaseTotal(m + 0.5, phase);
    if (at !== phaseTotal(m + 0.5 - 1e-9, phase)) bills.push(at);
  }
  return bills;
}

const energyCases = [];
{
  const boundaries = [0, 1, 2, 50, 99, 100, 101, 150, 199, 200, 201, 250, 299, 300, 301, 312, 313, 350, 399, 400, 401, 450, 499, 500, 501, 550, 599, 600, 601, 650, 699, 700, 701, 750, 799, 800, 801, 900, 999, 1000, 1001, 1312, 1313, 1500, 2000, 2500, 2999, 3000];
  for (const phase of ['single', 'three']) {
    for (const units of boundaries) {
      const total = phaseTotal(units, phase);
      for (const bill of [total - 1, total, total + 1]) {
        if (bill > 0) energyCases.push(energyCase(`boundary:${phase}:${units}u:bill${bill}`, { billAmount: bill, billingCycle: 'bimonthly', phase }));
      }
    }
  }
  const monthlyBills = [50, 56, 100, 250, 500, 750, 908, 1000, 1500, 2000, 2500, 2911, 3000, 3500, 4000, 5000, 6000, 7500, 10000, 13915, 15000, 20000, 25000, 40000];
  for (const phase of ['single', 'three', '1P', '3P', 'Three Phase', '3-phase', null, '']) {
    for (const bill of monthlyBills) energyCases.push(energyCase(`monthly:${phase}:${bill}`, { billAmount: bill, billingCycle: 'monthly', phase }));
  }
  for (const phase of ['3p', '3 PH', '3ph', 'THREE_PHASE', 'three-phase', 'single phase', '1 phase']) {
    energyCases.push(energyCase(`phase:${phase}`, { billAmount: 3000, billingCycle: 'monthly', phase }));
  }
  const sizes = [null, 0, -1, 1, 1.5, 2, 2.2, 2.5, 2.75, 3, 3.24, 3.25, 3.27, 3.3, 4, 4.4, 5, 5.45, 6, 6.6, 7.5, 8, 9.9, 10, 12];
  for (const bill of [3000, 1200]) {
    for (const size of sizes) energyCases.push(energyCase(`size:${bill}:${size}`, { billAmount: bill, billingCycle: 'monthly', phase: 'single', systemSizeKw: size }));
  }
  energyCases.push(energyCase('size:string', { billAmount: 3000, billingCycle: 'monthly', phase: 'single', systemSizeKw: '5' }));
  for (const bill of [0.01, 55.5, 99.99, 1234.5, 1234.56, 2999.99, 3000.5, 17963.25]) energyCases.push(energyCase(`decimal:${bill}`, { billAmount: bill, billingCycle: 'monthly', phase: 'single' }));
  for (const bill of [0, -100, null, 'abc', '3000', '  4500 ']) energyCases.push(energyCase(`invalid-or-text:${JSON.stringify(bill)}`, { billAmount: bill, billingCycle: 'monthly', phase: 'single' }));
  for (const cycle of ['bimonthly', 'monthly', 'BIMONTHLY', 'bi-monthly', null]) energyCases.push(energyCase(`cycle:${cycle}`, { billAmount: 6000, billingCycle: cycle, phase: 'single' }));
  energyCases.push(energyCase('defaults', { billAmount: 3000 }));
  for (const name of Object.keys(ENERGY_CONFIGS).filter((n) => n !== 'real')) {
    for (const bill of [500, 3000, 20000]) {
      for (const phase of ['single', 'three']) energyCases.push(energyCase(`config:${name}:${phase}:${bill}`, { billAmount: bill, billingCycle: 'monthly', phase }, name));
    }
  }
  for (const regionId of ['kerala', 'tamilnadu', '']) energyCases.push(energyCase(`region:${regionId}`, { billAmount: 3000, billingCycle: 'monthly', phase: 'single' }, 'real', regionId));
  for (const phase of ['single', 'three']) {
    for (const bill of halfUnitTieBills(phase)) energyCases.push(energyCase(`tie:${phase}:${bill}`, { billAmount: bill, billingCycle: 'bimonthly', phase }));
  }
  // exact half-rupee products: generation value = daily generation × 30 × effective rate lands on ₹x.5
  for (const [bill, size] of [[1173, 2.5], [1862, 2.5], [4901, undefined], [4897, 5.45]]) {
    energyCases.push(energyCase(`half-rupee:${bill}:${size}`, { billAmount: bill, billingCycle: 'bimonthly', phase: 'single', systemSizeKw: size }));
  }
  // (review) the same for a WHOLE-kW auto size under another yield (4.2 kWh/kW/day: 10 kW → high 46.2 kWh/day)
  energyCases.push(energyCase('half-rupee:altYield:9078:three', { billAmount: 9078, billingCycle: 'monthly', phase: 'three' }, 'altYield'));
  write('core_energy.json', { engine: 'energy', version: energy.ENERGY_ENGINE_VERSION, sources: HEADER_SOURCES('energyEngine.js'), d8Phases: [...D8_PHASES] }, energyCases, { configs: ENERGY_CONFIGS });
}

// dense tables: every whole unit 0..3000 → bill, and the bill → units search over every whole bi-monthly rupee
{
  const tables = {};
  for (const phase of ['single', 'three']) {
    tables[phase] = [];
    for (let units = 0; units <= 3000; units += 1) {
      const b = energy.unitsToBill(units, KERALA, phase);
      tables[phase].push([b.energyCharge, b.fixedCharge, b.duty, b.meterRent, b.total]);
    }
  }
  const fractional = [];
  for (const phase of ['single', 'three', 'other']) {
    for (const units of [0.5, 2.25, 99.5, 100.5, 250.75, 312.5, 500.5, 612.5, 1312.5, 2999.5, 3500, 5000]) {
      fractional.push({ phase, units, output: plain(energy.unitsToBill(units, KERALA, phase)) });
    }
  }
  for (const name of ['legacyDefaults', 'legacyExplicit', 'partialTables', 'shortSlabs']) {
    for (const phase of ['single', 'three']) {
      for (const units of [0, 150, 350, 450, 550, 800, 1500]) fractional.push({ config: name, phase, units, output: plain(energy.unitsToBill(units, ENERGY_CONFIGS[name].regions.kerala, phase)) });
    }
  }
  const search = {};
  for (const phase of ['single', 'three']) {
    search[phase] = [];
    for (let bill = 1; bill <= 30000; bill += 1) search[phase].push(energy.billToUnits(bill, KERALA, phase));
  }
  const halfRupees = {};
  for (const phase of ['single', 'three']) {
    halfRupees[phase] = [];
    for (let bill = 0.5; bill <= 3000; bill += 1) halfRupees[phase].push(energy.billToUnits(bill, KERALA, phase));
  }
  const variantSearch = [];
  for (const name of ['legacyDefaults', 'legacyExplicit', 'partialTables', 'shortSlabs', 'altYield']) {
    for (const phase of ['single', 'three']) {
      for (const bill of [1, 150, 999, 2500, 7777, 12001, 29999.99]) variantSearch.push({ config: name, phase, bill, units: energy.billToUnits(bill, ENERGY_CONFIGS[name].regions.kerala, phase) });
    }
  }
  fs.writeFileSync(
    path.join(OUT, 'core_energy_tables.json'),
    `${JSON.stringify({
      header: { schema: SCHEMA, engine: 'energy', version: energy.ENERGY_ENGINE_VERSION, sources: HEADER_SOURCES('energyEngine.js'), fixedNow: FIXED_NOW, unitsToBillColumns: ['energyCharge', 'fixedCharge', 'duty', 'meterRent', 'total'], billToUnitsRange: { from: 1, to: 30000, step: 1 }, billToUnitsHalfRupeeRange: { from: 0.5, to: 2999.5, step: 1 } },
      unitsToBill: tables,
      unitsToBillCases: fractional,
      billToUnits: search,
      billToUnitsHalfRupees: halfRupees,
      billToUnitsVariants: variantSearch,
    })}\n`,
  );
  console.log('core_energy_tables.json: 6002 unitsToBill rows, 60000 + 6000 billToUnits searches');
}

// ---- savings ------------------------------------------------------------------------------------------------------
{
  const cases = [];
  function savingsCase(id, { energyInputs = null, energyResult = undefined, energyConfig = 'real', investment = null, currentBillAmount = null, currentBillCycle = 'monthly', withRegion = true, config = 'real' }) {
    const eConfig = ENERGY_CONFIGS[energyConfig];
    const run = (inputs) => {
      const profile = energyResult !== undefined ? energyResult : inputs ? energy.calculateEnergyProfile(inputs, eConfig) : null;
      const regionConfig = withRegion ? eConfig.regions[profile?.regionId || eConfig.defaultRegion] ?? null : null;
      return plain(savings.calculateSavings({ energyProfileResult: profile, customerTotalIncludingGST: investment, currentBillAmount, currentBillCycle, regionConfig, config: SAVINGS_CONFIGS[config] ?? {} }));
    };
    const item = {
      id,
      input: plain({ energyInputs, energyResult: energyResult === undefined ? undefined : energyResult, energyConfig, investment, currentBillAmount, currentBillCycle, withRegion, config }),
      output: run(energyInputs),
    };
    if (energyInputs && D8_PHASES.has(energyInputs.phase)) item.outputWithFix = run({ ...energyInputs, phase: 'three' });
    return item;
  }
  for (const bill of [40, 56, 60, 100, 500, 1000, 1500, 2000, 3000, 5000, 10000, 20000]) {
    for (const phase of ['single', 'three', '3P']) {
      for (const size of [null, 3, 5]) {
        cases.push(savingsCase(`v2:${bill}:${phase}:${size}`, { energyInputs: { billAmount: bill, billingCycle: 'monthly', phase, systemSizeKw: size }, investment: 229000 }));
      }
    }
  }
  for (const bill of [1000, 3000, 10000]) {
    for (const [amount, cycle] of [[null, 'monthly'], [3000, 'monthly'], [6000, 'bimonthly'], [6001, 'Bi-Monthly'], [2500.5, 'monthly'], [0, 'monthly'], [4000, null]]) {
      for (const investment of [229000, null]) {
        cases.push(savingsCase(`v1:${bill}:${amount}:${cycle}:${investment}`, { energyInputs: { billAmount: bill, billingCycle: 'monthly', phase: 'single' }, investment, currentBillAmount: amount, currentBillCycle: cycle, withRegion: false }));
      }
    }
  }
  for (const config of Object.keys(SAVINGS_CONFIGS)) {
    for (const bill of [1500, 3000, 8000]) {
      for (const investment of [229000, null]) cases.push(savingsCase(`config:${config}:${bill}:${investment}`, { energyInputs: { billAmount: bill, billingCycle: 'monthly', phase: 'single', systemSizeKw: 4 }, investment, config }));
    }
    cases.push(savingsCase(`config-v1:${config}`, { energyInputs: { billAmount: 3000, billingCycle: 'monthly', phase: 'single' }, investment: 229000, config, withRegion: false }));
  }
  // V1 fallback where generation × effective rate is an exact half rupee
  for (const [bill, size] of [[476, 5], [481, undefined], [483, 2]]) {
    cases.push(savingsCase(`v1-half-rupee:${bill}:${size}`, { energyInputs: { billAmount: bill, billingCycle: 'bimonthly', phase: 'single', systemSizeKw: size }, investment: 229000, currentBillAmount: bill, currentBillCycle: 'bimonthly', withRegion: false }));
  }
  for (const investment of [229000, null, 0, -5, 150000.5, 1, 35388, 2949]) cases.push(savingsCase(`investment:${investment}`, { energyInputs: { billAmount: 3000, billingCycle: 'monthly', phase: 'single' }, investment }));
  for (const size of [1, 1.5, 2.2, 2.5, 3.25, 3.3, 5.45, 6.6, 10]) {
    for (const bill of [1500, 3000]) cases.push(savingsCase(`size:${bill}:${size}`, { energyInputs: { billAmount: bill, billingCycle: 'monthly', phase: 'single', systemSizeKw: size }, investment: 229000, config: 'projected' }));
  }
  for (const name of ['legacyDefaults', 'legacyExplicit', 'partialTables', 'altYield', 'shortSlabs']) {
    for (const phase of ['single', 'three']) cases.push(savingsCase(`energy-config:${name}:${phase}`, { energyInputs: { billAmount: 3000, billingCycle: 'monthly', phase }, energyConfig: name, investment: 229000 }));
  }
  // blocked paths
  const reference = energy.calculateEnergyProfile({ billAmount: 3000, billingCycle: 'monthly', phase: 'single' }, ENERGY_CONFIG);
  cases.push(savingsCase('blocked:no-energy', { energyResult: null, investment: 229000 }));
  cases.push(savingsCase('blocked:energy-unavailable', { energyInputs: { billAmount: 0, billingCycle: 'monthly', phase: 'single' }, investment: 229000 }));
  cases.push(savingsCase('blocked:zero-tariff', { energyResult: { ...reference, averageTariffRate: 0 }, investment: 229000 }));
  cases.push(savingsCase('blocked:null-tariff', { energyResult: { ...reference, averageTariffRate: null }, investment: 229000 }));
  cases.push(savingsCase('blocked:zero-generation', { energyResult: { ...reference, monthlyGeneration: 0 }, investment: 229000 }));
  cases.push(savingsCase('blocked:zero-consumption', { energyResult: { ...reference, monthlyConsumption: 0 }, investment: 229000 }));
  cases.push(savingsCase('payload:reference', { energyResult: plain(reference), investment: 229000 }));
  cases.push(savingsCase('payload:no-size', { energyResult: { ...plain(reference), recommendedSystemSizeKw: null }, investment: 229000 }));
  cases.push(savingsCase('payload:generation-above-consumption', { energyResult: { ...plain(reference), monthlyGeneration: 400, dailyGenerationLow: 12, dailyGenerationHigh: 14.6 }, investment: 229000, withRegion: false }));
  // (review) a BLOCKED energy payload: savings must answer MISSING_ENERGY_RESULT, not a tariff/generation code
  cases.push(savingsCase('payload:blocked', { energyResult: plain(energy.calculateEnergyProfile({ billAmount: 0, billingCycle: 'monthly', phase: 'single' }, ENERGY_CONFIG)), investment: 229000 }));
  // (review) panel-multiple sizes where the V2 (live) dailyGenerationUnits = round((low + high) / 2, 1) is an exact
  // x.x5 that binary64 evaluates just below: 8 × 580 W, 11 × 535 W, 19 × 590 W
  for (const size of [4.64, 5.885, 11.21]) {
    cases.push(savingsCase(`size-half:3000:${size}`, { energyInputs: { billAmount: 3000, billingCycle: 'monthly', phase: 'single', systemSizeKw: size }, investment: 229000 }));
  }
  write('core_savings.json', { engine: 'savings', version: savings.SAVINGS_ENGINE_VERSION, sources: HEADER_SOURCES('savingsEngine.js', 'energyEngine.js'), d8Phases: [...D8_PHASES] }, cases, { energyConfigs: ENERGY_CONFIGS, configs: SAVINGS_CONFIGS });
}

// ---- subsidy ------------------------------------------------------------------------------------------------------
{
  const cases = [];
  const add = (id, params, configName = 'real') => {
    const args = { ...params };
    if (configName !== undefined) args.subsidyConfig = configName === null ? null : SUBSIDY_CONFIGS[configName];
    cases.push({ id, config: configName, input: plain(params), output: plain(subsidy.calculateSubsidy(args)) });
  };
  const sizes = [0.5, 0.99, 1, 1.25, 1.5, 2, 2.2, 2.5, 2.75, 3, 3.3, 4, 5, 7.5, 9.99, 10, 10.01, 11, 25];
  for (const panelType of ['DCR', 'dcr', 'NON_DCR', 'non-DCR', null]) {
    for (const size of sizes) add(`residential:${panelType}:${size}`, { systemSizeKw: size, subsidyType: 'residential', panelType, connectionType: 'domestic' });
  }
  for (const size of [0.5, 1, 2.5, 3, 5, 12]) {
    for (const houses of [1, 4, 200, 0, 2.5, -1, null, '4']) add(`ghs:${size}:${houses}`, { systemSizeKw: size, subsidyType: 'ghs', ghsHouses: houses, panelType: 'DCR', connectionType: 'domestic' });
  }
  add('ghs:non-dcr', { systemSizeKw: 5, subsidyType: 'ghs', ghsHouses: 4, panelType: 'NON_DCR' });
  for (const type of ['none', null, '', 'commercial', 'Residential', 'GHS']) {
    for (const panelType of ['DCR', 'NON_DCR']) add(`type:${type}:${panelType}`, { systemSizeKw: 3, subsidyType: type, panelType });
  }
  for (const connectionType of ['commercial', null, 'Domestic', 'industrial']) {
    for (const type of ['residential', 'ghs']) add(`connection:${connectionType}:${type}`, { systemSizeKw: 3, subsidyType: type, connectionType, panelType: 'DCR' });
  }
  for (const size of [0, -1, null, '3', true]) add(`size-invalid:${JSON.stringify(size)}`, { systemSizeKw: size, subsidyType: 'residential' });
  add('defaults', { systemSizeKw: 3 });
  add('config-missing:residential', { systemSizeKw: 3, subsidyType: 'residential' }, null);
  add('config-missing:none', { systemSizeKw: 3, subsidyType: 'none' }, null);
  for (const name of Object.keys(SUBSIDY_CONFIGS).filter((n) => n !== 'real')) {
    for (const size of [0.5, 1.5, 2, 2.5, 3, 5, 6, 11]) {
      for (const panelType of ['DCR', 'NON_DCR']) add(`config:${name}:residential:${size}:${panelType}`, { systemSizeKw: size, subsidyType: 'residential', panelType }, name);
    }
    for (const [size, houses] of [[2, 3], [5, 40], [3, 1]]) add(`config:${name}:ghs:${size}x${houses}`, { systemSizeKw: size, subsidyType: 'ghs', ghsHouses: houses, panelType: 'DCR' }, name);
  }
  add('rooftop:non-dcr', { systemSizeKw: 3, subsidyType: 'rooftop', panelType: 'NON_DCR' });
  write('core_subsidy.json', { engine: 'subsidy', version: subsidy.SUBSIDY_ENGINE_VERSION, sources: HEADER_SOURCES('subsidyEngine.js') }, cases, { configs: SUBSIDY_CONFIGS });
}

// ---- finance ------------------------------------------------------------------------------------------------------
{
  const cases = [];
  const add = (id, params, configName = 'real') => {
    const args = { ...params, financeConfig: configName === null ? null : FINANCE_CONFIGS[configName] };
    cases.push({ id, fn: 'calculateFinance', config: configName, input: plain(params), output: plain(finance.calculateFinance(args)) });
  };
  const principals = [0, 1, 999, 49999.5, 100000, 150000, 199999.99, 200000, 200000.004, 200000.01, 250000, 500000, 1234567.89];
  for (const principal of principals) {
    for (const tenureYears of [undefined, 1, 3, 5, 7, 10]) add(`tiers:${principal}:${tenureYears}`, { principal, tenureYears, calculatedAt: FIXED_NOW });
  }
  for (const rate of [0, 0.5, 5.65, 5.75, 7, 7.9, 8, 8.25, 8.8, 9.25, 12, 18]) {
    for (const tenureYears of [1, 5, 10]) {
      for (const principal of [150000, 250000]) add(`rate:${rate}:${tenureYears}:${principal}`, { principal, annualInterestRate: rate, tenureYears, calculatedAt: FIXED_NOW });
    }
  }
  for (const principal of [120000, 100001, 60, 7]) {
    for (const tenureYears of [1, 10]) add(`zero-rate:${principal}:${tenureYears}`, { principal, annualInterestRate: 0, tenureYears, calculatedAt: FIXED_NOW });
  }
  for (const principal of [-1, null, 'abc', '1000', true]) add(`principal-invalid:${JSON.stringify(principal)}`, { principal, calculatedAt: FIXED_NOW });
  for (const rate of [-0.5, 18.01, 25, '7']) add(`rate-invalid:${JSON.stringify(rate)}`, { principal: 150000, annualInterestRate: rate, calculatedAt: FIXED_NOW });
  for (const tenureYears of [0, 0.5, 11, 12, '5', 2.5, 1.25, null]) add(`tenure:${JSON.stringify(tenureYears)}`, { principal: 150000, tenureYears, calculatedAt: FIXED_NOW });
  add('passthrough', { principal: 128100, calculatedAt: FIXED_NOW, downPaymentPercentage: 10, downPaymentAmount: 22900, grossQuotationAmount: 229000, postSubsidyInvestment: 151000 });
  add('config-missing', { principal: 150000, calculatedAt: FIXED_NOW }, null);
  for (const name of Object.keys(FINANCE_CONFIGS).filter((n) => n !== 'real')) {
    for (const principal of [0, 50000, 150000, 200000.005, 250000, 400000]) add(`config:${name}:${principal}`, { principal, calculatedAt: FIXED_NOW }, name);
    for (const [rate, tenureYears] of [[10, 5], [15, 8], [5.75, 1]]) add(`config:${name}:rate${rate}:t${tenureYears}`, { principal: 150000, annualInterestRate: rate, tenureYears, calculatedAt: FIXED_NOW }, name);
  }
  // resolveFinanceResult (down payment on the ORIGINAL gross before subsidy; the principal is gross − dp − subsidy)
  const subsidyResults = {
    none: null,
    eligible78000: plain(subsidy.calculateSubsidy({ systemSizeKw: 3, subsidyConfig: SUBSIDY_CONFIG })),
    eligible30000: plain(subsidy.calculateSubsidy({ systemSizeKw: 1, subsidyConfig: SUBSIDY_CONFIG })),
    giveItUp: plain(subsidy.calculateSubsidy({ systemSizeKw: 3, panelType: 'NON_DCR', subsidyConfig: SUBSIDY_CONFIG })),
    ineligible: plain(subsidy.calculateSubsidy({ systemSizeKw: 11, subsidyConfig: SUBSIDY_CONFIG })),
  };
  for (const gross of [229000, 230050, 150000, 80000, 50000, 0, -1, null]) {
    for (const [subsidyName, subsidyResult] of Object.entries(subsidyResults)) {
      for (const configName of ['real', 'downPayment20', 'noDownPayment', 'emiRules']) {
        const snapshot = gross === null ? null : { status: commercialSnapshot.COMMERCIAL_SNAPSHOT_STATUS.ISSUED, pricing: { customerTotalIncludingGST: gross } };
        const output = resolveFinanceResult(snapshot, subsidyResult, FINANCE_CONFIGS[configName]);
        cases.push({ id: `resolve:${gross}:${subsidyName}:${configName}`, fn: 'resolveFinanceResult', config: configName, input: { gross, subsidy: subsidyName }, output: output === null ? null : plain(output) });
      }
    }
  }
  cases.push({ id: 'resolve:no-config', fn: 'resolveFinanceResult', config: null, input: { gross: 229000, subsidy: 'none' }, output: resolveFinanceResult({ status: 'ISSUED', pricing: { customerTotalIncludingGST: 229000 } }, null, null) });
  const draft = resolveFinanceResult({ status: 'DRAFT', pricing: { customerTotalIncludingGST: 229000 } }, null, FINANCE_CONFIG);
  if (draft !== null) throw new Error('a DRAFT snapshot must not be financed');
  write('core_finance.json', { engine: 'finance', version: finance.FINANCE_ENGINE_VERSION, sources: HEADER_SOURCES('financeEngine.js', 'quotationWorkspace.js'), resolveFinanceResultSha256: crypto.createHash('sha256').update(resolveFinanceSource).digest('hex') }, cases, {
    configs: FINANCE_CONFIGS,
    subsidyResults,
  });
}
