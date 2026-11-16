// Admission: this file shows that `land` (.spire/tool/land) classifies a change from its real paths, runs the areas it touches (its world has a small project file of its own: areas.test.mjs
// and checks.test.mjs hold the areas' own proofs), refuses a landing with a failing area (13) and has no flag that lands past one, lands a docs change
// on a bare remote as a plain fast-forward with its Land-* attestation on the commit,
// rebases a second clone that finds main moved and lands it, refuses without a PR when the remote refuses main, and opens the branch path (exit 20) only under LAND_VIA_PR=1. It would
// fail if the classifier, the lander wiring, the attestation or the fallback broke. Oracle: real git repositories in a temp dir and the real vendored lander;
// nothing here is mocked (a PR is not opened: there is no GitHub; that step is run live and quoted in the PR).
import assert from "node:assert/strict";
import { execFile, execFileSync, spawnSync } from "node:child_process";
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
function world({ council = false, tests = false } = {}) {
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
  delete cfg.checks.install; // this world has no dependencies to install
  // `council`: false: the project removes the whole council (a file of each default's name with `calls: []`), so a landing here needs no model; true: it keeps the Warden alone (the tests of
  // the council ask for it: world({ council: true })); "full": the default council as shipped
  if (council !== "full") {
    if (!council) cfg.sensitive = { globs: [], nameGlobs: [], always: [] };
    mkdirSync(join(seed, ".spire/review"), { recursive: true });
    for (const role of ["inspector", "steward", "architect", "designer", ...(council ? [] : ["warden"])]) writeFileSync(join(seed, ".spire/review", `${role}.md`), `---\nrole: ${role}\ncalls: []\n---\nremoved by this project\n`);
  }
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
// (exit 2), an `Override:` trailer a commit message carries is stripped, never kept. It would fail if the attestation went on the tip only, or if an override still passed. Oracle: real git, the real
// lander and `land`, a docs change (no council, no key).
test("land: every commit of a landing carries the evidence; there is no --override and an Override trailer is never kept", () => {
  const w = world();
  const c = w.clone("c");
  try {
    commitDocs(c, "one.md");
    commitDocs(c, "two.md");
    for (const flag of [["--override", "hotfix"], ["--council-skip", "down"], ["--ui-skip", "no browser"]]) {
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
    assert.deepEqual(ran(), ["tests:code"], "the lander's gate carried the pass over the rebase");

    commitDocs(a, "two.md");
    assert.equal(land(a).status, 0);
    const r = spawnSync(join(b, ".spire/tool/land"), [], { cwd: b, env: x, encoding: "utf8" });
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /rebased onto origin\/main/);
    assert.deepEqual(ran(), ["tests:code"], "land carried it too");
    const msg = git(w.bare, "log", "-1", "--format=%B", "main");
    assert.match(msg, new RegExp(`^Land-Checks: .*checks=.*code:carried from ${t0}.* total=\\ds tree=${git(w.bare, "rev-parse", "main^{tree}").trim().slice(0, 12)}`, "m"));
    assert.match(msg, /code-check:\ds/); // the cheap step ran on the tree that landed
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

test("land: a member that cannot answer stops the landing at 15 before anything is pushed (no key), and nothing lands it", () => {
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
    const r = run();
    assert.equal(r.status, 15, r.stdout + r.stderr);
    assert.match(r.stdout, /council did not pass \(exit 15\)/);
    assert.doesNotMatch(r.stdout, /checks ok/, "nothing is attested while a member could not answer");
    assert.equal(git(w.bare, "rev-list", "--count", "main").trim(), "1"); // nothing landed
    const s = run("--council-skip", "the council is down"); // not a flag any more
    assert.equal(s.status, 2, s.stdout + s.stderr);
    assert.equal(run().status, 15);
    assert.equal(git(w.bare, "rev-list", "--count", "main").trim(), "1");
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

// The council and the checks run at the same time and the first red result stops the other (measured: 90 of 242 landings in a day ended at the council after 54523 s of checks on a tree its fix then
// changed; a red check made the council wait for nothing). It would fail if the checks ran to their end after a finding, if the council were waited for after a red check, or if they ran one after the other.
// Oracle: the real `land` with a check that records its start and would finish after 40 s, and a council that would answer after 60 s; the clock bounds each case.
test("land: the council and the checks run together; a finding stops the checks at once, a red check stops the council at once", () => {
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
    // a finding (the council answers 18 at once): the checks, which started with it, are killed and never finish
    stub("checks.sh", `touch ${marks}/c-start; sleep 40; touch ${marks}/c-end; echo checks=slow | tee "$LAND_CHECKS_OUT"`);
    stub("council", `touch ${marks}/k-start; echo "council: warden BLOCK"; echo '{"trailer":"x"}'; exit 18`);
    let { r, took } = land();
    assert.equal(r.status, 18, r.stdout + r.stderr);
    assert.ok(existsSync(join(marks, "c-start")), "the checks started with the council");
    assert.ok(took < 30, `a finding stops the checks at once (${took} s)`);
    assert.match(r.stdout, /a council finding stops the checks/);
    spawnSync("sleep", ["1"]);
    // a red check: the council, which would answer after 60 s, is stopped and not waited for
    rmSync(join(marks, "c-start"));
    stub("checks.sh", `touch ${marks}/c-start; echo "land: check FAILED: x" >&2; exit 13`);
    stub("council", `touch ${marks}/k-start; sleep 60; touch ${marks}/k-end; echo '{"trailer":"x"}'`);
    ({ r, took } = land());
    assert.equal(r.status, 13, r.stdout + r.stderr);
    assert.ok(took < 30, `a red check stops the council at once (${took} s)`);
    assert.match(r.stdout, /a failed check stops the council/);
    assert.ok(!existsSync(join(marks, "k-end")), "the council did not finish");
    assert.equal(git(w.bare, "rev-list", "--count", "main").trim(), "1");
    rmSync(marks, { recursive: true, force: true });
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

// Admission: this test shows that `land` never turns an unresolved council finding into a landing: a seeded duplicate that a member rates `fix` stops it (18, main not moved, the member
// and the finding printed) however often land is run again (no count of rounds lets it through), and lands only when the change fixes it and the member re-reviews it clean;
// the Land-Council trailer then records the finding and how it was resolved. It would fail if a round count landed the change with a note (the old behaviour: "capped, lands"). Oracle: real git, the real lander, the real `land` and `council` processes, a real HTTP
// gateway that answers from what the member was given; the check step is the one thing replaced (this world has no npm install).
test("land: a finding rated fix stops the landing however often it is run (18) and lands only once fixed and re-reviewed clean", async () => {
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
    const run = () =>
      new Promise((resolve) =>
        execFile(
          join(c, ".spire/tool/land"),
          [],
          { cwd: c, env: viaGateway, encoding: "utf8" },
          (e, stdout, stderr) =>
            resolve({ status: e ? e.code : 0, stdout, stderr }),
        ),
      );
    const exits = [];
    for (let i = 0; i < 4; i++) {
      const r = await run();
      exits.push(r.status);
      assert.match(
        r.stdout,
        /warden: FIX P1 server\/src\/authority\/x\.ts:41/,
      );
      assert.equal(mainCount(w), "1"); // main did not move
      assert.doesNotMatch(r.stdout, /checks ok/, "a finding to close attests nothing");
    }
    assert.deepEqual(exits, [18, 18, 18, 18]);
    const again = await run();
    assert.equal(again.status, 18);
    assert.equal(lastJson(again).result, "council-stopped");
    put(false); // the fix
    git(c, "commit", "-qam", "repair the duplicate");
    const ok = await run();
    assert.equal(ok.status, 0, ok.stdout + ok.stderr);
    assert.equal(mainCount(w), "3");
    const body = git(w.bare, "log", "-1", "--format=%B", "main");
    assert.match(
      body,
      /^Land-Council: ran: warden=approve \(models fast, round \d+, diff [0-9a-f]+\)$/m,
    );
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
// answer is exit 15, so "the council was asked" is observable); the check step is the one thing replaced.
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

    // end to end: with no key every change stops at 15 (the Warden was asked), a sensitive path, plain code and a document alike
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
      ["server/src/identity/x.ts", 15],
      ["server/src/foo.ts", 15],
      ["docs/note.md", 15],
    ]) {
      git(c, "fetch", "-q");
      git(c, "checkout", "-q", "-B", `b-${file.length}-${file.replace(/\W/g, "")}`, "origin/main");
      mkdirSync(join(c, file, ".."), { recursive: true });
      writeFileSync(join(c, file), "a\nb\nc\nd\ne\n");
      git(c, "add", "-A");
      git(c, "commit", "-qm", `change ${file}`);
      const r = run();
      assert.equal(r.status, status, r.stdout + r.stderr);
      assert.match(r.stdout, /no COUNCIL_MODEL_URL/); // the Warden was asked, and no member could answer
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
