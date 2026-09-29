#!/usr/bin/env node
// Golden file of the Flarize pack release: every pack of the APPROVED pack configuration, built, priced and checked by
// the REAL Flarize JavaScript over the real data (read-only, FLARIZE_ROOT/data).
//
// For every (systemType × size × tier × {standard, future-ready pairs of the same phase} × battery configuration)
// — the enumeration of server-pack-publish.js, with the hybrid battery configurations of the template — it records:
//   bom       buildBom({…, configSource:'approved', actorRole:'PROJECT_HEAD', batteryQuantity}) lines + totals
//   pricing   pricePack({config: approved, roofType:'FLAT', distanceKm: 0, lines}) (the pack's base price)
//   checker   runPackageChecker on the package publishApprovedPacks derives from that BOM (componentsFromBom)
//
// The Python side (packs/tests/test_release_parity.py) imports the same data through the platform importers
// (catalog, pricing, bom, packs), publishes PriceRelease #1 and PackRelease #1 through the services and compares every
// pack with this file. The input data the Python side uses is engines/tests/golden/fixtures/flarize_commercial.json
// (catalog, pack store, registry reduced to the records buildBom reads, battery master); `sources` records the SHA-256
// of every JavaScript and data file, and the generator refuses to run when the data differs from that fixture's.
//
// Usage: TZ=UTC node packs/tests/golden/generate_packs.mjs [FLARIZE_ROOT]   (writes packs/tests/golden/flarize_packs.json)

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(process.argv[2] || process.env.FLARIZE_ROOT || '/home/user/flarize-main/flarize');
const OUT = process.env.GOLDEN_OUT || path.join(HERE, 'flarize_packs.json');
const FIXTURE = path.resolve(HERE, '../../../engines/tests/golden/fixtures/flarize_commercial.json');
const AT = '2026-09-20T10:00:00.000Z';

const SOURCES = [
  'server-bom-builder.js', 'server-pack-publish.js', 'src/lib/packPricing.js', 'src/lib/packConfig.js', 'src/lib/packageApproval.js',
  'src/lib/engineeringChecker.js', 'src/lib/deviceAllocation.js', 'src/lib/money.js',
  'data/catalog.json', 'data/pack-config.json', 'data/packages.proposed.json', 'data/battery-master.json',
];
const sha = (p) => crypto.createHash('sha256').update(fs.readFileSync(path.join(ROOT, p))).digest('hex');
const sources = Object.fromEntries(SOURCES.map((p) => [p, sha(p)]));
const fixture = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
for (const file of ['data/catalog.json', 'data/pack-config.json', 'data/packages.proposed.json', 'data/battery-master.json']) {
  if (fixture.sources[file] !== sources[file]) throw new Error(`${file} differs from the data engines/tests/golden/fixtures/flarize_commercial.json was built from`);
}

const readData = (f) => JSON.parse(fs.readFileSync(path.join(ROOT, 'data', f), 'utf8'));
const packStore = readData('pack-config.json');
const catalog = readData('catalog.json');
const batteryMaster = readData('battery-master.json').batteries || {};
const req = createRequire(path.join(ROOT, 'package.json'));
const lib = (name) => pathToFileURL(path.join(ROOT, 'src/lib', name)).href;
const bb = req(path.join(ROOT, 'server-bom-builder.js'));
const publish = req(path.join(ROOT, 'server-pack-publish.js'));
const PP = await import(lib('packPricing.js'));
const PA = await import(lib('packageApproval.js'));

const approved = packStore.approved.config;
bb.setPackConfigProvider(() => approved);
const catalogForChecker = bb.loadCatalog('approved');
const ADMIN = { userId: 'golden', role: 'ADMIN' };

const cases = [];
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
    const batteries = systemType === 'hybrid' ? (t.batteryConfigs || ['0', '1', '2']).map(Number) : [null];
    for (const tier of t.tiers || ['base', 'value', 'premium']) {
      for (const fr of variants) {
        for (const bat of batteries) {
          const input = { systemType, size, tier, phase, futureSystemSize: fr, batteryQuantity: bat };
          const entry = { input };
          try {
            const config = { systemType, size, tier, phase, configSource: 'approved', actorRole: 'PROJECT_HEAD' };
            if (fr) config.futureSystemSize = fr;
            if (bat !== null) config.batteryQuantity = bat;
            const bom = bb.buildBom(config);
            entry.bom = {
              lines: bom.lines.map((l) => ({ category: l.category, componentId: l.componentId ?? null, name: l.name, qty: l.qty, unitPrice: l.unitPrice, amount: l.amount, gst: l.gst, gstAmt: l.gstAmt, selectionMethod: l.selectionMethod, isVariable: l.isVariable })),
              totals: bom.totals,
              systemConfig: bom.systemConfig,
              engineering: bom.engineering,
            };
            entry.pricing = PP.pricePack({ config: approved, systemType, size, tier, roofType: 'FLAT', distanceKm: 0, batteryConfig: bom.systemConfig.batteryQuantity, futureSystemSize: fr, lines: bom.lines, pricedAt: AT });
            const components = publish.componentsFromBom(bom);
            const pkg = { packageId: 'GOLDEN', systemType, size, phase, tier, profileKey: bom.systemConfig.profileKey, architecture: null, components, revisionNumber: 1 };
            const validation = PA.runPackageChecker({ packages: [pkg] }, { catalog: catalogForChecker, batteryMaster, catalogVersion: null }, { actor: ADMIN, packageId: 'GOLDEN', at: AT });
            entry.checker = {
              status: validation.status,
              architecture: validation.architecture,
              findings: validation.findings.map((f) => ({ ruleId: f.ruleId, severity: f.severity, componentIds: f.componentIds })),
              deterministicKey: validation.deterministicKey,
            };
          } catch (e) {
            entry.error = { name: e.name, message: e.message, code: e.code ?? null };
          }
          cases.push(entry);
        }
      }
    }
  }
}

const doc = { generator: 'packs/tests/golden/generate_packs.mjs', at: AT, configVersion: packStore.approved.version, sources, cases };
fs.writeFileSync(OUT, JSON.stringify(doc));
console.log(`${cases.length} packs → ${OUT}`);
