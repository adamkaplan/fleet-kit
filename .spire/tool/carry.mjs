// carry.mjs: the ONE decision whether a test step that passed on an earlier tree of the same change is carried to the rebased tree, and which of its test files re-run. Pure: lists of paths in, a decision out.
// A passed TEST step (retry "test") is carried when
//   1. the earlier pass is known and what came in (the files that differ between the earlier tree and this one) can be read,
//   2. the change's own files can be read and none of them is among what came in (an overlap: the rebase merged both; run everything),
//   3. what the incoming files REACH can be read (the graph, reach.mjs; null: it could not be built, run everything).
// THE GATE'S OWN RUNNER is the one thing every test step runs under: an incoming file under the project's `.spire/` folder that the runner reaches (its land.json, box.json, the tool files it imports) can change how any step runs, so the step runs whole; a file under `.spire/` the runner does not reach (PIN, the principles, the reviewers, the council, the tool's own tests) is judged like any other file. A runner that is not in the graph (not tracked) reaches every file under `.spire/`: unsure runs more.
// A step marked `select: "reach"` is carried too, over the tests it has to pass on THIS tree: the first run's selection on this tree (the tests the change reaches), of which the ones that passed on the earlier tree (the earlier run's own list, kept by run-steps.mjs) and that no incoming file reaches are carried; the rest run. A step that cannot be carried (an overlap, an unreadable diff or graph, the gate's own runner) is selected again as a first run, never run whole for that.
// What is carried is every test file the incoming files do not reach. THERE IS NO TABLE of shared inputs (.spire/, lock files, helpers, fixtures): a file is a shared input exactly when many tests reach it, and then many tests re-run; a file nothing reaches re-runs nothing.
// The rest (`rerun`) is run again: the test files the change ITSELF adds or edits and every test file that reaches a file that came in (a test file that came in reaches itself). What "reaches" means is reach.mjs.
// A step marked `select: "reach"` is narrowed only inside run-steps.mjs's state directory (a run with none runs it whole), and only by the mark in the project's land.json: `.spire/**` is a path the project holds for a person, so a builder cannot narrow its own gate in the change that lands it.
// A step that cannot be narrowed (files: null) runs whole. Unsure (an unreadable diff or graph) runs more, never less.
// A step that is NOT a test step (typecheck, lint, style, architecture, a build, a browser job, a ratchet) is carried over a rebase only when the project says what it READS: `reads`, globs in land.json, the BOUNDARY of its inputs (a directory it scans, the scripts it runs, a config, the lock files). Without `reads` it ALWAYS runs again (nothing says what it reads). Its inputs are exactly:
//   - the files its `reads` globs match (nothing is followed from them: the project names the whole boundary; readsGap below is the check a project's own test runs, that no non-test file inside the boundary imports one outside it),
//   - the toolchain pins (.node-version ...), which change what every process of the tree runs under,
//   - the gate's own runner's inputs (an incoming `.spire/` file the runner reads), as for a test.
// SOUNDNESS: the step passed on the tree T of this change (the same patch-id) and ran there; the rebased tree T' differs from T exactly in the incoming files, none of which the change touches (an overlap re-runs); when no incoming file is an input of the step, every input of the step has the same bytes in T and T', so the step's result on T' is its result on T. It is the overlap rule of the tests with "the step reads" in place of "the test reaches". What the text cannot see (a path built at run time, a directory the globs do not name) is the project's to name; a project that names it wrongly carries a pass it should not: the same residual risk as for tests, and the release-candidate run before every roll is the net. The FINAL tree is the one that is gated: a step whose inputs came in runs on it, a step carried has the pass of a tree that is the final one in every file it reads, and a red on it lands nothing.
// Steps that wait for one another (`after`) are carried whole or not at all (run-steps.mjs): one reads the files the other makes.
// What stays re-run: a step with no `reads`; a step an incoming file of whose inputs matches; an overlap; any unreadable diff or graph; the gate's own runner's inputs; a toolchain pin; the first run of a change.
// THE FIRST RUN selects by REACH (a step the project marks `"select": "reach"`): the step runs the test files the change ITSELF adds or edits and every test file that reaches (reach.mjs, transitively) a file the change touches, deletions included (a deleted file's importers still name it); a file nothing reaches selects nothing, and a step no test of which is selected does not run at all (selected 0 of N). The step runs WHOLE instead when what the text cannot see is touched:
//   - the gate's own runner reads it (under `.spire/`, as on a re-check), or a file every test of the step runs under changed (`preload`, plan.mjs runnerInputs: the files its command names (its runner, its --import files), its list command and its environment's recipe name, the files the step's `runsUnder` globs of land.json name (the project's own test harness), and what those load or name directly; what they load only further down is judged by reach like any other file: the residual risk of this rule);
//   - a pin of the toolchain (.node-version, .nvmrc, mise.toml, .tool-versions), SQL, a migration, an environment file;
//   - a data file (not code, not prose) that no source names: only a path built at run time could read it;
//   - the graph, the change's own files or the step's test list cannot be read (unsure runs more, never less).
// WHAT THE GRAPH DOES NOT SEE: what reach.mjs names (a path built at run time, a directory read as text), and the files a runner's reset helper imports in every worker process: a coverage map attributes them to the first test of a process, no test depends on them (a break there fails the group, which re-runs alone and passes), and taking them as an input of every test would select nearly every test. The measurement, and the rules the graph follows, are LAYOUT.md's and reach.mjs's.
// `deepFor` (optional, with `select: "reach"`, globs in land.json): the files the WHOLE step must see (the project's database layer, which every test loads). A change to such a file selects by full reach, as above; a change to any other file selects only the test files that edit it or import it directly (a test that reaches it only through a hub is left to the project's release-candidate run), and the coverage map adds nothing for it. The reason line says how many files of each kind.
// `deepFor` reads the same on what came in over a rebase: an incoming file outside the named files re-runs only the tests that edit or directly import it (main's own landing ran it under the same rule), an incoming file the globs name, or a lock file, re-runs by full reach.
// A step that is not marked (a guard that scans the tree, the doc tests) has no reach: it runs whole, as before, unless a usable coverage map narrows it (below).
// THE COVERAGE MAP (optional input `map`, from parseMap): `test file -> source files it executed`, built elsewhere and NEVER committed to main (fetched from a git ref of origin, and used only when built at an ancestor of the base; see LAYOUT.md). With a usable map:
//   FIRST RUN (no earlier pass), a step that is not marked `select: "reach"`: a test step runs only the test files whose recorded coverage reaches a file the change touches, plus the test files the change itself adds or edits. The FULL area runs instead when a touched file is
//   SQL, a migration, configuration, environment (a kind the map cannot see), is deleted, or is a code file the map does not know (new or unknown). Without a usable map (missing, corrupt, too old) the full area runs and the reason names the map.
//   FIRST RUN of a step marked `select: "reach"`: the graph is the base and the map only ADDS (the tests whose recorded coverage reaches a touched file, and the tests it does not know); a new or unknown file is no reason to run the whole step there.
//   RE-CHECK (an earlier pass): the overlap rule above stays the base and is never overridden. The graph's reach is the base for every incoming file; a usable map only ADDS the tests whose recorded coverage reaches an incoming file (a subprocess the graph cannot see) and the tests it does not know. The map never removes a re-run.
// There are no default values: a missing input throws.
import { posix } from "node:path";
import { PIN_FILES, isCode, isDependencyFile, reaching } from "./reach.mjs";

const need = (name, v, type) => {
  const ok = type === "array" ? Array.isArray(v) : typeof v === type;
  if (!ok) throw new Error(`carry: missing input ${name}`);
};

/**
 * decide({ step: {id, retry, select?, reads?: (path) => boolean, made from the globs of land.json by the caller}, earlier, incoming, changed, files, graph, map?, deleted? }) -> { carry: boolean, reason: string, rerun: string[], select: string[] | null }
 *   select: when not null, the step runs ONLY these test files (chosen by reach or by the map; empty only for a step marked `select: "reach"`: no test of it reaches the change, so it does not run; the map alone never selects nothing: that step runs whole)
 *   by: with select, how it was chosen ("reach" or "reach, coverage map" or "coverage map")
 *   map: parseMap's answer (absent: no map is in play); deleted: the change's deleted paths (absent: none; null: unreadable, the full area runs)
 *   files: the step's test files (what its list command gives), or null when the step cannot be narrowed
 *   rerun: when carried, the step's test files that run again (the change's own, the incoming ones, the ones that reach what came in): nothing else of the step does
 *   earlier: true when the same change passed this step on an earlier tree (the caller read the record); passed: for a step marked `select: "reach"`, the test files that pass was of (absent: the whole step)
 *   universe: with carry, the test files the pass stands for after the rerun (the whole step, or the ones a step marked `select: "reach"` has to pass on this tree)
 *   incoming / changed: arrays of paths, or null when they could not be read
 *   graph: buildGraph's answer for this tree, or null when it could not be built (a re-check needs it)
 *   runner / own: the path of the gate's runner (run-steps.mjs) and the project's `.spire/` folder as a prefix ("/"-ended)
 *   graph (a first run of a step marked `select: "reach"` needs it too), preload: the tracked files every test of the step runs under (plan.mjs runnerInputs)
 */
export const decide = (input) => {
  need("input", input, "object");
  const { step, earlier, incoming, changed, files } = input;
  if (files === undefined) throw new Error("carry: missing input files");
  need("step", step, "object");
  need("step.id", step.id, "string");
  need("earlier", earlier, "boolean");
  if (files !== null) need("files", files, "array");
  const mapped = input.map === undefined || input.map === null ? undefined : input.map;
  if (step.retry !== "test") return carryCheck({ step, earlier, incoming, changed, input });
  const deleted = input.deleted === undefined ? [] : input.deleted;
  if (!earlier) return firstRun({ step, mapped, changed, deleted, files, input });
  // a step marked `select: "reach"` that cannot be carried is selected again, as a first run on this tree (never more than the first run runs, never less); the others run whole
  const marked = step.select === "reach";
  const stop = (reason) => {
    if (!marked) return { carry: false, reason, rerun: [], select: null };
    const first = firstRun({ step, mapped, changed, deleted, files, input });
    return { ...first, reason: `${reason}; ${first.reason}` };
  };
  if (incoming === null) return stop("what came in cannot be read");
  if (changed === null) return stop("the change's own files cannot be read");
  need("incoming", incoming, "array");
  need("changed", changed, "array");
  const own_ = new Set(changed);
  const overlap = incoming.filter((f) => own_.has(f));
  if (overlap.length) return stop(`${overlap[0]} came in and the change touches it too`);
  if (input.graph === undefined) throw new Error("carry: missing input graph");
  if (input.graph === null) return stop("what the files that came in reach cannot be read");
  if (files === null) return stop("the step has no test list to narrow: it runs whole");
  need("runner", input.runner, "string");
  need("own", input.own, "string");
  const gate = incoming.find((f) => readByRunner(input, f));
  if (gate) return stop(`${gate} is read by the gate's own runner (${input.runner}): it can change how every step runs`);
  // the tests this tree has to pass: every test of the step, or (a step marked `select: "reach"`) the ones the change reaches on THIS tree, as a first run chooses them; of those only the ones that passed on the earlier tree (`passed`: absent = the whole step did) can be carried
  const first = marked ? firstRun({ step, mapped, changed, deleted, files, input }) : undefined;
  if (first && first.select && first.select.length === 0) return first; // the change reaches none of its tests: it does not run
  const universe = first?.select ?? files;
  const have = Array.isArray(input.passed) ? new Set(input.passed) : undefined;
  const rest = universe.filter((f) => !own_.has(f));
  // `deepFor` (a step marked `select: "reach"`) reads the same on what came in as on the change: an incoming file outside the project's hubs re-runs only the tests that edit or directly import it (a test that reaches it only through a hub is left to the release-candidate run, as it is for the change itself; main's own landing ran it under the same rule); a lock file is deep always
  const hubs = marked && typeof step.deepFor === "function" ? step.deepFor : null;
  const deepIn = hubs ? incoming.filter((f) => hubs(f) || isDependencyFile(f)) : incoming;
  const nearIn = hubs ? incoming.filter((f) => !hubs(f) && !isDependencyFile(f)) : [];
  const usable = mapped !== undefined && mapped.ok && (!hubs || deepIn.length > 0);
  const byGraph = reaching(input.graph, rest, deepIn);
  for (const f of nearIn) {
    if (rest.includes(f)) byGraph.add(f);
    for (const i of input.graph.importers.get(f) ?? []) if (rest.includes(i)) byGraph.add(i);
  }
  const more = rest.filter((t) => byGraph.has(t) || (have && !have.has(t)) || (usable && (!mapped.tests.has(t) || reachingMap(mapped, t, deepIn))));
  const said = `${incoming.length} file(s) came in, none the change touches`;
  const how = mapped === undefined ? "" : usable ? `; coverage map age ${mapped.age}` : `; ${mapped.reason}`;
  return { carry: true, reason: `${said}${how}${hubs ? `; ${deepIn.length} deep, ${nearIn.length} near` : ""}: ${more.length} of ${rest.length} other test file(s) ${marked ? "the change reaches " : ""}reach them${have ? " or did not pass before" : ""}`, rerun: [...universe.filter((f) => own_.has(f)), ...more], select: null, universe };
};

// A step that is not a test step: carried when it passed on an earlier tree of this change, the project says what it reads (`step.reads`, a predicate made from land.json's globs by the caller) and no file that came in is one of its inputs (the header). Else it runs again, naming why.
const carryCheck = ({ step, earlier, incoming, changed, input }) => {
  const again = (reason) => ({ carry: false, reason, rerun: [], select: null });
  if (typeof step.reads !== "function") return again("not a test step: it always runs again");
  if (!earlier) return again("no earlier pass of this change");
  if (incoming === null) return again("what came in cannot be read");
  if (changed === null) return again("the change's own files cannot be read");
  need("incoming", incoming, "array");
  need("changed", changed, "array");
  const own_ = new Set(changed);
  const overlap = incoming.find((f) => own_.has(f));
  if (overlap) return again(`${overlap} came in and the change touches it too`);
  if (input.graph === undefined) throw new Error("carry: missing input graph");
  if (input.graph === null) return again("what the files that came in reach cannot be read");
  need("runner", input.runner, "string");
  need("own", input.own, "string");
  const gate = incoming.find((f) => readByRunner(input, f));
  if (gate) return again(`${gate} is read by the gate's own runner (${input.runner}): it can change how every step runs`);
  const pin = incoming.find((f) => PIN_FILES.includes(posix.basename(f)));
  if (pin) return again(`${pin} pins the toolchain every step runs under`);
  const read = incoming.find((f) => step.reads(f));
  if (read) return again(`${read} came in and the step reads it`);
  return { carry: true, reason: `${incoming.length} file(s) came in, none the step reads`, rerun: [], select: null };
};

/**
 * readsGap({ graph, reads, isTest }) -> [{ file, imports }]: the files inside a step's `reads` boundary (not tests) that import (reach.mjs: imports, a name in a string) a tracked file OUTSIDE it: where the project's boundary is not closed, so a change to the outside file would
 * not re-run the step. For the project's own test (the tool cannot know a project's tests): a step's `reads` is checked closed, or the step says why not.
 *   graph: buildGraph's answer; reads: the predicate of the step's globs; isTest: the predicate of the project's test paths
 */
export const readsGap = ({ graph, reads, isTest }) => {
  need("graph", graph, "object");
  need("reads", reads, "function");
  need("isTest", isTest, "function");
  const gaps = [];
  for (const [to, set] of graph.importers) {
    if (reads(to)) continue;
    for (const from of set) if (reads(from) && !isTest(from) && !graph.isProse(from)) gaps.push({ file: from, imports: to });
  }
  return gaps.sort((x, y) => (x.file + x.imports < y.file + y.imports ? -1 : 1));
};

// a file under the project's `.spire/` folder that the gate's own runner reads (or any, when the runner is not in the graph: unsure runs more)
const readByRunner = (input, f) => f.startsWith(input.own) && (!input.graph.files.has(input.runner) || reaching(input.graph, [input.runner], [f]).size > 0);

const KIND_TESTS = [
  ["sql", (f) => /\.sql$/i.test(f)],
  ["migration", (f) => /migrat/i.test(f) || /(^|\/)db\//.test(f)],
  ["environment", (f) => /^\.env|\.env(\.|$)/.test(posix.basename(f))],
  ["configuration", (f) => /\.(json|ya?ml|toml|ini|conf)$/i.test(f) || /^(Dockerfile|Makefile)/.test(posix.basename(f)) || /(^|\/)docker\//.test(f) || /^\.[^/]*rc/.test(posix.basename(f)) || /^\.(github|githooks)\//.test(f)],
];

/** The kind of input a touched file is that no coverage map can see (sql, migration, configuration, environment), or "" for code. */
export const inputKind = (f) => {
  need("f", f, "string");
  for (const [kind, is] of KIND_TESTS) if (is(f)) return kind;
  return "";
};

/**
 * parseMap({ text, now, maxAgeHours }) -> { ok: true, tests: Map<test, Set<source>>, covered: Set<source>, age: "12m"|"3h", commit } | { ok: false, reason }
 *   text: the map file's text, or null when there is none. Format (the one indexed form): { meta: { built, commit }, files: [path], tests: { test: [index] } }.
 */
export const parseMap = ({ text, now, maxAgeHours }) => {
  need("now", now, "number");
  need("maxAgeHours", maxAgeHours, "number");
  if (text === null) return { ok: false, reason: "no coverage map" };
  let m;
  try {
    m = JSON.parse(text);
  } catch {
    return { ok: false, reason: "coverage map unreadable" };
  }
  const built = typeof m?.meta?.built === "number" ? m.meta.built : Date.parse(m?.meta?.built);
  if (!m || typeof m.tests !== "object" || m.tests === null || Array.isArray(m.tests) || Number.isNaN(built)) return { ok: false, reason: "coverage map unreadable" };
  const tests = new Map();
  for (const [t, list] of Object.entries(m.tests)) {
    if (!Array.isArray(list)) return { ok: false, reason: "coverage map unreadable" };
    const paths = list.map((i) => (Array.isArray(m.files) && Number.isInteger(i) ? m.files[i] : undefined));
    if (paths.some((p) => typeof p !== "string")) return { ok: false, reason: "coverage map unreadable" };
    tests.set(t, new Set(paths));
  }
  const ageMs = now - built;
  if (ageMs < 0) return { ok: false, reason: "coverage map built in the future" };
  const mins = Math.floor(ageMs / 60000);
  const age = mins < 60 ? `${mins}m` : `${Math.floor(mins / 60)}h`;
  if (ageMs > maxAgeHours * 3600000) return { ok: false, reason: `coverage map too old (${age}, at most ${maxAgeHours}h)` };
  return { ok: true, tests, covered: new Set([...tests.values()].flatMap((s) => [...s])), age, commit: String(m.meta.commit ?? "unknown") };
};

/** Whether the recorded coverage of test `t` reaches any of `touched`. */
const reachingMap = (map, t, touched) => touched.some((f) => map.tests.get(t)?.has(f));

// The first run of a test step: the tests the change reaches (a step marked `select: "reach"`), the tests the map says it reaches, or the whole area.
const firstRun = ({ step, mapped, changed, deleted, files, input }) => {
  if (step.select === "reach") return reachRun({ step, mapped, changed, deleted, files, input });
  const full = (reason) => ({ carry: false, reason, rerun: [], select: null });
  if (mapped === undefined) return full("no earlier pass of this change");
  if (!mapped.ok) return full(`${mapped.reason}: the full area runs`);
  if (changed === null) return full("the change's own files cannot be read: the full area runs");
  if (deleted === null) return full("the change's deleted files cannot be read: the full area runs");
  need("changed", changed, "array");
  if (files === null) return full("the step has no test list to narrow: it runs whole");
  const own_ = new Set(changed);
  for (const f of [...changed, ...deleted]) {
    const kind = inputKind(f);
    if (kind) return full(`coverage map cannot see a ${kind} (${f}): the full area runs`);
  }
  for (const f of deleted) return full(`deleted file: ${f}: the full area runs`);
  for (const f of changed) {
    if (!mapped.covered.has(f) && !mapped.tests.has(f) && !files.includes(f)) return full(`new or unknown file: ${f}: the full area runs`);
  }
  const select = files.filter((t) => own_.has(t) || !mapped.tests.has(t) || reachingMap(mapped, t, changed));
  if (!select.length) return full(`coverage map age ${mapped.age}: no test of the step reaches the change: the full area runs`);
  return { carry: false, reason: `coverage map age ${mapped.age}: ${select.length} of ${files.length} test file(s) reach the change`, rerun: [], select, by: "coverage map" };
};

// What every test of a step runs under: its `roots` (the files its command, its list and its environment name, and the project's `runsUnder` globs) and what they load or name DIRECTLY (one step, no further): a change to any of them can change every test.
const runsUnder = (graph, roots) => {
  const every = new Set(roots);
  for (const [to, set] of graph.importers) for (const from of set) if (roots.includes(from)) every.add(to);
  return every;
};

// A first run by REACH: the whole step when something the text of the tree cannot follow is touched (the header lists them), else the test files the change reaches, (a test file it touches reaches itself).
const reachRun = ({ step, mapped, changed: touched, deleted, files, input }) => {
  const full = (reason) => ({ carry: false, reason: `${reason}: the whole step runs`, rerun: [], select: null });
  if (touched === null) return full("the change's own files cannot be read");
  if (deleted === null) return full("the change's deleted files cannot be read");
  const changed = [...new Set([...touched, ...deleted])]; // a deleted file is touched too: the files that still name it reach it
  if (files === null) return full("the step has no test list to narrow");
  need("changed", touched, "array");
  if (!changed.length) return full("the change is empty (an untouched tree checked as its own base): there is nothing to select by");
  if (input.graph === undefined) throw new Error("carry: missing input graph");
  if (input.graph === null) return full("what the change reaches cannot be read");
  need("runner", input.runner, "string");
  need("own", input.own, "string");
  need("preload", input.preload, "array");
  const graph = input.graph;
  for (const f of changed) {
    if (readByRunner(input, f)) return full(`${f} is read by the gate's own runner (${input.runner}): it can change how every step runs`);
    if (graph.isProse(f)) continue; // only people read prose: no test reaches it (a name in a path such as "migration" makes it no kind of input)
    if (PIN_FILES.includes(posix.basename(f))) return full(`${f} pins the toolchain every test runs under`);
    const kind = inputKind(f);
    if (kind === "sql" || kind === "migration" || kind === "environment") return full(`a ${kind} (${f}) is read by no import`);
    // data (not code, not prose, not a test of this step) that no source names: only a path built at run time could read it
    if (!isCode(f) && !graph.isProse(f) && !files.includes(f) && !isDependencyFile(f) && !(graph.importers.get(f)?.size > 0)) return full(`${f} is a data file no source names`);
  }
  const under = runsUnder(graph, input.preload);
  const hit = changed.find((f) => under.has(f));
  if (hit) return full(`${hit} is what the step runs under (its command, its list, its environment's recipe or a file the project says every test runs under)`);
  // `deepFor`: the rule is the header of this file
  const hubs = typeof step.deepFor === "function" ? step.deepFor : null; // (path) => boolean, made from the project's globs by the caller
  const deep = hubs ? changed.filter((f) => hubs(f)) : changed;
  const near = hubs ? changed.filter((f) => !hubs(f)) : [];
  const byGraph = reaching(graph, files, deep); // a test file the change touches reaches itself
  for (const f of near) {
    if (files.includes(f)) byGraph.add(f);
    for (const i of graph.importers.get(f) ?? []) if (files.includes(i)) byGraph.add(i);
  }
  const usable = mapped !== undefined && mapped.ok && (!hubs || deep.length > 0);
  const mapAdds = (t) => usable && (!mapped.tests.has(t) || reachingMap(mapped, t, deep));
  const select = files.filter((t) => byGraph.has(t) || mapAdds(t));
  const how = (hubs ? `; ${deep.length} deep (reached through every import), ${near.length} near (their own and their direct importers' tests)` : "") + (usable ? `; the coverage map (age ${mapped.age}) added ${select.filter((t) => !byGraph.has(t)).length}` : "");
  return { carry: false, reason: `selected ${select.length} of ${files.length} test file(s): ${changed.length} changed file(s)${how}`, rerun: [], select, by: usable ? "reach, coverage map" : "reach" };
};
