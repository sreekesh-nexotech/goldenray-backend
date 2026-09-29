#!/usr/bin/env node
// Golden file of the Flarize quotation flow: five representative quotations issued by the REAL Flarize JavaScript
// (salesOrchestrator.orchestrateThreeTiers, exactly as POST /api/sales/orchestrate calls it in pack mode) over the real
// data (read-only, FLARIZE_ROOT/data). For each case it records the orchestration result, the issued document (payload
// with its alternatives, commercial freeze) and the per-tier commercial and BOM snapshots.
//
// The Python side (quotations/tests/test_orchestrator_parity.py) imports the same data through the platform importers
// (catalog, pricing, bom, packs, quotation content), publishes PriceRelease #1 / PackRelease #1, issues the same five
// quotations through quotations.services and compares the engine outputs field by field (documented exclusions:
// identifiers, clock-stamped ids, company/branding, which come from the platform's company master).
//
// Deterministic: TZ=UTC, the clock is frozen at AT, Math.random is a fixed LCG, no CMS campaign, no renderer pin.
// Personal data: the customers are synthetic; the testimonials come from the masked fixture.
//
// Usage: TZ=UTC node quotations/tests/golden/generate_quotations.mjs [FLARIZE_ROOT]
//        (writes quotations/tests/golden/flarize_quotations.json)

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(process.argv[2] || process.env.FLARIZE_ROOT || '/home/user/flarize-main/flarize');
const OUT = process.env.GOLDEN_OUT || path.join(HERE, 'flarize_quotations.json');
const AT = '2026-09-20T10:00:00.000Z';

// ---- frozen clock and randomness -----------------------------------------------------------------------------------
const RealDate = Date;
globalThis.Date = class FrozenDate extends RealDate {
  constructor(...args) {
    if (args.length === 0) super(AT);
    else super(...args);
  }
  static now() {
    return new RealDate(AT).getTime();
  }
};
let seed = 20260920;
Math.random = () => {
  seed = (seed * 1103515245 + 12345) % 2147483648;
  return seed / 2147483648;
};

const SOURCES = [
  'src/lib/salesOrchestrator.js', 'src/lib/quotationWorkspace.js', 'src/lib/quotationPayload.js', 'src/lib/quotationPolicy.js',
  'src/lib/commercialSnapshot.js', 'src/lib/commercialFreeze.js', 'src/lib/bomSnapshot.js', 'src/lib/bomLock.js',
  'src/lib/projectWorkspace.js', 'src/lib/projectBom.js', 'src/lib/packPricing.js', 'src/lib/engineeringChecker.js',
  'src/lib/energyEngine.js', 'src/lib/savingsEngine.js', 'src/lib/subsidyEngine.js', 'src/lib/financeEngine.js',
  'src/lib/quotationBranding.js', 'src/lib/packageProjection.js', 'server-bom-builder.js', 'server-sales-orchestrator.js',
  'data/catalog.json', 'data/pack-config.json', 'data/packages.proposed.json', 'data/battery-master.json', 'data/cost-config.json',
  'data/quotation-content.json', 'data/quotation-testimonials.json', 'data/quotation-inclusions.json', 'data/tier-display-names.json',
  'data/quotation-policy.json', 'data/energy-config.json', 'data/savings-config.json', 'data/subsidy-config.json', 'data/finance-config.json',
];
const sha = (p) => crypto.createHash('sha256').update(fs.readFileSync(path.join(ROOT, p))).digest('hex');
const sources = Object.fromEntries(SOURCES.map((p) => [p, sha(p)]));

const readData = (f, fallback = null) => {
  try { return JSON.parse(fs.readFileSync(path.join(ROOT, 'data', f), 'utf8')); } catch { return fallback; }
};
const req = createRequire(path.join(ROOT, 'package.json'));
const lib = (name) => import(pathToFileURL(path.join(ROOT, 'src/lib', name)).href);

const SO = await lib('salesOrchestrator.js');
const W = await lib('projectWorkspace.js');
const Q = await lib('quotationWorkspace.js');
const PROJ = await lib('packageProjection.js');
const QP = await lib('quotationPolicy.js');
const bb = req(path.join(ROOT, 'server-bom-builder.js'));

const packStore = readData('pack-config.json');
const approved = packStore.approved.config;
bb.setPackConfigProvider((source) => (source === 'draft' && packStore.draft ? packStore.draft.config : approved));

const catalog = readData('catalog.json', { categories: {} });
const costConfig = readData('cost-config.json', {});
const env = {
  catalog,
  catalogVersion: costConfig.sourceCatalogVersion || null,
  batteryMaster: (readData('battery-master.json', {}) || {}).batteries || {},
  packageRegistry: PROJ.visibleForRuntime(PROJ.hydrateRegistry(readData('packages.proposed.json', { packages: [] }))),
  costConfig,
  priceMaster: readData('procurement-price-master.json', {}),
  rateCard: readData('project-rate-card.json', null),
  historyStore: null,
  procurementPriceVersion: costConfig.procurement?.purchasePriceVersion ?? null,
  landedCostVersion: costConfig.procurement?.landedCostVersion ?? null,
};

const contentStore = readData('quotation-content.json');
// Testimonials: the masked fixture (homeowner names replaced) the Python side imports — same entries otherwise.
const TESTIMONIALS = JSON.parse(fs.readFileSync(path.resolve(HERE, '../fixtures/flarize/quotation-testimonials.json'), 'utf8'));
Q.setQuotationContentProvider(() => ({
  testimonials: TESTIMONIALS,
  document: contentStore?.published?.content ? { version: contentStore.published.version, content: contentStore.published.content } : null,
}));

const policy = readData('quotation-policy.json');
const ACTOR = { userId: 'admin-001', role: 'ADMIN' };

// The five representative quotations (every sellable on-grid size/phase, a future-ready pack, roof types, transport
// beyond the included km, both languages, every subsidy type, Sales appliance rows).
const CASES = [
  {
    id: 'ongrid-3-value-flat-residential',
    body: { systemType: 'ongrid', size: '3', tier: 'value', roofType: 'FLAT', distanceKm: 60, vehicleType: 'ACE', subsidyType: 'residential',
      quotationLanguage: 'en', customer: { name: 'Test Customer One', phone: '9000000001', address: 'Test Street 1', pincode: '688001', district: 'Alappuzha', currentBillAmount: 6000, currentBillCycle: 'bimonthly' } },
  },
  {
    id: 'ongrid-5sp-base-sheet-transport-ml',
    body: { systemType: 'ongrid', size: '5sp', tier: 'base', roofType: 'SHEET', distanceKm: 150, vehicleType: 'ACE', subsidyType: 'none',
      quotationLanguage: 'ml', customer: { name: 'Test Customer Two', phone: '9000000002', address: 'Test Street 2', pincode: '688002', currentBillAmount: 3000, currentBillCycle: 'monthly' } },
  },
  {
    id: 'ongrid-5tp-premium-elevated-3p',
    body: { systemType: 'ongrid', size: '5tp', tier: 'premium', phase: '3P', roofType: 'ELEVATED', distanceKm: 80, vehicleType: 'ACE', subsidyType: 'residential',
      quotationLanguage: 'en', customer: { name: 'Test Customer Three', phone: '9000000003', address: 'Test Street 3', pincode: '682001', currentBillAmount: 9000, currentBillCycle: 'bimonthly' } },
  },
  {
    id: 'ongrid-3-value-future-5sp',
    body: { systemType: 'ongrid', size: '3', tier: 'value', futureSystemSize: '5sp', roofType: 'FLAT', distanceKm: 100, vehicleType: 'ACE', subsidyType: 'residential',
      quotationLanguage: 'en', customer: { name: 'Test Customer Four', phone: '9000000004', address: 'Test Street 4', pincode: '688004', currentBillAmount: 4500, currentBillCycle: 'bimonthly' } },
  },
  {
    id: 'ongrid-5sp-value-ghs-appliances',
    body: { systemType: 'ongrid', size: '5sp', tier: 'value', roofType: 'FLAT', distanceKm: 20, vehicleType: 'ACE', subsidyType: 'ghs', ghsHouses: 4,
      quotationLanguage: 'en', applianceRows: [{ id: 'lights_fans', qty: 8, hours: 6 }, { id: 'refrigerator', qty: 1, hours: 24 }, { id: 'ac', qty: 2, hours: 4.5 }],
      customer: { name: 'Test Customer Five', phone: '9000000005', address: 'Test Street 5', pincode: '688005', currentBillAmount: 5200, currentBillCycle: 'monthly' } },
  },
];

function resolvePackMode() {
  return { config: approved, configVersion: packStore.approved.version, materialise: (params) => bb.buildBom(params) };
}

function approvedAlternativesResolver(body, actor) {
  return (tier) => bb.getAlternatives({
    systemType: body.systemType, size: body.size, tier, phase: body.phase || undefined, actorRole: actor.role,
    futureSystemSize: body.futureSystemSize || null,
    batteryQuantity: (body.batteryQuantity === 0 || body.batteryQuantity === 1 || body.batteryQuantity === 2) ? body.batteryQuantity : null,
  }).alternatives || {};
}

const plain = (value) => JSON.parse(JSON.stringify(value ?? null));
const cases = [];
for (const { id, body: raw } of CASES) {
  const workspaceStore = W.createWorkspaceStore();
  const quotationStore = Q.createQuotationStore();
  const body = { ...raw, at: AT, quotationDate: AT, quotationNumber: `GR-GOLDEN-${cases.length + 1}` };
  if (body.roofType && !body.installationType) body.installationType = String(body.roofType).toUpperCase();
  if (!body.validUntil) {
    try { body.validUntil = QP.resolveEffectiveValidity(policy, body.validityOverrideDays ?? null, body.quotationDate).validUntil; } catch { /* stays null */ }
  }
  const options = {
    at: AT, catalog, offers: catalog.offers || [], approvedAlternativesFor: approvedAlternativesResolver(body, ACTOR), cmsStore: null, companyProfile: null,
    tierDisplayNames: readData('tier-display-names.json'), inclusionMatrix: readData('quotation-inclusions.json'),
    energyConfig: readData('energy-config.json'), savingsConfig: readData('savings-config.json'), subsidyConfig: readData('subsidy-config.json'),
    financeConfig: readData('finance-config.json'), brandingStore: null, policy, productionMode: false, packMode: resolvePackMode(), rendererPinning: null,
  };
  const threeInput = { ...body, recommendedTier: body.tier, tierSelections: body.tierSelections || {} };
  const entry = { id, input: raw };
  try {
    const result = SO.orchestrateThreeTiers(workspaceStore, quotationStore, env, threeInput, ACTOR, { ...options, allowPartialAlternatives: true });
    const document = quotationStore.documents.get(result.quotationId)[0];
    const record = quotationStore.quotations.get(result.quotationId);
    const tiers = {};
    for (const t of result.tierResults) {
      tiers[t.tier] = {
        primary: t.primary,
        bomSnapshot: plain(quotationStore.bomSnapshots.get(t.bomSnapshotId)),
        commercialSnapshot: plain(quotationStore.commercialSnapshots.get(t.commercialSnapshotId)),
      };
    }
    Object.assign(entry, { result: plain(result), record: plain(record), document: plain(document), tiers });
  } catch (error) {
    entry.error = { name: error.name, code: error.code ?? null, message: error.message, detail: plain(error.detail) };
  }
  cases.push(entry);
}

const out = { schema: 'quotations.golden/1', generatedFrom: 'salesOrchestrator.orchestrateThreeTiers (pack mode)', at: AT, sources, cases };
fs.writeFileSync(OUT, `${JSON.stringify(out)}\n`);
console.log(`wrote ${cases.length} cases to ${OUT}: ${cases.map((c) => `${c.id}${c.error ? ` ERROR ${c.error.code}` : ''}`).join(', ')}`);
