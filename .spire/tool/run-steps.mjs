#!/usr/bin/env node
// run-steps.mjs BASE: run the steps plan.mjs makes for the change (classification JSON on stdin: the areas it touches) in the current tree, at the same time on the host's cores (the independent ones), and print
// {"ran": [...], "areas": [...], "tests": N, "files": N, "notes": [...]} on stdout (tests: the runners' own count of the tests that ran). A failed step prints the tail of its output and "land: check FAILED: <step>" on stderr and exits 13.
// The environment recipes (a database, ...) and the install of dependencies are the project's (land.json), run here, never named here.
import { createHash } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { join, relative } from "node:path";
import { layout } from "./layout.mjs";
import { loadConfig, matcher } from "./config.mjs";
import { patchId } from "./patch-id.mjs";
import { planSteps } from "./plan.mjs";
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
// A step that names no environment is given none of the variables the project withholds (`checks.withhold` of land.json) nor of what the box's wrap provides (LAND_BOX_EXPORTS: wrap.exports of box.json):
// inside a box (`box land`) they are the box's own server, a secret that belongs to the steps whose recipe attaches to it (`environment`), and a test that is told "it is here" without the key it then needs stops the whole group.
const envOf = (s) => {
  const rest = { ...process.env };
  for (const k of [...(cfg.checks?.withhold ?? []), ...(process.env.LAND_BOX_EXPORTS ?? "").split(/\s+/).filter(Boolean)]) delete rest[k];
  // the step's own variables come last: the unit tests need the ones CI's unit shards set (a database URL that is only checked for, a session secret), which no caller's own can stand in for
  return { ...(s.environment ? process.env : rest), ...(s.env ?? {}) };
};
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
const fail = (label, dir, argv, out, exit = true) => {
  process.stderr.write(`${out.trim().split("\n").slice(-40).join("\n")}\n`);
  process.stderr.write(
    `land: check FAILED: ${label} (${dir}: ${argv.join(" ")}); nothing lands\n`,
  );
  if (exit) {
    process.exit(13);
  }
};

// dependencies: the project's `checks.install` names the directories, the lock file, what an install makes there (`installed`: with none, the install runs every time) and the command. Link the submitter's (same lock file) when the lander
// runs us in its scratch worktree, else install
const inst = cfg.checks?.install;
for (const d of inst?.dirs ?? []) {
  const made = inst.installed ?? "";
  if (made && existsSync(join(d, made))) {
    continue;
  }
  const top = process.env.LAND_TOP;
  const same = (f) =>
    existsSync(join(top ?? "", d, inst.lock)) &&
    readFileSync(join(d, inst.lock)).equals(
      readFileSync(join(top, d, inst.lock)),
    );
  if (made && top && existsSync(join(top, d, made)) && same()) {
    symlinkSync(join(top, d, made), join(d, made));
  } else {
    const t = Date.now();
    const r = run(d, inst.cmd);
    if (r.rc) {
      fail(`install:${d}`, d, inst.cmd, r.out);
    }
    ran.push(`install:${d}:${secs(t)}`);
  }
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

// A step that passed on THIS tree (the git tree id of HEAD) is not run again on it: a round after a rebuttal re-runs nothing (only the council), and a run stopped half way (a council finding, a red
// step) keeps what had passed, so `land` after a red step re-runs that step alone. Another tree is another run: what Land-Checks attests ran on the tree that lands, or says where it was carried from
// (below). The key is the tree, the step's id and its command; a marker is a file in the git common dir, kept three days. Steps that wait for one another (`after`) are kept
// or re-run together: the output of one is what the other reads. A kept step prints `id:kept` in the checks line.
//
// A REBASE onto a moved main makes another tree, not another change: a test group (a step with a `list`) that passed on an earlier tree of the same change (the same diff by its patch-id: the council keeps its verdicts the same way)
// and whose area's declared inputs (the `paths` of every area that carries the step, the tool and the project's files in `.spire/`) are the same in that tree and this one is CARRIED: this tree is given the pass (its marker, so a retry
// of it keeps it) and the checks line says `AREA:carried from TREE`, the tree it really ran on. The proof is the two trees, never a word of the caller: nothing but what the steps recorded here names the earlier tree, and what
// came in from main is their difference. The cheap whole-tree steps (typecheck, lint, ratchets: every step that is not a test group) run again whatever came in. A group of test steps that wait for one another is carried whole or not at all.
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
const commandOf = (x) => [x.id, x.dir, x.environment, x.env, x.argv.map((a) => String(a).replace(/land-step-\w+/g, "land-step-"))];
const sha = (v) => createHash("sha1").update(JSON.stringify(v)).digest("hex");
const writeMarker = (file, body) => {
  writeFileSync(`${file}.${process.pid}.tmp`, JSON.stringify(body)); // whole or absent: written aside, then renamed
  renameSync(`${file}.${process.pid}.tmp`, file);
};
// a pass of a test group: its marker on this tree, and the name of this tree as the latest one this change's group passed on
const passed = (x, tree, body) => {
  writeMarker(reusable.get(x.id), body);
  if (lastPass.has(x.id)) writeMarker(lastPass.get(x.id), { tree, ...body });
};
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
  if (change) for (const x of steps) if (x.retry === "test") lastPass.set(x.id, join(keep, `last-${sha([change, ...commandOf(x)])}`));
  const group = new Map(steps.map((x) => [x.id, x.id]));
  const root = (id) => (group.get(id) === id ? id : root(group.get(id)));
  for (const x of steps) for (const a of x.after ?? []) if (group.has(a)) group.set(root(a), root(x.id));
  const sameGroup = (x) => steps.filter((y) => root(y.id) === root(x.id));
  const own = `${relative(layout.root, layout.spire)}/`;
  const incoming = new Map(); // an earlier tree -> the files that differ between it and this one (null: cannot be read)
  const touches = (x, before) => {
    if (!incoming.has(before)) {
      const d = run(".", ["git", "diff", "--name-only", "-z", before, tree]);
      incoming.set(before, d.rc === 0 ? d.out.split("\0").filter(Boolean) : null);
    }
    const files = incoming.get(before);
    const inputs = matcher(cfg.areas.filter((a) => a.steps.some((s) => s.id === x.id)).flatMap((a) => a.paths));
    return !files || files.some((f) => f.startsWith(own) || inputs(f));
  };
  const earlier = new Map();
  for (const x of steps) {
    if (!lastPass.has(x.id) || counts(x) !== undefined) continue;
    try {
      const m = JSON.parse(readFileSync(lastPass.get(x.id), "utf8"));
      if (m.tree !== tree && run(".", ["git", "cat-file", "-e", `${m.tree}^{tree}`]).rc === 0 && !touches(x, m.tree)) earlier.set(x.id, m);
    } catch {
      // no earlier pass of this change's group, or a record cut short: it runs
    }
  }
  for (const x of steps) {
    if (!earlier.has(x.id) || !sameGroup(x).filter((y) => y.retry === "test").every((y) => earlier.has(y.id))) continue;
    const m = earlier.get(x.id);
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
    const budget = budgetBytes(cfg.checks, s.area);
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
  const r = await runStepNow(s);
  gates.get(s.id).resolve();
  return r;
};
const runStepNow = async (s) => {
  const mark = reusable.get(s.id);
  if (carried.has(s.id) || kept.has(s.id)) {
    const ranOn = marker(s)?.ranOn; // a pass carried over a rebase says so, however often this tree is run
    return { s, took: ranOn ? `${s.area}:carried from ${ranOn.slice(0, 12)}` : `${s.label}:kept`, count: counts(s) };
  }
  const r = await runStepOnce(s);
  if (mark && r.took) {
    passed(s, tree, { count: r.count ?? 0 });
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
const results = await Promise.all(steps.map(runStep));
// EVERY step finished (a failure stops none of the others): one report of all of them, and the failed steps and files kept for `land --retest`
const bad = results.filter((r) => r.bad !== undefined);
if (bad.length) {
  const nfiles = bad.reduce((n, r) => n + (r.files ?? []).length, 0);
  process.stderr.write(
    `land: ===== ${bad.length} step(s) FAILED${nfiles ? `, ${nfiles} test file(s)` : ""}: ${bad.map((r) => r.s.label).join(", ")} =====\n`,
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
  process.exit(13);
}
if (failedFile) {
  rmSync(failedFile, { force: true });
}
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
