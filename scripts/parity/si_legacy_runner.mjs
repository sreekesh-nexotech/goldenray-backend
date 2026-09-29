// Runs the legacy Site Inspection V2 readiness engine (lib/site-inspection.ts) and equipment scorer
// (lib/equipment-assessment.ts) over the scenarios written by capture_si_legacy.py.
//
//   node si_legacy_runner.mjs <site-inspection-v2 root> <input.json> <output.json>
//
// The legacy module is copied to a temporary directory with its "./db" import pointed at an in-memory,
// read-only fake that serves one scenario's rows to the exact SELECTs getReadiness/getFieldCompletion issue.
// Needs Node >= 22.18 (TypeScript type stripping).

import { copyFileSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const [root, inputPath, outputPath] = process.argv.slice(2);
if (!root || !inputPath || !outputPath) {
  throw new Error("usage: node si_legacy_runner.mjs <site-inspection-v2 root> <input.json> <output.json>");
}

const FAKE_DB = `
let current = null;
export function setScenario(scenario) { current = scenario; }
export const newId = (prefix) => prefix + "_fake";
export const nowIso = () => "2026-09-01T00:00:00.000Z";
function byVersionDesc(rows) { return [...rows].sort((a, b) => b.version - a.version || String(b.created_at).localeCompare(String(a.created_at))); }
function answer(sql) {
  if (/FROM\\s+site_inspections\\b/.test(sql)) return { one: current.row };
  if (/FROM\\s+site_inspection_approvals\\b/.test(sql)) {
    const rows = sql.includes("status = 'APPROVED'") ? current.approvals.filter((a) => a.status === "APPROVED") : current.approvals;
    return { one: byVersionDesc(rows)[0] };
  }
  if (/FROM\\s+site_inspection_annotations\\b/.test(sql)) {
    const rows = [...current.annotations].sort((a, b) => a.annotation_type.localeCompare(b.annotation_type) || b.created_at.localeCompare(a.created_at));
    return { many: rows };
  }
  if (/FROM\\s+site_inspection_equipment_assessments\\b/.test(sql)) return { many: current.equipment };
  throw new Error("unexpected query: " + sql);
}
export const db = {
  prepare(sql) {
    return {
      get() { return answer(sql).one; },
      all() { return answer(sql).many; },
      run() { throw new Error("the parity run is read-only"); },
    };
  },
};
`;

const work = mkdtempSync(join(tmpdir(), "si-legacy-"));
writeFileSync(join(work, "package.json"), '{"type": "module"}');
writeFileSync(join(work, "fake-db.mjs"), FAKE_DB);
const original = readFileSync(join(root, "lib", "site-inspection.ts"), "utf8");
const patched = original.replace('import { db, newId, nowIso } from "./db";', 'import { db, newId, nowIso } from "./fake-db.mjs";');
if (patched === original) throw new Error("the legacy module no longer imports ./db as expected");
writeFileSync(join(work, "site-inspection.ts"), patched);
copyFileSync(join(root, "lib", "equipment-assessment.ts"), join(work, "equipment-assessment.ts"));

const fake = await import(pathToFileURL(join(work, "fake-db.mjs")).href);
const si = await import(pathToFileURL(join(work, "site-inspection.ts")).href);
const ea = await import(pathToFileURL(join(work, "equipment-assessment.ts")).href);

const input = JSON.parse(readFileSync(inputPath, "utf8"));
const output = { checklists: ea.EQUIPMENT_ASSESSMENTS, statuses: {}, readiness: {}, completion: {} };

for (const [id, results] of Object.entries(input.result_sets)) {
  output.statuses[id] = ea.calculateAssessmentStatus(results);
}
for (const [id, scenario] of Object.entries(input.scenarios)) {
  const equipment = scenario.equipment.map((row) => ({ status: ea.calculateAssessmentStatus(JSON.parse(row.results_json)), ...row }));
  fake.setScenario({ ...scenario, equipment });
  const readiness = si.getReadiness(scenario.row.id);
  const completion = si.getFieldCompletion(scenario.row.id);
  output.readiness[id] = { ready: readiness.ready, blockers: readiness.blockers };
  output.completion[id] = { ready: completion.ready, blockers: completion.blockers };
}
writeFileSync(outputPath, JSON.stringify(output));
