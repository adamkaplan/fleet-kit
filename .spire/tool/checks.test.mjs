// Admission: these tests show how checks.sh runs a change's tests by AREA (areas.mjs, classify.mjs, plan.mjs, run-steps.mjs): EVERY test of each area a change touches, nothing narrower and
// nothing else; a path in two areas runs both; a path in no area runs the full suite; a tree that passed is not run again, but a main that moved after the checks is a NEW tree and the areas run
// again on it (a test group is carried from the older main only where carry.mjs allows it: see the second-rebase test); a failed test file is red (exit 13): nothing re-runs it. They would fail if a test were
// narrowed away, if an untouched area ran, if an unmapped path were skipped, if a rebase over a moved main reused an older result, or if a real failure were forgiven.
// Oracle: the real checks.sh, classify, plan and run-steps and the real node test runner on a small real repository with a project of its own (an iOS app: nothing of Spire is in it); no mocks.
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import {
  chmodSync,
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { availableParallelism, tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { globToRegExp, matcher } from "./config.mjs";

const repo = join(fileURLToPath(import.meta.url), "../..");
// a nested `node --test` must start as a runner of its own, not as a child of this one
const { NODE_TEST_CONTEXT: _context, ...inheritedAll } = process.env;
const inherited = Object.fromEntries(
  Object.entries(inheritedAll).filter(([k]) => k !== "NODE_OPTIONS"),
);
const env = {
  ...inherited,
  GIT_AUTHOR_NAME: "t",
  GIT_AUTHOR_EMAIL: "t@t",
  GIT_COMMITTER_NAME: "t",
  GIT_COMMITTER_EMAIL: "t@t",
};
const git = (cwd, ...a) =>
  execFileSync("git", a, { cwd, env, encoding: "utf8" });

test("globs mean what the file says", () => {
  const m = (g, p, nocase) => matcher([g], nocase)(p);
  assert.equal(m("app/**", "app/src/a.tsx"), true);
  assert.equal(m("app/**", "apple/a.tsx"), false);
  assert.equal(m("**/tests/**", "tests/a.ts"), true);
  assert.equal(m("**/tests/**", "server/tests/x/a.ts"), true);
  assert.equal(m("**/tests/**", "server/mytests/a.ts"), false);
  assert.equal(m(".spire/**", ".spire/tool/a.mjs"), true);
  assert.equal(m(".spire/*", ".spire/tool/a.mjs"), false);
  assert.equal(m(".spire/*", ".spire/land.json"), true);
  assert.equal(m("**/*.test.{ts,mjs}", "a/b/c.test.mjs"), true);
  assert.equal(m("**/*.test.{ts,mjs}", "a/b/c.test.js"), false);
  assert.equal(m("*.md", "README.md"), true);
  assert.equal(m("*.md", "docs/README.md"), false);
  assert.equal(m("**/*auth*.ts", "server/src/Author-map.ts", true), true);
  assert.equal(m("**/*auth*.ts", "server/src/Author-map.ts", false), false);
  assert.equal(globToRegExp("a?c").test("abc"), true);
});

const node = (js) => ["node", "-e", js];
// every run of a step appends its id to $RUNS (a file outside the tree), so a test can count how often a step ran
const mark = (id, code = 0) =>
  node(
    `require('fs').appendFileSync(process.env.RUNS, '${id}\\n'); process.exit(${code})`,
  );
const nodeTest = ["node", "--experimental-strip-types", "--test"];

/** A foreign project's world: the mechanism's scripts, its own land.json (areas: ios, android, docs), one commit on main and a change on `work` in ios/. */
// a test file per area that main already has and the change does not touch: it carries over a rebase, while the change's own test files re-run (carry.mjs)
function quiet(dir) {
  const at = git(dir, "rev-parse", "--abbrev-ref", "HEAD").trim();
  git(dir, "checkout", "-q", "main");
  for (const a of ["ios", "android"]) {
    mkdirSync(join(dir, a), { recursive: true });
    writeFileSync(join(dir, a, "zTests.mjs"), 'import test from "node:test";\ntest("z", () => {});\n');
  }
  git(dir, "add", "-A");
  git(dir, "commit", "-qm", "main has an untouched test file per area");
  git(dir, "checkout", "-q", at);
  git(dir, "rebase", "-q", "main");
}

// the harness's own variables (the runs file, the red switch) reach every step of these projects by the step's own `pass`, as a project's step names what it reads from the host (step-env.mjs)
const HARNESS = ["RUNS", "RED"];
const withHarness = (config) => {
  for (const a of config.areas) for (const s of a.steps) s.pass ??= HARNESS;
  return config;
};

function foreign(over = {}) {
  const dir = mkdtempSync(join(tmpdir(), "areas-test-"));
  mkdirSync(join(dir, ".spire"), { recursive: true });
  cpSync(join(repo, "tool"), join(dir, ".spire/tool"), {
    recursive: true,
    filter: (src) => !src.endsWith(".test.mjs"),
  });
  const config = {
    version: 1,
    paths: {
      tests: ["**/*Tests/**"],
      prose: ["**/*.md"],
      notProd: ["**/*.md"],
    },
    sensitive: { globs: [], nameGlobs: [], always: [] },
    areas: [
      {
        name: "ios",
        paths: ["ios/**"],
        steps: [
          {
            id: "swiftlint",
            dir: ".",
            whenChanged: "ios/**/*.swift",
            cmd: mark("swiftlint"),
          },
          {
            id: "unit",
            dir: ".",
            cmd: node(
              "require('fs').appendFileSync(process.env.RUNS, 'unit\\n'); process.exit(Number(process.env.RED || 0))",
            ),
          },
        ],
      },
      {
        name: "android",
        paths: ["android/**"],
        steps: [{ id: "gradle", dir: ".", cmd: mark("gradle") }],
      },
      {
        name: "docs",
        paths: ["**/*.md"],
        steps: [{ id: "doc-check", dir: ".", cmd: mark("doc-check") }],
      },
    ],
    checks: {
      testConcurrencyFlag: "--test-concurrency={n}",
      testReporterFlag: "--test-reporter=spec",
    },
    environments: {},
    ...over,
  };
  writeFileSync(join(dir, ".spire/land.json"), JSON.stringify(withHarness(config)));
  mkdirSync(join(dir, ".spire/review"));
  writeFileSync(join(dir, ".spire/review/reviewer.md"), '---\nrole: reviewer\ncalls: ["ios/**"]\n---\nLook at the iOS code.\n'); // the project's own viewpoint: the paths that call it are its own
  for (const role of ["inspector", "steward", "architect", "designer", "warden"]) writeFileSync(join(dir, ".spire/review", `${role}.md`), `---\nrole: ${role}\ncalls: []\n---\nremoved by this project\n`); // the project removes the default council
  mkdirSync(join(dir, "ios"));
  mkdirSync(join(dir, "android"));
  mkdirSync(join(dir, "docs"));
  writeFileSync(join(dir, "ios/App.swift"), "let a = 1\n");
  writeFileSync(join(dir, "android/App.kt"), "val a = 1\n");
  writeFileSync(join(dir, "docs/a.md"), "a\n");
  git(dir, "init", "-q", "-b", "main");
  git(dir, "add", "-A");
  git(dir, "commit", "-qm", "seed");
  git(dir, "checkout", "-q", "-b", "work");
  writeFileSync(join(dir, "ios/App.swift"), "let a = 2\nlet b = 3\n");
  git(dir, "commit", "-qam", "ios: a change");
  return dir;
}
const runsOf = (dir) =>
  existsSync(join(dir, "..", `${dir.split("/").pop()}.runs`))
    ? readFileSync(join(dir, "..", `${dir.split("/").pop()}.runs`), "utf8")
        .split("\n")
        .filter(Boolean)
    : [];
const checksIn = (dir, extra = {}) =>
  spawnSync(join(dir, ".spire/tool/checks.sh"), ["main"], {
    cwd: dir,
    env: {
      ...env,
      RUNS: join(dir, "..", `${dir.split("/").pop()}.runs`),
      ...extra,
    },
    encoding: "utf8",
  });
const cleanup = (dir) => {
  rmSync(dir, { recursive: true, force: true });
  rmSync(join(dir, "..", `${dir.split("/").pop()}.runs`), { force: true });
  rmSync(join(dir, "..", `${dir.split("/").pop()}.runs.flaky`), { force: true });
};
// the project's file moves on MAIN (the change under test is the work branch's own), so that editing it is never part of the diff the checks classify
const setConfig = (dir, edit, message = "the project's file") => {
  const at = git(dir, "rev-parse", "--abbrev-ref", "HEAD").trim();
  git(dir, "checkout", "-q", "main");
  const p = join(dir, ".spire/land.json");
  const cfg = JSON.parse(readFileSync(p, "utf8"));
  edit(cfg);
  writeFileSync(p, JSON.stringify(withHarness(cfg)));
  git(dir, "commit", "-qam", message);
  git(dir, "checkout", "-q", at);
  git(dir, "rebase", "-q", "main");
};
const classifyIn = (dir) =>
  JSON.parse(
    execFileSync(
      "node",
      [join(dir, ".spire/tool/classify.mjs"), "main", "HEAD"],
      { cwd: dir, env, encoding: "utf8" },
    ),
  );

test("a project that is not Spire is classified and checked from its own file: only the area it touches runs, and the line says which and how many tests", () => {
  const dir = foreign();
  try {
    const info = classifyIn(dir);
    assert.deepEqual(info.council, ["reviewer"]);
    assert.deepEqual(info.areas.names, ["ios"]);
    const green = checksIn(dir);
    assert.equal(green.status, 0, green.stdout + green.stderr);
    assert.match(
      green.stdout,
      /^areas=ios tests=0 files=0 checks=swiftlint:\ds,unit:\ds total=\ds tree=[0-9a-f]{12}$/m,
    );
    assert.deepEqual(runsOf(dir).sort(), ["swiftlint", "unit"]); // android and docs did not run
    writeFileSync(join(dir, "ios/App.swift"), "let a = 3\nlet b = 3\n");
    git(dir, "commit", "-qam", "ios: another");
    const red = checksIn(dir, { RED: "1" });
    assert.equal(red.status, 13, red.stdout + red.stderr);
    assert.match(red.stderr, /check FAILED: unit/);
  } finally {
    cleanup(dir);
  }
});

// A5, the land loop: every step of every touched area runs to its end and ONE report names every failed step and test file; the failures are kept; `--retest` runs only those (and their steps'
// prerequisites) until green, attests nothing; then a plain run does the whole touched set once on the final tree. They would fail if the first red step stopped the others, if the report named only
// the first failure, or if --retest ran the whole set.
test("one pass reports every failure of every touched area; --retest runs only the failed files until green; then the whole set runs once", () => {
  const dir = foreign();
  try {
    const step = (id, glob) => ({ id, dir: ".", list: ["git", "ls-files", "--", glob], cmd: nodeTest });
    setConfig(dir, (c) => {
      c.areas[0].steps = [step("tests:ios", "ios/*Tests.mjs")];
      c.areas[1].steps = [step("tests:android", "android/*Tests.mjs")];
    });
    const pass = 'import test from "node:test";\ntest("fine", () => {});\n';
    const red = (n) => `import test from "node:test";\ntest("red ${n}", () => { throw new Error("seeded ${n}"); });\n`;
    const files = {
      "ios/aTests.mjs": pass, "ios/bTests.mjs": red("b"), "ios/cTests.mjs": red("c"), "ios/dTests.mjs": pass + 'test("a known gap", { todo: "later" }, () => { throw new Error("gap"); });\n',
      "android/xTests.mjs": red("x"), "android/yTests.mjs": pass,
    };
    for (const [p, body] of Object.entries(files)) {
      writeFileSync(join(dir, p), body);
    }
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "three seeded failing tests in two areas");
    const all = checksIn(dir, { RED: "0" });
    assert.equal(all.status, 13, all.stdout + all.stderr);
    // ONE report: both areas' steps, all three failed files
    assert.match(all.stderr, /2 step\(s\) FAILED, 3 test file\(s\): tests:ios\(4\), tests:android\(2\)/);
    for (const f of ["ios/bTests.mjs", "ios/cTests.mjs", "android/xTests.mjs"]) {
      assert.match(all.stderr, new RegExp(`^  ${f.replace(".", "\\.")}$`, "m"), f);
    }
    assert.doesNotMatch(all.stderr, /^  ios\/aTests/m);
    assert.doesNotMatch(all.stderr, /^  ios\/dTests/m, "a failing todo test is not a failed file");
    const state = JSON.parse(
      readFileSync(join(dir, ".git/spire-land/failed.json"), "utf8"),
    );
    assert.deepEqual(state.steps.map((x) => [x.id, x.files.sort()]), [
      ["tests:ios", ["ios/bTests.mjs", "ios/cTests.mjs"]],
      ["tests:android", ["android/xTests.mjs"]],
    ]);
    // --retest: only the failed files run (2 + 1, not 4 + 2), and still red names the ones still red
    const retest = (...a) =>
      spawnSync(join(dir, ".spire/tool/checks.sh"), ["--retest", "main"], {
        cwd: dir,
        env: { ...env, RUNS: join(dir, "..", "x.runs"), ...Object.fromEntries(a) },
        encoding: "utf8",
      });
    writeFileSync(join(dir, "ios/bTests.mjs"), pass);
    git(dir, "commit", "-qam", "fix b");
    const still = retest();
    assert.equal(still.status, 13, still.stdout + still.stderr);
    assert.match(still.stderr, /2 step\(s\) FAILED, 2 test file\(s\): tests:ios\(2\), tests:android\(1\)/); // b and c re-ran (b is green now), x: not the 6 of the whole set
    writeFileSync(join(dir, "ios/cTests.mjs"), pass);
    git(dir, "commit", "-qam", "fix c");
    const one = retest();
    assert.equal(one.status, 13, one.stdout + one.stderr);
    assert.match(one.stderr, /1 step\(s\) FAILED, 1 test file\(s\): tests:android\(1\)/);
    writeFileSync(join(dir, "android/xTests.mjs"), pass);
    git(dir, "commit", "-qam", "fix x");
    const green = retest();
    assert.equal(green.status, 0, green.stdout + green.stderr);
    assert.match(green.stdout, /^retest green: tests:android\(1\):\ds /m);
    assert.equal(existsSync(join(dir, ".git/spire-land/failed.json")), false);
    // nothing to retest once green; and a plain run is the whole set ONCE, attested
    assert.equal(retest().status, 2);
    const full = checksIn(dir);
    assert.equal(full.status, 0, full.stdout + full.stderr);
    assert.match(full.stdout, /areas=ios,android tests=7 files=6 checks=tests:ios\(4\):\ds,tests:android\(2\):\ds /);
  } finally {
    cleanup(dir);
  }
});

test("a path in two areas runs both; a path in no area runs the closest enclosing area; a path nowhere runs the full suite", () => {
  const dir = foreign();
  try {
    // ios/README.md is in the ios area and in docs: both run, and android does not
    git(dir, "checkout", "-q", "main");
    git(dir, "checkout", "-q", "-b", "two");
    writeFileSync(join(dir, "ios/README.md"), "r\n");
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "a document in ios");
    assert.deepEqual(classifyIn(dir).areas.names, ["ios", "docs"]);
    const two = checksIn(dir);
    assert.equal(two.status, 0, two.stdout + two.stderr);
    assert.match(two.stdout, /areas=ios,docs /);
    assert.deepEqual(runsOf(dir).sort(), ["doc-check", "unit"]); // no swift file changed: the lint gate (whenChanged) stays out

    // a file under android/ that no glob of the area names (android/** owns it: so narrow the area to its sources first)
    rmSync(join(dir, "..", `${dir.split("/").pop()}.runs`), { force: true });
    git(dir, "checkout", "-q", "main");
    git(dir, "checkout", "-q", "-b", "enclosing");
    setConfig(dir, (c) => {
      c.areas[1].paths = ["android/src/**", "android/**/*.kt"];
    });
    mkdirSync(join(dir, "android/gradle"), { recursive: true });
    writeFileSync(join(dir, "android/gradle/wrapper.properties"), "x\n");
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "a build file next to the sources");
    const info = classifyIn(dir);
    assert.deepEqual(info.areas.names, ["android"]); // the closest enclosing area, every test of it
    assert.equal(info.areas.full, false);

    // a path in no area and under no area's directory: UNMAPPED, the full suite
    git(dir, "checkout", "-q", "main");
    git(dir, "checkout", "-q", "-b", "unmapped");
    writeFileSync(join(dir, ".tool-versions"), "node 22\n");
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "a root file");
    const full = classifyIn(dir);
    assert.equal(full.areas.full, true);
    assert.deepEqual(full.areas.unmapped, [".tool-versions"]);
    assert.deepEqual(full.areas.names, ["ios", "android", "docs"]);
    rmSync(join(dir, "..", `${dir.split("/").pop()}.runs`), { force: true });
    const all = checksIn(dir);
    assert.equal(all.status, 0, all.stdout + all.stderr);
    assert.match(all.stdout, /areas=ios,android,docs /);
    assert.match(all.stdout, /full-suite-for-unmapped=\.tool-versions/);
    assert.deepEqual(runsOf(dir).sort(), ["doc-check", "gradle", "unit"]);
  } finally {
    cleanup(dir);
  }
});

// Measured: 90 of 242 landings in a day ended at the council or a red step and then re-ran every step on the same tree (a line in the commit message changes no tree). A step that passed on a tree is kept for that tree only:
// a run stopped half way keeps what passed, so the next run of the same tree runs what did not; another tree runs every step again (what Land-Checks attests ran on the tree that lands).
// Oracle: real processes and commits; each step appends its id to a file, so what ran is counted.
test("a step that passed on a tree is kept for that tree: a red run re-runs only the red step on the same tree; any other tree runs every step", () => {
  const dir = foreign();
  try {
    writeFileSync(join(dir, "android/App.kt"), "val a = 2\n");
    git(dir, "commit", "-qam", "android: a change");
    const run = (extra = {}) => {
      rmSync(join(dir, "..", `${dir.split("/").pop()}.runs`), { force: true });
      const r = checksIn(dir, extra);
      return { r, ran: runsOf(dir).sort() };
    };
    let o = run({ RED: "1" });
    assert.equal(o.r.status, 13, o.r.stdout + o.r.stderr);
    assert.deepEqual(o.ran, ["gradle", "swiftlint", "unit"]);
    // the same tree, the red step now green: only it runs; the others passed on this very tree and are said to be kept
    o = run({ RED: "0" });
    assert.equal(o.r.status, 0, o.r.stdout + o.r.stderr);
    assert.deepEqual(o.ran, ["unit"]);
    assert.match(o.r.stdout, /gradle:kept/);
    assert.match(o.r.stdout, /swiftlint:kept/);
    // a marker cut short is no marker: the step runs
    const markers = join(dir, ".git/spire-land/step-green");
    for (const f of readdirSync(markers)) writeFileSync(join(markers, f), "{");
    o = run({ RED: "0" });
    assert.deepEqual(o.ran, ["gradle", "swiftlint", "unit"]);
    // a fix in the android area is another tree: every step runs again, none is carried over
    writeFileSync(join(dir, "android/App.kt"), "val a = 3\n");
    git(dir, "commit", "-qam", "android: the fix");
    o = run({ RED: "0" });
    assert.equal(o.r.status, 0, o.r.stdout + o.r.stderr);
    assert.deepEqual(o.ran, ["gradle", "swiftlint", "unit"]);
    assert.doesNotMatch(o.r.stdout, /kept/);
  } finally {
    cleanup(dir);
  }
});

test("a tree that passed is not run again, but a main that moved after the checks is a new tree: the touched areas run again on it", () => {
  const dir = foreign();
  try {
    const first = checksIn(dir);
    assert.equal(first.status, 0, first.stdout + first.stderr);
    assert.equal(runsOf(dir).filter((r) => r === "unit").length, 1);

    // the same tree: nothing runs, and the line says each step was kept
    const again = checksIn(dir);
    assert.equal(again.status, 0);
    assert.match(again.stdout, /unit:kept/);
    assert.equal(runsOf(dir).filter((r) => r === "unit").length, 1);

    // main moves in a file the change does not touch (a document: no test can read it here): the rebased tree is another tree, and the area runs again
    git(dir, "checkout", "-q", "main");
    writeFileSync(join(dir, "docs/a.md"), "a\nmore\n");
    git(dir, "commit", "-qam", "docs: main moved");
    git(dir, "checkout", "-q", "work");
    git(dir, "rebase", "-q", "main");
    const rebased = checksIn(dir);
    assert.equal(rebased.status, 0, rebased.stdout + rebased.stderr);
    assert.doesNotMatch(rebased.stdout, /kept/);
    assert.equal(runsOf(dir).filter((r) => r === "unit").length, 2);

    // main moves in a file of the change's own area: it runs again as well, and a red there stops the landing
    git(dir, "checkout", "-q", "main");
    writeFileSync(join(dir, "ios/Other.swift"), "let c = 1\n");
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "ios: main moved in the area");
    git(dir, "checkout", "-q", "work");
    git(dir, "rebase", "-q", "main");
    const red = checksIn(dir, { RED: "1" });
    assert.equal(red.status, 13, red.stdout + red.stderr);
    assert.equal(runsOf(dir).filter((r) => r === "unit").length, 3);
  } finally {
    cleanup(dir);
  }
});

// Admission: these tests show the cheaper SECOND REBASE. After a rebase onto a moved main the cheap whole-tree steps run again on the new tree, and the test files of a group that the change does not add or edit are carried (not run) only when the same change (its diff's
// patch-id) passed it on an earlier tree and carry.mjs allows it (the rule is there); the line says so by area (`tests:ios(1 re-run of 2, rest carried from TREE)`: the change's own test file runs, the rest is carried) and the
// tree it names is the one the tests really ran on. There is no switch to carry: the earlier tree is what the runs recorded. They would fail if a group that main's change could affect were carried, if a cheap step were carried,
// if the line said `kept` or nothing for a carried group, if a retry of the carried tree ran the group again, if a carry named the wrong tree, or if another change were given the pass.
// Oracle: real commits, the real checks.sh and real `node --test` files that append their id to a file.
test("a rebase that brings in none of the change's own files nor a file the rest reaches carries the untouched test files and re-runs the change's own; the cheap checks rerun; one that does, or the tool, or the project's file, or a changed diff, reruns everything", () => {
  const dir = foreign();
  try {
    const testStep = (area) => ({ id: `tests:${area}`, dir: ".", list: ["git", "ls-files", "--", `${area}/*Tests.mjs`], cmd: nodeTest });
    setConfig(dir, (c) => {
      c.areas[0].steps = [c.areas[0].steps[0], testStep("ios")]; // swiftlint (cheap, whole tree), then the area's tests
      c.areas[1].steps = [testStep("android")];
    });
    const body = (id) => `import { appendFileSync } from "node:fs";\nimport test from "node:test";\ntest("t", () => appendFileSync(process.env.RUNS, "${id}\\n"));\n`;
    quiet(dir);
    writeFileSync(join(dir, "ios/aTests.mjs"), body("tests:ios"));
    writeFileSync(join(dir, "android/bTests.mjs"), body("tests:android"));
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "tests in both areas");
    const fresh = () => rmSync(join(dir, "..", `${dir.split("/").pop()}.runs`), { force: true });
    const treeOf = () => git(dir, "rev-parse", "HEAD^{tree}").trim();
    const both = ["swiftlint", "tests:android", "tests:ios"];
    const onMain = (file, text, message) => {
      const at = git(dir, "rev-parse", "--abbrev-ref", "HEAD").trim();
      git(dir, "checkout", "-q", "main");
      mkdirSync(join(dir, file, ".."), { recursive: true });
      writeFileSync(join(dir, file), text);
      git(dir, "add", "-A");
      git(dir, "commit", "-qm", message);
      git(dir, "checkout", "-q", at);
      git(dir, "rebase", "-q", "main");
    };
    const first = checksIn(dir);
    assert.equal(first.status, 0, first.stdout + first.stderr);
    assert.deepEqual(runsOf(dir).sort(), both);
    const t0 = treeOf();

    // main moved in a document no area's tests read: every group is carried, the cheap step runs again, and the line says so by area and names the tree they ran on
    onMain("docs/a.md", "a\nmore\n", "docs: main moved");
    fresh();
    const a = checksIn(dir);
    assert.equal(a.status, 0, a.stdout + a.stderr);
    assert.deepEqual(runsOf(dir).sort(), both);
    assert.match(a.stdout, /tests:ios\(1 re-run of 2, rest carried from /); // only the change's own test file ran, the untouched one is carried
    assert.match(a.stdout, /tests:android\(1 re-run of 2, rest carried from /);
    assert.match(a.stderr, /carried tests:ios: .*; re-run 1 test file\(s\)/);
    assert.doesNotMatch(a.stdout, /tests:ios\(1\):\ds/); // never the unnarrowed label: the untouched file did not run
    assert.match(a.stdout, /tests=2 /); // the count is the pass that is carried
    assert.match(a.stdout, new RegExp(`tree=${treeOf().slice(0, 12)}`)); // Land-Checks names the tree that lands
    // the same tree again: the marker keyed by the tree keeps the carried pass and nothing runs (the cheap step has a marker now)
    fresh();
    const again = checksIn(dir);
    assert.equal(again.status, 0, again.stdout + again.stderr);
    assert.deepEqual(runsOf(dir), []);
    assert.match(again.stdout, new RegExp(`ios:carried from ${t0.slice(0, 12)}`)); // still said to be carried, never `kept` as if it had run here

    // a second rebase over another document names the tree the tests REALLY ran on, not the one before it
    onMain("docs/b.md", "b\n", "docs: main moved again");
    fresh();
    const chain = checksIn(dir);
    assert.equal(chain.status, 0, chain.stdout + chain.stderr);
    assert.deepEqual(runsOf(dir).sort(), both);
    assert.match(chain.stdout, new RegExp(`tests:ios\\(1 re-run of 2, rest carried from ${t0.slice(0, 12)}`));

    // main moved in a file of ios that the change does not touch and no test of the change imports (carry.mjs: the rule is the change's own files, not the area's paths): both groups are carried
    onMain("ios/Other.swift", "let c = 1\n", "ios: main moved in the area");
    fresh();
    const b = checksIn(dir);
    assert.equal(b.status, 0, b.stdout + b.stderr);
    assert.deepEqual(runsOf(dir).sort(), both);
    assert.match(b.stdout, /tests:android\(1 re-run of 2, rest carried from /);
    assert.match(b.stdout, /tests:ios\(1 re-run of 2, rest carried from /);
    assert.match(b.stderr, /carried tests:ios: /);
    assert.match(b.stderr, /re-run swiftlint: not a test step/);

    // the project's file moved (what an area runs is in it): nothing is carried
    setConfig(dir, (c) => (c.checks.note = "moved"));
    fresh();
    const cfg = checksIn(dir);
    assert.equal(cfg.status, 0, cfg.stdout + cfg.stderr);
    assert.deepEqual(runsOf(dir).sort(), both);
    assert.doesNotMatch(cfg.stdout, /carried/);

    // the tool moved in a file the runner reads (it imports it): nothing is carried either
    const plan = readFileSync(join(dir, ".spire/tool/plan.mjs"), "utf8");
    onMain(".spire/tool/plan.mjs", `${plan}\n// a newer tool\n`, "the tool moved");
    fresh();
    const tool = checksIn(dir);
    assert.deepEqual(runsOf(dir).sort(), both);
    assert.doesNotMatch(tool.stdout, /carried/);
    assert.match(tool.stderr, /plan\.mjs is read by the gate's own runner/);

    // a file under .spire/ that the runner does not read (no table of .spire/ paths: reach): the groups are carried
    onMain(".spire/tool/note.txt", "a note nobody reads\n", "the tool has a note");
    fresh();
    const note = checksIn(dir);
    assert.equal(note.status, 0, note.stdout + note.stderr);
    assert.match(note.stderr, /carried tests:ios: /);
    assert.match(note.stdout, /tests:android\(1 re-run of 2, rest carried from /);

    // the rebase changed the change's own file (a resolution that differs only by white space inside a line, which a patch-id does not see): an overlap, so every test group runs
    writeFileSync(join(dir, "ios/aTests.mjs"), body("tests:ios").replace("appendFileSync(process", "appendFileSync( process"));
    git(dir, "commit", "-qa", "--amend", "-m", "tests in both areas");
    onMain("docs/c.md", "c\n", "docs: main moved a third time");
    fresh();
    const blank = checksIn(dir);
    assert.equal(blank.status, 0, blank.stdout + blank.stderr);
    assert.deepEqual(runsOf(dir).sort(), both); // the rule is the change's own files (carry.mjs), not the area's: an overlap runs every test group
    assert.match(blank.stderr, /re-run tests:android: ios\/aTests\.mjs came in and the change touches it too/);

    // another change (its diff is not the one that passed) is given no pass: a fix in ios is another diff, so main moving after it carries nothing from before it
    writeFileSync(join(dir, "ios/App.swift"), "let a = 9\nlet b = 3\n");
    git(dir, "commit", "-qam", "ios: another change");
    onMain("docs/d.md", "d\n", "docs: main moved a fourth time");
    fresh();
    const other = checksIn(dir);
    assert.equal(other.status, 0, other.stdout + other.stderr);
    assert.deepEqual(runsOf(dir).sort(), both);
    assert.doesNotMatch(other.stdout, /carried/);
  } finally {
    cleanup(dir);
  }
});

// Admission (carry.mjs): what decides a carry is the change's OWN files and what the incoming files REACH, replayed here on six sequential incoming commits to files the change does not touch (all carried: A3), then on an
// incoming file the change's test imports (the test re-runs), a lock file (reaches the package's tests) and a file the change touches (runs everything). They would fail if a rebase over unrelated files ran the tests again, or if a reached test or an overlap were carried.
// Oracle: real commits, the real checks.sh and real `node --test` files that append their id to a file; each run prints one `carried|re-run ID: reason` line per step.
test("a replay of six incoming commits carries every test step; an overlap and a lock file carry none, and a file a test imports is not read", () => {
  const dir = foreign();
  try {
    const testStep = (area) => ({ id: `tests:${area}`, dir: ".", list: ["git", "ls-files", "--", `${area}/*Tests.mjs`], cmd: nodeTest });
    setConfig(dir, (c) => {
      c.areas[0].steps = [c.areas[0].steps[0], testStep("ios")];
      c.areas[1].steps = [testStep("android")];
    });
    const body = (id, imp) => `import { appendFileSync } from "node:fs";\nimport test from "node:test";\n${imp}test("t", () => appendFileSync(process.env.RUNS, "${id}\\n"));\n`;
    const fresh = () => rmSync(join(dir, "..", `${dir.split("/").pop()}.runs`), { force: true });
    const onMain = (file, text, message) => {
      const at = git(dir, "rev-parse", "--abbrev-ref", "HEAD").trim();
      git(dir, "checkout", "-q", "main");
      mkdirSync(join(dir, file, ".."), { recursive: true });
      writeFileSync(join(dir, file), text);
      git(dir, "add", "-A");
      git(dir, "commit", "-qm", message);
      git(dir, "checkout", "-q", at);
      git(dir, "rebase", "-q", "main");
    };
    quiet(dir);
    onMain("ios/lib/util.mjs", "export const u = 1;\n", "main has the helper");
    writeFileSync(join(dir, "ios/aTests.mjs"), body("tests:ios", 'import "./lib/util.mjs";\n'));
    writeFileSync(join(dir, "android/bTests.mjs"), body("tests:android", ""));
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "tests in both areas");
    const first = checksIn(dir);
    assert.equal(first.status, 0, first.stdout + first.stderr);
    let carriedSteps = 0;
    let totalSteps = 0;
    for (let i = 0; i < 6; i++) {
      onMain(`android/server/f${i}.txt`, `${i}\n`, `main moved ${i}`);
      fresh();
      const r = checksIn(dir);
      assert.equal(r.status, 0, r.stdout + r.stderr);
      assert.deepEqual(runsOf(dir).sort(), ["swiftlint", "tests:android", "tests:ios"]);
      const lines = r.stderr.split("\n").filter((l) => /^land: (carried|re-run) /.test(l));
      assert.equal(lines.length, 3, r.stderr); // one line per step
      carriedSteps += lines.filter((l) => /^land: carried tests:/.test(l)).length;
      totalSteps += lines.filter((l) => /tests:/.test(l)).length;
    }
    assert.equal(`${carriedSteps}/${totalSteps}`, "12/12"); // 6 rebases x 2 test steps
    // a file the change's own test imports came in: no import is read; the own test file re-runs against it, so the change to util.mjs is executed (the inspector's case), and the untouched zTests carry
    onMain("ios/lib/util.mjs", "export const u = 2;\n", "main moved a file a test of the change imports");
    fresh();
    const imp = checksIn(dir);
    assert.equal(imp.status, 0, imp.stdout + imp.stderr);
    assert.deepEqual(runsOf(dir).sort(), ["swiftlint", "tests:android", "tests:ios"]);
    assert.match(imp.stderr, /land: carried tests:ios: /);
    assert.match(imp.stderr, /land: carried tests:android: /);
    // a lock file
    onMain("package-lock.json", "{}\n", "main moved the lock file");
    fresh();
    const lock = checksIn(dir);
    assert.deepEqual(runsOf(dir).sort(), ["swiftlint", "tests:android", "tests:ios"]);
    assert.match(lock.stderr, /carried tests:ios: .*; re-run 2 test file\(s\)/); // the root lock file reaches every test of the tree (no table: reach): both re-run, none is carried
    assert.match(lock.stderr, /carried tests:android: .*; re-run 2 test file\(s\)/);
    // overlap: the rebase leaves a file the change touches different from the earlier pass's (a resolution differing by white space, which a patch-id does not see): 0 carried
    writeFileSync(join(dir, "android/bTests.mjs"), body("tests:android", "").replace("appendFileSync(process", "appendFileSync( process"));
    git(dir, "commit", "-qa", "--amend", "-m", "tests in both areas");
    onMain("docs/z.md", "z\n", "main moved a document");
    fresh();
    const ov = checksIn(dir);
    assert.deepEqual(runsOf(dir).sort(), ["swiftlint", "tests:android", "tests:ios"]);
    assert.equal(ov.stderr.split("\n").filter((l) => /^land: carried /.test(l)).length, 0, ov.stderr);
  } finally {
    cleanup(dir);
  }
});

// Admission (spire-venue#8358, the real logs said `no earlier pass of this change` on a step that HAD passed on the earlier tree): the record of an earlier pass is keyed by the change and the step's command, and the command of a
// test step ends in the list of the test files of the TREE. A rebase onto a main that added a test file made another list, so another key, so no record. The key is the change and the step (its id, directory, environment, command
// WITHOUT the list). They would fail if a test file arriving on main hid the earlier pass of the group that lists it.
// Oracle: real commits, the real checks.sh; the group that lists the new file prints its decision line.
test("a rebase onto a main that added a test file to the group's list still finds the earlier pass: the list is no part of the key", () => {
  const dir = foreign();
  try {
    const testStep = (area) => ({ id: `tests:${area}`, dir: ".", list: ["git", "ls-files", "--", `${area}/*Tests.mjs`], cmd: nodeTest });
    setConfig(dir, (c) => {
      c.areas[0].steps = [c.areas[0].steps[0], testStep("ios")];
      c.areas[1].steps = [testStep("android")];
    });
    const body = (id) => `import { appendFileSync } from "node:fs";\nimport test from "node:test";\ntest("t", () => appendFileSync(process.env.RUNS, "${id}\\n"));\n`;
    quiet(dir);
    writeFileSync(join(dir, "ios/aTests.mjs"), body("tests:ios"));
    writeFileSync(join(dir, "android/bTests.mjs"), body("tests:android"));
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "tests in both areas");
    const first = checksIn(dir);
    assert.equal(first.status, 0, first.stdout + first.stderr);
    const at = git(dir, "rev-parse", "--abbrev-ref", "HEAD").trim();
    git(dir, "checkout", "-q", "main");
    writeFileSync(join(dir, "android/cTests.mjs"), body("main:android"));
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "main adds a test file to the android group");
    git(dir, "checkout", "-q", at);
    git(dir, "rebase", "-q", "main");
    rmSync(join(dir, "..", `${dir.split("/").pop()}.runs`), { force: true });
    const r = checksIn(dir);
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.doesNotMatch(r.stderr, /no earlier pass/);
    assert.match(r.stderr, /land: carried tests:android: /);
    assert.match(r.stderr, /land: carried tests:ios: /);
    // the file main added IS run on this tree: a test file that came in re-runs (carry.mjs); the group's other file is carried
    assert.ok(runsOf(dir).includes("main:android"));
    assert.deepEqual(runsOf(dir).sort(), ["main:android", "swiftlint", "tests:android", "tests:ios"]);
  } finally {
    cleanup(dir);
  }
});

// every test of the area runs, however many, and the line carries the runner's own count; a failed file is red, whatever the cause: nothing re-runs it
test("every test file of the area runs, however many; a file that fails is red, and named", () => {
  const dir = foreign();
  try {
    const testStep = {
      id: "tests:ios",
      dir: ".",
      list: ["git", "ls-files", "--", "ios/*Tests.mjs"],
      cmd: nodeTest,
    };
    setConfig(dir, (c) => {
      c.areas[0].steps = [testStep];
    });
    // 12 test files with 2 tests each, none of them related to the file the change touches
    for (let i = 0; i < 12; i++) {
      writeFileSync(
        join(dir, `ios/t${String(i).padStart(2, "0")}Tests.mjs`),
        'import test from "node:test";\ntest("one", () => {});\ntest("two", () => {});\n',
      );
    }
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "twelve tests");
    const all = checksIn(dir);
    assert.equal(all.status, 0, all.stdout + all.stderr);
    assert.match(all.stdout, /areas=ios tests=24 files=12 checks=tests:ios\(12\):\ds /);

    writeFileSync(
      join(dir, "ios/flakyTests.mjs"),
      `import { existsSync, writeFileSync } from "node:fs";\nimport test from "node:test";\n\ntest("fails the first time only", () => {\n  if (!existsSync(process.env.RUNS + ".flaky")) {\n    writeFileSync(process.env.RUNS + ".flaky", "x");\n    throw new Error("a timeout under load");\n  }\n});\n`,
    );
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "a flaky test");
    const flaky = checksIn(dir);
    assert.equal(flaky.status, 13, "a file that fails the first time only is red: nothing re-runs it");
    assert.match(flaky.stderr, /flakyTests\.mjs/);

    writeFileSync(
      join(dir, "ios/redTests.mjs"),
      'import test from "node:test";\n\ntest("really red", () => {\n  throw new Error("a real failure");\n});\n',
    );
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "a red test");
    const red = checksIn(dir);
    assert.equal(red.status, 13, red.stdout + red.stderr);
    assert.match(red.stderr, /check FAILED: tests:ios\(14\)/);
    assert.match(
      red.stderr,
      /test at .*redTests\.mjs/,
      "the failing file is named, on any Node (the reporter is fixed, not the runner's default)",
    );
  } finally {
    cleanup(dir);
  }
});

test("a test only the area's command lists runs too (a test an agent adds lives in its area by path); an area that lists none is a fault, never a skip", () => {
  const dir = foreign();
  try {
    setConfig(dir, (c) => {
      c.areas[0].steps = [
        {
          id: "tests:ios",
          dir: ".",
          list: ["git", "ls-files", "--", "ios/*Tests.mjs"],
          cmd: nodeTest,
        },
      ];
    });
    const none = checksIn(dir);
    assert.equal(none.status, 13, none.stdout + none.stderr);
    assert.match(none.stderr, /lists no test file/);
    writeFileSync(
      join(dir, "ios/newTests.mjs"),
      'import test from "node:test";\ntest("added by an agent", () => { throw new Error("red"); });\n',
    );
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "a new test");
    const red = checksIn(dir);
    assert.equal(red.status, 13, red.stdout + red.stderr);
    assert.match(red.stderr, /newTests\.mjs/);
  } finally {
    cleanup(dir);
  }
});

test("a project file with no areas stops the classification: nothing would be checked", () => {
  const dir = foreign();
  try {
    setConfig(dir, (c) => {
      c.areas = [];
    });
    const r = spawnSync(
      "node",
      [join(dir, ".spire/tool/classify.mjs"), "main", "HEAD"],
      { cwd: dir, env, encoding: "utf8" },
    );
    assert.equal(r.status, 2);
    assert.match(r.stderr, /declares no test areas/);
  } finally {
    cleanup(dir);
  }
});

// Admission: an environment a step needs (for Spire, Postgres) is MADE when the host lacks it, by the project's recipe, and a host that cannot make it FAILS the step (exit 13, naming the
// host's bug); a project may choose "skip-and-say" and then the line says so. It would fail if a missing environment skipped its checks without a word.
test("a missing environment is made by the recipe; one that cannot be made fails the step, never a skip", () => {
  const step = {
    id: "db",
    dir: ".",
    environment: "db",
    cmd: node("if (process.env.DB !== 'ready') process.exit(1)"),
  };
  const recipe = (ifMissing, ensure) => ({
    db: {
      wrap: ["env", "DB={bundle}"],
      bundle: {
        have: ["node", "-e", "process.exit(require('fs').existsSync('made') ? 0 : 1)"],
        path: ["node", "-e", "console.log('ready')"],
        ...(ensure
          ? { ensure: ["node", "-e", "require('fs').writeFileSync('made', 'x')"] }
          : {}),
      },
      ifMissing,
    },
  });
  for (const [label, ifMissing, ensure, status, pattern] of [
    ["made by the recipe", "fail", true, 0, /checks=swiftlint:\ds,unit:\ds,db:\ds/],
    ["cannot be made: the step fails", "fail", false, 13, /check FAILED: db/],
    ["cannot be made: skip and say", "skip-and-say", false, 0, /skipped=db: no db environment/],
  ]) {
    const dir = foreign({ environments: recipe(ifMissing, ensure) });
    try {
      setConfig(dir, (c) => c.areas[0].steps.push(step));
      const r = checksIn(dir);
      assert.equal(r.status, status, `${label}: ${r.stdout}${r.stderr}`);
      // a skip is never silent: the line says what was skipped and why
      assert.match(status === 0 ? r.stdout : r.stderr, pattern, label);
    } finally {
      cleanup(dir);
    }
  }
});

// Inside an instance (LAND_INSTANCE_DB: its database server) a step that needs an environment runs the recipe's `attach` form on that server, so no second server is made; outside one it runs `wrap`.
test("inside an instance a step runs the recipe's attach form on the instance's server, never its own", () => {
  const dir = foreign({
    environments: {
      db: {
        wrap: ["env", "DB=own"],
        attach: ["env", "DB=attached"],
        bundle: { have: ["true"], path: ["echo", "unused"] },
        ifMissing: "fail",
      },
    },
  });
  try {
    setConfig(dir, (c) =>
      c.areas[0].steps.push({
        id: "db",
        dir: ".",
        environment: "db",
        cmd: node("if (process.env.DB !== 'attached') process.exit(1)"),
      }),
    );
    const inside = checksIn(dir, { LAND_INSTANCE_DB: "db://i/db" });
    assert.equal(inside.status, 0, inside.stdout + inside.stderr);
    assert.match(inside.stdout, /db:\ds/);
    git(dir, "checkout", "-q", "-b", "other");
    writeFileSync(join(dir, "ios/App.swift"), "let a = 9\n");
    git(dir, "commit", "-qam", "another change");
    const outside = checksIn(dir, { LAND_INSTANCE_DB: "" });
    assert.equal(outside.status, 13, "outside an instance the step runs its own wrap");
    assert.match(outside.stderr, /check FAILED: db/);
  } finally {
    cleanup(dir);
  }
});

test("inside a box a step that names no environment is not given the box's database URL; a step that does, is", () => {
  const dir = foreign({
    environments: {
      db: { wrap: ["env"], bundle: { have: ["true"], path: ["echo", "unused"] }, ifMissing: "fail" },
    },
  });
  try {
    setConfig(dir, (c) =>
      c.areas[0].steps.push(
        { id: "plain", dir: ".", cmd: node("if (process.env.DATABASE_URL) process.exit(1)") },
        { id: "withdb", dir: ".", environment: "db", cmd: node("if (!process.env.DATABASE_URL) process.exit(1)") },
      ),
    );
    const r = checksIn(dir, { DATABASE_URL: "db://box/db", LAND_BOX_EXPORTS: "DATABASE_URL" });
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /plain:\ds/);
    assert.match(r.stdout, /withdb:\ds/);
  } finally {
    cleanup(dir);
  }
});

test("checks.install: an install runs when what it makes (`installed`, the project's own word) is missing and not when it is there; with none declared it runs every time", () => {
  for (const [installed, second] of [[".deps", 1], [undefined, 2]]) {
    const dir = foreign({
      checks: {
        testConcurrencyFlag: "--test-concurrency={n}",
        testReporterFlag: "--test-reporter=spec",
        install: { dirs: ["."], lock: "Package.resolved", ...(installed ? { installed } : {}), cmd: node("require('fs').appendFileSync(process.env.RUNS, 'install\\n'); require('fs').mkdirSync('.deps', { recursive: true })") },
      },
    });
    try {
      assert.equal(checksIn(dir).status, 0);
      assert.equal(runsOf(dir).filter((x) => x === "install").length, 1, "the first run installs");
      writeFileSync(join(dir, "ios/App.swift"), "let a = 3\nlet b = 4\n");
      git(dir, "commit", "-qam", "ios: another change");
      assert.equal(checksIn(dir).status, 0);
      assert.equal(runsOf(dir).filter((x) => x === "install").length, second, installed ? "what it made is there: no second install" : "nothing declared: it runs again");
    } finally {
      cleanup(dir);
    }
  }
});

test("a step runs in a clean environment: the base and the names its own `pass` lists reach it, nothing else of the caller's shell; a step that names an environment also gets the box's exports", () => {
  const dir = foreign({
    environments: {
      db: { wrap: ["env"], bundle: { have: ["true"], path: ["echo", "unused"] }, ifMissing: "fail" },
    },
  });
  const seen = (...names) =>
    node(`const seen = ${JSON.stringify(names)}.filter((n) => process.env[n] !== undefined); require('fs').appendFileSync(process.env.RUNS, process.env.STEP + ':' + seen.join(',') + '\\n')`);
  try {
    setConfig(dir, (c) => {
      const probe = (id, extra) => ({ id, dir: ".", env: { STEP: id }, pass: ["RUNS"], cmd: seen("PATH", "HOME", "LANG", "LC_ALL", "SPIRE_EXECUTION_WORKER", "POSTGRES_PORT", "OPENROUTER_BASE_URL", "HOST_SECRETS", "DATABASE_URL", "EXTRA", "BOX_VAR"), ...extra });
      c.areas[0].steps.push(
        probe("plain"),
        probe("passes", { pass: ["RUNS", "EXTRA"] }),
        probe("withenv", { environment: "db" }),
      );
    });
    const r = checksIn(dir, {
      LANG: "C.UTF-8",
      LC_ALL: "C.UTF-8",
      SPIRE_EXECUTION_WORKER: "0",
      POSTGRES_PORT: "5999",
      OPENROUTER_BASE_URL: "http://127.0.0.1:7500",
      HOST_SECRETS: "OPENROUTER_API_KEY",
      DATABASE_URL: "db://caller/db",
      EXTRA: "1",
      BOX_VAR: "b",
      LAND_BOX_EXPORTS: "BOX_VAR",
    });
    assert.equal(r.status, 0, r.stdout + r.stderr);
    const runs = runsOf(dir);
    assert.ok(runs.includes("plain:PATH,HOME,LANG,LC_ALL"), runs.join("\n"));
    assert.ok(runs.includes("passes:PATH,HOME,LANG,LC_ALL,EXTRA"), runs.join("\n"));
    assert.ok(runs.includes("withenv:PATH,HOME,LANG,LC_ALL,BOX_VAR"), runs.join("\n"));
  } finally {
    cleanup(dir);
  }
});

// The independent steps of the areas run AT THE SAME TIME (each step records when it started and when it ended: all three start before any ends, whatever the host's load; no wall-clock bound), every failure is reported (not only the first), and the line keeps the plan's order.
test("the independent steps run at the same time; every failure is reported", () => {
  const dir = foreign();
  let marks;
  try {
    marks = mkdtempSync(join(tmpdir(), "marks-"));
    const wait = (id, code = 0) => ({
      id,
      dir: ".",
      cmd: node(`const fs = require("fs"); fs.writeFileSync(${JSON.stringify(join(marks, `${id}.start`))}, String(Date.now())); setTimeout(() => { fs.writeFileSync(${JSON.stringify(join(marks, `${id}.end`))}, String(Date.now())); process.exit(${code}); }, 1500)`),
    });
    setConfig(dir, (c) => {
      c.areas[0].steps = [wait("one"), wait("two"), wait("three")];
    });
    const green = checksIn(dir);
    assert.equal(green.status, 0, green.stdout + green.stderr);
    const at = (id, k) => Number(readFileSync(join(marks, `${id}.${k}`), "utf8"));
    const lastStart = Math.max(...["one", "two", "three"].map((id) => at(id, "start")));
    const firstEnd = Math.min(...["one", "two", "three"].map((id) => at(id, "end")));
    assert.ok(lastStart < firstEnd, "the three steps ran one after the other: the last started after the first ended");
    assert.match(green.stdout, /checks=one:\ds,two:\ds,three:\ds/);
    setConfig(dir, (c) => {
      c.areas[0].steps = [wait("one", 1), wait("two"), wait("three", 1)];
    });
    const red = checksIn(dir);
    assert.equal(red.status, 13, red.stdout + red.stderr);
    assert.match(red.stderr, /check FAILED: one/);
    assert.match(red.stderr, /check FAILED: three/);
  } finally {
    cleanup(dir);
    if (marks) rmSync(marks, { recursive: true, force: true });
  }
});

// THE CHEAP CHECKS FIRST: a step marked `first` runs alone before the others start; a red among them is reported at once and nothing else starts (the slowest step is never waited for); green, the others then run side by side.
// They would fail if a later step started before a `first` step ended, if a red `first` step let the others run, or if a `first` step waited for one that is not.
// Oracle: real steps that record when they start and end, and append their id to $RUNS.
test("cheap checks marked first run before everything else, a red among them stops the rest at once, and every landing prints what ran and why", () => {
  const dir = foreign();
  let marks;
  try {
    marks = mkdtempSync(join(tmpdir(), "marks-"));
    const stamp = (id, code, first) => ({
      id,
      dir: ".",
      ...(first ? { first: true } : {}),
      cmd: node(`const fs = require("fs"); fs.appendFileSync(process.env.RUNS, ${JSON.stringify(id + "\n")}); fs.writeFileSync(${JSON.stringify(join(marks, `${id}.start`))}, String(Date.now())); setTimeout(() => { fs.writeFileSync(${JSON.stringify(join(marks, `${id}.end`))}, String(Date.now())); process.exit(${code}); }, 600)`),
    });
    const at = (id, k) => Number(readFileSync(join(marks, `${id}.${k}`), "utf8"));
    setConfig(dir, (c) => {
      c.areas[0].steps = [stamp("slow-a", 0, false), stamp("lint", 0, true), stamp("slow-b", 0, false), stamp("types", 0, true)];
    });
    const green = checksIn(dir);
    assert.equal(green.status, 0, green.stdout + green.stderr);
    const lastFirstEnd = Math.max(at("lint", "end"), at("types", "end"));
    assert.ok(at("slow-a", "start") >= lastFirstEnd && at("slow-b", "start") >= lastFirstEnd, "a later step started before the cheap checks ended");
    assert.ok(Math.abs(at("lint", "start") - at("types", "start")) < 400, "the cheap checks ran side by side");
    assert.match(green.stderr, /land: what ran, and why:/);
    assert.match(green.stderr, /land:   lint:\ds \[cheap, first\]: not a test step: it always runs again/);
    // red among the cheap checks: reported at once, the others never start
    for (const f of ["slow-a", "slow-b", "lint", "types"]) for (const k of ["start", "end"]) rmSync(join(marks, `${f}.${k}`), { force: true });
    writeFileSync(join(dir, "..", `${dir.split("/").pop()}.runs`), "");
    setConfig(dir, (c) => {
      c.areas[0].steps = [stamp("slow-a", 0, false), stamp("lint", 1, true), stamp("slow-b", 0, false)];
    });
    const red = checksIn(dir);
    assert.equal(red.status, 13, red.stdout + red.stderr);
    assert.match(red.stderr, /check FAILED: lint/);
    assert.match(red.stderr, /cheap checks first: the 2 other step\(s\) did not start/);
    assert.deepEqual(runsOf(dir), ["lint"]);
    assert.match(red.stderr, /land: what ran, and why:\nland:   lint: FAILED \[cheap, first\]: not a test step/); // the red path prints it too
    assert.match(red.stderr, /land:   lint: FAILED[^\n]*\nland:   areas touched: ios \(1 changed path\(s\)\)\nland:   no step ran for the other areas/);
    // a first step may wait only for another first step
    setConfig(dir, (c) => {
      c.areas[0].steps = [stamp("slow-a", 0, false), { ...stamp("lint", 0, true), after: ["slow-a"] }];
    });
    const bad = checksIn(dir);
    assert.equal(bad.status, 13, bad.stdout + bad.stderr);
    assert.match(bad.stderr, /step lint is `first` but waits for slow-a/);
  } finally {
    cleanup(dir);
    if (marks) rmSync(marks, { recursive: true, force: true });
  }
});

// A step that needs another's output (the stylesheet test after the app build) waits for it; a gate whose `whenChanged` is a list applies when any glob has a changed file; a step two touched areas
// both carry runs once.
test("a step waits for the step it comes after; whenChanged takes a list; a step two areas share runs once", () => {
  const dir = foreign();
  try {
    setConfig(dir, (c) => {
      c.areas[0].steps = [
        { id: "build", dir: ".", cmd: node("setTimeout(() => require('fs').writeFileSync('built', 'x'), 1000)") },
        { id: "needs-build", dir: ".", after: ["build"], cmd: node("process.exit(require('fs').existsSync('built') ? 0 : 1)") },
        { id: "listed", dir: ".", whenChanged: ["nothing/**", "ios/**/*.swift"], cmd: node("process.exit(0)") },
        { id: "not-listed", dir: ".", whenChanged: ["nothing/**", "android/**"], cmd: node("process.exit(1)") },
        { id: "shared", dir: ".", cmd: mark("shared") },
      ];
      c.areas[2].steps = [{ id: "shared", dir: ".", cmd: mark("shared") }];
    });
    writeFileSync(join(dir, "ios/README.md"), "r\n");
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "ios and docs both touched");
    const r = checksIn(dir);
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /checks=build:\ds,needs-build:\ds,listed:\ds,shared:\ds /);
    assert.doesNotMatch(r.stdout, /not-listed/);
    assert.equal(runsOf(dir).filter((x) => x === "shared").length, 1);
  } finally {
    cleanup(dir);
  }
});

// {tmp} in a step's command is a real scratch directory outside the tree under test (the existing-schema startup step writes its receipts there).
test("{tmp} in a step is a scratch directory outside the tree", () => {
  const dir = foreign();
  try {
    const js =
      "const d = process.argv[1]; require('fs').mkdirSync(d, { recursive: true }); require('fs').writeFileSync(d + '/receipt', 'x')";
    setConfig(dir, (c) => {
      c.areas[0].steps = [{ id: "writes", dir: ".", cmd: ["node", "-e", js, "{tmp}/out"] }];
    });
    const r = checksIn(dir);
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.equal(existsSync(join(dir, "{tmp}")), false, "the literal {tmp} directory was made in the tree");
  } finally {
    cleanup(dir);
  }
});

// Every command runs with the project's pinned tool versions (versions.mjs), never the host's default. The host here has the real node first in PATH and a node "22.99.1" installed where mise keeps
// them (a stub that says its version and runs the real node otherwise): the project pins 22.99, so the steps see 22.99.1; a pin the host lacks stops the run before any step starts.
test("the steps run with the project's pinned node, and a pin the host does not have stops the run with the reason before any step", () => {
  const dir = foreign();
  const data = mkdtempSync(join(tmpdir(), "pins-mise-"));
  try {
    const bin = join(data, "installs/node/22.99.1/bin");
    mkdirSync(bin, { recursive: true });
    writeFileSync(
      join(bin, "node"),
      `#!/bin/sh\ncase "$1" in --version|-v) echo v22.99.1; exit 0;; esac\nexec ${process.execPath} "$@"\n`,
      { mode: 0o755 },
    );
    const sh = "echo \"$(node --version) $SPIRE_PINNED\" >> \"$RUNS\"";
    setConfig(dir, (c) => {
      c.areas[0].steps = [{ id: "sees", dir: ".", cmd: ["sh", "-c", sh] }];
    });
    writeFileSync(join(dir, ".node-version"), "22.99\n");
    const hostNode = spawnSync("node", ["--version"], { env, encoding: "utf8" }).stdout.trim();
    assert.notEqual(hostNode, "v22.99.1");
    const r = checksIn(dir, { MISE_DATA_DIR: data });
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.deepEqual(runsOf(dir), ["v22.99.1 node@22.99.1"]);
    assert.match(r.stderr, /spire-land: pins: node 22\.99\.1 \(pin 22\.99 in \.node-version\)/);
    // the pin moves to a version the host lacks: nothing runs, the reason says how to get it
    cleanupRuns(dir);
    writeFileSync(join(dir, ".node-version"), "22.98\n");
    const no = checksIn(dir, { MISE_DATA_DIR: data });
    assert.equal(no.status, 1, no.stdout + no.stderr);
    assert.match(no.stderr, /node: the project pins 22\.98 \(\.node-version\) and this host has no such version\. Installed: .*22\.99\.1.*Install it: mise install node@22\.98\./);
    assert.deepEqual(runsOf(dir), []);
  } finally {
    rmSync(data, { recursive: true, force: true });
    cleanup(dir);
  }
});
// ONE python pin for every step: the project pins python once (.tool-versions); a step's plain `python3` is that interpreter, whatever the host's python3 is, and a pin the host lacks stops the run.
test("the steps' plain python3 is the project's pinned python, and a python pin the host does not have stops the run before any step", () => {
  const dir = foreign();
  const data = mkdtempSync(join(tmpdir(), "pins-mise-"));
  try {
    const bin = join(data, "installs/python/3.12.16/bin");
    mkdirSync(bin, { recursive: true });
    for (const name of ["python3", "python"]) {
      writeFileSync(join(bin, name), `#!/bin/sh\ncase "$1" in --version) echo Python 3.12.16;; *) echo pinned-python;; esac\n`, { mode: 0o755 });
    }
    setConfig(dir, (c) => {
      c.areas[0].steps = [{ id: "sees", dir: ".", cmd: ["sh", "-c", 'echo "$(python3 -m venv v) $SPIRE_PINNED" >> "$RUNS"'] }];
    });
    writeFileSync(join(dir, ".tool-versions"), "python 3.12.16\n");
    const r = checksIn(dir, { MISE_DATA_DIR: data });
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.deepEqual(runsOf(dir), ["pinned-python python@3.12.16"]);
    assert.match(r.stderr, /spire-land: pins: python 3\.12\.16 \(pin 3\.12\.16 in \.tool-versions\)/);
    cleanupRuns(dir);
    writeFileSync(join(dir, ".tool-versions"), "python 3.12.17\n");
    const no = checksIn(dir, { MISE_DATA_DIR: data });
    assert.equal(no.status, 1, no.stdout + no.stderr);
    assert.match(no.stderr, /python: the project pins 3\.12\.17 \(\.tool-versions\) and this host has no such version\..*Install it: mise install python@3\.12\.17\./);
    assert.deepEqual(runsOf(dir), []);
  } finally {
    rmSync(data, { recursive: true, force: true });
    cleanup(dir);
  }
});
const cleanupRuns = (dir) => rmSync(join(dir, "..", `${dir.split("/").pop()}.runs`), { force: true });

// A test group's parallel workers are capped by the host's cores AND its free memory at the project's budget per worker (checks.workerBudgetMB); one line reports it. They would fail if the
// budget did not move the count, if the count could reach 0, if a bad budget were ignored, or if the run printed nothing of what it measured.
test("a test group starts min(core share, free memory / budget) workers, never fewer than 1, and reports it; a bad budget stops the run", () => {
  const dir = foreign();
  try {
    setConfig(dir, (c) => {
      c.areas[0].steps = [{ id: "tests:ios", dir: ".", list: ["git", "ls-files", "--", "ios/*Tests.mjs"], cmd: nodeTest }];
    });
    writeFileSync(join(dir, "ios/aTests.mjs"), 'import test from "node:test";\ntest("fine", async () => { await new Promise((r) => setTimeout(r, 1200)); });\n');
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "a test");
    const budget = (workerBudgetMB) =>
      setConfig(dir, (c) => {
        c.checks = { ...c.checks, workerBudgetMB };
      });
    const line = (r) => /^area ios: (\d+) workers \(([^)]*)\); (.*)$/m.exec(r.stderr);
    const peak = (r) => /^area ios: (peak .*)$/m.exec(r.stderr)?.[1];
    budget({ ios: 1e12 }); // a budget no host can pay: one worker, not none
    const huge = checksIn(dir);
    assert.equal(huge.status, 0, huge.stdout + huge.stderr);
    assert.equal(line(huge)?.[1], "1", huge.stderr);
    budget({ ios: 1 }); // a budget that memory never binds: the cores do
    const small = checksIn(dir);
    assert.equal(small.status, 0, small.stdout + small.stderr);
    assert.equal(Number(line(small)?.[1]), Math.max(2, Math.floor(availableParallelism() / 2)), small.stderr);
    assert.match(peak(small), process.platform === "linux" ? /^peak measured [\d.]+ (MB|GB)\/worker \([\d.]+ (MB|GB) at the most, for \d+ workers\)$/ : /^peak/);
    assert.match(line(small)[3], /^(MemoryMax [\d.]+ (MB|GB)|no MemoryMax here \(worker count only\)); read (host|cgroup v[12])$/, "it says which ceiling holds");
    setConfig(dir, (c) => {
      delete c.checks.testConcurrencyFlag; // nothing to cap with: the line says so instead of vanishing
    });
    const free = checksIn(dir);
    assert.equal(free.status, 0, free.stdout + free.stderr);
    assert.match(free.stderr, /^area ios: workers not capped \(no checks\.testConcurrencyFlag/m);
    budget({ ios: 0 });
    const bad = checksIn(dir);
    assert.equal(bad.status, 13, bad.stdout + bad.stderr);
    assert.match(bad.stderr, /workerBudgetMB\.ios must be a positive number/);
  } finally {
    cleanup(dir);
  }
});

// The landing re-enters a systemd user scope with a MemoryMax once, and the run inside it (marked by an argument no environment can carry) does not enter another. A stub `systemd-run` on PATH
// stands in for the host's: it answers the probe with the limit it was given, records every call, and runs the command. They would fail if the re-exec never happened, if it ran away (a scope in a
// scope), or if the limit asked for were not the host-share one.
test("the run re-enters one systemd scope with a MemoryMax, and the run inside it does not enter another", () => {
  const dir = foreign();
  const stub = mkdtempSync(join(tmpdir(), "stub-systemd-"));
  try {
    writeFileSync(
      join(stub, "systemd-run"),
      `#!/bin/sh
echo "$*" >> "$STUB_LOG"
limit=
while [ "$1" != "--" ]; do case "$1" in MemoryMax=*) limit=\${1#MemoryMax=};; esac; shift; done
shift
if [ "$1" = sh ]; then echo "$limit"; exit 0; fi
exec "$@"
`,
    );
    chmodSync(join(stub, "systemd-run"), 0o755);
    setConfig(dir, (c) => {
      c.areas[0].steps = [{ id: "tests:ios", dir: ".", list: ["git", "ls-files", "--", "ios/*Tests.mjs"], cmd: nodeTest }];
    });
    writeFileSync(join(dir, "ios/aTests.mjs"), 'import test from "node:test";\ntest("fine", () => {});\n');
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "a test");
    const log = join(stub, "calls.log");
    const r = checksIn(dir, { PATH: `${stub}:${env.PATH}`, STUB_LOG: log });
    assert.equal(r.status, 0, r.stdout + r.stderr);
    const calls = readFileSync(log, "utf8").trim().split("\n");
    const scoped = calls.filter((c) => c.includes("--in-memory-scope"));
    assert.equal(scoped.length, 1, `one scope for the landing, none inside it: ${calls.join(" | ")}`);
    assert.match(scoped[0], /--user --scope .*-p MemoryMax=\d+ -p MemorySwapMax=0 -- .*run-steps\.mjs \S+ --in-memory-scope$/);
    assert.equal(calls.length, 2, "the probe and the one scope");
    assert.match(calls[0], /MemoryMax=(\d+)/);
    assert.equal(calls[0].match(/MemoryMax=(\d+)/)[1], scoped[0].match(/MemoryMax=(\d+)/)[1], "the probe asked for the limit the scope got");
    assert.match(r.stdout, /tests:ios/, "the run inside the scope finished the checks");
  } finally {
    cleanup(dir);
    rmSync(stub, { recursive: true, force: true });
  }
});

// Admission (carry.mjs, run-steps.mjs): the coverage map is read ONLY from the git ref of origin that checks.coverageMap names (fetched, never a local file) and only when its commit is an ancestor of the base. With such a map a first run runs
// the test files that reach the change; a map whose commit is not an ancestor (a forged one) runs the whole area, and so does a configuration with no ref even when a map file lies in the state dir.
// They would fail if the map were read from a file, if a map not built at an ancestor were used, or if a mapped first run ran the whole area.
test("a first run reads the coverage map from origin's ref: only the tests that reach the change run; a map built at a commit that is not an ancestor of the base, or a local file, never narrows", () => {
  const dir = foreign();
  const origin = mkdtempSync(join(tmpdir(), "covmap-origin-"));
  try {
    const testStep = { id: "tests:ios", dir: ".", list: ["git", "ls-files", "--", "ios/*Tests.mjs"], cmd: nodeTest };
    setConfig(dir, (c) => {
      c.areas[0].steps = [testStep];
      c.checks.coverageMap = { ref: "coverage-map", maxAgeHours: 24 };
    });
    const body = (id) => `import { appendFileSync } from "node:fs";\nimport test from "node:test";\ntest("t", () => appendFileSync(process.env.RUNS, "${id}\\n"));\n`;
    git(dir, "checkout", "-q", "main");
    for (const id of ["a", "b", "z"]) writeFileSync(join(dir, `ios/${id}Tests.mjs`), body(id));
    writeFileSync(join(dir, "ios/lib.mjs"), "export const x = 1;\n");
    writeFileSync(join(dir, "ios/other.mjs"), "export const y = 1;\n");
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "main: three tests and two sources");
    git(dir, "checkout", "-q", "work");
    git(dir, "reset", "-q", "--hard", "main"); // the world's own change (App.swift) is another change
    writeFileSync(join(dir, "ios/lib.mjs"), "export const x = 2;\n\n\n\n\n\nexport const y = 1;\n");
    git(dir, "commit", "-qam", "change lib");
    git(origin, "init", "-q", "--bare");
    git(dir, "remote", "add", "origin", origin);
    const publish = (commit, built) => {
      const map = { meta: { commit, built }, files: ["ios/lib.mjs", "ios/other.mjs", "ios/App.swift"], tests: { "ios/aTests.mjs": [0, 2], "ios/bTests.mjs": [1], "ios/zTests.mjs": [1] } };
      const file = join(dir, "..", `${dir.split("/").pop()}.map.json`);
      writeFileSync(file, JSON.stringify(map));
      const h = git(dir, "hash-object", "-w", file).trim();
      const tree = execFileSync("git", ["mktree"], { cwd: dir, env, encoding: "utf8", input: `100644 blob ${h}\tcoverage-map.json\n` }).trim();
      const c = git(dir, "commit-tree", tree, "-m", "map").trim();
      git(dir, "push", "-q", "-f", "origin", `${c}:refs/heads/coverage-map`);
      rmSync(file, { force: true });
    };
    const fresh = () => rmSync(join(dir, "..", `${dir.split("/").pop()}.runs`), { force: true });
    publish(git(dir, "rev-parse", "main").trim(), new Date(Date.now() - 600000).toISOString());
    const ok = checksIn(dir);
    assert.equal(ok.status, 0, ok.stdout + ok.stderr);
    assert.deepEqual(runsOf(dir), ["a"], ok.stderr); // b and z do not reach ios/lib.mjs
    assert.match(ok.stderr, /land: coverage map age \d+m \(commit [0-9a-f]+\)/);
    assert.match(ok.stderr, /1 of 3 test file\(s\) reach the change/);
    // a map of a commit that is not in the base's history is refused: the whole area runs
    fresh();
    publish("0".repeat(40), new Date().toISOString());
    const forged = checksIn(dir);
    assert.equal(forged.status, 0, forged.stdout + forged.stderr);
    assert.deepEqual(runsOf(dir).sort(), ["a", "b", "z"]);
    assert.match(forged.stderr, /not an ancestor of the base/);
  } finally {
    rmSync(origin, { recursive: true, force: true });
    cleanup(dir);
  }
});

test("a coverage map file in the state dir is never read: with no checks.coverageMap.ref the whole area runs and the line names the map", () => {
  const dir = foreign();
  try {
    const testStep = { id: "tests:ios", dir: ".", list: ["git", "ls-files", "--", "ios/*Tests.mjs"], cmd: nodeTest };
    setConfig(dir, (c) => {
      c.areas[0].steps = [testStep];
    });
    const body = (id) => `import { appendFileSync } from "node:fs";\nimport test from "node:test";\ntest("t", () => appendFileSync(process.env.RUNS, "${id}\\n"));\n`;
    git(dir, "checkout", "-q", "main");
    for (const id of ["a", "b"]) writeFileSync(join(dir, `ios/${id}Tests.mjs`), body(id));
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "tests");
    git(dir, "checkout", "-q", "work");
    git(dir, "rebase", "-q", "main");
    const state = join(dir, ".git/spire-land");
    mkdirSync(state, { recursive: true });
    writeFileSync(join(state, "coverage-map.json"), JSON.stringify({ meta: { built: Date.now(), commit: git(dir, "rev-parse", "main").trim() }, tests: { "ios/aTests.mjs": ["ios/App.swift"] } }));
    const r = checksIn(dir);
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.deepEqual(runsOf(dir).sort(), ["a", "b"]);
    assert.match(r.stderr, /no coverage map \(checks\.coverageMap\.ref is not set\)/);
  } finally {
    cleanup(dir);
  }
});

test("a first run by reach: only the tests a change reaches run, a change that reaches none runs none, the step's own runner inputs run it whole, and the line says what ran and why", () => {
  const dir = foreign();
  try {
    const nodeTestWith = [...nodeTest];
    // the step's command preloads ios/preload.mjs under every test: a change to it is a change to every test
    const step = (area, glob) => ({ id: `tests:${area}`, dir: ".", select: "reach", list: ["git", "ls-files", "--", glob], cmd: [...nodeTestWith.slice(0, 1), "--import", "./ios/preload.mjs", ...nodeTestWith.slice(1)] });
    setConfig(dir, (c) => {
      c.areas[0].steps = [step("ios", "ios/*Tests.mjs")];
      c.areas[1].steps = [{ ...step("android", "android/*Tests.mjs"), cmd: nodeTest }];
    });
    const body = (id, imp) => `import { appendFileSync } from "node:fs";\n${imp}import test from "node:test";\ntest("t", () => appendFileSync(process.env.RUNS, "${id}\\n"));\n`;
    git(dir, "checkout", "-q", "main");
    writeFileSync(join(dir, "ios/preload.mjs"), "export const p = 1;\n");
    writeFileSync(join(dir, "ios/lib.mjs"), "export const x = 1;\n");
    writeFileSync(join(dir, "ios/lonely.mjs"), "export const l = 1;\n");
    writeFileSync(join(dir, "ios/aTests.mjs"), body("a", 'import "./lib.mjs";\n'));
    writeFileSync(join(dir, "ios/bTests.mjs"), body("b", ""));
    writeFileSync(join(dir, "ios/cTests.mjs"), body("c", ""));
    writeFileSync(join(dir, "android/dTests.mjs"), body("d", ""));
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "main: tests and sources");
    git(dir, "checkout", "-q", "work");
    git(dir, "rebase", "-q", "main");
    const edit = (file, text, msg) => {
      git(dir, "reset", "-q", "--hard", "main");
      writeFileSync(join(dir, file), text);
      git(dir, "add", "-A");
      git(dir, "commit", "-qm", msg);
    };
    const fresh = () => rmSync(join(dir, "..", `${dir.split("/").pop()}.runs`), { force: true });
    const check = (msg) => {
      fresh();
      const r = checksIn(dir);
      assert.equal(r.status, 0, `${msg}\n${r.stdout}${r.stderr}`);
      return r;
    };
    // a source file one test imports: that test runs, the others do not; the line says N of M and why
    edit("ios/lib.mjs", "export const x = 2;\n", "change lib");
    let r = check("lib");
    assert.deepEqual(runsOf(dir), ["a"], r.stderr);
    assert.match(r.stderr, /land:   tests:ios\(1 of 3 by reach\):\ds: selected 1 of 3 test file\(s\): 1 changed file\(s\)/);
    assert.match(r.stderr, /land:   areas touched: ios \(1 changed path\(s\)\)/);
    assert.match(r.stderr, /no step ran for the other areas \(no changed path of theirs\): android, docs/);
    // a source file nothing imports: no test of the step runs (the runner is never started on an empty list), and the line says so
    edit("ios/lonely.mjs", "export const l = 2;\n", "change lonely");
    r = check("lonely");
    assert.deepEqual(runsOf(dir), []);
    assert.match(r.stderr, /tests:ios\(0 of 3 by reach\):none: selected 0 of 3 test file\(s\)/);
    // a test file nothing imports selects itself
    edit("ios/cTests.mjs", body("c", "") + "// edited\n", "change c");
    check("c");
    assert.deepEqual(runsOf(dir), ["c"]);
    // a change in another area runs that area's tests and none of ios's
    edit("android/dTests.mjs", body("d", "") + "// edited\n", "change d");
    r = check("android");
    assert.deepEqual(runsOf(dir), ["d"]);
    assert.doesNotMatch(r.stderr, /tests:ios/);
    // the file the step preloads under every test: the whole step
    edit("ios/preload.mjs", "export const p = 2;\n", "change preload");
    r = check("preload");
    assert.deepEqual(runsOf(dir).sort(), ["a", "b", "c"]);
    assert.match(r.stderr, /is what the step runs under .*: the whole step runs/);
    // a green selected step is kept on the same tree (its marker holds the list it ran), and a rebase is a new first run
    edit("ios/lib.mjs", "export const x = 3;\n", "change lib again");
    check("again");
    r = checksIn(dir);
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stderr, /tests:ios\(1 of 3 by reach\):kept/);
  } finally {
    cleanup(dir);
  }
});

// Admission: a step that passed on a tree is kept for that tree wherever the tree is checked: the push gate runs the same tree in a scratch worktree of its own, at a path of its own, and a step whose command names
// `{root}` (the venue's test groups run `{root}/scripts/ci/group-runner.mjs`) must not run again for the path. The key of the marker is the tree and the step's command with the project root as `{root}`, never as a path.
// It would fail if the root's path were part of the key: every test group of the venue ran again in every push gate, though main had not moved. Oracle: a real clone, a real second worktree of it, the real checks.sh in both.
test("a step that passed on a tree is kept in another worktree of the same clone: the project root's path is no part of its key", () => {
  const dir = foreign();
  const other = `${dir}-wt`;
  try {
    setConfig(dir, (c) => {
      c.areas[0].steps = [{ id: "rooted", dir: ".", cmd: ["node", "{root}/ios/rooted.mjs"] }]; // a command that names the root, as the venue's groups do
    });
    git(dir, "checkout", "-q", "main");
    writeFileSync(join(dir, "ios/rooted.mjs"), 'import { appendFileSync } from "node:fs";\nappendFileSync(process.env.RUNS, "rooted\\n");\n');
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "main: the step");
    git(dir, "checkout", "-q", "work");
    git(dir, "rebase", "-q", "main");
    const first = checksIn(dir);
    assert.equal(first.status, 0, first.stdout + first.stderr);
    assert.deepEqual(runsOf(dir), ["rooted"]);
    git(dir, "worktree", "add", "-q", "-f", "--detach", other, "HEAD");
    const again = spawnSync(join(other, ".spire/tool/checks.sh"), ["main"], { cwd: other, env: { ...env, RUNS: join(dir, "..", `${dir.split("/").pop()}.runs`) }, encoding: "utf8" });
    assert.equal(again.status, 0, again.stdout + again.stderr);
    assert.deepEqual(runsOf(dir), ["rooted"], "the same tree in another worktree ran the step again");
    assert.match(again.stdout, /rooted:kept/);
  } finally {
    git(dir, "worktree", "remove", "-f", other);
    cleanup(dir);
  }
});

// Admission: a step marked `select: "reach"` is carried over a rebase too (carry.mjs): of the tests the change reaches on the rebased tree, the ones that passed before and that nothing incoming reaches are carried, the others run.
// It would fail if a rebase ran the selection again whole (the tests that passed run again), if an incoming file's test were carried, if a test that never ran were carried, or if a red incoming file were not caught on the final tree.
// Oracle: real commits, the real checks.sh, real `node --test` files that append their id to a file.
test("a step marked select reach is carried over a rebase: only the selected tests an incoming file reaches, and the ones that never ran, run again; a red there fails the final tree", () => {
  const dir = foreign();
  try {
    setConfig(dir, (c) => {
      c.areas[0].steps = [{ id: "tests:ios", dir: ".", select: "reach", list: ["git", "ls-files", "--", "ios/*Tests.mjs"], cmd: nodeTest }];
    });
    const body = (id, imp) => `import { appendFileSync } from "node:fs";\n${imp}import test from "node:test";\ntest("t", () => appendFileSync(process.env.RUNS, "${id}\\n"));\n`;
    git(dir, "checkout", "-q", "main");
    writeFileSync(join(dir, "ios/lib.mjs"), "export const x = 1;\n\n\n\n\n\nexport const y = 1;\n");
    writeFileSync(join(dir, "ios/dep.mjs"), "export const d = 1;\n");
    writeFileSync(join(dir, "ios/aTests.mjs"), body("a", 'import "./lib.mjs";\nimport "./dep.mjs";\n'));
    writeFileSync(join(dir, "ios/bTests.mjs"), body("b", 'import "./lib.mjs";\n'));
    writeFileSync(join(dir, "ios/cTests.mjs"), body("c", ""));
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "main: tests and sources");
    git(dir, "checkout", "-q", "work");
    git(dir, "reset", "-q", "--hard", "main"); // the world's own change (App.swift) is another change
    writeFileSync(join(dir, "ios/lib.mjs"), "export const x = 2;\n\n\n\n\n\nexport const y = 1;\n");
    git(dir, "commit", "-qam", "change lib");
    const onMain = (file, text, msg) => {
      git(dir, "checkout", "-q", "main");
      writeFileSync(join(dir, file), text);
      git(dir, "add", "-A");
      git(dir, "commit", "-qm", msg);
      git(dir, "checkout", "-q", "work");
      git(dir, "rebase", "-q", "main");
    };
    const check = () => {
      rmSync(join(dir, "..", `${dir.split("/").pop()}.runs`), { force: true });
      return checksIn(dir);
    };
    let r = check();
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.deepEqual(runsOf(dir).sort(), ["a", "b"], r.stderr);
    // main moved in a document nothing reaches: the selection is the same two tests, both passed before: nothing runs
    onMain("docs/a.md", "a\nmore\n", "docs: main moved");
    r = check();
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.deepEqual(runsOf(dir), [], "both selected tests are carried");
    assert.match(r.stderr, /carried tests:ios: .*: 0 of 2 other test file\(s\) the change reaches reach them/);
    assert.match(r.stdout, /ios:carried from /);
    // main moved in a file one of them imports: that test runs, the other is carried
    onMain("ios/dep.mjs", "export const d = 2;\n", "main: dep");
    r = check();
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.deepEqual(runsOf(dir), ["a"]);
    assert.match(r.stdout, /tests:ios\(1 re-run of 2, rest carried from /);
    // main added a test that reaches the change: it never ran, so it runs; the two that passed are carried
    onMain("ios/eTests.mjs", body("e", 'import "./lib.mjs";\n'), "main: a new test");
    r = check();
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.deepEqual(runsOf(dir), ["e"]);
    // a test the change reaches that main added without the step ever having passed it is not carried; a file that makes a selected test red runs it on the final tree: the gate fails
    onMain("ios/dep.mjs", 'throw new Error("red");\n', "main: a dep that breaks its importer");
    r = check();
    assert.equal(r.status, 13, r.stdout + r.stderr);
    assert.match(r.stderr, /FAILED: tests:ios\(1 re-run of 3, rest carried from /, "only the test that imports the incoming file ran (it throws before it can say so), and the gate is red");
    assert.match(r.stderr, /failed files of tests:ios[^\n]*\n\s+ios\/aTests\.mjs/);
    // an overlap is selected again as a first run (the tests that reach the change, never the whole step), not carried
    git(dir, "checkout", "-q", "main");
    writeFileSync(join(dir, "ios/dep.mjs"), "export const d = 3;\n");
    git(dir, "commit", "-qam", "main: dep fixed");
    git(dir, "checkout", "-q", "work");
    git(dir, "rebase", "-q", "main");
    writeFileSync(join(dir, "ios/lib.mjs"), "export const x = 3;\n\n\n\n\n\nexport const y = 1;\n");
    git(dir, "commit", "-qam", "change lib again");
    assert.equal(check().status, 0, "the new change passes its first run");
    onMain("ios/lib.mjs", "export const x = 1;\n\n\n\n\n\nexport const y = 7;\n", "main: lib moved too, elsewhere in the file");
    r = check();
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.deepEqual(runsOf(dir).sort(), ["a", "b", "e"], "an overlap is no carry: the tests that reach the change run");
    assert.match(r.stderr, /re-run tests:ios: ios\/lib\.mjs came in and the change touches it too; selected 3 of 4 test file\(s\)/);
    // main DELETED a file a selected test still imports (the test is not changed under it): the deleted file is an incoming file the test reaches, and the test runs on the final tree and is red
    git(dir, "checkout", "-q", "main");
    git(dir, "rm", "-q", "ios/dep.mjs");
    git(dir, "commit", "-qm", "main: dep deleted");
    git(dir, "checkout", "-q", "work");
    git(dir, "rebase", "-q", "main");
    r = check();
    assert.equal(r.status, 13, r.stdout + r.stderr);
    assert.match(r.stderr, /FAILED: tests:ios\(1 re-run of 3, rest carried from /);
  } finally {
    cleanup(dir);
  }
});

test("the files every test of a step runs under are the ones its command, its list command and its environment's recipe name", async () => {
  const { runnerInputs } = await import("./plan.mjs");
  const tracked = new Set(["scripts/ci/group-runner.mjs", "scripts/ci/postgres-suites.mjs", "scripts/dev/sandbox.mjs", "server/tests/helpers/reset.ts", "scripts/dev/ephemeral-pg.sh", "scripts/pg/bundle.sh", "server/tests/a.test.ts", "scripts/ci/unrelated.mjs", "scripts/ci/shard-plan.json"]);
  const step = {
    dir: "server",
    environment: "postgres",
    files: ["tests/a.test.ts"],
    argv: ["node", "/r/scripts/ci/group-runner.mjs", "--import", "../scripts/dev/sandbox.mjs", "--test", "--group-reset=tests/helpers/reset.ts", "tests/a.test.ts"],
    listKey: ["bash", "-c", "node scripts/ci/postgres-suites.mjs unit | sed 's#^#server/#'"],
    runsUnder: ["scripts/ci/shard-plan*"],
  };
  const environments = { postgres: { wrap: ["{root}/scripts/dev/ephemeral-pg.sh", "--"], bundle: { path: ["{root}/scripts/pg/bundle.sh", "--path"] } }, other: { wrap: ["scripts/ci/unrelated.mjs"] } };
  const got = runnerInputs({ step, environments, tracked, root: "/r" }).sort();
  assert.deepEqual(got, ["scripts/ci/group-runner.mjs", "scripts/ci/postgres-suites.mjs", "scripts/dev/ephemeral-pg.sh", "scripts/dev/sandbox.mjs", "scripts/pg/bundle.sh", "server/tests/helpers/reset.ts", "scripts/ci/shard-plan.json"].sort());
  // the step's own test files are not runner inputs, nor is another environment's recipe
  assert.ok(!got.includes("server/tests/a.test.ts") && !got.includes("scripts/ci/unrelated.mjs"));
});
