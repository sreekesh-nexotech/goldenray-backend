#!/usr/bin/env node
// Golden parity capture for engines-rules (engineering_checker, bom_domain, gate, quotation_payload, content_fit).
//
// Imports the REAL Flarize modules from FLARIZE_ROOT (default /home/user/flarize-main/flarize: src/lib, scripts,
// server-bom-builder.js, data/) and writes engines/tests/golden/rules_*.json next to this file. The Python tests
// (engines/tests/test_rules_parity.py) replay every case against engines/*.py and compare the outputs exactly.
//
//   TZ=UTC /opt/node22/bin/node engines/tests/golden/generate_rules.mjs
//
// Deterministic: no randomness, TZ=UTC, the clock is frozen, every file records the sha256 of the JavaScript sources
// and data files it was captured from. Personal data from data/quotation-state.json (customer name, phone, e-mail,
// address, pincode, salutation, the salesperson's display name) and bank account numbers are masked BEFORE the real
// modules run, so the committed goldens hold no personal data and still reproduce the frozen documents exactly.

process.env.TZ = 'UTC';

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = path.resolve(process.env.FLARIZE_ROOT || '/home/user/flarize-main/flarize');
const LIB = path.join(ROOT, 'src', 'lib');
const DATA = path.join(ROOT, 'data');
const OUT = path.resolve(process.env.GOLDEN_OUT || path.dirname(fileURLToPath(import.meta.url)));
const FIXED_NOW = '2026-09-28T00:00:00.000Z';
const SCHEMA = 'engines-rules.golden/1';

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

const load = (rel) => import(pathToFileURL(path.join(ROOT, rel)).href);
const require = createRequire(path.join(ROOT, 'package.json'));

const checker = await load('src/lib/engineeringChecker.js');
const validation = await load('src/lib/engineeringValidation.js');
const validateBom = await load('scripts/validate-bom.mjs');
const battery = await load('src/lib/batteryCompatibility.js');
const batteryMaster = await load('src/lib/batteryMaster.js');
const upgradeModel = await load('src/lib/upgradeModel.js');
const roles = await load('src/lib/bomRoles.js');
const identity = await load('src/lib/componentIdentity.js');
const lifecycle = await load('src/lib/catalogLifecycle.js');
const projectBom = await load('src/lib/projectBom.js');
const bomSnapshot = await load('src/lib/bomSnapshot.js');
const bomLock = await load('src/lib/bomLock.js');
const bomStatus = await load('src/lib/bomStatus.js');
const payloadLib = await load('src/lib/quotationPayload.js');
const workspace = await load('src/lib/quotationWorkspace.js');
const commercialSnapshot = await load('src/lib/commercialSnapshot.js');
const commercialFreeze = await load('src/lib/commercialFreeze.js');
const policyLib = await load('src/lib/quotationPolicy.js');
const content = await load('src/lib/quotationContent.js');
const branding = await load('src/lib/quotationBranding.js');
const packageApproval = await load('src/lib/packageApproval.js');
const packPricing = await load('src/lib/packPricing.js');
const costEngine = await load('src/lib/costEngine.js');
const pricingEngine = await load('src/lib/pricingEngine.js');
const bomBuilder = require(path.join(ROOT, 'server-bom-builder.js'));

const SOURCES = [
  'src/lib/engineeringChecker.js', 'src/lib/engineeringValidation.js', 'scripts/validate-bom.mjs', 'src/lib/batteryCompatibility.js',
  'src/lib/batteryMaster.js', 'src/lib/resolveBattery.js', 'src/lib/upgradeModel.js', 'src/lib/bomRoles.js', 'src/lib/componentIdentity.js',
  'src/lib/catalogLifecycle.js', 'src/lib/projectBom.js', 'src/lib/bomSnapshot.js', 'src/lib/bomLock.js', 'src/lib/bomStatus.js',
  'src/lib/quotationPayload.js', 'src/lib/quotationWorkspace.js', 'src/lib/commercialSnapshot.js', 'src/lib/commercialFreeze.js',
  'src/lib/quotationPolicy.js', 'src/lib/quotationContent.js', 'src/lib/quotationBranding.js', 'src/lib/packageApproval.js',
  'src/lib/packPricing.js', 'server-bom-builder.js',
  'data/catalog.json', 'data/pack-config.json', 'data/battery-master.json', 'data/quotation-state.json', 'data/quotation-content.json',
  'data/quotation-branding-state.json', 'data/quotation-policy.json', 'data/tier-display-names.json', 'data/quotation-inclusions.json',
  'data/quotation-testimonials.json', 'data/cost-config.json', 'data/procurement-price-master.json',
];
const sha256 = (rel) => crypto.createHash('sha256').update(fs.readFileSync(path.join(ROOT, rel))).digest('hex');
const SOURCE_HASHES = Object.fromEntries(SOURCES.map((rel) => [rel, sha256(rel)]));

const readJson = (name) => JSON.parse(fs.readFileSync(path.join(DATA, name), 'utf8'));
const plain = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
const clone = plain;
const AT = '2026-09-20T10:00:00.000Z';

function captured(fn) {
  try {
    return { output: plain(fn()) };
  } catch (e) {
    return { error: { code: e.code ?? null, message: e.message, detail: plain(e.detail ?? null) } };
  }
}

// Large shared values (the catalogue content, stored snapshots, stores) are written once under "refs"; any value deep-
// equal to a ref is written as {"$ref": name}. The Python loader resolves them before replaying.
function compactor(refs) {
  const index = new Map(Object.entries(refs).map(([name, value]) => [JSON.stringify(value), name]));
  const compact = (value) => {
    if (value === null || typeof value !== 'object') return value;
    const text = JSON.stringify(value);
    if (index.has(text)) return { $ref: index.get(text) };
    if (Array.isArray(value)) return value.map(compact);
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, compact(v)]));
  };
  return compact;
}

function write(name, header, sections, refs = {}) {
  const compact = compactor(refs);
  const lines = [];
  let count = 0;
  for (const [section, cases] of Object.entries(sections)) {
    lines.push(`${JSON.stringify(section)}: [\n${cases.map((c) => JSON.stringify(compact(plain(c)))).join(',\n')}\n]`);
    count += cases.length;
  }
  const head = JSON.stringify({ schema: SCHEMA, fixedNow: FIXED_NOW, sources: SOURCE_HASHES, ...header, caseCount: count });
  const refText = Object.entries(refs).map(([k, v]) => `${JSON.stringify(k)}: ${JSON.stringify(v)}`).join(',\n');
  fs.writeFileSync(path.join(OUT, name), `{"header": ${head},\n"refs": {\n${refText}\n},\n"sections": {\n${lines.join(',\n')}\n}}\n`);
  console.log(`${name}: ${count} cases, ${Object.keys(refs).length} refs`);
}

// Lift an unexported function's source text verbatim (its sha256 is in the header via the module hash).
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
const workspaceSource = fs.readFileSync(path.join(LIB, 'quotationWorkspace.js'), 'utf8');
const lift = (name, params, args) => new Function(...params, `${extractFunction(workspaceSource, name)}\nreturn ${name};`)(...args);
const deriveSubsidyTreatment = lift('deriveSubsidyTreatment', [], []);
const resolveInputs = lift('resolveInputs', ['QUOTATION_SOURCE'], [workspace.QUOTATION_SOURCE]);
const resolvePanelDcrType = lift('resolvePanelDcrType', [], []);
const normalizeRendererPinning = lift('normalizeRendererPinning', ['QuotationError', 'QUOTATION_ERROR'], [
  class QuotationError extends Error { constructor(code, message, detail) { super(message); this.code = code; this.detail = detail; } },
  workspace.QUOTATION_ERROR,
]);

// ---- masking -----------------------------------------------------------------------------------------------------
const maskedCustomers = new Map();
function maskCustomer(customer) {
  if (!customer || typeof customer !== 'object') return customer;
  const key = customer.customerId ?? customer.phone ?? JSON.stringify(customer);
  if (!maskedCustomers.has(key)) maskedCustomers.set(key, maskedCustomers.size + 1);
  const n = maskedCustomers.get(key);
  const out = { ...customer };
  const replace = (field, value) => { if (field in out && out[field] != null) out[field] = value; };
  replace('customerId', `CUST-MASKED-${String(n).padStart(3, '0')}`);
  replace('customerName', `Customer ${n}`);
  replace('name', `Customer ${n}`);
  replace('salutationName', `Customer ${n}`);
  replace('phone', `90000${String(n).padStart(5, '0')}`);
  replace('alternatePhone', `90001${String(n).padStart(5, '0')}`);
  replace('email', `customer${n}@example.invalid`);
  replace('address', `Masked address ${n}`);
  replace('pincode', '680001');
  return out;
}
const maskBankValues = (values) => {
  if (!values || typeof values !== 'object') return values;
  const out = { ...values };
  for (const key of ['accountNumber', 'bankAccountNumber', 'bankAccountName']) if (out[key] != null) out[key] = 'MASKED-ACCOUNT';
  return out;
};
function maskPayload(p) {
  if (!p || typeof p !== 'object') return p;
  const out = { ...p };
  if (out.customer && out.customer.source) out.customer = maskCustomer(out.customer);
  if (out.quotation && out.quotation.proposalBy != null) out.quotation = { ...out.quotation, proposalBy: 'Sales Person' };
  if (out.company && out.company.value) out.company = { ...out.company, value: maskBankValues(out.company.value) };
  if (Array.isArray(out.alternatives)) out.alternatives = out.alternatives.map((a) => ({ ...a, payload: maskPayload(a.payload) }));
  return out;
}
function maskBrandingSnapshot(b) {
  if (!b || typeof b !== 'object') return b;
  const out = {};
  for (const [k, v] of Object.entries(b)) out[k] = v && typeof v === 'object' ? { ...v, values: maskBankValues(v.values) } : v;
  return out;
}
function maskBrandingStore(store) {
  const s = clone(store);
  for (const kind of ['bank', 'upi']) {
    for (const acc of Object.values(s[kind]?.accounts || {})) for (const v of Object.values(acc.versions || {})) v.values = maskBankValues(v.values);
  }
  return s;
}

// =================================================================================================================
// Catalogue fixture (the approved catalogue the checker reads: catalog.json + the approved pack-config overlay)
// =================================================================================================================
const packConfig = readJson('pack-config.json');
bomBuilder.setPackConfigProvider((source) => (source === 'approved' ? packConfig.approved.config : packConfig.draft.config));
const fullCatalog = bomBuilder.loadCatalog('approved');
const STRIPPED = new Set(['changeLog', 'createdBy', 'createdAt', 'updatedBy', 'updatedAt', 'warrantyUpdatedBy', 'warrantyUpdatedAt', 'landedUnitCost', 'purchasePrice', 'supplier']);
const stripItem = (item) => Object.fromEntries(Object.entries(item).filter(([k]) => !STRIPPED.has(k)));
const catalog = {
  categories: Object.fromEntries(Object.entries(fullCatalog.categories).map(([k, c]) => [k, { label: c.label, items: (c.items || []).map(stripItem) }])),
  bomTemplates: Object.fromEntries(Object.entries(fullCatalog.bomTemplates).map(([k, t]) => [k, { sizes: t.sizes, threePhase: t.threePhase, tiers: t.tiers }])),
  packageProfiles: fullCatalog.packageProfiles,
};
const BATTERY_MASTER = readJson('battery-master.json').batteries;
const CATALOG_VERSION = 'catalog@golden';

write('rules_catalog.json', { note: 'The approved catalogue (catalog.json + approved pack-config overlay; internal cost fields and change logs stripped) and the battery master overlay.' }, {
  catalog: [{ id: 'approved', catalog, batteryMaster: BATTERY_MASTER, catalogVersion: CATALOG_VERSION }],
});

// =================================================================================================================
// Checker fixtures: a small explicit catalogue whose items trigger every PBC rule
// =================================================================================================================
const T = ['base', 'value', 'premium'];
const FX_CATALOG = {
  categories: {
    panel: { items: [
      { id: 'fx_pnl', name: 'Fixture Panel 540', brand: 'Waaree', watt: 540, voc: 49.5, isc: 13.9, tempCoeffVoc: -0.27, maxSysVoltage: 1500, panelType: 'DCR', dcr: true, tiers: T, price: 13585 },
      { id: 'fx_pnl_nodata', name: 'Panel Without Data', brand: 'Acme', watt: 500, tiers: T, price: 12000 },
      { id: 'fx_pnl_lowsys', name: 'Panel 600V', brand: 'Acme', watt: 400, voc: 41.2, isc: 11.1, tempCoeffVoc: -0.3, maxSysVoltage: 600, tiers: T, price: 9000 },
      { id: 'fx_pnl_hot', name: 'String Module 1100V', brand: 'Acme', watt: 550, voc: 1100, isc: 10, tempCoeffVoc: -0.3, maxSysVoltage: 1500, tiers: T, price: 15000 },
      { id: 'fx_pnl_edge', name: 'Edge Module', brand: 'Acme', watt: 550, voc: '975', isc: 10, tempCoeffVoc: '-0.3', maxSysVoltage: 1500, tiers: T, price: 15000 },
      { id: 'fx_pnl_test', name: 'Test Panel', brand: 'Acme', watt: 540, voc: 49.5, isc: 13.9, tempCoeffVoc: -0.27, maxSysVoltage: 1500, status: 'TEST', tiers: T, price: 0 },
      { id: 'fx_pnl_inactive', name: 'Retired Panel', brand: 'Acme', watt: 540, voc: 49.5, isc: 13.9, tempCoeffVoc: -0.27, maxSysVoltage: 1500, status: 'inactive', tiers: T, price: 1 },
    ] },
    inverter: { items: [
      { id: 'fx_inv_1p', name: 'Deye 5kW 1P', brand: 'Deye', kw: 5, phase: '1P', type: 'ongrid', maxInputVoltage: 1000, mpptCount: 2, maxStringsPerMppt: 1, tiers: T, price: 40000 },
      { id: 'fx_inv_3p', name: 'Deye 10kW 3P', brand: 'Deye', kw: 10, phase: '3P', type: 'ongrid', maxInputVoltage: 1000, mpptCount: 2, tiers: T, price: 80000 },
      { id: 'fx_inv_hyb', name: 'Deye Hybrid 5kW', brand: 'Deye', kw: 5, phase: 'HYB', type: 'hybrid', maxInputVoltage: 500, batteryVoltageMin: 40, batteryVoltageMax: 60, maxBatteryCurrent: 100, communicationProtocols: ['CAN', 'RS485'], tiers: T, price: 90000 },
      { id: 'fx_inv_hyb_str', name: 'Hybrid with text protocols', brand: 'Growatt', kw: 5, phase: 'HYB', type: 'hybrid', maxInputVoltage: 550, batteryVoltageMin: 40, batteryVoltageMax: 60, maxBatteryCurrent: 90, communicationProtocols: 'CAN/RS485', tiers: T, price: 85000 },
      { id: 'fx_inv_nomax', name: 'Legacy Inverter', brand: 'Deye', kw: 5, phase: '1P', type: 'ongrid', tiers: T, price: 30000 },
    ] },
    isolator: { items: [
      { id: 'is1', name: 'AC Isolator 1P', brand: 'Havells', phase: '1P', tiers: T, price: 900 },
      { id: 'is2', name: 'AC Isolator 3P', brand: 'Havells', phase: '3P', tiers: T, price: 1500 },
      { id: 'fx_iso', name: 'Other Isolator', brand: 'Havells', phase: '1P', tiers: T, price: 800 },
    ] },
    acdb: { items: [
      { id: 'fx_acdb_1p', name: 'ACDB 1P', brand: 'Polycab', phase: '1P', tiers: T, price: 3000 },
      { id: 'fx_acdb_3p', name: 'ACDB 3P', brand: 'Polycab', phase: '3P', tiers: T, price: 5000 },
    ] },
    dcdb: { items: [
      { id: 'fx_dcdb_1p', name: 'DCDB 1P', brand: 'Polycab', phase: '1P', tiers: T, price: 2500 },
      { id: 'fx_dcdb_hyb', name: 'DCDB Universal', brand: 'Polycab', phase: 'HYB', tiers: T, price: 2600 },
    ] },
    structure_material: { items: [{ id: 'fx_str', name: 'GI Tube 60x40', tubeSize: '60x40', tiers: T, price: 95 }] },
    battery: { items: [
      { id: 'fx_bat_ok', name: 'Deye 5kWh LiFePO4', brand: 'Deye', kwh: 5, tiers: T, price: 120000 },
      { id: 'fx_bat_unknown', name: 'Unknown Battery', brand: 'Acme', tiers: T, price: 90000 },
      { id: 'fx_bat_bad', name: 'Mismatched Battery', brand: 'Acme', tiers: T, price: 70000 },
      { id: 'fx_bat_norating', name: 'SEG. 100Ah', brand: 'SEG', tiers: T, price: 60000 },
      { id: 'fx_bat_enp', name: 'Enphase IQ Battery 5P', brand: 'Enphase', kwh: 5, tiers: ['premium'], price: 300000 },
      { id: 'fx_bat_optional', name: 'Optional Protection Battery', brand: 'Deye', tiers: T, price: 100000 },
    ] },
    mccb_box: { items: [{ id: 'mb1', name: '160A MCCB', brand: 'L&T', tiers: T, price: 4500 }] },
    battery_cable: { items: [{ id: 'fx_bcable', name: '25mm Battery Cable', brand: 'Polycab', tiers: T, price: 400 }] },
    enphase: { items: [
      { id: 'en1', name: 'IQ8P Micro Inverter', brand: 'Enphase', phase: '1P', tiers: ['premium'], price: 22000 },
      { id: 'en3', name: 'Envoy S Metered', brand: 'Enphase', tiers: ['premium'], price: 30000 },
      { id: 'en7', name: 'System Controller', brand: 'Enphase', tiers: ['premium'], price: 110000 },
    ] },
    hoymiles: { items: [{ id: 'fx_micro_hoy', name: 'Hoymiles HM-800', brand: 'Hoymiles', phase: '1P', tiers: T, price: 15000 }] },
  },
  bomTemplates: {
    ongrid: { sizes: { 3: '3 kW', 5: '5 kW', '5tp': '5 kW 3P', 10: '10 kW' }, threePhase: ['5tp', '10'] },
    hybrid: { sizes: { 3: '3 kW', 5: '5 kW' }, threePhase: [] },
  },
};
const FX_MASTER = {
  fx_bat_ok: { nominalVoltage: 51.2, capacityKwh: 5, maximumDischargeCurrent: 100, integratedProtection: false, externalProtectionRequired: true, protectionRating: '125A MCCB', communicationProtocol: 'CAN', communicationRequired: true, compatibleInverters: ['fx_inv_hyb', 'fx_inv_hyb_str'], compatibleSystemTypes: ['hybrid'], compatiblePhases: ['1P', '3P'], engineeringStatus: 'APPROVED' },
  fx_bat_bad: { nominalVoltage: 300, capacityKwh: 5, maximumDischargeCurrent: 20, integratedProtection: false, externalProtectionRequired: true, protectionRating: '63A', communicationProtocol: 'MODBUS', compatibleInverters: ['other'], compatibleSystemTypes: ['ongrid'], compatiblePhases: ['3P'] },
  fx_bat_norating: { nominalVoltage: 48, capacityKwh: 4.8, maximumDischargeCurrent: 100, integratedProtection: false, externalProtectionRequired: true, protectionRating: null, communicationRequired: false, compatibleInverters: ['fx_inv_hyb'], compatibleSystemTypes: ['hybrid'], compatiblePhases: 'any' },
  fx_bat_enp: { nominalVoltage: 67, capacityKwh: 5, maximumDischargeCurrent: 50, integratedProtection: true, communicationRequired: false, compatibleSystemTypes: ['hybrid', 'ongrid'], compatiblePhases: ['1P'], engineeringStatus: 'APPROVED' },
  fx_bat_optional: { nominalVoltage: 51.2, capacityKwh: 5, maximumDischargeCurrent: 100, integratedProtection: false, externalProtectionRequired: false, communicationProtocol: 'RS485', compatibleInverters: ['fx_inv_hyb'], compatibleSystemTypes: ['hybrid'], compatiblePhases: ['1P'] },
};

const line = (role, componentId, quantity = 1) => ({ role, componentId, quantity });
const DEYE_LINES = [line('PANEL', 'fx_pnl', 10), line('INVERTER', 'fx_inv_1p'), line('AC_ISOLATOR', 'is1'), line('ACDB', 'fx_acdb_1p'), line('DCDB', 'fx_dcdb_1p'), line('STRUCTURE', 'fx_str', 30)];
const HYBRID_LINES = [line('PANEL', 'fx_pnl', 6), line('INVERTER', 'fx_inv_hyb'), line('BATTERY', 'fx_bat_ok'), line('BATTERY_PROTECTION', 'mb1'), line('BATTERY_CABLE', 'fx_bcable', 2), line('AC_ISOLATOR', 'is1'), line('ACDB', 'fx_acdb_1p'), line('DCDB', 'fx_dcdb_hyb'), line('STRUCTURE', 'fx_str', 20)];
const ENPHASE_LINES = [line('PANEL', 'fx_pnl', 6), line('MICRO_INVERTER', 'en1', 6), line('BATTERY', 'fx_bat_enp'), line('ENERGY_SYSTEM_CONTROLLER', 'en7'), line('AC_ISOLATOR', 'is1'), line('ACDB', 'fx_acdb_1p'), line('STRUCTURE', 'fx_str', 20)];
const ENPHASE_ONGRID_LINES = [line('PANEL', 'fx_pnl', 6), line('MICRO_INVERTER', 'en1', 6), line('AC_ISOLATOR', 'is1'), line('STRUCTURE', 'fx_str', 20)];

const pbom = (architecture, sysType, phase, lines, extra = {}) => projectBom.createProjectBom({
  projectId: 'PRJ-FX', packageId: 'PKG-FX', architecture, sysType, tier: 'value', sizeKw: 5, phase, lines, createdBy: 'ph-1', createdAt: AT, ...extra,
});
const swap = (lines, role, componentId, quantity) => lines.map((l) => (l.role === role ? { ...l, componentId: componentId === undefined ? l.componentId : componentId, quantity: quantity === undefined ? l.quantity : quantity } : l));
const drop = (lines, ...rolesToDrop) => lines.filter((l) => !rolesToDrop.includes(l.role));
const DEYE = (lines = DEYE_LINES, phase = '1P') => pbom('DEYE', 'ongrid', phase, lines);
const HYB = (lines = HYBRID_LINES) => pbom('DEYE', 'hybrid', '1P', lines);
const ENP = (lines = ENPHASE_LINES, sysType = 'hybrid') => pbom('ENPHASE', sysType, '1P', lines);

// [id, rule, expect ('fail' | 'pass'), arguments]
const RULE_FIXTURES = [
  ['A-001 no architecture', 'PBC-A-001', 'fail', { bom: pbom(null, 'ongrid', '1P', DEYE_LINES) }],
  ['A-001 declared', 'PBC-A-001', 'pass', { bom: DEYE() }],
  ['A-002 non-Enphase micro on Enphase', 'PBC-A-002', 'fail', { bom: ENP(swap(ENPHASE_LINES, 'MICRO_INVERTER', 'fx_micro_hoy')) }],
  ['A-002 Enphase part on Deye', 'PBC-A-002', 'fail', { bom: DEYE([...DEYE_LINES, line('MONITORING', 'en3')]) }],
  ['A-002 Deye battery on Enphase (BC-J)', 'PBC-A-002', 'fail', { bom: ENP(swap(ENPHASE_LINES, 'BATTERY', 'fx_bat_ok')) }],
  ['A-002 Enphase battery on Deye (BC-J)', 'PBC-A-002', 'fail', { bom: HYB(swap(HYBRID_LINES, 'BATTERY', 'fx_bat_enp')) }],
  ['A-002 all Enphase', 'PBC-A-002', 'pass', { bom: ENP() }],
  ['B-001 no inverter', 'PBC-B-001', 'fail', { bom: DEYE(drop(DEYE_LINES, 'INVERTER')) }],
  ['B-001 inverter present', 'PBC-B-001', 'pass', { bom: DEYE() }],
  ['B-001 Enphase has no string inverter', 'PBC-B-001', 'pass', { bom: ENP() }],
  ['B-002 panel 600 V below inverter 1000 V', 'PBC-B-002', 'fail', { bom: DEYE(swap(DEYE_LINES, 'PANEL', 'fx_pnl_lowsys')) }],
  ['B-002 panel 1500 V', 'PBC-B-002', 'pass', { bom: DEYE() }],
  ['C-001 panel data missing', 'PBC-C-001', 'fail', { bom: DEYE(swap(DEYE_LINES, 'PANEL', 'fx_pnl_nodata')) }],
  ['C-001 panel data complete', 'PBC-C-001', 'pass', { bom: DEYE() }],
  ['D-001 inverter window missing', 'PBC-D-001', 'fail', { bom: DEYE(swap(DEYE_LINES, 'INVERTER', 'fx_inv_nomax')) }],
  ['D-001 inverter window recorded', 'PBC-D-001', 'pass', { bom: DEYE() }],
  ['D-002 cold Voc 1133 V above 1000 V', 'PBC-D-002', 'fail', { bom: DEYE(swap(DEYE_LINES, 'PANEL', 'fx_pnl_hot')) }],
  ['D-002 cold Voc from text values', 'PBC-D-002', 'fail', { bom: DEYE(swap(DEYE_LINES, 'PANEL', 'fx_pnl_edge')) }],
  ['D-002 cold Voc 50.8 V', 'PBC-D-002', 'pass', { bom: DEYE() }],
  ['D-003 panel and inverter', 'PBC-D-003', 'fail', { bom: DEYE() }],
  ['D-003 Enphase (no string inverter)', 'PBC-D-003', 'pass', { bom: ENP() }],
  ['N-001 panel and inverter', 'PBC-N-001', 'fail', { bom: DEYE() }],
  ['N-001 inverter without MPPT data', 'PBC-N-001', 'fail', { bom: DEYE(swap(DEYE_LINES, 'INVERTER', 'fx_inv_nomax')) }],
  ['N-001 Enphase', 'PBC-N-001', 'pass', { bom: ENP() }],
  ['E-001 battery not listed for system type and inverter', 'PBC-E-001', 'fail', { bom: HYB(swap(HYBRID_LINES, 'BATTERY', 'fx_bat_bad')) }],
  ['E-001 compatible battery', 'PBC-E-001', 'pass', { bom: HYB() }],
  ['F-001 battery voltage outside window', 'PBC-F-001', 'fail', { bom: HYB(swap(HYBRID_LINES, 'BATTERY', 'fx_bat_bad')) }],
  ['F-001 voltage inside window', 'PBC-F-001', 'pass', { bom: HYB() }],
  ['F-002 voltage unknown', 'PBC-F-002', 'fail', { bom: HYB(swap(HYBRID_LINES, 'BATTERY', 'fx_bat_unknown')) }],
  ['F-002 voltage known', 'PBC-F-002', 'pass', { bom: HYB() }],
  ['G-001 battery current below requirement', 'PBC-G-001', 'fail', { bom: HYB(swap(HYBRID_LINES, 'BATTERY', 'fx_bat_bad')) }],
  ['G-001 current sufficient', 'PBC-G-001', 'pass', { bom: HYB() }],
  ['G-002 current unknown', 'PBC-G-002', 'fail', { bom: HYB(swap(HYBRID_LINES, 'BATTERY', 'fx_bat_unknown')) }],
  ['G-002 current known', 'PBC-G-002', 'pass', { bom: HYB() }],
  ['H-001 protection mode unknown', 'PBC-H-001', 'fail', { bom: HYB(swap(HYBRID_LINES, 'BATTERY', 'fx_bat_unknown')) }],
  ['H-001 protection mode recorded', 'PBC-H-001', 'pass', { bom: HYB() }],
  ['H-002 external protection without rating', 'PBC-H-002', 'fail', { bom: HYB(swap(HYBRID_LINES, 'BATTERY', 'fx_bat_norating')) }],
  ['H-002 rating recorded', 'PBC-H-002', 'pass', { bom: HYB() }],
  ['H-003 external protection line missing', 'PBC-H-003', 'fail', { bom: HYB(drop(HYBRID_LINES, 'BATTERY_PROTECTION')) }],
  ['H-003 protection line present', 'PBC-H-003', 'pass', { bom: HYB() }],
  ['H-003 optional protection, no line', 'PBC-H-003', 'pass', { bom: HYB(drop(swap(HYBRID_LINES, 'BATTERY', 'fx_bat_optional'), 'BATTERY_PROTECTION')) }],
  ['H-004 MCCB on integrated protection', 'PBC-H-004', 'fail', { bom: ENP([...ENPHASE_LINES, line('BATTERY_PROTECTION', 'mb1')]) }],
  ['H-004 DC battery cable on integrated protection', 'PBC-H-004', 'fail', { bom: ENP([...ENPHASE_LINES, line('BATTERY_CABLE', 'fx_bcable', 2)]) }],
  ['H-004 clean Enphase', 'PBC-H-004', 'pass', { bom: ENP() }],
  ['I-001 protocol not recorded', 'PBC-I-001', 'fail', { bom: HYB(swap(HYBRID_LINES, 'BATTERY', 'fx_bat_unknown')) }],
  ['I-001 protocol supported', 'PBC-I-001', 'pass', { bom: HYB() }],
  ['I-001 protocols as text', 'PBC-I-001', 'pass', { bom: HYB(swap(HYBRID_LINES, 'INVERTER', 'fx_inv_hyb_str')) }],
  ['J-001 3P inverter on 1P', 'PBC-J-001', 'fail', { bom: DEYE(swap(DEYE_LINES, 'INVERTER', 'fx_inv_3p')) }],
  ['J-001 matching phase', 'PBC-J-001', 'pass', { bom: DEYE() }],
  ['J-001 HYB is phase-agnostic', 'PBC-J-001', 'pass', { bom: HYB() }],
  ['K-001 isolator missing', 'PBC-K-001', 'fail', { bom: DEYE(drop(DEYE_LINES, 'AC_ISOLATOR')) }],
  ['K-001 micro-inverter missing on Enphase', 'PBC-K-001', 'fail', { bom: ENP(drop(ENPHASE_LINES, 'MICRO_INVERTER')) }],
  ['K-001 template scope exempts structure', 'PBC-K-001', 'pass', { bom: DEYE(drop(DEYE_LINES, 'STRUCTURE')), templateScope: true }],
  ['K-001 all present', 'PBC-K-001', 'pass', { bom: DEYE() }],
  ['K-002 role without component', 'PBC-K-002', 'fail', { bom: DEYE(swap(DEYE_LINES, 'DCDB', null)) }],
  ['K-002 every role selected', 'PBC-K-002', 'pass', { bom: DEYE() }],
  ['K-003 unknown component', 'PBC-K-003', 'fail', { bom: DEYE(swap(DEYE_LINES, 'ACDB', 'fx_missing')) }],
  ['K-003 every component in the catalogue', 'PBC-K-003', 'pass', { bom: DEYE() }],
  ['K-004 TEST record', 'PBC-K-004', 'fail', { bom: DEYE(swap(DEYE_LINES, 'PANEL', 'fx_pnl_test')) }],
  ['K-004 lower-case inactive status', 'PBC-K-004', 'fail', { bom: DEYE(swap(DEYE_LINES, 'PANEL', 'fx_pnl_inactive')) }],
  ['K-004 ACTIVE records', 'PBC-K-004', 'pass', { bom: DEYE() }],
  ['L-001 zero quantity', 'PBC-L-001', 'fail', { bom: DEYE(swap(DEYE_LINES, 'PANEL', undefined, 0)) }],
  ['L-001 negative and text quantities', 'PBC-L-001', 'fail', { bom: DEYE(swap(swap(DEYE_LINES, 'PANEL', undefined, -2), 'STRUCTURE', undefined, 'thirty')) }],
  ['L-001 null quantity (lines given)', 'PBC-L-001', 'fail', { bom: { architecture: 'DEYE', phase: '1P', sysType: 'ongrid' }, lines: swap(DEYE_LINES, 'ACDB', undefined, null) }],
  ['L-001 positive quantities (text number accepted)', 'PBC-L-001', 'pass', { bom: DEYE(swap(DEYE_LINES, 'STRUCTURE', undefined, ' 30 ')) }],
  ['M-001 micro count differs from panels', 'PBC-M-001', 'fail', { bom: ENP(swap(ENPHASE_LINES, 'MICRO_INVERTER', undefined, 5)) }],
  ['M-001 one micro per panel', 'PBC-M-001', 'pass', { bom: ENP() }],
  ['M-002 two Enphase batteries', 'PBC-M-002', 'fail', { bom: ENP(swap(ENPHASE_LINES, 'BATTERY', undefined, 2)) }],
  ['M-002 one battery', 'PBC-M-002', 'pass', { bom: ENP() }],
  ['M-003 hybrid Enphase without controller', 'PBC-M-003', 'fail', { bom: ENP(drop(ENPHASE_LINES, 'ENERGY_SYSTEM_CONTROLLER')) }],
  ['M-003 undeclared system type defaults to hybrid', 'PBC-M-003', 'fail', { bom: pbom('ENPHASE', null, '1P', drop(ENPHASE_LINES, 'ENERGY_SYSTEM_CONTROLLER')) }],
  ['M-003 on-grid Enphase needs no controller', 'PBC-M-003', 'pass', { bom: ENP(ENPHASE_ONGRID_LINES, 'ongrid') }],
  ['M-003 controller present', 'PBC-M-003', 'pass', { bom: ENP() }],
  ['O-001 3P ACDB on 1P', 'PBC-O-001', 'fail', { bom: DEYE(swap(DEYE_LINES, 'ACDB', 'fx_acdb_3p')) }],
  ['O-001 matching boards', 'PBC-O-001', 'pass', { bom: DEYE() }],
  ['O-001 HYB DCDB', 'PBC-O-001', 'pass', { bom: DEYE(swap(DEYE_LINES, 'DCDB', 'fx_dcdb_hyb')) }],
  ['P-001 other isolator on 1P', 'PBC-P-001', 'fail', { bom: DEYE(swap(DEYE_LINES, 'AC_ISOLATOR', 'fx_iso')) }],
  ['P-001 is1 on a 3P system', 'PBC-P-001', 'fail', { bom: DEYE(swap(swap(swap(DEYE_LINES, 'INVERTER', 'fx_inv_3p'), 'ACDB', 'fx_acdb_3p'), 'DCDB', 'fx_dcdb_hyb'), '3P') }],
  ['P-001 is2 on a 3P system', 'PBC-P-001', 'pass', { bom: DEYE(swap(swap(swap(swap(DEYE_LINES, 'INVERTER', 'fx_inv_3p'), 'ACDB', 'fx_acdb_3p'), 'DCDB', 'fx_dcdb_hyb'), 'AC_ISOLATOR', 'is2'), '3P') }],
  ['P-001 is1 on 1P', 'PBC-P-001', 'pass', { bom: DEYE() }],
  ['P-002 DC isolator role', 'PBC-P-002', 'fail', { bom: { architecture: 'DEYE', phase: '1P', sysType: 'ongrid' }, lines: [...DEYE_LINES, line('DC_ISOLATOR', 'is1')] }],
  ['P-002 no DC isolator', 'PBC-P-002', 'pass', { bom: DEYE() }],
  ['Q-001 structure missing at project scope', 'PBC-Q-001', 'fail', { bom: DEYE(drop(DEYE_LINES, 'STRUCTURE')) }],
  ['Q-001 template scope', 'PBC-Q-001', 'pass', { bom: DEYE(drop(DEYE_LINES, 'STRUCTURE')), templateScope: true }],
  ['Q-001 structure present', 'PBC-Q-001', 'pass', { bom: DEYE() }],
  ['R-001 unapproved 3→7', 'PBC-R-001', 'fail', { bom: DEYE(), upgrade: { fromKw: 3, toKw: 7 } }],
  ['R-001 unparsable path', 'PBC-R-001', 'fail', { bom: DEYE(), upgrade: { fromKw: 'three', toKw: 5 } }],
  ['R-001 approved 5→8', 'PBC-R-001', 'pass', { bom: DEYE(), upgrade: { fromKw: 5, toKw: 8 } }],
  ['R-002 auto-scaled 3→6', 'PBC-R-002', 'fail', { bom: DEYE(), upgrade: { fromKw: '3', toKw: '6' } }],
  ['R-002 explicit 3→5', 'PBC-R-002', 'pass', { bom: DEYE(), upgrade: { fromKw: 3, toKw: 5 } }],
  ['S-001 future-ready', 'PBC-S-001', 'fail', { bom: DEYE(), futureUpgrade: true }],
  ['S-001 not future-ready', 'PBC-S-001', 'pass', { bom: DEYE(), futureUpgrade: false }],
  ['edge: no BOM at all', null, null, { bom: null }],
  ['edge: removed role and override', null, null, { bom: projectBom.setComponent(projectBom.removeRole(DEYE(), { role: 'DCDB', selectedBy: 'ph-1', at: AT }), { role: 'INVERTER', componentId: 'fx_inv_nomax', selectedBy: 'ph-1', at: AT }) }],
];

const runChecker = (args, cat = FX_CATALOG, master = FX_MASTER) => checker.checkProjectBom({
  bom: args.bom, lines: args.lines ?? null, templateScope: args.templateScope ?? false, catalog: cat, batteryMaster: master,
  at: AT, catalogVersion: 'fx@1', upgrade: args.upgrade ?? null, futureUpgrade: args.futureUpgrade ?? false,
});
const pbcCases = RULE_FIXTURES.map(([id, rule, expect, args]) => {
  const out = runChecker(args);
  const fired = out.findings.some((f) => f.ruleId === rule);
  if (rule && (expect === 'fail') !== fired) throw new Error(`fixture ${id}: expected ${expect} for ${rule}`);
  return { id, rule, expect, input: plain({ ...args, at: AT, catalogVersion: 'fx@1' }), output: plain(out) };
});
const coveredFail = new Set(pbcCases.filter((c) => c.expect === 'fail').map((c) => c.rule));
const coveredPass = new Set(pbcCases.filter((c) => c.expect === 'pass').map((c) => c.rule));
for (const r of checker.CHECKER_RULES) if (!coveredFail.has(r.id) || !coveredPass.has(r.id)) throw new Error(`rule ${r.id} lacks a pass or a fail fixture`);

// ---- every approved pack of data/pack-config.json, checked at template scope (packageApproval.runPackageChecker) ----
function registryLines(components) {
  return components
    .map((c) => ({ role: (c.componentId && roles.ENPHASE_COMPONENT_ROLES[c.componentId]) || roles.CATEGORY_TO_ROLE[c.role] || roles.ROLES.OTHER, componentId: c.componentId, quantity: c.quantity }))
    .sort((a, b) => String(a.role).localeCompare(String(b.role)));
}
const packCases = [];
const approvedConfig = packConfig.approved.config;
for (const [systemType, t] of Object.entries(approvedConfig.bomTemplates)) {
  if (systemType === 'upgrade') continue;
  const pairs = approvedConfig.futureUpgrade?.pairs || [];
  for (const size of Object.keys(t.sizes || {})) {
    const phase = (t.threePhase || []).includes(size) ? '3P' : '1P';
    const variants = [{ variant: 'STANDARD', systemSize: size }];
    for (const p of pairs) {
      if ((p.systemType || systemType) !== systemType || String(p.panelSize) !== String(size)) continue;
      if (!t.sizes[String(p.systemSize)]) continue;
      if (((t.threePhase || []).includes(String(p.systemSize)) ? '3P' : '1P') !== phase) continue;
      variants.push({ variant: 'FUTURE_READY', systemSize: String(p.systemSize) });
    }
    for (const tier of t.tiers || T) {
      for (const v of variants) {
        const built = bomBuilder.buildBom({ systemType, size, tier, phase, configSource: 'approved', futureSystemSize: v.variant === 'FUTURE_READY' ? v.systemSize : null, actorRole: 'PROJECT_HEAD' });
        const components = (built.lines || []).filter((l) => l.componentId && l.category !== 'fixed' && !l.isStructure).map((l) => ({ role: l.category, componentId: l.componentId, quantity: l.qty }));
        const architecture = packageApproval.resolvePackageArchitecture({ architecture: null, profileKey: built.systemConfig?.profileKey }, fullCatalog).architecture;
        const bom = { phase, architecture, sysType: systemType, lines: [] };
        const lines = registryLines(components);
        const args = { bom, lines, templateScope: true, catalog, batteryMaster: BATTERY_MASTER, at: AT, catalogVersion: CATALOG_VERSION };
        const out = checker.checkProjectBom(args);
        // the stripped catalogue must not change the verdict, and the real registry wiring must agree
        const full = checker.checkProjectBom({ ...args, catalog: fullCatalog });
        if (JSON.stringify(full) !== JSON.stringify(out)) throw new Error(`stripped catalogue changes ${systemType}/${size}/${tier}`);
        const registry = { packages: [{ packageId: 'PKG-G', systemType, size, phase, tier, profileKey: built.systemConfig?.profileKey, architecture: null, components, revisionNumber: 1 }] };
        const viaRegistry = packageApproval.runPackageChecker(registry, { catalog, batteryMaster: BATTERY_MASTER, catalogVersion: CATALOG_VERSION }, { actor: { userId: 'PACK_PUBLISH', role: 'PROJECT_HEAD' }, packageId: 'PKG-G', at: AT });
        if (JSON.stringify({ ...viaRegistry, revisionNumber: undefined, architecture: undefined, architectureSource: undefined }) !== JSON.stringify(out)) throw new Error('registry wiring differs');
        packCases.push({ id: `${systemType}|${size}|${phase}|${tier}|${v.variant}${v.variant === 'FUTURE_READY' ? v.systemSize : ''}`, input: { bom, lines, templateScope: true, at: AT, catalogVersion: CATALOG_VERSION, catalogRef: 'approved' }, output: plain(out) });
      }
    }
  }
}

// ---- battery compatibility, protection, master projection ---------------------------------------------------------
const compatCases = [];
const inverterOf = (id) => FX_CATALOG.categories.inverter.items.find((i) => i.id === id) ?? null;
for (const batId of ['fx_bat_ok', 'fx_bat_unknown', 'fx_bat_bad', 'fx_bat_norating', 'fx_bat_enp', 'fx_bat_optional']) {
  const item = FX_CATALOG.categories.battery.items.find((i) => i.id === batId);
  const master = batteryMaster.toBatteryMaster(item, FX_MASTER[batId] || {});
  compatCases.push({ id: `master ${batId}`, fn: 'toBatteryMaster', input: { item, overlay: FX_MASTER[batId] || {} }, output: plain(master) });
  compatCases.push({ id: `protection ${batId}`, fn: 'resolveProtectionRequirement', input: { battery: master }, output: plain(battery.resolveProtectionRequirement(master)) });
  for (const [ctxId, ctx] of Object.entries({
    hybrid: { inverter: inverterOf('fx_inv_hyb'), sysType: 'hybrid', phase: '1P', quantity: 1, architecture: 'DEYE' },
    textProtocols: { inverter: inverterOf('fx_inv_hyb_str'), sysType: 'hybrid', phase: '3P', quantity: 2, architecture: 'DEYE' },
    noInverter: { sysType: 'ongrid', quantity: 0, architecture: 'ENPHASE' },
    defaults: {},
  })) compatCases.push({ id: `compat ${batId} ${ctxId}`, fn: 'checkBatteryCompatibility', input: { battery: master, context: ctx }, output: plain(battery.checkBatteryCompatibility(master, ctx)) });
}
compatCases.push({ id: 'compat no battery', fn: 'checkBatteryCompatibility', input: { battery: null, context: {} }, output: plain(battery.checkBatteryCompatibility(null, {})) });
compatCases.push({ id: 'protection no battery', fn: 'resolveProtectionRequirement', input: { battery: null }, output: plain(battery.resolveProtectionRequirement(null)) });
compatCases.push({ id: 'master no item', fn: 'toBatteryMaster', input: { item: null, overlay: {} }, output: plain(batteryMaster.toBatteryMaster(null, {})) });
for (const [id, b] of Object.entries(BATTERY_MASTER)) {
  const item = catalog.categories.battery.items.find((i) => i.id === id) || { id };
  const master = batteryMaster.toBatteryMaster(item, b);
  compatCases.push({ id: `master real ${id}`, fn: 'toBatteryMaster', input: { item, overlay: b }, output: plain(master) });
  compatCases.push({ id: `compat real ${id}`, fn: 'checkBatteryCompatibility', input: { battery: master, context: { sysType: 'hybrid', phase: '1P', quantity: 1, architecture: id === 'bt2' ? 'ENPHASE' : 'DEYE' } }, output: plain(battery.checkBatteryCompatibility(master, { sysType: 'hybrid', phase: '1P', quantity: 1, architecture: id === 'bt2' ? 'ENPHASE' : 'DEYE' })) });
}

// ---- upgrade identity and section policy --------------------------------------------------------------------------
const upgradeCases = [];
for (const [fromKw, toKw] of [[3, 5], [3, 6], ['5', '8'], [8, 10], [3, 7], [2.5, 3.75], ['x', 5]]) {
  const input = { fromKw, toKw, tier: 'value', sections: fromKw === 3 ? { panels: true, structure: true, inverter: true, wiring: true } : null };
  upgradeCases.push({ id: `identity ${fromKw}→${toKw}`, fn: 'buildUpgradeIdentity', input, output: plain(upgradeModel.buildUpgradeIdentity(input)) });
  // the key of the identity as stored (JSON: an unparsable size is null, not NaN)
  const identityOut = plain(upgradeModel.buildUpgradeIdentity(input));
  upgradeCases.push({ id: `key ${fromKw}→${toKw}`, fn: 'upgradeIdentityKey', input: { identity: identityOut }, output: upgradeModel.upgradeIdentityKey(identityOut) });
}
for (const sections of [null, {}, { panels: true, structure: true, inverter: true, wiring: true }, { panels: true, wiring: 1 }, { panels: 'yes', structure: true, inverter: true, wiring: 0 }]) {
  upgradeCases.push({ id: `policy ${JSON.stringify(sections)}`, fn: 'checkUpgradeSectionPolicy', input: { sections }, output: plain(upgradeModel.checkUpgradeSectionPolicy(sections)) });
}

// ---- bomLock ------------------------------------------------------------------------------------------------------
const PRICES = { fx_pnl: { purchaseCost: 12500, sellingPrice: 13585 }, fx_inv_1p: { purchaseCost: 36000 }, en1: { sellingPrice: 22000 } };
const lockArgs = (bom, extra = {}) => ({ bom, catalog: FX_CATALOG, batteryMaster: FX_MASTER, catalogVersion: 'fx@1', lockedBy: 'ph-1', lockedAt: AT, priceLookup: PRICES, ...extra });
const ACKS = [{ ruleId: 'PBC-D-003', by: 'ph-1', at: AT, note: 'string layout checked on site' }, { ruleId: 'PBC-N-001', by: 'ph-1', at: AT, note: 'MPPT loading reviewed' }];
const lockedBom = bomLock.attemptLock(lockArgs(ENP(ENPHASE_ONGRID_LINES, 'ongrid'))).bom;
const lockCases = [
  ['blocked', lockArgs(DEYE(drop(DEYE_LINES, 'AC_ISOLATOR')))],
  ['unacknowledged warnings', lockArgs(DEYE())],
  ['one acknowledgement missing', lockArgs(DEYE(), { acknowledgements: ACKS.slice(0, 1) })],
  ['acknowledged warnings', lockArgs(DEYE(), { acknowledgements: ACKS })],
  ['valid (Enphase on-grid)', lockArgs(ENP(ENPHASE_ONGRID_LINES, 'ongrid'))],
  ['auto-scaled upgrade needs acknowledgement', lockArgs(ENP(ENPHASE_ONGRID_LINES, 'ongrid'), { upgrade: { fromKw: 3, toKw: 8 } })],
  ['warnings not requiring acknowledgement', lockArgs(ENP(ENPHASE_ONGRID_LINES, 'ongrid'), { futureUpgrade: true })],
  ['already locked', lockArgs(lockedBom)],
  ['overridden line keeps its provenance', lockArgs(projectBom.setComponent(ENP(ENPHASE_ONGRID_LINES, 'ongrid'), { role: 'STRUCTURE', componentId: 'fx_str', quantity: 24, selectedBy: 'ph-2', at: AT, reason: 'roof survey' }))],
].map(([id, args]) => ({ id, input: plain(args), ...captured(() => bomLock.attemptLock(args)) }));
lockCases.push({ id: 'no bom', input: { bom: null }, ...captured(() => bomLock.attemptLock({ bom: null })) });
const summaryCases = [null, runChecker({ bom: DEYE(drop(DEYE_LINES, 'AC_ISOLATOR')) }), runChecker({ bom: DEYE() }), runChecker({ bom: ENP(ENPHASE_ONGRID_LINES, 'ongrid') })]
  .map((v, i) => ({ id: `summary ${i}`, input: { validation: plain(v) }, output: plain(bomLock.lockSummary(v)) }));

// ---- engineeringValidation (ENG-*) ----------------------------------------------------------------------------------
const app = (name, qty, category, extra = {}) => ({ name, qty, category, ...extra });
const ONGRID_APP = [app('Fixture Panel 540', 10, 'panel', { itemId: 'fx_pnl', panelType: 'DCR' }), app('Deye 5kW 1P', 1, 'inverter', { itemId: 'fx_inv_1p', raw: { phase: '1P' }, type: 'ongrid' }), app('DCDB 1P', 1, 'dcdb', { itemId: 'fx_dcdb_1p' }), app('ACDB 1P', 1, 'acdb', { itemId: 'fx_acdb_1p' }), app('AC Isolator 1P', 1, 'isolator', { itemId: 'is1' })];
const HYBRID_APP = [app('Fixture Panel 540', 6, 'panel', { itemId: 'fx_pnl' }), app('Deye Hybrid 5kW', 1, 'inverter', { itemId: 'fx_inv_hyb', raw: { phase: 'HYB' }, type: 'hybrid' }), app('SEG. 100Ah', 1, 'battery', { itemId: 'fx_bat_norating' }), app('160A MCCB', 1, 'mccb_box', { itemId: 'mb1' }), app('25mm Battery Cable', 2, 'battery_cable', { itemId: 'fx_bcable' })];
const PREMIUM_APP = [app('Fixture Panel 540', 6, 'panel', { itemId: 'fx_pnl' }), app('IQ8P Micro Inverter', 6, 'enphase', { itemId: 'en1' }), app('Enphase Flex Battery', 1, 'battery', { itemId: 'fx_bat_enp' }), app('System Controller', 1, 'enphase', { itemId: 'en7' })];
const PROFILE_1 = { batteryIncluded: true, batteryQuantity: 1 };
const PROFILE_0 = { batteryIncluded: false, batteryQuantity: 0 };
const V = (lines, extra = {}) => ({ bom: { bomLines: lines }, catalog: FX_CATALOG, sysType: 'ongrid', size: '5', tier: 'value', phase: '1P', ...extra });
const ENG_FIXTURES = [
  ['BAT-001 declared battery missing', 'ENG-BAT-001', 'fail', V(drop(HYBRID_APP), { sysType: 'hybrid', profile: PROFILE_1, bom: { bomLines: HYBRID_APP.filter((l) => l.category !== 'battery') } })],
  ['BAT-001 battery present', 'ENG-BAT-001', 'pass', V(HYBRID_APP, { sysType: 'hybrid', profile: PROFILE_1 })],
  ['BAT-002 quantity differs', 'ENG-BAT-002', 'fail', V(HYBRID_APP.map((l) => (l.category === 'battery' ? { ...l, qty: '2 nos' } : l)), { sysType: 'hybrid', profile: PROFILE_1 })],
  ['BAT-002 quantity matches', 'ENG-BAT-002', 'pass', V(HYBRID_APP, { sysType: 'hybrid', profile: PROFILE_1 })],
  ['BAT-003 MCCB missing', 'ENG-BAT-003', 'fail', V(HYBRID_APP.filter((l) => l.category !== 'mccb_box'), { sysType: 'hybrid', profile: PROFILE_1 })],
  ['BAT-003 MCCB present', 'ENG-BAT-003', 'pass', V(HYBRID_APP, { sysType: 'hybrid', profile: PROFILE_1 })],
  ['BAT-004 accessories without battery', 'ENG-BAT-004', 'fail', V([...ONGRID_APP, app('25mm Battery Cable', 2, 'battery_cable'), app('40A Change Over Switch', 1, 'change_over')], { profile: PROFILE_0 })],
  ['BAT-004 no accessories', 'ENG-BAT-004', 'pass', V(ONGRID_APP, { profile: PROFILE_0 })],
  ['BAT-005 battery not declared', 'ENG-BAT-005', 'fail', V(HYBRID_APP, { sysType: 'hybrid', profile: PROFILE_0 })],
  ['BAT-005 none declared, none present', 'ENG-BAT-005', 'pass', V(ONGRID_APP, { profile: PROFILE_0 })],
  ['BAT-006 profile inconsistent', 'ENG-BAT-006', 'fail', V(ONGRID_APP, { profile: { batteryIncluded: true, batteryQuantity: 0 } })],
  ['BAT-006 battery cable missing', 'ENG-BAT-006', 'fail', V(HYBRID_APP.filter((l) => l.category !== 'battery_cable'), { sysType: 'hybrid', profile: PROFILE_1 })],
  ['BAT-006 consistent profile', 'ENG-BAT-006', 'pass', V(HYBRID_APP, { sysType: 'hybrid', profile: PROFILE_1 })],
  ['BAT-007 protection mode unconfirmed', 'ENG-BAT-007', 'fail', V(HYBRID_APP, { sysType: 'hybrid', profile: PROFILE_1 })],
  ['BAT-007 protection mode confirmed', 'ENG-BAT-007', 'pass', V(HYBRID_APP, { sysType: 'hybrid', profile: PROFILE_1, batteryProtectionMode: 'EXTERNAL_REQUIRED' })],
  ['BAT-008 generic parts on integrated topology', 'ENG-BAT-008', 'fail', V(HYBRID_APP, { sysType: 'hybrid', profile: PROFILE_1, batteryProtectionMode: 'INTEGRATED' })],
  ['BAT-008 premium hybrid with mb1 and 25 mm cable', 'ENG-BAT-008', 'fail', V([...PREMIUM_APP, app('160A MCCB', 1, 'mccb_box', { itemId: 'mb1' }), app('25mm Battery Cable', 2, 'battery_cable')], { sysType: 'hybrid', tier: 'premium', profile: PROFILE_1, batteryProtectionMode: 'INTEGRATED' })],
  ['BAT-008 clean premium', 'ENG-BAT-008', 'pass', V(PREMIUM_APP, { sysType: 'hybrid', tier: 'premium', profile: PROFILE_1, batteryProtectionMode: 'INTEGRATED' })],
  ['BAT-009 battery not approved', 'ENG-BAT-009', 'fail', V(HYBRID_APP, { sysType: 'hybrid', battery: { required: true, engineeringApproved: false, approvalStatus: 'PENDING_ENGINEERING_APPROVAL', componentId: 'fx_bat_norating', component: { name: 'SEG. 100Ah' }, missingForSafeIssue: [] } })],
  ['BAT-009 approved battery', 'ENG-BAT-009', 'pass', V(HYBRID_APP, { sysType: 'hybrid', battery: { required: true, engineeringApproved: true, componentId: 'fx_bat_ok', missingForSafeIssue: [] } })],
  ['BAT-010 safe-issue data missing', 'ENG-BAT-010', 'fail', V(HYBRID_APP, { sysType: 'hybrid', battery: { required: true, componentId: 'fx_bat_norating', missingForSafeIssue: ['integratedProtection', 'protectionRating'], protection: { mode: 'EXTERNAL_REQUIRED', rating: null } } })],
  ['BAT-010 data complete', 'ENG-BAT-010', 'pass', V(HYBRID_APP, { sysType: 'hybrid', battery: { required: true, componentId: 'fx_bat_ok', missingForSafeIssue: [], protection: { mode: 'EXTERNAL_REQUIRED', rating: '125A' } } })],
  ['SYS-001 size not offered', 'ENG-SYS-001', 'fail', V(ONGRID_APP, { size: 4 })],
  ['SYS-001 offered size', 'ENG-SYS-001', 'pass', V(ONGRID_APP, { size: 5 })],
  ['SYS-002 3P for a 1P size', 'ENG-SYS-002', 'fail', V(ONGRID_APP, { size: '3', phase: '3P' })],
  ['SYS-002 3P size', 'ENG-SYS-002', 'pass', V(ONGRID_APP, { size: '5tp', phase: '3P' })],
  ['SYS-003 N/A lines', 'ENG-SYS-003', 'fail', V([...ONGRID_APP, app('N/A', 0, 'meter', { pos: 7, label: 'Net meter' }), app('', '3 m', 'dc_cable', { pos: 8 }), app('Earth rod', 2, 'cb_rod', { isNA: true })])],
  ['SYS-003 every line resolved', 'ENG-SYS-003', 'pass', V(ONGRID_APP)],
  ['CMP-001 TEST and unknown components', 'ENG-CMP-001', 'fail', V([...ONGRID_APP, app('Test Panel', 1, 'panel', { itemId: 'fx_pnl_test' }), app('Ghost part', 1, 'meter', { catalogItemId: 'fx_ghost' })])],
  ['CMP-001 ACTIVE components', 'ENG-CMP-001', 'pass', V(ONGRID_APP)],
  ['CMP-002 inverter phase differs', 'ENG-CMP-002', 'fail', V(ONGRID_APP.map((l) => (l.category === 'inverter' ? { ...l, raw: { phase: '3P' } } : l)))],
  ['CMP-002 inverter phase matches', 'ENG-CMP-002', 'pass', V(ONGRID_APP)],
  ['CMP-003 string inverter on hybrid', 'ENG-CMP-003', 'fail', V(ONGRID_APP, { sysType: 'hybrid', size: '5' })],
  ['CMP-003 hybrid inverter', 'ENG-CMP-003', 'pass', V(HYBRID_APP, { sysType: 'hybrid' })],
  ['CMP-004 two inverters', 'ENG-CMP-004', 'fail', V([...ONGRID_APP, app('Deye 5kW 1P', 1, 'inverter', { itemId: 'fx_inv_1p' }), app('ACDB 1P', 1, 'acdb', { itemId: 'fx_acdb_1p' })])],
  ['CMP-004 one per role', 'ENG-CMP-004', 'pass', V(ONGRID_APP)],
  ['CMP-005 fallback selection', 'ENG-CMP-005', 'fail', V(ONGRID_APP.map((l) => (l.category === 'dcdb' ? { ...l, autoSelected: true, resolution: 'NEAREST_KW' } : l)))],
  ['CMP-005 approved selections', 'ENG-CMP-005', 'pass', V(ONGRID_APP)],
  ['PNL-001 non-DCR with subsidy', 'ENG-PNL-001', 'fail', V(ONGRID_APP.map((l) => (l.category === 'panel' ? { ...l, panelType: 'NON_DCR' } : l)), { subsidyType: 'residential' })],
  ['PNL-001 DCR by flag with GHS', 'ENG-PNL-001', 'pass', V(ONGRID_APP.map((l) => (l.category === 'panel' ? { ...l, panelType: undefined, dcr: true } : l)), { subsidyType: 'ghs' })],
  ['PNL-001 no subsidy', 'ENG-PNL-001', 'pass', V(ONGRID_APP.map((l) => (l.category === 'panel' ? { ...l, panelType: 'NON_DCR' } : l)), { subsidyType: 'none' })],
  ['ENP-001 premium hybrid without controller', 'ENG-ENP-001', 'fail', V(PREMIUM_APP.filter((l) => l.itemId !== 'en7'), { sysType: 'hybrid', tier: 'premium' })],
  ['ENP-001 controller present', 'ENG-ENP-001', 'pass', V(PREMIUM_APP, { sysType: 'hybrid', tier: 'premium' })],
  ['ENP-002 IQ8P count differs', 'ENG-ENP-002', 'fail', V(PREMIUM_APP.map((l) => (l.itemId === 'en1' ? { ...l, qty: '5' } : l)), { tier: 'premium' })],
  ['ENP-002 IQ8P per panel', 'ENG-ENP-002', 'pass', V(PREMIUM_APP, { tier: 'premium' })],
  ['ENP-003 pending Enphase parts', 'ENG-ENP-003', 'fail', V([...PREMIUM_APP, app('Envoy S Metered', 1, 'enphase', { itemId: 'en3' })], { tier: 'premium' })],
  ['ENP-003 no pending parts', 'ENG-ENP-003', 'pass', V(PREMIUM_APP, { tier: 'premium' })],
  ['ENP-004 premium hybrid', 'ENG-ENP-004', 'fail', V(PREMIUM_APP, { sysType: 'hybrid', tier: 'premium' })],
  ['ENP-004 premium on-grid', 'ENG-ENP-004', 'pass', V(PREMIUM_APP, { tier: 'premium' })],
  ['UPG-001 unapproved path', 'ENG-UPG-001', 'fail', V(ONGRID_APP, { upgrade: { fromKw: '3', toKw: '7' } })],
  ['UPG-001 approved path', 'ENG-UPG-001', 'pass', V(ONGRID_APP, { upgrade: { fromKw: 5, toKw: 10 } })],
  ['UPG-002 auto-scaled path', 'ENG-UPG-002', 'fail', V(ONGRID_APP, { upgrade: { fromKw: 3, toKw: 8 } })],
  ['UPG-002 explicit path', 'ENG-UPG-002', 'pass', V(ONGRID_APP, { upgrade: { fromKw: 3, toKw: 5 } })],
  ['UPG-003 mandatory sections missing', 'ENG-UPG-003', 'fail', V(ONGRID_APP, { upgrade: { fromKw: 3, toKw: 5, sections: { panels: true, wiring: true } } })],
  ['UPG-003 sections complete', 'ENG-UPG-003', 'pass', V(ONGRID_APP, { upgrade: { fromKw: 3, toKw: 5, sections: { panels: 1, structure: true, inverter: 'yes', wiring: true } } })],
  ['declared only: PNL-002, STR-001, DEV-001, SYS-004 are never emitted', 'ENG-PNL-002', 'pass', V(ONGRID_APP)],
  ['edge: nothing supplied', null, null, {}],
  ['edge: lines key instead of bomLines, unknown template', null, null, { bom: { lines: ONGRID_APP }, catalog: FX_CATALOG, sysType: 'upgrade', size: '3', profile: PROFILE_0 }],
];
const engCases = ENG_FIXTURES.map(([id, rule, expect, input]) => {
  const out = validation.validateEngineering(input);
  const fired = out.findings.some((f) => f.ruleId === rule);
  if (rule && (expect === 'fail') !== fired) throw new Error(`fixture ${id}: expected ${expect} for ${rule}`);
  return { id, rule, expect, input: plain(input), output: plain(out) };
});
const DECLARED_ONLY = ['ENG-PNL-002', 'ENG-STR-001', 'ENG-DEV-001', 'ENG-SYS-004'];
for (const r of validation.RULES) {
  if (DECLARED_ONLY.includes(r.id)) continue;
  if (!engCases.some((c) => c.rule === r.id && c.expect === 'fail') || !engCases.some((c) => c.rule === r.id && c.expect === 'pass')) throw new Error(`rule ${r.id} lacks a pass or a fail fixture`);
}
const consistencyCases = [
  ['declared, present', { bomLines: HYBRID_APP }, PROFILE_1, {}],
  ['declared, missing', { bomLines: ONGRID_APP }, PROFILE_1, {}],
  ['declared, integrated', { bomLines: HYBRID_APP }, PROFILE_1, { protectionMode: 'INTEGRATED' }],
  ['declared, MCCB not required', { bomLines: HYBRID_APP.filter((l) => l.category !== 'mccb_box') }, PROFILE_1, { requireMccbWhenBattery: false }],
  ['none declared, orphans allowed', { lines: [...ONGRID_APP, app('25mm Battery Cable', 2, 'battery_cable')] }, PROFILE_0, { allowAccessoriesWithoutBattery: true }],
  ['profile text quantity', { bomLines: HYBRID_APP }, { batteryQuantity: '1', batteryIncluded: 'yes' }, {}],
  ['no bom, no profile', null, null, {}],
].map(([id, bom, profile, opts]) => ({ id, input: { bom, profile, opts }, output: plain(validateBom.validateBatteryConsistency(bom, profile, opts)) }));

write('rules_checker.json', {
  rulesVersion: checker.RULES_VERSION,
  checkerRuleCount: checker.CHECKER_RULES.length,
  checkerRules: plain(checker.CHECKER_RULES),
  warningsRequiringAcknowledgement: bomLock.WARNINGS_REQUIRING_ACKNOWLEDGEMENT,
  validationRuleCount: validation.RULES.length,
  validationRules: plain(validation.RULES),
  declaredOnlyValidationRules: DECLARED_ONLY,
  approvedUpgradePaths: plain(upgradeModel.APPROVED_UPGRADE_PATHS),
  packConfigVersion: packConfig.approved.version,
  fixtureNote: 'refs.fixtureCatalog / refs.fixtureBatteryMaster are the catalogue every pbc, lock, battery and validation case runs against; packs run against rules_catalog.json.',
}, {
  pbc: pbcCases, packs: packCases, battery: compatCases, upgrade: upgradeCases, lock: lockCases, lockSummary: summaryCases, validation: engCases, consistency: consistencyCases,
}, { fixtureCatalog: FX_CATALOG, fixtureBatteryMaster: FX_MASTER });

// =================================================================================================================
// BOM domain: roles, identity, lifecycle, project BOM, snapshot, status
// =================================================================================================================
const roleCases = [
  null, {}, { itemId: 'en1', category: 'enphase' }, { catalogItemId: 'en_sysctrl' }, { itemId: 'en_flexbat', category: 'battery' }, { category: 'ug_cable' },
  { category: 'structure_material' }, { category: 'unknown', isStructure: true }, { name: '  25mm Battery Cable ' }, { name: '40A Change Over Switch' },
  { name: '40a change over switch' }, { itemId: 'p2', category: 'panel', name: 'Solar Panel' }, { itemId: 'zz', category: 'meter_box' },
].map((l, i) => ({ id: `role ${i}`, input: { line: l }, output: roles.roleForLine(l) }));

const identityCases = [];
for (const [key, cat] of Object.entries(catalog.categories)) {
  for (const item of cat.items.slice(0, 3)) identityCases.push({ id: `toComponent ${key}/${item.id}`, fn: 'toComponent', input: { categoryKey: key, item }, output: plain(identity.toComponent(key, item)) });
}
identityCases.push({ id: 'toComponent null', fn: 'toComponent', input: { categoryKey: 'panel', item: null }, output: plain(identity.toComponent('panel', null)) });
identityCases.push({ id: 'toComponent cable default unit', fn: 'toComponent', input: { categoryKey: 'armoured_cable', item: { id: 'c1', name: 'x', sqmm: 4, status: 'TEST' } }, output: plain(identity.toComponent('armoured_cable', { id: 'c1', name: 'x', sqmm: 4, status: 'TEST' })) });
for (const id of ['p2', 'i20', 'en7', 'bt2', 'is1', 'nope', '', null]) identityCases.push({ id: `find ${id}`, fn: 'findByComponentId', input: { componentId: id, catalogRef: 'approved' }, output: plain(identity.findByComponentId(catalog, id)) });
for (const id of ['p2', 'en7', 'bt3']) {
  const c = identity.findByComponentId(catalog, id);
  identityCases.push({ id: `selectable ${id}`, fn: 'isSelectable', input: { component: plain(c) }, output: identity.isSelectable(c) });
}
identityCases.push({ id: 'selectable null', fn: 'isSelectable', input: { component: null }, output: identity.isSelectable(null) });
identityCases.push({ id: 'selectable no tiers', fn: 'isSelectable', input: { component: { status: 'ACTIVE', tiers: [] } }, output: identity.isSelectable({ status: 'ACTIVE', tiers: [] }) });
identityCases.push({ id: 'duplicates', fn: 'findDuplicateIdentities', input: { catalogRef: 'approved' }, output: plain(identity.findDuplicateIdentities(catalog)) });
identityCases.push({ id: 'allComponents count', fn: 'allComponents.length', input: { catalogRef: 'approved' }, output: identity.allComponents(catalog).length });

const lifecycleCases = [];
const LC_ITEMS = [
  ['panel', { name: 'New Solar Panel', price: 0, brand: 'x' }], ['panel', { name: 'test panel', price: 5000, brand: 'x' }], ['panel', { name: 'hari 550', price: 12000, brand: 'Hari' }],
  ['panel', { name: 'Adani 600', price: 0, brand: 'Adani', tiers: [] }], ['panel', { name: 'Adani 600W', price: 12000 }], ['ss_screw', { name: 'SS Screw', price: 0 }],
  ['ss_screw', { name: 'screw 12', price: 3 }], ['inverter', { name: 'Deye', brand: 'Deye', price: 1, status: 'INACTIVE' }], ['inverter', { name: 'Deye', brand: 'Deye', status: 'RETIRED', price: 2 }],
  ['panel', { name: '  Demo module', brand: 'x', price: 1 }], ['panel', { name: 'Newton 540', brand: 'Newton', price: 1 }], [undefined, undefined],
];
LC_ITEMS.forEach(([key, item], i) => lifecycleCases.push({ id: `classify ${i}`, fn: 'classifyCatalogItem', input: { categoryKey: key, item, context: {} }, output: plain(lifecycle.classifyCatalogItem(key, item)) }));
lifecycleCases.push({ id: 'classify duplicate', fn: 'classifyCatalogItem', input: { categoryKey: 'panel', item: { name: 'Waaree 540', brand: 'Waaree', price: 1 }, context: { duplicateOf: 'p2' } }, output: plain(lifecycle.classifyCatalogItem('panel', { name: 'Waaree 540', brand: 'Waaree', price: 1 }, { duplicateOf: 'p2' })) });
lifecycleCases.push({ id: 'classify catalogue', fn: 'classifyCatalog', input: { catalogRef: 'approved' }, output: plain(lifecycle.classifyCatalog(catalog)) });
lifecycleCases.push({ id: 'exclusion gaps', fn: 'findExclusionGaps', input: { catalogRef: 'approved' }, output: plain(lifecycle.findExclusionGaps(catalog)) });
lifecycleCases.push({ id: 'exclusion gaps fixture', fn: 'findExclusionGaps', input: { catalog: FX_CATALOG, tiers: ['premium', 'base'] }, output: plain(lifecycle.findExclusionGaps(FX_CATALOG, ['premium', 'base'])) });

// project BOM operation sequences: each step is recorded with the resulting BOM (or the error)
const projectCases = [];
function sequence(id, start, steps) {
  let bom = start;
  const out = [];
  for (const [op, arg] of steps) {
    const r = captured(() => {
      if (op === 'setComponent') return (bom = projectBom.setComponent(bom, arg));
      if (op === 'setQuantity') return (bom = projectBom.setQuantity(bom, arg));
      if (op === 'removeRole') return (bom = projectBom.removeRole(bom, arg));
      if (op === 'effectiveLine') return projectBom.effectiveLine(bom, arg.role);
      if (op === 'effectiveLines') return projectBom.effectiveLines(bom);
      if (op === 'diffAgainstPackage') return projectBom.diffAgainstPackage(bom);
      throw new Error(op);
    });
    out.push({ op, arg, ...r });
  }
  projectCases.push({ id, start: plain(start), steps: out });
}
const createArgs = { projectId: 'PRJ-1', packageId: 'PKG-1', architecture: 'DEYE', sysType: 'ongrid', tier: 'value', sizeKw: 3, phase: '1P', lines: [...DEYE_LINES, { role: 'METER' }], createdBy: 'ph-1', createdAt: AT };
projectCases.push({ id: 'create', start: null, steps: [{ op: 'create', arg: createArgs, output: plain(projectBom.createProjectBom(createArgs)) }] });
projectCases.push({ id: 'create defaults', start: null, steps: [{ op: 'create', arg: { projectId: 'PRJ-2' }, output: plain(projectBom.createProjectBom({ projectId: 'PRJ-2' })) }] });
sequence('edit sequence', projectBom.createProjectBom(createArgs), [
  ['setComponent', { role: 'PANEL', componentId: 'fx_pnl_lowsys', selectedBy: 'ph-1', at: AT, reason: 'stock' }],
  ['setComponent', { role: 'MONITORING', componentId: 'en3', quantity: 2, selectedBy: 'eng-1', at: AT, method: 'ENGINEER_PROPOSAL' }],
  ['setQuantity', { role: 'PANEL', quantity: 12, selectedBy: 'ph-1', at: AT }],
  ['removeRole', { role: 'DCDB', selectedBy: 'ph-1', at: AT, reason: 'integrated' }],
  ['removeRole', { role: 'MONITORING', selectedBy: 'ph-1', at: AT }],
  ['setComponent', { role: 'DCDB', componentId: 'fx_dcdb_hyb', selectedBy: 'ph-1', at: AT }],
  ['effectiveLine', { role: 'PANEL' }], ['effectiveLine', { role: 'MONITORING' }], ['effectiveLines', null], ['diffAgainstPackage', null],
  ['setComponent', { role: 'STRUCTURE', componentId: 'fx_str', quantity: 30 }], ['diffAgainstPackage', null],
]);
sequence('errors', projectBom.createProjectBom(createArgs), [
  ['setComponent', { componentId: 'x' }], ['setComponent', { role: 'DC_ISOLATOR', componentId: 'is1' }], ['setComponent', { role: 'PANEL' }],
  ['setComponent', { role: 'PANEL', componentId: null }], ['setQuantity', { role: 'BATTERY', quantity: 1 }], ['removeRole', { role: 'NOT_THERE' }],
]);
sequence('locked', { ...projectBom.createProjectBom(createArgs), status: 'LOCKED' }, [['setComponent', { role: 'PANEL', componentId: 'fx_pnl' }], ['removeRole', { role: 'PANEL' }], ['setQuantity', { role: 'PANEL', quantity: 1 }]]);

const snapshotCases = [];
const snapLines = [
  { componentId: 'fx_pnl', role: 'PANEL', quantity: 10, unitPurchaseCost: 12500, unitSellingPrice: 13585, selectionMethod: 'PACKAGE_DEFAULT' },
  { itemId: 'fx_inv_1p', role: 'INVERTER', qty: 1, unitPrice: 36000, engineeringApprovalState: 'VALID', selectedBy: 'ph-1', selectionTimestamp: AT },
  { catalogItemId: 'is1', qty: 1, componentDataVersion: 'cat@0' },
  { role: 'STRUCTURE' },
];
const snapArgs = { projectId: 'PRJ-1', bomLines: snapLines, selections: { PANEL: { selectionMethod: 'PROJECT_HEAD_OVERRIDE', selectedBy: 'ph-2', at: AT }, null: { selectedBy: 'orphan' } }, lockedBy: 'ph-1', lockedAt: AT, dataVersion: 'cat@1', engineeringStatus: 'WARNING' };
const snap = bomSnapshot.createProjectBomSnapshot(snapArgs);
snapshotCases.push({ id: 'create', fn: 'createProjectBomSnapshot', input: snapArgs, output: plain(snap) });
snapshotCases.push({ id: 'create defaults', fn: 'createProjectBomSnapshot', input: {}, output: plain(bomSnapshot.createProjectBomSnapshot({})) });
const draftSnap = { ...plain(snap), status: 'DRAFT' };
snapshotCases.push({ id: 'price change on LOCKED', fn: 'applyProcurementPriceChange', input: { snapshot: plain(snap), updates: { fx_pnl: 13000 } }, output: plain(bomSnapshot.applyProcurementPriceChange(snap, { fx_pnl: 13000 })) });
snapshotCases.push({ id: 'price change on DRAFT', fn: 'applyProcurementPriceChange', input: { snapshot: draftSnap, updates: { fx_pnl: 13000, is1: 0 } }, output: plain(bomSnapshot.applyProcurementPriceChange(draftSnap, { fx_pnl: 13000, is1: 0 })) });
snapshotCases.push({ id: 'price change on null', fn: 'applyProcurementPriceChange', input: { snapshot: null, updates: {} }, output: plain(bomSnapshot.applyProcurementPriceChange(null, {})) });
snapshotCases.push({ id: 'price new quotation', fn: 'priceForNewQuotation', input: { lines: snapLines, prices: { fx_pnl: 13100, fx_inv_1p: 35000 } }, output: plain(bomSnapshot.priceForNewQuotation(snapLines, { fx_pnl: 13100, fx_inv_1p: 35000 })) });
snapshotCases.push({ id: 'diff prices', fn: 'diffSnapshotAgainstCurrentPrices', input: { snapshot: plain(snap), prices: { fx_pnl: 13100, fx_inv_1p: 36000, is1: 950 } }, output: plain(bomSnapshot.diffSnapshotAgainstCurrentPrices(snap, { fx_pnl: 13100, fx_inv_1p: 36000, is1: 950 })) });
snapshotCases.push({ id: 'diff null snapshot', fn: 'diffSnapshotAgainstCurrentPrices', input: { snapshot: null, prices: {} }, output: plain(bomSnapshot.diffSnapshotAgainstCurrentPrices(null, {})) });
snapshotCases.push({ id: 'revise', fn: 'reviseSnapshot', input: { snapshot: plain(snap), changes: { dataVersion: 'cat@2' }, revisedBy: 'ph-3', revisedAt: FIXED_NOW }, output: plain(bomSnapshot.reviseSnapshot(snap, { dataVersion: 'cat@2' }, { revisedBy: 'ph-3', revisedAt: FIXED_NOW })) });
snapshotCases.push({ id: 'revise lines', fn: 'reviseSnapshot', input: { snapshot: plain(snap), changes: { bomLines: snapLines.slice(0, 1), selections: { PANEL: { at: FIXED_NOW } }, engineeringStatus: 'VALID' }, revisedBy: 'ph-3', revisedAt: FIXED_NOW }, output: plain(bomSnapshot.reviseSnapshot(snap, { bomLines: snapLines.slice(0, 1), selections: { PANEL: { at: FIXED_NOW } }, engineeringStatus: 'VALID' }, { revisedBy: 'ph-3', revisedAt: FIXED_NOW })) });

const statusCases = [];
const STATUSES = ['DRAFT', 'VALID', 'WARNING', 'BLOCKED', 'APPROVED', 'BOGUS', null];
for (const s of STATUSES) statusCases.push({ id: `status ${s}`, input: { status: s }, output: { canGenerateQuotation: bomStatus.canGenerateQuotation(s), canLockProjectBom: bomStatus.canLockProjectBom(s), requiresApproval: bomStatus.requiresApproval(s), statusReason: bomStatus.statusReason(s), quotationGate: plain(bomStatus.quotationGate(s)) } });
for (const a of STATUSES) for (const b of STATUSES) statusCases.push({ id: `transition ${a}→${b}`, input: { from: a, to: b }, output: bomStatus.canTransition(a, b) });
for (const v of [null, { status: 'BLOCKED' }, { status: 'WARNING' }, { status: 'VALID' }, { status: 'OTHER' }]) statusCases.push({ id: `fromValidation ${JSON.stringify(v)}`, input: { validation: v }, output: bomStatus.statusFromValidation(v) });

write('rules_bom.json', { roles: plain(roles.ROLES), categoryToRole: plain(roles.CATEGORY_TO_ROLE), enphaseComponentRoles: plain(roles.ENPHASE_COMPONENT_ROLES), projectEditableRoles: projectBom.PROJECT_EDITABLE_ROLES }, {
  roles: roleCases, identity: identityCases, lifecycle: lifecycleCases, projectBom: projectCases, snapshot: snapshotCases, status: statusCases,
});

// =================================================================================================================
// Quotation state: masked replay of issued documents (payload, commercial snapshots, freeze, document identity)
// =================================================================================================================
const state = readJson('quotation-state.json');
for (const q of Object.values(state.quotations)) {
  q.customer = maskCustomer(q.customer);
  if (q.proposalBy != null) q.proposalBy = 'Sales Person';
}
for (const docs of Object.values(state.documents)) {
  for (const d of docs) {
    d.payload = maskPayload(d.payload);
    if (d.snapshot && d.snapshot.branding) d.snapshot.branding = maskBrandingSnapshot(d.snapshot.branding);
  }
}
const storeLike = { bomSnapshots: new Map(Object.entries(state.bomSnapshots)), commercialSnapshots: new Map(Object.entries(state.commercialSnapshots)) };
const TIER_NAMES = readJson('tier-display-names.json');
const INCLUSIONS = readJson('quotation-inclusions.json');
const TESTIMONIALS = readJson('quotation-testimonials.json');

// Rebuild the engine results a payload was assembled from, out of the sections it carries (the payload copies them).
function payloadInputs(record, recordInputs, frozen) {
  const rows = frozen.bomSummary?.rows || [];
  const componentAttributes = Object.fromEntries(rows.filter((r) => r.attributesFound).map((r) => [r.componentId, r.attributes]));
  let subsidyResult = null;
  if (frozen.subsidy?.available) subsidyResult = frozen.subsidy.value;
  else if (frozen.subsidy && frozen.subsidy.treatment === 'ENGINE_RESULT' && !frozen.subsidy.dependency) {
    subsidyResult = { available: false, treatment: frozen.subsidy.treatment, source: frozen.subsidy.source, status: frozen.subsidy.status, eligibilityStatus: frozen.subsidy.eligibilityStatus, subsidyType: frozen.subsidy.subsidyType, reason: frozen.subsidy.reason };
  }
  const energy = frozen.energyProfile?.available
    ? { available: true, source: frozen.energyProfile.source, status: frozen.energyProfile.status, ...frozen.energyProfile.value, ...(frozen.consumption?.available ? frozen.consumption.value : {}),
      ...(frozen.applianceUsage?.available && frozen.applianceUsage.source !== 'SALES_APPLIANCE_ROWS' ? { appliances: frozen.applianceUsage.value.appliances, totalDailyUsage: frozen.applianceUsage.value.totalDailyUsage } : {}) }
    : null;
  return {
    ...recordInputs,
    subsidyTreatment: deriveSubsidyTreatment(record, subsidyResult),
    componentAttributes,
    campaignResult: frozen.campaign?.available ? { available: true, source: frozen.campaign.source, ...frozen.campaign.value } : null,
    energyProfileResult: energy,
    savingsResult: frozen.savings?.available ? frozen.savings.value : null,
    subsidyResult,
    financingResult: frozen.financing?.available ? frozen.financing.value : null,
    companyProfile: frozen.company && !(frozen.company.available === false && frozen.company.dependency === 'COMPANY_MASTER') ? frozen.company : null,
    tierDisplayNames: tierNamesOf(frozen),
    inclusionMatrix: inclusionsOf(frozen),
    testimonialsContent: testimonialsOf(frozen),
    documentContent: frozen.content?.available ? { version: frozen.content.value.version, content: frozen.content.value.content } : null,
    applianceRows: Array.isArray(record.applianceRows) ? record.applianceRows : null,
    capabilities: ['COST_VIEW'],
  };
}

// The configuration files changed after most documents were issued: rebuild the configuration each payload was built
// with from what it froze (names, inclusion matrix, testimonials are copied into the payload verbatim).
function tierNamesOf(frozen) {
  const sys = frozen.system;
  if (!sys || sys.available === false) return TIER_NAMES;
  const sections = sys.tierColumnNames ? Object.entries(sys.tierColumnNames) : [[String(sys.packageTier || '').toLowerCase(), sys.packageTierDisplayName]];
  if (sections.every(([, section]) => section && section.available === false && section.reason === 'No tier display name configuration supplied.')) return null;
  const names = {};
  for (const [tier, section] of sections) if (section && section.available) names[tier] = section.value;
  const tier = String(sys.packageTier || '').toLowerCase();
  if (sys.recommendedBadge) Object.assign(names, { recommendedTier: tier, recommendedBadge: sys.recommendedBadge });
  else Object.assign(names, { recommendedTier: TIER_NAMES.recommendedTier !== tier ? TIER_NAMES.recommendedTier : null, recommendedBadge: TIER_NAMES.recommendedBadge });
  return names;
}
function inclusionsOf(frozen) {
  const sections = frozen.inclusionsByTier ? Object.entries(frozen.inclusionsByTier) : [[String(frozen.system?.packageTier || '').toLowerCase(), frozen.inclusions]];
  if (sections.every(([, section]) => !section || (section.available === false && section.reason === 'No inclusion matrix configuration supplied.'))) return null;
  const matrix = {};
  const serviceRows = [];
  for (const [tier, section] of sections) {
    if (!section || !section.available) continue;
    matrix[section.tier] = { ...section.items, ...(section.tier.toLowerCase() === tier ? {} : { _tierCode: tier }) };
    (section.serviceMatrix || []).forEach((row, i) => { serviceRows[i] = { ...(serviceRows[i] || { label: row.label }), [tier]: row.value }; });
  }
  if (sections.some(([, section]) => section && section.serviceMatrix)) matrix._serviceMatrix = serviceRows;
  return matrix;
}
function testimonialsOf(frozen) {
  const t = frozen.testimonials;
  if (!t || !t.available) return { entries: [] };
  return { intro: t.value.intro, entries: t.value.entries, video: t.value.video };
}

function alternativeRecordInputs(q, opt, version) {
  const system = { ...(q.system ?? {}), ...(opt.system ?? {}), packageTier: opt.tier ?? opt.packageTier ?? q.system?.packageTier ?? null };
  return {
    quotation: { quotationNumber: q.quotationNumber ?? null, quotationVersion: version, quotationDate: q.quotationDate ?? null, validUntil: q.validUntil ?? null, proposalBy: q.proposalBy ?? null, salespersonId: q.salespersonId ?? null, quotationLanguage: q.quotationLanguage ?? null, status: q.status },
    customer: q.customer ?? null, site: q.site ?? null, system,
    bomSnapshot: opt.bomSnapshotId ? (storeLike.bomSnapshots.get(opt.bomSnapshotId) ?? null) : null,
    commercialSnapshot: opt.commercialSnapshotId ? (storeLike.commercialSnapshots.get(opt.commercialSnapshotId) ?? null) : null,
    engineering: q.engineering ?? null, upgradeIdentity: q.upgradeIdentity ?? null, subsidyTreatment: q.subsidyTreatment ?? null, variant: q.variant ?? 'FULL',
  };
}

function reconstructPackPricing(cs) {
  const p = cs.pricing, c = cs.cost, pk = cs.pack, v = cs.versions;
  const lcc = c.landedCostCheck || {};
  const costResult = lcc.status === 'COMPLETE'
    ? { status: 'COMPLETE', totalActualProjectCost: lcc.totalActualProjectCost, landedCostVersion: lcc.landedCostVersion, procurementPriceVersion: v.procurementPriceVersion, costEngineVersion: v.costEngineVersion, materialLandedCost: c.materialLandedCost, catalogVersion: v.catalogVersion }
    : (lcc.status === 'NOT_RUN' && !(lcc.errors || []).length ? null : { status: lcc.status, errors: (lcc.errors || []).map((code) => ({ code })) });
  return {
    snapshotId: cs.snapshotId, projectId: cs.projectId, bomSnapshotId: v.bomSnapshotId, configVersion: pk.configVersion, materialList: pk.materialList, costResult,
    issuedBy: cs.issuedBy, issuedAt: cs.issuedAt, moneyRuleVersion: v.moneyRuleVersion, catalogVersion: v.catalogVersion,
    offer: cs.offer ? { ...cs.offer } : null,
    packPricing: {
      status: 'COMPLETE', packPricingVersion: v.pricingEngineVersion, marketRateKey: pk.marketRateKey, inputs: pk.inputs, gst: { regime: p.gstRegime, ratePct: p.gstRatePct },
      customer: { sellingPriceBeforeGST: p.sellingPriceBeforeGST, gstAmount: p.gstAmount, sellingPriceIncludingGST: p.sellingPriceIncludingGST, customerTotalIncludingGST: p.customerTotalIncludingGST, marketRate: p.marketRate, swapDeltaTotal: p.swapDeltaTotal, roofAddOn: p.roofAddOn, transportExtra: p.extrasIncludingGST, transportExtraDetail: pk.transport },
      internal: { referenceCost: { material: c.materialCost, structureMaterial: c.structureCost, structureLabour: 0, structureRepair: 0, installation: c.installationCost, transportationBase: c.transportationCost, service: c.serviceAmcCost, miscellaneous: c.miscellaneousCost, office: c.officeExpenseAllocation }, referenceTotal: c.totalActualProjectCost, marginPct: c.referenceMarginPct, grand: c.referenceGrand, marginVsMarket: c.marginVsMarket, marginVsMarketPct: c.marginVsMarketPct },
      swapDeltas: pk.swapDeltas, roofAddOnDetail: pk.roofAddOnDetail, structureMaterial: pk.structureMaterial,
    },
  };
}

function reconstructFreeze(doc) {
  const s = doc.snapshot;
  const systemConfig = s.package || s.battery ? { ...(s.battery || {}), ...(s.package ? { systemType: s.package.systemType, size: s.package.size, phase: s.package.phase, tier: s.package.tier, profileKey: s.package.profileKey } : {}) } : undefined;
  const bom = s.engineering || systemConfig ? { ...(s.engineering ? { engineering: s.engineering } : {}), ...(systemConfig ? { systemConfig } : {}) } : null;
  const brandingStore = {};
  const brandingOverrides = {};
  for (const kind of ['bank', 'upi', 'signature', 'seal']) {
    const e = s.branding?.[kind];
    if (!e) continue;
    const rec = { versionId: e.versionId, publishedAt: e.publishedAt, publishedBy: e.publishedBy, isDemo: e.isDemo, values: e.values };
    if (kind === 'bank' || kind === 'upi') brandingStore[kind] = { primary: e.accountId, accounts: { [e.accountId]: { accountId: e.accountId, label: e.accountId, status: 'ACTIVE', isDemo: e.isDemo, current: e.versionId, versions: { [e.versionId]: rec } } } };
    else brandingStore[kind] = { current: e.versionId, versions: { [e.versionId]: rec } };
    if (e.source === 'PROJECT_HEAD_OVERRIDE') brandingOverrides[kind] = kind === 'bank' || kind === 'upi' ? { accountId: e.accountId, versionId: e.versionId } : e.versionId;
  }
  let policy = null, validityOverrideDays = null;
  const val = s.validity;
  if (val?.source === 'EFFECTIVE_POLICY') policy = { policies: [{ policyId: val.policyId, validityDays: val.validityDays, effectiveFrom: val.effectiveFrom, effectiveTo: val.effectiveTo, status: 'ACTIVE', version: val.policyVersion }] };
  else if (val?.source === 'ADMIN_DEFAULT') policy = { validity: { defaultDays: val.validityDays, version: val.policyVersion } };
  else if (val?.source === 'PROJECT_HEAD_OVERRIDE') { policy = { validity: { version: val.policyVersion } }; validityOverrideDays = val.validityDays; }
  const sub = s.subsidy;
  return {
    bom, project: { distanceKm: s.transportation?.distanceKm ?? null, vehicleType: s.transportation?.vehicleType ?? null },
    transportationConfig: null, commercialSnapshot: storeLike.commercialSnapshots.get(doc.commercialSnapshotId) ?? null,
    subsidyResult: sub ? { available: sub.available, subsidyType: sub.subsidyType, centralSubsidy: sub.centralSubsidy, stateSubsidy: sub.stateSubsidy, totalSubsidy: sub.totalSubsidy, eligibilityStatus: sub.eligibilityStatus, schemeName: sub.schemeName, calculatedAt: sub.calculatedAt } : null,
    brandingStore: s.branding ? brandingStore : null, brandingOverrides, policy, validityOverrideDays,
    issuedAt: doc.issuedAt, rendererPin: doc.rendererPinning, packageProfile: s.package?.profileNotes != null ? { notes: s.package.profileNotes } : null, productionMode: false,
  };
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
// Compare a rebuilt document with the frozen one. Members the frozen document does not have at all were added to the
// assembler after it was issued (the store holds documents from earlier code revisions); they are reported, not hidden.
function compareFrozen(rebuilt, frozen, p = '$', added = [], differing = []) {
  if (same(rebuilt, frozen)) return { added, differing };
  if (rebuilt && frozen && typeof rebuilt === 'object' && typeof frozen === 'object' && Array.isArray(rebuilt) === Array.isArray(frozen)) {
    if (Array.isArray(rebuilt) && rebuilt.length !== frozen.length) { differing.push(`${p} (length)`); return { added, differing }; }
    for (const k of Object.keys(rebuilt)) {
      if (!(k in frozen)) added.push(`${p}.${k}`);
      else compareFrozen(rebuilt[k], frozen[k], `${p}.${k}`, added, differing);
    }
    for (const k of Object.keys(frozen)) if (!(k in rebuilt)) differing.push(`${p}.${k} (removed)`);
    return { added, differing };
  }
  differing.push(p);
  return { added, differing };
}
function verdict(rebuilt, frozen) {
  const { added, differing } = compareFrozen(rebuilt, frozen);
  if (differing.length) return { verdict: 'differs', differing: differing.slice(0, 5) };
  if (added.length) return { verdict: 'identical apart from members added after issue', addedLater: added };
  return { verdict: 'identical' };
}

const replaySummary = {};
const replayCases = [];
const REPRESENTATIVE = new Set([
  'QT-ongrid_3_value_20260914075628864zarj', // latest: bilingual content, appliance rows, two alternatives
  'QT-ongrid_5sp_value_20260913163027211f4p0', // 5 kW single phase, content
  'QT-ongrid_3_premium_202609131408141573rhq', // Enphase premium primary, two alternatives
  'QT-ongrid_5sp_base_20260912055807178qnq1', // one alternative
  'QT-ongrid_5sp_base_20260910120000000l3n6', // subsidy not quoted
]);
for (const [qid, docs] of Object.entries(state.documents)) {
  for (const doc of docs) {
    const q = { ...state.quotations[qid], status: 'DRAFT' };
    const version = doc.version;
    const recordInputs = resolveInputs(storeLike, q, version);
    const primaryInputs = payloadInputs(q, { ...recordInputs, quotation: { ...recordInputs.quotation, status: 'ISSUED' } }, doc.payload);
    const primary = payloadLib.buildQuotationPayload(primaryInputs);
    const alternatives = (Array.isArray(q.alternativeOptions) ? q.alternativeOptions : []).map((opt, index) => {
      const frozenAlt = (doc.payload.alternatives || [])[index];
      const altInputs = payloadInputs(q, alternativeRecordInputs(q, opt, version), frozenAlt?.payload || {});
      return { opt, index, inputs: altInputs, payload: payloadLib.buildQuotationPayload(altInputs) };
    });
    const complete = alternatives.length ? { ...primary, alternatives: alternatives.map((a) => ({ optionIndex: a.index, tier: a.inputs.system.packageTier, bomSnapshotId: a.opt.bomSnapshotId ?? null, commercialSnapshotId: a.opt.commercialSnapshotId ?? null, payload: a.payload })) } : primary;
    const payloadVerdict = verdict(plain(complete), doc.payload);
    // commercial snapshots of the primary and every alternative
    const snapshotIds = [q.commercialSnapshotId, ...alternatives.map((a) => a.opt.commercialSnapshotId)].filter(Boolean);
    const commercial = snapshotIds.map((id) => {
      const stored = state.commercialSnapshots[id];
      if (!stored || stored.pricingMode !== 'PACK_MARKET_RATE') return { id, stored: !!stored, pack: false, matches: null };
      const args = reconstructPackPricing(stored);
      const rebuilt = commercialSnapshot.createPackCommercialSnapshot(args);
      return { id, args, output: plain(rebuilt), matches: same(plain(rebuilt), stored), verdict: verdict(plain(rebuilt), stored) };
    });
    let freeze = null;
    if (doc.snapshot) {
      const args = reconstructFreeze(doc);
      const rebuilt = captured(() => commercialFreeze.buildCommercialSnapshot(args));
      freeze = { args, ...rebuilt, matches: same(rebuilt.output, doc.snapshot) };
    }
    const pin = doc.rendererPinning ? plain(normalizeRendererPinning(doc.rendererPinning, doc.issuedAt)) : null;
    replaySummary[`${qid}#${version}`] = {
      payload: payloadVerdict.verdict === 'identical apart from members added after issue' ? `identical apart from members added after issue: ${payloadVerdict.addedLater.join(', ')}` : payloadVerdict.verdict === 'differs' ? `differs at ${payloadVerdict.differing.join(', ')}` : 'identical',
      commercialSnapshots: commercial.map((c) => (c.matches === null ? 'not a pack snapshot' : c.verdict.verdict === 'differs' ? `differs at ${c.verdict.differing.join(', ')}` : c.verdict.verdict === 'identical' ? 'identical' : `identical apart from members added after issue: ${c.verdict.addedLater.join(', ')}`)).join('; '),
      freeze: freeze ? (freeze.matches ? 'identical' : `differs${freeze.error ? `: ${freeze.error.code}` : ''}`) : 'no freeze',
      rendererPin: doc.rendererPinning ? (same(pin, doc.rendererPinning) ? 'identical' : 'differs') : 'none',
    };
    if (!REPRESENTATIVE.has(qid)) continue;
    if (payloadVerdict.verdict === 'differs' && !process.env.RULES_PROBE) throw new Error(`representative ${qid} does not replay: ${replaySummary[`${qid}#${version}`].payload}`);
    replayCases.push({
      id: `${qid}#${version}`,
      record: q,
      primaryInputs: plain(primaryInputs),
      alternatives: alternatives.map((a) => ({ option: a.opt, inputs: plain(a.inputs) })),
      commercial: commercial.filter((c) => c.args),
      freeze,
      document: plain({ ...doc, payload: undefined, snapshot: undefined }),
      expectedPayload: plain(complete),
      frozenPayloadVerdict: payloadVerdict,
      frozenSnapshotMatchesFreeze: freeze ? freeze.matches : null,
    });
  }
}

// ---- synthetic payload cases --------------------------------------------------------------------------------------
const base = replayCases.find((c) => c.id.startsWith('QT-ongrid_3_value_20260914075628864zarj'));
const baseInputs = base.primaryInputs;
const payloadCases = [];
const addPayload = (id, inputs) => payloadCases.push({ id, input: plain(inputs), output: plain(payloadLib.buildQuotationPayload(inputs)) });
addPayload('nothing supplied', {});
addPayload('no COST_VIEW', { ...baseInputs, capabilities: [] });
addPayload('draft BOM, no commercial snapshot', { ...baseInputs, bomSnapshot: { ...baseInputs.bomSnapshot, status: 'DRAFT' }, commercialSnapshot: null });
addPayload('snapshot pins a discount and customer-side expenses', { ...baseInputs, commercialSnapshot: { ...baseInputs.commercialSnapshot, versions: undefined, pricing: { ...baseInputs.commercialSnapshot.pricing, discount: { amount: 5000, reason: 'festival' }, customerSideExpenses: [{ expenseId: 'NET_METER', description: 'Net meter (KSEB)', amount: 7500 }], extras: [{ extraId: 'X', description: 'Extra wiring' }] } } });
addPayload('subsidy NOT_QUOTED', { ...baseInputs, subsidyTreatment: 'NOT_QUOTED' });
addPayload('subsidy ineligible result', { ...baseInputs, subsidyResult: { available: false, source: 'SUBSIDY_ENGINE', status: 'LIVE', eligibilityStatus: 'GIVE_IT_UP', subsidyType: 'residential' } });
addPayload('subsidy unresolved', { ...baseInputs, subsidyTreatment: null, subsidyResult: null });
addPayload('no engine results, profile appliance rows', { ...baseInputs, savingsResult: null, financingResult: { available: false }, energyProfileResult: null, applianceRows: [] });
addPayload('energy result without content', { ...baseInputs, documentContent: null, energyProfileResult: { available: true, source: 'ENERGY_ENGINE', status: 'LIVE', dailyGenerationLow: 10.8, dailyGenerationHigh: 13.2, monthlyKsebValueLow: 2945, monthlyKsebValueHigh: 3600, appliances: [{ id: 'fan', units: 1.2 }], totalDailyUsage: 11 } });
addPayload('appliance rows: unknown ids, zero quantities, default hours', { ...baseInputs, applianceRows: [{ id: 'ghost', qty: 2, hours: 3 }, { id: 'lights_fans', qty: 0, hours: 5 }, { id: 'refrigerator', qty: '1' }, { id: 'ac', qty: -1, hours: 5 }, { id: 'tv_ott', qty: 2, hours: 2.25 }] });
addPayload('appliance rows all dropped', { ...baseInputs, applianceRows: [{ id: 'ghost', qty: 1, hours: 1 }] });
addPayload('profile for a size below every band', { ...baseInputs, applianceRows: null, system: { ...baseInputs.system, systemSizeKw: 1 } });
addPayload('testimonials, campaign, tier names, inclusions: edge shapes', {
  ...baseInputs,
  testimonialsContent: { intro: '  12 homes nearby  ', entries: [{ name: ' A ', quote: ' Great ', systemKw: '3', billBefore: '', billAfter: null, monthlySaving: 'x', place: ' Alappuzha ', installedOn: ' June 2025 ', photoUri: ' /p.jpg ' }, { name: 'B' }, { name: 'C', quote: 'ok', systemKw: true }, { name: 'D', quote: 'ok' }, { name: 'E', quote: 'late' }], video: { title: ' Tour ', text: 3, link: ' https://example.invalid/v ' } },
  campaignResult: { available: true, id: 'cmp_1', name: 'Onam', theme: { festival: 'onam' }, primaryBenefit: { headline: 'Save' }, offers: [{ title: 'Free AMC', enabled: true }], luckyDraw: null, referral: { enabled: true }, artwork: { qrImage: 'q.png' }, period: { displayText: 'Aug–Sep' } },
  tierDisplayNames: { base: 'Essential System', value: '', premium: 7, recommendedTier: 'BASE', recommendedBadge: 'Best' },
  inclusionMatrix: { _serviceMatrix: [{ label: 'Monitoring', base: true, value: 'App', premium: ' ' }, { label: '' }, null], Standard: { _tierCode: 'base', solarPanels: true, inverter: 'yes', amc: null } },
  upgradeIdentity: upgradeModel.buildUpgradeIdentity({ fromKw: 3, toKw: 5, tier: 'value' }),
  site: { roofType: 'SHEET', distanceKm: 30 },
});
addPayload('commercial attributes are refused, odd shapes spread', {
  ...baseInputs,
  componentAttributes: Object.fromEntries(Object.entries(baseInputs.componentAttributes).map(([id, attrs], i) => [id, i === 0 ? { ...attrs, unitPrice: 13585, landedCost: 12000, supplierRef: 'S-1', crossSection: 4, gstRate: 5 } : i === 1 ? 'text attributes' : attrs])),
  campaignResult: { available: true, source: null, name: 'Text theme', theme: 'diwali', offers: 'none', primaryBenefit: ['a', 'b'], active: null },
  customer: { ...baseInputs.customer, source: 'OVERRIDDEN' },
});
addPayload('tier names and inclusions missing', { ...baseInputs, tierDisplayNames: 'names', inclusionMatrix: null, testimonialsContent: { entries: [] }, campaignResult: { available: false }, companyProfile: null, engineering: { status: 'WARNING', blocked: ['x'], warnings: ['PBC-D-003'] } });

// ---- projection, pinning, attributes, DCR ---------------------------------------------------------------------------
const projectionCases = [];
const smallPayload = { payloadVersion: 'quotationPayload.1', pricing: { available: true, customer: { gstAmount: 1 }, internal: { cost: { x: 1 } }, internalWithheld: false }, versions: Object.fromEntries(payloadLib.REQUIRED_VERSION_KEYS.map((k) => [k, `${k}@1`])), versionKeys: payloadLib.REQUIRED_VERSION_KEYS };
const withAlts = { ...smallPayload, alternatives: [{ optionIndex: 0, tier: 'base', payload: smallPayload }, null, { optionIndex: 2, tier: 'premium' }] };
for (const [id, payload, role] of [['sales reader', withAlts, 'SALES'], ['admin reader', withAlts, 'ADMIN'], ['no pricing section', { versions: { marginVersion: 'm' } }, 'SALES'], ['null payload', null, 'SALES']]) {
  projectionCases.push({ id, fn: 'projectIssuedPayloadForActor', input: { payload, canSeeInternalCost: workspace.canSeeInternalCost({ role }) }, output: plain(workspace.projectIssuedPayloadForActor(payload, { role })) });
}
const fullSnapshot = base.freeze.output;
for (const role of ['SALES', 'ADMIN']) projectionCases.push({ id: `snapshot ${role}`, fn: 'projectIssuedSnapshotForActor', input: { snapshot: fullSnapshot, canSeeInternalCost: workspace.canSeeInternalCost({ role }) }, output: plain(workspace.projectIssuedSnapshotForActor(fullSnapshot, { role })) });
projectionCases.push({ id: 'snapshot null', fn: 'projectIssuedSnapshotForActor', input: { snapshot: null, canSeeInternalCost: false }, output: plain(workspace.projectIssuedSnapshotForActor(null, { role: 'SALES' })) });
for (const [id, pin] of [['full pin', { name: 'Flarize Solar Quotation', version: 'v1', bundleSha256: 'a', templateSha256: 'b', manifestSha256: 'c', rendererVersion: 'approvedTemplate.1' }], ['server pinnedAt', { name: 'n', pinnedAt: '2026-01-01T00:00:00.000Z', extra: 'dropped' }], ['empty field', { name: '' }], ['number field', { version: 1 }], ['not an object', 'v1'], ['null', null]]) {
  projectionCases.push({ id: `pin ${id}`, fn: 'normalizeRendererPinning', input: { pinning: pin, at: AT }, ...captured(() => normalizeRendererPinning(pin, AT)) });
}
const someIds = [...new Set(Object.values(state.bomSnapshots).flatMap((s) => (s.lines || []).map((l) => l.componentId)))].slice(0, 40);
projectionCases.push({ id: 'attributes of locked ids', fn: 'componentAttributesFor', input: { catalogRef: 'approved', componentIds: [...someIds, 'nope', null] }, output: plain(workspace.componentAttributesFor(catalog, [...someIds, 'nope', null])) });
const everyId = Object.values(catalog.categories).flatMap((c) => c.items.map((i) => i.id));
projectionCases.push({ id: 'attributes of every catalogue item', fn: 'componentAttributesFor', input: { catalogRef: 'approved', componentIds: everyId }, output: plain(workspace.componentAttributesFor(catalog, everyId)) });
projectionCases.push({ id: 'attributes of fixture items', fn: 'componentAttributesFor', input: { catalog: FX_CATALOG, componentIds: ['fx_pnl', 'fx_inv_1p', 'fx_str', 'fx_bcable', 'en1'] }, output: plain(workspace.componentAttributesFor(FX_CATALOG, ['fx_pnl', 'fx_inv_1p', 'fx_str', 'fx_bcable', 'en1'])) });
const dcrAttrs = { p_dcr: { dcr: true }, p_non: { dcr: false }, p_tech: { technology: 'non-dcr' }, p_tech2: { technology: 'DCR' } };
for (const [id, lines] of [
  ['one DCR panel', [{ role: 'PANEL', componentId: 'p_dcr' }]], ['mixed panels', [{ role: 'PANEL', componentId: 'p_dcr' }, { category: 'module', componentId: 'p_non' }]],
  ['technology text', [{ role: 'PANEL', componentId: 'p_tech' }]], ['technology DCR', [{ category: 'solar_panel', componentId: 'p_tech2' }]], ['line panelType', [{ role: 'PANEL', panelType: 'dcr' }]],
  ['line dcr flag', [{ role: 'PANEL', dcr: 0 }]], ['unknown panel', [{ role: 'PANEL', componentId: 'p_unknown' }]], ['no panel', [{ role: 'INVERTER', componentId: 'i' }]],
]) projectionCases.push({ id: `dcr ${id}`, fn: 'resolvePanelDcrType', input: { bomSnapshot: { lines }, componentAttributes: dcrAttrs }, output: resolvePanelDcrType({ lines }, dcrAttrs) });

// ---- commercial snapshots (gross-margin and pack; the pack case priced by the real pricePack) ------------------------
const snapCases = [];
const costOk = { status: 'COMPLETE', projectId: 'PRJ-GM', bomSnapshotId: 'SNAP-GM', catalogVersion: 'cat@3', procurementPriceVersion: 'ppm@7', landedCostVersion: 'lc@7', costConfigVersion: 'cost-config.4', costEngineVersion: 'costEngine.3', materialCost: 123116.3, structureCost: 9972, installationCost: 15000, siteSurveyCost: 500, engineeringDesignCost: 500, transportationCost: 4550, serviceAmcCost: 10000, miscellaneousCost: 1631.16, officeExpenseAllocation: 15000, directProjectCost: 164269.46, totalActualProjectCost: 180900.46, traces: [{ head: 'TRANSPORTATION', amount: 4550, inputs: { vehicleType: 'STANDARD', distanceKm: 130, ratePerKm: 35, distanceBasis: 'ONE_WAY' }, version: 'VEH-STANDARD@2026-09-05', source: 'COST_CONFIG_VEHICLES' }] };
const pricingOk = { status: 'COMPLETE', marginType: 'GROSS_MARGIN', targetGrossMargin: 0.2, sellingPriceBeforeGST: 226125.58, grossProfit: 45225.12, gstRatePct: 8.9, gstAmount: 20125, sellingPriceIncludingGST: 246250.58, extras: [], extrasIncludingGST: 0, customerTotalIncludingGST: 246250.58, marginVersion: 'margin@2', gstVersion: 'gst@1', pricingEngineVersion: 'pricingEngine.3', marketRateReference: { value: 'mr@1' } };
snapCases.push({ id: 'gross margin', fn: 'createCommercialSnapshot', input: { snapshotId: 'CS-GM-1', costResult: costOk, pricingResult: pricingOk, issuedBy: 'ph-1', issuedAt: AT, offer: { id: 'offer_1', name: 'Onam', type: 'flat', value: '5000', active: true, version: 3, offerAmount: 5000 } }, ...captured(() => commercialSnapshot.createCommercialSnapshot({ snapshotId: 'CS-GM-1', costResult: costOk, pricingResult: pricingOk, issuedBy: 'ph-1', issuedAt: AT, offer: { id: 'offer_1', name: 'Onam', type: 'flat', value: '5000', active: true, version: 3, offerAmount: 5000 } })) });
snapCases.push({ id: 'gross margin, legacy total, offer value', fn: 'createCommercialSnapshot', input: { snapshotId: 'CS-GM-2', costResult: { ...costOk, totalActualProjectCost: undefined, totalActualCost: 180900.46, materialLandedCost: 120000 }, pricingResult: { ...pricingOk, customerSideExpenses: undefined }, moneyRuleVersion: 'money.2', offer: { offerId: 'o2', offerValue: 7, offerStatus: 'EXPIRED' } }, ...captured(() => commercialSnapshot.createCommercialSnapshot({ snapshotId: 'CS-GM-2', costResult: { ...costOk, totalActualProjectCost: undefined, totalActualCost: 180900.46, materialLandedCost: 120000 }, pricingResult: { ...pricingOk, customerSideExpenses: undefined }, moneyRuleVersion: 'money.2', offer: { offerId: 'o2', offerValue: 7, offerStatus: 'EXPIRED' } })) });
snapCases.push({ id: 'incomplete cost', fn: 'createCommercialSnapshot', input: { snapshotId: 'x', costResult: { ...costOk, status: 'INCOMPLETE' }, pricingResult: pricingOk }, ...captured(() => commercialSnapshot.createCommercialSnapshot({ snapshotId: 'x', costResult: { ...costOk, status: 'INCOMPLETE' }, pricingResult: pricingOk })) });
snapCases.push({ id: 'rejected pricing', fn: 'createCommercialSnapshot', input: { snapshotId: 'x', costResult: costOk, pricingResult: { ...pricingOk, status: 'REJECTED' } }, ...captured(() => commercialSnapshot.createCommercialSnapshot({ snapshotId: 'x', costResult: costOk, pricingResult: { ...pricingOk, status: 'REJECTED' } })) });
const builtBom = bomBuilder.buildBom({ systemType: 'ongrid', size: '3', tier: 'value', phase: '1P', configSource: 'approved', actorRole: 'PROJECT_HEAD' });
const vehicles = approvedConfig.transportConfig?.vehicles || [];
for (const [roof, distance] of [['SHEET', 130], ['FLAT', 60]]) {
  const priced = packPricing.pricePack({ config: approvedConfig, systemType: 'ongrid', size: '3', tier: 'value', roofType: roof, distanceKm: distance, vehicleType: vehicles[0]?.vehicleType, lines: builtBom.lines, pricedAt: AT });
  for (const [variant, costResult] of [['no cost run', null], ['cost complete', { ...costOk, materialLandedCost: 118000 }], ['cost incomplete', { status: 'INCOMPLETE', errors: [{ code: 'LANDED_COST_MISSING' }, { code: 'RATE_NOT_CONFIGURED' }] }]]) {
    const args = { snapshotId: `CS-PACK-${roof}`, projectId: 'PRJ-PACK', bomSnapshotId: 'SNAP-PACK', packPricing: priced, configVersion: packConfig.approved.version, materialList: builtBom.lines.slice(0, 3).map((l) => ({ componentId: l.componentId ?? null, name: l.name, qty: l.qty })), costResult, issuedBy: 'sales-1', issuedAt: AT, offer: null, catalogVersion: variant === 'cost complete' ? null : 'cat@9' };
    snapCases.push({ id: `pack ${roof} ${distance} km, ${variant}`, fn: 'createPackCommercialSnapshot', input: plain(args), ...captured(() => commercialSnapshot.createPackCommercialSnapshot(args)) });
  }
}
snapCases.push({ id: 'pack blocked', fn: 'createPackCommercialSnapshot', input: { snapshotId: 'x', packPricing: { status: 'BLOCKED' } }, ...captured(() => commercialSnapshot.createPackCommercialSnapshot({ snapshotId: 'x', packPricing: { status: 'BLOCKED' } })) });
const aSnapshot = state.commercialSnapshots[base.record.commercialSnapshotId];
snapCases.push({ id: 'config change never mutates', fn: 'applyConfigChange', input: { snapshot: aSnapshot, change: { marginVersion: 'margin@3' } }, output: plain(commercialSnapshot.applyConfigChange(aSnapshot, { marginVersion: 'margin@3' })) });
snapCases.push({ id: 'config change without description', fn: 'applyConfigChange', input: { snapshot: aSnapshot }, output: plain(commercialSnapshot.applyConfigChange(aSnapshot)) });
snapCases.push({ id: 'supersede', fn: 'supersede', input: { snapshot: aSnapshot, supersededBy: 'CS-NEXT', at: AT }, output: plain(commercialSnapshot.supersede(aSnapshot, { supersededBy: 'CS-NEXT', at: AT })) });
// the recalculation path with the REAL cost and pricing engines (the comparison is what the engine ports)
const costConfig = readJson('cost-config.json');
const priceMaster = readJson('procurement-price-master.json');
const lockedBomSnapshot = state.bomSnapshots[base.record.bomSnapshotId];
const pick = (o, keys) => Object.fromEntries(keys.filter((k) => o && o[k] !== undefined).map((k) => [k, o[k]]));
const gmSnapshot = commercialSnapshot.createCommercialSnapshot({ snapshotId: 'CS-GM-1', costResult: costOk, pricingResult: pricingOk, issuedBy: 'ph-1', issuedAt: AT });
for (const [id, previous] of [['against a gross-margin snapshot', gmSnapshot], ['against a stored pack snapshot', aSnapshot], ['no previous snapshot', null]]) {
  const r = commercialSnapshot.recalculateForNewQuotation({ previousSnapshot: previous, bomSnapshot: lockedBomSnapshot, config: costConfig, priceMaster, project: { sizeKw: 3, sizeKey: '3', installationType: 'flat', distanceKm: 130, vehicleType: 'STANDARD' }, margin: costConfig.margin, gst: costConfig.gst, calculatedAt: AT, pricedAt: AT, catalogVersion: 'cat@3', procurementPriceVersion: 'ppm@8' });
  const keys = ['totalActualCost', 'sellingPriceBeforeGST', 'pricingEngineVersion', ...commercialSnapshot.VERSION_KEYS];
  snapCases.push({ id: `recalculation ${id}`, fn: 'recalculationComparison', input: { previousSnapshot: previous ? plain(previous) : null, cost: plain(pick(r.cost, keys)), pricing: plain(pick(r.pricing, keys)) }, output: plain(r.comparison) });
}

// ---- commercial freeze: every branch -----------------------------------------------------------------------------------
const freezeCases = [];
const BRANDING_STORE = maskBrandingStore(readJson('quotation-branding-state.json'));
const POLICY = readJson('quotation-policy.json');
const freezeBase = { ...base.freeze.args };
const addFreeze = (id, args) => freezeCases.push({ id, input: plain(args), ...captured(() => commercialFreeze.buildCommercialSnapshot(args)) });
addFreeze('issuedAt missing', { ...freezeBase, issuedAt: null });
addFreeze('real branding store and policy', { ...freezeBase, brandingStore: BRANDING_STORE, brandingOverrides: {}, policy: POLICY, validityOverrideDays: null });
addFreeze('branding overrides', { ...freezeBase, brandingStore: BRANDING_STORE, brandingOverrides: { bank: { accountId: Object.keys(BRANDING_STORE.bank.accounts)[0] }, signature: Object.keys(BRANDING_STORE.signature.versions)[0] }, policy: POLICY, validityOverrideDays: 45 });
addFreeze('production refuses demo branding', { ...freezeBase, brandingStore: BRANDING_STORE, productionMode: true });
const liveBranding = clone(BRANDING_STORE);
for (const kind of ['bank', 'upi']) for (const acc of Object.values(liveBranding[kind].accounts)) { acc.isDemo = false; for (const v of Object.values(acc.versions)) v.isDemo = false; }
for (const kind of ['signature', 'seal']) for (const v of Object.values(liveBranding[kind].versions)) v.isDemo = false;
addFreeze('production accepts live branding', { ...freezeBase, brandingStore: liveBranding, productionMode: true });
addFreeze('production refuses demo transport rate', { ...freezeBase, brandingStore: null, commercialSnapshot: gmSnapshot, transportationConfig: costConfig.transportation, project: { vehicleType: 'STANDARD', distanceKm: 130 }, productionMode: true, policy: POLICY });
addFreeze('rate from the rate card passes in production', { ...freezeBase, brandingStore: null, commercialSnapshot: { ...plain(gmSnapshot), cost: { ...plain(gmSnapshot).cost, traces: [{ head: 'TRANSPORTATION', amount: 4550, inputs: { vehicleType: 'STANDARD', distanceKm: 130, ratePerKm: 35 }, version: 'RC-1', source: 'PROJECT_HEAD_RATE_CARD' }] } }, transportationConfig: costConfig.transportation, project: { vehicleType: 'STANDARD', distanceKm: 130 }, productionMode: true, policy: POLICY });
addFreeze('transport from cost-config vehicles (no trace)', { ...freezeBase, commercialSnapshot: { ...plain(gmSnapshot), cost: { ...plain(gmSnapshot).cost, traces: [] } }, transportationConfig: costConfig.transportation, project: { vehicleType: 'LORRY', distanceKm: 12 }, policy: null });
addFreeze('gross-margin snapshot, 0.15 target margin', { ...freezeBase, commercialSnapshot: { ...plain(gmSnapshot), pricing: { ...plain(gmSnapshot).pricing, targetGrossMargin: 0.15 } }, policy: null });
addFreeze('gross-margin snapshot, 0.07 target margin', { ...freezeBase, commercialSnapshot: { ...plain(gmSnapshot), pricing: { ...plain(gmSnapshot).pricing, targetGrossMargin: 0.07 } }, policy: null });
addFreeze('flat commercial shape', { issuedAt: AT, commercialSnapshot: { totalActualProjectCost: 100, marginType: 'GROSS_MARGIN', targetMarginPct: 20, minimumMarginPct: 15, listSellingPriceBeforeGST: 125, discount: { pct: 2 }, discountAmount: 2.5, sellingPriceBeforeGST: 122.5, finalSellingPriceBeforeGST: 122.5, gstRegime: 'SOLAR_70_30_COMPOSITE', gstRatePct: 8.9, gstAmount: 10.9, sellingPriceIncludingGST: 133.4, marginVersion: 'm', gstVersion: 'g', pricingMode: 'GROSS_MARGIN', procurementPriceVersion: 'p', landedCostVersion: 'l', catalogVersion: 'c' }, project: {} });
addFreeze('nothing but issuedAt', { issuedAt: AT });
addFreeze('policy not configured (development)', { ...freezeBase, policy: { schema: 'flarize.quotation-policy/2', policies: [] } });
addFreeze('policy not configured (production)', { ...freezeBase, brandingStore: null, policy: { schema: 'flarize.quotation-policy/2', policies: [] }, productionMode: true });
addFreeze('invalid override', { ...freezeBase, policy: POLICY, validityOverrideDays: 2.5 });

// ---- validity policy ---------------------------------------------------------------------------------------------------
const policyCases = [];
const addPolicy = (id, fn, input, call) => policyCases.push({ id, fn, input: plain(input), ...captured(call) });
for (const at of ['2026-09-14T07:56:28.864Z', '2026-08-15T00:00:00.000Z', '2026-10-01T00:00:00.000Z', '2027-03-01T12:00:00Z', '2026-09-01']) {
  addPolicy(`resolve at ${at}`, 'resolveEffectiveValidity', { store: POLICY, overrideDays: null, issuedAt: at }, () => policyLib.resolveEffectiveValidity(POLICY, null, at));
  addPolicy(`active at ${at}`, 'resolveActivePolicy', { store: POLICY, issuedAt: at }, () => policyLib.resolveActivePolicy(POLICY, at));
}
const overlapping = { validity: { defaultDays: null }, policies: [
  { policyId: 'A', validityDays: 10, effectiveFrom: '2026-09-01T00:00:00.000Z', status: 'ACTIVE' },
  { policyId: 'B', validityDays: 20, effectiveFrom: '2026-09-05T00:00:00.000Z', effectiveTo: '2026-12-01T00:00:00.000Z', status: 'ACTIVE', version: 'B@1' },
  { policyId: 'C', validityDays: 30, effectiveFrom: '2026-09-05T00:00:00.000Z', status: 'ACTIVE' },
  { policyId: 'D', validityDays: 40, effectiveFrom: '2026-09-06T00:00:00.000Z', status: 'INACTIVE' },
  { policyId: 'E', validityDays: 0, effectiveFrom: '2026-09-07T00:00:00.000Z', status: 'ACTIVE' },
  { policyId: 'F', validityDays: 5, effectiveFrom: 'not a date', status: 'ACTIVE' },
  null,
] };
for (const [id, store, override, at] of [
  ['overlap: latest from, then id descending', overlapping, null, '2026-09-10T00:00:00.000Z'], ['override wins', overlapping, 7, '2026-09-10T00:00:00.000Z'],
  ['override not an integer', overlapping, 1.5, AT], ['override zero', overlapping, 0, AT], ['before every window, no default', overlapping, null, '2026-08-01T00:00:00.000Z'],
  ['admin default', { validity: { defaultDays: 15, version: 'DEFAULT@x' }, policies: [] }, null, AT], ['empty store', {}, null, AT], ['invalid issuedAt', POLICY, null, 'yesterday'],
  ['default as text', { validity: { defaultDays: '15' } }, null, AT], ['integral decimal override', {}, 15.0, AT],
]) addPolicy(`validity ${id}`, 'resolveEffectiveValidity', { store, overrideDays: override, issuedAt: at }, () => policyLib.resolveEffectiveValidity(store, override, at));
for (const [id, rec] of [
  ['valid', { policyId: 'P', validityDays: 15, effectiveFrom: '2026-09-01T00:00:00Z' }], ['not an object', 'P'], ['no id', { validityDays: 15, effectiveFrom: '2026-09-01' }],
  ['bad days', { policyId: 'P', validityDays: 1.5, effectiveFrom: '2026-09-01' }], ['no from', { policyId: 'P', validityDays: 15 }], ['bad from', { policyId: 'P', validityDays: 15, effectiveFrom: 'soon' }],
  ['to before from', { policyId: 'P', validityDays: 15, effectiveFrom: '2026-09-02', effectiveTo: '2026-09-01' }], ['bad to', { policyId: 'P', validityDays: 15, effectiveFrom: '2026-09-02', effectiveTo: 'later' }],
  ['bad status', { policyId: 'P', validityDays: 15, effectiveFrom: '2026-09-02', status: 'PAUSED' }],
]) addPolicy(`record ${id}`, 'validatePolicyRecord', { policy: rec }, () => (policyLib.validatePolicyRecord(rec), 'ok'));
const ADMIN = { userId: 'admin-001', role: 'ADMIN' };
addPolicy('publish', 'publishPolicy', { store: POLICY, actorId: 'admin-001', policy: { policyId: 'NEW-1', validityDays: 21, effectiveFrom: '2026-11-01T00:00:00.000Z', note: 'Diwali' }, at: AT }, () => policyLib.publishPolicy(POLICY, { actor: ADMIN, policy: { policyId: 'NEW-1', validityDays: 21, effectiveFrom: '2026-11-01T00:00:00.000Z', note: 'Diwali' }, at: AT }));
addPolicy('publish into an empty store', 'publishPolicy', { store: null, actorId: 'admin-001', policy: { policyId: 'NEW-2', validityDays: 7, effectiveFrom: '2026-11-01', effectiveTo: '2026-12-01', status: 'INACTIVE', version: 'v9' }, at: AT }, () => policyLib.publishPolicy(null, { actor: ADMIN, policy: { policyId: 'NEW-2', validityDays: 7, effectiveFrom: '2026-11-01', effectiveTo: '2026-12-01', status: 'INACTIVE', version: 'v9' }, at: AT }));
addPolicy('publish duplicate', 'publishPolicy', { store: POLICY, actorId: 'admin-001', policy: { policyId: POLICY.policies[0].policyId, validityDays: 5, effectiveFrom: '2026-11-01' }, at: AT }, () => policyLib.publishPolicy(POLICY, { actor: ADMIN, policy: { policyId: POLICY.policies[0].policyId, validityDays: 5, effectiveFrom: '2026-11-01' }, at: AT }));
addPolicy('publish invalid', 'publishPolicy', { store: POLICY, actorId: 'admin-001', policy: { policyId: 'X', validityDays: -1, effectiveFrom: '2026-11-01' }, at: AT }, () => policyLib.publishPolicy(POLICY, { actor: ADMIN, policy: { policyId: 'X', validityDays: -1, effectiveFrom: '2026-11-01' }, at: AT }));
addPolicy('publish without at', 'publishPolicy', { store: POLICY, actorId: 'admin-001', policy: { policyId: 'X' }, at: null }, () => policyLib.publishPolicy(POLICY, { actor: ADMIN, policy: { policyId: 'X' }, at: null }));
addPolicy('deactivate', 'setPolicyStatus', { store: POLICY, actorId: 'admin-001', policyId: POLICY.policies[0].policyId, status: 'INACTIVE', at: AT, reason: 'superseded' }, () => policyLib.setPolicyStatus(POLICY, { actor: ADMIN, policyId: POLICY.policies[0].policyId, status: 'INACTIVE', at: AT, reason: 'superseded' }));
addPolicy('reactivate without a reason', 'setPolicyStatus', { store: POLICY, actorId: 'admin-001', policyId: POLICY.policies[1].policyId, status: 'ACTIVE', at: AT }, () => policyLib.setPolicyStatus(POLICY, { actor: ADMIN, policyId: POLICY.policies[1].policyId, status: 'ACTIVE', at: AT }));
addPolicy('status unknown policy', 'setPolicyStatus', { store: POLICY, actorId: 'admin-001', policyId: 'NOPE', status: 'ACTIVE', at: AT }, () => policyLib.setPolicyStatus(POLICY, { actor: ADMIN, policyId: 'NOPE', status: 'ACTIVE', at: AT }));
addPolicy('status invalid', 'setPolicyStatus', { store: POLICY, actorId: 'admin-001', policyId: 'NOPE', status: 'PAUSED', at: AT }, () => policyLib.setPolicyStatus(POLICY, { actor: ADMIN, policyId: 'NOPE', status: 'PAUSED', at: AT }));
addPolicy('status without at', 'setPolicyStatus', { store: POLICY, actorId: 'admin-001', policyId: 'NOPE', status: 'ACTIVE', at: '' }, () => policyLib.setPolicyStatus(POLICY, { actor: ADMIN, policyId: 'NOPE', status: 'ACTIVE', at: '' }));
addPolicy('default days', 'setDefaultValidityDays', { store: POLICY, actorId: 'admin-001', defaultDays: 30, at: AT }, () => policyLib.setDefaultValidityDays(POLICY, { actor: ADMIN, defaultDays: 30, at: AT }));
addPolicy('default days cleared', 'setDefaultValidityDays', { store: {}, actorId: 'admin-001', defaultDays: null, at: AT }, () => policyLib.setDefaultValidityDays({}, { actor: ADMIN, defaultDays: null, at: AT }));
addPolicy('default days invalid', 'setDefaultValidityDays', { store: {}, actorId: 'admin-001', defaultDays: 0, at: AT }, () => policyLib.setDefaultValidityDays({}, { actor: ADMIN, defaultDays: 0, at: AT }));
addPolicy('default days without at', 'setDefaultValidityDays', { store: {}, actorId: 'admin-001', defaultDays: 3, at: null }, () => policyLib.setDefaultValidityDays({}, { actor: ADMIN, defaultDays: 3, at: null }));
for (const now of [AT, null, 'garbage']) addPolicy(`describe at ${now}`, 'describePolicyStore', { store: POLICY, now }, () => policyLib.describePolicyStore(POLICY, now));
addPolicy('describe empty', 'describePolicyStore', { store: undefined, now: AT }, () => policyLib.describePolicyStore(undefined, AT));

const payloadRefs = { policy: POLICY, brandingStore: BRANDING_STORE, transportation: costConfig.transportation, grossMarginSnapshot: plain(gmSnapshot) };
for (const c of replayCases) {
  const record = c.record;
  const ids = [record.bomSnapshotId, record.commercialSnapshotId, ...(record.alternativeOptions || []).flatMap((o) => [o.bomSnapshotId, o.commercialSnapshotId])].filter(Boolean);
  for (const id of ids) {
    payloadRefs[id] = state.bomSnapshots[id] ?? state.commercialSnapshots[id];
    const pack = state.commercialSnapshots[id]?.pack;
    if (pack) for (const key of ['materialList', 'structureMaterial', 'swapDeltas', 'roofAddOnDetail']) if (pack[key] && typeof pack[key] === 'object') payloadRefs[`${id}.pack.${key}`] = pack[key];
  }
  const contentValue = c.primaryInputs.documentContent?.content;
  if (contentValue) payloadRefs[`content@${c.primaryInputs.documentContent.version}:${c.id}`] = contentValue;
  if (c.primaryInputs.companyProfile) payloadRefs[`company:${c.id}`] = c.primaryInputs.companyProfile;
}
// identical values need one ref only
const seenRef = new Map();
for (const [k, v] of Object.entries(payloadRefs)) { const t = JSON.stringify(v); if (seenRef.has(t)) delete payloadRefs[k]; else seenRef.set(t, k); }
write('rules_payload.json', {
  payloadVersion: payloadLib.QUOTATION_PAYLOAD_VERSION,
  versionKeys: commercialSnapshot.VERSION_KEYS,
  replayedDocuments: Object.keys(replaySummary).length,
  replaySummary,
  masking: 'customer name/phone/e-mail/address/pincode/salutation → pseudonyms; proposalBy → "Sales Person"; bank account numbers → MASKED-ACCOUNT (applied to inputs and frozen documents alike).',
}, {
  replay: replayCases, payload: payloadCases, projection: projectionCases, commercialSnapshot: snapCases, freeze: freezeCases, policy: policyCases,
}, payloadRefs);

// =================================================================================================================
// Gate
// =================================================================================================================
const gateCases = [];
const gateBase = { customer: base.primaryInputs.customer, system: base.primaryInputs.system, bomSnapshot: base.primaryInputs.bomSnapshot, engineering: base.primaryInputs.engineering, commercialSnapshot: base.primaryInputs.commercialSnapshot, subsidyTreatment: 'ENGINE_RESULT', quotation: base.primaryInputs.quotation };
const addGate = (id, check, inputs) => gateCases.push({ id, check, input: plain(inputs), output: plain(payloadLib.evaluateGenerationGate(inputs)) });
addGate('all pass', null, gateBase);
addGate('all fail', 'ALL', {});
addGate('CUSTOMER_DATA: no customer', 'CUSTOMER_DATA', { ...gateBase, customer: null });
addGate('CUSTOMER_DATA: no name', 'CUSTOMER_DATA', { ...gateBase, customer: { ...gateBase.customer, customerName: '' } });
addGate('SYSTEM_CONFIGURATION: no phase', 'SYSTEM_CONFIGURATION', { ...gateBase, system: { ...gateBase.system, phase: null } });
addGate('SYSTEM_CONFIGURATION: size missing', 'SYSTEM_CONFIGURATION', { ...gateBase, system: { systemType: 'ongrid', phase: '1P' } });
addGate('SYSTEM_CONFIGURATION: size zero passes', null, { ...gateBase, system: { ...gateBase.system, systemSizeKw: 0 } });
addGate('BOM_EXISTS: no lines', 'BOM_EXISTS', { ...gateBase, bomSnapshot: { ...gateBase.bomSnapshot, lines: [] } });
addGate('BOM_EXISTS: lines not a list', 'BOM_EXISTS', { ...gateBase, bomSnapshot: { ...gateBase.bomSnapshot, lines: { a: 1 } } });
addGate('BOM_LOCKED: draft', 'BOM_LOCKED', { ...gateBase, bomSnapshot: { ...gateBase.bomSnapshot, status: 'DRAFT' } });
addGate('BOM: none', 'BOM', { ...gateBase, bomSnapshot: null });
addGate('ENGINEERING_VALIDATION: blocked', 'ENGINEERING_VALIDATION', { ...gateBase, engineering: { status: 'BLOCKED' } });
addGate('ENGINEERING_VALIDATION: no verdict', 'ENGINEERING_VALIDATION', { ...gateBase, engineering: { status: '' } });
addGate('ENGINEERING_VALIDATION: missing', 'ENGINEERING_VALIDATION', { ...gateBase, engineering: null });
addGate('COMMERCIAL_SNAPSHOT: superseded', 'COMMERCIAL_SNAPSHOT', { ...gateBase, commercialSnapshot: { ...gateBase.commercialSnapshot, status: 'SUPERSEDED' } });
addGate('SUBSIDY_RESOLVED: unresolved', 'SUBSIDY_RESOLVED', { ...gateBase, subsidyTreatment: null });
addGate('SUBSIDY_RESOLVED: unknown treatment', 'SUBSIDY_RESOLVED', { ...gateBase, subsidyTreatment: 'ASSUMED_ZERO' });
addGate('SUBSIDY_RESOLVED: not quoted passes', null, { ...gateBase, subsidyTreatment: 'NOT_QUOTED' });
addGate('QUOTATION_METADATA: no number', 'QUOTATION_METADATA', { ...gateBase, quotation: { ...gateBase.quotation, quotationNumber: null } });
addGate('QUOTATION_METADATA: version zero passes', null, { ...gateBase, quotation: { ...gateBase.quotation, quotationVersion: 0 } });
addGate('QUOTATION_METADATA: no version', 'QUOTATION_METADATA', { ...gateBase, quotation: { ...gateBase.quotation, quotationVersion: undefined } });
const treatmentCases = [];
for (const [declared, result] of [['NOT_QUOTED', { available: true }], [null, { available: false }], [null, null], ['ENGINE_RESULT', null], ['CUSTOM', null], [undefined, undefined], ['NOT_QUOTED', null]]) {
  const record = declared === undefined ? {} : { subsidyTreatment: declared };
  treatmentCases.push({ id: `treatment ${declared}/${JSON.stringify(result)}`, input: { record, subsidyResult: result ?? null }, output: deriveSubsidyTreatment(record, result) });
}
const inputsCases = [];
for (const [id, q, version] of [
  ['issued record', base.record, 1], ['draft without snapshots', { status: 'DRAFT', quotationNumber: 'GR-1', customer: { customerId: 'C' } }, 2],
  ['record naming unknown snapshots', { status: 'DRAFT', bomSnapshotId: 'SNAP-NONE', commercialSnapshotId: 'CS-NONE', quotationSource: 'AFFILIATE', affiliateId: 'AFF-1', district: 'Alappuzha', variant: 'ACCOUNTS' }, 3],
]) {
  const out = resolveInputs(storeLike, q, version);
  inputsCases.push({ id, input: { record: q, version, bomSnapshot: q.bomSnapshotId ? (storeLike.bomSnapshots.get(q.bomSnapshotId) ?? null) : null, commercialSnapshot: q.commercialSnapshotId ? (storeLike.commercialSnapshots.get(q.commercialSnapshotId) ?? null) : null }, output: plain(out) });
}
write('rules_gate.json', { checks: Object.values(payloadLib.GATE_CHECK) }, { gate: gateCases, treatment: treatmentCases, inputs: inputsCases }, {
  bomSnapshot: gateBase.bomSnapshot, commercialSnapshot: gateBase.commercialSnapshot, customer: gateBase.customer,
});

// =================================================================================================================
// Content: the bilingual fit guard, helpers, store lifecycle; branding
// =================================================================================================================
const contentStore = readJson('quotation-content.json');
const PUBLISHED = contentStore.published.content;
const fitCases = [];
const addFit = (id, c, opts = {}) => fitCases.push({ id, input: { content: c, dailyGenPerKw: opts.dailyGenPerKw ?? null }, output: plain(content.validateContent(c, opts)), ...(fitCases.length % 6 === 0 ? { summary: plain(content.fitSummary(c, opts)) } : {}) });
const mutate = (fn) => { const c = clone(PUBLISHED); fn(c); return c; };
const pad = (n, ch = 'a') => ch.repeat(n);
addFit('published content fits', PUBLISHED);
addFit('draft content', contentStore.draft.content);
addFit('not an object', 'text');
addFit('array', []);
addFit('empty object', {});
addFit('labels missing', mutate((c) => { delete c.labels; }));
addFit('label en exactly at the limit', mutate((c) => { c.labels[Object.keys(c.labels)[0]].en = pad(300); }));
addFit('label en one over', mutate((c) => { c.labels[Object.keys(c.labels)[0]].en = pad(301); }));
addFit('label ml at 360, one over', mutate((c) => { const [a, b] = Object.keys(c.labels); c.labels[a].ml = pad(360, 'മ'); c.labels[b].ml = pad(361, 'മ'); }));
addFit('label counted in UTF-16 units (emoji)', mutate((c) => { c.labels[Object.keys(c.labels)[0]].en = `${pad(298)}😀`; c.labels[Object.keys(c.labels)[1]].en = `${pad(299)}😀`; }));
addFit('label trimmed before counting', mutate((c) => { c.labels[Object.keys(c.labels)[0]].en = `   ${pad(300)}   `; }));
addFit('label required in both languages', mutate((c) => { c.labels[Object.keys(c.labels)[0]] = { en: '  ', ml: null }; }));
addFit('label as plain text', mutate((c) => { c.labels[Object.keys(c.labels)[0]] = 'plain'; }));
addFit('term title and body limits', mutate((c) => { c.terms[0].title = { en: pad(61), ml: pad(90, 'മ') }; c.terms[1].body = { en: pad(1000), ml: pad(1101, 'മ') }; }));
addFit('terms total over', mutate((c) => { for (const t of c.terms) { t.body = { en: pad(990), ml: pad(1000, 'മ') }; } }));
addFit('terms: none', mutate((c) => { c.terms = []; }));
addFit('terms: twenty', mutate((c) => { c.terms = Array.from({ length: 20 }, (_, i) => ({ title: { en: `T${i}`, ml: `ടി${i}` }, body: { en: 'x', ml: 'y' } })); }));
addFit('terms not a list', mutate((c) => { c.terms = { a: 1 }; }));
addFit('timeline too short and too long', mutate((c) => { c.timeline = c.timeline.slice(0, 3); }));
addFit('timeline seven steps with long days', mutate((c) => { c.timeline = Array.from({ length: 7 }, () => ({ days: { en: '1234567', ml: '1-2' }, title: { en: 't', ml: 't' }, body: { en: 'b', ml: 'b' } })); }));
addFit('weHandle / customerScope / requiredDocuments limits', mutate((c) => { c.weHandle = c.weHandle.slice(0, 2); c.customerScope[0] = { en: pad(271), ml: pad(280, 'മ') }; c.requiredDocuments = Array.from({ length: 9 }, () => ({ en: 'd', ml: 'd' })); }));
addFit('lifeNow and lifeSolar exactly three', mutate((c) => { c.lifeNow = c.lifeNow.slice(0, 2); c.lifeSolar = [...c.lifeSolar, c.lifeSolar[0]]; c.lifeSolar[0].title = { en: pad(33), ml: pad(49, 'മ') }; }));
addFit('extra structure cost', mutate((c) => { c.extraStructureCost = { label: { en: pad(49), ml: '' }, value: pad(21) }; }));
addFit('extra structure cost not an object', mutate((c) => { c.extraStructureCost = 'n/a'; }));
addFit('appliances missing', mutate((c) => { delete c.appliances; }));
addFit('appliances master not a list', mutate((c) => { c.appliances.master = {}; }));
addFit('appliance master rules', mutate((c) => {
  const m = c.appliances.master;
  m[0].id = 'Bad-Id'; m[1].id = m[2].id; m[3].name = { en: pad(29), ml: pad(29, 'മ') }; m[4].name = { ml: 'x' }; m[5].watts = 0; m[6].watts = 10001; m[7].defaultHours = 25;
  m.push(...Array.from({ length: 9 }, (_, i) => ({ id: `extra_${i}`, name: { en: 'X' }, watts: 10, defaultHours: 1 })));
}));
addFit('appliance profile rules', mutate((c) => {
  const p = c.appliances.profiles;
  p.abc = []; p['-1'] = []; p['12'] = 'rows';
  const k = Object.keys(p).find((x) => Array.isArray(p[x]) && p[x].length);
  p[k] = [...p[k], { id: 'ghost', qty: 51, hours: -1 }, { ...p[k][0] }, ...Array.from({ length: 9 }, () => ({ id: p[k][0].id, qty: 1, hours: 1 }))];
}));
addFit('profile over generation', mutate((c) => { const p = c.appliances.profiles; const k = Object.keys(p)[0]; p[k] = p[k].map((r) => ({ ...r, qty: 50, hours: 24 })); }));
addFit('live generation rate tightens the rule', PUBLISHED, { dailyGenPerKw: 1.2 });
addFit('non-numeric generation rate falls back', PUBLISHED, { dailyGenPerKw: 'x' });

const helperCases = [];
const addHelper = (id, fn, input, call) => helperCases.push({ id, fn, input: plain(input), ...captured(call) });
for (const [field, lang] of [[{ en: 'Hello', ml: 'നമസ്കാരം' }, 'ml'], [{ en: 'Hello', ml: '  ' }, 'ml'], [{ ml: 'മ' }, 'en'], [{ hi: 'नमस्ते', ta: '' }, 'en'], ['plain', 'ml'], [null, 'en'], [42, 'en'], [{ en: 5 }, 'en'], [{}, 'en']]) {
  addHelper(`pick ${JSON.stringify(field)} ${lang}`, 'pick', { field, lang }, () => content.pick(field, lang));
}
for (const v of ['ML', 'malayalam', ' ml-IN ', 'en', 'hi', null, undefined, 3]) addHelper(`language ${v}`, 'normaliseLanguage', { value: v ?? null }, () => content.normaliseLanguage(v));
const labelKey = Object.keys(PUBLISHED.labels)[0];
for (const [key, lang, vars] of [[labelKey, 'ml', null], [labelKey, 'en', { kw: 3 }], ['missing', 'en', { kw: 3 }]]) addHelper(`label ${key} ${lang}`, 'label', { key, lang, vars }, () => content.label(PUBLISHED, key, lang, vars || undefined));
const synthetic = { labels: { greet: { en: 'Hi {name}, {kw} kW {missing}', ml: '' } } };
addHelper('label interpolation', 'label', { content: synthetic, key: 'greet', lang: 'ml', vars: { name: 'Anu', kw: 3.5, missing: null } }, () => content.label(synthetic, 'greet', 'ml', { name: 'Anu', kw: 3.5, missing: null }));
for (const kw of [1, 3, 4.5, 5, 8, 10, 12, 'x', null]) {
  addHelper(`profileKey ${kw}`, 'profileKeyForKw', { profiles: PUBLISHED.appliances.profiles, kw }, () => content.profileKeyForKw(PUBLISHED.appliances.profiles, kw));
  addHelper(`defaultRows ${kw}`, 'defaultRowsForKw', { kw }, () => content.defaultRowsForKw(PUBLISHED, kw));
  addHelper(`dailyGeneration ${kw}`, 'dailyGenerationForKw', { kw, rate: 4.4 }, () => content.dailyGenerationForKw(kw, 4.4));
}
addHelper('profileKey none', 'profileKeyForKw', { profiles: {}, kw: 3 }, () => content.profileKeyForKw({}, 3));
addHelper('dailyGeneration fallback rate', 'dailyGenerationForKw', { kw: 3, rate: null }, () => content.dailyGenerationForKw(3, null));
const rowsIn = [...PUBLISHED.appliances.profiles[Object.keys(PUBLISHED.appliances.profiles)[0]], { id: 'ghost', qty: 1 }, { id: PUBLISHED.appliances.master[0].id, qty: '2', hours: null, watts: 15 }];
for (const lang of ['en', 'ml']) addHelper(`materialise ${lang}`, 'materialiseRows', { rows: rowsIn, lang }, () => content.materialiseRows(PUBLISHED, rowsIn, lang));
addHelper('totalUnits', 'totalUnits', { rows: [{ units: 1.1 }, { units: 2.25 }, { units: 'x' }, {}] }, () => content.totalUnits([{ units: 1.1 }, { units: 2.25 }, { units: 'x' }, {}]));

const storeCases = [];
const emptyStore = content.createEmptyStore(clone(PUBLISHED));
storeCases.push({ id: 'create', fn: 'createEmptyStore', input: { content: PUBLISHED }, output: plain(emptyStore) });
const edited = mutate((c) => { c.labels[labelKey].en = 'Edited'; });
let working = clone(emptyStore);
storeCases.push({ id: 'save draft', fn: 'saveDraft', input: { store: plain(working), content: edited, actorId: 'ph-1', at: AT }, ...captured(() => (working = content.saveDraft(working, clone(edited), { userId: 'ph-1' }, AT))) });
storeCases.push({ id: 'describe after save', fn: 'describeStore', input: { store: plain(working) }, output: plain(content.describeStore(working)) });
storeCases.push({ id: 'save invalid draft', fn: 'saveDraft', input: { store: plain(working), content: {}, actorId: 'ph-1', at: AT }, ...captured(() => content.saveDraft(clone(working), {}, { userId: 'ph-1' }, AT)) });
storeCases.push({ id: 'publish', fn: 'publishDraft', input: { store: plain(working), actorId: 'admin-001', at: FIXED_NOW }, ...captured(() => (working = content.publishDraft(working, { userId: 'admin-001' }, FIXED_NOW))) });
storeCases.push({ id: 'describe after publish', fn: 'describeStore', input: { store: plain(working) }, output: plain(content.describeStore(working)) });
const brokenDraft = { ...clone(working), draft: { ...clone(working).draft, content: { labels: {} } } };
storeCases.push({ id: 'publish invalid draft', fn: 'publishDraft', input: { store: brokenDraft, actorId: 'admin-001', at: FIXED_NOW }, ...captured(() => content.publishDraft(clone(brokenDraft), { userId: 'admin-001' }, FIXED_NOW)) });
storeCases.push({ id: 'discard', fn: 'discardDraft', input: { store: brokenDraft }, output: plain(content.discardDraft(clone(brokenDraft))) });
storeCases.push({ id: 'accessors', fn: 'accessors', input: { store: plain(working) }, output: { published: plain(content.getPublished(working)) === null ? null : 'content', draft: content.getDraft({}) } });

const brandingCases = [];
const addBranding = (id, fn, input, call) => brandingCases.push({ id, fn, input: plain(input), ...captured(call) });
const bankAccounts = Object.keys(BRANDING_STORE.bank.accounts);
const sigVersions = Object.keys(BRANDING_STORE.signature.versions);
const firstBankVersion = Object.keys(BRANDING_STORE.bank.accounts[bankAccounts[0]].versions)[0];
addBranding('freeze primary', 'freezeBrandingSnapshot', { store: BRANDING_STORE, overrides: {} }, () => branding.freezeBrandingSnapshot(BRANDING_STORE, {}));
addBranding('freeze v2 override', 'freezeBrandingSnapshot', { store: BRANDING_STORE, overrides: { bank: { accountId: bankAccounts[bankAccounts.length - 1] }, upi: { accountId: Object.keys(BRANDING_STORE.upi.accounts)[0], versionId: Object.keys(Object.values(BRANDING_STORE.upi.accounts)[0].versions)[0] } } }, () => branding.freezeBrandingSnapshot(BRANDING_STORE, { bank: { accountId: bankAccounts[bankAccounts.length - 1] }, upi: { accountId: Object.keys(BRANDING_STORE.upi.accounts)[0], versionId: Object.keys(Object.values(BRANDING_STORE.upi.accounts)[0].versions)[0] } }));
addBranding('freeze v1 string overrides', 'freezeBrandingSnapshot', { store: BRANDING_STORE, overrides: { bank: firstBankVersion, signature: sigVersions[0] } }, () => branding.freezeBrandingSnapshot(BRANDING_STORE, { bank: firstBankVersion, signature: sigVersions[0] }));
for (const [id, overrides] of [
  ['override without accountId', { bank: { versionId: 'x' } }], ['unknown account', { bank: { accountId: 'NOPE' } }], ['unknown version', { bank: { accountId: bankAccounts[0], versionId: 'NOPE' } }],
  ['unknown string version', { upi: 'NOPE' }], ['object override on single slot', { seal: { accountId: 'legacy' } }], ['array override', { bank: ['x'] }],
]) addBranding(`freeze ${id}`, 'freezeBrandingSnapshot', { store: BRANDING_STORE, overrides }, () => branding.freezeBrandingSnapshot(BRANDING_STORE, overrides));
const inactiveStore = clone(BRANDING_STORE); inactiveStore.bank.accounts[inactiveStore.bank.primary].status = 'INACTIVE';
addBranding('freeze inactive primary', 'freezeBrandingSnapshot', { store: inactiveStore, overrides: {} }, () => branding.freezeBrandingSnapshot(inactiveStore, {}));
addBranding('freeze override of inactive account (string)', 'freezeBrandingSnapshot', { store: inactiveStore, overrides: { bank: Object.keys(inactiveStore.bank.accounts[inactiveStore.bank.primary].versions)[0] } }, () => branding.freezeBrandingSnapshot(inactiveStore, { bank: Object.keys(inactiveStore.bank.accounts[inactiveStore.bank.primary].versions)[0] }));
const v1Store = { schema: 'flarize.quotation-branding/1', bank: { current: 'BK-1', versions: { 'BK-1': { versionId: 'BK-1', publishedAt: AT, publishedBy: 'admin-001', values: { bankName: 'Federal Bank', accountName: 'Flarize', accountNumber: 'MASKED-ACCOUNT', ifsc: 'FDRL0000001' } } } }, upi: { current: null, versions: {} }, seal: { current: 'SEAL-9', versions: {} } };
addBranding('freeze v1 store', 'freezeBrandingSnapshot', { store: v1Store, overrides: {} }, () => branding.freezeBrandingSnapshot(v1Store, {}));
addBranding('freeze empty store', 'freezeBrandingSnapshot', { store: {}, overrides: {} }, () => branding.freezeBrandingSnapshot({}, {}));
const danglingPrimary = { bank: { primary: 'GONE', accounts: {} }, upi: { primary: 'U', accounts: { U: { accountId: 'U', current: null, versions: {} } } } };
addBranding('freeze dangling primary', 'freezeBrandingSnapshot', { store: danglingPrimary, overrides: {} }, () => branding.freezeBrandingSnapshot(danglingPrimary, {}));
for (const store of [BRANDING_STORE, v1Store, {}]) {
  for (const kind of ['bank', 'signature']) {
    addBranding(`list ${kind} ${store.schema ?? 'empty'}`, 'listAccounts', { store, kind }, () => branding.listAccounts(store, kind));
    addBranding(`demo ${kind} ${store.schema ?? 'empty'}`, 'kindContainsDemo', { store, kind }, () => branding.kindContainsDemo(store, kind));
  }
  addBranding(`describe ${store.schema ?? 'empty'}`, 'describeBrandingStore', { store }, () => branding.describeBrandingStore(store));
  addBranding(`current bank ${store.schema ?? 'empty'}`, 'currentVersionId', { store, kind: 'bank' }, () => branding.currentVersionId(store, 'bank'));
}
addBranding('list unknown kind', 'listAccounts', { store: {}, kind: 'logo' }, () => branding.listAccounts({}, 'logo'));
addBranding('get version', 'getVersion', { store: BRANDING_STORE, kind: 'bank', versionId: firstBankVersion }, () => branding.getVersion(BRANDING_STORE, 'bank', firstBankVersion));
addBranding('get missing version', 'getVersion', { store: BRANDING_STORE, kind: 'bank', versionId: 'NOPE' }, () => branding.getVersion(BRANDING_STORE, 'bank', 'NOPE'));
const BANK_VALUES = { bankName: 'Federal Bank', accountName: 'Flarize Technologies', accountNumber: 'MASKED-ACCOUNT', ifsc: 'FDRL0001267', branch: 'Alappuzha' };
for (const [id, args] of [
  ['new bank account', { kind: 'bank', accountId: 'ACC-FED', label: 'Federal', values: BANK_VALUES, at: AT }],
  ['new bank account made primary, demo', { kind: 'bank', accountId: 'ACC FED/2', values: BANK_VALUES, at: AT, isDemo: true, makePrimary: true }],
  ['next version of an existing account', { kind: 'bank', accountId: bankAccounts[0], values: BANK_VALUES, at: FIXED_NOW, versionId: 'BK-NEXT' }],
  ['signature', { kind: 'signature', values: { signatoryName: 'Director' }, at: AT }],
  ['seal into a v1 store', { kind: 'seal', values: {}, at: AT, store: v1Store }],
  ['upi into an empty store', { kind: 'upi', accountId: 'UPI-1', values: { upiId: 'pay@bank' }, at: AT, store: {} }],
  ['missing values', { kind: 'bank', accountId: 'ACC-X', values: { bankName: 'x', accountNumber: '' }, at: AT }],
  ['values not an object', { kind: 'upi', accountId: 'U', values: null, at: AT }],
  ['duplicate version', { kind: 'bank', accountId: bankAccounts[0], values: BANK_VALUES, at: AT, versionId: firstBankVersion }],
  ['duplicate slot version', { kind: 'signature', values: { signatoryName: 'D' }, at: AT, versionId: sigVersions[0] }],
  ['bank without account', { kind: 'bank', values: BANK_VALUES, at: AT }],
  ['unknown kind', { kind: 'logo', values: {}, at: AT }],
  ['without at', { kind: 'upi', accountId: 'U', values: { upiId: 'x' }, at: '' }],
]) {
  const { store: given, ...rest } = args;
  const store = given ?? BRANDING_STORE;
  addBranding(`publish ${id}`, 'publishBrandingVersion', { store, actorId: 'admin-001', ...rest }, () => branding.publishBrandingVersion(store, { actor: ADMIN, ...rest }));
}
for (const [id, fn, args] of [
  ['deactivate primary hands over', 'setBrandingAccountStatus', { kind: 'bank', accountId: BRANDING_STORE.bank.primary, status: 'INACTIVE', at: AT }],
  ['reactivate', 'setBrandingAccountStatus', { kind: 'upi', accountId: Object.keys(BRANDING_STORE.upi.accounts)[0], status: 'ACTIVE', at: AT }],
  ['status of a single slot', 'setBrandingAccountStatus', { kind: 'seal', accountId: 'legacy', status: 'ACTIVE', at: AT }],
  ['status invalid', 'setBrandingAccountStatus', { kind: 'bank', accountId: bankAccounts[0], status: 'PAUSED', at: AT }],
  ['status unknown account', 'setBrandingAccountStatus', { kind: 'bank', accountId: 'NOPE', status: 'ACTIVE', at: AT }],
  ['status without at', 'setBrandingAccountStatus', { kind: 'bank', accountId: 'NOPE', status: 'ACTIVE', at: null }],
  ['primary', 'setPrimaryBrandingAccount', { kind: 'bank', accountId: bankAccounts[bankAccounts.length - 1], at: AT }],
  ['primary of a single slot', 'setPrimaryBrandingAccount', { kind: 'signature', accountId: 'legacy', at: AT }],
  ['primary unknown account', 'setPrimaryBrandingAccount', { kind: 'upi', accountId: 'NOPE', at: AT }],
  ['primary without at', 'setPrimaryBrandingAccount', { kind: 'upi', accountId: 'NOPE', at: '' }],
]) addBranding(id, fn, { store: BRANDING_STORE, actorId: 'admin-001', ...args }, () => branding[fn](BRANDING_STORE, { actor: ADMIN, ...args }));
const oneInactive = clone(BRANDING_STORE); oneInactive.bank.accounts[bankAccounts[0]].status = 'INACTIVE';
addBranding('primary must be active', 'setPrimaryBrandingAccount', { store: oneInactive, actorId: 'admin-001', kind: 'bank', accountId: bankAccounts[0], at: AT }, () => branding.setPrimaryBrandingAccount(oneInactive, { actor: ADMIN, kind: 'bank', accountId: bankAccounts[0], at: AT }));
const solo = { bank: { primary: 'A', accounts: { A: { accountId: 'A', status: 'ACTIVE', current: 'v', versions: { v: { values: BANK_VALUES } } } } } };
addBranding('deactivate the only account', 'setBrandingAccountStatus', { store: solo, actorId: 'admin-001', kind: 'bank', accountId: 'A', status: 'INACTIVE', at: AT }, () => branding.setBrandingAccountStatus(solo, { actor: ADMIN, kind: 'bank', accountId: 'A', status: 'INACTIVE', at: AT }));

const contentRefs = { published: PUBLISHED, draft: contentStore.draft.content, edited, brandingStore: BRANDING_STORE, inactiveBrandingStore: inactiveStore, oneInactiveBrandingStore: oneInactive };
for (const [key, value] of Object.entries(PUBLISHED)) contentRefs[`published.${key}`] = value;
contentRefs['published.appliances.master'] = PUBLISHED.appliances.master;
contentRefs['published.appliances.profiles'] = PUBLISHED.appliances.profiles;
write('rules_content.json', { limits: content.LIMITS, storeVersion: content.CONTENT_STORE_VERSION, brandingKinds: branding.BRANDING_KINDS }, {
  fit: fitCases, helpers: helperCases, store: storeCases, branding: brandingCases,
}, contentRefs);
