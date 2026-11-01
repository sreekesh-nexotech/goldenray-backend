#!/usr/bin/env node
/**
 * Lift the Golden-Ray-BOM calculation engines out of `src/App.jsx` into a plain,
 * callable ES module — `source_engine.mjs`, which this writes next to itself.
 *
 * Why slice rather than drive the SPA: every engine below is a `useMemo` whose body
 * reads only `catalog` plus plain state scalars. None touches the DOM, a ref, or a
 * React API, and the two structure helpers are already module-level pure functions.
 * So the bodies run unchanged in bare Node once their closure variables are supplied
 * as an argument — no React, no jsdom, no browser.
 *
 * The slice is anchored on each memo's own dependency array (`}, [a, b, c]);`), which
 * React requires to list exactly the closure variables the body reads. That array is
 * therefore a contract the source maintains for us: we parse it and turn it into the
 * generated function's parameter list. If a dependency is ever added or removed
 * upstream, the generated signature changes with it instead of silently drifting.
 *
 * Run from anywhere:
 *     node bom/tests/parity/extract_source.mjs
 *     node bom/tests/parity/extract_source.mjs --check     # verify, write nothing
 *
 * `--check` re-extracts and compares against the committed `source_engine.mjs`,
 * exiting non-zero on any difference. That is the CI-safe form: it catches App.jsx
 * moving under us without needing the source repo to be writable.
 *
 * The generated file is COMMITTED so a capture run is reproducible from this repo
 * alone, and so a reviewer can read exactly what was replayed.
 */

import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP_JSX = resolve(HERE, "../../../../bom/Golden-Ray-BOM/src/App.jsx");
const OUT = resolve(HERE, "source_engine.mjs");
// Import specifiers for the generated module, which sits in HERE — so they are the same
// relative depth as APP_JSX above and resolve into the sibling source checkout.
const LIB = "../../../../bom/Golden-Ray-BOM/src/lib";
const DATA = "../../../../bom/Golden-Ray-BOM/data";

/**
 * Each engine is located by the text that opens it and the first dependency array
 * that closes it, never by a hardcoded line number — line numbers rot the moment
 * anyone edits App.jsx, and a silently mis-sliced engine would poison every golden
 * case downstream.
 *
 * The structure helpers (`getStructureQty`, `calcStructureCost`) are deliberately NOT
 * parameters. They are module-level functions in App.jsx, they are copied into the
 * generated module verbatim, and the engine bodies call them by name — so they resolve
 * lexically, exactly as they do in the source. Making them parameters would shadow the
 * real definitions with whatever a caller passed, which is precisely the kind of
 * substitution this harness exists to prevent.
 */
const ENGINES = [
  // `version-2` computes all three tiers at once and the UI indexes into the result
  // (`const bom = allTierBoms[tier]`, App.jsx:2367). The memo keeps the same shape, so the
  // slicer is unchanged -- but the generated function returns `{base, value, premium}`
  // rather than one tier's BOM, and `tier` is no longer in its dependency array.
  // `capture_source.mjs` selects the tier; see `computeBomForTier` there.
  { name: "computeAllTierBoms", open: "  const allTierBoms = useMemo(() => {" },
  { name: "computeBillCalc", open: "  const billCalc = useMemo(() => {" },
  { name: "computeStringConfig", open: "  const stringConfig = useMemo(() => {" },
  // `upgradeBom`'s body reads `panelTypeFilter` and `subsidyType` (App.jsx:2736-2739) but
  // its dependency array (App.jsx:3063) declares neither -- an upstream stale-memo bug:
  // changing the panel filter does not recompute the upgrade BOM until something else
  // does. It also reads `size` and `roofType` for its installation-matrix lookup
  // (App.jsx:2970) -- and `size` there is the *base* system size, not the upgrade delta,
  // so an upgrade's installation cost is looked up against the wrong kW. Both are
  // upstream defects. In React they still resolve from the component closure, so they are passed here
  // as parameters rather than left undefined, which would be a crash the source does not
  // have. Replicated and flagged, not fixed; `undeclaredDeps` keeps it visible.
  { name: "computeUpgradeBom", open: "  const upgradeBom = useMemo(() => {", undeclaredDeps: ["panelTypeFilter", "subsidyType", "size", "roofType"] },
  { name: "computeUpgradeStringConfig", open: "  const upgradeStringConfig = useMemo(() => {" },
  { name: "computeDiscountCalc", open: "  const discountCalc = useMemo(() => {" },
];

/**
 * Pure declarations the engine bodies reference by name, copied across verbatim so they
 * resolve lexically in the generated module exactly as they do in App.jsx.
 *
 * `getStructureQty`/`calcStructureCost` are module-level functions (App.jsx:28-66).
 * `upgSectionMap` is a component-scope constant (App.jsx:1802) that only
 * `computeUpgradeBom` reads; it is a frozen lookup table, not state, so hoisting it to
 * module scope is behaviour-preserving.
 *
 * `closer` is the line that ends the declaration, at the same indentation it opened on.
 */
/**
 * Modules the engine bodies now call into, re-imported rather than copied.
 *
 * `version-2` moved real logic out of App.jsx and into `src/lib/` — battery resolution,
 * engineering validation, the subsidy slabs. Copying those bodies here would fork them: the
 * corpus would be generated against this file's snapshot rather than against the source the
 * app actually runs. Importing from the source tree keeps one definition, and the path is
 * relative to the generated module's own location so it resolves the same way App.jsx's does.
 *
 * `batteryMasterFile` is a JSON import; Node needs the import attribute for it.
 */
const IMPORTS = [
  `import { resolveBattery } from "${LIB}/resolveBattery.js";`,
  `import { toBatteryMaster, isPermanentlyUnselectable } from "${LIB}/batteryMaster.js";`,
  `import { validateEngineering } from "${LIB}/engineeringValidation.js";`,
  `import { statusFromValidation, quotationGate } from "${LIB}/bomStatus.js";`,
  `import { calculateSubsidy } from "${LIB}/subsidyEngine.js";`,
  `import batteryMasterFile from "${DATA}/battery-master.json" with { type: "json" };`,
  `import subsidyConfigData from "${DATA}/subsidy-config.json" with { type: "json" };`,
  // App.jsx:22, verbatim. A one-line derivation of the JSON import rather than a
  // declaration block, so it belongs here rather than in HELPERS.
  "const BATTERY_MASTER = batteryMasterFile?.batteries || {};",
];

/** Names `IMPORTS` brings into scope, so the free-variable check accepts them. */
const IMPORTED_NAMES = [
  "resolveBattery",
  "toBatteryMaster",
  "isPermanentlyUnselectable",
  "validateEngineering",
  "statusFromValidation",
  "quotationGate",
  "calculateSubsidy",
  "batteryMasterFile",
  "BATTERY_MASTER",
  "subsidyConfigData",
];

const HELPERS = [
  { name: "getStructureQty", open: "function getStructureQty(item, kw) {", closer: "}" },
  // version-2: the package profile now decides battery quantity and structure grade
  // (decision C4). All four are module-level in App.jsx, so they copy across verbatim.
  // version-2 records WHY each item was chosen (App.jsx:55). A frozen label table.
  { name: "RESOLUTION", open: "const RESOLUTION = {", closer: "};" },
  { name: "getProfileKey", open: "function getProfileKey(sysType, tier) {", closer: "}" },
  { name: "getPackageProfile", open: "function getPackageProfile(catalog, profileKey) {", closer: "}" },
  { name: "getProfileStructure", open: "function getProfileStructure(catalog, profileKey) {", closer: "}" },
  { name: "calcStructureCost", open: "function calcStructureCost(template, kw, ratePerKg) {", closer: "}" },
  { name: "upgSectionMap", open: "  const upgSectionMap = {", closer: "  };", dedent: 2 },
];

const source = readFileSync(APP_JSX, "utf8");
const lines = source.split("\n");

/** The 0-based index of the single line equal to `needle` (after trailing-space trim). */
function findLine(needle, what) {
  const hits = [];
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].trimEnd() === needle) hits.push(i);
  }
  if (hits.length !== 1) {
    throw new Error(
      `${what}: expected exactly one line matching\n  ${needle}\nbut found ${hits.length}` +
        (hits.length ? ` (lines ${hits.map((h) => h + 1).join(", ")})` : "") +
        `\nApp.jsx has changed shape — update ENGINES/HELPERS in extract_source.mjs.`,
    );
  }
  return hits[0];
}

/**
 * Slice a `useMemo` body and read its dependency array.
 *
 * Returns `{ params, body }`, where `params` comes from the dependency array and
 * `body` is every line strictly between the opening line and the closing `}, [...]);`.
 */
function sliceMemo({ name, open }) {
  const start = findLine(open, name);
  // `}, [` at exactly two spaces of indentation closes a memo in this file; a nested
  // array inside the body is always more deeply indented, so this cannot run long.
  let end = -1;
  for (let i = start + 1; i < lines.length; i++) {
    if (/^ {2}\}, \[/.test(lines[i])) {
      end = i;
      break;
    }
  }
  if (end === -1) throw new Error(`${name}: no dependency array found after line ${start + 1}`);

  const depsText = lines
    .slice(end)
    .join("\n")
    .match(/^ {2}\}, \[([\s\S]*?)\]\);/);
  if (!depsText) throw new Error(`${name}: could not parse the dependency array at line ${end + 1}`);

  const deps = depsText[1]
    .split(",")
    .map((d) => d.trim())
    .filter(Boolean);

  // React lists every closure variable the body reads, so the dependency array IS the
  // parameter list — nothing is added to it. Module-level helpers resolve lexically.
  const body = lines.slice(start + 1, end);
  return { params: deps, body, startLine: start + 1, endLine: end + 1 };
}

/** Slice a declaration from its opening line through the first matching `closer`. */
function sliceDeclaration({ name, open, closer, dedent = 0 }) {
  const start = findLine(open, name);
  let end = -1;
  for (let i = start + 1; i < lines.length; i++) {
    if (lines[i].trimEnd() === closer) {
      end = i;
      break;
    }
  }
  if (end === -1) throw new Error(`${name}: no closing \`${closer}\` found after line ${start + 1}`);
  let body = lines.slice(start, end + 1);
  if (dedent) body = body.map((l) => (l.startsWith(" ".repeat(dedent)) ? l.slice(dedent) : l));
  return { body, startLine: start + 1, endLine: end + 1 };
}

const helpers = HELPERS.map((h) => ({ ...h, ...sliceDeclaration(h) }));
const engines = ENGINES.map((e) => ({ ...e, ...sliceMemo(e) }));

/**
 * Fail at extraction time if an engine body reads a bare identifier that is neither one
 * of its parameters, one of the copied helpers, a JS global, nor something it declares
 * itself. Without this check a missing binding surfaces as a ReferenceError thousands of
 * cases into a capture run — or worse, as an engine that silently returns an empty BOM
 * because the error was swallowed by an early return.
 *
 * This is a deliberately rough lexical scan, not a parser: it over-collects candidates
 * and then subtracts everything locally bound. It only has to be good enough to catch a
 * genuinely free variable, which is what bit us with `upgSectionMap`.
 */
const JS_GLOBALS = new Set([
  "Math", "Number", "String", "Boolean", "Object", "Array", "JSON", "Date", "Set", "Map",
  "parseFloat", "parseInt", "isNaN", "isFinite", "console", "undefined", "null", "true",
  "false", "NaN", "Infinity", "RegExp", "Error",
]);
const RESERVED = new Set([
  "const", "let", "var", "function", "return", "if", "else", "for", "of", "in", "while",
  "break", "continue", "new", "typeof", "instanceof", "delete", "void", "do", "switch",
  "case", "default", "try", "catch", "finally", "throw", "this", "class", "extends",
  "super", "yield", "await", "async",
]);

const helperNames = new Set(helpers.map((h) => h.name));
const freeVariableProblems = [];

for (const engine of engines) {
  const text = engine.body.join("\n");
  // Strip strings, template literals, regexes and comments so their contents cannot
  // masquerade as identifiers.
  const stripped = text
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/\/\/[^\n]*/g, " ")
    .replace(/'(?:[^'\\\n]|\\.)*'/g, "''")
    .replace(/"(?:[^"\\\n]|\\.)*"/g, '""')
    .replace(/`(?:[^`\\]|\\.)*`/g, "``")
    .replace(/\/(?![/*])(?:[^/\\\n[]|\\.|\[(?:[^\]\\]|\\.)*\])+\/[gimsuy]*/g, "/RE/");

  // Everything the body binds itself: declarations, destructuring, params, labels.
  const bound = new Set();
  for (const m of stripped.matchAll(/\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)/g)) bound.add(m[1]);
  for (const m of stripped.matchAll(/\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)/g)) bound.add(m[1]);
  // Destructuring patterns and arrow/function parameter lists.
  for (const m of stripped.matchAll(/(?:const|let|var)\s*[[{]([^)\]}]*)[\]}]/g)) {
    for (const part of m[1].split(",")) {
      const id = part.split(":").pop().split("=")[0].replace(/[.\s]/g, "");
      if (/^[A-Za-z_$][\w$]*$/.test(id)) bound.add(id);
    }
  }
  for (const m of stripped.matchAll(/\(([^()]*)\)\s*=>/g)) {
    for (const part of m[1].split(",")) {
      const id = part.trim().split(/[:=\s]/)[0].replace(/[[\]{}.]/g, "");
      if (/^[A-Za-z_$][\w$]*$/.test(id)) bound.add(id);
    }
  }
  for (const m of stripped.matchAll(/(?:^|[(,\s])([A-Za-z_$][\w$]*)\s*=>/gm)) bound.add(m[1]);
  for (const m of stripped.matchAll(/\bcatch\s*\(\s*([A-Za-z_$][\w$]*)/g)) bound.add(m[1]);
  for (const m of stripped.matchAll(/\bfor\s*\(\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)/g)) bound.add(m[1]);

  // Candidate reads: identifiers not preceded by `.` and not followed by `:` (a key).
  const free = new Set();
  for (const m of stripped.matchAll(/(^|[^.\w$])([A-Za-z_$][\w$]*)\s*(?!:)/gm)) {
    const id = m[2];
    if (RESERVED.has(id) || JS_GLOBALS.has(id)) continue;
    if (bound.has(id) || helperNames.has(id) || engine.params.includes(id)) continue;
    if (engine.undeclaredDeps?.includes(id)) continue;
    if (IMPORTED_NAMES.includes(id)) continue;
    free.add(id);
  }
  // Object-literal keys and member names slip through the rough scan; only flag an
  // identifier that is actually *called* or read as a bare value somewhere.
  const genuinelyFree = [...free].filter((id) => {
    const called = new RegExp(`(^|[^.\\w$])${id}\\s*\\(`, "m").test(stripped);
    const indexed = new RegExp(`(^|[^.\\w$])${id}\\s*\\[`, "m").test(stripped);
    // `RESOLUTION.SAVED` is a read of a free `RESOLUTION`, and missing it meant the
    // generated module threw at runtime rather than failing here, which is the whole
    // point of this check. An object-literal key cannot be followed by `.`, so this
    // does not reintroduce the false positives the filter exists to suppress.
    const dotted = new RegExp(`(^|[^.\\w$])${id}\\s*\\.`, "m").test(stripped);
    return called || indexed || dotted;
  });
  if (genuinelyFree.length) {
    freeVariableProblems.push(`  ${engine.name}: ${genuinelyFree.sort().join(", ")}`);
  }
}

if (freeVariableProblems.length) {
  throw new Error(
    "Engine bodies reference identifiers that are neither parameters nor copied helpers:\n" +
      freeVariableProblems.join("\n") +
      "\n\nAdd each to HELPERS (if it is a pure declaration) in extract_source.mjs.\n" +
      "Do NOT reimplement it — copy it verbatim from App.jsx.",
  );
}

const sourceSha = createHash("sha256").update(source).digest("hex");

const banner = `// @generated by bom/tests/parity/extract_source.mjs — DO NOT EDIT BY HAND.
//
// Verbatim slices of Golden-Ray-BOM src/App.jsx, the behavioural source of truth for
// the BOM calculator. Each function below is one of the app's \`useMemo\` bodies, moved
// unchanged; its parameters are that memo's own React dependency array, which lists
// exactly the closure variables the body reads.
//
// Nothing here is reimplemented, reformatted or "cleaned up". The arithmetic — including
// the placement of every Math.round — is load-bearing: these functions produce the golden
// corpus that the platform engine is held to, so any edit here silently moves the target.
// Re-run the extractor instead.
//
// App.jsx sha256: ${sourceSha}
`;

const helperBlocks = helpers.map(
  (h) => `// ── ${h.name} — App.jsx:${h.startLine}-${h.endLine} ──\n${h.body.join("\n")}`,
);

const engineBlocks = engines.map((e) => {
  const all = [...e.params, ...(e.undeclaredDeps ?? [])];
  const signature = all.length
    ? `{\n${all.map((p) => `  ${p},`).join("\n")}\n}`
    : "";
  return [
    `// ── ${e.name} — App.jsx:${e.startLine}-${e.endLine} ──`,
    `// deps: ${e.params.join(", ") || "(none)"}`,
    ...(e.undeclaredDeps?.length
      ? [`// UNDECLARED (read by the body, absent from the dependency array — upstream stale-memo bug): ${e.undeclaredDeps.join(", ")}`]
      : []),
    `export function ${e.name}(${signature}) {`,
    e.body.join("\n"),
    `}`,
  ].join("\n");
});

const generated = [
  banner,
  IMPORTS.join("\n"),
  helperBlocks.join("\n\n"),
  engineBlocks.join("\n\n"),
  `export const SOURCE_SHA256 = ${JSON.stringify(sourceSha)};`,
  "",
].join("\n");

const checkOnly = process.argv.includes("--check");

if (checkOnly) {
  let committed;
  try {
    committed = readFileSync(OUT, "utf8");
  } catch {
    console.error(`FAIL: ${OUT} does not exist. Run without --check to generate it.`);
    process.exit(1);
  }
  if (committed !== generated) {
    console.error(
      "FAIL: source_engine.mjs is stale — App.jsx has changed since it was generated.\n" +
        "      Re-run `node bom/tests/parity/extract_source.mjs`, then RE-CAPTURE the corpus\n" +
        "      (the golden cases were produced by the old slices and no longer describe the source).",
    );
    process.exit(1);
  }
  console.log(`OK: source_engine.mjs matches App.jsx (sha256 ${sourceSha.slice(0, 12)}…)`);
  process.exit(0);
}

writeFileSync(OUT, generated, "utf8");

console.log(`Wrote ${OUT}`);
console.log(`  App.jsx sha256: ${sourceSha}`);
for (const h of helpers) console.log(`  helper ${h.name.padEnd(26)} App.jsx:${h.startLine}-${h.endLine}`);
for (const e of engines) {
  console.log(`  engine ${e.name.padEnd(26)} App.jsx:${e.startLine}-${e.endLine}  (${e.params.length} params)`);
}
