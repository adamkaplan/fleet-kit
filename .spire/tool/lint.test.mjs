// Admission: the lint defaults (defaults/lint/<language>/) reach the project's landing only by its opt-in, and a lint error stops the checks (exit 13) while a clean tree passes.
// They would fail if the lint step ran without `"lint": [...]` in land.json, if it were skipped with the opt-in, if a missing linter passed silently, or if a lint error or a format error were forgiven.
// Oracle: the real checks.sh on a toy project (fixtures/lint/<language>/) in a real git repository with the project's own copy of the default (made as the installer makes it). The TypeScript proof needs oxlint and
// oxfmt: the test installs the toy's devDependencies (fixtures/lint/typescript/package.json) once into a cache under the temp dir, or uses SPIRE_LINT_TS_MODULES (a node_modules that holds them); it never skips.
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { after } from "node:test";
import { fileURLToPath } from "node:url";

const repo = join(fileURLToPath(import.meta.url), "../..");
const { NODE_TEST_CONTEXT: _c, NODE_OPTIONS: _o, ...inherited } = process.env;
const env = { ...inherited, GIT_AUTHOR_NAME: "t", GIT_AUTHOR_EMAIL: "t@t", GIT_COMMITTER_NAME: "t", GIT_COMMITTER_EMAIL: "t@t" };
const git = (cwd, ...a) => execFileSync("git", a, { cwd, env, encoding: "utf8" });
const scratch = [];
after(() => scratch.forEach((d) => rmSync(d, { recursive: true, force: true })));

/** A toy project of `language` on `main`, with the project's own copy of the default lint set and a change on `work`. `optIn`: its land.json lists the language. */
function toy(language, { optIn, modules }) {
  const dir = mkdtempSync(join(tmpdir(), `lint-${language}-`));
  scratch.push(dir);
  mkdirSync(join(dir, ".spire/lint"), { recursive: true });
  cpSync(join(repo, "tool"), join(dir, ".spire/tool"), { recursive: true, filter: (s) => !s.endsWith(".test.mjs") && !s.includes("/fixtures/") });
  cpSync(join(repo, "tool/defaults/lint", language), join(dir, ".spire/lint", language), { recursive: true });
  const land = { version: 1, paths: { tests: [], prose: ["**/*.md"], notProd: ["**/*.md"] }, sensitive: { globs: [], nameGlobs: [], always: [] }, areas: [{ name: "src", paths: ["src/**"], steps: [{ id: "noop", dir: ".", cmd: ["true"] }] }], environments: {}, ...(optIn ? { lint: [language] } : {}) };
  writeFileSync(join(dir, ".spire/land.json"), JSON.stringify(land));
  cpSync(join(repo, "tool/fixtures/lint", language), dir, { recursive: true });
  writeFileSync(join(dir, ".gitignore"), "node_modules\n");
  if (modules) symlinkSync(modules, join(dir, "node_modules"));
  git(dir, "init", "-q", "-b", "main");
  git(dir, "add", "-A");
  git(dir, "commit", "-qm", "seed");
  git(dir, "checkout", "-q", "-b", "work");
  return dir;
}
const change = (dir, file, text) => {
  writeFileSync(join(dir, file), text);
  git(dir, "add", "-A");
  git(dir, "commit", "-qm", `change ${file}`);
};
const check = (dir) => spawnSync(join(dir, ".spire/tool/checks.sh"), ["main"], { cwd: dir, env, encoding: "utf8" });

test("the default lint set is whole: a declared detect file, an executable one command", () => {
  const d = join(repo, "tool/defaults/lint/typescript");
  assert.deepEqual(JSON.parse(readFileSync(join(d, "lint.json"), "utf8")).detect, ["package.json"]);
  for (const f of ["run", ".oxlintrc.json", ".oxfmtrc.json"]) assert.ok(existsSync(join(d, f)), f);
  assert.equal(statSync(join(d, "run")).mode & 0o111, 0o111, "run is executable");
});

test("without the opt-in in land.json no lint step runs, whatever the project holds", () => {
  const dir = toy("typescript", { optIn: false });
  change(dir, "src/bad.ts", "const unused: number = 1;\nexport const x = 2;\n");
  const r = check(dir);
  assert.equal(r.status, 0, r.stderr);
  assert.doesNotMatch(r.stdout, /lint:/);
});

test("with the opt-in a missing linter is a failure that says how to install it, never a pass", () => {
  const dir = toy("typescript", { optIn: true });
  change(dir, "src/ok2.ts", "export const two = 2;\n");
  const r = check(dir);
  assert.equal(r.status, 13, r.stdout + r.stderr);
  assert.match(r.stderr, /check FAILED: lint:typescript/);
  assert.match(r.stderr, /oxlint is not installed; add it: npm install/);
});

test("a land.json step that carries the id of a lint step is refused, never replaces the gate", () => {
  const dir = toy("typescript", { optIn: true });
  const land = JSON.parse(readFileSync(join(dir, ".spire/land.json"), "utf8"));
  land.areas[0].steps.push({ id: "lint:typescript", dir: ".", cmd: ["true"] });
  writeFileSync(join(dir, ".spire/land.json"), JSON.stringify(land));
  git(dir, "commit", "-qam", "a colliding step");
  change(dir, "src/ok3.ts", "export const three = 3;\n");
  const r = check(dir);
  assert.equal(r.status, 13, r.stdout + r.stderr);
  assert.match(r.stderr, /already has the id lint:typescript/);
});

/** The toy's node_modules: $SPIRE_LINT_TS_MODULES, else installed once (npm, scripts off) beside the temp dir and reused by every later run. */
function tsModules() {
  if (process.env.SPIRE_LINT_TS_MODULES) return process.env.SPIRE_LINT_TS_MODULES;
  const base = join(tmpdir(), "spire-lint-ts-toy");
  if (!existsSync(join(base, "node_modules/.bin/oxlint")) || !existsSync(join(base, "node_modules/.bin/oxfmt"))) {
    mkdirSync(base, { recursive: true });
    cpSync(join(repo, "tool/fixtures/lint/typescript/package.json"), join(base, "package.json"));
    execFileSync("npm", ["install", "--ignore-scripts", "--no-audit", "--no-fund", "--loglevel=error"], { cwd: base, env, encoding: "utf8", timeout: 600000 });
  }
  return join(base, "node_modules");
}
test("TypeScript: a clean tree passes; a lint error and a format error each stop the checks (13)", { timeout: 900000 }, () => {
  const modules = tsModules();
  const dir = toy("typescript", { optIn: true, modules });
  change(dir, "src/two.ts", "export const two = 2;\n");
  const ok = check(dir);
  assert.equal(ok.status, 0, ok.stdout + ok.stderr);
  assert.match(ok.stdout, /lint:typescript:/);
  change(dir, "src/bad.ts", "const unused: number = 1;\nexport const x = 2;\n");
  const lint = check(dir);
  assert.equal(lint.status, 13, lint.stdout + lint.stderr);
  assert.match(lint.stderr, /no-unused-vars/);
  assert.match(lint.stderr, /check FAILED: lint:typescript/);
  change(dir, "src/bad.ts", "export const x   =  2;\n");
  const fmt = check(dir);
  assert.equal(fmt.status, 13, fmt.stdout + fmt.stderr);
  assert.match(fmt.stderr, /src\/bad\.ts/);
  change(dir, "src/bad.ts", "export const x = 2;\n");
  assert.equal(check(dir).status, 0);
});
