// Admission: this file shows that the lander (`push`) gives its gate worktree dependencies of its OWN (run-steps.mjs: `checks.install`), never a link into the submitter's tree, so that rebasing and
// checking again after main moved works at any scale. A step of the gate may run `npm ci` in its directory (a project's browser job does, when its lock file is newer than what is installed: always, in a
// fresh worktree): `npm ci` empties the directory it is given, and through a LINK that is the submitter's own node_modules, which a concurrent step of the same gate then finds half gone (Cannot find
// module ...) and the submitter's next run finds empty. And a retry rebased onto a main that moved the lock file must see the new lock's dependencies, not the first attempt's. It would fail if a step
// of the gate could damage the submitter's node_modules, or if a retry kept dependencies installed for an older lock file. Oracle: real git, a bare remote, the vendored tool and the real `npm ci` on a
// package with local dependencies (no network).
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { cpSync, symlinkSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repo = join(fileURLToPath(import.meta.url), "../..");
const { NODE_TEST_CONTEXT: _context, NODE_OPTIONS: _options, ...inherited } = process.env;
const env = { ...inherited, BOX_ID: "", LAND_INSTANCE_URL: "", LAND_INSTANCE_DB: "", GIT_AUTHOR_NAME: "t", GIT_AUTHOR_EMAIL: "t@t", GIT_COMMITTER_NAME: "t", GIT_COMMITTER_EMAIL: "t@t" };
const git = (cwd, ...a) => execFileSync("git", a, { cwd, env, encoding: "utf8" });
const node = (js) => ["node", "-e", js];

// the harness's own variables (the runs file, the author of the commit the mover makes) reach the steps by the step's own `pass` (step-env.mjs)
const withHarness = (cfg) => {
  for (const a of cfg.areas) for (const st of a.steps) st.pass ??= ["RUNS", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"];
  return cfg;
};

// the project: app/ has a dependency (a local package: `npm ci` needs no network) and a step that reads it; the `mover` step is the one that moves main during the gate, from another clone
function world(dir) {
  const seed = join(dir, "seed");
  const bare = join(dir, "remote.git");
  mkdirSync(join(seed, ".spire/review"), { recursive: true });
  cpSync(join(repo, "tool"), join(seed, ".spire/tool"), { recursive: true, filter: (src) => !src.endsWith(".test.mjs") });
  writeFileSync(join(seed, ".spire/principles.md"), "# Principles\n\n1. P1: one way.\n");
  for (const role of ["inspector", "steward", "architect", "designer", "warden"]) writeFileSync(join(seed, ".spire/review", `${role}.md`), `---\nrole: ${role}\ncalls: []\n---\nremoved by this project\n`);
  const cfg = {
    version: 1,
    paths: { tests: ["**/*Tests/**"], prose: ["**/*.md"], notProd: ["**/*.md"] },
    sensitive: { globs: [], nameGlobs: [], always: [] },
    areas: [
      { name: "app", paths: ["app/**"], steps: [{ id: "ssr", dir: "app", cmd: node("require('dep'); require('fs').appendFileSync(process.env.RUNS, 'ssr\\n')") }] },
      { name: "docs", paths: ["**/*.md"], steps: [{ id: "doc-check", dir: ".", cmd: node("0") }] },
    ],
    checks: { install: { dirs: ["app"], lock: "package-lock.json", installed: "node_modules", cmd: ["npm", "ci", "--no-audit", "--no-fund", "--offline"] } },
    environments: {},
  };
  writeFileSync(join(seed, ".spire/land.json"), JSON.stringify(withHarness(cfg)));
  mkdirSync(join(seed, "app/dep"), { recursive: true });
  writeFileSync(join(seed, "app/dep/package.json"), '{"name":"dep","version":"1.0.0","main":"index.js"}');
  writeFileSync(join(seed, "app/dep/index.js"), "module.exports = 1;\n");
  writeFileSync(join(seed, "app/package.json"), '{"name":"app","version":"1.0.0","dependencies":{"dep":"file:dep"}}');
  writeFileSync(join(seed, ".gitignore"), "node_modules\n");
  git(seed, "init", "-q", "-b", "main");
  git(seed, "add", "-A");
  git(seed, "commit", "-qm", "seed");
  // the lock file of the dependency, made by npm itself
  execFileSync("npm", ["install", "--package-lock-only", "--no-audit", "--no-fund", "--offline"], { cwd: join(seed, "app"), env, stdio: "pipe" });
  git(seed, "add", "-A");
  git(seed, "commit", "-qm", "lock");
  git(dir, "clone", "-q", "--bare", seed, bare);
  const clone = (n) => {
    git(dir, "clone", "-q", bare, n);
    return join(dir, n);
  };
  return { bare, clone };
}
const commitFile = (c, p, body, msg) => {
  mkdirSync(join(c, p, ".."), { recursive: true });
  writeFileSync(join(c, p), body);
  git(c, "add", "-A");
  git(c, "commit", "-qm", msg);
};
const npm = (cwd, ...a) => execFileSync("npm", [...a, "--no-audit", "--no-fund", "--offline"], { cwd, env, stdio: "pipe" });
const lines = (f) => (existsSync(f) ? readFileSync(f, "utf8").split("\n").filter(Boolean) : []);
const setProject = (c, edit) => {
  const p = join(c, ".spire/land.json");
  const cfg = JSON.parse(readFileSync(p, "utf8"));
  edit(cfg);
  writeFileSync(p, JSON.stringify(withHarness(cfg)));
  git(c, "commit", "-qam", "the project's file");
};
// a step that moves main once, from another clone, while the gate runs
const mover = (moved, other, files) => ({
  id: "mover",
  dir: ".",
  cmd: node(
    `const fs=require('fs'),cp=require('child_process');if(!fs.existsSync(${JSON.stringify(moved)})){fs.writeFileSync(${JSON.stringify(moved)},'1');${files};for(const c of [['add','-A'],['commit','-qm','main moves'],['push','-q','origin','HEAD:main']])cp.execFileSync('git',c,{cwd:${JSON.stringify(other)},stdio:'pipe'})}`,
  ),
});
const ready = (w, dir) => {
  const a = w.clone("a"); // the submitter: has its dependencies installed, as a builder's tree does
  const other = w.clone("other");
  npm(join(a, "app"), "ci");
  writeFileSync(join(a, "app/node_modules/.submitter-mark"), "mine\n");
  const lock = readFileSync(join(a, "app/node_modules/.package-lock.json"), "utf8");
  return { a, other, lock, runs: join(dir, "runs"), moved: join(dir, "moved") };
};

test("push: a step of the gate that runs `npm ci` in its directory leaves the submitter's node_modules whole, and a retry after main moved still lands", () => {
  const dir = mkdtempSync(join(tmpdir(), "retry-install-"));
  try {
    const w = world(dir);
    const { a, other, lock, runs, moved } = ready(w, dir);
    setProject(a, (cfg) => {
      // a browser job's way: reinstall when it thinks it must (here: always), then the steps that read the dependencies
      cfg.areas[0].steps.push({ id: "reinstall", dir: "app", cmd: ["npm", "ci", "--no-audit", "--no-fund", "--offline"] });
      cfg.areas[0].steps.find((s) => s.id === "ssr").after = ["reinstall"];
      // a step that rewrites a file of node_modules in place (a cache, a patch): never a byte of the submitter's
      cfg.areas[0].steps.push({ id: "rewrite", dir: "app", cmd: node("require('fs').appendFileSync('node_modules/.package-lock.json', ' rewritten')") });
      cfg.areas[0].steps.push(mover(moved, other, `fs.writeFileSync(${JSON.stringify(join(other, "moved.md"))},'x\\n')`));
    });
    git(a, "push", "-q", "origin", "HEAD:main");
    git(other, "pull", "-q", "--rebase", "origin", "main");
    commitFile(a, "app/change.txt", "a change\n", "app: a change");

    const r = spawnSync(join(a, ".spire/tool/push"), [], { cwd: a, env: { ...env, RUNS: runs }, encoding: "utf8" });
    const out = r.stdout + r.stderr;
    assert.match(out, /moved during the gate: rebasing and checking again/, "main moved during the first gate");
    assert.equal(r.status, 0, out);
    assert.ok(existsSync(join(a, "app/node_modules/.submitter-mark")), "the submitter's node_modules is untouched by the gate's `npm ci`");
    assert.ok(readdirSync(join(a, "app/node_modules")).includes("dep"));
    assert.equal(readFileSync(join(a, "app/node_modules/.package-lock.json"), "utf8"), lock, "a file the gate rewrote in place is the gate's own bytes, not the submitter's");
    assert.deepEqual(lines(runs).filter((x) => x.startsWith("ssr")), ["ssr", "ssr"], "the ssr step ran in each attempt, with its dependency");
    assert.match(git(w.bare, "log", "-1", "--format=%s", "main"), /app: a change/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("push: main moves the lock file during the gate: the retry's worktree has the new dependencies (not the first attempt's), the submitter's node_modules is as it was", () => {
  const dir = mkdtempSync(join(tmpdir(), "retry-install-"));
  try {
    const w = world(dir);
    const { a, other, lock, runs, moved } = ready(w, dir);
    setProject(a, (cfg) => {
      cfg.areas[0].steps.find((s) => s.id === "ssr").cmd = node("let n='none';try{require('dep2');n='dep2'}catch{}require('fs').appendFileSync(process.env.RUNS,'ssr '+n+'\\n')");
      // main adds a dependency (dep2) while the first gate runs
      cfg.areas[0].steps.push(mover(moved, other, `fs.mkdirSync(${JSON.stringify(join(other, "app/dep2"))},{recursive:true});fs.writeFileSync(${JSON.stringify(join(other, "app/dep2/package.json"))},'{"name":"dep2","version":"1.0.0","main":"index.js"}');fs.writeFileSync(${JSON.stringify(join(other, "app/dep2/index.js"))},'module.exports=2;');const pj=JSON.parse(fs.readFileSync(${JSON.stringify(join(other, "app/package.json"))}));pj.dependencies.dep2='file:dep2';fs.writeFileSync(${JSON.stringify(join(other, "app/package.json"))},JSON.stringify(pj));cp.execFileSync('npm',['install','--package-lock-only','--no-audit','--no-fund','--offline'],{cwd:${JSON.stringify(join(other, "app"))},stdio:'pipe'})`));
    });
    git(a, "push", "-q", "origin", "HEAD:main");
    git(other, "pull", "-q", "--rebase", "origin", "main");
    commitFile(a, "app/change.txt", "a change\n", "app: a change");

    const r = spawnSync(join(a, ".spire/tool/push"), [], { cwd: a, env: { ...env, RUNS: runs }, encoding: "utf8" });
    const out = r.stdout + r.stderr;
    assert.match(out, /moved during the gate: rebasing and checking again/, "main moved during the first gate");
    assert.equal(r.status, 0, out);
    assert.deepEqual(lines(runs).filter((x) => x.startsWith("ssr")), ["ssr none", "ssr dep2"], "the first attempt had the old lock's dependencies, the retry the new one's");
    assert.ok(existsSync(join(a, "app/node_modules/.submitter-mark")), "the submitter's node_modules is untouched");
    assert.equal(existsSync(join(a, "app/node_modules/dep2")), false, "and holds what it held");
    assert.equal(readFileSync(join(a, "app/node_modules/.package-lock.json"), "utf8"), lock);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("push: a link at node_modules in the gate's tree (a tracked one) is dropped, never installed through: the submitter's node_modules it points to is untouched", () => {
  const dir = mkdtempSync(join(tmpdir(), "retry-install-"));
  try {
    const w = world(dir);
    const { a, runs } = ready(w, dir); // `a` holds the dependencies the link names
    const b = w.clone("b");
    setProject(b, (cfg) => cfg.areas[0].steps.push({ id: "reinstall", dir: "app", cmd: ["npm", "ci", "--no-audit", "--no-fund", "--offline"] }));
    symlinkSync(join(a, "app/node_modules"), join(b, "app/node_modules"));
    git(b, "add", "-f", "app/node_modules");
    git(b, "commit", "-qm", "app: a link to dependencies (a mistake)");
    const r = spawnSync(join(b, ".spire/tool/push"), [], { cwd: b, env: { ...env, RUNS: runs }, encoding: "utf8" });
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.ok(existsSync(join(a, "app/node_modules/.submitter-mark")), "what the link named is untouched by the gate's `npm ci`");
    assert.ok(readdirSync(join(a, "app/node_modules")).includes("dep"));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
