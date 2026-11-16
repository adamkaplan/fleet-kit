// Admission: these tests show how checks.sh runs a change's tests by AREA (areas.mjs, classify.mjs, plan.mjs, run-steps.mjs): EVERY test of each area a change touches, nothing narrower and
// nothing else; a path in two areas runs both; a path in no area runs the full suite; a tree that passed is not run again, but a main that moved after the checks is a NEW tree and the areas run
// again on it (a test group is carried from the older main only where the same change passed it and the rebase brings nothing into the area: see the second-rebase test); a failed test file is red (exit 13): nothing re-runs it. They would fail if a test were
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
  writeFileSync(join(dir, ".spire/land.json"), JSON.stringify(config));
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
  writeFileSync(p, JSON.stringify(cfg));
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

// Measured: 90 of 242 landings in a day ended at the council or a red step and then re-ran every step on the same tree (a rebuttal changes no tree). A step that passed on a tree is kept for that tree only:
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

// Admission: these tests show the cheaper SECOND REBASE. After a rebase onto a moved main the cheap whole-tree steps run again on the new tree, and a test group is carried (not run) only when the same change (its diff's
// patch-id) passed it on an earlier tree and nothing that differs between that tree and this one is in its area's declared inputs, the tool or the project's files; the line says so by area (`ios:carried from TREE`) and the
// tree it names is the one the tests really ran on. There is no switch to carry: the earlier tree is what the runs recorded. They would fail if a group that main's change could affect were carried, if a cheap step were carried,
// if the line said `kept` or nothing for a carried group, if a retry of the carried tree ran the group again, if a carry named the wrong tree, or if another change were given the pass.
// Oracle: real commits, the real checks.sh and real `node --test` files that append their id to a file.
test("a rebase that brings nothing into an area's inputs carries its tests; the cheap checks rerun; one that does, or the tool, or the project's file, or a changed diff, reruns them", () => {
  const dir = foreign();
  try {
    const testStep = (area) => ({ id: `tests:${area}`, dir: ".", list: ["git", "ls-files", "--", `${area}/*Tests.mjs`], cmd: nodeTest });
    setConfig(dir, (c) => {
      c.areas[0].steps = [c.areas[0].steps[0], testStep("ios")]; // swiftlint (cheap, whole tree), then the area's tests
      c.areas[1].steps = [testStep("android")];
    });
    const body = (id) => `import { appendFileSync } from "node:fs";\nimport test from "node:test";\ntest("t", () => appendFileSync(process.env.RUNS, "${id}\\n"));\n`;
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
    assert.deepEqual(runsOf(dir), ["swiftlint"]);
    assert.match(a.stdout, new RegExp(`ios:carried from ${t0.slice(0, 12)}`));
    assert.match(a.stdout, new RegExp(`android:carried from ${t0.slice(0, 12)}`));
    assert.match(a.stdout, /tests=2 /); // the count is the pass that is carried
    assert.match(a.stdout, new RegExp(`tree=${treeOf().slice(0, 12)}`)); // Land-Checks names the tree that lands
    assert.doesNotMatch(a.stdout, /tests:ios\(1\):\ds/);
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
    assert.deepEqual(runsOf(dir), ["swiftlint"]);
    assert.match(chain.stdout, new RegExp(`ios:carried from ${t0.slice(0, 12)}`));

    // main moved in a file of ios: its tests run again, android's are carried
    onMain("ios/Other.swift", "let c = 1\n", "ios: main moved in the area");
    fresh();
    const b = checksIn(dir);
    assert.equal(b.status, 0, b.stdout + b.stderr);
    assert.deepEqual(runsOf(dir).sort(), ["swiftlint", "tests:ios"]);
    assert.match(b.stdout, /android:carried from /);
    assert.doesNotMatch(b.stdout, /ios:carried/);

    // the project's file moved (what an area runs is in it): nothing is carried
    setConfig(dir, (c) => (c.checks.note = "moved"));
    fresh();
    const cfg = checksIn(dir);
    assert.equal(cfg.status, 0, cfg.stdout + cfg.stderr);
    assert.deepEqual(runsOf(dir).sort(), both);
    assert.doesNotMatch(cfg.stdout, /carried/);

    // the tool moved: nothing is carried either
    onMain(".spire/tool/note.txt", "a newer tool\n", "the tool moved");
    fresh();
    const tool = checksIn(dir);
    assert.deepEqual(runsOf(dir).sort(), both);
    assert.doesNotMatch(tool.stdout, /carried/);

    // the rebase changed the change's own file in an area (a resolution that differs only by white space inside a line, which a patch-id does not see): that area's tests run, the other's are carried
    writeFileSync(join(dir, "ios/aTests.mjs"), body("tests:ios").replace("appendFileSync(process", "appendFileSync( process"));
    git(dir, "commit", "-qa", "--amend", "-m", "tests in both areas");
    onMain("docs/c.md", "c\n", "docs: main moved a third time");
    fresh();
    const blank = checksIn(dir);
    assert.equal(blank.status, 0, blank.stdout + blank.stderr);
    assert.deepEqual(runsOf(dir).sort(), both.filter((x) => x !== "tests:android"));

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

test("a variable the project withholds (checks.withhold) never reaches a step that names no environment, in a box or not; one that does gets it", () => {
  const dir = foreign({
    environments: {
      db: { wrap: ["env"], bundle: { have: ["true"], path: ["echo", "unused"] }, ifMissing: "fail" },
    },
  });
  try {
    setConfig(dir, (c) => {
      c.checks = { ...(c.checks ?? {}), withhold: ["PRIVATE_THING"] };
      c.areas[0].steps.push(
        { id: "plain", dir: ".", cmd: node("if (process.env.PRIVATE_THING) process.exit(1)") },
        { id: "withenv", dir: ".", environment: "db", cmd: node("if (!process.env.PRIVATE_THING) process.exit(1)") },
      );
    });
    const r = checksIn(dir, { PRIVATE_THING: "secret" });
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /plain:\ds/);
    assert.match(r.stdout, /withenv:\ds/);
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
