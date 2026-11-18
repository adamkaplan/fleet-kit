// Admission: these tests show the ONE decision whether a passed test step is carried to a rebased tree (carry.mjs), and which of its test files re-run: the ones the incoming files REACH (reach.mjs). The rule is in carry.mjs; typecheck, lint, style and ratchet steps always run again;
// anything unreadable or unsure runs everything; a missing input stops the run. Every rule has a MUTANT: the same file with that rule cut out, which the scenarios below must catch (a rule nobody tests is a rule nobody holds).
// They would fail if an overlap were carried, if a test that reaches an incoming file (by import, by name, by package) were carried, if a file nothing reaches forced a re-run (the table of shared inputs is gone), if a cheap step were carried, if an unreadable diff or graph were carried, or if a missing input were guessed.
// Oracle: the real carry.mjs (and, for the mutants, copies of its text with one rule removed) on plain path lists and small graphs; run-steps.mjs's own text for "one decision function".
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { decide, parseMap } from "./carry.mjs";
import { matcher } from "./config.mjs";
import { PIN_FILES, buildGraph } from "./reach.mjs";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SOURCE = readFileSync(join(HERE, "carry.mjs"), "utf8");

/** A graph of a small tree: path -> text. */
export const graphOf = (sources) => buildGraph({ files: Object.keys(sources), read: (p) => sources[p], isProse: (f) => f.endsWith(".md") });
const TREE = {
  "server/tests/a.test.mjs": 'import "../src/feature.mjs";',
  "server/tests/b.test.mjs": 'import { s } from "./helpers/shared.mjs"; import { readFileSync } from "node:fs"; readFileSync("fixtures/data.json");',
  "server/tests/c.test.mjs": 'import { d } from "../src/other2.mjs"; readFileSync("fixtures/c-data.json"); // docs/x.md',
  "server/tests/helpers/shared.mjs": "export const s = 1;",
  "server/tests/fixtures/data.json": "{}",
  "server/src/feature.mjs": "export const f = 1;",
  "server/src/other.mjs": "export const o = 1;",
  "server/src/other2.mjs": 'import { d } from "./deep.mjs"; export { d };',
  "server/src/deep.mjs": "export const d = 1;",
  "server/package.json": "{}",
  "server/package-lock.json": "{}",
  "app/package-lock.json": "{}",
  "app/src/x.test.mjs": "",
  "server/tests/fixtures/c-data.json": "{}",
  ".spire/land.json": "{}",
  ".spire/principles.md": "",
  ".spire/tool/PIN": "version=1",
  ".spire/tool/run-steps.mjs": 'import { c } from "./config.mjs";',
  ".spire/tool/config.mjs": 'readFileSync(".spire/land.json");',
  ".spire/tool/council.mjs": "export const k = 1;",
  ".spire/tool/pin.mjs": 'readFileSync(".spire/tool/PIN");',
  "docs/x.md": "",
};
const GRAPH = graphOf(TREE);
const RUN = { runner: ".spire/tool/run-steps.mjs", own: ".spire/" }; // the gate's own runner and the project's folder
const A = "server/tests/a.test.mjs";
const B = "server/tests/b.test.mjs";
const C = "server/tests/c.test.mjs";

// every scenario returns a failure text or "" : the real module has none, each mutant has at least one
const scenarios = (m) => {
  const bad = [];
  const test_ = { id: "tests:server", retry: "test" };
  const go = (over) => {
    const changed = over.changed === undefined ? [A, "server/src/feature.mjs"] : over.changed;
    try {
      return m.decide({
        step: over.step === undefined ? test_ : over.step,
        earlier: over.earlier === undefined ? true : over.earlier,
        incoming: over.incoming === undefined ? ["server/src/other.mjs"] : over.incoming,
        changed,
        files: over.files === undefined ? [A, B, C] : over.files,
        graph: over.graph === undefined ? GRAPH : over.graph,
        ...RUN,
      });
    } catch (e) {
      return { carry: "threw", reason: e.message };
    }
  };
  const expect = (name, got, carry, rerun) => {
    if (got.carry !== carry) bad.push(`${name}: expected carry=${carry}, got ${JSON.stringify(got)}`);
    if (typeof got.reason !== "string" || !got.reason) bad.push(`${name}: no reason`);
    if (rerun !== undefined && JSON.stringify(got.rerun) !== JSON.stringify(rerun)) bad.push(`${name}: expected re-run ${JSON.stringify(rerun)}, got ${JSON.stringify(got.rerun)}`);
  };
  // what nothing reaches carries; the change's own test is the only re-run
  expect("a file nothing reaches carries (S3)", go({}), true, [A]);
  expect("a file only prose names carries", go({ incoming: ["docs/x.md"] }), true, [A]);
  expect("a .spire/ file nothing names carries (no table of shared inputs)", go({ incoming: [".spire/principles.md"] }), true, [A]);
  expect("a lock file of ANOTHER package carries", go({ incoming: ["app/package-lock.json"] }), true, [A]);
  // what a test reaches re-runs, and nothing else does
  expect("a helper a test imports re-runs that test", go({ incoming: ["server/tests/helpers/shared.mjs"] }), true, [A, B]);
  expect("a fixture a test names re-runs that test", go({ incoming: ["server/tests/fixtures/data.json"] }), true, [A, B]);
  expect("a fixture another test reads re-runs that test", go({ incoming: ["server/tests/fixtures/c-data.json"] }), true, [A, C]);
  expect("PIN, which the runner does not reach, carries (no .spire/ table)", go({ incoming: [".spire/tool/PIN"] }), true, [A]);
  expect("a tool file the runner does not reach carries", go({ incoming: [".spire/tool/council.mjs"] }), true, [A]);
  expect("land.json, which the runner reads, runs the step whole", go({ incoming: [".spire/land.json"] }), false);
  expect("a tool file the runner imports runs the step whole", go({ incoming: [".spire/tool/config.mjs"] }), false);
  expect("a runner that is not tracked reaches every file under .spire/: unsure runs more", go({ incoming: [".spire/principles.md"], graph: graphOf(Object.fromEntries(Object.entries(TREE).filter(([f]) => f !== ".spire/tool/run-steps.mjs"))) }), false);
  expect("a file reached through an import chain re-runs the test at its end", go({ incoming: ["server/src/deep.mjs"] }), true, [A, C]);
  expect("a lock file of the package re-runs the package's tests", go({ incoming: ["server/package-lock.json"] }), true, [A, B, C]);
  expect("a test file that came in re-runs", go({ incoming: [C] }), true, [A, C]);
  expect("overlap runs (S8)", go({ incoming: ["server/src/other.mjs", "server/src/feature.mjs"] }), false);
  expect("a non-test step that names nothing it reads always runs", go({ step: { id: "typecheck", retry: "step" } }), false);
  expect("a non-test step with nothing incoming still runs", go({ step: { id: "lint", retry: "lint" }, incoming: [] }), false);
  expect("no earlier pass runs", go({ earlier: false }), false);
  expect("unreadable incoming runs", go({ incoming: null }), false);
  expect("unreadable change runs", go({ changed: null }), false);
  expect("an unreadable graph runs", go({ graph: null }), false);
  if (go({ files: null }).carry !== false) bad.push("a step that cannot be narrowed was carried");
  for (const [name, call] of [
    ["no input", () => m.decide(undefined)],
    ["no step", () => m.decide({ earlier: true, incoming: [], changed: [], files: [], graph: GRAPH })],
    ["no earlier flag", () => m.decide({ step: test_, incoming: [], changed: [], files: [], graph: GRAPH })],
    ["no files", () => m.decide({ step: test_, earlier: true, incoming: [], changed: [], graph: GRAPH })],
    ["no incoming", () => m.decide({ step: test_, earlier: true, changed: [], files: [], graph: GRAPH })],
    ["no graph", () => m.decide({ step: test_, earlier: true, incoming: ["a"], changed: [], files: [], ...RUN })],
    ["no runner", () => m.decide({ step: test_, earlier: true, incoming: ["a"], changed: [], files: [], graph: GRAPH, own: ".spire/" })],
    ["no own", () => m.decide({ step: test_, earlier: true, incoming: ["a"], changed: [], files: [], graph: GRAPH, runner: ".spire/tool/run-steps.mjs" })],
  ]) {
    try {
      call();
      bad.push(`missing input (${name}) did not throw (S12)`);
    } catch (e) {
      if (!/missing input/.test(String(e.message))) bad.push(`missing input (${name}) threw another error: ${e.message}`);
    }
  }
  return bad;
};

const load = async (text, name) => {
  const dir = mkdtempSync(join(tmpdir(), "carry-mutant-"));
  try {
    // the mutant sits next to the real reach.mjs it imports
    writeFileSync(join(dir, "reach.mjs"), readFileSync(join(HERE, "reach.mjs"), "utf8"));
    writeFileSync(join(dir, `${name}.mjs`), text);
    return await import(pathToFileURL(join(dir, `${name}.mjs`)).href);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
};

test("the real decision carries what nothing reaches, re-runs what is reached, and runs on every overlap, cheap step, unreadable diff or graph and missing input", async () => {
  assert.deepEqual(scenarios(await load(SOURCE, "real")), []);
});

const MUTANTS = {
  "carries on overlap (S8)": ["if (overlap.length)", "if (false)"],
  "carries a cheap step": ['step.retry !== "test"', "false"],
  "carries with no earlier pass": ["if (!earlier)", "if (false)"],
  "carries an unreadable incoming diff": ["if (incoming === null)", "if (false)"],
  "carries an unreadable change": ["if (changed === null)", "if (false)"],
  "carries a test that reaches what came in": ["byGraph.has(t) ||", ""],
  "carries a file the gate's own runner reads": ["if (gate) return", "if (false) return"],
  "carries on an unreadable graph": ["if (input.graph === null)", "if (false)"],
  "carries the own test files too (no re-run)": ["...universe.filter((f) => own_.has(f)), ...more", "...more"],
  "carries a step that cannot be narrowed": ["if (files === null) return", "if (false) return"],
  "guesses a missing input": ["throw new Error(`carry: missing input ${name}`)", "void name"],
};
for (const [name, [from, to]] of Object.entries(MUTANTS)) {
  test(`mutant: a decision that ${name} is caught`, async () => {
    assert.ok(SOURCE.includes(from), `the mutant's anchor is gone from carry.mjs: ${from}`);
    const failures = scenarios(await load(SOURCE.replace(from, to), "mutant"));
    assert.ok(failures.length > 0, `the scenarios did not notice: ${name}`);
  });
}

// ---- a step that is NOT a test, and says what it reads (`reads`): carried over a rebase when nothing it reads came in (carry.mjs, carryCheck) ----
// Oracle: the real carry.mjs on a small graph; each rule has a mutant below. `reads` is the predicate the caller makes from land.json's globs: the boundary of the step's inputs, nothing followed from it.
const NT_TREE = {
  ...TREE,
  "scripts/lint.mjs": 'import { rules } from "./lint-rules.mjs";',
  "scripts/lint-rules.mjs": "export const rules = 1;",
  "scripts/other.mjs": "export const o = 1;",
  "app/src/a.tsx": "export const a = 1;",
  "app/src/b.tsx": "export const b = 1;",
  ".node-version": "22",
  "package.json": "{}",
  "package-lock.json": "{}",
};
const NT_GRAPH = graphOf(NT_TREE);
const readsApp = matcher(["scripts/lint.mjs", "app/**", "package*.json"]);
const ntScenarios = (m) => {
  const bad = [];
  const go = (over = {}) => {
    try {
      return m.decide({ step: { id: "typecheck:app", retry: undefined, reads: readsApp, ...(over.step ?? {}) }, earlier: true, incoming: ["server/src/other.mjs"], changed: ["app/src/a.tsx"], files: null, graph: NT_GRAPH, ...RUN, ...over, ...("step" in over ? { step: { id: "typecheck:app", reads: readsApp, ...over.step } } : {}) });
    } catch (e) {
      return { carry: "threw", reason: e.message };
    }
  };
  const expect = (name, got, carry) => {
    if (got.carry !== carry) bad.push(`${name}: expected carry=${carry}, got ${JSON.stringify(got)}`);
    if (typeof got.reason !== "string" || !got.reason) bad.push(`${name}: no reason`);
    if (got.carry === true && (got.rerun.length || got.select)) bad.push(`${name}: a carried step re-runs nothing`);
  };
  expect("main moved in a file the step does not read: carried", go({}), true);
  expect("main moved in a doc: carried", go({ incoming: ["docs/x.md"] }), true);
  expect("a script the step runs, a file it reads: runs", go({ incoming: ["scripts/lint.mjs"] }), false);
  expect("a file of a directory the step reads: runs", go({ incoming: ["app/src/b.tsx"] }), false);
  expect("a file the step's script imports but the project's boundary does not name is no input of the step (readsGap is the project's check of that)", go({ incoming: ["scripts/lint-rules.mjs"] }), true);
  expect("a file the step's script does not import: carried", go({ incoming: ["scripts/other.mjs"] }), true);
  expect("a toolchain pin that came in: runs", go({ incoming: [".node-version"] }), false);
  expect("the project's lock file the step names: runs", go({ incoming: ["package-lock.json"] }), false);
  expect("the lock file of another package: carried", go({ incoming: ["server/package-lock.json"] }), true);
  expect("a .spire/ file the runner reads: runs", go({ incoming: [".spire/land.json"] }), false);
  expect("a .spire/ file the runner does not read: carried", go({ incoming: [".spire/principles.md"] }), true);
  expect("a file of a directory the step reads that main deleted (it is no root any more): runs", go({ incoming: ["app/src/gone.tsx"] }), false);
  expect("an overlap with the change's own file: runs", go({ incoming: ["scripts/other.mjs"], changed: ["scripts/other.mjs"] }), false);
  expect("no earlier pass: runs", go({ earlier: false }), false);
  expect("unreadable incoming: runs", go({ incoming: null }), false);
  expect("unreadable change: runs", go({ changed: null }), false);
  expect("unreadable graph: runs", go({ graph: null }), false);
  expect("a step that says nothing about what it reads always runs", go({ step: { reads: undefined } }), false);
  if (!/always runs again/.test(go({ step: { reads: undefined } }).reason)) bad.push("a step with no `reads` does not say it always runs again");
  for (const [name, call] of [
    ["no graph", () => m.decide({ step: { id: "t", reads: readsApp }, earlier: true, incoming: ["a"], changed: [], files: null, ...RUN })],
    ["no runner", () => m.decide({ step: { id: "t", reads: readsApp }, earlier: true, incoming: ["a"], changed: [], files: null, graph: NT_GRAPH, own: ".spire/" })],
  ]) {
    try {
      call();
      bad.push(`missing input (${name}) did not throw`);
    } catch (e) {
      if (!/missing input/.test(String(e.message))) bad.push(`missing input (${name}) threw another error: ${e.message}`);
    }
  }
  return bad;
};

test("a step that is not a test and names what it reads is carried when nothing it reads came in, and runs when anything does, or on an overlap, a pin, the runner's inputs, an unreadable diff or graph", async () => {
  assert.deepEqual(ntScenarios(await load(SOURCE, "ntreal")), []);
});

const NT_MUTANTS = {
  "carries an overlap": ["if (overlap) return again", "if (false) return again"],
  "carries a file the step reads": ["if (read) return again", "if (false) return again"],
  "carries a toolchain pin": ["if (pin) return again", "if (false) return again"],
  "carries a file the gate's own runner reads": ["if (gate) return again", "if (false) return again"],
  "carries with no earlier pass": ["if (!earlier) return again", "if (false) return again"],
  "carries an unreadable incoming diff": ["if (incoming === null) return again", "if (false) return again"],
  "carries an unreadable change": ["if (changed === null) return again", "if (false) return again"],
  "carries on an unreadable graph": ['if (input.graph === null) return again("what the files that came in reach cannot be read")', "if (false) return again(0)"],
  "carries a step that does not say what it reads": ['if (typeof step.reads !== "function") return again', "if (false) return again"],
  "never carries": ["return { carry: true, reason: `${incoming.length} file(s) came in, none the step reads", "return { carry: false, reason: `${incoming.length} file(s) came in, none the step reads"],
};
for (const [name, [from, to]] of Object.entries(NT_MUTANTS)) {
  test(`mutant: a decision for a step that is not a test that ${name} is caught`, async () => {
    assert.ok(SOURCE.includes(from), `the mutant's anchor is gone from carry.mjs: ${from}`);
    const failures = ntScenarios(await load(SOURCE.replace(from, to), "ntmutant"));
    assert.ok(failures.length > 0, `the scenarios did not notice: ${name}`);
  });
}

test("readsGap: a file inside a step's boundary that imports a file outside it is a gap the project's test finds; tests and prose do not count", async () => {
  const { readsGap } = await import("./carry.mjs");
  const graph = graphOf({ "app/src/a.tsx": 'import "../../shared/s.mts"; import "./b.tsx";', "app/src/b.tsx": "", "app/src/a.test.tsx": 'import "../../server/x.ts";', "shared/s.mts": "", "server/x.ts": "", "docs/n.md": "" });
  const isTest = matcher(["**/*.test.*"]);
  assert.deepEqual(readsGap({ graph, reads: matcher(["app/src/**"]), isTest }), [{ file: "app/src/a.tsx", imports: "shared/s.mts" }]);
  assert.deepEqual(readsGap({ graph, reads: matcher(["app/src/**", "shared/**"]), isTest }), []);
  assert.throws(() => readsGap({ graph, reads: matcher([]) }), /missing input isTest/);
});

// ---- a step marked `select: "reach"` is carried over a rebase (carry.mjs): of the tests the change reaches on this tree, the ones that passed before and that nothing incoming reaches ----
const SEL_TREE = {
  "ios/aTests.mjs": 'import "./lib.mjs"; import "./dep.mjs";',
  "ios/bTests.mjs": 'import "./lib.mjs";',
  "ios/cTests.mjs": "",
  "ios/lib.mjs": "",
  "ios/dep.mjs": "",
  ".spire/tool/run-steps.mjs": "",
};
const selScenarios = (m) => {
  const bad = [];
  const files = ["ios/aTests.mjs", "ios/bTests.mjs", "ios/cTests.mjs"];
  const go = (over = {}) => {
    try {
      return m.decide({ step: { id: "tests:ios", retry: "test", select: "reach" }, earlier: true, passed: ["ios/aTests.mjs", "ios/bTests.mjs"], incoming: ["docs/x.md"], changed: ["ios/lib.mjs"], deleted: [], files, graph: graphOf(SEL_TREE), preload: [], ...RUN, ...over });
    } catch (e) {
      return { carry: "threw", reason: e.message };
    }
  };
  const is = (name, got, want) => {
    for (const [k, v] of Object.entries(want)) if (JSON.stringify(got[k]) !== JSON.stringify(v)) bad.push(`${name}: ${k} expected ${JSON.stringify(v)}, got ${JSON.stringify(got[k])} (${got.reason})`);
  };
  is("nothing incoming reaches: the two selected tests are carried, none run", go(), { carry: true, rerun: [], universe: ["ios/aTests.mjs", "ios/bTests.mjs"] });
  is("an incoming file one test imports: that test runs, the other is carried", go({ incoming: ["ios/dep.mjs"] }), { carry: true, rerun: ["ios/aTests.mjs"] });
  is("a test that never ran (not in the earlier pass) runs", go({ passed: ["ios/aTests.mjs"] }), { carry: true, rerun: ["ios/bTests.mjs"] });
  is("a step that passed whole (no list) carries what it reaches", go({ passed: undefined }), { carry: true, rerun: [] });
  is("an overlap is selected again, never run whole", go({ incoming: ["ios/lib.mjs"], changed: ["ios/lib.mjs"] }), { carry: false, select: ["ios/aTests.mjs", "ios/bTests.mjs"] });
  is("an unreadable diff is selected again, never run whole", go({ incoming: null }), { carry: false, select: ["ios/aTests.mjs", "ios/bTests.mjs"] });
  is("the gate's own runner's input is selected again", go({ incoming: [".spire/tool/run-steps.mjs"] }), { carry: false, select: ["ios/aTests.mjs", "ios/bTests.mjs"] });
  is("a change that reaches no test does not run it, carried or not", go({ changed: ["ios/other.mjs"] }), { carry: false, select: [] });
  // deepFor: an incoming file outside the hubs re-runs its own and its direct importers' tests only; one the globs name re-runs by full reach
  const deepTree = { ...SEL_TREE, "ios/hub.mjs": 'import "./leaf.mjs";', "ios/leaf.mjs": "", "ios/aTests.mjs": 'import "./lib.mjs"; import "./hub.mjs";', "ios/bTests.mjs": 'import "./leaf.mjs"; import "./lib.mjs";', "ios/db/core.mjs": 'import "./leaf.mjs";' };
  const deepGo = (over) => go({ graph: graphOf(deepTree), step: { id: "tests:ios", retry: "test", select: "reach", deepFor: matcher(["ios/db/**"]) }, passed: ["ios/aTests.mjs", "ios/bTests.mjs"], ...over });
  is("deepFor: an incoming file outside the hubs re-runs only the test that imports it directly (b imports leaf; a reaches it through hub)", deepGo({ incoming: ["ios/leaf.mjs"] }), { carry: true, rerun: ["ios/bTests.mjs"] });
  is("deepFor: an incoming file the globs name re-runs by full reach", deepGo({ incoming: ["ios/db/core.mjs"], graph: graphOf({ ...deepTree, "ios/aTests.mjs": 'import "./lib.mjs"; import "./db/core.mjs";' }) }), { carry: true, rerun: ["ios/aTests.mjs"] });
  is("without deepFor the full reach re-runs both", go({ graph: graphOf(deepTree), incoming: ["ios/leaf.mjs"], passed: ["ios/aTests.mjs", "ios/bTests.mjs"] }), { carry: true, rerun: ["ios/aTests.mjs", "ios/bTests.mjs"] });
  is("what the change touches, as a whole step, stays whole: a file the runner preloads", go({ changed: ["ios/dep.mjs"], preload: ["ios/dep.mjs"] }), { carry: true });
  return bad;
};

test("a step marked select reach: the tests it has to pass on the rebased tree that passed before and that nothing incoming reaches are carried, the others run; what cannot be carried is selected again", async () => {
  assert.deepEqual(selScenarios(await load(SOURCE, "selreal")), []);
});

const SEL_MUTANTS = {
  "carries a selected test that never ran": ["(have && !have.has(t)) ||", ""],
  "carries a selected test an incoming file reaches": ["byGraph.has(t) ||", ""],
  "runs the whole step when it cannot carry": ["const first = firstRun({ step, mapped, changed, deleted, files, input });\n    return { ...first, reason: `${reason}; ${first.reason}` };", 'return { carry: false, reason, rerun: [], select: null };'],
  "carries tests the change does not reach": ["const universe = first?.select ?? files;", "const universe = files;"],
  "reaches through the hubs for an incoming file outside deepFor": ['const hubs = marked && typeof step.deepFor === "function" ? step.deepFor : null;', "const hubs = null;"],
  "runs a step the change reaches no test of": ["if (first && first.select && first.select.length === 0) return first;", ""],
};
for (const [name, [from, to]] of Object.entries(SEL_MUTANTS)) {
  test(`mutant: a decision for a step marked select reach that ${name} is caught`, async () => {
    assert.ok(SOURCE.includes(from), `the mutant's anchor is gone from carry.mjs: ${from}`);
    const failures = selScenarios(await load(SOURCE.replace(from, to), "selmutant"));
    assert.ok(failures.length > 0, `the scenarios did not notice: ${name}`);
  });
}

test("a test file that came in re-runs; a map only ADDS re-runs: a test the graph reaches re-runs although the map's coverage does not list the file", async () => {
  const go = (over = {}) => decide({ step: { id: "tests:server", retry: "test" }, earlier: true, incoming: ["server/tests/c.test.mjs"], changed: ["server/tests/a.test.mjs"], files: ["server/tests/a.test.mjs", "server/tests/b.test.mjs", "server/tests/c.test.mjs"], graph: graphOf({}), ...RUN, ...over });
  assert.deepEqual(go().rerun, ["server/tests/a.test.mjs", "server/tests/c.test.mjs"]);
  const known = mapOf({ files: ["server/tests/c.test.mjs", "server/src/z.mjs"], tests: { "server/tests/a.test.mjs": [1], "server/tests/b.test.mjs": [1], "server/tests/c.test.mjs": [1], "server/tests/z.test.mjs": [0] } });
  assert.deepEqual(go({ map: known }).rerun, ["server/tests/a.test.mjs", "server/tests/c.test.mjs"]);
  // b spawns a script by name (no import): the map executed the script in another test only; the graph reaches b, so b re-runs
  const spawns = graphOf({ "server/tests/b.test.mjs": 'spawn("node", ["scripts/x.mjs"]);', "scripts/x.mjs": "" });
  const viaName = go({ incoming: ["scripts/x.mjs"], graph: spawns, map: mapOf({ files: ["scripts/x.mjs"], tests: { "server/tests/a.test.mjs": [0], "server/tests/b.test.mjs": [0], "server/tests/c.test.mjs": [0] } }) });
  assert.ok(viaName.rerun.includes("server/tests/b.test.mjs"));
  const onlyMap = go({ incoming: ["scripts/x.mjs"], graph: spawns, map: mapOf({ files: ["scripts/x.mjs", "server/src/other.mjs"], tests: { "server/tests/a.test.mjs": [0], "server/tests/b.test.mjs": [1], "server/tests/c.test.mjs": [0] } }) });
  assert.deepEqual(onlyMap.rerun, ["server/tests/a.test.mjs", "server/tests/b.test.mjs", "server/tests/c.test.mjs"]); // the graph's b, the map's c
});

test("a table of shared inputs is gone: no path kind is named in the decision", () => {
  assert.doesNotMatch(SOURCE, /sharedKind|sharedReason|LOCK_FILES|startsWith\("helpers\/"\)|__snapshots__|-helper/);
});

test("the change's own test files re-run: added or edited, and only those besides what is reached; a step with no list runs whole", () => {
  const base = { step: { id: "tests:ios", retry: "test" }, earlier: true, incoming: ["ios/lib/util.mjs"], graph: graphOf({}), ...RUN };
  const files = ["ios/a.test.mjs", "ios/b.test.mjs", "ios/c.test.mjs"];
  assert.deepEqual(decide({ ...base, files, changed: ["ios/a.test.mjs", "ios/c.test.mjs", "ios/lib/x.mjs"] }).rerun, ["ios/a.test.mjs", "ios/c.test.mjs"]);
  const whole = decide({ ...base, files: null, changed: ["ios/a.test.mjs"] });
  assert.equal(whole.carry, false);
  assert.match(whole.reason, /no test list/);
  assert.throws(() => decide({ ...base, changed: [] }), /missing input files/);
  // everything that re-runs whole carries no narrowing
  for (const over of [{ incoming: ["ios/a.test.mjs"] }, { earlier: false }, { changed: null }, { graph: null }]) {
    const d = decide({ ...base, files, changed: ["ios/a.test.mjs"], ...over });
    assert.equal(d.carry, false);
    assert.deepEqual(d.rerun, []);
  }
});

test("run-steps.mjs asks the one decision function and holds no carry rule of its own", () => {
  const text = readFileSync(join(HERE, "run-steps.mjs"), "utf8");
  assert.equal((text.match(/\bdecide\(/g) ?? []).length, 1);
  assert.doesNotMatch(text, /matcher\(cfg\.areas/); // the area-inputs rule is gone
  assert.doesNotMatch(text, /LOCK_FILES|package-lock/);
});

// ---- the coverage map (S5-S9, S11, S12): first-run selection, the unchanged overlap rule on a re-check, fallbacks that name the map ----
const NOW = Date.parse("2026-11-13T12:00:00Z");
const mapOf = (over = {}) =>
  parseMap({
    text: JSON.stringify({
      meta: { built: "2026-11-13T11:30:00Z", commit: "abc1234" },
      files: ["server/src/feature.mjs", "server/src/util.mjs"],
      tests: { "server/tests/a.test.mjs": [0], "server/tests/b.test.mjs": [1], "server/tests/c.test.mjs": [0, 1] },
      ...over,
    }),
    now: NOW,
    maxAgeHours: 24,
  });
const first = (over) =>
  decide({
    step: { id: "tests:server", retry: "test" },
    earlier: false,
    incoming: [],
    changed: ["server/src/feature.mjs"],
    files: ["server/tests/a.test.mjs", "server/tests/b.test.mjs", "server/tests/c.test.mjs"],
    map: mapOf(),
    ...over,
  });

test("S6: a first run with a map runs the tests that reach the touched files, plus the change's own tests", () => {
  const d = first({});
  assert.equal(d.carry, false);
  assert.deepEqual(d.select, ["server/tests/a.test.mjs", "server/tests/c.test.mjs"]);
  assert.match(d.reason, /coverage map age 30m/);
  assert.deepEqual(first({ changed: ["server/src/util.mjs", "server/tests/a.test.mjs"] }).select, ["server/tests/a.test.mjs", "server/tests/b.test.mjs", "server/tests/c.test.mjs"]);
  // no test of the step reaches the change: the step runs whole, never nothing
  assert.equal(first({ changed: ["server/src/feature.mjs"], files: ["server/tests/b.test.mjs"] }).select, null);
});

test("S5: the map's age is read (the indexed format) and a map older than the limit is refused", () => {
  assert.equal(mapOf().age, "30m");
  assert.equal(mapOf().commit, "abc1234");
  const direct = parseMap({ text: JSON.stringify({ meta: { built: NOW - 3 * 3600000 }, files: ["src/x.mjs"], tests: { "t.test.mjs": [0] } }), now: NOW, maxAgeHours: 24 });
  assert.equal(direct.age, "3h");
  assert.ok(direct.covered.has("src/x.mjs"));
  assert.equal(parseMap({ text: JSON.stringify({ meta: { built: NOW }, tests: { "t.test.mjs": ["src/x.mjs"] } }), now: NOW, maxAgeHours: 24 }).ok, false); // paths instead of indexes: not a map of this format
  const old = parseMap({ text: JSON.stringify({ meta: { built: NOW - 25 * 3600000 }, tests: {} }), now: NOW, maxAgeHours: 24 });
  assert.equal(old.ok, false);
  assert.match(old.reason, /too old/);
  assert.throws(() => parseMap({ text: "{}", now: NOW }), /missing input maxAgeHours/);
});

test("S8/S12: a missing, corrupt or old map runs the full area and names the map; a new or unknown file runs the full area", () => {
  for (const text of [null, "not json", "{}", JSON.stringify({ meta: { built: "x" }, tests: {} }), JSON.stringify({ meta: { built: NOW - 99 * 3600000 }, tests: {} })]) {
    const d = first({ map: parseMap({ text, now: NOW, maxAgeHours: 24 }) });
    assert.equal(d.select, null);
    assert.match(d.reason, /coverage map.*full area/);
  }
  const unknown = first({ changed: ["server/src/new.mjs"] });
  assert.equal(unknown.select, null);
  assert.match(unknown.reason, /new or unknown file: server\/src\/new\.mjs/);
  assert.match(first({ changed: ["server/src/feature.mjs"], deleted: ["server/src/gone.mjs"] }).reason, /deleted file/);
  assert.equal(first({ changed: null }).select, null);
});

test("S9: a touched sql, migration, configuration or environment file (what no map can see) runs the full area, naming the kind", () => {
  const cases = {
    "db/schema.sql": "sql",
    "server/migrations/001.mjs": "migration",
    "server/db/x.mjs": "migration",
    "server/tsconfig.json": "configuration",
    "Dockerfile.ci": "configuration",
    "Makefile": "configuration",
    ".github/ci.mjs": "configuration",
    ".eslintrc.cjs": "configuration",
    "server/.env.test": "environment",
    "server/package-lock.json": "configuration",
    ".spire/land.json": "configuration",
  };
  for (const [f, kind] of Object.entries(cases)) {
    const d = first({ changed: ["server/src/feature.mjs", f] });
    assert.equal(d.select, null, f);
    assert.ok(d.reason.includes(kind), `${f}: ${d.reason}`);
  }
});

test("a helper, fixture or .spire/ code file is no kind of its own: the map selects the tests that execute it; one the map does not know runs the full area", () => {
  const known = mapOf({ files: ["server/src/feature.mjs", "server/src/util.mjs", "server/tests/helpers/db-helper.mjs", ".spire/tool/x.mjs"], tests: { "server/tests/a.test.mjs": [0], "server/tests/b.test.mjs": [1], "server/tests/c.test.mjs": [2, 3] } });
  const d = first({ map: known, changed: ["server/tests/helpers/db-helper.mjs"] });
  assert.deepEqual(d.select, ["server/tests/c.test.mjs"]);
  assert.deepEqual(first({ map: known, changed: [".spire/tool/x.mjs"] }).select, ["server/tests/c.test.mjs"]);
  const unknown = first({ map: known, changed: ["server/tests/a.fixture.mjs"] });
  assert.equal(unknown.select, null);
  assert.match(unknown.reason, /new or unknown file/);
});

test("S7: on a re-check the overlap rule stays the base and the map only adds the tests that reach what came in", () => {
  const re = (over) =>
    decide({
      step: { id: "tests:server", retry: "test" },
      earlier: true,
      incoming: ["server/src/util.mjs"],
      changed: ["server/tests/a.test.mjs"],
      files: ["server/tests/a.test.mjs", "server/tests/b.test.mjs", "server/tests/c.test.mjs"],
      graph: graphOf({}),
      ...RUN,
      map: mapOf(),
      ...over,
    });
  const d = re({});
  assert.equal(d.carry, true);
  assert.deepEqual(d.rerun, ["server/tests/a.test.mjs", "server/tests/b.test.mjs", "server/tests/c.test.mjs"]);
  // the overlap rule's re-run is never carried by the map
  const overlap = re({ incoming: ["server/tests/a.test.mjs"] });
  assert.equal(overlap.carry, false);
  // a file the map did not execute is the graph's: the test that imports it re-runs, the others carry
  const viaGraph = re({ incoming: ["server/src/new-helper.mjs"], graph: graphOf({ "server/tests/c.test.mjs": 'import "../src/new-helper.mjs";', "server/src/new-helper.mjs": "" }) });
  assert.deepEqual(viaGraph.rerun, ["server/tests/a.test.mjs", "server/tests/c.test.mjs"]);
  // the same file without a map is the graph's too
  assert.deepEqual(re({ map: undefined, incoming: ["server/src/new-helper.mjs"], graph: graphOf({ "server/tests/b.test.mjs": 'import "../src/new-helper.mjs";', "server/src/new-helper.mjs": "" }) }).rerun, ["server/tests/a.test.mjs", "server/tests/b.test.mjs"]);
  // an unusable map leaves the overlap rule as it was
  const bad = re({ map: parseMap({ text: null, now: NOW, maxAgeHours: 24 }) });
  assert.equal(bad.carry, true);
  assert.deepEqual(bad.rerun, ["server/tests/a.test.mjs"]);
  assert.match(bad.reason, /no coverage map/);
  assert.deepEqual(re({ map: undefined }).rerun, ["server/tests/a.test.mjs"]);
});

test("mutants of the map rule are caught (S7, S9, S8)", async () => {
  const run = async (from, to) => {
    assert.ok(SOURCE.includes(from), from);
    const m = await load(SOURCE.replace(from, to), "mapmutant");
    const d = (over) => m.decide({ step: { id: "t", retry: "test" }, earlier: false, incoming: [], changed: ["a.mjs"], files: ["a.test.mjs", "b.test.mjs"], map: parseMap({ text: JSON.stringify({ meta: { built: NOW }, files: ["a.mjs", "x.sql", "b.mjs"], tests: { "a.test.mjs": [0, 1], "b.test.mjs": [2] } }), now: NOW, maxAgeHours: 1 }), ...over });
    return d;
  };
  assert.deepEqual(decide({ step: { id: "t", retry: "test" }, earlier: false, incoming: [], changed: ["a.mjs"], files: ["a.test.mjs", "b.test.mjs"], map: parseMap({ text: JSON.stringify({ meta: { built: NOW }, files: ["a.mjs", "x.sql", "b.mjs"], tests: { "a.test.mjs": [0, 1], "b.test.mjs": [2] } }), now: NOW, maxAgeHours: 1 }) }).select, ["a.test.mjs"]);
  const noKind = await run("if (kind) return", "if (false) return");
  assert.notEqual(noKind({ changed: ["a.mjs", "x.sql"] }).select, null); // the real one runs the full area on sql
  const noUnknown = await run("if (!mapped.covered.has(f) &&", "if (false &&");
  assert.notEqual(noUnknown({ changed: ["a.mjs", "new.mjs"] }).select, null);
  const overrides = await run("const more = rest.filter(", "return { carry: true, reason: 'x', rerun: [], select: null }; const more = rest.filter(");
  const o = overrides({ earlier: true, incoming: ["b.mjs"], changed: ["a.test.mjs"], graph: graphOf({}), ...RUN });
  assert.deepEqual(o.rerun, []); // a map that ignores what came in carries b.test.mjs, which the real rule re-runs
});

test("a test the map does not know (it came in on main after the map was built) is selected on a first run and on a re-check", () => {
  const files = ["server/tests/a.test.mjs", "server/tests/b.test.mjs", "server/tests/d.test.mjs"];
  assert.deepEqual(first({ files }).select, ["server/tests/a.test.mjs", "server/tests/d.test.mjs"]);
  const re = decide({ step: { id: "tests:server", retry: "test" }, earlier: true, incoming: ["server/src/util.mjs"], changed: ["server/src/feature.mjs"], files, graph: graphOf({}), ...RUN, map: mapOf() });
  assert.equal(re.carry, true);
  assert.deepEqual(re.rerun, ["server/tests/b.test.mjs", "server/tests/d.test.mjs"]);
});

test("an unreadable list of deleted files runs the full area on a first run; an absent one means none", () => {
  const d = first({ deleted: null });
  assert.equal(d.select, null);
  assert.match(d.reason, /deleted files cannot be read/);
  assert.notEqual(first({}).select, null);
});

// ---- THE FIRST RUN BY REACH (a step marked `select: "reach"`): the tests the change reaches, nothing when it reaches none, the whole step when the text cannot follow it ----
const REACH_TREE = {
  "server/tests/a.test.mjs": 'import "../src/feature.mjs"; readFileSync("mise.toml");',
  "server/tests/b.test.mjs": 'import { s } from "./helpers/shared.mjs"; import "../src/other.mjs";',
  "server/tests/c.test.mjs": 'import "./helpers/shared.mjs"; readFileSync("fixtures/c-data.json"); readFileSync("db/001.sql");',
  "server/tests/d.test.mjs": 'import "../src/uses-gone.mjs";',
  "server/tests/helpers/shared.mjs": 'import "../../src/deep.mjs";',
  "server/tests/helpers/preload.mjs": 'import "../../src/booted.mjs";',
  "server/tests/fixtures/c-data.json": "{}",
  "server/db/001.sql": "",
  "server/src/feature.mjs": "export const f = 1;",
  "server/src/other.mjs": "export const o = 1;",
  "server/src/deep.mjs": "export const d = 1;",
  "server/src/booted.mjs": 'import "./booted-deep.mjs";',
  "server/src/booted-deep.mjs": "export const b = 1;",
  "server/src/lonely.mjs": "export const l = 1;", // nothing imports it
  "server/src/uses-gone.mjs": 'import "./gone.mjs";', // gone.mjs is deleted by the change: this file still names it
  "server/package.json": "{}",
  "server/scripts/pins.json": "{}",
  "app/src/page.tsx": 'import "./page.css";',
  "app/src/page.css": "",
  "app/src/page.test.tsx": 'import "./page.tsx";',
  "app/package-lock.json": "{}",
  ".node-version": "22",
  "mise.toml": "",
  ".spire/land.json": "{}",
  ".spire/tool/run-steps.mjs": 'import "./config.mjs";',
  ".spire/tool/config.mjs": 'readFileSync(".spire/land.json");',
  ".spire/principles.md": "",
  "docs/x.md": "",
};
const REACH_GRAPH = buildGraph({ files: [...Object.keys(REACH_TREE), "server/src/gone.mjs"], read: (p) => REACH_TREE[p] ?? null, isProse: (f) => f.endsWith(".md") });
const SERVER_TESTS = ["server/tests/a.test.mjs", "server/tests/b.test.mjs", "server/tests/c.test.mjs", "server/tests/d.test.mjs"];
const byReach = (changed, over = {}) =>
  decide({ step: { id: "tests:server", retry: "test", select: "reach", dir: "server", isTest: (f) => /\.test\.mjs$/.test(f) }, earlier: false, incoming: [], changed, deleted: [], files: SERVER_TESTS, graph: REACH_GRAPH, preload: ["server/tests/helpers/preload.mjs"], ...RUN, ...over });

test("a first run by reach: an app-only change selects zero server tests, and the step does not run", () => {
  const d = byReach(["app/src/page.tsx", "app/src/page.css"]);
  assert.equal(d.carry, false);
  assert.deepEqual(d.select, []);
  assert.equal(d.by, "reach");
  assert.match(d.reason, /selected 0 of 4 test file\(s\)/);
  assert.deepEqual(byReach(["docs/x.md"]).select, []); // prose: no test reaches it
  assert.deepEqual(byReach(["docs/db-migration-notes.md"]).select, []); // not a migration, whatever its name
});

test("a first run by reach: a server file selects the tests that reach it, through helpers, and no others", () => {
  assert.deepEqual(byReach(["server/src/feature.mjs"]).select, ["server/tests/a.test.mjs"]);
  assert.deepEqual(byReach(["server/src/deep.mjs"]).select, ["server/tests/b.test.mjs", "server/tests/c.test.mjs"]); // through helpers/shared.mjs
  assert.deepEqual(byReach(["server/tests/helpers/shared.mjs"]).select, ["server/tests/b.test.mjs", "server/tests/c.test.mjs"]);
  assert.deepEqual(byReach(["server/tests/fixtures/c-data.json"]).select, ["server/tests/c.test.mjs"]); // a data file a test names
  assert.deepEqual(byReach(["server/src/feature.mjs", "server/src/other.mjs"]).select, ["server/tests/a.test.mjs", "server/tests/b.test.mjs"]);
  assert.match(byReach(["server/src/feature.mjs"]).reason, /selected 1 of 4 test file\(s\)/);
});

test("a first run by reach: a file nothing imports selects its own tests, and a source nothing imports selects none", () => {
  assert.deepEqual(byReach(["server/tests/d.test.mjs"]).select, ["server/tests/d.test.mjs"]); // a test is imported by nobody: it selects itself
  assert.deepEqual(byReach(["server/tests/new-one.test.mjs"], { files: [...SERVER_TESTS, "server/tests/new-one.test.mjs"] }).select, ["server/tests/new-one.test.mjs"]);
  assert.deepEqual(byReach(["server/src/lonely.mjs"], { changed: ["app/src/page.tsx"] }).select, []); // not touched: nothing to select by
  assert.deepEqual(byReach(["server/src/lonely.mjs"], { step: { id: "tests:server", retry: "test", select: "reach" } }).select, []); // a step that says no directory: the old rule, a source nothing imports selects none
});

test("#8524: source in the step's own tree that nothing imports or names runs the whole step (what loads it cannot be read); a test of another step, a file of another tree and a file something names do not", () => {
  const d = byReach(["server/src/lonely.mjs"]);
  assert.equal(d.select, null, JSON.stringify(d));
  assert.match(d.reason, /server\/src\/lonely\.mjs is source in the step's own tree that nothing imports or names/);
  const tree = { ...REACH_TREE, "server/tests/other-step.test.mjs": "", "scripts/hand-run.mjs": "", "app/src/entry.tsx": "" };
  const graph = buildGraph({ files: Object.keys(tree), read: (p) => tree[p] ?? null, isProse: (f) => f.endsWith(".md") });
  assert.deepEqual(byReach(["server/tests/other-step.test.mjs"], { graph }).select, [], "a test nothing imports is a test of some step: not this step's concern");
  assert.deepEqual(byReach(["scripts/hand-run.mjs"], { graph }).select, [], "outside the step's tree");
  assert.deepEqual(byReach(["app/src/entry.tsx"], { graph }).select, [], "another package's source");
  assert.deepEqual(byReach(["server/src/feature.mjs"]).select, ["server/tests/a.test.mjs"], "a file a test imports is read");
  const typedTree = { ...REACH_TREE, "server/src/shapes.mts": "export type S = 1;", "server/src/uses-shapes.mjs": 'import type { S } from "./shapes.mts";' };
  const typedGraph = buildGraph({ files: Object.keys(typedTree), read: (p) => typedTree[p] ?? null, isProse: (f) => f.endsWith(".md") });
  assert.deepEqual(byReach(["server/src/shapes.mts"], { graph: typedGraph }).select, [], "a file only a type-only import names is named: it is erased at run time, no test loads it");
  // the project's directory is its word: "." (the root) names no tree, so no file of it is "in the step's own tree"
  assert.deepEqual(byReach(["server/src/lonely.mjs"], { step: { id: "tests:server", retry: "test", select: "reach", dir: "." } }).select, []);
  // the same on a re-check: a step that selects again as a first run reads the same rule
  const re = decide({ step: { id: "tests:server", retry: "test", select: "reach", dir: "server", isTest: () => false }, earlier: true, passed: SERVER_TESTS, incoming: [], changed: ["server/src/lonely.mjs"], deleted: [], files: SERVER_TESTS, graph: REACH_GRAPH, preload: [], ...RUN });
  assert.equal(re.carry, true, "nothing came in: the whole pass of the earlier tree stands for the same bytes");
  assert.deepEqual(re.universe, SERVER_TESTS, "the universe is the whole step, not the empty selection");
});

test("a first run by reach: a deleted file reaches the files that still name it; a renamed one is its old path deleted", () => {
  assert.deepEqual(byReach([], { deleted: ["server/src/gone.mjs"] }).select, ["server/tests/d.test.mjs"]);
  assert.deepEqual(byReach(["server/src/gone.mjs"]).select, ["server/tests/d.test.mjs"]); // the same when the caller lists it among the changed
  assert.equal(byReach([], { deleted: null }).select, null);
});

test("a first run by reach: what the text cannot follow runs the whole step, naming why", () => {
  const whole = (changed, over, re) => {
    const d = byReach(changed, over);
    assert.equal(d.select, null, JSON.stringify(d));
    assert.match(d.reason, re);
  };
  whole(["server/tests/helpers/preload.mjs"], {}, /what the step runs under/); // a preloaded file
  whole(["server/src/booted.mjs"], {}, /what the step runs under/); // what a preloaded file imports
  // one step further is the rule's named residual: judged by reach, like any file
  assert.deepEqual(byReach(["server/src/booted-deep.mjs"]).select, []);
  whole([".spire/land.json"], {}, /read by the gate's own runner/);
  whole([".node-version"], {}, /pins the toolchain/);
  whole(["mise.toml"], {}, /pins the toolchain/); // named by a test, yet a pin
  whole(["server/db/001.sql"], {}, /sql/);
  whole(["server/db/migrations/x.ts"], {}, /migration/);
  whole(["server/.env.test"], {}, /environment/);
  whole(["server/scripts/pins.json"], {}, /data file no source names/);
  assert.deepEqual(byReach(["server/package.json"]).select, SERVER_TESTS); // a package file reaches every test under it
  whole([], {}, /change is empty/); // an untouched tree checked as its own base (the Workroom's check) runs every test, never none
  whole(["server/src/feature.mjs"], { graph: null }, /cannot be read/);
  whole(["server/src/feature.mjs"], { changed: null }, /cannot be read/);
  whole(["server/src/feature.mjs"], { files: null }, /no test list/);
  // another package's lock file reaches no test of this one
  assert.deepEqual(byReach(["app/package-lock.json"]).select, []);
  assert.throws(() => byReach(["a"], { graph: undefined }), /missing input graph/);
  assert.throws(() => byReach(["a"], { preload: undefined }), /missing input preload/);
});

test("a first run by reach: a usable coverage map only ADDS; a new file is no reason to run the whole step; a step that is not marked is untouched", () => {
  const map = mapOf({ files: ["server/src/other.mjs"], tests: { "server/tests/a.test.mjs": [0], "server/tests/b.test.mjs": [], "server/tests/c.test.mjs": [] } }); // d is unknown to the map
  const d = byReach(["server/src/feature.mjs"], { map });
  assert.deepEqual(d.select, ["server/tests/a.test.mjs", "server/tests/d.test.mjs"]); // the graph's a; the map adds d (a test it does not know)
  assert.equal(d.by, "reach, coverage map");
  assert.deepEqual(byReach(["server/src/other.mjs"], { map }).select, ["server/tests/a.test.mjs", "server/tests/b.test.mjs", "server/tests/d.test.mjs"]); // a: the map's coverage; b: the graph's
  // an unusable map changes nothing
  assert.deepEqual(byReach(["server/src/feature.mjs"], { map: parseMap({ text: null, now: NOW, maxAgeHours: 24 }) }).select, ["server/tests/a.test.mjs"]);
  // a step with no `select`: the old rule (no map: the whole step), whatever graph is given
  const plain = decide({ step: { id: "tests:guards", retry: "test" }, earlier: false, incoming: [], changed: ["server/src/feature.mjs"], deleted: [], files: SERVER_TESTS, graph: REACH_GRAPH, preload: [], ...RUN });
  assert.equal(plain.select, null);
  assert.match(plain.reason, /no earlier pass/);
});

test("mutants of the reach rule are caught", async () => {
  const load = async (text, name) => {
    const dir = mkdtempSync(join(tmpdir(), "reachmutant-"));
    try {
      for (const f of ["carry.mjs", "reach.mjs"]) writeFileSync(join(dir, f), f === "carry.mjs" ? text : readFileSync(join(HERE, f), "utf8"));
      return await import(pathToFileURL(join(dir, "carry.mjs")).href + `?${name}`);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  };
  const ask = (m, changed, over = {}) => m.decide({ step: { id: "tests:server", retry: "test", select: "reach" }, earlier: false, incoming: [], changed, deleted: [], files: SERVER_TESTS, graph: REACH_GRAPH, preload: ["server/tests/helpers/preload.mjs"], ...RUN, ...over });
  const mutate = async (from, to, probe) => {
    assert.ok(SOURCE.includes(from), `the rule text moved: ${from}`);
    const m = await load(SOURCE.replace(from, to), "m" + Math.random().toString(36).slice(2));
    assert.notDeepEqual(probe(m), probe({ decide }), `mutant survived: ${from}`);
  };
  await mutate("if (!changed.length) return full(", "if (false) return full(", (m) => ask(m, []).select);
  await mutate("if (hit) return full(", "if (false) return full(", (m) => ask(m, ["server/src/booted.mjs"]).select);
  await mutate("if (roots.includes(from)) every.add(to);", "", (m) => ask(m, ["server/src/booted.mjs"]).select);
  await mutate("if (PIN_FILES.includes(posix.basename(f))) return", "if (false) return", (m) => ask(m, ["mise.toml"]).select);
  await mutate('if (kind === "sql" || kind === "migration" || kind === "environment") return', "if (false) return", (m) => ask(m, ["server/db/001.sql"]).select);
  await mutate("if (readByRunner(input, f)) return full(", "if (false) return full(", (m) => ask(m, [".spire/land.json"]).select);
  await mutate("!(graph.importers.get(f)?.size > 0)) return full(", "false) return full(", (m) => ask(m, ["server/scripts/pins.json"]).select);
  await mutate("files.filter((t) => byGraph.has(t) || mapAdds(t))", "files.filter((t) => mapAdds(t))", (m) => ask(m, ["server/src/feature.mjs"]).select);
  const askNear = (m, changed) => ask(m, changed, { step: { id: "tests:pg", retry: "test", select: "reach", deepFor: matcher(["server/src/deep.mjs"]) } });
  await mutate("for (const i of graph.importers.get(f) ?? []) if (files.includes(i)) byGraph.add(i);", "", (m) => askNear(m, ["server/src/other.mjs"]).select);
  await mutate("const near = hubs ? changed.filter((f) => !hubs(f)) : [];", "const near = [];", (m) => askNear(m, ["server/src/other.mjs"]).select);
  await mutate("const deep = hubs ? changed.filter((f) => hubs(f)) : changed;", "const deep = changed;", (m) => ask(m, ["server/src/deep.mjs"], { step: { id: "tests:pg", retry: "test", select: "reach", deepFor: matcher(["server/src/feature.mjs"]) } }).select); // deep.mjs is not a hub here: only its direct test importers
});

test("the toolchain pin files the reach rule names are the ones versions.mjs reads", () => {
  const versions = readFileSync(join(HERE, "versions.mjs"), "utf8");
  for (const f of PIN_FILES) assert.ok(versions.includes(f), `${f} is not read by versions.mjs`);
});

test("a first run by reach with `deepFor`: a file the project names reaches the whole step, any other file only the tests that edit or directly import it; the map adds only for a deep file", () => {
  const deepFor = matcher(["server/src/deep.mjs", "server/db/**"]);
  const near = (changed, over = {}) => byReach(changed, { step: { id: "tests:pg", retry: "test", select: "reach", deepFor }, ...over });
  // deep: as ever, through helpers
  assert.deepEqual(near(["server/src/deep.mjs"]).select, ["server/tests/b.test.mjs", "server/tests/c.test.mjs"]);
  // not deep: feature.mjs is imported by a.test.mjs directly; other.mjs by b.test.mjs directly
  assert.deepEqual(near(["server/src/feature.mjs"]).select, ["server/tests/a.test.mjs"]);
  assert.deepEqual(near(["server/src/other.mjs"]).select, ["server/tests/b.test.mjs"]);
  assert.match(near(["server/src/other.mjs"]).reason, /0 deep .*, 1 near/);
  // a test the change edits runs, a source whose only importer is a helper selects no test of its own (the release-candidate run has it)
  assert.deepEqual(near(["server/tests/d.test.mjs"]).select, ["server/tests/d.test.mjs"]);
  assert.deepEqual(near(["server/src/booted-deep.mjs"]).select, []);
  // both kinds in one change: the union
  assert.deepEqual(near(["server/src/deep.mjs", "server/src/feature.mjs"]).select, ["server/tests/a.test.mjs", "server/tests/b.test.mjs", "server/tests/c.test.mjs"]);
  // without the project's deepFor nothing changes: the whole reach
  assert.deepEqual(byReach(["server/src/deep.mjs", "server/src/feature.mjs"]).select, ["server/tests/a.test.mjs", "server/tests/b.test.mjs", "server/tests/c.test.mjs"]);
  // the map: it knows b.test.mjs executes feature.mjs, a fact the near rule leaves to the release-candidate run; a deep file still gets the map's tests
  const map = parseMap({ text: JSON.stringify({ meta: { built: "2026-11-13T11:30:00Z", commit: "abc1234" }, files: ["server/src/feature.mjs", "server/src/deep.mjs"], tests: { "server/tests/a.test.mjs": [0], "server/tests/b.test.mjs": [0, 1], "server/tests/c.test.mjs": [1] } }), now: NOW, maxAgeHours: 24 });
  assert.equal(map.ok, true);
  assert.deepEqual(near(["server/src/feature.mjs"], { map }).select, ["server/tests/a.test.mjs"]); // the map says b executes feature.mjs: not added
  assert.deepEqual(byReach(["server/src/feature.mjs"], { map }).select, ["server/tests/a.test.mjs", "server/tests/b.test.mjs", "server/tests/d.test.mjs"]); // without deepFor it is (and d, a test the map does not know)
});
