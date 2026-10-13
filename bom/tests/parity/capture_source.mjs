#!/usr/bin/env node
/**
 * Capture the golden corpus for the BOM port by replaying Golden-Ray-BOM's own engines.
 *
 *     node bom/tests/parity/capture_source.mjs
 *     node bom/tests/parity/capture_source.mjs --out bom/tests/parity/source
 *
 * Run BY HAND, never in CI: the output is committed and reviewed, and re-running it is
 * how you deliberately move the target. `extract_source.mjs --check` is the CI-safe
 * guard that the slices still match App.jsx.
 *
 * Golden-Ray-BOM is the behavioural source of truth for the BOM calculator, and the
 * platform engine (bom/services/website_quote.py) has drifted from it. These cases are
 * what bom/tests/test_source_parity.py holds the platform to, so every number here is
 * produced by the source's own arithmetic in its own operation order — nothing is
 * recomputed, rounded or normalised on the way out.
 *
 * Each case is {id, engine, params, result}. `params` is the full argument object the
 * engine was called with, so a failing case is reproducible by hand without re-deriving
 * the matrix. The catalog is captured once, by sha256, rather than per case.
 *
 * The matrix is a deterministic product with coarse sampling where the full cross would
 * be wasteful (see `SAMPLE_EVERY`). It is deliberately weighted towards the paths the
 * port is most likely to break: GST and margin (every size x tier x battery), the
 * market-rate key space (DCR x future-upgrade), and the upgrade section toggles.
 */

import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

import * as E from "./source_engine.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const CATALOG_PATH = resolve(HERE, "../../../../bom/Golden-Ray-BOM/data/catalog.json");

const argv = process.argv.slice(2);
const outArg = argv.indexOf("--out");
const OUT_DIR = outArg === -1 ? resolve(HERE, "source") : resolve(process.cwd(), argv[outArg + 1]);

const catalogText = readFileSync(CATALOG_PATH, "utf8");
const catalog = JSON.parse(catalogText);
const catalogSha = createHash("sha256").update(catalogText).digest("hex");

// ── shared defaults ───────────────────────────────────────────────────────────────────
// The source's own initial state (App.jsx:650-778), so a case differs from the app's
// cold start only in the axes a matrix varies.

const NO_ADDONS = {
  structure: { enabled: false, type: "elevated" },
  extraDcCable: { enabled: false, itemId: "", qty: 0 },
  extraAcCable: { enabled: false, itemId: "", qty: 0 },
  armouredCable: { enabled: false, itemId: "", qty: 0 },
};
const ALL_FU_OPTS = { includeStructure: true, includeDB: true, includeCables: true, includeMeter: true };

const BOM_DEFAULTS = {
  catalog,
  sysType: "ongrid",
  size: "3",
  tier: "value",
  batConfig: "0",
  microBrand: "enphase",
  zeroExport: true,
  selections: {},
  qtyOverrides: {},
  priceOverrides: {},
  customItems: [],
  distKm: 100,
  costOvr: {},
  marginType: "percent",
  marginVal: 20,
  futureUpgrade: false,
  upgradeSize: "",
  fuOpts: ALL_FU_OPTS,
  addOns: NO_ADDONS,
  structGrade: "default",
  structGradeMode: "manual",
  moduleDcr: "dcr",
};

const UPGRADE_DEFAULTS = {
  catalog,
  sysType: "upgrade",
  upgradeFromKw: "3",
  upgradeToKw: "5",
  upgradePhase: "1P",
  upgradeTier: "value",
  upgradeSelections: {},
  upgradeQtyOverrides: {},
  upgradePriceOverrides: {},
  upgradeSections: { panels: true, structure: true, inverter: true, hybridInv: false, battery: false, wiring: true },
  distKm: 100,
  costOvr: {},
  marginType: "percent",
  marginVal: 20,
  upgradeCustomItems: [],
  upgradeAddOns: { structure: { enabled: false, type: "elevated" } },
  moduleDcr: "dcr",
};

// Template dimensions, read from the catalog rather than hardcoded, so the matrix
// follows the data if a size or tier is ever added.
const SIZES = {
  ongrid: Object.keys(catalog.bomTemplates.ongrid.sizes),
  hybrid: Object.keys(catalog.bomTemplates.hybrid.sizes),
};
const TIERS = ["base", "value", "premium"];
const BAT_CONFIGS = catalog.bomTemplates.hybrid.batteryConfigs ?? ["0", "1", "2"];
const STRUCTURES = ["elevated", "sheetRoof"];
const UPGRADE_PATHS = [
  // The four paths the source has hardcoded panel counts for (App.jsx:2113) …
  ["3", "5"], ["5", "8"], ["5", "10"], ["8", "10"],
  // … and four it must auto-scale, including a large jump and a non-adjacent pair.
  ["3", "6"], ["6", "10"], ["3", "10"], ["10", "20"],
];

/**
 * `catalog` is dropped from the recorded params — it is identical in every case and
 * pinned by sha256 in the manifest. Keeping it would make each file ~160KB larger per
 * case and bury the axis that actually varies.
 */
function recordable(params) {
  const { catalog: _catalog, bom: _bom, upgradeBom: _upgradeBom, ...rest } = params;
  return rest;
}

/**
 * Every BOM line carries `available`/`allAvailable` — the full catalog rows behind that
 * slot's dropdown, ~8KB per line. They are the UI's option list, not an output of the
 * pricing arithmetic, and the platform's quote response has no equivalent, so holding
 * the port to them would assert nothing while making each case ~40KB.
 *
 * What does matter is WHICH item the selection logic landed on and what the default
 * would have been, because that is what `priceDiffTotal` is computed from. So the arrays
 * collapse to their ids: enough to catch a selection change, no dropdown payload.
 */
function slimLine(line) {
  const slim = { ...line };
  for (const key of ["available", "allAvailable"]) {
    if (Array.isArray(line[key])) slim[`${key}Ids`] = line[key].map((item) => item.id);
    delete slim[key];
  }
  return slim;
}

/** Collapse the option arrays wherever an engine's result carries BOM lines. */
function slimResult(result) {
  if (!result || typeof result !== "object") return result;
  const out = { ...result };
  if (Array.isArray(out.lines)) out.lines = out.lines.map(slimLine);
  // stringConfig echoes the resolved panel/inverter rows; those are the specs under
  // test, so they stay whole.
  return out;
}

const corpora = new Map();
function emit(file, engine, id, params, result) {
  if (!corpora.has(file)) corpora.set(file, []);
  corpora.get(file).push({ id, engine, params: recordable(params), result: slimResult(result) });
}

/** Sampling stride for axes whose full cross product is not worth the file size. */
const SAMPLE_EVERY = 3;

// ── corpus_standard: the GST / margin / market-rate core ───────────────────────────────
// Every size x tier x battery combination, because Phase 1 rewrites the ladder that
// produces these numbers and a sampled version could hide a per-size rounding fault.
{
  let n = 0;
  for (const sysType of ["ongrid", "hybrid"]) {
    for (const size of SIZES[sysType]) {
      for (const tier of TIERS) {
        const bats = sysType === "hybrid" ? BAT_CONFIGS : ["0"];
        for (const batConfig of bats) {
          for (const moduleDcr of ["dcr", "ndcr"]) {
            const params = { ...BOM_DEFAULTS, sysType, size, tier, batConfig, moduleDcr };
            emit("corpus_standard", "computeBom", `std-${++n}`, params, E.computeBom(params));
          }
        }
      }
    }
  }

  // Commercial axes, varied against a fixed mid configuration so a difference is
  // attributable to the axis rather than to the system.
  const base = { ...BOM_DEFAULTS, sysType: "ongrid", size: "6", tier: "value" };
  for (const distKm of [0, 50, 100, 101, 250]) {
    const params = { ...base, distKm };
    emit("corpus_standard", "computeBom", `std-dist-${distKm}`, params, E.computeBom(params));
  }
  for (const [marginType, marginVal] of [["percent", 0], ["percent", 35], ["flat", 0], ["flat", 45000]]) {
    const params = { ...base, marginType, marginVal };
    emit("corpus_standard", "computeBom", `std-margin-${marginType}-${marginVal}`, params, E.computeBom(params));
  }
  for (const structGrade of ["default", "gp", "gi"]) {
    for (const structGradeMode of ["manual", "flat"]) {
      const params = { ...base, structGrade, structGradeMode };
      emit("corpus_standard", "computeBom", `std-grade-${structGrade}-${structGradeMode}`, params, E.computeBom(params));
    }
  }
  // Custom items are the ONLY writer of gst5/gst12/gst18 once the flat 9% model is on,
  // so all three buckets need a case each or the legacy fields go untested.
  for (const gst of [5, 12, 18]) {
    const params = { ...base, customItems: [{ id: 1, name: `Custom ${gst}%`, qty: 3, price: 1250, gst }] };
    emit("corpus_standard", "computeBom", `std-custom-gst${gst}`, params, E.computeBom(params));
  }

  // Add-ons: structure deltas, and each cable add-on against a real catalog item.
  for (const type of STRUCTURES) {
    const params = { ...base, addOns: { ...NO_ADDONS, structure: { enabled: true, type } } };
    emit("corpus_standard", "computeBom", `std-struct-${type}`, params, E.computeBom(params));
  }
  for (const key of ["extraDcCable", "extraAcCable", "armouredCable"]) {
    const catKey = { extraDcCable: "dc_cable", extraAcCable: "ac_cable", armouredCable: "armoured_cable" }[key];
    const itemId = catalog.categories[catKey]?.items?.[0]?.id;
    if (!itemId) continue;
    const params = { ...base, addOns: { ...NO_ADDONS, [key]: { enabled: true, itemId, qty: 30 } } };
    emit("corpus_standard", "computeBom", `std-addon-${key}`, params, E.computeBom(params));
  }
  // Every add-on at once, which is how addOnTotal's three summands interact.
  {
    const params = {
      ...base,
      distKm: 180,
      addOns: {
        structure: { enabled: true, type: "elevated" },
        extraDcCable: { enabled: true, itemId: catalog.categories.dc_cable.items[0].id, qty: 40 },
        extraAcCable: { enabled: true, itemId: catalog.categories.ac_cable.items[0].id, qty: 25 },
        armouredCable: { enabled: true, itemId: catalog.categories.armoured_cable.items[0].id, qty: 15 },
      },
    };
    emit("corpus_standard", "computeBom", "std-addon-all", params, E.computeBom(params));
  }
}

// ── corpus_standard_fu: future-upgrade (inverter oversizing) ──────────────────────────
// fuOpts is a 4-bit field and each bit moves a different category between panel-kW and
// inverter-kW sizing, so all 16 combinations are captured for one representative pair.
{
  const fuSizes = { ongrid: ["5sp", "5tp", "6", "8", "10"], hybrid: ["5", "8", "10"] };
  let n = 0;
  for (const sysType of ["ongrid", "hybrid"]) {
    for (const upgradeSize of fuSizes[sysType]) {
      for (const tier of TIERS) {
        const params = {
          ...BOM_DEFAULTS, sysType, size: "3", tier, futureUpgrade: true, upgradeSize,
        };
        emit("corpus_standard_fu", "computeBom", `fu-${++n}`, params, E.computeBom(params));
      }
    }
  }
  for (let bits = 0; bits < 16; bits++) {
    const fuOpts = {
      includeStructure: Boolean(bits & 1),
      includeDB: Boolean(bits & 2),
      includeCables: Boolean(bits & 4),
      includeMeter: Boolean(bits & 8),
    };
    const params = {
      ...BOM_DEFAULTS, sysType: "ongrid", size: "3", tier: "value",
      futureUpgrade: true, upgradeSize: "8", fuOpts,
    };
    emit("corpus_standard_fu", "computeBom", `fu-opts-${bits}`, params, E.computeBom(params));
  }
}

// ── corpus_micro: premium micro-inverter accessories ──────────────────────────────────
// Quantities here are panel-count arithmetic (floor(n/4), the capped hms1000, ceil(n/99)),
// so every size matters; hybrid premium is included because it force-selects Enphase
// regardless of microBrand.
{
  let n = 0;
  for (const sysType of ["ongrid", "hybrid"]) {
    for (const size of SIZES[sysType]) {
      for (const microBrand of ["enphase", "hoymiles"]) {
        for (const zeroExport of [true, false]) {
          const params = { ...BOM_DEFAULTS, sysType, size, tier: "premium", microBrand, zeroExport };
          emit("corpus_micro", "computeBom", `micro-${++n}`, params, E.computeBom(params));
        }
      }
    }
  }
  for (const batConfig of BAT_CONFIGS) {
    const params = { ...BOM_DEFAULTS, sysType: "hybrid", size: "5", tier: "premium", batConfig };
    emit("corpus_micro", "computeBom", `micro-hyb-bat${batConfig}`, params, E.computeBom(params));
  }
}

// ── corpus_upgrade ────────────────────────────────────────────────────────────────────
// The 6 section toggles are a 64-way field. Capturing all 64 for one path pins the
// gating, then every path x tier x phase is captured with the default sections.
{
  let n = 0;
  for (const [from, to] of UPGRADE_PATHS) {
    for (const upgradeTier of TIERS) {
      for (const upgradePhase of ["1P", "3P"]) {
        const params = { ...UPGRADE_DEFAULTS, upgradeFromKw: from, upgradeToKw: to, upgradeTier, upgradePhase };
        emit("corpus_upgrade", "computeUpgradeBom", `upg-${++n}`, params, E.computeUpgradeBom(params));
      }
    }
  }
  const SECTION_KEYS = ["panels", "structure", "inverter", "hybridInv", "battery", "wiring"];
  for (let bits = 0; bits < 64; bits++) {
    const upgradeSections = Object.fromEntries(SECTION_KEYS.map((k, i) => [k, Boolean(bits & (1 << i))]));
    const params = { ...UPGRADE_DEFAULTS, upgradeSections };
    emit("corpus_upgrade", "computeUpgradeBom", `upg-sect-${bits}`, params, E.computeUpgradeBom(params));
  }
  // Hybrid upgrade with battery: the slot-surgery path (splice inverter/dcdb/acdb,
  // inject pos 100-105) plus the <=5kW 1P forcing rule.
  for (const [from, to] of [["3", "5"], ["5", "8"], ["8", "10"]]) {
    for (const upgradePhase of ["1P", "3P"]) {
      const params = {
        ...UPGRADE_DEFAULTS, upgradeFromKw: from, upgradeToKw: to, upgradePhase,
        upgradeSections: { panels: true, structure: true, inverter: false, hybridInv: true, battery: true, wiring: true },
      };
      emit("corpus_upgrade", "computeUpgradeBom", `upg-hyb-${from}-${to}-${upgradePhase}`, params, E.computeUpgradeBom(params));
    }
  }
  for (const type of STRUCTURES) {
    const params = { ...UPGRADE_DEFAULTS, upgradeAddOns: { structure: { enabled: true, type } } };
    emit("corpus_upgrade", "computeUpgradeBom", `upg-struct-${type}`, params, E.computeUpgradeBom(params));
  }
  for (const upgradeCustomItems of [[{ id: 1, name: "Scaffolding", qty: 2, price: 3500, gst: 18 }]]) {
    const params = { ...UPGRADE_DEFAULTS, upgradeCustomItems };
    emit("corpus_upgrade", "computeUpgradeBom", "upg-custom", params, E.computeUpgradeBom(params));
  }
  for (const moduleDcr of ["dcr", "ndcr"]) {
    const params = { ...UPGRADE_DEFAULTS, moduleDcr };
    emit("corpus_upgrade", "computeUpgradeBom", `upg-dcr-${moduleDcr}`, params, E.computeUpgradeBom(params));
  }
}

// ── corpus_string: the MPPT engine ────────────────────────────────────────────────────
// stringConfig reads the SELECTED panel and inverter, so the axis that matters is the
// catalog pair. Driving it through computeBom (rather than fabricating specs) keeps the
// selection logic in the loop, which is where phase and tier filtering happens.
{
  let n = 0;
  for (const sysType of ["ongrid", "hybrid"]) {
    for (const size of SIZES[sysType]) {
      for (const tier of TIERS) {
        const bomParams = { ...BOM_DEFAULTS, sysType, size, tier };
        const bom = E.computeBom(bomParams);
        const params = { bom, catalog };
        emit("corpus_string", "computeStringConfig", `str-${++n}`, { ...bomParams, _via: "computeBom" }, E.computeStringConfig(params));
      }
    }
  }
  // Cross-tier panel selection, to reach panels the default tier would never pick.
  const panels = catalog.categories.panel.items;
  for (let i = 0; i < panels.length; i += SAMPLE_EVERY) {
    const panel = panels[i];
    const bomParams = {
      ...BOM_DEFAULTS, sysType: "ongrid", size: "6", tier: "value",
      moduleDcr: panel.dcr === true ? "dcr" : "ndcr",
      selections: { ongrid_value_6_1: panel.id },
    };
    const bom = E.computeBom(bomParams);
    emit("corpus_string", "computeStringConfig", `str-panel-${panel.id}`, { ...bomParams, _via: "computeBom" }, E.computeStringConfig({ bom, catalog }));
  }
  for (const [from, to] of UPGRADE_PATHS) {
    const upgParams = { ...UPGRADE_DEFAULTS, upgradeFromKw: from, upgradeToKw: to };
    const upgradeBom = E.computeUpgradeBom(upgParams);
    emit(
      "corpus_string", "computeUpgradeStringConfig", `str-upg-${from}-${to}`,
      { ...upgParams, _via: "computeUpgradeBom" },
      E.computeUpgradeStringConfig({ upgradeBom, catalog, upgradeFromKw: from }),
    );
  }
}

// ── corpus_bill: KSEB bill -> system size ─────────────────────────────────────────────
// The slab boundaries and the telescopic/non-telescopic switch are where this breaks,
// so the sweep is dense and the boundaries are hit exactly.
{
  for (const billFreq of ["monthly", "bimonthly"]) {
    for (const housePhase of ["1P", "3P"]) {
      for (let amt = 200; amt <= 40000; amt += 400) {
        const params = { billAmt: String(amt), billFreq, sysType: "ongrid", housePhase };
        emit("corpus_bill", "computeBillCalc", `bill-${billFreq}-${housePhase}-${amt}`, params, E.computeBillCalc(params));
      }
    }
  }
  // Degenerate and boundary inputs: empty, zero, non-numeric, and the slab edges.
  for (const billAmt of ["", "0", "abc", "1", "999999"]) {
    const params = { billAmt, billFreq: "bimonthly", sysType: "ongrid", housePhase: "1P" };
    emit("corpus_bill", "computeBillCalc", `bill-edge-${billAmt || "empty"}`, params, E.computeBillCalc(params));
  }
  for (const sysType of ["ongrid", "hybrid", "upgrade"]) {
    const params = { billAmt: "6000", billFreq: "bimonthly", sysType, housePhase: "1P" };
    emit("corpus_bill", "computeBillCalc", `bill-sys-${sysType}`, params, E.computeBillCalc(params));
  }
}

// ── corpus_discount: offers, subsidy, DCR gating ──────────────────────────────────────
// The catalog carries one flat ongrid offer, which cannot exercise the percent path or
// the tier filter. These synthetic offers do; they are inputs, not catalog edits.
{
  const OFFERS = [
    ...(catalog.offers ?? []),
    { id: "syn_pct_all", name: "10% everything", type: "percent", value: 10, appliesTo: "all", appliesToTier: "all", active: true },
    { id: "syn_pct_prem", name: "5% premium only", type: "percent", value: 5, appliesTo: "all", appliesToTier: "premium", active: true },
    { id: "syn_flat_hyb", name: "Flat 25k hybrid", type: "flat", value: 25000, appliesTo: "hybrid", appliesToTier: "all", active: true },
    { id: "syn_flat_upg", name: "Flat 8k upgrade", type: "flat", value: 8000, appliesTo: "upgrade", appliesToTier: "all", active: true },
  ];
  const offerIds = ["", ...OFFERS.map((o) => o.id)];

  const bomCache = new Map();
  const bomFor = (sysType, size, tier, moduleDcr) => {
    const key = [sysType, size, tier, moduleDcr].join("|");
    if (!bomCache.has(key)) {
      bomCache.set(key, E.computeBom({ ...BOM_DEFAULTS, sysType, size, tier, moduleDcr }));
    }
    return bomCache.get(key);
  };
  const upgradeBom = E.computeUpgradeBom(UPGRADE_DEFAULTS);

  let n = 0;
  for (const sysType of ["ongrid", "hybrid", "upgrade"]) {
    const sizes = sysType === "upgrade" ? ["5"] : SIZES[sysType];
    for (const size of sizes) {
      for (const tier of TIERS) {
        for (const subsidyType of ["residential", "ghs", "none"]) {
          for (const moduleDcr of ["dcr", "ndcr"]) {
            for (const selectedOffer of offerIds) {
              // Keep the file honest in size: sample the offer axis except for the
              // no-offer case, which every configuration needs.
              if (selectedOffer && n % SAMPLE_EVERY !== 0) { n++; continue; }
              const params = {
                sysType,
                bom: bomFor(sysType === "upgrade" ? "ongrid" : sysType, size, tier, moduleDcr),
                upgradeBom,
                offers: OFFERS,
                selectedOffer,
                size,
                tier,
                upgradeTier: tier,
                subsidyType,
                ghsHouses: 1,
                moduleDcr,
              };
              emit("corpus_discount", "computeDiscountCalc", `disc-${++n}`, params, E.computeDiscountCalc(params));
            }
          }
        }
      }
    }
  }
  // GHS scales with house count and caps at min(500, houses*3)*18000 -- hit both sides
  // of the cap and the exact crossover at 167 houses.
  for (const ghsHouses of [1, 2, 10, 166, 167, 168, 500, 1000]) {
    for (const size of ["3", "6", "10"]) {
      const params = {
        sysType: "ongrid",
        bom: bomFor("ongrid", size, "value", "dcr"),
        upgradeBom,
        offers: OFFERS,
        selectedOffer: "",
        size,
        tier: "value",
        upgradeTier: "value",
        subsidyType: "ghs",
        ghsHouses,
        moduleDcr: "dcr",
      };
      emit("corpus_discount", "computeDiscountCalc", `disc-ghs-${ghsHouses}-${size}`, params, E.computeDiscountCalc(params));
    }
  }
}

// ── write ─────────────────────────────────────────────────────────────────────────────

mkdirSync(OUT_DIR, { recursive: true });

const manifest = {
  captured_from: "bom/Golden-Ray-BOM/src/App.jsx",
  app_jsx_sha256: E.SOURCE_SHA256,
  catalog_path: "bom/Golden-Ray-BOM/data/catalog.json",
  catalog_sha256: catalogSha,
  node_version: process.version,
  corpora: {},
};

// Gzipped, like the corpus this replaces (bom/tests/golden/bom_calculate.json.gz):
// these files are ~8MB of highly repetitive JSON that compresses to ~220KB, and git
// stores no delta for a committed blob that is rewritten wholesale on every re-capture.
let total = 0;
let bytes = 0;
for (const [file, cases] of [...corpora].sort()) {
  const payload = gzipSync(Buffer.from(JSON.stringify({ cases }, null, 1), "utf8"), { level: 9 });
  writeFileSync(resolve(OUT_DIR, `${file}.json.gz`), payload);
  manifest.corpora[file] = cases.length;
  total += cases.length;
  bytes += payload.length;
  console.log(`  ${file}.json.gz`.padEnd(34) + `${String(cases.length).padStart(5)} cases  ${String(Math.round(payload.length / 1024)).padStart(5)} KB`);
}
manifest.total_cases = total;

writeFileSync(resolve(OUT_DIR, "manifest.json"), JSON.stringify(manifest, null, 2) + "\n", "utf8");
writeFileSync(resolve(OUT_DIR, "catalog.json.gz"), gzipSync(Buffer.from(catalogText, "utf8"), { level: 9 }));

console.log(`\n  ${total} cases, ${Math.round(bytes / 1024)} KB compressed -> ${OUT_DIR}`);
console.log(`  App.jsx  sha256 ${E.SOURCE_SHA256.slice(0, 12)}…`);
console.log(`  catalog  sha256 ${catalogSha.slice(0, 12)}…`);
