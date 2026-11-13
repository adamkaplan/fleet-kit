// Admission: LAYOUT.md's contract holds: a copy of the tool at <project>/.spire/tool/ finds its own folder, .spire/ and the project root from where its files are (never from the current
// directory), reads the project's files from .spire/, and substitutes {tool} and {root} in a step. It would fail if a path were resolved against the cwd, a project path were named in the
// tool, or the project's files were looked for anywhere but .spire/. Oracle: a real copy of the tool in a scratch project, run from a directory that is not the project.
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { cpSync, mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { layoutOf } from "./layout.mjs";

const repo = join(fileURLToPath(import.meta.url), "../..");

/** A project with the tool vendored into .spire/tool/ and its own land.json, in a git repository. */
function project(land) {
  const root = realpathSync(mkdtempSync(join(tmpdir(), "layout-test-")));
  mkdirSync(join(root, ".spire"), { recursive: true });
  cpSync(join(repo, "tool"), join(root, ".spire/tool"), { recursive: true });
  writeFileSync(join(root, ".spire/land.json"), JSON.stringify(land));
  const git = (...a) => execFileSync("git", a, { cwd: root, encoding: "utf8", env: { ...process.env, GIT_AUTHOR_NAME: "t", GIT_AUTHOR_EMAIL: "t@t", GIT_COMMITTER_NAME: "t", GIT_COMMITTER_EMAIL: "t@t" } });
  git("init", "-q", "-b", "main");
  return { root, git };
}
const clean = (extra = {}) => {
  const env = { ...process.env, ...extra };
  for (const k of ["LAND_CONFIG", "BOX_CONFIG"]) if (!(k in extra)) delete env[k];
  return env;
};

test("layoutOf: the tool folder, .spire/, the project root and the project's own files, from the tool's place alone", () => {
  const l = layoutOf("/p/.spire/tool");
  assert.deepEqual(l, {
    tool: "/p/.spire/tool",
    spire: "/p/.spire",
    root: "/p",
    land: "/p/.spire/land.json",
    box: "/p/.spire/box.json",
    principles: "/p/.spire/principles.md",
    review: "/p/.spire/review",
  });
});

test("a copy of the tool resolves its layout from its own place, whatever the current directory", () => {
  const p = project({});
  const away = mkdtempSync(join(tmpdir(), "layout-away-"));
  try {
    const out = execFileSync(
      process.execPath,
      ["-e", `import(${JSON.stringify(join(p.root, ".spire/tool/layout.mjs"))}).then((m) => console.log(JSON.stringify(m.layout)))`],
      { cwd: away, encoding: "utf8" },
    );
    const l = JSON.parse(out);
    assert.equal(l.tool, join(p.root, ".spire/tool"));
    assert.equal(l.spire, join(p.root, ".spire"));
    assert.equal(l.root, p.root);
    assert.equal(l.land, join(p.root, ".spire/land.json"));
  } finally {
    rmSync(p.root, { recursive: true, force: true });
    rmSync(away, { recursive: true, force: true });
  }
});

test("the project's land.json is read from .spire/ (no LAND_CONFIG), and a step's {tool} and {root} are this copy's", async () => {
  const p = project({
    version: 1,
    paths: { tests: [], prose: ["**/*.md"], notProd: [] },
    areas: [{ name: "all", paths: ["**"], steps: [{ id: "echo", dir: ".", cmd: ["echo", "{tool}", "{root}"] }] }],
  });
  const away = mkdtempSync(join(tmpdir(), "layout-away-"));
  try {
    const script = `
      const { loadConfig } = await import(${JSON.stringify(join(p.root, ".spire/tool/config.mjs"))});
      const { planSteps } = await import(${JSON.stringify(join(p.root, ".spire/tool/plan.mjs"))});
      const cfg = await loadConfig();
      const steps = planSteps(cfg, { areas: { names: ["all"], full: false, unmapped: [] }, files: [], paths: [] }, { base: "main" });
      console.log(JSON.stringify({ path: cfg.path, argv: steps.steps[0].argv }));`;
    const r = spawnSync(process.execPath, ["--input-type=module", "-e", script], { cwd: away, encoding: "utf8", env: clean() });
    assert.equal(r.status, 0, r.stderr);
    const got = JSON.parse(r.stdout);
    assert.equal(got.path, join(p.root, ".spire/land.json"));
    assert.deepEqual(got.argv, ["echo", join(p.root, ".spire/tool"), p.root]);
  } finally {
    rmSync(p.root, { recursive: true, force: true });
    rmSync(away, { recursive: true, force: true });
  }
});

test("nothing in the tool names a project path of the old layout or the retired lander", () => {
  const r = spawnSync("grep", ["-rEIl", "scripts/land|land\\.config|box\\.config|land-direct|vbm-land|land\\.conf", join(repo, "tool"), "--exclude=layout.test.mjs"], { encoding: "utf8" });
  assert.equal(r.stdout.trim(), "", `the old names are still in: ${r.stdout}`);
});
