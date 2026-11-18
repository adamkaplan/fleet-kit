#!/usr/bin/env node
// run-steps.mjs BASE: run the steps plan.mjs makes for the change (classification JSON on stdin: the areas it touches) in the current tree, at the same time on the host's cores (the independent ones), and print
// {"ran": [...], "areas": [...], "tests": N, "files": N, "notes": [...]} on stdout (tests: the runners' own count of the tests that ran). A failed step prints the tail of its output and "land: check FAILED: <step>" on stderr and exits 13.
// The environment recipes (a database, ...) and the install of dependencies are the project's (land.json), run here, never named here.
import { createHash } from "node:crypto";
import {
  existsSync,
  lstatSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { appendLine } from "./metrics.mjs";
import { join, relative } from "node:path";
import { layout } from "./layout.mjs";
import { loadConfig, matcher } from "./config.mjs";
import { decide, parseMap } from "./carry.mjs";
import { buildGraph } from "./reach.mjs";
import { patchId } from "./patch-id.mjs";
import { recipeEnv, stepEnv } from "./step-env.mjs";
import { planSteps, restrict, runnerInputs } from "./plan.mjs";
import { applyPins, spawn, spawnSync } from "./versions.mjs";
import { failedFilesOf } from "./failed-files.mjs";
import { availableBytes, cpuCores, limitSource, budgetBytes, hostShareBytes, memoryMaxBytes, groupCap, peakLine, planLine, scopeCommand, scopeWorks, uncappedLine, watchPeak } from "./workers.mjs";

applyPins(); // the tree under test's pinned tool versions: every step runs with them

// A landing never takes its host down: it runs under a MemoryMax of its share of the host's free memory (a systemd user scope) where the host can hold one; the run is started again inside it
// (same arguments plus a plain argv marker that says it is inside the scope, same stdin). Where the host cannot, the workers' count (below) is the only cap; the report line reads the MemoryMax the
// process's own cgroup really holds, so it says so.
const SCOPED = "--in-memory-scope";
if (!process.argv.includes(SCOPED)) {
  const bytes = hostShareBytes(availableBytes());
  if (bytes >= 256 * 1024 * 1024 && scopeWorks(bytes)) {
    const [cmd, ...args] = scopeCommand(bytes, [process.execPath, ...process.argv.slice(1, 3), SCOPED]);
    const r = spawnSync(cmd, args, { stdio: "inherit" });
    process.exit(r.status ?? 1);
  }
}

const cfg = await loadConfig();
const base = process.argv[2];
const info = JSON.parse(readFileSync(0, "utf8"));
const run = (dir, argv, env = process.env, timeout = 0) => {
  const r = spawnSync(argv[0], argv.slice(1), {
    cwd: dir,
    env,
    encoding: "utf8",
    maxBuffer: 1 << 28,
    timeout,
  });
  return { rc: r.status ?? 1, out: `${r.stdout ?? ""}${r.stderr ?? ""}` };
};
// A step runs in a CLEAN environment (step-env.mjs: the one place): a step that names an environment also gets what that recipe gives it, and the step's own variables come last: the unit tests need the ones CI's unit shards
// set (a database URL that is only checked for, a session secret), which no caller's own can stand in for.
const envOf = (s) => ({
  ...stepEnv(process.env, s.pass),
  ...(s.environment ? recipeEnv(process.env, cfg.environments[s.environment]) : {}),
  ...(s.env ?? {}),
});
const runAsync = (dir, argv, env = process.env, measure = false) =>
  new Promise((resolve) => {
    const c = spawn(argv[0], argv.slice(1), { cwd: dir, env });
    const watch = measure && c.pid ? watchPeak(c.pid) : undefined;
    let out = "";
    c.stdout.on("data", (d) => (out += d));
    c.stderr.on("data", (d) => (out += d));
    c.on("error", (e) => resolve({ rc: 1, out: String(e) }));
    c.on("close", (rc) => {
      watch?.stop();
      resolve({ rc: rc ?? 1, out, peak: watch?.peak() ?? 0 });
    });
  });
// the runner's own count: node's spec reporter ends with `ℹ tests N`, unittest with `Ran N tests`; a step that prints neither counts none
const countTests = (out) => {
  const node = [...out.matchAll(/^ℹ tests (\d+)$/gm)].at(-1)?.[1];
  return node !== undefined
    ? Number(node)
    : [...out.matchAll(/^Ran (\d+) tests?\b/gm)].reduce((n, m) => n + Number(m[1]), 0);
};
const secs = (t) => `${Math.round((Date.now() - t) / 1000)}s`;
const ran = [];
// the measurement record (metrics.mjs): each step's end as it happens, one line in the file the caller names (land: its temp directory; push: its run directory), so a run that is killed or ends red has said what it saw. Never an error, never slower than a write.
const noteStep = (id, state, took) => {
  if (!process.env.LAND_STEPS_OUT) return;
  try {
    appendLine(process.env.LAND_STEPS_OUT, { id, at: Date.now() / 1000, took_s: took, state });
  } catch {}
};
const fail = (label, dir, argv, out, exit = true) => {
  if (exit) noteStep(label, "red", null);
  process.stderr.write(`${out.trim().split("\n").slice(-40).join("\n")}\n`);
  process.stderr.write(
    `land: check FAILED: ${label} (${dir}: ${argv.join(" ")}); nothing lands\n`,
  );
  if (exit) {
    process.exit(13);
  }
};

// dependencies: the project's `checks.install` names the directories, the lock file, what an install makes there (`installed`: with none, the install runs every time) and the command. A tree installs ITS OWN, never links or
// copies another tree's (the submitter's): `npm ci` empties the directory it is given, and a step of the gate that runs it (a browser job that finds its lock file newer than what is installed) emptied the SUBMITTER's node_modules
// when the gate's tree was given a link to it, under the steps running beside it and under the next run. An install is seconds (the package cache), the price of a gate that cannot damage what it was started from.
// What this tool installs is stamped with the lock file it was made for (`.land-lock`, in the directory it made): a retry's tree, rebased onto a main that moved the lock file, is installed again; a directory with no stamp
// (the builder's own) is kept, and so is a link in the builder's own tree; the lander's scratch tree (LAND_TOP) drops a link.
const inst = cfg.checks?.install;
const lockId = (d) => {
  try {
    return createHash("sha1").update(readFileSync(join(d, inst.lock))).digest("hex");
  } catch {
    return undefined; // no lock file to name: nothing to compare
  }
};
for (const d of inst?.dirs ?? []) {
  const made = inst.installed ?? "";
  const dir = join(d, made);
  const lock = lockId(d);
  if (made) {
    let st = lstatSync(dir, { throwIfNoEntry: false });
    if (st?.isSymbolicLink() && process.env.LAND_TOP) {
      unlinkSync(dir); // the lander's scratch tree (LAND_TOP) never keeps a link, nor installs through one (a tracked one, say): the link goes, never what it points to
      st = undefined;
    }
    if (st) {
      let own;
      try {
        own = st.isSymbolicLink() ? undefined : readFileSync(join(dir, ".land-lock"), "utf8");
      } catch {
        own = undefined;
      }
      if (own === undefined || lock === undefined || own === lock) {
        continue;
      }
      rmSync(dir, { recursive: true, force: true }); // installed here for an older lock file (a retry on a moved main)
    }
  }
  const t = Date.now();
  const r = run(d, inst.cmd);
  if (r.rc) {
    fail(`install:${d}`, d, inst.cmd, r.out);
  }
  if (made && lock !== undefined && existsSync(dir)) writeFileSync(join(dir, ".land-lock"), lock);
  ran.push(`install:${d}:${secs(t)}`);
  noteStep(`install:${d}`, "ran", Math.round((Date.now() - t) / 1000));
}

// LAND_STATE_DIR: where checks.sh keeps the failed steps and files of the last run (failed.json). `--retest` (LAND_RETEST=1) runs ONLY those, with their prerequisites.
const failedFile = process.env.LAND_STATE_DIR
  ? join(process.env.LAND_STATE_DIR, "failed.json")
  : "";
let retest;
if (process.env.LAND_RETEST === "1") {
  if (!failedFile || !existsSync(failedFile)) {
    process.stderr.write(
      "land: nothing to retest: the last run recorded no failed step (run land once and let it finish)\n",
    );
    process.exit(2);
  }
  retest = Object.fromEntries(
    JSON.parse(readFileSync(failedFile, "utf8")).steps.map((x) => [
      x.id,
      x.files?.length ? x.files : null,
    ]),
  );
}
let plan;
try {
  plan = planSteps(cfg, info, { base, retest });
} catch (e) {
  process.stderr.write(`land: cannot plan the checks: ${e.message}\n`);
  process.exit(13);
}
const { steps, notes } = plan;
if (retest && !steps.length) {
  process.stderr.write(
    "land: nothing to retest: no failed step of the last run is part of this change any more (run land: it runs every test of the touched areas)\n",
  );
  process.exit(2);
}
// the environment a step wants: the recipe is the project's. A recipe that can make what is missing (`bundle.ensure`) does; one that cannot either fails the
// step (`ifMissing: "fail"`: a host without it is a bug to fix, never a skip) or says so and skips it ("skip-and-say").
const environment = async (name) => {
  const e = cfg.environments[name];
  const sub = (a) => a.replaceAll("{tool}", layout.tool)
      .replaceAll("{root}", layout.root);
  const have = e.bundle
    ? (await runAsync(".", e.bundle.have.map(sub))).rc === 0
    : true;
  let bundle = "";
  if (e.bundle && !have) {
    const made = e.bundle.ensure
      ? await runAsync(".", e.bundle.ensure.map(sub))
      : { rc: 1, out: `no ${name} and no recipe to make it` };
    if (made.rc) {
      return { missing: made.out.trim() };
    }
  }
  if (e.bundle?.path) {
    bundle = (await runAsync(".", e.bundle.path.map(sub))).out.trim();
  }
  // inside a workroom instance (LAND_INSTANCE_DB, in the environment: its database server's URL, a secret never put in argv) there is no second server: the recipe's `attach` form runs the step on a FRESH database of the instance's own server
  const inst = process.env.LAND_INSTANCE_DB;
  const recipe = inst && e.attach ? e.attach : e.wrap;
  return { wrap: recipe.map((a) => sub(a).replaceAll("{bundle}", bundle)) };
};

// A step that passed on THIS tree (the git tree id of HEAD) is not run again on it: a land after advice that left the tree alone re-runs nothing (only the council), and a run stopped half way (a council finding, a red
// step) keeps what had passed, so `land` after a red step re-runs that step alone. Another tree is another run: what Land-Checks attests ran on the tree that lands, or says where it was carried from
// (below). The key is the tree, the step's id and its command; a marker is a file in the git common dir, kept three days. Steps that wait for one another (`after`) are kept
// or re-run together: the output of one is what the other reads. A kept step prints `id:kept` in the checks line.
//
// A REBASE onto a moved main makes another tree, not another change: a test group (a step with a `list`), or a step that names what it reads (`reads`), that passed on an earlier tree of the same change (the same diff by its patch-id: the council keeps its verdicts the same way)
// may be CARRIED to this tree (a step marked `select: "reach"`: its tests that passed and that nothing incoming reaches; a step with no `reads` that is not a test always runs again). The rule is in tool/carry.mjs (the one decision) and nowhere else. A carried group gives this tree the pass (its marker, so a retry of it keeps it), the checks line says `AREA:carried from TREE`, the tree it
// really ran on, and one line `carried|re-run ID: reason` per step goes to stderr. The proof is the two trees, never a word of the caller. A group of test steps that wait for one another is carried whole or not at all.
// Nothing is carried by --retest, nor when what came in cannot be read.
const keep = process.env.LAND_STATE_DIR ? join(process.env.LAND_STATE_DIR, "step-green") : "";
const reusable = new Map();
const lastPass = new Map(); // the file that names the latest tree a test group passed on, per command and per change
const kept = new Set();
const carried = new Map();
const marker = (x) => {
  try {
    return JSON.parse(readFileSync(reusable.get(x.id), "utf8"));
  } catch {
    return undefined; // no marker, or one that was cut short: the step runs
  }
};
const counts = (x) => {
  const m = marker(x);
  return m ? (m.count ?? 0) : undefined;
};
// what names a step: ONE definition. The marker of a tree adds the step's whole command (the list of the tree's test files included); the record of a change's earlier pass adds the step's unexpanded command
// template (cmdKey: no list, no {base}), because main adding a test file or a rebase is no other step.
const stepName = (x) => [x.id, x.dir, x.environment, x.env];
// the project root is `{root}` in the key, never a path: the push gate checks the same tree in a scratch worktree of its own, at a path of its own, and a step that names the root (a test group's runner) is the same step there
const commandOf = (x) => [...stepName(x), x.argv.map((a) => String(a).replace(/land-step-\w+/g, "land-step-").replaceAll(layout.root, "{root}"))];
const stepKeyOf = (x) => [...stepName(x), x.cmdKey];
const sha = (v) => createHash("sha1").update(JSON.stringify(v)).digest("hex");
const writeMarker = (file, body) => {
  writeFileSync(`${file}.${process.pid}.tmp`, JSON.stringify(body)); // whole or absent: written aside, then renamed
  renameSync(`${file}.${process.pid}.tmp`, file);
};
// a pass of a test group: its marker on this tree, and the name of this tree as the latest one this change's group passed on
// (a step marked `select: "reach"` names the test files its pass was of: x.passedFiles, absent when it passed whole; carry.mjs reads them back as `passed`)
const passed = (x, tree, body) => {
  writeMarker(reusable.get(x.id), body);
  if (lastPass.has(x.id)) writeMarker(lastPass.get(x.id), { tree, ...body, ...(x.passedFiles ? { files: x.passedFiles } : {}) });
};
// a step that is not a test and says what it reads (`reads` in land.json) is carried over a rebase by the same rule as a test group (carry.mjs); one that does not always runs again
const carryable = (x) => x.retry !== "test" && Array.isArray(x.reads) && x.reads.length > 0;
const said = new Map(); // step id -> the decision line (one per step): what ran and why
const narrow = new Map(); // step id -> its test files the change itself adds or edits (the only ones a carried step runs)
let tree = "";
if (keep && !retest) {
  mkdirSync(keep, { recursive: true });
  for (const f of readdirSync(keep)) {
    if (Date.now() - statSync(join(keep, f)).mtimeMs > 3 * 86400_000) rmSync(join(keep, f), { force: true });
  }
  tree = run(".", ["git", "rev-parse", "HEAD^{tree}"]).out.trim();
  for (const x of steps) reusable.set(x.id, join(keep, sha([tree, ...commandOf(x)])));
  const patch = spawnSync("git", ["diff", base, "HEAD"], { encoding: "utf8", maxBuffer: 1 << 28 });
  const change = patch.status === 0 ? patchId(patch.stdout) : "";
  if (change) for (const x of steps) if (x.retry === "test" || carryable(x)) lastPass.set(x.id, join(keep, `last-${sha([change, ...stepKeyOf(x)])}`));
  const group = new Map(steps.map((x) => [x.id, x.id]));
  const root = (id) => (group.get(id) === id ? id : root(group.get(id)));
  for (const x of steps) for (const a of x.after ?? []) if (group.has(a)) group.set(root(a), root(x.id));
  const sameGroup = (x) => steps.filter((y) => root(y.id) === root(x.id));
  // what a file reaches (reach.mjs): built once, from this tree, the first time a step has files that came in to weigh; null when the tree cannot be read (the decision then re-runs)
  let graph;
  const reach = () => {
    if (graph === undefined) {
      const listed = run(".", ["git", "ls-files", "-z"]);
      try {
        if (listed.rc !== 0) throw new Error(listed.out);
        // the change's deleted files stay in the graph as names: a file that still imports one reaches it, and a test that does is then selected (a deleted file is a change); so do the files that main deleted under the change (the
        // incoming files this tree no longer has): a test that still imports one is reached by it
        const tracked = listed.out.split("\0").filter(Boolean);
        const here = new Set(tracked);
        const gone = [...earlierTrees].flatMap((t) => (came(t) ?? []).filter((f) => !here.has(f)));
        graph = buildGraph({ files: [...tracked, ...(deleted ?? []), ...gone], isProse: cfg.isProse, read: (f) => { try { return readFileSync(f, "utf8"); } catch { return null; } } });
      } catch (e) {
        process.stderr.write(`land: cannot read what the incoming files reach: ${String(e.message).trim().split("\n")[0]}\n`);
        graph = null;
      }
    }
    return graph;
  };
  const earlierTrees = new Set(); // the trees the steps of this change passed on before (their records): what came in since is what differs from each
  for (const x of steps) {
    try {
      const t = JSON.parse(readFileSync(lastPass.get(x.id), "utf8")).tree;
      if (t !== tree && run(".", ["git", "cat-file", "-e", `${t}^{tree}`]).rc === 0) earlierTrees.add(t);
    } catch {
      // no earlier pass of this step: nothing came in for it
    }
  }
  const incoming = new Map(); // an earlier tree -> the files that differ between it and this one (null: cannot be read)
  const came = (before) => {
    if (!incoming.has(before)) {
      const d = run(".", ["git", "diff", "--name-only", "-z", before, tree]);
      incoming.set(before, d.rc === 0 ? d.out.split("\0").filter(Boolean) : null);
    }
    return incoming.get(before);
  };
  // no rename detection: a renamed file is its old path deleted and its new path added (the files that name the old one reach it)
  const own_ = run(".", ["git", "diff", "--name-only", "--no-renames", "-z", base, "HEAD"]);
  const changed = own_.rc === 0 ? own_.out.split("\0").filter(Boolean) : null;
  const gone = run(".", ["git", "diff", "--name-only", "--no-renames", "-z", "--diff-filter=D", base, "HEAD"]);
  const deleted = gone.rc === 0 ? gone.out.split("\0").filter(Boolean) : null; // unreadable: unknown, so the first run runs the full area (never less)
  // the coverage map: published on a git ref of origin (checks.coverageMap.ref, a branch or ref; checks.coverageMap.file, default coverage-map.json), fetched here, never read from a local file and never in main
  const cm = cfg.checks?.coverageMap;
  let mapText = null;
  let readError = "";
  if (cm?.ref) {
    const got = run(".", ["git", "fetch", "--quiet", "origin", cm.ref]);
    const shown = got.rc === 0 ? run(".", ["git", "show", `FETCH_HEAD:${cm.file ?? "coverage-map.json"}`]) : got;
    if (shown.rc === 0) mapText = shown.out;
    else readError = `cannot fetch ${cm.ref} from origin: ${shown.out.trim().split("\n")[0]}`;
  }
  let map = readError ? { ok: false, reason: `coverage map unreadable (${readError})` } : !cm?.ref ? { ok: false, reason: "no coverage map (checks.coverageMap.ref is not set)" } : parseMap({ text: mapText, now: Date.now(), maxAgeHours: cm.maxAgeHours ?? 24 });
  // a map is used only when the commit it was built at is an ancestor of the base (a hand-written or foreign map is not)
  if (map.ok && !(/^[0-9a-f]{7,40}$/.test(map.commit) && run(".", ["git", "merge-base", "--is-ancestor", map.commit, base]).rc === 0)) map = { ok: false, reason: `coverage map built at ${map.commit}, which is not an ancestor of the base` };
  if (map.ok) process.stderr.write(`land: coverage map age ${map.age} (commit ${map.commit})\n`);
  const earlier = new Map();
  const selected = new Map(); // step id -> the test files the selection chose (a first run)
  const how = new Map(); // step id -> what chose them ("reach", "coverage map")
  for (const x of steps) {
    if (counts(x) !== undefined) {
      said.set(x.id, `carried ${x.id}: already passed on this tree`);
      continue;
    }
    let m;
    try {
      m = lastPass.has(x.id) ? JSON.parse(readFileSync(lastPass.get(x.id), "utf8")) : undefined;
    } catch {
      m = undefined; // no earlier pass of this change's group, or a record cut short: it runs
    }
    const known = m !== undefined && m.tree !== tree && run(".", ["git", "cat-file", "-e", `${m.tree}^{tree}`]).rc === 0;
    const reading = carryable(x) ? matcher(x.reads) : undefined;
    const d = decide({ step: { id: x.id, retry: x.retry, select: x.select, dir: x.dir, isTest: matcher(cfg.paths.tests), deepFor: x.deepFor ? matcher(x.deepFor) : undefined, reads: reading }, passed: known && Array.isArray(m.files) ? m.files : undefined, earlier: known, incoming: known ? came(m.tree) : [], changed, files: x.files ?? null, graph: known || x.select === "reach" ? reach() : null, runner: `${relative(layout.root, layout.tool)}/run-steps.mjs`, own: `${relative(layout.root, layout.spire)}/`, map, deleted, preload: x.select === "reach" && x.retry === "test" && graph ? runnerInputs({ step: x, environments: cfg.environments, tracked: graph.files, root: layout.root }) : [] });
    if (d.select) {
      selected.set(x.id, d.select);
      how.set(x.id, d.by ?? "coverage map");
    }
    if (d.carry) earlier.set(x.id, m);
    if (d.carry && d.rerun.length) narrow.set(x.id, d.rerun);
    if (d.carry && x.select === "reach") x.passedFiles = d.universe; // what this tree's pass will be of (carried and re-run alike)
    said.set(x.id, `${d.carry ? "carried" : "re-run"} ${x.id}: ${d.reason}${d.carry ? `; re-run ${d.rerun.length} test file(s)` : ""}`);
  }
  // a group of steps that wait for one another is carried whole or not at all: the test steps of it by themselves (as before), a step that is not a test with every step of its group (the build one reads is the build the other made)
  for (let moved = true; moved; ) {
    moved = false;
    for (const x of steps) {
      const needed = sameGroup(x).filter((y) => x.retry !== "test" || y.retry === "test");
      if (!earlier.has(x.id) || needed.every((y) => earlier.has(y.id))) continue;
      earlier.delete(x.id);
      narrow.delete(x.id);
      said.set(x.id, `re-run ${x.id}: a step it waits for re-runs`);
      moved = true;
    }
  }
  for (const x of steps) {
    if (!selected.has(x.id)) continue;
    const total = x.files.length;
    const chosen = new Set(selected.get(x.id));
    Object.assign(x, restrict(x, (f) => chosen.has(f)));
    // it runs part of its list: its marker is of THAT list (the key holds the command); a record of a pass of the change's group names the list, or (a selection by a coverage map) there is none: it cannot stand for the whole step, which a rebase would carry
    x.partial = true;
    if (x.select === "reach") x.passedFiles = x.files; // a step marked `select: "reach"` keeps the list it ran, so a rebase carries what passed (carry.mjs); another selection (a coverage map's) cannot stand for the step
    else lastPass.delete(x.id);
    reusable.set(x.id, join(keep, sha([tree, ...commandOf(x)])));
    x.label = `${x.id}(${x.files.length} of ${total} by ${how.get(x.id)})`;
    // nothing selected: the step does not run (a runner given no file would run every test of its directory)
    x.none = x.files.length === 0;
  }
  for (const x of steps) process.stderr.write(`land: ${said.get(x.id)}\n`);
  for (const x of steps) {
    if (!earlier.has(x.id)) continue;
    const m = earlier.get(x.id);
    if (narrow.has(x.id)) {
      // carried but for the change's own test files: the step runs ONLY those (the same restriction --retest makes), and its pass stands for the whole step
      const own_files = new Set(narrow.get(x.id));
      const total = x.passedFiles?.length ?? x.files.length; // a step marked `select: "reach"`: of the tests it had to pass on this tree
      Object.assign(x, restrict(x, (f) => own_files.has(f))); // the same restriction --retest makes
      x.earlierCount = m.count ?? 0;
      x.earlierRanOn = m.ranOn ?? m.tree;
      x.label = `${x.id}(${x.files.length} re-run of ${total}, rest carried from ${x.earlierRanOn.slice(0, 12)})`;
      continue;
    }
    carried.set(x.id, { count: m.count ?? 0, ranOn: m.ranOn ?? m.tree });
    passed(x, tree, carried.get(x.id));
  }
  for (const x of steps) {
    if (sameGroup(x).every((y) => counts(y) !== undefined)) kept.add(x.id);
  }
}

// The independent steps run AT THE SAME TIME on the host's cores (style, lint, architecture, typechecks, test groups: none reads what another writes). The test groups share the
// cores between them (the project's `checks.testConcurrencyFlag`; `checks.testReporterFlag` fixes the output the failed files are read from), so a group is not starved into a timeout by the others. A red file is red: nothing re-runs it.
// The test counts come from the runners' own summaries, read from the output of each step that passes.
const { cores, source: cpuSource } = cpuCores();
const nTests = steps.filter((s) => s.retry === "test").length;
// the cores are shared between the test groups AND the other steps (typecheck, build, the browser job): one share is left for them, so a test is not starved into a timeout
// ...and so is the free memory (one share per test group, one left for the others), each group held to the project's budget per worker (checks.workerBudgetMB): the worker count of a group is
// min(its core share, its memory share / budget), never below 1
const freeBytes = availableBytes();
const caps = new Map();
try {
  for (const s of steps.filter((x) => x.retry === "test")) {
    const budget = budgetBytes(cfg.checks, s.area, s.kind);
    caps.set(s.id, { budget, cap: groupCap({ cores, groups: nTests, freeBytes, budgetBytes: budget }) });
  }
} catch (e) {
  process.stderr.write(`land: cannot plan the checks: ${e.message}\n`);
  process.exit(13);
}
const gates = new Map(steps.map((s) => [s.id, Promise.withResolvers()]));
const runStep = async (s) => {
  // a step that needs another's output (the stylesheet test after the app build) waits for it; one that failed is not waited on in vain
  await Promise.all((s.after ?? []).map((id) => gates.get(id)?.promise));
  const began = Date.now();
  const r = await runStepNow(s);
  gates.get(s.id).resolve();
  noteStep(s.id, r.bad !== undefined ? "red" : r.skipped ? "skipped" : carried.has(s.id) ? "carried" : kept.has(s.id) ? "kept" : s.none ? "none" : "ran", Math.round((Date.now() - began) / 1000));
  return r;
};
const runStepNow = async (s) => {
  const mark = reusable.get(s.id);
  if (carried.has(s.id) || kept.has(s.id)) {
    const ranOn = marker(s)?.ranOn; // a pass carried over a rebase says so, however often this tree is run
    return { s, took: ranOn ? `${s.retry === "test" ? s.area : s.id}:carried from ${ranOn.slice(0, 12)}` : `${s.label}:kept`, count: counts(s) };
  }
  if (s.none) return { s, took: `${s.label}:none`, count: 0 }; // a step that ran nothing says so, never a time
  const r = await runStepOnce(s);
  if (mark && r.took) {
    passed(s, tree, s.earlierRanOn ? { count: s.earlierCount, ranOn: s.earlierRanOn } : { count: r.count ?? 0 });
  }
  return r;
};
const runStepOnce = async (s) => {
  let argv = s.argv;
  if (s.environment) {
    const env = await environment(s.environment);
    if (env.missing !== undefined) {
      if (cfg.environments[s.environment].ifMissing === "fail") {
        return {
          s,
          argv,
          bad: `no ${s.environment} environment on this host: ${env.missing}`,
        };
      }
      notes.push(
        `${s.label.replace(/^tests:/, "")}: no ${s.environment} environment, CI runs them`,
      );
      return { s, skipped: true };
    }
    argv = [...env.wrap, ...argv];
  }
  const flag = cfg.checks?.testConcurrencyFlag;
  // the reporter is named, never the runner's default: the default is TAP when output is not a terminal on the pinned Node (22) and spec on a newer one, and only spec names the
  // failing files (`test at FILE:L:C`, which `land --retest` and a reader of the output's tail both need)
  const reporter = cfg.checks?.testReporterFlag;
  const at = argv.indexOf("--test");
  const limit = s.retry === "test" && at >= 0 && flag ? caps.get(s.id) : undefined;
  if (s.retry === "test" && at >= 0) {
    argv = [
      ...argv.slice(0, at + 1),
      ...(limit ? [flag.replace("{n}", String(limit.cap.workers))] : []),
      ...(reporter ? [reporter] : []),
      ...argv.slice(at + 1),
    ];
  }
  if (s.retry === "test" && !limit) {
    process.stderr.write(`${uncappedLine({ area: s.area, limitBytes: memoryMaxBytes(), source: limitSource() })}\n`);
  }
  if (limit) {
    process.stderr.write(`${planLine({ area: s.area, cap: limit.cap, cores, cpuSource, shares: nTests + 1, freeBytes, budget: limit.budget, limitBytes: memoryMaxBytes(), source: limitSource() })}\n`);
  }
  const t = Date.now();
  const r = await runAsync(s.dir, argv, envOf(s), !!limit);
  if (limit) {
    process.stderr.write(`${peakLine({ area: s.area, cap: limit.cap, peakBytes: r.peak })}\n`);
  }
  if (r.rc === 0) {
    return { s, took: `${s.label}:${secs(t)}`, count: countTests(r.out) };
  }
  return { s, argv, bad: r.out, files: s.retry === "test" ? failedFilesOf(r.out) : [] };
};
// THE CHEAP CHECKS FIRST (a step the project marks `first: true`: style, lint, typecheck, the small ratchets): they run alone, side by side; a red among them is reported AT ONCE and nothing else starts, so a lint failure
// costs seconds, not the minutes of the slowest test group. Only when they are green do the other steps start (all at the same time, as before). A `first` step may wait only for another `first` step.
// WHAT RAN AND WHY, one line per step, on every landing, red or green: the step as it ran (the files it ran of its list, its seconds, a carry) and the reason the selection gave
const whatRan = (done) => {
  process.stderr.write("land: what ran, and why:\n");
  for (const r of done) {
    const why = (said.get(r.s.id) ?? "").replace(/^(?:carried|re-run) \S+: /, "") || (r.s.retry === "test" ? "every test of the area" : "a check always runs");
    const state = r.bad !== undefined ? `${r.s.label}: FAILED` : r.skipped ? `${r.s.label}: skipped` : r.took ?? r.s.label;
    process.stderr.write(`land:   ${state}${r.s.first ? " [cheap, first]" : ""}: ${why}\n`);
  }
  const touched = info.areas.names;
  process.stderr.write(`land:   areas touched: ${touched.map((n) => `${n} (${(info.areas.byArea?.[n] ?? []).length} changed path(s))`).join(", ") || "none"}${info.areas.full ? " [a path in no area: every area runs]" : ""}\n`);
  const idle = cfg.areas.map((x) => x.name).filter((n) => !touched.includes(n));
  if (idle.length) process.stderr.write(`land:   no step ran for the other areas (no changed path of theirs): ${idle.join(", ")}\n`);
};
const reportBad = (bad, note, done) => {
  const nfiles = bad.reduce((n, r) => n + (r.files ?? []).length, 0);
  process.stderr.write(
    `land: ===== ${bad.length} step(s) FAILED${nfiles ? `, ${nfiles} test file(s)` : ""}: ${bad.map((r) => r.s.label).join(", ")} =====${note}\n`,
  );
  for (const r of bad) {
    fail(r.s.label, r.s.dir, r.argv, r.bad, false);
    if ((r.files ?? []).length) {
      process.stderr.write(
        `land: failed files of ${r.s.id} (${r.s.dir}):\n${r.files.map((f) => `  ${f}`).join("\n")}\n`,
      );
    }
  }
  if (failedFile) {
    writeFileSync(
      failedFile,
      JSON.stringify({
        steps: bad.map((r) => ({ id: r.s.id, area: r.s.area, dir: r.s.dir, files: r.files ?? [] })),
      }),
    );
    process.stderr.write(
      `land: the failed steps and files are kept (${failedFile}): fix them, then \`land --retest\` runs only those until green, and \`land\` runs the whole touched set once on the final tree\n`,
    );
  }
  whatRan(done);
  process.exit(13);
};
const firstIds = new Set(steps.filter((s) => s.first).map((s) => s.id));
for (const s of steps.filter((x) => x.first)) {
  const late = (s.after ?? []).find((a) => !firstIds.has(a) && steps.some((y) => y.id === a));
  if (late) {
    process.stderr.write(`land: cannot plan the checks: step ${s.id} is \`first\` but waits for ${late}, which is not\n`);
    process.exit(13);
  }
}
const quick = steps.filter((s) => s.first);
const quickResults = await Promise.all(quick.map(runStep));
const quickBad = quickResults.filter((r) => r.bad !== undefined);
if (quickBad.length) {
  const waiting = steps.length - quick.length;
  reportBad(quickBad, waiting ? ` (cheap checks first: the ${waiting} other step(s) did not start)` : "", quickResults);
}
const results = [...quickResults, ...(await Promise.all(steps.filter((s) => !s.first).map(runStep)))];
// EVERY other step finished (a failure stops none of the others): one report of all of them, and the failed steps and files kept for `land --retest`
const bad = results.filter((r) => r.bad !== undefined);
if (bad.length) reportBad(bad, "", results);
if (failedFile) {
  rmSync(failedFile, { force: true });
}
whatRan(results);
const tests = results.reduce((n, r) => n + (r.count ?? 0), 0);
const files = steps.reduce((n, s) => n + (s.files?.length ?? 0), 0);
console.log(
  JSON.stringify({
    ran: [...ran, ...results.filter((r) => r.took).map((r) => r.took)],
    areas: info.areas.names,
    retest: !!retest,
    tests,
    files,
    notes,
  }),
);
