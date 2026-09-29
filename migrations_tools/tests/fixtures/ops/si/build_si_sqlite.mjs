#!/usr/bin/env node
// Build the Site Inspection V2 SQLite fixture of migration-ops (`flarize-site-inspection.db`) with the SI app's OWN
// schema and seed — no production SQLite file exists in the reference estate.
//
//   1. the app's `lib/db.ts` runs unchanged: it opens `$FLARIZE_DATA_DIR/flarize-site-inspection.db`, creates every table
//      (`initDb()`) and applies every `ensureColumn` migration, exactly as the app does on start-up;
//   2. the app's `scripts/seed.ts` (`npm run db:seed`) runs unchanged: engineer ENG-001 and its login `engineer`;
//   3. the representative rows of the site-inspections package (`site_inspections/tests/fixtures/legacy_si.json`: every
//      legacy encoding — "No" flags, PARTIAL cabling, container rectangles, base64 photos, keys-only equipment results;
//      all personal values invented) are inserted through the same connection. The fixture's ENG-001 and its login are
//      the seeded ones (the seed's ids replace the fixture's), and the PA inspection references the Purchase Agreement
//      record `agr_1789902000000` of `agreements/tests/fixtures/pa/flarize_agr.json` (customer phone 9000000101 in both),
//      so `import_pa` → `import_si` links it and matches the customer across sources.
//
// better-sqlite3 (a native module) is not installed in the reference tree; `better-sqlite3` is shimmed over Node's built-in
// `node:sqlite` (same SQLite engine, the three calls db.ts/seed.ts use: exec, pragma, prepare().get/all/run). TypeScript
// is transpiled with the global `typescript` package. Clock, UUIDs and salts are frozen so the file is reproducible.
//
//   node migrations_tools/tests/fixtures/ops/si/build_si_sqlite.mjs [SI_ROOT] [OUT_DB]
//
// Defaults: SI_ROOT = the reference copy of site-inspection-v2; OUT_DB = flarize-site-inspection.db next to this script.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "../../../../..");
const SI_ROOT = path.resolve(process.argv[2] || "/tmp/claude-0/-home-user/2d0c9eb0-ce20-5c8e-9860-d18f6ad69685/scratchpad/utils/site-inspection-v2");
const OUT = path.resolve(process.argv[3] || path.join(here, "flarize-site-inspection.db"));
const ROWS = path.join(repo, "site_inspections/tests/fixtures/legacy_si.json");
const PA_RECORD = "agr_1789902000000";
const require = createRequire(import.meta.url);
const ts = require("/opt/node22/lib/node_modules/typescript");

// ── frozen clock, UUIDs and salts ────────────────────────────────────────────────────────────────────────────────
const NOW = Date.parse("2026-06-01T03:30:00.000Z");
const RealDate = Date;
globalThis.Date = class extends RealDate {
  constructor(...args) {
    if (args.length === 0) super(NOW);
    else super(...args);
  }
  static now() {
    return NOW;
  }
};
let counter = 0;
const nodeCrypto = require("node:crypto");
const uuid = () => `00000000-0000-4000-8000-${String(++counter).padStart(12, "0")}`;
globalThis.crypto.randomUUID = uuid;
nodeCrypto.randomUUID = uuid;
nodeCrypto.randomBytes = (n) => Buffer.alloc(n, 7);

// ── transpile the app's modules into a scratch tree with the better-sqlite3 shim ─────────────────────────────────
const work = fs.mkdtempSync(path.join(os.tmpdir(), "si-fixture-"));
const dataDir = path.join(work, "data");
for (const file of ["lib/db.ts", "lib/auth.ts", "scripts/seed.ts"]) {
  const source = fs.readFileSync(path.join(SI_ROOT, file), "utf8");
  const out = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } });
  const target = path.join(work, file.replace(/\.ts$/, ".js"));
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, out.outputText);
}
fs.mkdirSync(path.join(work, "node_modules/better-sqlite3"), { recursive: true });
fs.writeFileSync(
  path.join(work, "node_modules/better-sqlite3/index.js"),
  `const { DatabaseSync } = require("node:sqlite");
class Database {
  constructor(file) { this.db = new DatabaseSync(file); }
  pragma(text) { return this.db.prepare("PRAGMA " + text).all(); }
  exec(sql) { this.db.exec(sql); return this; }
  prepare(sql) { return this.db.prepare(sql); }
  close() { this.db.close(); }
}
module.exports = Database;
module.exports.default = Database;
`,
);
process.env.FLARIZE_DATA_DIR = dataDir;
process.env.NODE_ENV = "production";
const load = createRequire(path.join(work, "scripts/seed.js"));
load("./seed.js"); // lib/db.ts (schema + ensureColumn) then scripts/seed.ts
const { db } = load("../lib/db.js");

// ── the representative rows ─────────────────────────────────────────────────────────────────────────────────────
const tables = JSON.parse(fs.readFileSync(ROWS, "utf8"));
const seeded = db.prepare("SELECT id, engineer_code FROM engineers").all();
const engineerIds = {};
for (const row of tables.engineers) {
  const existing = seeded.find((item) => item.engineer_code === row.engineer_code);
  if (existing) engineerIds[row.id] = existing.id;
}
const remap = (value) => (value in engineerIds ? engineerIds[value] : value);
for (const row of tables.site_inspections) {
  row.engineer_id = remap(row.engineer_id);
  if (row.purchase_agreement_id) row.purchase_agreement_id = PA_RECORD;
}
for (const row of tables.customers) {
  if (row.id === "cust_a1") Object.assign(row, { name: "Anu Varghese", phone: "9000000101", customer_code: `PA-${PA_RECORD}` });
}
const approvals = tables.site_inspection_approvals;
for (const row of approvals) if (row.customer_phone === "9847000101") row.customer_phone = "9000000101";

const ORDER = [
  "customers",
  "engineers",
  "engineer_users",
  "site_inspections",
  "site_inspection_photos",
  "site_inspection_annotations",
  "site_inspection_equipment_assessments",
  "site_inspection_approvals",
  "site_inspection_observations",
];
const counts = {};
for (const table of ORDER) {
  let rows = tables[table] || [];
  if (table === "engineers") rows = rows.filter((row) => !(row.id in engineerIds));
  if (table === "engineer_users") rows = rows.filter((row) => !(row.engineer_id in engineerIds));
  const columns = new Set(db.prepare(`PRAGMA table_info(${table})`).all().map((column) => column.name));
  for (const row of rows) {
    const keys = Object.keys(row).filter((key) => columns.has(key));
    const values = keys.map((key) => (typeof row[key] === "boolean" ? Number(row[key]) : row[key] === undefined ? null : row[key]));
    db.prepare(`INSERT INTO ${table} (${keys.join(", ")}) VALUES (${keys.map(() => "?").join(", ")})`).run(...values);
  }
  counts[table] = db.prepare(`SELECT count(*) AS n FROM ${table}`).get().n;
}
db.pragma("wal_checkpoint(TRUNCATE)");
db.pragma("journal_mode = DELETE");
db.close();
fs.copyFileSync(path.join(dataDir, "flarize-site-inspection.db"), OUT);
fs.rmSync(work, { recursive: true, force: true });
console.log(JSON.stringify({ out: path.relative(repo, OUT), counts }, null, 1));
