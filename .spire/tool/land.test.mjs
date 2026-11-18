// Admission: this file shows that `land` (.spire/tool/land) classifies a change from its real paths, runs the areas it touches (its world has a small project file of its own: areas.test.mjs
// and checks.test.mjs hold the areas' own proofs), refuses a landing with a failing area (13) and has no flag that lands past one, lands a docs change
// on a bare remote as a plain fast-forward with its Land-* attestation on the commit,
// rebases a second clone that finds main moved and lands it, refuses without a PR when the remote refuses main, and opens the branch path (exit 20) only under LAND_VIA_PR=1. It would
// fail if the classifier, the lander wiring, the attestation or the fallback broke. Oracle: real git repositories in a temp dir and the real vendored lander;
// nothing here is mocked (a PR is not opened: there is no GitHub; that step is run live and quoted in the PR).
import assert from "node:assert/strict";
import { execFile, execFileSync, spawn, spawnSync } from "node:child_process";
import { generateKeyPairSync } from "node:crypto";
import { createServer } from "node:http";
import {
  chmodSync,
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { attestedBy } from "./attest.mjs";
import { startStub } from "./fixtures/app-stub.mjs";
import { SKIPPED_PREFIXES } from "./fixtures/project/scripts/ci/code-scope.mjs";
const DOCS = SKIPPED_PREFIXES[0].replace(/\/$/, ""); // a path CI skips, taken from its one definition (the scope guard forbids naming one)
const repo = join(fileURLToPath(import.meta.url), "../..");
const env = {
  ...process.env,
  BOX_ID: "", // the tests may run inside a box (box land): a world is no box unless a test says so
  LAND_INSTANCE_URL: "",
  LAND_INSTANCE_DB: "",
  GIT_AUTHOR_NAME: "t",
  GIT_AUTHOR_EMAIL: "t@t",
  GIT_COMMITTER_NAME: "t",
  GIT_COMMITTER_EMAIL: "t@t",
};
const git = (cwd, ...a) =>
  execFileSync("git", a, { cwd, env, encoding: "utf8" });
const land = (cwd, ...a) =>
  spawnSync(join(cwd, ".spire/tool/land"), a, {
    cwd,
    env,
    encoding: "utf8",
  });
const landViaPr = (cwd) =>
  spawnSync(join(cwd, ".spire/tool/land"), [], {
    cwd,
    env: { ...env, LAND_VIA_PR: "1" },
    encoding: "utf8",
  });
const lastJson = (r) => JSON.parse(r.stdout.trim().split("\n").at(-1));

// a bare remote whose main holds the real lander files, and a clone of it
function world({ council = false, tests = false, reading = false } = {}) {
  const dir = mkdtempSync(join(tmpdir(), "land-test-"));
  const seed = join(dir, "seed"),
    bare = join(dir, "remote.git");
  mkdirSync(join(seed, "scripts/ci"), { recursive: true });
  mkdirSync(join(seed, ".spire"), { recursive: true });
  // the tool is vendored whole into .spire/tool/ (LAYOUT.md); the project's own file is the example's, and its readByChecks module is the fixture
  cpSync(join(repo, "tool"), join(seed, ".spire/tool"), { recursive: true });
  cpSync(join(repo, "examples/spire-venue/land.json"), join(seed, ".spire/land.json"));
  cpSync(join(repo, "tool/fixtures/project/scripts/ci/code-scope.mjs"), join(seed, "scripts/ci/code-scope.mjs"));
  cpSync(join(repo, "tool/fixtures/principles.md"), join(seed, ".spire/principles.md")); // the project's own law (numbered rules): the council reads it and the tool's default viewpoints
  // the world's project file: Spire's sensitive paths, and two small areas of its own (a docs one and a code one), so that a landing here runs real steps in a second, not Spire's whole suite
  const cfgPath = join(seed, ".spire/land.json");
  const cfg = JSON.parse(readFileSync(cfgPath, "utf8"));
  cfg.areas = [
    {
      name: "docs",
      paths: ["docs/**", "**/*.md"],
      steps: [{ id: "doc-check", dir: ".", cmd: ["node", "-e", "0"] }],
    },
    {
      name: "code",
      paths: ["server/**", "app/**", "shared/**", "scripts/**"],
      steps: [
        {
          id: "code-check",
          dir: ".",
          cmd: ["node", "-e", "process.exit(Number(process.env.CODE_RED || 0))"],
        },
      ],
    },
  ];
  // `tests`: the code area also carries a test group (a step with a `list`: the files named *Tests.mjs under server/), each of which appends its id to $RUNS
  if (tests) cfg.areas[1].steps.push({ id: "tests:code", dir: ".", list: ["git", "ls-files", "--", "server/*Tests.mjs"], cmd: ["node", "--test"] });
  // `reading`: two steps that name what they read (`reads`, land.json): each appends its id to $RUNS and is red while a file `bad.txt` is in the directory it reads
  if (reading) {
    const reads = (id, dir) => ({ id, dir: ".", reads: [`${dir}/**`], cmd: ["node", "-e", `require("fs").appendFileSync(process.env.RUNS, "${id}\\n"); process.exit(require("fs").existsSync("${dir}/bad.txt") ? 1 : 0)`] });
    // `after-check` waits for scripts-check (it reads what that step makes): a group of steps is carried whole or not at all
    cfg.areas[1].steps.push(reads("server-check", "server"), reads("scripts-check", "scripts"), { ...reads("after-check", DOCS), after: ["scripts-check"] }, { id: "always-check", dir: ".", cmd: ["node", "-e", 'require("fs").appendFileSync(process.env.RUNS, "always-check\\n")'] });
  }
  delete cfg.checks.install; // this world has no dependencies to install
  // `council`: false: the project removes the whole council (a file of each default's name with `calls: []`), so a landing here needs no model; true: it keeps the Warden alone (the tests of
  // the council ask for it: world({ council: true })); "full": the default council as shipped
  if (council !== "full") {
    if (!council) cfg.sensitive = { globs: [], nameGlobs: [], always: [] };
    mkdirSync(join(seed, ".spire/review"), { recursive: true });
    for (const role of ["inspector", "steward", "architect", "designer", ...(council ? [] : ["warden"])]) writeFileSync(join(seed, ".spire/review", `${role}.md`), `---\nrole: ${role}\ncalls: []\n---\nremoved by this project\n`);
  }
  // the harness's own variables (the red switch, the runs file, the author of a commit a step makes) reach the steps by the step's own `pass` (step-env.mjs)
  for (const a of cfg.areas) for (const st of a.steps) st.pass ??= ["CODE_RED", "RUNS", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"];
  writeFileSync(cfgPath, JSON.stringify(cfg));
  mkdirSync(join(seed, DOCS), { recursive: true });
  writeFileSync(join(seed, DOCS, "README.md"), "notes\n");
  git(seed, "init", "-q", "-b", "main");
  git(seed, "add", "-A");
  git(seed, "commit", "-qm", "seed");
  git(dir, "clone", "-q", "--bare", seed, bare);
  const clone = (n) => {
    git(dir, "clone", "-q", bare, n);
    return join(dir, n);
  };
  return { dir, bare, clone };
}
const commitDocs = (c, name) => {
  writeFileSync(join(c, DOCS, name), `${name}\n`);
  git(c, "add", "-A");
  git(c, "commit", "-qm", `docs: ${name}`);
};

test("the areas and the members, from real commits: docs, code, a path no area owns, a sensitive path", () => {
  const w = world({ council: "full" });
  const c = w.clone("c");
  const cls = (files) => {
    git(c, "checkout", "-q", "-B", "t", "origin/main");
    for (const [p, n] of Object.entries(files)) {
      mkdirSync(join(c, p, ".."), { recursive: true });
      writeFileSync(
        join(c, p),
        typeof n === "string"
          ? n
          : Array.from({ length: n }, (_, i) => `l${i}`).join("\n") + "\n",
      );
    }
    git(c, "add", "-A");
    git(c, "commit", "-qm", "x");
    return JSON.parse(
      execFileSync(
        "node",
        [join(c, ".spire/tool/classify.mjs"), "origin/main", "HEAD"],
        { cwd: c, encoding: "utf8" },
      ),
    );
  };
  try {
    assert.deepEqual(cls({ [`${DOCS}/a.md`]: 3 }).areas.names, ["docs"]);
    assert.deepEqual(cls({ "server/src/y.ts": 3 }).areas.names, ["code"]);
    // a path no area owns and no area encloses: the full suite (every area), never a skip
    assert.deepEqual(cls({ ".node-version": "22\n" }).areas.names, ["docs", "code"]);
    assert.equal(cls({ ".node-version": "22\n" }).areas.full, true);
    // the members are chosen by the viewpoints' own `calls`, never by size or by a label: every default calls `@all`, so the whole council sits on code, a document and a test alike
    const FULL = ["architect", "designer", "inspector", "steward", "warden"];
    for (const files of [{ "server/src/x.ts": 5 }, { "server/src/x.ts": 400 }, { [`${DOCS}/a.md`]: 3 }, { "docs/venue.md": 3 }, { "server/src/x.test.ts": 5 }, { "server/src/authority/x.ts": 5 }, { "app/src/x.tsx": 30 }]) {
      assert.deepEqual(cls(files).council.sort(), FULL, JSON.stringify(files));
    }
    assert.equal("class" in cls({ "server/src/x.ts": 5 }), false, "no class: the label is gone");
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

test("land: a docs change lands as a fast-forward with its attestation", () => {
  const w = world();
  const c = w.clone("c");
  try {
    commitDocs(c, "one.md");
    const r = land(c);
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.equal(lastJson(r).result, "landed");
    // F4: the audit of the record on main is printed with the landing, before the typed result line
    assert.match(r.stdout, /^land: attest: 1\/2 commits of origin\/main \(last 300\) carry a record of the tree that landed \(50\.0%\)$/m);
    const msg = git(w.bare, "log", "-1", "--format=%B", "main");
    assert.match(
      msg,
      /^Land-Checks: areas=docs tests=0 files=0 checks=doc-check:\ds total=\ds tree=[0-9a-f]{12}$/m,
    );
    assert.match(
      msg,
      /^Land-Council: none needed \(no path calls for a member\)$/m,
    );
    assert.equal(git(w.bare, "rev-list", "--count", "main").trim(), "2");
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

// Admission: the evidence the land-evidence gate on main reads (Land-Checks and Land-Council) is on EVERY commit of a multi-commit landing, and there is no way past it: `--override` is not a flag
// (exit 2: they never were; `--override` is the owner's route, below), an `Override:` trailer a commit message carries is stripped, never kept. It would fail if the attestation went on the tip only, or if an override still passed. Oracle: real git, the real
// lander and `land`, a docs change (no council, no key).
test("land: every commit of a landing carries the evidence; no flag skips a check or the council and an Override trailer typed by hand is never kept", () => {
  const w = world();
  const c = w.clone("c");
  try {
    commitDocs(c, "one.md");
    commitDocs(c, "two.md");
    for (const flag of [["--council-skip", "down"], ["--ui-skip", "no browser"], ["--skip-checks"]]) {
      const r = land(c, ...flag);
      assert.equal(r.status, 2, `${flag[0]}: ${r.stdout}${r.stderr}`);
      assert.match(r.stderr, /usage: land/);
    }
    assert.equal(git(w.bare, "rev-list", "--count", "main").trim(), "1", "nothing landed");
    // an Override trailer on the tip AND on a commit below it (the gate reads every commit of a push)
    git(c, "commit", "-q", "--amend", "-m", "docs: two.md", "-m", "Override: smuggled past the gate");
    git(c, "rebase", "-q", "--exec", 'git log -1 --format=%s | grep -q "docs: one.md" && git commit -q --amend -m "docs: one.md" -m "Override: smuggled lower down" || true', "origin/main");
    const r = land(c);
    assert.equal(r.status, 0, r.stdout + r.stderr);
    const bodies = git(w.bare, "log", "-2", "--format=%B%x1e", "main")
      .split("\x1e")
      .map((b) => b.trim())
      .filter(Boolean);
    assert.equal(bodies.length, 2);
    for (const b of bodies) {
      assert.match(b, /^Land-Checks: areas=docs /m);
      assert.match(
        b,
        /^Land-Council: none needed \(no path calls for a member\)$/m,
      );
      assert.doesNotMatch(b, /^Override: /m);
    }
    // F2: the earlier commit says it is part of the tip's tree, and the verifier counts both commits of the push
    const tipTree = git(w.bare, "rev-parse", "main^{tree}").trim();
    assert.doesNotMatch(bodies[0], /part-of=/);
    assert.match(bodies[1], new RegExp(`^Land-Checks: .* tree=${tipTree.slice(0, 12)} part-of=${tipTree.slice(0, 12)}$`, "m"));
    const pushed = git(w.bare, "rev-list", "--reverse", "main~2..main").trim().split("\n").map((sha) => ({ sha, tree: git(w.bare, "rev-parse", `${sha}^{tree}`).trim(), body: git(w.bare, "log", "-1", "--format=%B", sha) }));
    assert.deepEqual(attestedBy(pushed).map((r) => r.status), ["push", "tree"]);
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

// Admission: a project's file may mark paths `held` for a person: a change that touches one stops at once (exit 22) with the reason, before anything is checked or pushed, and there is no flag that
// lands it. It would fail if a held path were checked and landed, or if the stop named no reason. Oracle: the real `land` on a world whose file holds docs/frozen/.
test("land: a held path stops at once (22) with its reason; nothing is checked or pushed", () => {
  const w = world();
  const c = w.clone("c");
  try {
    const p = join(c, ".spire/land.json");
    const cfg = JSON.parse(readFileSync(p, "utf8"));
    cfg.held = [{ paths: ["docs/frozen/**"], reason: "the law files are landed by a person" }];
    writeFileSync(p, JSON.stringify(cfg));
    git(c, "commit", "-qam", "the project holds docs/frozen for a person");
    git(c, "push", "-q", "origin", "HEAD:main");
    mkdirSync(join(c, "docs/frozen"), { recursive: true });
    writeFileSync(join(c, "docs/frozen/law.md"), "law\n");
    git(c, "add", "-A");
    git(c, "commit", "-qm", "docs: a held file");
    const r = land(c);
    assert.equal(r.status, 22, r.stdout + r.stderr);
    assert.match(r.stdout, /held for a person: the law files are landed by a person \(docs\/frozen\/law\.md\)/);
    assert.equal(lastJson(r).result, "held-for-a-person");
    assert.doesNotMatch(r.stdout, /areas:|council:|checks ok/);
    assert.equal(git(w.bare, "rev-list", "--count", "main").trim(), "2", "only the project's own file landed earlier");
    // a change that edits the file to drop the hold is judged by the base's file: it still stops
    const dropped = JSON.parse(readFileSync(p, "utf8"));
    delete dropped.held;
    writeFileSync(p, JSON.stringify(dropped));
    git(c, "commit", "-qam", "the change drops its own hold");
    const sneaky = land(c);
    assert.equal(sneaky.status, 22, sneaky.stdout + sneaky.stderr);
    // a change that does not touch it lands
    git(c, "fetch", "-q");
    git(c, "checkout", "-q", "-B", "other", "origin/main");
    commitDocs(c, "free.md");
    assert.equal(land(c).status, 0);
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

test("land: a second clone that finds main moved rebases and lands; a refused push to main is a plain stop (exit 1, no branch, no PR); LAND_VIA_PR=1 pushes the branch (exit 20)", () => {
  const w = world();
  const a = w.clone("a"),
    b = w.clone("b");
  try {
    commitDocs(a, "from-a.md");
    commitDocs(b, "from-b.md");
    const [ra, rb] = [land(a), land(b)]; // b's base is stale: the lander rebases it onto what a landed (the live proof races them in parallel)
    assert.equal(ra.status, 0, ra.stdout + ra.stderr);
    assert.equal(rb.status, 0, rb.stdout + rb.stderr);
    assert.equal(git(w.bare, "rev-list", "--count", "main").trim(), "3");

    writeFileSync(
      join(w.bare, "hooks/pre-receive"),
      '#!/bin/sh\nwhile read o n ref; do [ "$ref" = refs/heads/main ] && { echo "remote: error: GH006: Protected branch update failed for $ref." >&2; exit 1; }; done\nexit 0\n',
    );
    chmodSync(join(w.bare, "hooks/pre-receive"), 0o755);
    git(w.bare, "config", "core.hooksPath", join(w.bare, "hooks")); // a host's global core.hooksPath must not replace this bare repo's own hook
    git(a, "fetch", "-q");
    git(a, "checkout", "-q", "-B", "prot", "origin/main");
    commitDocs(a, "prot.md");
    const refused = land(a);
    assert.equal(refused.status, 1, refused.stdout + refused.stderr);
    assert.doesNotMatch(refused.stdout, /opening a PR/);
    assert.equal(git(w.bare, "branch", "--list", "prot").trim(), "");
    git(a, "checkout", "-q", "-B", "prot2", "origin/main");
    commitDocs(a, "prot2.md");
    const p = landViaPr(a);
    assert.equal(p.status, 20, p.stdout + p.stderr);
    assert.match(p.stdout, /LAND_VIA_PR=1: pushing branch prot2/);
    assert.match(git(w.bare, "branch", "--list", "prot2"), /prot2/);
    assert.equal(git(w.bare, "rev-list", "--count", "main").trim(), "3");
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

// Admission: this test shows the cheaper second rebase through the real `land` and `push`. A change's test group passes once; main then moves in a document (twice) and the change is rebased onto it, first by the
// lander's gate (`push`: its scratch worktree) and then by `land` itself: neither runs the group again, the cheap step runs each time, and the Land-Checks trailer of the commit that lands says by area where the pass
// was carried from. It would fail if either entry did not name the tree it carried from, if a rebase ran the group again, or if the evidence said the group ran on the tree that landed. Oracle: real git, the vendored tool, a
// bare remote and real `node --test` files that append to a file.
test("land: a change rebased over a main that moved in a document does not run its test group again, in the lander's gate and in land; Land-Checks says where it was carried from", () => {
  const w = world({ tests: true });
  const a = w.clone("a"),
    b = w.clone("b");
  try {
    const runs = join(w.dir, "runs");
    const { NODE_TEST_CONTEXT: _context, ...own } = env; // a nested `node --test` starts as a runner of its own, not as a child of this one
    const x = { ...own, RUNS: runs };
    const ran = () => (existsSync(runs) ? readFileSync(runs, "utf8").split("\n").filter(Boolean) : []);
    // main already has a test file the change does not touch: it carries over a rebase, the change's own test file re-runs (carry.mjs)
    mkdirSync(join(a, "server"), { recursive: true });
    writeFileSync(join(a, "server/zTests.mjs"), 'import test from "node:test";\ntest("z", () => {});\n');
    git(a, "add", "server/zTests.mjs");
    git(a, "commit", "-qm", "server: an untouched test");
    assert.equal(land(a).status, 0);
    git(b, "pull", "-q", "--rebase", "origin", "main");
    mkdirSync(join(b, "server"), { recursive: true });
    writeFileSync(join(b, "server/bTests.mjs"), 'import { appendFileSync } from "node:fs";\nimport test from "node:test";\ntest("t", () => appendFileSync(process.env.RUNS, "tests:code\\n"));\n');
    git(b, "add", "-A");
    git(b, "commit", "-qm", "server: a change with its test");
    const checked = spawnSync(join(b, ".spire/tool/land"), ["--dry-run"], { cwd: b, env: x, encoding: "utf8" });
    assert.equal(checked.status, 0, checked.stdout + checked.stderr);
    assert.deepEqual(ran(), ["tests:code"]);
    const t0 = git(b, "rev-parse", "HEAD^{tree}").trim().slice(0, 12);

    commitDocs(a, "one.md");
    assert.equal(land(a).status, 0);
    const gate = spawnSync(join(b, ".spire/tool/push"), ["--dry-run"], { cwd: b, env: x, encoding: "utf8" });
    assert.equal(gate.status, 0, gate.stdout + gate.stderr);
    assert.deepEqual(ran(), ["tests:code", "tests:code"], "the lander's gate carried the pass over the rebase and re-ran only the own test file");

    commitDocs(a, "two.md");
    assert.equal(land(a).status, 0);
    const r = spawnSync(join(b, ".spire/tool/land"), [], { cwd: b, env: x, encoding: "utf8" });
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /rebased onto origin\/main/);
    assert.deepEqual(ran(), ["tests:code", "tests:code", "tests:code"], "land carried it too, re-running only the own test file");
    const msg = git(w.bare, "log", "-1", "--format=%B", "main");
    assert.match(msg, new RegExp(`^Land-Checks: .*checks=.*tests:code\\(1 re-run of 2, rest carried from ${t0}\\).* total=\\ds tree=${git(w.bare, "rev-parse", "main^{tree}").trim().slice(0, 12)}`, "m"));
    assert.match(msg, /code-check:\ds/); // the cheap step ran on the tree that landed
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

// Admission: the push gate re-runs a step only when something the step READS came in under the rebase (carry.mjs). Three steps of the code area besides code-check: `always-check` names nothing it reads (always runs), `server-check` reads server/**, `scripts-check`
// reads scripts/**. The change is in app/. The gate's first run passes all three; main then moves in a document (nothing re-runs but code-check), in scripts/ (scripts-check runs, server-check is carried), and in server/ in a file that
// makes server-check red: the step runs on the FINAL tree (the rebased combination), the push is refused (13) and main does not move. It would fail if a step was carried over a rebase that brought in a file it reads, if the
// step with no `reads` was carried, or if a landing could skip the step on the tree that lands. Oracle: real git, a bare remote, the vendored tool, real steps that append their id to a file.
test("push: a step that names what it reads is carried over a rebase that brings in nothing it reads, runs when a file it reads came in, and the final tree is the one that is gated", () => {
  const w = world({ reading: true });
  const a = w.clone("a"),
    b = w.clone("b");
  try {
    const runs = join(w.dir, "runs");
    const { NODE_TEST_CONTEXT: _context, ...own } = env;
    const x = { ...own, RUNS: runs };
    const ran = () => (existsSync(runs) ? readFileSync(runs, "utf8").split("\n").filter(Boolean).sort() : []);
    const fresh = () => rmSync(runs, { force: true });
    const onMain = (path, text, direct = false) => {
      git(a, "pull", "-q", "--rebase", "origin", "main");
      mkdirSync(join(a, path, ".."), { recursive: true });
      writeFileSync(join(a, path), text);
      git(a, "add", "-A");
      git(a, "commit", "-qm", `main: ${path}`);
      if (direct) git(a, "push", "-q", "origin", "HEAD:main"); // a file that makes a step red lands another way than through the gate (a push of a person's), to show the next gate catches it
      else {
        const l = spawnSync(join(a, ".spire/tool/land"), [], { cwd: a, env: x, encoding: "utf8" });
        assert.equal(l.status, 0, l.stdout + l.stderr);
      }
    };
    mkdirSync(join(b, "app"), { recursive: true });
    writeFileSync(join(b, "app/x.txt"), "x\n");
    git(b, "add", "-A");
    git(b, "commit", "-qm", "app: a change");
    const all = ["after-check", "always-check", "scripts-check", "server-check"];
    const first = spawnSync(join(b, ".spire/tool/land"), ["--dry-run"], { cwd: b, env: x, encoding: "utf8" });
    assert.equal(first.status, 0, first.stdout + first.stderr);
    assert.deepEqual(ran(), all, "the first run runs every step");

    // main moved in a document: server-check reads none of it and is carried; always-check says nothing about what it reads and runs again; after-check reads it, and scripts-check, which it waits for, runs with it (a group is carried whole or not at all)
    onMain(`${DOCS}/one.md`, "one\n");
    fresh();
    const doc = spawnSync(join(b, ".spire/tool/push"), ["--dry-run"], { cwd: b, env: x, encoding: "utf8" });
    assert.equal(doc.status, 0, doc.stdout + doc.stderr);
    assert.deepEqual(ran(), ["after-check", "always-check", "scripts-check"], "server-check was carried; the group of two ran whole");

    // main moved in scripts/: the step that reads it runs, the one that reads server/ is carried
    onMain("scripts/tool.txt", "a tool\n");
    fresh();
    const scripts = spawnSync(join(b, ".spire/tool/push"), ["--dry-run"], { cwd: b, env: x, encoding: "utf8" });
    assert.equal(scripts.status, 0, scripts.stdout + scripts.stderr);
    assert.deepEqual(ran(), ["after-check", "always-check", "scripts-check"], "the step that reads scripts/ runs and the step that waits for it with it; server-check is carried");

    // main moved in server/, a file that makes the step that reads it red: it runs on the final tree and refuses the landing; nothing is pushed
    onMain("server/bad.txt", "red\n", true);
    fresh();
    const before = git(w.bare, "rev-parse", "main").trim();
    const red = spawnSync(join(b, ".spire/tool/push"), [], { cwd: b, env: x, encoding: "utf8" });
    assert.equal(red.status, 13, red.stdout + red.stderr);
    assert.deepEqual(ran(), ["always-check", "server-check"], "server-check ran on the rebased tree; the group of scripts-check and after-check was carried");
    assert.equal(git(w.bare, "rev-parse", "main").trim(), before, "a red gate lands nothing");
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

test("push: what lands is the tree that was gated, and the carried steps are named in Land-Checks with the tree they ran on", () => {
  const w = world({ reading: true });
  const a = w.clone("a"),
    b = w.clone("b");
  try {
    const runs = join(w.dir, "runs");
    const { NODE_TEST_CONTEXT: _context, ...own } = env;
    const x = { ...own, RUNS: runs };
    mkdirSync(join(b, "app"), { recursive: true });
    writeFileSync(join(b, "app/x.txt"), "x\n");
    git(b, "add", "-A");
    git(b, "commit", "-qm", "app: a change");
    const t0 = (() => {
      const r = spawnSync(join(b, ".spire/tool/land"), ["--dry-run"], { cwd: b, env: x, encoding: "utf8" });
      assert.equal(r.status, 0, r.stdout + r.stderr);
      return git(b, "rev-parse", "HEAD^{tree}").trim().slice(0, 12);
    })();
    mkdirSync(join(a, "app"), { recursive: true });
    writeFileSync(join(a, "app/y.txt"), "y\n"); // main moves in a file none of the three steps reads
    git(a, "add", "-A");
    git(a, "commit", "-qm", "app: another");
    const m = spawnSync(join(a, ".spire/tool/land"), [], { cwd: a, env: x, encoding: "utf8" });
    assert.equal(m.status, 0, m.stdout + m.stderr);
    const r = spawnSync(join(b, ".spire/tool/land"), [], { cwd: b, env: x, encoding: "utf8" });
    assert.equal(r.status, 0, r.stdout + r.stderr);
    const msg = git(w.bare, "log", "-1", "--format=%B", "main");
    const tree = git(w.bare, "rev-parse", "main^{tree}").trim().slice(0, 12);
    assert.match(msg, new RegExp(`checks=.*server-check:carried from ${t0}.*scripts-check:carried from ${t0}.*after-check:carried from ${t0}.* total=\\ds tree=${tree}`), msg);
    assert.match(msg, /code-check:\ds/); // the step that says nothing about what it reads ran on the tree that landed
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

// Admission: a member that cannot answer (no model route here) is never a stop: the change lands and the Land-Council trailer records the member as unable to review, and the output says it did not block.
// It would fail if a silent member stopped a landing (exit 15), or landed it without a record. Oracle: the real `land` and `council` with no key in a real git world; the check step is the one thing replaced.
test("land: a member that cannot answer is recorded as unable to review and the change lands (no key)", () => {
  const w = world({ council: true });
  const c = w.clone("c");
  try {
    // the checks need a whole npm install of the venue: this world has none, so the check step is the one thing replaced; the council is the real command
    writeFileSync(
      join(c, ".spire/tool/checks.sh"),
      '#!/usr/bin/env bash\necho "checks=stub" | tee "$LAND_CHECKS_OUT"\n',
    );
    mkdirSync(join(c, "server/src/authority"), { recursive: true });
    mkdirSync(join(c, "server"), { recursive: true });
    cpSync(
      join(repo, "tool/fixtures/model-registry.json"),
      join(c, "server/model-registry.json"),
    );
    writeFileSync(
      join(c, "server/src/authority/x.ts"),
      Array.from({ length: 40 }, (_, i) => `l${i}`).join("\n") + "\n",
    );
    git(c, "add", "-A");
    git(c, "commit", "-qm", "server change");
    const noKey = {
      ...env,
      PATH: `${dirname(process.execPath)}:/usr/bin:/bin`,
      COUNCIL_MODEL_KEY: "",
      COUNCIL_MODEL_URL: "",
    };
    const run = (...a) =>
      spawnSync(
        join(c, ".spire/tool/land"),
        a,
        {
          cwd: c,
          env: noKey,
          encoding: "utf8",
        },
      );
    const s = run("--council-skip", "the council is down"); // not a flag any more
    assert.equal(s.status, 2, s.stdout + s.stderr);
    assert.equal(git(w.bare, "rev-list", "--count", "main").trim(), "1");
    const r = run();
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /UNABLE TO REVIEW/);
    assert.match(r.stdout, /did not block/);
    assert.match(r.stdout, /checks ok/);
    assert.equal(r.stderr.split("\n").filter((l) => /^land: ERROR: the council cannot answer: COUNCIL_MODEL_KEY missing/.test(l)).length, 1, r.stderr); // the loud line, once
    assert.ok(r.stdout.indexOf("checks ok") > -1 && r.stderr.indexOf("COUNCIL_MODEL_KEY") > -1);
    assert.equal(lastJson(r).result, "landed");
    assert.equal(git(w.bare, "rev-list", "--count", "main").trim(), "2"); // landed
    assert.match(git(w.bare, "log", "-1", "--format=%B", "main"), /^Land-Council: ran: warden=unable to review \(COUNCIL_MODEL_KEY missing\) \(models fast, diff [0-9a-f]+\)$/m);
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

// The council and the checks run at the same time and the first red check stops the council (a red check made the council wait for nothing); a council that answers first never stops the checks, and a council that
// crashes is recorded as unable to review. It would fail if a finding stopped the checks, if the council were waited for after a red check, if a crashed council stopped the landing, or if they ran one after the other.
// Oracle: the real `land` with a check that would finish after 40 s, and a council that would answer after 60 s; the clock bounds each case.
test("land: the council and the checks run together; a red check stops the council at once, an early advice stops nothing, a crashed council is a note", () => {
  const w = world({ council: true });
  const c = w.clone("c");
  try {
    mkdirSync(join(c, "server/src/authority"), { recursive: true });
    writeFileSync(join(c, "server/src/authority/x.ts"), Array.from({ length: 40 }, (_, i) => `l${i}`).join("\n") + "\n");
    git(c, "add", "-A");
    git(c, "commit", "-qm", "server change");
    const marks = join(c, "..", `marks-${process.pid}`);
    mkdirSync(marks, { recursive: true });
    const stub = (name, body) => {
      writeFileSync(join(c, ".spire/tool", name), `#!/usr/bin/env bash\n${body}\n`);
      chmodSync(join(c, ".spire/tool", name), 0o755);
      git(c, "add", "-A");
      git(c, "commit", "-qm", `stub ${name}`);
    };
    const land = () => {
      const t = Date.now();
      const r = spawnSync(join(c, ".spire/tool/land"), [], { cwd: c, env, encoding: "utf8" });
      return { r, took: (Date.now() - t) / 1000 };
    };
    // a red check: the council, which would answer after 60 s, is stopped and not waited for
    stub("checks.sh", `touch ${marks}/c-start; echo "land: check FAILED: x" >&2; exit 13`);
    stub("council", `touch ${marks}/k-start; sleep 60; touch ${marks}/k-end; echo '{"trailer":"x"}'`);
    let { r, took } = land();
    assert.equal(r.status, 13, r.stdout + r.stderr);
    assert.ok(took < 30, `a red check stops the council at once (${took} s)`);
    assert.match(r.stdout, /a failed check stops the council/);
    assert.ok(!existsSync(join(marks, "k-end")), "the council did not finish");
    assert.equal(git(w.bare, "rev-list", "--count", "main").trim(), "1");
    // advice that comes at once does not stop the checks: they finish (5 s here) and the change lands with the advice recorded
    stub("checks.sh", `touch ${marks}/c-start; sleep 5; touch ${marks}/c-end; echo checks=slow | tee "$LAND_CHECKS_OUT"`);
    stub("council", `echo "council: warden ADVICE"; echo '{"trailer":"ran: warden=advice(block P1) (models fast)"}'`);
    ({ r, took } = land());
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.ok(existsSync(join(marks, "c-end")), "the checks ran to their end beside the council's advice");
    assert.match(git(w.bare, "log", "-1", "--format=%B", "main"), /^Land-Council: ran: warden=advice\(block P1\) \(models fast\)$/m);
    // a council that crashes (exit 2): not a stop; the trailer records it
    git(c, "fetch", "-q");
    git(c, "reset", "-q", "--hard", "origin/main");
    mkdirSync(join(c, "server/src/authority"), { recursive: true });
    writeFileSync(join(c, "server/src/authority/y.ts"), "y\n");
    git(c, "add", "-A");
    git(c, "commit", "-qm", "another change");
    stub("council", `echo "council: boom" >&2; exit 2`);
    ({ r } = land());
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /council did not run \(exit 2\).*did not block/);
    assert.match(git(w.bare, "log", "-1", "--format=%B", "main"), /^Land-Council: unable to review \(the council exited 2\)$/m);
    rmSync(marks, { recursive: true, force: true });
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

// Admission: this test shows that a council finding is ADVICE and never a gate (captain ruling, 15 Nov 2026): a seeded duplicate that a member rates `fix` is printed ONCE by the first land, which lands the change in the same run
// (exit 0, main moved, the line "did not block" printed); the Land-Council trailer on the landed commit records member, rating and principle number. `--why-not TEXT` is the optional way to record why the agent did not follow it (a Land-Why-Not trailer);
// a change that follows the advice lands clean. It would fail if the first land were stopped by the finding (the old behaviour: exit 17), or if the advice were landed unrecorded. Oracle: real git, the real lander, the real `land` and `council` processes, a real HTTP
// gateway that answers from what the member was given; the check step is the one thing replaced (this world has no npm install).
test("land: a finding rated fix is printed once and recorded, and the change lands in the same run (never 17)", async () => {
  const gateway = createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const user = JSON.parse(body).messages.at(-1).content;
      const seeded = /^\/\/ DUPLICATE_OF_FOLD$/m.test(
        typeof user === "string" ? user : JSON.stringify(user),
      );
      res.setHeader("content-type", "application/json");
      res.end(
        JSON.stringify({
          choices: [
            {
              finish_reason: "stop",
              message: {
                content: seeded
                  ? "verdict: request-changes\nhead: x\nfindings:\n- P1 | fix | server/src/authority/x.ts:41 | a second definition of the fold | delete it"
                  : "verdict: approve\nhead: x\nfindings:\n",
              },
            },
          ],
        }),
      );
    });
  });
  await new Promise((r) => gateway.listen(0, "127.0.0.1", r));
  const w = world({ council: true });
  const c = w.clone("c");
  try {
    writeFileSync(
      join(c, ".spire/tool/checks.sh"),
      '#!/usr/bin/env bash\necho "checks=stub" | tee "$LAND_CHECKS_OUT"\n',
    );
    mkdirSync(join(c, "server/src/authority"), { recursive: true });
    cpSync(
      join(repo, "tool/fixtures/model-registry.json"),
      join(c, "server/model-registry.json"),
    );
    const put = (dup) =>
      writeFileSync(
        join(c, "server/src/authority/x.ts"),
        Array.from({ length: 40 }, (_, i) => `l${i}`).join("\n") +
          `\n${dup ? "// DUPLICATE_OF_FOLD\n" : "const ok = 1;\n"}`,
      );
    put(true);
    git(c, "add", "-A");
    git(c, "commit", "-qm", "server change");
    const viaGateway = {
      ...env,
      PATH: `${dirname(process.execPath)}:/usr/bin:/bin`,
      COUNCIL_MODEL_KEY: "",
      COUNCIL_MODEL_URL: `http://127.0.0.1:${gateway.address().port}/api/v1`,
    };
    const run = (...a) =>
      new Promise((resolve) =>
        execFile(
          join(c, ".spire/tool/land"),
          a,
          { cwd: c, env: viaGateway, encoding: "utf8" },
          (e, stdout, stderr) =>
            resolve({ status: e ? e.code : 0, stdout, stderr }),
        ),
      );
    const first = await run();
    assert.equal(first.status, 0, first.stdout + first.stderr);
    assert.equal(first.stdout.match(/FIX P1 \| server\/src\/authority\/x\.ts:41/g)?.length, 1, "the advice is printed once");
    assert.match(first.stdout, /did not block/);
    assert.equal(lastJson(first).result, "landed");
    assert.equal(mainCount(w), "2"); // landed on the first land
    const msg1 = git(w.bare, "log", "-1", "--format=%B", "main");
    assert.match(msg1, /^Land-Council: ran: warden=advice\(fix P1\) \(models fast, diff [0-9a-f]+\)$/m);
    assert.doesNotMatch(msg1, /Land-Why-Not/);
    // the agent declines the advice of another change in one line, with the optional --why-not: it lands in one run and the line is a trailer
    writeFileSync(join(c, "server/src/authority/y.ts"), `${Array.from({ length: 40 }, (_, i) => `l${i}`).join("\n")}\n// DUPLICATE_OF_FOLD\n`);
    git(c, "add", "-A");
    git(c, "commit", "-qm", "another duplicate");
    const why = await run("--why-not", "the second reader is the oracle");
    assert.equal(why.status, 0, why.stdout + why.stderr);
    assert.equal(mainCount(w), "3");
    const body = git(w.bare, "log", "-1", "--format=%B", "main");
    assert.match(body, /^Land-Council: ran: warden=advice\(fix P1\) \(models fast, diff [0-9a-f]+\)$/m);
    assert.match(body, /^Land-Why-Not: the second reader is the oracle$/m);
    // a change that follows the advice lands as well, clean
    put(false);
    git(c, "commit", "-qam", "repair the duplicate");
    const fixed = await run();
    assert.equal(fixed.status, 0, fixed.stdout + fixed.stderr);
    assert.equal(mainCount(w), "4");
    assert.match(git(w.bare, "log", "-1", "--format=%B", "main"), /^Land-Council: ran: warden=approve \(models fast, diff [0-9a-f]+\)$/m);
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
    gateway.close();
  }
});

const mainCount = (w) => git(w.bare, "rev-list", "--count", "main").trim();

// Admission: this test shows that a freeze the clone set for itself (`push --freeze`: a named owner and an expiry) stops `land` before it checks or pushes anything: frozen = exit 14 with main unmoved;
// a revert still lands; a thawed clone lands. It would fail if the freeze were not read, or read but not enforced. Oracle: real git and the real lander; no mocks. (No watchdog freeze is read: refs/land/status is not land's.)
test("land: a freeze the clone set for itself stops at 14, a revert lands, a thawed clone lands", () => {
  const w = world();
  const c = w.clone("c");
  const push = (...a) => execFileSync(join(c, ".spire/tool/push"), a, { cwd: c, env, encoding: "utf8" });
  try {
    git(c, "config", "user.email", "t@t");
    push("--freeze", "a person's freeze", "--owner", "adam", "--for", "1h");
    commitDocs(c, "one.md");
    const r = land(c);
    assert.equal(r.status, 14, r.stdout + r.stderr);
    assert.match(r.stdout, /main is frozen .*a person's freeze/);
    assert.equal(lastJson(r).result, "frozen");
    assert.equal(mainCount(w), "1");

    // a revert passes the freeze (the lander's rule)
    git(c, "checkout", "-q", "-B", "rev", "origin/main");
    writeFileSync(join(c, DOCS, "r.md"), "r\n");
    git(c, "add", "-A");
    git(c, "commit", "-qm", 'Revert "something bad"');
    const rv = land(c);
    assert.equal(rv.status, 0, rv.stdout + rv.stderr);
    assert.match(rv.stdout, /this change is a fix or a revert: landing/);
    assert.equal(mainCount(w), "2");

    push("--thaw");
    git(c, "checkout", "-q", "-B", "after", "origin/main");
    commitDocs(c, "two.md");
    const ok = land(c);
    assert.equal(ok.status, 0, ok.stdout + ok.stderr);
    assert.equal(mainCount(w), "3");
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

// Admission: these tests show that `land` rebases onto origin/main BEFORE it checks: the checks see the combination that lands (not the builder's older tree),
// a conflict stops with exit 11 naming the files and leaves the builder's commit as it was, and a clone behind main ends up on top of it. They would fail
// if the checks ran on the pre-rebase tree. Oracle: real git, the real lander; the check step is the one thing replaced (a script that records the tree it was
// given), since a whole npm install is not in this world.
test("land rebases first: the checks run on the combination that lands; a conflict is exit 11 with the files named", () => {
  const w = world();
  const a = w.clone("a");
  const b = w.clone("b");
  try {
    const stub =
      '#!/usr/bin/env bash\ngit rev-parse "HEAD^{tree}" >> "$(git rev-parse --path-format=absolute --git-common-dir)/checked-trees"\necho "checks=stub" | tee "${LAND_CHECKS_OUT:-/dev/null}"\n';
    commitDocs(a, "from-a.md");
    assert.equal(land(a).status, 0);
    writeFileSync(join(b, ".spire/tool/checks.sh"), stub); // part of b's own commit, so the lander's gate runs it too
    mkdirSync(join(b, "server/src"), { recursive: true });
    writeFileSync(
      join(b, "server/src/x.ts"),
      Array.from({ length: 40 }, (_, i) => `l${i}`).join("\n") + "\n",
    );
    git(b, "add", "-A");
    git(b, "commit", "-qm", "server: a change on an older main");
    const r = land(b);
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /rebased onto origin\/main/);
    const landed = git(w.bare, "rev-parse", "main^{tree}").trim();
    const checked = readFileSync(join(b, ".git/checked-trees"), "utf8")
      .trim()
      .split("\n");
    assert.equal(
      checked[0],
      landed,
      "the first check ran on the combination, not on the pre-rebase tree",
    );
    assert.ok(checked.every((t) => t === landed));
    assert.equal(
      git(b, "merge-base", "--is-ancestor", "origin/main~1", "HEAD") === "",
      true,
    );
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

test("land: a rebase conflict is exit 11, the files are named, and the builder's commit is as it was", () => {
  const w = world();
  const a = w.clone("a");
  const b = w.clone("b");
  try {
    for (const [c, text] of [
      [a, "from a\n"],
      [b, "from b\n"],
    ]) {
      writeFileSync(join(c, DOCS, "README.md"), text);
      git(c, "commit", "-qam", `docs: edit readme ${text.trim()}`);
    }
    assert.equal(land(a).status, 0);
    const before = git(b, "rev-parse", "HEAD").trim();
    const r = land(b);
    assert.equal(r.status, 11, r.stdout + r.stderr);
    assert.match(r.stdout, /conflict with main in: .*README\.md/);
    assert.equal(lastJson(r).result, "conflict");
    assert.equal(git(b, "rev-parse", "HEAD").trim(), before);
    assert.equal(
      git(b, "status", "--porcelain", "--untracked-files=no").trim(),
      "",
    );
    assert.equal(git(w.bare, "rev-list", "--count", "main").trim(), "2");
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

// Admission: this test shows that review is chosen by a change's PATHS and never by its size: a 5-line change to a sensitive path (authority, identity, credentials, the lander, the law)
// gets the Warden, and only the Warden; a change no reviewer's paths call for (plain code of any size, a document, a test) lands with none. It would fail if a small change were exempt from
// review (a size shortcut), if plain code sat a council, or if a document did. Oracle: real commits in a real repository, and the real `land` with no model key (a council that cannot
// answer is recorded as unable to review, so "the council was asked" is observable); the check step is the one thing replaced.
test("review is by the viewpoints' calls, not size: the Warden reads every change, a small plain one, a document and a test included", () => {
  const w = world({ council: true });
  const c = w.clone("c");
  try {
    const cls = (files) => {
      git(c, "checkout", "-q", "-B", "t", "origin/main");
      for (const [p, n] of Object.entries(files)) {
        mkdirSync(join(c, p, ".."), { recursive: true });
        writeFileSync(
          join(c, p),
          typeof n === "string"
            ? n
            : Array.from({ length: n }, (_, i) => `l${i}`).join("\n") + "\n",
        );
      }
      git(c, "add", "-A");
      git(c, "commit", "-qm", "x");
      return JSON.parse(
        execFileSync(
          "node",
          [join(c, ".spire/tool/classify.mjs"), "origin/main", "HEAD"],
          { cwd: c, encoding: "utf8" },
        ),
      );
    };
    for (const p of [
      "server/src/identity/x.ts",
      "server/src/authority/y.ts",
      "server/src/github/git-auth.ts",
      "server/src/usage/host-report.ts",
      ".spire/tool/z.sh",
      ".github/workflows/ci.yml",
      "shared/principles.md",
      "AGENTS.md",
      "REVIEW_LAWS.md",
      "SECURITY_STANDARDS.md",
      "server/src/github/credentials.ts",
      "server/src/x/tokenStore.ts",
      "server/src/x/sessions.ts",
      "server/src/x/oauthClient.ts",
    ]) {
      assert.deepEqual(cls({ [p]: 5 }).council, ["warden"], p); // however small, whatever the path: the Warden is not path-limited
    }
    for (const p of ["server/src/foo.ts", "docs/note.md", "server/src/identity/x.test.ts"]) assert.deepEqual(cls({ [p]: 5 }).council, ["warden"], p);

    // end to end: with no key every change lands with the Warden recorded unable to review (it was asked), a sensitive path, plain code and a document alike
    writeFileSync(
      join(c, ".spire/tool/checks.sh"),
      '#!/usr/bin/env bash\necho "checks=stub" | tee "$LAND_CHECKS_OUT"\n',
    );
    cpSync(
      join(repo, "tool/fixtures/model-registry.json"),
      join(c, "server/model-registry.json"),
    );
    git(c, "add", "-A");
    git(c, "commit", "-qm", "stub");
    git(c, "push", "-q", "origin", "HEAD:main");
    const noKey = {
      ...env,
      PATH: `${dirname(process.execPath)}:/usr/bin:/bin`,
      COUNCIL_MODEL_KEY: "",
      COUNCIL_MODEL_URL: "",
    };
    const run = () =>
      spawnSync(
        join(c, ".spire/tool/land"),
        [],
        {
          cwd: c,
          env: noKey,
          encoding: "utf8",
        },
      );
    for (const [file, status] of [
      ["server/src/identity/x.ts", 0],
      ["server/src/foo.ts", 0],
      ["docs/note.md", 0],
    ]) {
      git(c, "fetch", "-q");
      git(c, "checkout", "-q", "-B", `b-${file.length}-${file.replace(/\W/g, "")}`, "origin/main");
      mkdirSync(join(c, file, ".."), { recursive: true });
      writeFileSync(join(c, file), "a\nb\nc\nd\ne\n");
      git(c, "add", "-A");
      git(c, "commit", "-qm", `change ${file}`);
      const r = run();
      assert.equal(r.status, status, r.stdout + r.stderr);
      assert.match(r.stdout, /COUNCIL_MODEL_KEY missing/); // the Warden was asked, and no member could answer
    }
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

// Admission: a failing area refuses the landing and nothing else lets it through: `land` runs the areas the change touches, a red step of one is exit 13 with nothing pushed, the next landing of the
// fixed tree goes, and there is no flag that lands past a failing area (--ui-skip is gone: the browser job is the app-browser area's step, so a failing browser area is a failing area). It would
// fail if a red area landed, if a flag still skipped one, or if an area the change does not touch ran. Oracle: the real `land`, the real lander and real commits; the world's own two areas.
test("a failing area refuses the landing (13) before the push; a red result has no flag that lands it", () => {
  const w = world();
  const c = w.clone("c");
  try {
    git(c, "checkout", "-q", "-B", "red", "origin/main");
    mkdirSync(join(c, "server/src"), { recursive: true });
    writeFileSync(join(c, "server/src/x.ts"), "export const a = 1;\n");
    git(c, "add", "-A");
    git(c, "commit", "-qm", "server: a change");
    const red = spawnSync(join(c, ".spire/tool/land"), [], {
      cwd: c,
      env: { ...env, CODE_RED: "1" },
      encoding: "utf8",
    });
    assert.equal(lastJson(red).exit, 13, red.stdout + red.stderr);
    assert.equal(lastJson(red).result, "checks-failed");
    assert.match(red.stdout, /areas: code/);
    assert.match(red.stderr, /check FAILED: code-check/);
    assert.match(red.stdout, /checks failed \(exit 13\)/);
    // --retest runs only what failed, with no council and no push
    const retest = spawnSync(join(c, ".spire/tool/land"), ["--retest"], {
      cwd: c,
      env: { ...env, CODE_RED: "0" },
      encoding: "utf8",
    });
    assert.equal(retest.status, 0, retest.stdout + retest.stderr);
    assert.equal(lastJson(retest).result, "retest-green");
    assert.match(retest.stdout, /retest green: code-check:\ds/);
    assert.doesNotMatch(retest.stdout, /council:/);
    assert.equal(
      git(c, "ls-remote", "origin", "refs/heads/main").split("\t")[0],
      git(c, "rev-parse", "origin/main").trim(),
      "a retest pushes nothing",
    );
    assert.equal(
      git(c, "ls-remote", "origin", "refs/heads/main").split("\t")[0],
      git(c, "rev-parse", "origin/main").trim(),
      "nothing was pushed",
    );
    // no flag lands past it: --ui-skip is not a flag any more
    const flag = spawnSync(
      join(c, ".spire/tool/land"),
      ["--ui-skip", "no browser"],
      { cwd: c, env: { ...env, CODE_RED: "1" }, encoding: "utf8" },
    );
    assert.equal(flag.status, 2, flag.stdout + flag.stderr);
    assert.match(flag.stderr, /usage: land/);
    // the area the change does not touch did not run: a docs change is green while the code area would be red
    git(c, "checkout", "-q", "-B", "docs", "origin/main");
    commitDocs(c, "fine.md");
    const docs = spawnSync(join(c, ".spire/tool/land"), [], {
      cwd: c,
      env: { ...env, CODE_RED: "1" },
      encoding: "utf8",
    });
    assert.equal(docs.status, 0, docs.stdout + docs.stderr);
    assert.match(
      git(w.bare, "log", "-1", "--format=%B", "main"),
      /^Land-Checks: areas=docs /m,
    );
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

// Admission: every temp directory `land` makes is removed when the run ends. It would fail if a run left a file or directory of its own in TMPDIR.
// Oracle: a sandbox TMPDIR before and after two real `land --dry-run` runs (the second after a killed run's stale directory is planted), the real lander.
test("land: --dry-run twice leaves nothing in TMPDIR, and a killed run's stale directory is removed at the next start", () => {
  const w = world();
  const c = w.clone("c");
  const sandbox = mkdtempSync(join(tmpdir(), "land-tmp-sandbox-"));
  const dry = () =>
    spawnSync(
      join(c, ".spire/tool/land"),
      ["--dry-run"],
      {
        cwd: c,
        env: { ...env, TMPDIR: sandbox },
        encoding: "utf8",
      },
    );
  try {
    commitDocs(c, "one.md");
    const first = dry();
    assert.equal(first.status, 0, first.stdout + first.stderr);
    assert.deepEqual(readdirSync(sandbox), []);
    // a run killed long ago: its directory, its owner dead, older than the TTL
    const stale = join(sandbox, "land-run.stale1");
    mkdirSync(stale);
    writeFileSync(join(stale, ".owner"), "2147483646\n");
    const old = new Date(Date.now() - 6 * 3600e3);
    utimesSync(stale, old, old);
    const second = dry();
    assert.equal(second.status, 0, second.stdout + second.stderr);
    assert.deepEqual(readdirSync(sandbox), []);
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
    rmSync(sandbox, { recursive: true, force: true });
  }
});

// Admission: the lander MEASURES (the captain's ruling, 17 Nov 2026): every exit of `land` and of `push` appends one line to <git common dir>/spire-land/metrics.jsonl, so "time from Go to landed" and
// "time to the first failure" are read, not modelled. It would fail if a run that ends without landing (a red check, a held path) left no line, if the line lost the commission or the first red step and its
// second, the council's advice, or if a push that ran the gate again after main moved did not say so, and what each attempt cost; or if a landed run did not write its sha. Oracle: the real `land` and `push`
// in real git worlds; the commission is the one LAND_COMMISSION of the environment; the council is a stub that answers with advice.
const metricsOf = (c) =>
  readFileSync(join(git(c, "rev-parse", "--path-format=absolute", "--git-common-dir").trim(), "spire-land/metrics.jsonl"), "utf8")
    .split("\n")
    .filter(Boolean)
    .map((l) => JSON.parse(l));

test("land metrics: a red check (13) with the council's advice, and a held path (22), each write a line: commission, first red step and its second, advice", () => {
  const w = world({ council: true });
  const c = w.clone("c");
  try {
    mkdirSync(join(c, "server/src/authority"), { recursive: true });
    writeFileSync(join(c, "server/src/authority/x.ts"), "export const a = 1;\n");
    git(c, "add", "-A");
    git(c, "commit", "-qm", "server: a change");
    writeFileSync(
      join(c, ".spire/tool/council"),
      `#!/usr/bin/env bash\necho '{"result":"advice","exit":0,"members":[{"role":"warden","verdict":"advice","findings":[{"severity":"fix","principle":1},{"severity":"note","principle":null}]}],"trailer":"ran: warden=advice(fix P1)"}'\n`,
    );
    chmodSync(join(c, ".spire/tool/council"), 0o755);
    git(c, "add", "-A");
    git(c, "commit", "-qm", "a council that gives advice");
    const red = spawnSync(join(c, ".spire/tool/land"), [], { cwd: c, env: { ...env, CODE_RED: "1", LAND_COMMISSION: "c-42" }, encoding: "utf8" });
    assert.equal(red.status, 13, red.stdout + red.stderr);
    const [line] = metricsOf(c);
    assert.equal(metricsOf(c).length, 1);
    assert.equal(line.tool, "land");
    assert.equal(line.exit, 13);
    assert.equal(line.result, "checks-failed");
    assert.equal(line.commission, "c-42");
    assert.equal(line.branch, git(c, "symbolic-ref", "--short", "HEAD").trim());
    assert.equal(line.subject, "server: a change");
    assert.equal(line.tree, git(c, "rev-parse", "--short=12", "HEAD^{tree}").trim());
    assert.match(line.run, /^\d+-\d+$/);
    assert.ok(line.wall_s >= 0 && line.ts >= line.start);
    assert.equal(line.checks.first_red.step, "code-check", JSON.stringify(line));
    assert.ok(Number.isInteger(line.checks.first_red.s) && line.checks.first_red.s <= line.wall_s);
    assert.deepEqual(line.checks.steps.find((s) => s.id === "code-check").state, "red");
    assert.equal(line.landed, null);
    // the council may have been stopped by the red check or have answered first: when it ended it said its two findings
    if (line.council.end_s !== null) assert.equal(line.council.findings, 2);
    assert.equal(git(w.bare, "rev-list", "--count", "main").trim(), "1", "nothing landed");

    // a held path: stops before anything starts, and says so
    const p = join(c, ".spire/land.json");
    const cfg = JSON.parse(readFileSync(p, "utf8"));
    cfg.held = [{ paths: ["docs/frozen/**"], reason: "a person lands the law" }];
    writeFileSync(p, JSON.stringify(cfg));
    git(c, "commit", "-qam", "hold docs/frozen");
    git(c, "push", "-q", "origin", "HEAD:main");
    git(c, "checkout", "-q", "-B", "held", "origin/main");
    mkdirSync(join(c, "docs/frozen"), { recursive: true });
    writeFileSync(join(c, "docs/frozen/law.md"), "law\n");
    git(c, "add", "-A");
    git(c, "commit", "-qm", "docs: a held file");
    const held = land(c);
    assert.equal(held.status, 22, held.stdout + held.stderr);
    const last = metricsOf(c).at(-1);
    assert.equal(last.exit, 22);
    assert.equal(last.result, "held-for-a-person");
    assert.equal(last.commission, null);
    assert.equal(last.checks, null);
    assert.equal(metricsOf(c).length, 2);
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

test("land metrics: a push that ran the gate again after main moved says so, with each attempt's seconds; the landed run writes its sha and the second it landed", () => {
  const w = world();
  const c = w.clone("c");
  const other = w.clone("other");
  try {
    // the checks are a stub that, the first time the push's gate runs it (in the lander's own scratch worktree), lands a commit from another clone: main moves under the gate
    const moved = join(w.dir, "moved");
    writeFileSync(
      join(c, ".spire/tool/checks.sh"),
      `#!/usr/bin/env bash\ncase "$PWD" in */spire-land/run.*) if [ ! -e ${moved} ]; then touch ${moved}; git -C ${other} pull -q --rebase origin main && echo moved > ${other}/${DOCS}/moved.md && git -C ${other} add -A && git -C ${other} commit -qm "docs: main moves" && git -C ${other} push -q origin HEAD:main; fi;; esac\necho "checks=stub" | tee "$LAND_CHECKS_OUT"\n`,
    );
    chmodSync(join(c, ".spire/tool/checks.sh"), 0o755);
    git(c, "add", "-A");
    git(c, "commit", "-qm", "a stub gate");
    commitDocs(c, "one.md");
    const r = spawnSync(join(c, ".spire/tool/land"), [], { cwd: c, env: { ...env, LAND_COMMISSION: "c-7" }, encoding: "utf8" });
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /moved during the gate/);
    const lines = metricsOf(c);
    const lk = lines.find((l) => l.tool === "land");
    const pk = lines.find((l) => l.tool === "push");
    assert.equal(lines.length, 2, "one line per exit of land and of push");
    assert.equal(lk.exit, 0);
    assert.equal(lk.result, "landed");
    assert.equal(lk.commission, "c-7");
    assert.equal(lk.landed.sha, git(w.bare, "rev-parse", "main").trim());
    assert.equal(pk.land_run, lk.run, "the push line joins its land");
    assert.equal(pk.result, "landed");
    assert.equal(pk.attempts, 2);
    assert.equal(pk.walls.length, 2);
    assert.ok(pk.walls.every(Number.isInteger));
    assert.ok(pk.landed_at >= lk.start && pk.landed_at <= lk.ts);
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

test("push metrics: a gate that is red on the rebased tree writes its line with the first red step and the seconds of the attempt", () => {
  const w = world({ reading: true });
  const a = w.clone("a");
  const b = w.clone("b");
  try {
    const { NODE_TEST_CONTEXT: _context, ...own } = env;
    const x = { ...own, RUNS: join(w.dir, "runs") };
    mkdirSync(join(b, "app"), { recursive: true });
    writeFileSync(join(b, "app/x.txt"), "x\n");
    git(b, "add", "-A");
    git(b, "commit", "-qm", "app: a change");
    assert.equal(spawnSync(join(b, ".spire/tool/land"), ["--dry-run"], { cwd: b, env: x, encoding: "utf8" }).status, 0);
    mkdirSync(join(a, "server"), { recursive: true });
    writeFileSync(join(a, "server/bad.txt"), "red\n"); // pushed by a person, past the gate: the next gate finds it
    git(a, "add", "-A");
    git(a, "commit", "-qm", "server: a file that makes a step red");
    git(a, "push", "-q", "origin", "HEAD:main");
    const red = spawnSync(join(b, ".spire/tool/push"), [], { cwd: b, env: { ...x, LAND_RUN: "1-2" }, encoding: "utf8" });
    assert.equal(red.status, 13, red.stdout + red.stderr);
    const line = metricsOf(b).filter((l) => l.tool === "push").at(-1); // the dry-run land of the change before it wrote its own
    assert.equal(line.exit, 13);
    assert.equal(line.result, "gate");
    assert.equal(line.land_run, "1-2");
    assert.equal(line.attempts, 1);
    assert.equal(line.walls.length, 1);
    assert.equal(line.first_red.step, "server-check", JSON.stringify(line));
    assert.ok(Number.isInteger(line.first_red.s) && line.first_red.s >= 0 && line.first_red.s <= line.total_s, "seconds since the push's start, as on a land line");
    assert.equal(line.landed_at, null);
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

// Admission (F1): a landing that raced a newer main is gated again by `push` on the rebased tree, and the commit that lands carries THAT gate's line: its `tree=` is the tree on main, not the tree `land`
// checked before the rebase. The stamp is a message-only amend: every tree of the push is the one the gate saw. It would fail if a stale `tree=` reached main, or if a commit's tree changed by the stamp.
// Oracle: real git and the real `land` and `push`; the gate is a stub that prints the real line format (tree of the tree it runs on) and, the first time it runs in the lander's scratch worktree, lands a commit from another clone.
test("push: main moved during the gate: every commit that lands carries the line of the gate that ran on the final tree", () => {
  const w = world();
  const c = w.clone("c");
  const other = w.clone("other");
  try {
    const moved = join(w.dir, "moved");
    writeFileSync(
      join(c, ".spire/tool/checks.sh"),
      `#!/usr/bin/env bash\ncase "$PWD" in */spire-land/run.*) if [ ! -e ${moved} ]; then touch ${moved}; git -C ${other} pull -q --rebase origin main && echo moved > ${other}/${DOCS}/moved.md && git -C ${other} add -A && git -C ${other} commit -qm "docs: main moves" && git -C ${other} push -q origin HEAD:main; fi;; esac\necho "areas=stub tests=0 files=0 checks=stub:0s total=0s tree=$(git rev-parse --short=12 HEAD^{tree})" | tee "$LAND_CHECKS_OUT"\n`,
    );
    chmodSync(join(c, ".spire/tool/checks.sh"), 0o755);
    git(c, "add", "-A");
    git(c, "commit", "-qm", "a stub gate");
    commitDocs(c, "one.md");
    const checkedByLand = git(c, "rev-parse", "--short=12", "HEAD^{tree}").trim();
    const r = land(c);
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /moved during the gate/);
    const landed = git(w.bare, "rev-parse", "--short=12", "main^{tree}").trim();
    assert.notEqual(landed, checkedByLand, "main moved: the landed tree is not the one land checked");
    const bodies = git(w.bare, "log", "-2", "--format=%B%x1e", "main").split("\x1e").map((b) => b.trim()).filter(Boolean);
    assert.equal(bodies.length, 2);
    assert.match(bodies[0], new RegExp(`^Land-Checks: .* tree=${landed}$`, "m"), bodies[0]);
    assert.match(bodies[1], new RegExp(`^Land-Checks: .* tree=${landed} part-of=${landed}$`, "m"), bodies[1]); // F2: the earlier commit of the push says it is part of the tip's tree
    for (const b of bodies) {
      assert.doesNotMatch(b, new RegExp(`tree=${checkedByLand}`));
      assert.match(b, /^Land-Council: none needed/m, "the council's record is kept by the stamp");
    }
    // the stamp changed the message only: the trees of the two commits are the trees the gate saw (the tip's is the line's)
    assert.equal(git(w.bare, "rev-parse", "--short=12", "main^{tree}").trim(), landed);
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

// Admission (F3): `land --override "reason"` is the owner's route for a path the project holds for a person. It bypasses ONLY the held-path stop (22): the checks and the council run as for any landing, a red check still
// refuses (13), and every commit that lands carries the reason in an `Override:` trailer beside Land-Checks and Land-Council. Nothing else is skipped; an override on a change that holds nothing is said to be ignored and writes no
// trailer; a hand-typed Override is still stripped; a reason is required. It would fail if the held stop were still final, if a red check landed under an override, if the trailer were missing from any commit or written when
// nothing was held. Oracle: the real `land` and `push` on a world whose file holds server/frozen/ (the code area, so a check can be red).
test("land --override: the held stop is bypassed and nothing else: checks run, a red one refuses, the reason is an Override trailer on every commit; no held path: ignored", () => {
  const w = world();
  const c = w.clone("c");
  try {
    const p = join(c, ".spire/land.json");
    const cfg = JSON.parse(readFileSync(p, "utf8"));
    cfg.held = [{ paths: ["server/frozen/**"], reason: "the owner lands the frozen files" }];
    writeFileSync(p, JSON.stringify(cfg));
    git(c, "commit", "-qam", "the project holds server/frozen for its owner");
    git(c, "push", "-q", "origin", "HEAD:main");
    const held = (name) => {
      mkdirSync(join(c, "server/frozen"), { recursive: true });
      writeFileSync(join(c, "server/frozen", name), `${name}\n`);
      git(c, "add", "-A");
      git(c, "commit", "-qm", `server: ${name}`);
    };
    const run = (extra, ...a) => spawnSync(join(c, ".spire/tool/land"), a, { cwd: c, env: { ...env, ...extra }, encoding: "utf8" });
    const count = () => git(w.bare, "rev-list", "--count", "main").trim();
    held("one.txt");
    held("two.txt");
    // without the flag: still the final answer
    assert.equal(run({}).status, 22);
    // a reason is required
    assert.equal(run({}, "--override").status, 2);
    assert.equal(run({}, "--override", "").status, 2);
    // under an override a RED check still refuses: the checks ran
    const red = run({ CODE_RED: "1" }, "--override", "the owner: law fix");
    assert.equal(red.status, 13, red.stdout + red.stderr);
    assert.match(red.stdout, /held for a person: the owner lands the frozen files/);
    assert.match(red.stdout, /--override: landing it anyway/);
    assert.equal(count(), "2", "nothing landed");
    // green: lands, every commit of the push carries the reason beside the record
    const r = run({}, "--override", "the owner: law fix");
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /checks ok/);
    const bodies = git(w.bare, "log", "-2", "--format=%B%x1e", "main").split("\x1e").map((b) => b.trim()).filter(Boolean);
    assert.equal(bodies.length, 2);
    for (const b of bodies) {
      assert.match(b, /^Override: the owner: law fix$/m);
      assert.match(b, /^Land-Checks: areas=code /m);
      assert.match(b, /^Land-Council: /m);
    }
    assert.equal(count(), "4");
    // a change that holds nothing: the flag is ignored, said so, and no Override is written
    git(c, "fetch", "-q");
    git(c, "checkout", "-q", "-B", "free", "origin/main");
    commitDocs(c, "free.md");
    git(c, "commit", "-q", "--amend", "-m", "docs: free.md", "-m", "Override: typed by hand");
    const free = run({}, "--override", "not needed");
    assert.equal(free.status, 0, free.stdout + free.stderr);
    assert.match(free.stdout, /--override: nothing is held; ignored/);
    assert.doesNotMatch(git(w.bare, "log", "-1", "--format=%B", "main"), /^Override:/m);
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

// Admission: with the fleet's GitHub App on (FLEET_APP_ID set), `land` pushes main (and, under LAND_VIA_PR=1, the branch) by the App's installation token and by nothing else; an app that cannot make a token stops
// the land at once ("app not configured", exit 1) before any check and pushes nothing; the key is in no output and in no check's environment. It would fail if land or the lander pushed with the host's own identity, went
// on without a token, or handed the key to a child. Oracle: the real `land` and lander, a bare remote served over http by a stub of GitHub (fixtures/app-stub.mjs) that accepts a push only with the token's header. The key is generated here.
test("land: the App's token pushes main and the branch; an app that is on and cannot be used stops land at once, nothing pushed, no secret printed", async () => {
  const w = world();
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const pem = privateKey.export({ type: "pkcs8", format: "pem" });
  const token = "stub-land-installation-token-0123456789";
  const basic = `Basic ${Buffer.from(`x-access-token:${token}`).toString("base64")}`;
  git(w.bare, "config", "http.receivepack", "true");
  const stub = await startStub({ root: dirname(w.dir), publicKey: publicKey.export({ type: "spki", format: "pem" }), token, repos: [`${w.dir.split("/").at(-1)}/remote`] });
  // the stub is in this process: a child that talks to it is started asynchronously
  const go = (cwd, extra) => new Promise((ok) => {
    const c = spawn(join(cwd, ".spire/tool/land"), [], { cwd, env: { ...env, GITHUB_API_URL: stub.url, GITHUB_SERVER_URL: stub.url, FLEET_APP_ID: "5803159", GIT_TRACE: "1", GIT_CURL_VERBOSE: "1", ...extra } });
    let out = "";
    c.stdout.on("data", (d) => { out += d; });
    c.stderr.on("data", (d) => { out += d; });
    c.on("close", (status) => ok({ status, out }));
  });
  const secrets = (t) => [...pem.split("\n").filter((l) => l.length > 8), token, basic.slice(6)].filter((x) => t.includes(x));
  const c = w.clone("c");
  try {
    const before = git(w.bare, "rev-parse", "main").trim();
    commitDocs(c, "app.md");
    // no key: stops before the checks, nothing pushed, nothing tried
    const none = await go(c, { FLEET_APP_PRIVATE_KEY: "" });
    assert.equal(none.status, 1, none.out);
    assert.match(none.out, /app not configured: FLEET_APP_PRIVATE_KEY is missing or empty/);
    assert.doesNotMatch(none.out, /checks ok/);
    assert.equal(git(w.bare, "rev-parse", "main").trim(), before);
    assert.deepEqual(stub.calls.git.filter((g) => g.push), []);
    // the key: lands, by the token
    const ok = await go(c, { FLEET_APP_PRIVATE_KEY: pem });
    assert.equal(ok.status, 0, ok.out);
    assert.match(ok.out, /land: landed /);
    assert.equal(git(w.bare, "rev-list", "--count", "main").trim(), "2");
    const pushes = stub.calls.git.filter((g) => g.push);
    assert.ok(pushes.length > 0);
    assert.deepEqual([...new Set(pushes.map((g) => g.auth))], [basic]);
    assert.deepEqual(secrets(ok.out + none.out), []);
    // LAND_VIA_PR=1: the branch is pushed by the token too
    git(c, "fetch", "-q");
    git(c, "checkout", "-q", "-B", "viapr", "origin/main");
    commitDocs(c, "viapr.md");
    const n0 = stub.calls.git.filter((g) => g.push).length;
    const pr = await go(c, { FLEET_APP_PRIVATE_KEY: pem, LAND_VIA_PR: "1" });
    assert.equal(pr.status, 20, pr.out);
    assert.match(git(w.bare, "branch", "--list", "viapr"), /viapr/);
    assert.ok(stub.calls.git.filter((g) => g.push).length > n0);
    assert.deepEqual([...new Set(stub.calls.git.filter((g) => g.push).map((g) => g.auth))], [basic]);
    assert.deepEqual(secrets(pr.out), []);
  } finally {
    await stub.close();
    rmSync(w.dir, { recursive: true, force: true });
  }
});
