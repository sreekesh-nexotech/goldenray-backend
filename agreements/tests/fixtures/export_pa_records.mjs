// Export fixtures from the real Purchase Agreement page (agreement-goldenray-main/index.html), read-only.
//
// The page keeps its records only in the browser (localStorage `flarize_agr`); there is no server copy to dump. This
// script runs the page's own JavaScript in a Node `vm` context (DOM, storage and network stubbed, clock frozen) and:
//
//   1. records what the page writes to `flarize_agr`: its built-in demo records (`seed()`, written when storage is
//      empty) followed by one record per scenario saved through the page's own `saveAgr()` from a filled form
//      (FORMS 1/2/3 field ids, option values from the page's own lists);
//   2. captures `buildDoc(type, data, lang)` for every saved record in English, Malayalam and Hindi — the golden the
//      platform templates are compared with (agreements/tests/test_document_parity.py).
//
// Personal data: the scenario customers are synthetic; the demo records' phone numbers are masked.
//
//   node agreements/tests/fixtures/export_pa_records.mjs [path/to/index.html]
//
// Writes agreements/tests/fixtures/pa/flarize_agr.json and agreements/tests/golden/builddoc.json.

import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = process.argv[2] || "/tmp/claude-0/-home-user/2d0c9eb0-ce20-5c8e-9860-d18f6ad69685/scratchpad/utils/agreement-goldenray-main/index.html";
const html = fs.readFileSync(source, "utf8");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
const script = scripts[scripts.length - 1];

let clock = Date.parse("2026-09-20T10:00:00.000Z");
const RealDate = Date;
class FrozenDate extends RealDate {
  constructor(...args) {
    if (args.length === 0) super(clock);
    else super(...args);
  }
  static now() {
    return clock;
  }
}

const storage = new Map();
const element = () => ({ style: {}, classList: { toggle() {}, add() {}, remove() {} }, textContent: "", innerHTML: "", value: "", focus() {} });
const context = {
  console,
  Date: FrozenDate,
  JSON,
  Math,
  String,
  Number,
  Object,
  Array,
  RegExp,
  parseInt,
  isNaN,
  setTimeout: () => 0,
  clearTimeout: () => {},
  btoa: (s) => Buffer.from(s).toString("base64"),
  fetch: () => Promise.reject(new Error("offline")),
  localStorage: {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: (k) => storage.delete(k),
  },
  document: { getElementById: () => element(), querySelectorAll: () => [], addEventListener() {} },
  window: {},
  confirm: () => true,
};
vm.createContext(context);
vm.runInContext(script, context);
// The page's navigation re-renders the DOM; nothing of it matters to the records.
vm.runInContext("setNav=function(){}; toast=function(){}; renderMain=function(){};", context);

const scenarios = [
  [1, {
    quoteno: "1024", name: "Anu Varghese", phone: "9000000101", address: "House 12, Test Nagar\nAlappuzha, Kerala 688001",
    kw: "5 KW", phase: "Single Phase", panel: "Waaree - Mono Perc Bifacial - DCR", panelcap: "545W – 580W", inverter: "Growatt",
    invtype: "String Inverter", battery: "None", struct: "GI (Galvanized Iron) Structure", extra: "NO", walkway: "YES", lader: "NO",
    variant: "Premium", origprice: "335000", extcost: "", discount: "20000", total: "315000", kseb: "7800", addoffer: "Free annual cleaning for 2 years",
  }],
  [1, {
    quoteno: "", name: "Biju Mathew", phone: "+91 90000 00102", address: "Test Road, Kochi 682001",
    kw: "5KW Plant with 6KW Inverter – Hybrid", phase: "3 Phase", panel: "Adani - Bifacial Topcon Glass to Sheet - DCR", panelcap: "",
    inverter: "DEYE", invtype: "Hybrid Inverter", battery: "5.0 kWh", struct: "Hot Dip Galvanized", extra: "YES", walkway: "NO", lader: "YES",
    variant: "Value", origprice: "450000", extcost: "19500", discount: "10000", total: "459500", kseb: "13600", addoffer: "Custom: two free service visits",
  }],
  [1, {
    quoteno: "2048", name: "Chitra Nair", phone: "", address: "Test Lane 3, Kottayam",
    kw: "3 KW", phase: "Single Phase", panel: "Vikram Solar - Mono Perc Bifacial - NDCR", panelcap: "530W – 560W", inverter: "Solis",
    invtype: "Micro Inverter", battery: "None", struct: "GP (Pre Galvanized Pipe) Structure", extra: "NO", walkway: "NO", lader: "NO",
    variant: "Base", origprice: "", extcost: "", discount: "", total: "198000", kseb: "", addoffer: "",
  }],
  [2, {
    name: "Deepa Kurian", panel: "Renewsys - TopCon Bifacial Glass to Glass - NDCR", inverter: "Sungrow", invtype: "Hybrid Inverter", battery: "10 kWh",
    kw: "8KW Hybrid Plant", phase: "3 Phase", variant: "Value", amt: "585000", extra: "YES", extdesc: "Raised GI structure for east-facing slope", extcost: "19500", kseb: "11240",
  }],
  [2, {
    name: "Eldho Joseph", panel: "Premier Energies - TopCon Bifacial - DCR", inverter: "ENPHASE", invtype: "Micro Inverter", battery: "None",
    kw: "10 KW", phase: "3 Phase", variant: "Premium", amt: "712000", extra: "NO", extdesc: "", extcost: "", kseb: "",
  }],
  [3, {
    name: "Fathima Rasheed", panel: "Waaree - TopCon - DCR", inverter: "DEYE", invtype: "String Inverter", battery: "None",
    kw: "6 KW", phase: "Single Phase", variant: "Base", total: "372000", extcost: "19500",
  }],
  [3, {
    name: "Gopika Menon", panel: "Saatvik Solar - Bifacial Topcon Glass to Glass - DCR", inverter: "Growatt", invtype: "Hybrid Inverter", battery: "3.5 kWh",
    kw: "3KW Hybrid Plant", phase: "Single Phase", variant: "Value", total: "285000", extcost: "24000",
  }],
];

// 1. what the page writes: the demo records it seeds into an empty store, then every saved form (newest first)
vm.runInContext("save();", context);
for (const [type, values] of scenarios) {
  clock += 3_600_000;
  context.__values = values;
  vm.runInContext(`pick(${type}); Object.assign(form, __values); saveAgr();`, context);
}
const records = JSON.parse(storage.get("flarize_agr"));
const demoIds = new Set(["a1", "a2", "a3", "a4", "a5"]);
let masked = 0;
for (const record of records) {
  if (demoIds.has(record.id) && record.data.phone) record.data.phone = `90000000${String(++masked).padStart(2, "0")}`;
}

// 2. buildDoc golden for every saved record in the three languages
const golden = {};
for (const record of records.filter((r) => !demoIds.has(r.id))) {
  golden[record.id] = {};
  for (const [lang, name] of [["en", "English"], ["ml", "Malayalam"], ["hi", "Hindi"]]) {
    context.__record = record;
    golden[record.id][lang] = vm.runInContext(`buildDoc(__record.type, __record.data, ${JSON.stringify(name)})`, context);
  }
}

// 3. the shared catalog as api/products.js serves it on GET (first read seeds it from SEED_DEFAULTS); ids renumbered
const productsSource = fs.readFileSync(path.join(path.dirname(source), "api", "products.js"), "utf8");
const store = new Map();
const fakeRedis = { get: async (k) => store.get(k) ?? null, set: async (k, v) => store.set(k, v) };
let serial = 0;
const productsModule = { exports: {} };
const productsContext = {
  module: productsModule,
  exports: productsModule.exports,
  require: (name) => (name === "@upstash/redis" ? { Redis: { fromEnv: () => fakeRedis } } : name === "crypto" ? { randomUUID: () => `item-${String(++serial).padStart(3, "0")}` } : null),
  process: { env: { ADMIN_USERNAME: "admin", ADMIN_PASSWORD: "x", CRS_USERNAME: "crs", CRS_PASSWORD: "y" } },
  Buffer,
  Date: FrozenDate,
};
vm.createContext(productsContext);
vm.runInContext(productsSource, productsContext);
let catalog = null;
await productsModule.exports(
  { method: "GET", headers: { authorization: "Basic " + Buffer.from("crs:y").toString("base64") } },
  { status: () => ({ json: (body) => (catalog = body) }) },
);

fs.mkdirSync(path.join(here, "pa"), { recursive: true });
fs.writeFileSync(path.join(here, "pa", "catalog.json"), JSON.stringify(catalog, null, 1) + "\n");
fs.mkdirSync(path.join(here, "..", "golden"), { recursive: true });
fs.writeFileSync(path.join(here, "pa", "flarize_agr.json"), JSON.stringify(records, null, 1) + "\n");
fs.writeFileSync(
  path.join(here, "..", "golden", "builddoc.json"),
  JSON.stringify({ schema: "agreements.builddoc-golden/1", source: "agreement-goldenray-main/index.html buildDoc()", documents: golden }, null, 1) + "\n",
);
console.log(`${records.length} records (${records.length - scenarios.length} demo), ${Object.keys(golden).length} documents x 3 languages`);
