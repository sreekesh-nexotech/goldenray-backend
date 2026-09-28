#!/usr/bin/env node
// Golden parity files for the commercial engines (engines-commercial).
//
// Runs the REAL Flarize JavaScript (read-only, from FLARIZE_ROOT) against the real data files in
// FLARIZE_ROOT/data plus synthetic edge cases, and writes:
//   fixtures/flarize_commercial.json   the Flarize data the Python tests need (catalog, pack config store, the
//                                      package registry reduced to the records buildBom can match, battery master,
//                                      cost config, procurement price master, rate card, two procurement batches)
//   commercial_<module>.json           {generator, fixedNow, sources{file: sha256}, fixtures{…}, cases[…]}
//
// A case is {id, fn, args[...]} (+ env for buildBom) with either `result` or `error` {name, message, code, detail}.
// Placeholders inside args: {"$ref": name} (a fixture), {"$js": "undefined"|"NaN"|"Infinity"|"-Infinity"},
// {"$bomLines": caseId} (the lines of a buildBom case result). The clock is frozen at FIXED_ISO.
//
// Usage: node engines/tests/golden/generate_commercial.mjs [FLARIZE_ROOT]
// (defaults to $FLARIZE_ROOT or /home/user/flarize-main/flarize). Nothing is written outside this directory.

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(process.argv[2] || process.env.FLARIZE_ROOT || '/home/user/flarize-main/flarize');
const FIXED_ISO = '2026-09-20T10:00:00.000Z';
const FIXED_MS = Date.parse(FIXED_ISO);

// ---- frozen clock -------------------------------------------------------------------------------------------
const RealDate = Date;
class FixedDate extends RealDate {
  constructor(...args) { if (args.length === 0) super(FIXED_MS); else super(...args); }
  static now() { return FIXED_MS; }
}
globalThis.Date = FixedDate;

// ---- sources ------------------------------------------------------------------------------------------------
const SOURCES = [
  'server-bom-builder.js', 'src/lib/deviceAllocation.js', 'src/lib/packPricing.js', 'src/lib/packConfig.js', 'src/lib/rbac.js',
  'src/lib/costEngine.js', 'src/lib/pricingEngine.js', 'src/lib/offerLifecycle.js', 'src/lib/batteryCompatibility.js',
  'src/lib/resolveBattery.js', 'src/lib/batteryMaster.js', 'src/lib/procurementBatch.js', 'src/lib/procurementPriceMaster.js',
  'src/lib/projectRateCard.js', 'src/lib/commercialHistory.js', 'src/lib/packageApproval.js', 'src/lib/packageAuthority.js',
  'src/lib/packageProjection.js', 'src/lib/componentIdentity.js', 'src/lib/money.js',
  'data/catalog.json', 'data/pack-config.json', 'data/packages.proposed.json', 'data/battery-master.json', 'data/cost-config.json',
  'data/procurement-price-master.json', 'data/project-rate-card.json', 'data/procurement-state.json',
];
const sha = (p) => crypto.createHash('sha256').update(fs.readFileSync(path.join(ROOT, p))).digest('hex');
const sourceHashes = Object.fromEntries(SOURCES.map((p) => [p, sha(p)]));

const readData = (f) => JSON.parse(fs.readFileSync(path.join(ROOT, 'data', f), 'utf8'));
const catalog = readData('catalog.json');
const packStore = readData('pack-config.json');
const registryFull = readData('packages.proposed.json');
const batteryMasterFile = readData('battery-master.json');
const costConfig = readData('cost-config.json');
const priceMaster = readData('procurement-price-master.json');
const rateCardFile = readData('project-rate-card.json');
const procurementState = readData('procurement-state.json');

const req = createRequire(path.join(ROOT, 'package.json'));
const lib = (name) => pathToFileURL(path.join(ROOT, 'src/lib', name)).href;
const bb = req(path.join(ROOT, 'server-bom-builder.js'));
const DA = req(path.join(ROOT, 'src/lib/deviceAllocation.js'));
const PP = await import(lib('packPricing.js'));
const PC = await import(lib('packConfig.js'));
const CE = await import(lib('costEngine.js'));
const PE = await import(lib('pricingEngine.js'));
const OF = await import(lib('offerLifecycle.js'));
const BC = await import(lib('batteryCompatibility.js'));
const RB = await import(lib('resolveBattery.js'));
const BM = await import(lib('batteryMaster.js'));
const PB = await import(lib('procurementBatch.js'));
const PM = await import(lib('procurementPriceMaster.js'));
const RC = await import(lib('projectRateCard.js'));
const CH = await import(lib('commercialHistory.js'));
const PAU = await import(lib('packageAuthority.js'));
const PPR = await import(lib('packageProjection.js'));
const MN = await import(lib('money.js'));
let paInstance = 0;
const freshPackageApproval = () => import(`${lib('packageApproval.js')}?instance=${++paInstance}`);  // fresh _idSeq per scenario

// ---- helpers ------------------------------------------------------------------------------------------------
const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
const EMAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/;
function assertNoPersonalData(name, value) {
  if (EMAIL.test(JSON.stringify(value))) throw new Error(`fixture ${name} contains an e-mail address; mask it`);
}
const pick = (o, keys) => Object.fromEntries(keys.filter((k) => o[k] !== undefined).map((k) => [k, o[k]]));

// Registry reduced to the first package per (systemType, size, tier, phase): the only record findRegistryPackage returns.
const seenCombos = new Set();
const reducedPackages = [];
for (const p of registryFull.packages || []) {
  const key = [p.systemType, p.size, p.tier, p.phase].map(String).join('|');
  if (seenCombos.has(key)) continue;
  seenCombos.add(key);
  reducedPackages.push({
    ...pick(p, ['packageId', 'systemType', 'size', 'tier', 'phase']),
    components: (p.components || []).map((c) => pick(c, ['role', 'componentId', 'pinnedComponentId', 'derivedBy', 'approvedAlternates'])),
  });
}

// Pack config with a market rate for every pack key/size and an installation row for hybrid "5" (exercises every path).
const fullRatesConfig = clone(packStore.approved.config);
for (const [key, sizes] of Object.entries(fullRatesConfig.marketRates)) {
  if (key.startsWith('upgrade')) continue;
  const sys = key.startsWith('hybrid') ? 'hybrid' : 'ongrid';
  for (const size of Object.keys(fullRatesConfig.bomTemplates[sys].sizes)) {
    const kw = Number(String(size).replace(/[^0-9.]/g, '')) || 0;
    const tierBonus = key.includes('premium') ? 90000 : key.includes('value') ? 30000 : 0;
    sizes[size] = Math.round(120000 + kw * 36500 + tierBonus + (sys === 'hybrid' ? 150000 : 0) + (key.includes('_up') ? 17500.5 : 0));
  }
}
fullRatesConfig.installationMatrix['5'] = { flat: 22000, sheet: 26000, elevated: 32000 };

const batches = Object.values(procurementState.batches || {})
  .filter((b) => ['BATCH-SEED-003', 'BATCH-SEED-005'].includes(b.batchId))
  .map((b) => ({ batchId: b.batchId, deliveryCost: b.deliveryCost, otherChargesDeclared: b.otherChargesDeclared, lines: b.lines.map((l) => pick(l, ['componentId', 'quantity', 'purchaseUnitPrice', 'otherProcurementCharges'])) }));

const FIXTURES = {
  catalog,
  packStore,
  registry: { schema: registryFull.schema ?? null, packages: reducedPackages },
  batteryMaster: batteryMasterFile.batteries,
  costConfig,
  priceMaster,
  rateCard: rateCardFile,
  fullRatesConfig,
};
for (const [name, value] of Object.entries(FIXTURES)) assertNoPersonalData(name, value);

function resolveRef(name, local = {}) {
  const [head, ...rest] = name.split('.');
  let value = local[head] ?? FIXTURES[head] ?? SYNTH[head];
  for (const key of rest) value = value?.[key];
  if (value === undefined) throw new Error(`unknown fixture ${name}`);
  return clone(value);
}
function resolveArgs(value, local = {}) {
  if (Array.isArray(value)) return value.map((v) => resolveArgs(v, local));
  if (value && typeof value === 'object') {
    if ('$ref' in value) return resolveRef(value.$ref, local);
    if ('$js' in value) return { undefined, NaN, Infinity, '-Infinity': -Infinity }[value.$js];
    if ('$bomLines' in value) return clone(BOM_RESULTS.get(value.$bomLines).lines);
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, resolveArgs(v, local)]));
  }
  return value;
}
const BOM_RESULTS = new Map();
const SYNTH = {};

function errorOf(e) {
  if (e instanceof TypeError || e instanceof ReferenceError || e instanceof RangeError) {
    throw new Error(`a golden case crashed the JavaScript itself (${e.constructor.name}: ${e.message}); drop the case`);
  }
  return { name: e.name, message: e.message, code: e.code ?? null, detail: e.detail === undefined ? null : clone(e.detail) };
}
function capture(fn) {
  try {
    const value = fn();
    return value === undefined ? { result: { $js: 'undefined' } } : { result: clone(value) };
  } catch (e) {
    return { error: errorOf(e) };
  }
}

// Minified JSON, one document per file (git stores it zlib-compressed; `python -m json.tool` pretty-prints one).
function writeJson(file, doc) {
  fs.writeFileSync(file, JSON.stringify(doc) + '\n');
}
function writeGolden(name, cases, extra = {}) {
  const ids = new Set();
  for (const c of cases) {
    if (ids.has(c.id)) throw new Error(`duplicate case id ${c.id}`);
    ids.add(c.id);
  }
  const doc = { generator: 'engines/tests/golden/generate_commercial.mjs', fixedNow: FIXED_ISO, sources: sourceHashes, ...extra, cases };
  writeJson(path.join(HERE, `commercial_${name}.json`), doc);
  return cases.length;
}

// Call a JS function with resolved args and record the case.
function call(cases, id, fn, impl, args, local = {}) {
  const resolved = resolveArgs(clone(args), local);
  cases.push({ id, fn, args, ...capture(() => impl(...resolved)) });
}

const counts = {};

// =============================================================================================================
// money.js
// =============================================================================================================
{
  const cases = [];
  const values = [0, 1, 0.5, 1.5, 2.5, -0.5, -1.5, -2.5, 1.005, 2.675, 1234.5, 99999.49999999999, 0.49999999999999994, 1e15 + 0.5, 1e21,
    -1e-7, 229000 / 1.089, 13585 * 6 * 0.05, 8.9, '12.5', '  7 ', '', 'abc', '1e3', '0x1F', true, false, null, { $js: 'undefined' },
    { $js: 'NaN' }, { $js: 'Infinity' }, [], [3], {}];
  values.forEach((v, i) => {
    call(cases, `roundMoney/${i}`, 'roundMoney', MN.roundMoney, [v]);
    call(cases, `isMoney/${i}`, 'isMoney', MN.isMoney, [v]);
    call(cases, `mulExact/${i}`, 'mulExact', MN.mulExact, [v, 1.089]);
  });
  const lists = [[], [1, 2, 3], [0.1, 0.2, 0.3], ['5', null, 'x', 2.5], [1e16, 1, -1e16], [13585 * 6, 1998.3, 5000.7], null];
  lists.forEach((l, i) => call(cases, `sumExact/${i}`, 'sumExact', MN.sumExact, [l]));
  counts.money = writeGolden('money', cases);
}

// =============================================================================================================
// deviceAllocation.js
// =============================================================================================================
{
  const cases = [];
  const en1 = { componentId: 'en1', deviceType: 'microinverter', panelsPerDevice: 1, name: 'IQ8P Micro Inverter', price: 8200 };
  const hm = [
    { componentId: 'hm2', deviceType: 'microinverter', panelsPerDevice: 2, name: 'HMS-1000', price: 14000 },
    { componentId: 'hm1', deviceType: 'microinverter', panelsPerDevice: 4, name: 'HMS-2000', price: 22000 },
  ];
  const odd = [{ componentId: 'c3', deviceType: 'optimizer', panelsPerDevice: 3 }, { componentId: 'c5', deviceType: 'optimizer', panelsPerDevice: 5, price: 0 }];
  const only4 = [{ componentId: 'q4', deviceType: 'microinverter', panelsPerDevice: 4, name: '' }];
  const ties = [{ componentId: 'tA', deviceType: 'microinverter', panelsPerDevice: 2 }, { componentId: 'tB', deviceType: 'microinverter', panelsPerDevice: 2 }];
  const mix = [{ componentId: 'm6', deviceType: 'microinverter', panelsPerDevice: 6 }, { componentId: 'm4', deviceType: 'microinverter', panelsPerDevice: 4 }, { componentId: 'm1', deviceType: 'microinverter', panelsPerDevice: 1 }];
  const invalid = [
    { componentId: 's1', deviceType: 'string_inverter', panelsPerDevice: 1 }, { componentId: '', deviceType: 'microinverter', panelsPerDevice: 2 },
    { componentId: 'x', panelsPerDevice: 2 }, { componentId: 'y', deviceType: 'optimizer', panelsPerDevice: 0 },
    { componentId: 'z', deviceType: 'optimizer', panelsPerDevice: 1.5 }, { componentId: 'n', deviceType: 'optimizer' }, null, 'str', [1, 2],
  ];
  const withString = [...invalid, { componentId: 'w', deviceType: 'optimizer', panelsPerDevice: '2', name: 'W2', price: 5 }];
  const sets = { en1: [en1], hm, odd, only4, ties, mix, invalid, withString };
  for (const [name, candidates] of Object.entries(sets)) {
    for (let panels = 0; panels <= 23; panels += name === 'en1' || name === 'invalid' ? 5 : 1) {
      call(cases, `allocate/${name}/${panels}`, 'allocateDevices', DA.allocateDevices, [{ panels, candidates }]);
    }
  }
  for (const [i, panels] of [-1, 2.5, 'abc', null, '12', true, { $js: 'undefined' }, { $js: 'NaN' }, 1e3].entries()) {
    call(cases, `allocate/panels-invalid/${i}`, 'allocateDevices', DA.allocateDevices, [{ panels, candidates: hm }]);
  }
  call(cases, 'allocate/no-candidates', 'allocateDevices', DA.allocateDevices, [{ panels: 6, candidates: [] }]);
  call(cases, 'allocate/candidates-not-array', 'allocateDevices', DA.allocateDevices, [{ panels: 6, candidates: { a: 1 } }]);
  call(cases, 'allocate/no-args', 'allocateDevices', DA.allocateDevices, [{}]);
  counts.device_allocation = writeGolden('device_allocation', cases);
}

// =============================================================================================================
// server-bom-builder.js
// =============================================================================================================
const CATALOG_FILE = path.join(ROOT, 'data', 'catalog.json');
const REGISTRY_FILE = path.join(ROOT, 'data', 'packages.proposed.json');
const fsCjs = req('fs');
const realRead = fsCjs.readFileSync;
const overrides = new Map();
fsCjs.readFileSync = function patchedRead(p, ...rest) {
  const key = typeof p === 'string' ? path.resolve(p) : p;
  if (overrides.has(key)) return overrides.get(key);
  return realRead.call(this, p, ...rest);
};

function synthCatalog() {
  const all = ['base', 'value', 'premium'];
  return {
    categories: {
      panel: { label: 'Panel', gstDefault: 5, items: [
        { id: 'p_a', name: 'Panel A 540', brand: 'A', watt: 540, price: 12000, tiers: all },
        { id: 'p_b', name: 'Panel B 400', brand: 'B', watt: 400, price: 9000.5, tiers: ['value', 'premium'] },
        { id: 'p_c', name: 'Panel C 600', watt: 600, price: 15000, tiers: ['premium'], status: 'TEST' },
        { id: 'p_d', name: 'Panel D', price: 5000, tiers: ['base'] },
        { id: 'p_e', name: 'Panel E 455', watt: 455, price: 0, tiers: all, approvalStatus: 'PROPOSED' },
      ] },
      inverter: { label: 'Inverter', gstDefault: 5, items: [
        { id: 'i_1', name: 'Inv 3 1P', kw: 3, phase: '1P', type: 'ongrid', price: 20000, tiers: all },
        { id: 'i_2', name: 'Inv 5 3P', kw: 5, phase: '3P', type: 'ongrid', price: 30000, tiers: all },
        { id: 'i_3', name: 'Hyb 5 1P', kw: 5, phase: '1P', type: 'hybrid', price: 60000, tiers: all },
        { id: 'i_4', name: 'Hyb 8 3P', kw: 8, phase: '3P', type: 'hybrid', price: 90000, tiers: all },
        { id: 'i_5', name: 'Micro kit', kw: 4, phase: '1P', type: 'micro', price: 1, tiers: ['premium'] },
        { id: 'i_6', name: 'Inv 6 3P', kw: 6, phase: '3P', price: 34000, tiers: ['value', 'premium'] },
        { id: 'i_7', name: 'Inv nokw', phase: '1P', type: 'ongrid', price: 1000, tiers: ['base'] },
      ] },
      dcdb: { label: 'DCDB', gstDefault: 18, items: [
        { id: 'd_1', name: 'DCDB 1P HYB', phase: '1P-HYB', price: 3000, tiers: all },
        { id: 'd_2', name: 'DCDB 3P HYB', phase: '3P-HYB', price: 4000, tiers: all },
        { id: 'd_3', name: 'DCDB 1P', phase: '1P', price: 2000, tiers: all },
        { id: 'd_4', name: 'DCDB any', price: 2500, tiers: ['value'] },
      ] },
      meter: { label: 'Meter', gstDefault: 18, items: [{ id: 'mt1', name: 'Meter', price: 2500, tiers: all }] },
      battery: { label: 'Battery', gstDefault: 18, items: [
        { id: 'b_1', name: 'Battery 1', price: 80000, tiers: ['value', 'premium'] },
        { id: 'b_2', name: 'Battery 2', price: 95000, tiers: ['premium'] },
      ] },
      mccb_box: { label: 'MCCB', gstDefault: 18, items: [{ id: 'mb_1', name: 'MCCB', price: 8500, tiers: ['base', 'value'] }] },
      enphase: { label: 'Micro', gstDefault: 5, items: [
        { id: 'e_m4', name: 'Micro 4', deviceType: 'microinverter', panelsPerDevice: 4, price: 20000, tiers: ['premium'] },
        { id: 'e_m2', name: 'Micro 2', deviceType: 'microinverter', panelsPerDevice: 2, price: 11000, tiers: ['premium'] },
        { id: 'e_m15', name: 'Bad micro', deviceType: 'microinverter', panelsPerDevice: 1.5, price: 1 },
        { id: 'e_cab', name: 'Cable', microAccessoryRole: 'cable_per_panel', price: 700, gstOverride: 18 },
        { id: 'e_gw', name: 'Gateway', microAccessoryRole: 'gateway', price: 12000 },
        { id: 'e_rl', name: 'Relay', microAccessoryRole: 'relay', price: 5150 },
        { id: 'e_tm', name: 'Terminal', microAccessoryRole: 'terminal', price: 700 },
        { id: 'e_ct', name: 'CT', microAccessoryRole: 'ct', price: 1500 },
        { id: 'e_sc', name: 'Controller', microAccessoryRole: 'system_controller', price: 110000, gstOverride: 18 },
      ] },
      ug_cable: { label: 'UG', gstDefault: 18, items: [
        { id: 'ug2', name: 'UG 2 core', microAccessoryRole: 'cable_2core', price: 150 },
        { id: 'ug4', name: 'UG 4 core', microAccessoryRole: 'cable_4core', price: 250 },
      ] },
    },
    bomTemplates: {
      ongrid: {
        sizes: { 3: '3 kW', 5: '5 kW', 6: '6 kW', '5sp': '5 kW 1P' }, tiers: all, threePhase: ['6'],
        slots: [
          { pos: 0, category: 'panel', label: 'Panel', gst: 5, qty: { 3: 6, 5: 9, 6: 11, '5sp': 9 }, defaults: { 'value:3': 'p_b', 3: 'p_a', 'premium:*': 'p_b', '*': 'p_a' }, alternatives: ['p_a', 'p_b'] },
          { pos: 1, category: 'inverter', label: 'Inverter', filterType: 'ongrid', qty: { 3: 1, 5: 1, 6: 1, '5sp': 1 }, premiumQty: { 3: 0, 5: 0, 6: 0, '5sp': 0 } },
          { pos: 2, category: 'dcdb', qty: { 3: 1, 5: 1, 6: 1, '5sp': 1 }, salesSwap: false, defaults: { 'base:*': 'd_3' } },
          { pos: 3, category: 'meter', label: 'Meter', qty: { 3: 1, 5: 1, 6: 2 }, salesSwap: true },
          { pos: 4, category: 'nosuch', label: 'Ghost', qty: { 3: 1 } },
        ],
        fixedItems: [
          { id: 'fx_mc4', name: 'MC4', price: 56, qty: { 3: 3, 5: 4, 6: 5, '5sp': 4 }, premiumQty: { 3: 1, 5: 2, 6: 2 } },
          { name: 'Pipe', price: 45.5, gst: 12, qty: { 3: 1.5, 5: 2.5, 6: 3 } },
          { id: 'fx_zero', name: 'Zero', price: 10, qty: { 3: 0 } },
        ],
      },
      hybrid: {
        sizes: { 3: '3 kW', 5: '5 kW', 8: '8 kW' }, tiers: all, threePhase: ['8'], batteryConfigs: ['0', '1', '2'],
        slots: [
          { pos: 0, category: 'panel', qty: { 3: 6, 5: 9, 8: 15 } },
          { pos: 1, category: 'inverter', filterType: 'hybrid', qty: { 3: 1, 5: 1, 8: 1 }, premiumQty: { 3: 0, 5: 0, 8: 0 } },
          { pos: 2, category: 'dcdb', filterPhase: 'HYB', qty: { 3: 1, 5: 1, 8: 1 }, salesSwap: true },
          { pos: 3, category: 'mccb_box', batQty: { 0: { 3: 0, 5: 0, 8: 0 }, 1: { 3: 1, 5: 1, 8: 1 }, 2: { 3: 1, 5: 1, 8: 2 } }, premiumBatQty: { 0: { 3: 0, 5: 0, 8: 0 } } },
          { pos: 4, category: 'battery', batQty: { 0: { 3: 0 }, 1: { 3: 1 }, 2: { 3: 2 } } },
          { pos: 5, category: 'meter', batQty: { 0: { 3: 1, 5: 1, 8: 1 }, 1: { 3: 2, 5: 2, 8: 2 } }, premiumBatQty: { 0: { 3: 2, 5: 2, 8: 2 } } },
        ],
        fixedItems: [
          { id: 'fx_co', name: 'Change Over', price: 3500, batQty: { 0: { 3: 0, 5: 0, 8: 0 }, 1: { 3: 1, 5: 1, 8: 1 }, 2: { 3: 1, 5: 1, 8: 1 } }, premiumQty: { 3: 1, 5: 1, 8: 0 } },
          { id: 'fx_bc', name: 'Battery Cable', price: 180, batQty: { 1: { 3: 5, 5: 5, 8: 5 }, 2: { 3: 5, 5: 5, 8: 10 } } },
        ],
      },
    },
    packageProfiles: {
      ongrid_base: { batteryIncluded: false, inverterType: 'ongrid' },
      ongrid_value: { batteryIncluded: false, inverterType: 'ongrid' },
      ongrid_premium: { batteryIncluded: false, inverterType: 'micro' },
      hybrid_base: { batteryIncluded: false, batteryQuantity: 0, inverterType: 'hybrid' },
      hybrid_value: { batteryIncluded: true, batteryQuantity: 2, inverterType: 'hybrid' },
      hybrid_premium: { batteryIncluded: true, inverterType: 'micro' },
    },
  };
}
function synthRegistry() {
  return { packages: [
    { packageId: 'SYN-1', systemType: 'ongrid', size: '5', tier: 'value', phase: '1P', components: [
      { role: 'panel', componentId: 'p_b', derivedBy: 'explicit', approvedAlternates: ['p_a'] },
      { role: 'inverter', pinnedComponentId: 'i_1', derivedBy: 'fallback:nearestKw', approvedAlternates: [] },
      { role: 'meter', componentId: 'mt1', derivedBy: 'template' },
    ] },
    { packageId: 'SYN-1b', systemType: 'ongrid', size: '5', tier: 'value', phase: '1P', components: [{ role: 'panel', componentId: 'p_a', derivedBy: 'explicit' }] },
    { packageId: 'SYN-2', systemType: 'hybrid', size: '5', tier: 'base', phase: '1P', components: [
      { role: 'inverter', componentId: 'i_3', derivedBy: 'explicit', approvedAlternates: ['i_4'] },
      { role: 'dcdb', componentId: 'd_9', derivedBy: 'explicit' },
    ] },
  ] };
}
Object.assign(SYNTH, { synthCatalog: synthCatalog(), synthRegistry: synthRegistry() });

function runBom(fnName, config, env) {
  const cat = resolveRef(env.catalog.$ref);
  const reg = env.registry === null ? {} : resolveRef(env.registry.$ref);
  const packConfig = env.packConfig === 'store' ? ((src) => (src === 'draft' ? packStore.draft.config : packStore.approved.config))
    : env.packConfig === null ? (() => null)
      : (() => resolveRef(env.packConfig.$ref));
  overrides.clear();
  if (env.catalog?.$ref !== 'catalog') overrides.set(CATALOG_FILE, JSON.stringify(cat));
  if (env.registry === null || env.registry?.$ref !== 'registry') overrides.set(REGISTRY_FILE, JSON.stringify(reg));
  bb.setPackConfigProvider(packConfig);
  try {
    return capture(() => bb[fnName](clone(config)));
  } finally {
    overrides.clear();
  }
}

const bomCases = [];
function bomCase(id, fn, config, env) {
  const out = runBom(fn, config, env);
  bomCases.push({ id, fn, args: [config], env, ...out });
  if (fn === 'buildBom' && out.result) BOM_RESULTS.set(id, out.result);
  return out;
}
const REAL = { catalog: { $ref: 'catalog' }, registry: { $ref: 'registry' }, packConfig: 'store' };
const SYN = { catalog: { $ref: 'synthCatalog' }, registry: { $ref: 'synthRegistry' }, packConfig: null };

// Every pack of the approved configuration (the same enumeration as server-pack-publish.js), per battery quantity.
const approved = packStore.approved.config;
const packs = [];
for (const [systemType, t] of Object.entries(approved.bomTemplates)) {
  if (systemType === 'upgrade') continue;
  for (const size of Object.keys(t.sizes || {})) {
    const phase = (t.threePhase || []).includes(size) ? '3P' : '1P';
    const variants = [null];
    for (const p of approved.futureUpgrade?.pairs || []) {
      if ((p.systemType || systemType) !== systemType || String(p.panelSize) !== String(size)) continue;
      if (!t.sizes[String(p.systemSize)]) continue;
      if (((t.threePhase || []).includes(String(p.systemSize)) ? '3P' : '1P') !== phase) continue;
      variants.push(String(p.systemSize));
    }
    for (const tier of t.tiers || []) {
      for (const fr of variants) {
        for (const bat of systemType === 'hybrid' ? [null, 0, 1, 2] : [null]) {
          packs.push({ systemType, size, tier, phase, fr, bat });
        }
      }
    }
  }
}
for (const p of packs) {
  const id = `pack/${p.systemType}/${p.size}/${p.tier}${p.fr ? `/fr${p.fr}` : ''}${p.bat === null ? '' : `/bat${p.bat}`}`;
  const config = { systemType: p.systemType, size: p.size, tier: p.tier, phase: p.phase, configSource: 'approved', actorRole: 'PROJECT_HEAD' };
  if (p.fr) config.futureSystemSize = p.fr;
  if (p.bat !== null) config.batteryQuantity = p.bat;
  p.caseId = id;
  bomCase(id, 'buildBom', config, REAL);
}
// Sales swaps and refusals on the approved configuration.
const swaps = [
  ['swap/value3-p7-sales', { systemType: 'ongrid', size: '3', tier: 'value', phase: '1P', actorRole: 'SALES', selections: { panel: 'p7' } }],
  ['swap/value3-p2-sales-default', { systemType: 'ongrid', size: '3', tier: 'value', actorRole: 'SALES', selections: { panel: 'p2' } }],
  ['swap/base3-p7-sales', { systemType: 'ongrid', size: '3', tier: 'base', actorRole: 'SALES', selections: { panel: 'p7' } }],
  ['swap/value5sp-i97-head', { systemType: 'ongrid', size: '5sp', tier: 'value', actorRole: 'SALES_HEAD', selections: { inverter: 'i97' } }],
  ['swap/value3-p4-sales-not-approved', { systemType: 'ongrid', size: '3', tier: 'value', actorRole: 'SALES', selections: { panel: 'p4' } }],
  ['swap/value3-p4-ph', { systemType: 'ongrid', size: '3', tier: 'value', actorRole: 'PROJECT_HEAD', selections: { panel: 'p4' } }],
  ['swap/value3-dcdb-sales-not-permitted', { systemType: 'ongrid', size: '3', tier: 'value', actorRole: 'FIELD_SALES', selections: { dcdb: 'd1' } }],
  ['swap/value3-dcdb-ph', { systemType: 'ongrid', size: '3', tier: 'value', actorRole: 'PROJECT_HEAD', selections: { dcdb: 'd1' } }],
  ['swap/value3-acdb-sales', { systemType: 'ongrid', size: '3', tier: 'value', actorRole: 'SALES', selections: { acdb: 'a2' } }],
  ['swap/value3-unknown-component', { systemType: 'ongrid', size: '3', tier: 'value', selections: { panel: 'zzz' } }],
  ['swap/value3-wrong-tier', { systemType: 'ongrid', size: '3', tier: 'value', selections: { panel: 'p1' } }],
  ['swap/value5tp-1p-inverter', { systemType: 'ongrid', size: '5tp', tier: 'value', selections: { inverter: 'i20' } }],
  ['swap/hybrid5-panel-sales', { systemType: 'hybrid', size: '5', tier: 'value', actorRole: 'SALES_CRS', selections: { panel: 'p4' } }],
  ['swap/premium3-p5', { systemType: 'ongrid', size: '3', tier: 'premium', actorRole: 'PROJECT_HEAD', selections: { panel: 'p5' } }],
  ['swap/premium10-p9', { systemType: 'ongrid', size: '10', tier: 'premium', phase: '3P', selections: { panel: 'p9' } }],
];
for (const [id, cfg] of swaps) bomCase(`bom/${id}`, 'buildBom', { configSource: 'approved', ...cfg }, REAL);
const refusals = [
  ['ongrid-3-3P', { systemType: 'ongrid', size: '3', tier: 'value', phase: '3P' }],
  ['hybrid-3-3P', { systemType: 'hybrid', size: '3', tier: 'value', phase: '3P' }],
  ['fr-invalid-size', { systemType: 'ongrid', size: '3', tier: 'value', futureSystemSize: '7' }],
  ['fr-phase-change', { systemType: 'ongrid', size: '3', tier: 'value', futureSystemSize: '6' }],
  ['invalid-size', { systemType: 'ongrid', size: '4', tier: 'value' }],
  ['invalid-tier', { systemType: 'ongrid', size: '3', tier: 'gold' }],
  ['no-template', { systemType: 'offgrid', size: '3', tier: 'value' }],
  ['upgrade-template', { systemType: 'upgrade', size: '3', tier: 'value' }],
  ['numeric-size', { systemType: 'ongrid', size: 3, tier: 'base' }],
  ['phase-forced-3P', { systemType: 'ongrid', size: '5sp', tier: 'value', phase: '3P' }],
  ['bat-quantity-3', { systemType: 'hybrid', size: '3', tier: 'value', batteryQuantity: 3 }],
  ['bat-quantity-string', { systemType: 'hybrid', size: '3', tier: 'value', batteryQuantity: '1' }],
  ['ongrid-with-bat', { systemType: 'ongrid', size: '3', tier: 'value', batteryQuantity: 2 }],
  ['draft-source', { systemType: 'ongrid', size: '8', tier: 'base', configSource: 'draft' }],
];
for (const [id, cfg] of refusals) bomCase(`bom/refusal/${id}`, 'buildBom', { configSource: 'approved', ...cfg }, REAL);
// Catalog only (no pack config): catalog templates, registry pins and nearest-kW defaults.
for (const [i, cfg] of [
  { systemType: 'ongrid', size: '3', tier: 'value' }, { systemType: 'ongrid', size: '5tp', tier: 'base' }, { systemType: 'ongrid', size: '10', tier: 'value' },
  { systemType: 'ongrid', size: '6', tier: 'premium' }, { systemType: 'hybrid', size: '5', tier: 'value' }, { systemType: 'hybrid', size: '10', tier: 'base', batteryQuantity: 1 },
  { systemType: 'hybrid', size: '3', tier: 'premium' }, { systemType: 'ongrid', size: '3', tier: 'value', actorRole: 'SALES', selections: { panel: 'p4' } },
].entries()) {
  bomCase(`bom/catalog-only/${i}`, 'buildBom', cfg, { ...REAL, packConfig: null });
  bomCase(`bom/no-registry/${i}`, 'buildBom', cfg, { ...REAL, registry: null });
}
// Synthetic catalog: every branch of slot quantities, defaults, filters, allocation and fixed items.
{
  let n = 0;
  for (const systemType of ['ongrid', 'hybrid']) {
    const sizes = systemType === 'ongrid' ? ['3', '5', '6', '5sp'] : ['3', '5', '8'];
    for (const size of sizes) {
      for (const tier of ['base', 'value', 'premium']) {
        const bats = systemType === 'hybrid' ? [undefined, 0, 1, 2] : [undefined];
        for (const bat of bats) {
          const cfg = { systemType, size, tier };
          if (bat !== undefined) cfg.batteryQuantity = bat;
          if (n % 5 === 1) cfg.phase = '3P';
          bomCase(`bom/synth/${systemType}/${size}/${tier}/${bat ?? 'default'}/${n++}`, 'buildBom', cfg, SYN);
        }
      }
    }
  }
  const synthSel = [
    { systemType: 'ongrid', size: '3', tier: 'value', actorRole: 'SALES', selections: { panel: 'p_a' } },
    { systemType: 'ongrid', size: '3', tier: 'value', actorRole: 'SALES', selections: { dcdb: 'd_4' } },
    { systemType: 'ongrid', size: '3', tier: 'base', actorRole: 'PROJECT_HEAD', selections: { panel: 'p_d' } },
    { systemType: 'ongrid', size: '3', tier: 'base', selections: { panel: 'p_c' } },
    { systemType: 'ongrid', size: '5', tier: 'value', actorRole: 'SALES', selections: { panel: 'p_a' } },
    { systemType: 'ongrid', size: '5', tier: 'value', actorRole: 'SALES', selections: { inverter: 'i_7' } },
    { systemType: 'ongrid', size: '5', tier: 'value', actorRole: 'SALES', selections: { meter: 'mt1' } },
    { systemType: 'ongrid', size: '6', tier: 'value', selections: { inverter: 'i_1' } },
    { systemType: 'hybrid', size: '5', tier: 'base', actorRole: 'SALES', selections: { inverter: 'i_4' } },
    { systemType: 'hybrid', size: '5', tier: 'base', actorRole: 'SALES', selections: { dcdb: 'd_3' } },
    { systemType: 'hybrid', size: '3', tier: 'premium', batteryQuantity: 2, futureSystemSize: '5' },
    { systemType: 'hybrid', size: '5', tier: 'premium', futureSystemSize: '8' },
    { systemType: 'ongrid', size: '3', tier: 'premium', futureSystemSize: '5' },
    { systemType: 'ongrid', size: '5sp', tier: 'value', futureSystemSize: '5' },
  ];
  synthSel.forEach((cfg, i) => bomCase(`bom/synth-selection/${i}`, 'buildBom', cfg, SYN));
}
// buildAllTierBoms and getAlternatives.
bomCase('all-tiers/ongrid-5sp', 'buildAllTierBoms', { systemType: 'ongrid', size: '5sp', configSource: 'approved', actorRole: 'SALES', tierSelections: { value: { panel: 'p7' } } }, REAL);
bomCase('all-tiers/synth-hybrid-3', 'buildAllTierBoms', { systemType: 'hybrid', size: '3', batteryQuantity: 1 }, SYN);
bomCase('all-tiers/invalid', 'buildAllTierBoms', { systemType: 'hybrid', size: '3', phase: '3P' }, REAL);
{
  let n = 0;
  const roles = [undefined, 'SALES', 'PROJECT_HEAD', 'ADMIN', 'SALES_HEAD'];
  for (const [systemType, sizes] of [['ongrid', ['3', '5sp', '5tp', '10']], ['hybrid', ['3', '8']]]) {
    for (const size of sizes) {
      for (const tier of ['base', 'value', 'premium']) {
        const cfg = { systemType, size, tier, configSource: 'approved' };
        const role = roles[n % roles.length];
        if (role) cfg.actorRole = role;
        if (systemType === 'hybrid') cfg.batteryQuantity = [0, 1, 2, null][n % 4];
        if (n % 7 === 3) cfg.category = 'panel';
        if (n % 9 === 4) cfg.futureSystemSize = systemType === 'ongrid' ? '5sp' : '5';
        bomCase(`alternatives/real/${n++}`, 'getAlternatives', cfg, REAL);
      }
    }
  }
  for (const [i, cfg] of [
    { systemType: 'ongrid', size: '3', tier: 'value', actorRole: 'ADMIN' }, { systemType: 'ongrid', size: '5', tier: 'value' },
    { systemType: 'ongrid', size: '6', tier: 'base', actorRole: 'PROJECT_HEAD' }, { systemType: 'hybrid', size: '5', tier: 'base', actorRole: 'PROJECT_HEAD' },
    { systemType: 'hybrid', size: '8', tier: 'value', actorRole: 'PROJECT_HEAD' }, { systemType: 'hybrid', size: '3', tier: 'premium', actorRole: 'ADMIN', batteryQuantity: 0 },
    { systemType: 'offgrid', size: '3', tier: 'value' },
  ].entries()) bomCase(`alternatives/synth/${i}`, 'getAlternatives', cfg, SYN);
}
for (const [i, [s, t]] of [['ongrid', 'base'], ['ongrid', 'premium'], ['hybrid', 'premium'], ['hybrid', 'value'], ['upgrade', 'value'], [undefined, undefined]].entries()) {
  bomCases.push({ id: `profileKey/${i}`, fn: 'getProfileKey', args: [s ?? { $js: 'undefined' }, t ?? { $js: 'undefined' }], ...capture(() => bb.getProfileKey(s, t)) });
}
for (const [i, role] of ['SALES', 'SALES_HEAD', 'SALES_CRS', 'FIELD_SALES', 'ADMIN', 'PROJECT_HEAD', null].entries()) {
  bomCases.push({ id: `isSalesRole/${i}`, fn: 'isSalesRole', args: [role], ...capture(() => bb.isSalesRole(role)) });
}
bomCases.push({ id: 'strip/value3', fn: 'stripCostFieldsForSales', args: [{ $bomLines: 'pack/ongrid/3/value' }], ...capture(() => bb.stripCostFieldsForSales(clone(BOM_RESULTS.get('pack/ongrid/3/value').lines))) });
counts.bom = writeGolden('bom', bomCases, { fixtures: SYNTH });
fsCjs.readFileSync = realRead;

// =============================================================================================================
// packPricing.js
// =============================================================================================================
{
  const cases = [];
  const distances = [0, 45, 100, 130, 250.5, 1000];
  let n = 0;
  for (const p of packs) {
    if (!BOM_RESULTS.has(p.caseId)) continue;
    const base = { systemType: p.systemType, size: p.size, tier: p.tier, batteryConfig: BOM_RESULTS.get(p.caseId).systemConfig.batteryQuantity, futureSystemSize: p.fr, lines: { $bomLines: p.caseId }, pricedAt: FIXED_ISO };
    for (const roof of ['FLAT', 'SHEET', 'ELEVATED']) {
      call(cases, `price/approved/${p.caseId}/${roof}`, 'pricePack', PP.pricePack, [{ ...base, config: { $ref: 'packStore.approved.config' }, roofType: roof, distanceKm: distances[n++ % distances.length] }]);
    }
    const roof = ['FLAT', 'SHEET', 'ELEVATED'][n % 3];
    call(cases, `price/full-rates/${p.caseId}/${roof}`, 'pricePack', PP.pricePack, [{ ...base, config: { $ref: 'fullRatesConfig' }, roofType: roof.toLowerCase(), distanceKm: distances[n++ % distances.length] }]);
  }
  for (const [id] of swaps) {
    const caseId = `bom/${id}`;
    const bom = BOM_RESULTS.get(caseId);
    if (!bom) continue;
    const sc = bom.systemConfig;
    for (const roof of ['FLAT', 'SHEET']) {
      call(cases, `price/${caseId}/${roof}`, 'pricePack', PP.pricePack, [{ config: { $ref: 'fullRatesConfig' }, systemType: sc.systemType, size: sc.size, tier: sc.tier, roofType: roof, distanceKm: 130, batteryConfig: sc.batteryQuantity, futureSystemSize: sc.futureReady ? sc.systemSize : null, lines: { $bomLines: caseId } }]);
    }
  }
  const twoVehicles = clone(approved);
  twoVehicles.transportConfig.vehicles.push({ vehicleType: 'LORRY', vehicleName: 'Lorry', ratePerKm: 55 });
  const bare = { marketRates: { ongrid_value: { 3: 229000 } }, installationMatrix: { 3: { flat: 15000, sheet: 18000 } }, transportConfig: { vehicles: [{ vehicleType: 'ACE' }] } };
  const edge = [
    ['roof-invalid', { roofType: 'TIN' }], ['roof-missing', { roofType: { $js: 'undefined' } }], ['roof-lower', { roofType: 'sheet' }],
    ['distance-negative', { distanceKm: -1 }], ['distance-text', { distanceKm: 'abc' }], ['distance-missing', { distanceKm: { $js: 'undefined' } }],
    ['distance-null', { distanceKm: null }], ['distance-string', { distanceKm: '130' }], ['vehicle-unknown', { vehicleType: 'LORRY' }],
    ['vehicle-required', { config: twoVehicles }], ['vehicle-lorry', { config: twoVehicles, vehicleType: 'LORRY' }],
    ['all-errors', { roofType: 'X', distanceKm: -5, size: '6', config: twoVehicles }], ['no-lines', { lines: { $js: 'undefined' } }],
    ['bare-config-flat', { config: bare, roofType: 'FLAT' }], ['bare-config-sheet', { config: bare, roofType: 'SHEET' }],
    ['bare-config-elevated', { config: bare, roofType: 'ELEVATED' }], ['no-config', { config: { $js: 'undefined' } }],
    ['market-rate-zero', { size: '6' }], ['market-rate-fr', { futureSystemSize: '5sp', tier: 'base' }],
    ['lines-without-amount', { lines: [{ isVariable: true, category: 'panel', componentId: 'p7', defaultComponentId: 'p2', unitPrice: 13298, qty: 6, defaultUnitPrice: 13585, gst: 5 }, { unitPrice: 100, qty: 3 }, { amount: 50 }, { isVariable: true, defaultComponentId: 'x', componentId: 'y', unitPrice: 10, qty: 2, defaultUnitPrice: 10 }] }],
    ['swap-default-qty', { lines: [{ isVariable: true, category: 'inverter', componentId: 'i97', defaultComponentId: 'i20', unitPrice: 30000, qty: 1, defaultUnitPrice: 18250, defaultQty: 2, gst: 5, name: 'Inv', defaultName: 'Def' }] }],
    ['hybrid-bat-key', { systemType: 'hybrid', tier: 'value', batteryConfig: 2 }], ['hybrid-bat-null', { systemType: 'hybrid', tier: 'value', batteryConfig: null }],
  ];
  for (const [id, over] of edge) {
    const args = { config: { $ref: 'packStore.approved.config' }, systemType: 'ongrid', size: '3', tier: 'value', roofType: 'FLAT', distanceKm: 130, lines: { $bomLines: 'pack/ongrid/3/value' }, ...over };
    call(cases, `price/edge/${id}`, 'pricePack', PP.pricePack, [args]);
  }
  const st = approved.structureTemplates;
  for (const kw of [0.5, 1, 2.5, 3, 4, 5, 7.5, 8, 9, 10, 12.5, 20]) {
    for (const [key, template] of Object.entries(st)) {
      call(cases, `structureMaterial/${key}/${kw}`, 'structureMaterial', PP.structureMaterial, [template, kw, kw > 5 ? 98.3 : 85]);
    }
    call(cases, `structureQty/${kw}`, 'structureQty', PP.structureQty, [{ qty: { 3: 3, 5: 4, 8: 5, 10: 6 } }, kw]);
  }
  for (const [i, item] of [{}, { qty: {} }, { qty: { a: 1, 3: 2 } }, { qty: { '3.0': 2, 5: 4 } }, { qty: { 2: '4', 6: 10 } }].entries()) {
    call(cases, `structureQty/edge/${i}`, 'structureQty', PP.structureQty, [item, 4]);
  }
  call(cases, 'structureMaterial/none', 'structureMaterial', PP.structureMaterial, [null, 3, 85]);
  call(cases, 'structureMaterial/no-items', 'structureMaterial', PP.structureMaterial, [{ items: 'x' }, 3, 85]);
  for (const [i, [costs, tier]] of [[{}, 'base'], [{}, 'value'], [{ gpRatePerKg: 90, giRatePerKg: 101 }, 'base'], [{ gpRatePerKg: 90 }, 'premium'], [null, 'base']].entries()) {
    call(cases, `tubeRateFor/${i}`, 'tubeRateFor', PP.tubeRateFor, [costs, tier]);
  }
  for (const [i, size] of ['3', '5sp', '5tp', '10', 'abc', '', '1.2.3', 7.5, null].entries()) call(cases, `kwOf/${i}`, 'kwOf', PP.kwOf, [size]);
  for (const [i, k] of [
    { systemType: 'ongrid', tier: 'value' }, { systemType: 'hybrid', tier: 'value', batteryConfig: 2 }, { systemType: 'hybrid', tier: 'base' },
    { systemType: 'ongrid', tier: 'base', futureSystemSize: '5sp' }, { systemType: 'hybrid', tier: 'premium', batteryConfig: 0, futureSystemSize: '10' },
    { systemType: 'hybrid', tier: 'premium', batteryConfig: null }, { systemType: 'ongrid', tier: 'premium', batteryConfig: 1 },
  ].entries()) call(cases, `marketRateKey/${i}`, 'marketRateKey', PC.marketRateKey, [k]);
  counts.pack_pricing = writeGolden('pack_pricing', cases);
}

// =============================================================================================================
// packConfig.js — schema validation and the draft/submit/approve lifecycle
// =============================================================================================================
{
  const cases = [];
  const miniCatalog = {
    bomTemplates: { ongrid: { sizes: { 3: '3 kW' }, tiers: ['base', 'value'], threePhase: [], slots: [{ pos: 0, category: 'panel', qty: { 3: 6 } }, { pos: 1, category: 'meter', qty: { 3: 1 }, salesSwap: false, alternatives: ['m1'], defaults: 'x' }], fixedItems: [{ name: 'MC4', price: 56, qty: { 3: 3 } }] } },
    structureTemplates: { flatRoof: { label: 'Flat', items: [{ name: 'Tube', type: 'tube', weightKg: 12.2, qty: { 3: 3 } }] } },
    costs: { structureLabor: 3000, defaultDistKm: 90 }, installationMatrix: { 3: { flat: 15000, sheet: 18000, elevated: 22000 } },
    transportConfig: { costPerKm: 30 }, marketRates: { ongrid_value: { 3: 229000 } },
  };
  const PH = { userId: 'ph-1', role: 'PROJECT_HEAD' };
  const ADMIN = { userId: 'admin-1', role: 'ADMIN' };
  const miniStore = PC.seedFromCatalog(miniCatalog, { at: FIXED_ISO });
  const local = { miniStore };
  const describeAfterEdit = (store, a) => PC.describeStore(PC.updateDraftSection(store, a));
  const validations = [
    ...Object.keys(approved).map((s) => [`valid/${s}`, s, { $ref: `packStore.approved.config.${s}` }]),
    ['unknown-section', 'colours', {}], ['not-object', 'costs', 5], ['null', 'gst', null], ['array-ok', 'tubeWeights', []],
    ['install-roof-not-object', 'installationMatrix', { 3: 5 }], ['install-negative', 'installationMatrix', { 3: { flat: -1 } }],
    ['install-string', 'installationMatrix', { 3: { flat: '15000' } }], ['market-not-object', 'marketRates', { ongrid_value: null }],
    ['market-negative', 'marketRates', { ongrid_value: { 3: -5 } }], ['market-nan', 'marketRates', { ongrid_value: { 3: 'x' } }],
    ['transport-base', 'transportConfig', { baseDistanceKm: -1, vehicles: [] }], ['transport-no-vehicles', 'transportConfig', { baseDistanceKm: 100, vehicles: [] }],
    ['transport-vehicle-bad', 'transportConfig', { baseDistanceKm: 100, vehicles: [{ vehicleType: 'ACE', ratePerKm: -2 }] }],
    ['transport-vehicle-null', 'transportConfig', { baseDistanceKm: 100, vehicles: [null] }],
    ['costs-negative', 'costs', { office: -1 }], ['costs-bool', 'costs', { office: true }], ['costs-string-ok', 'costs', { note: 'x', office: null }],
    ['gst-over', 'gst', { ratePct: 101 }], ['gst-missing', 'gst', {}], ['pricing-bad', 'pricing', { mode: 'MARKUP' }],
    ['templates-no-sizes', 'bomTemplates', { ongrid: { slots: [] } }], ['templates-upgrade-ok', 'bomTemplates', { upgrade: { slots: [] } }],
    ['templates-slots', 'bomTemplates', { ongrid: { sizes: { 3: '3' }, slots: {} } }], ['templates-slot-category', 'bomTemplates', { ongrid: { sizes: {}, slots: [{ qty: {} }] } }],
    ['templates-qty', 'bomTemplates', { ongrid: { sizes: {}, slots: [{ category: 'panel', qty: { 3: -1 } }] } }],
    ['templates-fixed-name', 'bomTemplates', { ongrid: { sizes: {}, slots: [], fixedItems: [{ price: 5 }] } }],
    ['templates-fixed-price', 'bomTemplates', { ongrid: { sizes: {}, slots: [], fixedItems: [{ name: 'MC4', price: -5 }] } }],
    ['structure-items', 'structureTemplates', { flatRoof: {} }], ['structure-item-name', 'structureTemplates', { flatRoof: { items: [{ type: 'tube' }] } }],
    ['structure-tube-weight', 'structureTemplates', { flatRoof: { items: [{ name: 'T', type: 'tube' }] } }],
    ['structure-order', 'installationMatrix', { b: { flat: 1 }, 10: { flat: -1 }, 2: { flat: -2 } }],
  ];
  for (const [id, section, value] of validations) {
    call(cases, `validate/${id}`, 'updateDraftSectionDescribe', describeAfterEdit, [{ $ref: 'miniStore' }, { actor: PH, section, value, at: FIXED_ISO }], local);
  }
  call(cases, 'seed/mini', 'seedFromCatalog', PC.seedFromCatalog, [miniCatalog, { at: FIXED_ISO }]);
  call(cases, 'seed/mini-by', 'seedFromCatalog', PC.seedFromCatalog, [{ transportConfig: { vehicles: [{ vehicleType: 'X', ratePerKm: 1 }], baseDistanceKm: 0 } }, { at: FIXED_ISO, by: 'someone' }]);
  call(cases, 'seed/no-at', 'seedFromCatalog', PC.seedFromCatalog, [miniCatalog, {}]);
  call(cases, 'seed/real', 'seedFromCatalog', PC.seedFromCatalog, [{ $ref: 'catalog' }, { at: FIXED_ISO }]);

  // Lifecycle scenarios: each step replaces the store with the returned one (describeStore excepted).
  const rates = { ongrid_value: { 3: 235000 }, ongrid_base: { 3: 205000 } };
  const scenarios = {
    'submit-approve': [['updateDraftSection', { actor: PH, section: 'marketRates', value: rates, at: '2026-09-20T10:01:00.000Z', note: 'rates' }], ['submitDraft', { actor: PH, at: '2026-09-20T10:02:00.000Z' }], ['approveDraft', { actor: ADMIN, at: '2026-09-20T10:03:00.000Z', note: 'ok' }], ['describeStore']],
    'reject-withdraw': [['updateDraftSection', { actor: PH, section: 'gst', value: { regime: 'FLAT', ratePct: 9 }, at: '2026-09-20T10:01:00.000Z' }], ['submitDraft', { actor: PH, at: '2026-09-20T10:02:00.000Z' }], ['rejectDraft', { actor: ADMIN, at: '2026-09-20T10:03:00.000Z', reason: 'not now' }], ['submitDraft', { actor: PH, at: '2026-09-20T10:04:00.000Z' }], ['updateDraftSection', { actor: PH, section: 'gst', value: { regime: 'FLAT', ratePct: 9 }, at: '2026-09-20T10:05:00.000Z' }], ['describeStore'], ['rejectDraft', { actor: ADMIN, at: '2026-09-20T10:06:00.000Z' }], ['submitDraft', { actor: PH, at: '2026-09-20T10:07:00.000Z' }], ['approveDraft', { actor: ADMIN, at: '2026-09-20T10:08:00.000Z' }]],
    direct: [['approveDraftDirect', { actor: ADMIN, at: '2026-09-20T10:01:00.000Z' }], ['updateDraftSection', { actor: ADMIN, section: 'costs', value: { office: 6000 }, at: '2026-09-20T10:02:00.000Z' }], ['approveDraftDirect', { actor: ADMIN, at: '2026-09-20T10:03:00.000Z' }], ['updateDraftSection', { actor: PH, section: 'costs', value: { office: 6500 }, at: '2026-09-20T10:04:00.000Z' }], ['submitDraft', { actor: PH, at: '2026-09-20T10:05:00.000Z' }], ['approveDraftDirect', { actor: ADMIN, at: '2026-09-20T10:06:00.000Z', note: 'from submitted' }], ['describeStore']],
    rbac: [['updateDraftSection', { actor: { userId: 's1', role: 'SALES' }, section: 'costs', value: {}, at: FIXED_ISO }], ['updateDraftSection', { actor: { role: 'PROJECT_HEAD' }, section: 'costs', value: {}, at: FIXED_ISO }], ['updateDraftSection', { actor: { userId: 'x', role: 'CEO' }, section: 'costs', value: {}, at: FIXED_ISO }], ['updateDraftSection', { actor: PH, section: 'costs', value: {} }], ['submitDraft', { actor: { userId: 'e1', role: 'ENGINEERING' }, at: FIXED_ISO }], ['submitDraft', { actor: PH, at: FIXED_ISO }], ['approveDraft', { actor: PH, at: FIXED_ISO }], ['approveDraft', { actor: ADMIN, at: FIXED_ISO }], ['rejectDraft', { actor: ADMIN, at: FIXED_ISO, reason: 'x' }], ['resetDraft', { actor: { userId: 'p1', role: 'PROCUREMENT' }, at: FIXED_ISO }], ['approveDraftDirect', { actor: PH, at: FIXED_ISO }]],
    'submitted-twice': [['updateDraftSection', { actor: PH, section: 'tubeWeights', value: { '1x1': 9.5 }, at: FIXED_ISO }], ['submitDraft', { actor: PH, at: FIXED_ISO }], ['submitDraft', { actor: PH, at: FIXED_ISO }], ['approveDraft', { actor: ADMIN, at: '' }], ['resetDraft', { actor: ADMIN, at: '2026-09-21T00:00:00.000Z' }], ['describeStore']],
    template: [['updateDraftTemplate', { actor: PH, systemType: 'ongrid', template: { tiers: ['value'], sizes: { 3: '3 kW', 5: '5 kW' } }, at: FIXED_ISO }], ['updateDraftTemplate', { actor: PH, systemType: 'hybrid', template: { sizes: { 5: '5 kW' }, slots: [] }, at: FIXED_ISO, note: 'new hybrid' }], ['updateDraftTemplate', { actor: PH, systemType: 'hybrid', template: { slots: [{ qty: { 5: 1 } }] }, at: FIXED_ISO }], ['resetDraft', { actor: PH, at: '2026-09-22T00:00:00.000Z' }]],
  };
  for (const [name, steps] of Object.entries(scenarios)) {
    let store = clone(miniStore);
    const outputs = [];
    for (const [op, arg] of steps) {
      const out = capture(() => (op === 'describeStore' ? PC.describeStore(store) : PC[op](store, clone(arg))));
      if (out.result && op !== 'describeStore') store = clone(out.result);
      outputs.push(out);
    }
    cases.push({ id: `scenario/${name}`, fn: 'packConfigScenario', args: [{ $ref: 'miniStore' }, steps.map(([op, arg]) => (arg === undefined ? { op } : { op, args: arg }))], result: outputs });
  }
  // The real store: draft == approved.
  call(cases, 'real/describe', 'describeStore', PC.describeStore, [{ $ref: 'packStore' }]);
  call(cases, 'real/submit', 'submitDraft', PC.submitDraft, [{ $ref: 'packStore' }, { actor: PH, at: FIXED_ISO }]);
  call(cases, 'real/direct', 'approveDraftDirect', PC.approveDraftDirect, [{ $ref: 'packStore' }, { actor: ADMIN, at: FIXED_ISO }]);
  call(cases, 'real/edit-describe', 'updateDraftSectionDescribe', describeAfterEdit, [{ $ref: 'packStore' }, { actor: PH, section: 'marketRates', value: { ...approved.marketRates, ongrid_base: { ...approved.marketRates.ongrid_base, 6: 360000 } }, at: FIXED_ISO }]);
  for (const sys of ['ongrid', 'hybrid', 'upgrade', 'offgrid']) call(cases, `approvedSizes/${sys}`, 'approvedSizes', PC.approvedSizes, [{ $ref: 'packStore.approved.config' }, sys]);
  for (const [i, [sys, size]] of [['ongrid', '3'], ['ongrid', '5tp'], ['ongrid', '6'], ['ongrid', '8'], ['ongrid', '10'], ['hybrid', '3'], ['hybrid', '8'], ['hybrid', '5'], ['ongrid', 3]].entries()) {
    call(cases, `futurePairs/${i}/${sys}/${size}`, 'futurePairsFor', PC.futurePairsFor, [{ $ref: 'packStore.approved.config' }, sys, size]);
  }
  counts.pack_config = writeGolden('pack_config', cases, { fixtures: local });
}

// =============================================================================================================
// packageApproval.js / packageAuthority.js / packageProjection.js
// =============================================================================================================
{
  const cases = [];
  const PH = { userId: 'ph-1', role: 'PROJECT_HEAD' };
  const ADMIN = { userId: 'admin-1', role: 'ADMIN' };
  const comps = (panel) => [
    { role: 'panel', componentId: panel, quantity: 6, approvalStatus: 'PROPOSED' },
    { role: 'inverter', componentId: 'i20', quantity: 1, approvalStatus: 'APPROVED' },
    { role: 'meter', componentId: 'm2', quantity: 1 },
  ];
  const baseRegistry = {
    schema: 'flarize.package-registry/1',
    packages: [
      { packageId: 'PKG-A', systemType: 'ongrid', size: '3', phase: '1P', tier: 'value', profileKey: 'ongrid_value', approvalStatus: 'APPROVED', packageState: 'APPROVED', revisionNumber: 1, components: comps('p2') },
      { packageId: 'PKG-B', systemType: 'ongrid', size: '3', phase: '1P', tier: 'value', profileKey: 'ongrid_value', approvalStatus: 'PROPOSED', packageState: 'DRAFT', revisionNumber: 2, components: comps('p7'), comboKey: 'ongrid|3|1P|value' },
      { packageId: 'PKG-C', systemType: 'hybrid', size: '5', phase: '1P', tier: 'premium', profileKey: 'hybrid_premium', approvalStatus: 'REJECTED', components: [{ role: 'battery', componentId: 'bt2' }] },
      { packageId: 'PKG-D', systemType: 'ongrid', size: '3', phase: '1P', tier: 'value', variant: 'FUTURE_READY', systemSize: '5sp', approvalStatus: 'APPROVED', packageState: 'APPROVED', createdBy: 'ph-1', createdAt: FIXED_ISO, components: [] },
      { packageId: 'PKG-E', systemType: 'ongrid', size: '5sp', phase: '1P', tier: 'base', architecture: 'SUNNY', approvalStatus: 'PROPOSED', packageState: 'SUBMITTED', components: [{ role: 'panel', componentId: 'p_missing' }] },
    ],
  };
  const valid = (rev = 1, status = 'VALID') => ({ status, revisionNumber: rev, findings: [{ ruleId: 'PBC-1', severity: 'WARNING' }, { ruleId: 'PBC-2', severity: 'INFO' }, { severity: 'WARNING' }] });
  const scenarios = {
    'approve-supersede': [['setValidation', 'PKG-B', valid(2)], ['approvePackage', { actor: ADMIN, packageId: 'PKG-B', notes: 'ok', at: FIXED_ISO, catalog: { $ref: 'catalog' } }], ['archivePackage', { actor: PH, packageId: 'PKG-A', reason: '  superseded  ', at: FIXED_ISO }], ['listPackagesForRole', { actor: { role: 'SALES' } }], ['approvalSummary']],
    'approve-gates': [['approvePackage', { actor: ADMIN, packageId: 'PKG-B', at: FIXED_ISO }], ['setValidation', 'PKG-B', valid(1)], ['approvePackage', { actor: ADMIN, packageId: 'PKG-B', at: FIXED_ISO }], ['setValidation', 'PKG-B', valid(2, 'WARNING')], ['approvePackage', { actor: ADMIN, packageId: 'PKG-B', at: FIXED_ISO }], ['approvePackage', { actor: ADMIN, packageId: 'PKG-B', at: FIXED_ISO, acknowledgeWarnings: true }], ['approvePackage', { actor: ADMIN, packageId: 'PKG-B', at: FIXED_ISO }], ['approvePackage', { actor: ADMIN, packageId: 'NOPE', at: FIXED_ISO }], ['approvePackage', { actor: PH, packageId: 'PKG-B', at: FIXED_ISO }], ['approvePackage', { actor: ADMIN, packageId: 'PKG-B' }]],
    'component-gate': [['setValidation', 'PKG-E', valid(1)], ['approvePackage', { actor: ADMIN, packageId: 'PKG-E', at: FIXED_ISO, catalog: { $ref: 'catalog' } }], ['approvePackage', { actor: ADMIN, packageId: 'PKG-E', at: FIXED_ISO, catalog: { $ref: 'componentCatalog' } }]],
    'reject-reset': [['rejectPackage', { actor: ADMIN, packageId: 'PKG-B', reason: '  ', at: FIXED_ISO }], ['rejectPackage', { actor: ADMIN, packageId: 'PKG-B', reason: ' bad pick ', at: FIXED_ISO }], ['rejectPackage', { actor: ADMIN, packageId: 'NOPE', reason: 'x', at: FIXED_ISO }], ['resetPackage', { actor: ADMIN, packageId: 'PKG-A', at: FIXED_ISO }], ['archivePackage', { actor: PH, packageId: 'PKG-C', reason: 'old', at: FIXED_ISO }], ['resetPackage', { actor: ADMIN, packageId: 'PKG-C', at: FIXED_ISO }], ['resetPackage', { actor: ADMIN, packageId: 'NOPE', at: FIXED_ISO }]],
    authoring: [['createPackage', { actor: PH, at: FIXED_ISO, template: { systemType: 'hybrid', size: '8', phase: '3P', tier: 'value', architecture: 'DEYE', profileKey: 'hybrid_value', components: [{ role: 'panel', componentId: 'p2', quantity: 15 }] } }], ['createPackage', { actor: PH, at: FIXED_ISO, template: { packageId: 'MY-ID', systemType: 'ongrid', size: '3', phase: '1P', tier: 'base', architecture: 'ENPHASE', profileKey: 'ongrid_base', components: [], variant: 'FUTURE_READY', systemSize: '5sp' } }], ['createPackage', { actor: PH, at: FIXED_ISO, template: { systemType: 'ongrid' } }], ['createPackage', { actor: PH, at: FIXED_ISO, template: { systemType: 'ongrid', size: '3', phase: '1P', tier: 'base', architecture: 'X', profileKey: 'p', components: [] } }], ['createPackage', { actor: PH, at: FIXED_ISO, template: { systemType: 'ongrid', size: '3', phase: '1P', tier: 'base', architecture: 'DEYE', profileKey: 'p', components: {} } }], ['createPackage', { actor: PH, at: FIXED_ISO }], ['createPackage', { actor: ADMIN, at: FIXED_ISO, template: {} }], ['duplicatePackage', { actor: PH, sourcePackageId: 'PKG-D', at: FIXED_ISO }], ['duplicatePackage', { actor: PH, sourcePackageId: 'NOPE', at: FIXED_ISO }], ['createRevision', { actor: PH, sourcePackageId: 'PKG-A', at: FIXED_ISO }], ['createRevision', { actor: PH, sourcePackageId: 'PKG-B', at: FIXED_ISO }], ['listPackagesForRole', { actor: ADMIN, filter: { packageState: 'DRAFT' } }]],
    edit: [['editPackage', { actor: PH, packageId: 'PKG-B', at: FIXED_ISO, patch: { architecture: 'ENPHASE', profileKey: 'ongrid_premium', components: [{ role: 'panel', componentId: 'p3' }] } }], ['editPackage', { actor: PH, packageId: 'PKG-B', at: FIXED_ISO, patch: { tier: 'base' } }], ['editPackage', { actor: PH, packageId: 'PKG-B', at: FIXED_ISO, patch: { architecture: 'LG' } }], ['editPackage', { actor: PH, packageId: 'PKG-B', at: FIXED_ISO, patch: { components: 'x' } }], ['editPackage', { actor: PH, packageId: 'PKG-A', at: FIXED_ISO, patch: {} }], ['editPackage', { actor: PH, packageId: 'PKG-B', at: FIXED_ISO, patch: null }], ['submitPackage', { actor: PH, packageId: 'PKG-B', at: FIXED_ISO }], ['setValidation', 'PKG-B', valid(2, 'WARNING')], ['submitPackage', { actor: PH, packageId: 'PKG-B', at: FIXED_ISO }], ['submitPackage', { actor: PH, packageId: 'PKG-B', at: FIXED_ISO, acknowledgeWarnings: true }], ['submitPackage', { actor: PH, packageId: 'PKG-B', at: FIXED_ISO }], ['archivePackage', { actor: PH, packageId: 'PKG-A', reason: 'x', at: FIXED_ISO }], ['archivePackage', { actor: PH, packageId: 'PKG-B', at: FIXED_ISO }], ['archivePackage', { actor: ADMIN, packageId: 'PKG-B', reason: 'done', at: FIXED_ISO }]],
    bulk: [['setValidation', 'PKG-B', valid(2)], ['setValidation', 'PKG-E', valid(1)], ['bulkApprovePackages', { actor: ADMIN, at: FIXED_ISO, filter: { systemType: 'ongrid' }, notes: 'bulk' }], ['bulkApprovePackages', { actor: ADMIN, at: FIXED_ISO }], ['listPackagesForRole', { actor: { role: 'SALES_HEAD' }, filter: { tier: 'value', size: '3', phase: '1P', systemType: 'ongrid', approvalStatus: 'APPROVED' } }], ['assertPackageSelectableByActor', { actor: { role: 'SALES' }, packageId: 'PKG-B' }], ['assertPackageSelectableByActor', { actor: { role: 'SALES' }, packageId: 'PKG-C' }], ['assertPackageSelectableByActor', { actor: { role: 'SALES' }, packageId: 'NOPE' }], ['assertPackageSelectableByActor', { actor: ADMIN, packageId: 'NOPE' }]],
  };
  const componentCatalog = { categories: { panel: { items: [{ id: 'p_missing', name: 'Proposed panel', approvalStatus: 'PROPOSED' }] } } };
  const local = { componentCatalog };
  for (const [name, steps] of Object.entries(scenarios)) {
    const PA = await freshPackageApproval();
    const registry = clone(baseRegistry);
    const outputs = [];
    for (const step of steps) {
      const [op, a, b] = step;
      if (op === 'setValidation') {
        const pkg = registry.packages.find((p) => p.packageId === a);
        pkg.lastValidation = clone(b);
        outputs.push({ result: null, registry: clone(registry) });
        continue;
      }
      const arg = resolveArgs(clone(a), local);
      const out = capture(() => (op === 'approvalSummary' ? PA.approvalSummary(registry) : PA[op](registry, arg)));
      outputs.push({ ...out, registry: clone(registry) });
    }
    cases.push({ id: `registry/${name}`, fn: 'registryScenario', args: [baseRegistry, steps.map(([op, a, b]) => (op === 'setValidation' ? { op, packageId: a, validation: b } : a === undefined ? { op } : { op, args: a }))], result: outputs });
  }
  const PA = await freshPackageApproval();
  for (const [i, p] of [...baseRegistry.packages, { systemType: 'x' }, { variant: 'FUTURE_READY', systemSize: 8 }, {}].entries()) {
    call(cases, `comboKey/${i}`, 'comboKeyFor', PA.comboKeyFor, [p]);
  }
  for (const [i, p] of [...baseRegistry.packages, { profileKey: 'premium' }, { profileKey: 'nope' }, null].entries()) {
    call(cases, `architecture/${i}`, 'resolvePackageArchitecture', PA.resolvePackageArchitecture, [p, { $ref: 'catalog' }]);
  }
  const sample = { schema: registryFull.schema, packages: registryFull.packages.slice(0, 6).map((p) => ({ ...pick(p, ['packageId', 'systemType', 'size', 'phase', 'tier', 'approvalStatus', 'packageState', 'revisionNumber', 'supersededBy', 'createdBy', 'createdAt', 'variant', 'systemSize', 'comboKey', 'architecture']) })) };
  sample.packages.push(...baseRegistry.packages, 'junk', null, { approvalStatus: 'PROPOSED', createdBy: null, supersededBy: 'X' });
  call(cases, 'hydrate/sample', 'hydrateRegistry', PPR.hydrateRegistry, [sample]);
  call(cases, 'hydrate/visible', 'hydrateThenVisible', (r) => PPR.visibleForRuntime(PPR.hydrateRegistry(r)), [sample]);
  call(cases, 'hydrate/empty', 'hydrateRegistry', PPR.hydrateRegistry, [null]);
  call(cases, 'visible/empty', 'visibleForRuntime', PPR.visibleForRuntime, [{ packages: 'x' }]);
  let n = 0;
  for (const [key, profile] of Object.entries(catalog.packageProfiles)) {
    for (const [tier, phase] of [[undefined, undefined], ['base', '1P'], ['value', '3P'], ['premium', undefined]]) {
      const ctx = {};
      if (tier) ctx.tier = tier;
      if (phase) ctx.phase = phase;
      call(cases, `derive/${key}/${n++}`, 'derivePackageComponents', PAU.derivePackageComponents, [profile, { $ref: 'catalog' }, ctx]);
    }
  }
  call(cases, 'derive/explicit', 'derivePackageComponents', PAU.derivePackageComponents, [{ components: { panel: 'p7', inverter: 'nope' }, batteryComponentId: 'bt3' }, { $ref: 'catalog' }, { tier: 'base' }]);
  call(cases, 'derive/empty-catalog', 'derivePackageComponents', PAU.derivePackageComponents, [null, {}, {}]);
  const derived = PAU.derivePackageComponents(catalog.packageProfiles.ongrid_value, catalog, { tier: 'value' }).components;
  for (const [i, [role, cid]] of [['panel', derived.panel.componentId], ['panel', derived.panel.approvedAlternates.at(-1)], ['panel', 'zzz'], ['nothing', 'p1']].entries()) {
    call(cases, `isApprovedSelection/${i}`, 'isApprovedSelection', PAU.isApprovedSelection, [derived, role, cid]);
  }
  for (const role of ['panel', 'inverter', 'structure', 'battery', 'dcdb']) call(cases, `isSalesEditable/${role}`, 'isSalesEditable', PAU.isSalesEditable, [role]);
  counts.package_registry = writeGolden('package_registry', cases, { fixtures: { componentCatalog, idFactory: { nowMs: FIXED_MS, seq: 0 } } });
}

// =============================================================================================================
// costEngine.js (+ procurementPriceMaster.js, projectRateCard.js, commercialHistory.js)
// =============================================================================================================
const costResults = new Map();
{
  const cases = [];
  const audit = { changedBy: 'admin-001', changedAt: '2026-09-01T00:00:00.000Z', changeReason: 'seed', effectiveFrom: '2026-09-01', cardVersion: 'card-1' };
  const seededCard = RC.seedRateCardFromConfig(costConfig, audit);
  let bandCard = RC.setRate(seededCard, { rateType: 'ENGINEERING_DESIGN_RATE', recordId: '>20kW', value: { costPerProject: 40, unit: 'PER_KW' }, versionId: 'ENG-BAND-1', changedBy: 'ph', changedAt: '2026-09-02', changeReason: 'band', effectiveFrom: '2026-09-02' });
  bandCard = RC.setRate(bandCard, { rateType: 'INSTALLATION_RATE', recordId: '25kW/FLAT', value: { systemSizeKw: 25, installationType: 'FLAT', unit: 'PER_KW', rate: 3800 }, versionId: 'INST-25', changedBy: 'ph', changedAt: '2026-09-02', changeReason: 'big', effectiveFrom: '2026-09-02' });
  bandCard = RC.setRate(bandCard, { rateType: 'SITE_SURVEY_RATE', recordId: 'STANDARD', value: { costPerProject: 600, coverageKm: 80 }, versionId: 'SURVEY-2', changedBy: 'ph', changedAt: '2026-09-03', changeReason: 'rise', effectiveFrom: '2026-09-03' });
  const local = { seededCard, bandCard };
  const snapshotFromBom = (caseId, extra = {}) => {
    const bom = BOM_RESULTS.get(caseId);
    return { projectId: `P-${caseId}`, status: 'LOCKED', lockedAt: FIXED_ISO, ...extra, lines: bom.lines.map((l) => ({ componentId: l.componentId, quantity: l.qty, role: l.category })) };
  };
  const mini = { projectId: 'P-MINI', snapshotId: 'SNAP-1', status: 'LOCKED', lines: [{ componentId: 'p2', quantity: 6, role: 'panel' }, { componentId: 'i20', quantity: 1, role: 'inverter' }, { componentId: 'a1', quantity: 1, role: 'acdb' }] };
  const project = (over = {}) => ({ sizeKw: 3, installationType: 'FLAT', distanceKm: 130, vehicleType: 'STANDARD', structureMaterialCost: 9972, tier: 'value', ...over });
  // Full BOM snapshots for every standard ongrid / hybrid pack.
  let n = 0;
  for (const p of packs.filter((x) => !x.fr && x.bat === null && BOM_RESULTS.has(x.caseId)).filter((_, i) => i % 3 === 0)) {
    const kw = Number(String(p.size).replace(/[^0-9.]/g, '')) || 0;
    const roof = ['FLAT', 'SHEET', 'ELEVATED'][n % 3];
    const id = `cost/pack/${p.caseId}`;
    call(cases, id, 'calculateCost', CE.calculateCost, [{
      snapshot: snapshotFromBom(p.caseId), config: { $ref: 'costConfig' }, priceMaster: { $ref: 'priceMaster' },
      project: project({ sizeKw: kw, installationType: roof, distanceKm: [60, 100, 130, 250][n % 4], vehicleType: ['STANDARD', 'LORRY'][n % 2], tier: p.tier, structureMaterialCost: [0, 9972, 16556][n % 3] }),
      rateCard: n % 3 === 0 ? null : { $ref: n % 3 === 1 ? 'seededCard' : 'rateCard' }, calculatedAt: FIXED_ISO, catalogVersion: 'catalog@1', landedCostVersion: 'BATCH-SEED-005',
    }], local);
    const out = cases.at(-1);
    if (out.result) costResults.set(id, out.result);
    n++;
  }
  const brokenMaster = { p2: { purchasePrice: 13000 }, i20: { landedUnitCost: 18000, sellingPrice: 25000 }, a1: { purchasePrice: 3710, landedUnitCost: 3800, componentName: 'ACDB' } };
  const cfg = (mutate) => { const c = clone(costConfig); mutate(c); return c; };
  const variants = [
    ['ok', {}], ['survey-within', { project: project({ distanceKm: 60 }) }], ['survey-excluded', { project: project({ siteSurveyIncluded: false }) }],
    ['survey-no-distance', { project: project({ distanceKm: undefined }) }], ['survey-no-excess', { config: cfg((c) => { delete c.siteSurvey.excessRatePerKm; }) }],
    ['survey-not-configured', { config: cfg((c) => { delete c.siteSurvey; }) }], ['structure-missing', { project: project({ installationType: undefined }) }],
    ['structure-roof', { project: project({ installationType: undefined, roofType: 'SHEET' }) }], ['structure-unknown', { project: project({ installationType: 'TIN' }) }],
    ['install-per-kw', { config: cfg((c) => { c.installation.rates[0].unit = 'PER_KW'; c.installation.rates[0].rate = 5000; }) }],
    ['install-bad-unit', { config: cfg((c) => { c.installation.rates[0].unit = 'LUMP'; }) }], ['install-missing-size', { project: project({ sizeKw: 4 }) }],
    ['size-25-no-band', { project: project({ sizeKw: 25 }) }], ['size-25-band', { project: project({ sizeKw: 25 }), rateCard: { $ref: 'bandCard' } }],
    ['band-card-effective', { project: project({ sizeKw: 25, rateEffectiveAt: '2026-09-02' }), rateCard: { $ref: 'bandCard' } }],
    ['band-card-before', { project: project({ rateEffectiveAt: '2026-08-01' }), rateCard: { $ref: 'bandCard' } }],
    ['eng-no-limit', { config: cfg((c) => { delete c.engineeringDesign.sizeLimitKw; }) }], ['eng-no-cost', { config: cfg((c) => { c.engineeringDesign.costPerProject = null; }) }],
    ['transport-na', { config: cfg((c) => { c.transportation.notApplicable = true; }) }], ['transport-unknown', { project: project({ vehicleType: 'BIKE' }) }],
    ['transport-none', { project: project({ vehicleType: undefined }) }], ['transport-no-distance', { project: project({ distanceKm: 'far' }) }],
    ['transport-card', { rateCard: { $ref: 'rateCard' } }], ['transport-card-dated', { rateCard: { $ref: 'rateCard' }, project: project({ rateEffectiveAt: '2026-09-06T00:00:00.000Z' }) }],
    ['service-excluded', { config: cfg((c) => { c.serviceAmc.serviceIncluded = false; }) }], ['service-incomplete', { config: cfg((c) => { delete c.serviceAmc.visitsPerYear; c.serviceAmc.serviceYears = 'x'; }) }],
    ['service-out-of-coverage', { project: project({ sizeKw: 12 }) }], ['works', { project: project({ specialWorks: [{ workType: 'TRENCHING', amount: 4500.5, reason: 'r', addedBy: 'ph', at: FIXED_ISO }, { workType: 'CRANE', amount: '12000' }] }) }],
    ['works-invalid', { project: project({ specialWorks: [{ workType: 'CRANE' }, { amount: 5 }, { workType: 'OTHER', amount: 10 }] }) }],
    ['office-invalid', { config: cfg((c) => { c.officeExpenseAllocation.expectedProjectsPerMonth = 0; }) }], ['office-missing', { config: cfg((c) => { delete c.officeExpenseAllocation; }) }],
    ['misc-missing', { config: cfg((c) => { delete c.miscellaneous.percentage; }) }], ['misc-negative', { config: cfg((c) => { c.miscellaneous.percentage = -1; }) }],
    ['misc-100', { config: cfg((c) => { c.miscellaneous.percentage = 100; }) }], ['misc-zero', { config: cfg((c) => { c.miscellaneous.percentage = 0; }) }],
    ['misc-incomplete-direct', { priceMaster: brokenMaster }], ['price-kind-violation', { priceMaster: { ...priceMaster, i20: { ...priceMaster.i20, marketRate: 1 } } }],
    ['no-price-master', { priceMaster: { $js: 'undefined' } }], ['empty-config', { config: {} }], ['no-project', { project: { $js: 'undefined' } }],
    ['unlocked', { snapshot: { ...mini, status: 'DRAFT' } }], ['no-snapshot', { snapshot: null }], ['snapshot-no-id', { snapshot: { status: 'LOCKED', lines: [] } }],
    ['duplicate-lines', { snapshot: { ...mini, lines: [...mini.lines, { componentId: 'p2', quantity: 2, role: 'panel2' }, { quantity: 1 }] } }],
    ['versions', { projectRateCardVersion: 'prc-9', procurementPriceVersion: 'pp-1' }],
  ];
  for (const [id, over] of variants) {
    const args = { snapshot: mini, config: { $ref: 'costConfig' }, priceMaster: { $ref: 'priceMaster' }, project: project(), calculatedAt: FIXED_ISO, ...over };
    call(cases, `cost/variant/${id}`, 'calculateCost', CE.calculateCost, [args], local);
    if (cases.at(-1).result) costResults.set(`cost/variant/${id}`, cases.at(-1).result);
  }
  for (const [i, id] of [...costResults.keys()].filter((_, i) => i % 4 === 0).entries()) {
    cases.push({ id: `explainCost/${i}`, fn: 'explainCost', args: [costResults.get(id)], ...capture(() => CE.explainCost(costResults.get(id))) });
  }
  cases.push({ id: 'explainCost/none', fn: 'explainCost', args: [null], ...capture(() => CE.explainCost(null)) });
  // Price master.
  for (const [i, raw] of [{ componentId: 'x', purchasePrice: 10, landedUnitCost: 12 }, { purchasePrice: 0, landedUnitCost: 5 }, { SELLING_PRICE: 1 }, { customerPrice: null }, {}, { landedUnitCost: '' }].entries()) {
    call(cases, `toPriceRecord/${i}`, 'toPriceRecord', PM.toPriceRecord, [raw]);
    call(cases, `projectUnitCost/${i}`, 'projectUnitCost', (r) => PM.projectUnitCost(PM.toPriceRecord(r)), [raw]);
    call(cases, `procurementUplift/${i}`, 'procurementUplift', (r) => PM.procurementUplift(PM.toPriceRecord(r)), [raw]);
  }
  call(cases, 'projectUnitCost/null', 'projectUnitCost', PM.projectUnitCost, [null]);
  call(cases, 'buildPriceMaster/sample', 'buildPriceMaster', PM.buildPriceMaster, [{ a1: priceMaster.a1, p2: priceMaster.p2, bad: { marketRate: 5 } }]);
  counts.cost = writeGolden('cost', cases, { fixtures: { seededCard, bandCard } });
}

// =============================================================================================================
// procurementBatch.js — landed-cost allocation (allocate_landed parity: deliveryCost = Σ batch charges)
// =============================================================================================================
{
  const cases = [];
  const landed = (a) => PB.buildBatchLandedCosts({ procurementBatchId: a.batchId, deliveryCost: MN.sumExact((a.charges || []).map((c) => c.amount)), allocationMethod: a.allocationMethod, lines: a.lines, recordedBy: a.recordedBy, recordedAt: a.recordedAt, effectiveFrom: a.effectiveFrom, versionId: a.versionId });
  const three = [{ componentId: 'c', quantity: 10, purchaseUnitPrice: 1000 }, { componentId: 'a', quantity: 20, purchaseUnitPrice: 3500 }, { componentId: 'b', quantity: 4, purchaseUnitPrice: 5000 }];
  const equal = [{ componentId: 'x1', quantity: 1, purchaseUnitPrice: 100 }, { componentId: 'x2', quantity: 1, purchaseUnitPrice: 100 }, { componentId: 'x3', quantity: 1, purchaseUnitPrice: 100 }];
  const multi = [
    ['p2', [{ kind: 'FREIGHT', amount: 3000 }, { kind: 'INSURANCE', amount: 1500.5 }, { kind: 'HANDLING', amount: 499.5 }]],
    ['p3', [{ kind: 'FREIGHT', amount: 5000 }]], ['p4', [{ kind: 'DUTY', amount: 0.4 }, { kind: 'OTHER', amount: 0.4 }, { kind: 'FREIGHT', amount: 0.4 }]],
    ['p5', []], ['p6', [{ kind: 'FREIGHT', amount: 10000 }, { kind: 'INSURANCE', amount: 3000 }, { kind: 'HANDLING', amount: 2000 }]],
  ];
  for (const [id, charges] of multi) {
    call(cases, `allocate/three/${id}`, 'allocateLanded', landed, [{ batchId: `B-${id}`, lines: three, charges, recordedBy: 'proc-1', recordedAt: FIXED_ISO, effectiveFrom: '2026-09-20', versionId: `v-${id}` }]);
    call(cases, `allocate/equal/${id}`, 'allocateLanded', landed, [{ batchId: `E-${id}`, lines: equal, charges }]);
  }
  for (const b of batches) {
    const batchLines = Object.fromEntries(batches.map((x) => [`lines:${x.batchId}`, x.lines]));
    call(cases, `allocate/real/${b.batchId}`, 'allocateLanded', landed, [{ batchId: b.batchId, lines: { $ref: `lines:${b.batchId}` }, charges: [{ kind: 'FREIGHT', amount: 10000 }, { kind: 'INSURANCE', amount: 3000 }, { kind: 'HANDLING', amount: b.deliveryCost - 13000 }] }], batchLines);
    call(cases, `batch/real/${b.batchId}`, 'buildBatchLandedCosts', PB.buildBatchLandedCosts, [{ procurementBatchId: b.batchId, deliveryCost: b.deliveryCost, lines: { $ref: `lines:${b.batchId}` } }], batchLines);
  }
  const batchCases = [
    ['forbidden', { allocationMethod: 'WEIGHT', deliveryCost: 5, lines: three }], ['unknown', { allocationMethod: 'MAGIC', lines: three }],
    ['transport', { projectTransportCost: 0, lines: three }], ['invalid-lines', { lines: [{ componentId: 'a', quantity: 0, purchaseUnitPrice: 5 }, { quantity: 1, purchaseUnitPrice: 5 }, { componentId: 'b', quantity: 'x', purchaseUnitPrice: 5 }, { componentId: 'c', quantity: 1 }] }],
    ['zero-value', { deliveryCost: 500, lines: [{ componentId: 'a', quantity: 5, purchaseUnitPrice: 0 }] }], ['zero-value-no-delivery', { lines: [{ componentId: 'a', quantity: 5, purchaseUnitPrice: 0 }] }],
    ['example-70-20-10', { deliveryCost: 5000, lines: [{ componentId: 'a', quantity: 70, purchaseUnitPrice: 1000 }, { componentId: 'b', quantity: 20, purchaseUnitPrice: 1000 }, { componentId: 'c', quantity: 10, purchaseUnitPrice: 1000 }] }],
    ['other-charges', { deliveryCost: '999.5', lines: [{ componentId: 'a', quantity: 3, purchaseUnitPrice: 333.33, otherProcurementCharges: [{ label: 'loading', amount: 120 }, { amount: 'x' }, { label: 'unloading', amount: '30.5' }] }, { componentId: 'b', quantity: 7.5, purchaseUnitPrice: 12 }] }],
    ['no-delivery', { lines: three }], ['empty', {}], ['no-arg', { $js: 'undefined' }],
  ];
  for (const [id, batch] of batchCases) call(cases, `batch/${id}`, 'buildBatchLandedCosts', PB.buildBatchLandedCosts, [batch]);
  const ok = PB.buildBatchLandedCosts({ procurementBatchId: 'B-1', deliveryCost: 5000, lines: three });
  call(cases, 'priceMasterEntries/ok', 'toPriceMasterEntries', PB.toPriceMasterEntries, [ok, { purchasePriceVersion: 'B-1::pp', landedCostVersion: 'B-1::lc', supplier: 'S1' }]);
  call(cases, 'priceMasterEntries/fail', 'toPriceMasterEntries', PB.toPriceMasterEntries, [{ ok: false }]);
  counts.landed = writeGolden('landed', cases, { fixtures: Object.fromEntries(batches.map((x) => [`lines:${x.batchId}`, x.lines])) });
}

// =============================================================================================================
// pricingEngine.js
// =============================================================================================================
{
  const cases = [];
  const stub = (r) => pick(r, ['status', 'totalActualProjectCostExact', 'totalActualProjectCost', 'errors', 'tier', 'bomSnapshotId', 'projectId', 'costEngineVersion']);
  const complete = [...costResults.entries()].filter(([, r]) => r.status === 'COMPLETE');
  const incomplete = [...costResults.entries()].find(([, r]) => r.status === 'INCOMPLETE');
  const margin = costConfig.margin;
  const gst = costConfig.gst;
  let n = 0;
  for (const [id, result] of complete.slice(0, 24)) {
    const discount = [null, { amount: 5000, requestedBy: 'sales-1', reason: 'deal' }, { percentage: 3 }, { percentage: 10 }, null][n % 5];
    call(cases, `pricing/${id}`, 'calculatePricing', PE.calculatePricing, [{ costResult: stub(result), margin, gst, tier: [undefined, 'value', 'PREMIUM', 'Base'][n % 4], discount, marketRate: n % 3 === 0 ? 229000 : null, pricedAt: FIXED_ISO }]);
    n++;
  }
  const base = stub(complete[0][1]);
  const variants = [
    ['no-cost', { costResult: null }], ['unavailable', { costResult: { status: 'UNAVAILABLE', bomSnapshotId: 'S' } }], ['incomplete', { costResult: stub(incomplete[1]) }],
    ['incomplete-no-errors', { costResult: { status: 'INCOMPLETE' } }], ['legacy-total', { costResult: { status: 'COMPLETE', totalActualCostExact: 150000.4, totalActualCost: 150000 } }],
    ['flat-margin', { margin: { targetGrossMargin: 0.2, version: 'm1' } }], ['flat-margin-zero', { margin: { targetGrossMargin: 0 } }], ['flat-margin-99', { margin: { targetGrossMargin: 0.99 } }],
    ['flat-margin-1', { margin: { targetGrossMargin: 1 } }], ['flat-margin-negative', { margin: { targetGrossMargin: -0.1 } }], ['flat-margin-missing', { margin: {} }],
    ['flat-margin-string', { margin: { targetGrossMargin: '0.25' } }], ['markup', { margin: { marginType: 'MARKUP', targetGrossMargin: 0.2 } }], ['markup-tiers', { margin: { ...margin, marginType: 'MARKUP' } }],
    ['unknown-type', { margin: { marginType: 'NET', targetGrossMargin: 0.2 } }], ['tier-missing', { tier: 'gold' }], ['tier-undeclared', { costResult: { ...base, tier: undefined } }],
    ['tier-config-missing', { margin: { marginType: 'GROSS_MARGIN', tiers: { VALUE: { targetMarginPct: 20 } } } }], ['tier-target-100', { margin: { tiers: { VALUE: { targetMarginPct: 100, minimumMarginPct: 5 } } } }],
    ['tier-min-negative', { margin: { tiers: { VALUE: { targetMarginPct: 20, minimumMarginPct: -1 } } } }], ['tier-min-over-target', { margin: { tiers: { VALUE: { targetMarginPct: 20, minimumMarginPct: 25 } } } }],
    ['discount-amount', { discount: { amount: '2500' } }], ['discount-percentage', { discount: { percentage: 4.5 } }], ['discount-below-min', { discount: { percentage: 12 } }],
    ['discount-invalid', { discount: { note: 'x' } }], ['discount-negative', { discount: { amount: -1 } }], ['discount-over-list', { discount: { percentage: 101 } }],
    ['discount-all', { margin: { targetGrossMargin: 0.2 }, discount: { percentage: 100 } }], ['gst-flat', { gst: { ratePct: 9, version: 'g0' } }], ['gst-flat-regime', { gst: { regime: 'FLAT', ratePct: '18' } }],
    ['gst-flat-missing', { gst: { regime: 'FLAT' } }], ['gst-none', { gst: {} }], ['gst-unknown', { gst: { regime: 'VAT' } }], ['gst-split-missing', { gst: { regime: 'SOLAR_70_30_COMPOSITE', goodsValuationPct: 70 } }],
    ['gst-split-null', { gst: { regime: 'SOLAR_70_30_COMPOSITE', goodsValuationPct: 70, goodsRatePct: null, serviceValuationPct: 30, serviceRatePct: 18 } }],
    ['gst-split-sum', { gst: { ...gst, serviceValuationPct: 35 } }], ['gst-mismatch', { gst: { ...gst, effectiveRatePct: 9 } }], ['gst-applies', { gst: { ...gst, appliesTo: 'X' } }],
    ['extras', { extras: [{ extraId: 'Z-BATTERY', type: 'OPTIONAL_ACCESSORY', customerPrice: 45000.5, description: 'Battery' }, { extraId: 'A-AMC', customerPrice: '12000', gstRatePct: 18 }, { type: 'NET_METER', extraId: 'NM', customerPrice: 5000 }, { extraId: 'X' }, { customerPrice: 3 }, { extraId: 'B', type: 'NET_METERING', customerPrice: 1 }] }],
    ['customer-side', { customerSideExpenses: [{ expenseId: 'b-meter', type: 'NET_METER', amount: 5000.5, description: 'KSEB meter' }, { expenseId: 'a-fee', amount: '1500' }, { amount: 5 }, { expenseId: 'c', amount: 'x' }] }],
    ['market-rate', { marketRate: '229000' }], ['market-rate-zero', { marketRate: 0 }],
  ];
  for (const [id, over] of variants) call(cases, `pricing/variant/${id}`, 'calculatePricing', PE.calculatePricing, [{ costResult: base, margin, gst, tier: 'value', pricedAt: FIXED_ISO, ...over }]);
  for (const [i, [m, t]] of [[margin, 'value'], [margin, 'PREMIUM'], [margin, null], [margin, 'x'], [{ marginType: 'MARKUP' }, 'value'], [{ marginType: 'OTHER' }, 'value'], [{}, 'base'], [{ tiers: { BASE: { targetMarginPct: '20', minimumMarginPct: 15 } }, version: 'v' }, 'base'], [{ tiers: { BASE: { targetMarginPct: 10, minimumMarginPct: 10 } } }, 'base']].entries()) {
    call(cases, `resolveTierMargin/${i}`, 'resolveTierMargin', PE.resolveTierMargin, [m, t]);
  }
  for (const [i, g] of [gst, {}, { ratePct: 18 }, { ratePct: '5' }, { regime: 'FLAT', ratePct: 'x' }, { regime: 'X' }, { ...gst, effectiveRatePct: '8.9' }, { ...gst, effectiveRatePct: 8.9001 }, { ...gst, goodsValuationPct: 69.9999999999 , serviceValuationPct: 30.0000000001 }, { regime: 'SOLAR_70_30_COMPOSITE', goodsValuationPct: 60, goodsRatePct: 12, serviceValuationPct: 40, serviceRatePct: 18 }].entries()) {
    call(cases, `resolveGstRegime/${i}`, 'resolveGstRegime', PE.resolveGstRegime, [g]);
  }
  for (const [i, m] of [0.2, 0, 0.999, 1, 1.5, -0.01, '0.3', null, 'abc', { $js: 'undefined' }].entries()) call(cases, `validateGrossMargin/${i}`, 'validateGrossMargin', PE.validateGrossMargin, [m]);
  const priced = cases.filter((c) => c.fn === 'calculatePricing' && c.result).slice(0, 8);
  for (const [i, c] of priced.entries()) cases.push({ id: `explainPricing/${i}`, fn: 'explainPricing', args: [c.result], ...capture(() => PE.explainPricing(c.result)) });
  const rejected = cases.find((c) => c.fn === 'calculatePricing' && c.result?.status === 'REJECTED');
  cases.push({ id: 'explainPricing/rejected', fn: 'explainPricing', args: [rejected.result], ...capture(() => PE.explainPricing(rejected.result)) });
  cases.push({ id: 'explainPricing/none', fn: 'explainPricing', args: [null], ...capture(() => PE.explainPricing(null)) });
  counts.pricing = writeGolden('pricing', cases);
}

// =============================================================================================================
// offerLifecycle.js
// =============================================================================================================
{
  const cases = [];
  const offer = (over = {}) => ({ id: 'offer_1', name: 'Monsoon', type: 'flat', value: 5000, appliesTo: 'all', appliesToTier: 'all', appliesToSize: 'all', startDate: '2026-01-01', endDate: '2026-12-31', status: 'ACTIVE', offerVersion: 1, createdAt: '2026-08-30T16:34:25.827Z', ...over });
  for (const [i, o] of [offer(), offer({ name: '  ' }), offer({ name: undefined }), offer({ type: 'bogo' }), offer({ type: undefined }), offer({ value: -1 }), offer({ value: 'x' }), offer({ value: null }), offer({ type: 'percentage', value: 120 }), offer({ type: 'percentage', value: '15' }), offer({ startDate: '2026-12-31', endDate: '2026-01-01' }), offer({ startDate: '', endDate: '2026-01-01' }), offer({ value: undefined, name: '﻿' })].entries()) {
    call(cases, `validate/${i}`, 'validateOffer', OF.validateOffer, [o]);
  }
  for (const [i, [lp, cost, o, min]] of [[250000, 200000, offer(), 0.15], [250000, 200000, offer({ type: 'percentage', value: 10 }), 0.15], [250000, 240000, offer(), 0.15], [5000, 1000, offer(), 0.15], [4000, 1000, offer(), 0.1], [0, 100, offer(), 0.1], [100, null, offer(), 0.1], [100000, 50000, offer({ type: 'other' }), 0.2], [100000, 50000, offer({ type: 'percentage', value: '25' }), '0.3'], [-5, 5, offer(), 0.1], [200000, 150000, offer(), null]].entries()) {
    call(cases, `marginSafety/${i}`, 'marginSafetyCheck', OF.marginSafetyCheck, [lp, cost, o, min]);
  }
  const statuses = ['DRAFT', 'APPROVED', 'ACTIVE', 'EXPIRED', 'ARCHIVED', 'PAUSED', ''];
  for (const from of statuses) for (const to of statuses.slice(0, 6)) call(cases, `isValid/${from || 'empty'}/${to}`, 'isValidTransition', OF.isValidTransition, [from, to]);
  for (const from of ['DRAFT', 'APPROVED', 'ACTIVE', 'EXPIRED', 'ARCHIVED', undefined]) {
    for (const to of ['APPROVED', 'ACTIVE', 'DRAFT', 'EXPIRED', 'ARCHIVED']) {
      const o = offer({ status: from, transitions: from === 'EXPIRED' ? undefined : [] });
      if (from === undefined) delete o.status;
      call(cases, `transition/${from}/${to}`, 'transitionOffer', OF.transitionOffer, [o, to, 'admin-001', from === 'ACTIVE' ? 'campaign over' : undefined]);
    }
  }
  for (const [i, f] of [{ name: 'Diwali', type: 'percentage', value: '7.5', appliesTo: 'ongrid', startDate: '2026-10-01', endDate: '2026-11-15' }, {}, { id: 'custom', value: 'x', description: 'd' }, { value: 0, appliesToTier: 'value', appliesToSize: '3' }].entries()) {
    call(cases, `create/${i}`, 'createDraftOffer', OF.createDraftOffer, [f, 'admin-001']);
  }
  for (const [i, [o, u]] of [[offer({ status: 'DRAFT' }), { name: 'New', value: 6000 }], [offer({ status: 'APPROVED' }), { value: 5000 }], [offer({ status: 'APPROVED', transitions: [] }), { value: '5000', endDate: '2027-01-01' }], [offer({ status: 'ACTIVE' }), { name: 'x' }], [offer({ status: 'EXPIRED' }), {}], [offer({ status: 'DRAFT', offerVersion: undefined }), { description: 'more', status: 'ACTIVE' }], [offer({ status: 'DRAFT' }), { name: undefined }], [offer({ status: 'DRAFT' }), { appliesTo: null }]].entries()) {
    call(cases, `update/${i}`, 'updateOffer', OF.updateOffer, [o, u, 'admin-002']);
  }
  const pool = [offer({ id: 'o1', endDate: '2026-09-01' }), offer({ id: 'o2', status: 'DRAFT', endDate: '2026-01-01' }), offer({ id: 'o3', endDate: '' }), offer({ id: 'o4', endDate: '2026-09-20' }), offer({ id: 'o5', endDate: '2026-09-19', transitions: [{ from: 'x' }] })];
  call(cases, 'autoExpire/today', 'autoExpireOffers', (offers, asOf) => ({ expired: OF.autoExpireOffers(offers, asOf), offers }), [pool, { $js: 'undefined' }]);
  call(cases, 'autoExpire/asOf', 'autoExpireOffers', (offers, asOf) => ({ expired: OF.autoExpireOffers(offers, asOf), offers }), [pool, '2026-12-31']);
  call(cases, 'autoExpire/not-array', 'autoExpireOffers', (offers, asOf) => ({ expired: OF.autoExpireOffers(offers, asOf), offers }), [null, '2026-12-31']);
  const findPool = [
    offer({ id: 'f1', createdAt: '2026-08-01T00:00:00.000Z' }), offer({ id: 'f2', createdAt: '2026-09-01T00:00:00.000Z', appliesTo: 'hybrid' }),
    offer({ id: 'f3', createdAt: '2026-09-10T00:00:00.000Z', appliesToTier: 'premium' }), offer({ id: 'f4', createdAt: '2026-09-15T00:00:00.000Z', appliesToSize: '5sp' }),
    offer({ id: 'f5', createdAt: '2026-09-18T00:00:00.000Z', status: 'APPROVED' }), offer({ id: 'f6', createdAt: '2026-09-19T00:00:00.000Z', startDate: '2026-10-01' }),
    offer({ id: 'f7', createdAt: '2026-09-17T00:00:00.000Z', endDate: '2026-09-19' }), offer({ id: 'f8', createdAt: undefined }), offer({ id: 'f9', createdAt: '2026-09-15T00:00:00.000Z', appliesToSize: '5sp' }),
  ];
  for (const [i, crit] of [{ systemType: 'ongrid', tier: 'value', size: '3' }, { systemType: 'hybrid', tier: 'value', size: '3' }, { systemType: 'ongrid', tier: 'premium', size: '3' }, { systemType: 'ongrid', tier: 'value', size: '5sp' }, { systemType: 'ongrid', tier: 'value', size: '3', date: '2026-10-05' }, { systemType: 'ongrid', tier: 'value', size: '3', date: '2027-01-01' }, { systemType: 'ongrid', tier: 'value', size: '3', date: '2026-09-18' }, {}].entries()) {
    call(cases, `find/${i}`, 'findApplicableOffer', OF.findApplicableOffer, [findPool, crit]);
  }
  call(cases, 'find/none', 'findApplicableOffer', OF.findApplicableOffer, [null, {}]);
  call(cases, 'find/empty', 'findApplicableOffer', OF.findApplicableOffer, [[], { systemType: 'ongrid' }]);
  call(cases, 'find/catalog', 'findApplicableOffer', OF.findApplicableOffer, [catalog.offers, { systemType: 'ongrid', tier: 'value', size: '3' }]);
  for (const [i, [o, lp]] of [[offer(), 210285], [offer({ value: 300000 }), 210285], [offer({ type: 'percentage', value: 5 }), 210285], [offer({ type: 'percentage', value: '2.5' }), 99999.5], [offer({ type: 'x' }), 1], [offer(), 0], [null, 100], [offer(), -1], [offer({ value: 'abc' }), 100]].entries()) {
    call(cases, `amount/${i}`, 'calculateOfferAmount', OF.calculateOfferAmount, [o, lp]);
    call(cases, `payload/${i}`, 'offerPayloadShape', OF.offerPayloadShape, [o, lp]);
  }
  counts.offers = writeGolden('offers', cases);
}

// =============================================================================================================
// batteryMaster.js / batteryCompatibility.js / resolveBattery.js
// =============================================================================================================
{
  const cases = [];
  const master = batteryMasterFile.batteries;
  const items = catalog.categories.battery.items;
  const complete = { id: 'syn_deye', name: 'Deye LV 5.12', brand: 'Deye', tiers: ['value'], price: 90000 };
  const completeMaster = { brand: 'Deye', model: 'SE-G5.1', nominalVoltage: 51.2, capacityKwh: 5.12, maximumDischargeCurrent: 100, integratedProtection: false, externalProtectionRequired: true, protectionRating: '125A DC MCCB', communicationProtocol: 'CAN', communicationRequired: true, compatibleInverters: ['i58', 'syn_inv'], compatibleSystemTypes: ['hybrid'], compatiblePhases: ['1P', '3P'], engineeringStatus: 'APPROVED' };
  const enphase = { id: 'syn_enph', name: 'IQ Battery 5P', brand: 'Enphase', tiers: ['premium'] };
  const enphaseMaster = { integratedProtection: true, capacityKwh: 5, nominalVoltage: 67, maximumDischargeCurrent: 20, communicationRequired: false, compatibleInverters: 'all', compatibleSystemTypes: ['hybrid', 'ongrid'], compatiblePhases: [], engineeringStatus: 'APPROVED', procurementStatus: 'PENDING_SOURCING' };
  const allItems = [...items, complete, enphase, { id: 'syn_inactive', name: 'Old', brand: 'X', tiers: [], status: 'INACTIVE' }];
  const allMaster = { ...master, syn_deye: completeMaster, syn_enph: enphaseMaster, syn_inactive: { engineeringStatus: 'WITHDRAWN' } };
  const syntheticCatalog = { categories: { battery: { items: allItems } } };
  const local = { syntheticCatalog, allMaster };
  for (const item of allItems) {
    call(cases, `master/${item.id}`, 'toBatteryMaster', BM.toBatteryMaster, [item, allMaster[item.id] || {}]);
    call(cases, `master/raw/${item.id}`, 'toBatteryMaster', BM.toBatteryMaster, [item]);
  }
  call(cases, 'master/null', 'toBatteryMaster', BM.toBatteryMaster, [null]);
  call(cases, 'master/overlay-price', 'toBatteryMaster', BM.toBatteryMaster, [{ id: 'x', price: 5, tiers: 'value' }, { purchasePrice: null, compatiblePhases: '1P', componentId: 'y', displayName: 'Shown' }]);
  call(cases, 'allBatteries/real', 'allBatteries', BM.allBatteries, [{ $ref: 'catalog' }, { $ref: 'batteryMaster' }]);
  call(cases, 'allBatteries/none', 'allBatteries', BM.allBatteries, [{}]);
  const records = allItems.map((i) => BM.toBatteryMaster(i, allMaster[i.id] || {}));
  const statusRecords = [...records, { engineeringStatus: 'APPROVED', procurementStatus: 'DISCONTINUED' }, { engineeringStatus: 'APPROVED', status: 'TEST' }, { engineeringStatus: 'REJECTED' }, null];
  for (const [i, r] of statusRecords.entries()) {
    call(cases, `selectable/${i}`, 'isSelectable', BM.isSelectable, [r]);
    call(cases, `permanent/${i}`, 'isPermanentlyUnselectable', BM.isPermanentlyUnselectable, [r]);
    call(cases, `reason/${i}`, 'selectabilityReason', BM.selectabilityReason, [r]);
    call(cases, `protection/${i}`, 'resolveProtectionRequirement', BC.resolveProtectionRequirement, [r]);
  }
  call(cases, 'protection/optional', 'resolveProtectionRequirement', BC.resolveProtectionRequirement, [{ externalProtectionRequired: false }]);
  const inverters = [
    null,
    { id: 'syn_inv', name: 'Deye SUN-5K', brand: 'Deye', batteryVoltageMin: 40, batteryVoltageMax: 60, maxBatteryCurrent: 100, communicationProtocols: ['CAN', 'RS485'] },
    { id: 'i58', name: 'Hybrid 5 kW', batteryVoltageMin: 150, batteryVoltageMax: 500, maxBatteryCurrent: 120, communicationProtocols: ['RS485'] },
    { id: 'i60', batteryVoltageMin: 40, maxBatteryCurrent: 50 },
  ];
  let n = 0;
  for (const r of records) {
    for (const inverter of inverters) {
      for (const architecture of [null, 'DEYE', 'ENPHASE']) {
        const ctx = { inverter, architecture, sysType: ['hybrid', 'ongrid', 'hybrid'][n % 3], phase: [null, '1P', '3P', undefined][n % 4], quantity: [1, 2, 0, 1, null][n % 5] };
        if (ctx.phase === undefined) delete ctx.phase;
        call(cases, `compat/${r.componentId}/${inverter?.id ?? 'none'}/${architecture}/${n++}`, 'checkBatteryCompatibility', BC.checkBatteryCompatibility, [r, ctx]);
      }
    }
  }
  call(cases, 'compat/null', 'checkBatteryCompatibility', BC.checkBatteryCompatibility, [null, {}]);
  call(cases, 'compat/defaults', 'checkBatteryCompatibility', BC.checkBatteryCompatibility, [records[5]]);
  call(cases, 'compat/string-quantity', 'checkBatteryCompatibility', BC.checkBatteryCompatibility, [records[5], { quantity: 'two', sysType: null, inverter: inverters[1] }]);
  call(cases, 'compat/rating-missing', 'checkBatteryCompatibility', BC.checkBatteryCompatibility, [{ ...records[5], protectionRating: null, missingForSafeIssue: [] }, { quantity: 1 }]);
  call(cases, 'compat/protection-unknown', 'checkBatteryCompatibility', BC.checkBatteryCompatibility, [{ ...records[5], integratedProtection: false, externalProtectionRequired: null, missingForSafeIssue: undefined }, { quantity: 1 }]);
  n = 0;
  const profiles = Object.entries(catalog.packageProfiles);
  const overrides = [null, { componentId: 'bt2', selectedBy: 'ph-1', at: FIXED_ISO }, { componentId: 'bt1' }, { componentId: 'nope' }, { componentId: 'ba_3b51aef23f0b' }, { componentId: 'syn_deye', selectedBy: 'ph-2' }, {}];
  for (const [key, profile] of profiles) {
    for (const withMaster of [true, false]) {
      for (const projectQty of [null, 0, 1, 2, 3]) {
        const sysType = key.startsWith('hybrid') ? 'hybrid' : ['ongrid', 'hybrid'][n % 2];
        const tier = key.includes('premium') || key === 'premium' ? 'premium' : key.includes('base') ? 'base' : 'value';
        const ctx = { sysType, tier, projectOverride: overrides[n % overrides.length], projectBatteryQuantity: projectQty };
        if (withMaster) ctx.batteryMaster = { $ref: 'allMaster' };
        call(cases, `resolve/${key}/${withMaster ? 'master' : 'legacy'}/${projectQty}/${n++}`, 'resolveBattery', RB.resolveBattery, [profile, { $ref: 'syntheticCatalog' }, ctx], local);
      }
    }
  }
  call(cases, 'resolve/no-context', 'resolveBattery', RB.resolveBattery, [catalog.packageProfiles.hybrid_value, { $ref: 'catalog' }]);
  call(cases, 'resolve/inconsistent-profile', 'resolveBattery', RB.resolveBattery, [{ batteryIncluded: true, batteryQuantity: 0 }, { $ref: 'catalog' }, { sysType: 'hybrid', batteryMaster: { $ref: 'batteryMaster' } }]);
  call(cases, 'resolve/profile-component-missing', 'resolveBattery', RB.resolveBattery, [{ batteryIncluded: true, batteryQuantity: 2, batteryComponentId: 'bt9' }, { $ref: 'catalog' }, { sysType: 'hybrid', tier: 'base', batteryMaster: {} }]);
  call(cases, 'resolve/no-candidate', 'resolveBattery', RB.resolveBattery, [{ batteryIncluded: true, batteryQuantity: 1 }, { $ref: 'catalog' }, { sysType: 'hybrid', tier: 'gold', batteryMaster: { $ref: 'batteryMaster' } }]);
  call(cases, 'resolve/no-catalog', 'resolveBattery', RB.resolveBattery, [null, null, { sysType: 'hybrid', projectBatteryQuantity: 1 }]);
  for (const tier of ['base', 'value', 'premium', null]) {
    for (const withMaster of [true, false]) {
      call(cases, `shortlist/${tier}/${withMaster}`, 'approvedBatteryShortlist', RB.approvedBatteryShortlist, withMaster ? [{ $ref: 'syntheticCatalog' }, { $ref: 'allMaster' }, tier ? { tier } : {}] : [{ $ref: 'catalog' }], local);
    }
  }
  for (const [i, [cat, name]] of [['enphase', 'Enphase IQ Flex Battery 5P'], ['enphase', 'flex battery'], ['battery', 'Flex Battery'], ['enphase', 'IQ8P'], ['enphase', null], ['enphase', 42]].entries()) {
    call(cases, `alias/${i}`, 'isBatteryAlias', RB.isBatteryAlias, [cat, name]);
    call(cases, `canonical/${i}`, 'canonicalBatteryIdFor', RB.canonicalBatteryIdFor, [cat, name]);
  }
  counts.battery = writeGolden('battery', cases, { fixtures: local });
}

// =============================================================================================================
// commercialHistory.js / projectRateCard.js
// =============================================================================================================
{
  const cases = [];
  const audit = { changedBy: 'admin-001', changedAt: '2026-09-01T00:00:00.000Z', changeReason: 'seed', effectiveFrom: '2026-09-01', cardVersion: 'card-1' };
  call(cases, 'seed/cost-config', 'seedRateCardFromConfig', RC.seedRateCardFromConfig, [{ $ref: 'costConfig' }, audit]);
  call(cases, 'seed/empty', 'seedRateCardFromConfig', RC.seedRateCardFromConfig, [{}, audit]);
  call(cases, 'seed/minimal', 'seedRateCardFromConfig', RC.seedRateCardFromConfig, [{ installation: { rates: [{ systemSizeKw: 5, installationType: 'SHEET', unit: 'PER_PROJECT', rate: 26000 }] }, transportation: { vehicles: [{ vehicleType: 'PICKUP', ratePerKm: 30, versionId: 'VEH-P' }] }, siteSurvey: { costPerProject: 500, coverageKm: 100 }, engineeringDesign: { costPerProject: 500, sizeLimitKw: 20 }, serviceAmc: { costPerVisit: 1000, visitsPerYear: 2, serviceYears: 5, sizeLimitKw: 10 }, specialProjectWorks: { rates: [{ workType: 'CRANE', rate: 8000, unit: 'PER_PROJECT' }] } }, audit]);
  call(cases, 'seed/missing-audit', 'seedRateCardFromConfig', RC.seedRateCardFromConfig, [{ transportation: { vehicles: [{ vehicleType: 'PICKUP', ratePerKm: 30 }] } }, { ...audit, changeReason: '' }]);
  for (const [i, [type, rec, at]] of [['TRANSPORT_VEHICLE_RATE', 'STANDARD', null], ['TRANSPORT_VEHICLE_RATE', 'STANDARD', '2026-09-06'], ['TRANSPORT_VEHICLE_RATE', 'STANDARD', '2026-09-12T06:00:00.000Z'], ['TRANSPORT_VEHICLE_RATE', 'LORRY', '2026-01-01'], ['TRANSPORT_VEHICLE_RATE', 'LORRY', null], ['INSTALLATION_RATE', '3kW/FLAT', null], ['TRANSPORT_VEHICLE_RATE', 'BIKE', '2026-09-10']].entries()) {
    call(cases, `resolve/real/${i}`, 'resolveRate', RC.resolveRate, [{ $ref: 'rateCard' }, { rateType: type, recordId: rec, at }]);
  }
  call(cases, 'resolve/no-card', 'resolveRate', RC.resolveRate, [null, { rateType: 'SITE_SURVEY_RATE', recordId: 'STANDARD' }]);
  let card = RC.createRateCard({ cardVersion: 'c1' });
  const history = [];
  const steps = [
    { rateType: 'SITE_SURVEY_RATE', recordId: 'STANDARD', value: { costPerProject: 500, coverageKm: 100 }, versionId: 'S1', changedBy: 'ph', changedAt: '2026-09-01T00:00:00.000Z', changeReason: 'seed', effectiveFrom: '2026-09-01' },
    { rateType: 'SITE_SURVEY_RATE', recordId: 'STANDARD', value: { costPerProject: 550, coverageKm: 100, excessRatePerKm: 3 }, versionId: 'S2', changedBy: 'ph', changedAt: '2026-09-05T00:00:00.000Z', changeReason: 'rise', effectiveFrom: '2026-09-05', effectiveTo: '2026-12-31' },
    { rateType: 'SITE_SURVEY_RATE', recordId: 'STANDARD', value: { costPerProject: 600, coverageKm: 90 }, versionId: 'S3', changedBy: 'ph', changedAt: '2026-09-05T00:00:00.000Z', changeReason: 'fix', effectiveFrom: '2026-09-10', cardVersion: 'c2' },
    { rateType: 'SITE_SURVEY_RATE', recordId: 'STANDARD', value: {}, versionId: 'S3', changedBy: 'ph', changedAt: 'x', changeReason: 'dup', effectiveFrom: 'x' },
    { rateType: 'NOT_A_TYPE', recordId: 'X', value: {}, versionId: 'N1', changedBy: 'ph', changedAt: 'x', changeReason: 'x', effectiveFrom: 'x' },
    { rateType: 'SERVICE_RATE', recordId: 'STANDARD', value: { costPerVisit: 1000 }, versionId: 'V1', changedBy: null, changedAt: 'x', changeReason: 'x', effectiveFrom: 'x' },
  ];
  for (const [i, s] of steps.entries()) {
    const out = capture(() => RC.setRate(card, s));
    if (out.result) card = out.result;
    history.push({ op: 'setRate', args: s, ...out });
  }
  cases.push({ id: 'history/setRate-sequence', fn: 'setRateSequence', args: [RC.createRateCard({ cardVersion: 'c1' }), steps], result: history });
  for (const [i, at] of [null, '2026-08-31', '2026-09-01', '2026-09-07', '2026-09-10', '2027-01-01'].entries()) {
    call(cases, `history/resolve/${i}`, 'resolveRate', RC.resolveRate, [card, { rateType: 'SITE_SURVEY_RATE', recordId: 'STANDARD', at }]);
  }
  const archived = { ...card, store: CH.archiveVersion(card.store, { configType: 'SITE_SURVEY_RATE', recordId: 'STANDARD', versionId: 'S3', changedBy: 'admin', changedAt: '2026-09-11', changeReason: 'wrong' }) };
  cases.push({ id: 'history/archive', fn: 'archiveVersion', args: [card.store, { configType: 'SITE_SURVEY_RATE', recordId: 'STANDARD', versionId: 'S3', changedBy: 'admin', changedAt: '2026-09-11', changeReason: 'wrong' }], result: clone(archived.store) });
  call(cases, 'history/resolve-archived', 'resolveRate', RC.resolveRate, [archived, { rateType: 'SITE_SURVEY_RATE', recordId: 'STANDARD' }]);
  call(cases, 'history/resolve-archived-at', 'resolveRate', RC.resolveRate, [archived, { rateType: 'SITE_SURVEY_RATE', recordId: 'STANDARD', at: '2026-09-12' }]);
  call(cases, 'history/getHistory', 'getHistory', CH.getHistory, [card.store, 'SITE_SURVEY_RATE', 'STANDARD']);
  call(cases, 'history/getHistory-real', 'getHistory', CH.getHistory, [{ $ref: 'rateCard.store' }, 'TRANSPORT_VEHICLE_RATE', 'LORRY'], { 'rateCard.store': rateCardFile.store });
  call(cases, 'history/getVersion', 'getVersion', CH.getVersion, [card.store, 'SITE_SURVEY_RATE', 'STANDARD', 'S2']);
  call(cases, 'history/getVersion-missing', 'getVersion', CH.getVersion, [card.store, 'SITE_SURVEY_RATE', 'STANDARD', 'S9']);
  const [v1, v2, v3] = CH.getHistory(card.store, 'SITE_SURVEY_RATE', 'STANDARD');
  call(cases, 'history/compare-1-2', 'compareVersions', CH.compareVersions, [v1, v2]);
  call(cases, 'history/compare-2-3', 'compareVersions', CH.compareVersions, [v2, v3]);
  call(cases, 'history/compare-null', 'compareVersions', CH.compareVersions, [null, v2]);
  call(cases, 'history/view', 'historyView', CH.historyView, [card.store, 'SITE_SURVEY_RATE', 'STANDARD']);
  call(cases, 'history/view-empty', 'historyView', CH.historyView, [card.store, 'MARGIN', 'BASE']);
  call(cases, 'history/view-real', 'historyView', CH.historyView, [{ $ref: 'rateCard.store' }, 'TRANSPORT_VEHICLE_RATE', 'STANDARD'], { 'rateCard.store': rateCardFile.store });
  call(cases, 'listRates/real', 'listRates', RC.listRates, [{ $ref: 'rateCard' }]);
  call(cases, 'listRates/card', 'listRates', RC.listRates, [archived]);
  for (const [i, [kw, t]] of [[3, 'FLAT'], [5.5, 'SHEET'], [10, null]].entries()) call(cases, `installationKey/${i}`, 'installationKey', RC.installationKey, [kw, t]);
  for (const [i, [kw, lim]] of [[5, 20], [20, 20], [25, 20], ['30', '20']].entries()) call(cases, `engineeringKey/${i}`, 'engineeringKey', RC.engineeringKey, [kw, lim]);
  counts.rate_card = writeGolden('rate_card', cases);
}

// ---- fixtures -----------------------------------------------------------------------------------------------
fs.mkdirSync(path.join(HERE, 'fixtures'), { recursive: true });
writeJson(path.join(HERE, 'fixtures', 'flarize_commercial.json'), { generator: 'engines/tests/golden/generate_commercial.mjs', sources: sourceHashes, ...FIXTURES });

const total = Object.values(counts).reduce((a, b) => a + b, 0);
console.log(JSON.stringify({ total, ...counts }));
