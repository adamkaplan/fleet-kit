// Admission: this file shows which test AREAS a change touches (areas.mjs) and that Spire's own file (.spire/land.json) maps the repository's paths to the areas CI's jobs group:
// a path in two areas touches both, a path in no area touches the closest enclosing area, a path nowhere is UNMAPPED and touches every area (the full suite), a test an agent adds is in its area
// by path. It would fail if an area were skipped for a path it owns, if an unmapped path were silently dropped, if a document under server/ ran the server's tests, or if the project's file lost
// an area (a path then falls to the full suite, and the table below says which area it should have been). Oracle: the real project file; no mocks.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { staticDir, stepsOf, touchedAreas } from "./areas.mjs";
import { loadConfig } from "./config.mjs";

const here = fileURLToPath(new URL(".", import.meta.url));
// the real project file: the example of spire-venue, read with the fixture project root (where its readByChecks module is)
const example = join(here, "../examples/spire-venue/land.json");
const loadReal = () => loadConfig(example, join(here, "fixtures/project"));

const cfg = {
  areas: [
    { name: "docs", paths: ["docs/**", "**/*.md"] },
    { name: "app", paths: ["app/**", "shared/**"], exclude: ["app/**/*.md"] },
    { name: "server", paths: ["server/**", "shared/**"], exclude: ["server/**/*.md"] },
    { name: "db", paths: ["server/db/**", "server/src/platform/**"] },
  ],
};

test("an area's static directory is where its glob starts", () => {
  assert.equal(staticDir("app/src/**"), "app/src");
  assert.equal(staticDir("app/**/*.ts"), "app");
  assert.equal(staticDir("**/*.md"), "");
  assert.equal(staticDir("package.json"), "");
  assert.equal(staticDir("server/db/001.sql"), "server/db");
});

test("a path touches every area that owns it, in the project's order", () => {
  const r = touchedAreas(cfg, ["shared/x.mts", "docs/a.md"]);
  assert.deepEqual(r.names, ["docs", "app", "server"]);
  assert.equal(r.full, false);
  assert.deepEqual(r.byArea.app, ["shared/x.mts"]);
  assert.deepEqual(touchedAreas(cfg, ["server/db/1.sql"]).names, ["server", "db"]);
});

test("an area's `exclude` keeps a document out of the code's tests, and the document's own area takes it", () => {
  assert.deepEqual(touchedAreas(cfg, ["server/README.md"]).names, ["docs"]);
  assert.deepEqual(touchedAreas(cfg, ["app/src/notes.md"]).names, ["docs"]);
});

test("a path no glob owns runs the closest enclosing area; the areas that tie all run", () => {
  const c = { areas: [{ name: "app", paths: ["app/src/**"] }, { name: "app-browser", paths: ["app/src/**", "app/browser-tests/**"] }, { name: "server", paths: ["server/src/**"] }] };
  assert.deepEqual(touchedAreas(c, ["app/src/deep/x.cfg"]).names, ["app", "app-browser"]);
  const loose = touchedAreas({ areas: [{ name: "a", paths: ["app/**/*.ts"] }, { name: "b", paths: ["app/src/**"] }] }, ["app/tsconfig.json"]);
  assert.deepEqual(loose.names, ["a"]); // only a's glob starts in app/; b starts deeper and does not enclose it
  assert.equal(loose.full, false);
});

test("a path with no enclosing area is unmapped: every area runs, and the path is named", () => {
  const r = touchedAreas(cfg, ["docs/a.md", ".node-version"]);
  assert.equal(r.full, true);
  assert.deepEqual(r.unmapped, [".node-version"]);
  assert.deepEqual(r.names, ["docs", "app", "server", "db"]);
  assert.deepEqual(touchedAreas(cfg, ["brand-new-dir/x.ts"]).names, ["docs", "app", "server", "db"]);
});

test("Spire's own file: each path of the repository belongs to the area CI groups it in", async () => {
  const real = await loadReal();
  const names = (...p) => touchedAreas(real, p).names;
  assert.deepEqual(names("docs/venue.md"), ["docs"]);
  assert.deepEqual(names("AGENTS.md"), ["docs"]);
  assert.deepEqual(names("server/DEVELOPMENT.md"), ["docs"]); // a document under server/ is the docs area's, not the server's
  assert.deepEqual(names("app/src/chat/chat.tsx"), ["app"]);
  assert.deepEqual(names("app/src/chat/new-thing.test.ts"), ["app"]); // a test an agent adds is in its area by path
  assert.deepEqual(names("server/src/chats/store.ts"), ["server"]);
  assert.deepEqual(names("server/tests/new-thing.test.ts"), ["server"]);
  assert.deepEqual(names("server/db/200-x.sql"), ["server", "server-postgres"]);
  assert.deepEqual(names("server/src/platform/db.ts"), ["server", "server-postgres"]);
  assert.deepEqual(names("server/tests/queue-postgres.test.ts"), ["server", "server-postgres"]);
  assert.deepEqual(names("server/tests/design-system.test.ts"), ["app", "server"]);
  assert.deepEqual(names("shared/chat-send.mts"), ["app", "server"]);
  assert.deepEqual(names(".spire/tool/land"), ["scripts"]);
  assert.deepEqual(names("scripts/dev/fast"), ["scripts"]);
  assert.deepEqual(names("scripts/ci/ui-scope.mjs"), ["scripts"]);
  assert.deepEqual(names("ear/app.py"), ["ear"]);
  assert.deepEqual(names("evals/demo/scenario.json"), ["demo"]);
  assert.deepEqual(names("tests/demo/coverage.js"), ["demo"]);
  assert.deepEqual(names("Dockerfile"), ["infra"]);
  assert.deepEqual(names(".github/workflows/ci.yml"), ["infra"]);
  assert.deepEqual(names("docker/postgres/Dockerfile"), ["server-postgres", "infra"]);
  assert.deepEqual(names("package.json"), ["server"]);
  assert.deepEqual(names("vendor/spire-core/index.ts"), ["server"]);
  // nothing owns these: the full suite, never a skip
  for (const p of [".node-version", ".nvmrc", "brand-new-dir/x.ts"]) {
    const r = touchedAreas(real, [p]);
    assert.equal(r.full, true, p);
    assert.deepEqual(r.names, real.areas.map((a) => a.name), p);
  }
});

test("every area of Spire's file runs something, and every test step lists its files", async () => {
  const real = await loadReal();
  for (const a of real.areas) {
    assert.ok(a.steps.length > 0, `area ${a.name} has no step: a change to it would check nothing`);
    for (const s of a.steps.filter((x) => x.list)) {
      assert.ok(s.cmd.includes("--test"), `${a.name}/${s.id}: a listed step runs the node test runner`);
    }
  }
  const ids = real.areas.flatMap((a) => a.steps.map((s) => s.id));
  for (const gone of ["tests:root", "impact", "select"]) {
    assert.ok(!ids.includes(gone), `${gone} is part of the machinery the areas replaced`);
  }
});

// --- the project's areas in .spire/areas.json (S2): one declaration of what each area is, the paths and tests it owns, its kind; the steps are the recipe of its kind (land.json `recipes`) ---
// The fixture is spire-venue's own areas.json and the recipes of its land.json; replay-paths.txt is every path the last 100 landings of that repository touched (deleted ones too).
const proj = join(here, "../examples/spire-venue-areas");
const readJson = (f) => JSON.parse(readFileSync(join(proj, f), "utf8"));
const replayPaths = readFileSync(join(proj, "replay-paths.txt"), "utf8").split("\n").filter(Boolean);
const loadAreas = () => loadConfig(join(proj, "land.json"), join(here, "fixtures/project"));

test("areas.json is the third file of the view: its areas replace land.json's, with exclude and harnesses; land.json keeps its own fields", async () => {
  const cfg = await loadAreas();
  assert.equal(cfg.areas.length, readJson("areas.json").areas.length);
  assert.ok(cfg.areas.length > 40);
  assert.deepEqual(cfg.exclude, ["**/*.md"]);
  assert.deepEqual(cfg.harnesses.always, ["a11y-keyboard", "a11y-focus"]);
  assert.ok(cfg.recipes.server.length > 0);
  assert.ok(cfg.paths.tests.length > 0);
});

test("every path of the last 100 landings maps to an area of areas.json: none is unmapped, none runs the full suite", async () => {
  const cfg = await loadAreas();
  assert.ok(replayPaths.length > 1000);
  const r = touchedAreas(cfg, replayPaths);
  assert.deepEqual(r.unmapped, []);
  assert.equal(r.full, false);
});

test("an area owns its `tests` as well as its `paths`; its static directory comes from `paths` only", () => {
  const c = { areas: [{ name: "acp", paths: ["server/src/acp/**"], tests: ["server/tests/acp/**"] }, { name: "chat", paths: ["server/src/chat/**"], tests: ["server/tests/chat/**"] }] };
  assert.deepEqual(touchedAreas(c, ["server/tests/acp/a.test.ts"]).names, ["acp"]);
  assert.deepEqual(touchedAreas(c, ["server/src/chat/x.ts", "server/tests/acp/a.test.ts"]).names, ["acp", "chat"]);
  // a path no glob owns finds its area by the directories of `paths`: `server/tests/acp` is a test directory, not an enclosing one
  const r = touchedAreas(c, ["server/tests/acp/fixtures/x.json"]);
  assert.deepEqual(r.names, ["acp"]); // owned by the tests glob
  const loose = touchedAreas(c, ["server/src/acp/deep/x.cfg"]);
  assert.deepEqual(loose.names, ["acp"]);
  const unmapped = touchedAreas(c, ["server/tests/helpers/x.ts"]);
  assert.equal(unmapped.full, true); // `tests` are never an enclosing directory
});

test("the top-level `exclude` keeps prose out of every area but docs", () => {
  const c = {
    exclude: ["**/*.md"],
    areas: [
      { name: "docs", kind: "docs", paths: ["docs/**", "**/*.md"] },
      { name: "acp", paths: ["server/src/acp/**"], tests: ["server/tests/acp/**"] },
    ],
  };
  assert.deepEqual(touchedAreas(c, ["server/src/acp/README.md"]).names, ["docs"]);
  assert.deepEqual(touchedAreas(c, ["server/tests/acp/notes.md", "server/src/acp/a.ts"]).names, ["docs", "acp"]);
  assert.deepEqual(touchedAreas(c, ["docs/a.md"]).names, ["docs"]);
});

test("a recipe expands for an area: {name} in the words, {tests} once per test glob, the area's own steps after the kind's", () => {
  const recipes = {
    server: [
      { id: "tests:{name}", dir: "server", list: ["git", "ls-files", "--", ":(glob){tests}"], cmd: ["node", "--test"], note: "tests of {name}" },
      { id: "arch", cmd: ["npm", "run", "arch"] },
      { id: "pg", list: ["bash", "-c", 'git ls-files -- "$@"', "bash", "{tests}"], env: { AREA: "{name}" } },
    ],
  };
  const area = { name: "acp", kind: "server", tests: ["server/tests/acp/**", "server/tests/acp-*.test.ts"], steps: [{ id: "extra", cmd: ["true"] }] };
  const steps = stepsOf(area, recipes);
  assert.deepEqual(steps.map((s) => s.id), ["tests:acp", "arch", "pg", "extra"]);
  assert.deepEqual(steps[0].list, ["git", "ls-files", "--", ":(glob)server/tests/acp/**", ":(glob)server/tests/acp-*.test.ts"]);
  assert.equal(steps[0].note, "tests of acp");
  assert.deepEqual(steps[2].list, ["bash", "-c", 'git ls-files -- "$@"', "bash", "server/tests/acp/**", "server/tests/acp-*.test.ts"]);
  assert.deepEqual(steps[2].env, { AREA: "acp" });
  assert.equal(recipes.server[0].id, "tests:{name}", "the recipe itself is never changed");
  // an area of the old form (steps, no kind) is its own steps
  assert.deepEqual(stepsOf({ name: "x", steps: [{ id: "s" }] }, recipes).map((s) => s.id), ["s"]);
  // a fault in the project's files is an error, never a silent skip
  assert.throws(() => stepsOf({ name: "x", kind: "nope" }, recipes), /area x.*kind nope/);
  assert.throws(() => stepsOf({ name: "x", kind: "server" }, recipes), /area x.*tests/);
  assert.throws(() => stepsOf({ name: "x" }, recipes), /area x.*no step/);
});

test("the real recipes expand for every area of areas.json, and the steps of two areas share the ids that are not about the area", async () => {
  const cfg = await loadAreas();
  const ids = (name) => stepsOf(cfg.areas.find((a) => a.name === name), cfg.recipes).map((s) => s.id);
  assert.ok(ids("workroom").includes("tests:workroom"));
  assert.ok(ids("acp").includes("tests:acp") && ids("acp").includes("tests:acp-pg") && ids("acp").includes("tests:guards"));
  for (const a of cfg.areas) assert.ok(stepsOf(a, cfg.recipes).length > 0, a.name);
  const workroom = stepsOf(cfg.areas.find((a) => a.name === "workroom"), cfg.recipes);
  assert.deepEqual(workroom.find((s) => s.id === "tests:workroom").list.slice(-1), ["server/tests/workroom/**"]);
});
