// reach: what a file reaches (reach.mjs). The scenarios would fail if an import, a name in a string, a re-export, an alias or a package were not followed, or if a type-only import, a comment or a prose file made a false edge.
import assert from "node:assert/strict";
import test from "node:test";
import { buildGraph, reaching } from "./reach.mjs";

const graphOf = (sources) => buildGraph({ files: Object.keys(sources), read: (p) => sources[p], isProse: (f) => f.endsWith(".md") });
const reach = (sources, candidates, touched) => [...reaching(graphOf(sources), candidates, touched)].sort();

test("an import reaches, transitively; .js is a .ts; an index file; a re-export; a dynamic import and a require", () => {
  const src = {
    "t/a.test.ts": 'import { x } from "../src/x.js";',
    "t/b.test.ts": 'const m = await import("../src/lib");',
    "t/c.test.ts": 'const m = require("../src/y.cjs");',
    "t/d.test.ts": 'export * from "../src/z";',
    "t/e.test.ts": 'import { q } from "../src/other.ts";',
    "src/x.ts": 'import { deep } from "./deep.mts";',
    "src/deep.mts": "",
    "src/lib/index.ts": "",
    "src/y.cjs": "",
    "src/z.tsx": "",
    "src/other.ts": "",
  };
  const tests = ["t/a.test.ts", "t/b.test.ts", "t/c.test.ts", "t/d.test.ts", "t/e.test.ts"];
  assert.deepEqual(reach(src, tests, ["src/deep.mts"]), ["t/a.test.ts"]);
  assert.deepEqual(reach(src, tests, ["src/lib/index.ts"]), ["t/b.test.ts"]);
  assert.deepEqual(reach(src, tests, ["src/y.cjs"]), ["t/c.test.ts"]);
  assert.deepEqual(reach(src, tests, ["src/z.tsx"]), ["t/d.test.ts"]);
  assert.deepEqual(reach(src, tests, ["src/other.ts"]), ["t/e.test.ts"]);
  assert.deepEqual(reach(src, tests, ["t/c.test.ts"]), ["t/c.test.ts"]); // a test that is itself touched
});

test("a type-only import, a comment and a prose file are no edge; a mixed import is one", () => {
  const src = {
    "t/a.test.ts": 'import type { T } from "../src/types.ts";\nexport type { U } from "../src/types2.ts";',
    "t/b.test.ts": 'import { type T, real } from "../src/real.ts";',
    "t/c.test.ts": '// see ../src/commented.ts and docs/guide.md\n/* ../src/block.ts */\n * ../src/star.ts\n',
    "t/d.test.ts": 'readFileSync("docs/guide.md");',
    "src/types.ts": "",
    "src/types2.ts": "",
    "src/real.ts": "",
    "src/commented.ts": "",
    "src/block.ts": "",
    "src/star.ts": "",
    "docs/guide.md": "",
  };
  const tests = ["t/a.test.ts", "t/b.test.ts", "t/c.test.ts", "t/d.test.ts"];
  assert.deepEqual(reach(src, tests, ["src/types.ts", "src/types2.ts"]), []);
  assert.deepEqual(reach(src, tests, ["src/real.ts"]), ["t/b.test.ts"]);
  assert.deepEqual(reach(src, tests, ["src/commented.ts", "src/block.ts", "src/star.ts"]), []);
  assert.deepEqual(reach(src, tests, ["docs/guide.md"]), []); // prose: a test that names it is not reached by it
});

test("a type-only import with no semicolon never swallows the statement after it; a continuation line starting with * is the accepted blind spot", () => {
  const src = {
    "t/a.test.ts": 'import type { T } from "../src/types.ts"\nimport { real } from "../src/real.ts"\nimport type U from "../src/u.ts"\nimport def, { v } from "../src/def.ts"\nexport type * as NS from "../src/ns.ts"\n',
    "src/types.ts": "",
    "src/real.ts": "",
    "src/u.ts": "",
    "src/def.ts": "",
    "src/ns.ts": "",
  };
  assert.deepEqual(reach(src, ["t/a.test.ts"], ["src/real.ts"]), ["t/a.test.ts"]);
  assert.deepEqual(reach(src, ["t/a.test.ts"], ["src/def.ts"]), ["t/a.test.ts"]);
  assert.deepEqual(reach(src, ["t/a.test.ts"], ["src/types.ts"]), []);
  assert.deepEqual(reach(src, ["t/a.test.ts"], ["src/u.ts"]), []);
  assert.deepEqual(reach(src, ["t/a.test.ts"], ["src/ns.ts"]), []);
});

test("a file a test names reaches it: by basename, by the tail of its path, by a bare name; a name that is not a tracked file reaches nothing", () => {
  const src = {
    "t/a.test.ts": 'readFileSync(join(dir, "data.json"));',
    "t/b.test.ts": 'run("node scripts/dev/tool.mjs");',
    "t/c.test.ts": 'readFileSync("./.spire/land.json"); copy("Dockerfile");',
    "t/d.test.ts": 'readFileSync("other/land.json"); const PINNED = 1; const x = "nothing.json";',
    "data/data.json": "{}",
    "scripts/dev/tool.mjs": "",
    "scripts/other/tool.mjs": "",
    ".spire/land.json": "{}",
    Dockerfile: "",
    "ear/Dockerfile": "",
    ".spire/tool/PIN": "",
  };
  const tests = ["t/a.test.ts", "t/b.test.ts", "t/c.test.ts", "t/d.test.ts"];
  assert.deepEqual(reach(src, tests, ["data/data.json"]), ["t/a.test.ts"]);
  assert.deepEqual(reach(src, tests, ["scripts/dev/tool.mjs"]), ["t/b.test.ts"]);
  assert.deepEqual(reach(src, tests, ["scripts/other/tool.mjs"]), []); // the tail "scripts/dev/tool.mjs" is not this file's
  assert.deepEqual(reach(src, tests, [".spire/land.json"]), ["t/c.test.ts"]);
  assert.deepEqual(reach(src, tests, ["Dockerfile"]), ["t/c.test.ts"]);
  assert.deepEqual(reach(src, tests, [".spire/tool/PIN"]), []); // PINNED is not PIN
});

test("the alias @/ is the src folder above the importer", () => {
  const src = { "app/src/a/x.test.ts": 'import { y } from "@/lib/y";', "app/src/lib/y.ts": "", "other/src/lib/y.ts": "" };
  assert.deepEqual(reach(src, ["app/src/a/x.test.ts"], ["app/src/lib/y.ts"]), ["app/src/a/x.test.ts"]);
  assert.deepEqual(reach(src, ["app/src/a/x.test.ts"], ["other/src/lib/y.ts"]), []);
});

test("a lock file or package.json reaches the tests under its own directory and no other package's", () => {
  const src = { "server/package-lock.json": "{}", "server/package.json": "{}", "app/package-lock.json": "{}", "package-lock.json": "{}", "server/t/a.test.ts": "", "app/t/b.test.ts": "", "scripts/c.test.mjs": "" };
  const tests = ["server/t/a.test.ts", "app/t/b.test.ts", "scripts/c.test.mjs"];
  assert.deepEqual(reach(src, tests, ["server/package-lock.json"]), ["server/t/a.test.ts"]);
  assert.deepEqual(reach(src, tests, ["server/package.json"]), ["server/t/a.test.ts"]);
  assert.deepEqual(reach(src, tests, ["app/package-lock.json"]), ["app/t/b.test.ts"]);
  assert.deepEqual(reach(src, tests, ["package-lock.json"]), tests.sort());
});

test("a file that cannot be read reaches nothing; a missing input throws", () => {
  const g = buildGraph({ files: ["t/a.test.ts", "src/x.ts"], read: () => null, isProse: () => false });
  assert.deepEqual([...reaching(g, ["t/a.test.ts"], ["src/x.ts"])], []);
  assert.throws(() => buildGraph({ files: [], read: () => "" }), /missing input isProse/);
  assert.throws(() => buildGraph({ read: () => "", isProse: () => false }), /missing input files/);
  assert.throws(() => reaching(undefined, [], []), /missing input graph/);
});

test("a template import and a computed import() follow the directory the loader looks in, never a tree and never a directory read as text", () => {
  const many = Object.fromEntries(Array.from({ length: 70 }, (_, i) => [`big/lib/m${i}.mjs`, ""]));
  const src = {
    "t/a.test.ts": "await import(`../evals/demo/scenes/${name}.mjs`);", // a template: the files under its literal directory that end .mjs
    "t/b.test.ts": 'import { load } from "../evals/core/load.mjs";',
    "evals/core/load.mjs": 'export const load = (dir, f) => import(pathToFileURL(join(dir, "scenes", f)).href);', // a computed import() that looks in "scenes"
    "t/c.test.ts": 'import { look } from "../big/look.mjs";',
    "big/look.mjs": 'export const look = (f) => import(join(DIR, "lib", f));', // "lib" holds 70 modules: a tree, not a place a loader looks
    "t/d.test.ts": 'import { readdirSync } from "node:fs"; readdirSync("evals/demo/scenes");', // reads names as text: runs nothing
    "scenes/root.mjs": "", // a directory at the repository root
    "t/e.test.ts": 'import { load } from "../evals/core/load.mjs";',
    "evals/demo/scenes/scene-00.mjs": "",
    "evals/demo/scenes/data.json": "{}",
    "evals/toy/scenes/scene-00.mjs": "",
    "evals/demo/other/x.mjs": "",
    ...many,
  };
  const tests = ["t/a.test.ts", "t/b.test.ts", "t/c.test.ts", "t/d.test.ts"];
  assert.deepEqual(reach(src, [...tests, "t/e.test.ts"], ["scenes/root.mjs"]), ["t/b.test.ts", "t/e.test.ts"]);
  assert.deepEqual(reach(src, tests, ["evals/demo/scenes/scene-00.mjs"]), ["t/a.test.ts", "t/b.test.ts"]);
  assert.deepEqual(reach(src, tests, ["evals/toy/scenes/scene-00.mjs"]), ["t/b.test.ts"]); // the template names demo's directory only; the loader looks in every "scenes"
  assert.deepEqual(reach(src, tests, ["evals/demo/scenes/data.json"]), []); // not ending .mjs, and not a code file for the loader's look
  assert.deepEqual(reach(src, tests, ["evals/demo/other/x.mjs"]), []);
  assert.deepEqual(reach(src, tests, ["big/lib/m3.mjs"]), []);
});

test("a name written relative to the file (`./index.ts`) names the file beside it and no other file of that basename", () => {
  const src = {
    "src/post/a.ts": 'import { x } from "./index.ts"; const f = "./data.json";',
    "src/post/index.ts": "",
    "src/index.ts": 'import "./boot.ts";', // the composition root
    "src/boot.ts": "",
    "src/post/data.json": "{}",
    "other/data.json": "{}",
    "t/b.test.ts": 'const p = "./data.json";', // no data.json beside the test, and the basename is shared: it names none
  };
  const g = graphOf(src);
  const who = (f) => [...(g.importers.get(f) ?? [])].sort();
  assert.deepEqual(who("src/post/index.ts"), ["src/post/a.ts"]);
  assert.deepEqual(who("src/index.ts"), []); // a.ts's "./index.ts" is no import of the root, which imports the whole server
  assert.deepEqual(who("src/post/data.json"), ["src/post/a.ts"]);
  assert.deepEqual(who("other/data.json"), []); // never a.ts (its ./data.json is the one beside it), nor the test (a basename two files share, written relative to nothing)
});

test("a bare `index.ts` in a string names no file when several files have that name; a path that ends in it names that file", () => {
  const src = {
    // a loader that joins the name to a directory it computes: the directory is not in the text
    "src/core/loader.ts": 'const w = await import(pathToFileURL(join(dir, "index.ts")).href);',
    "src/index.ts": 'import "./boot.ts";', // the composition root, which imports the whole server
    "src/post/index.ts": "",
    "src/boot.ts": "",
    "vendor/core/index.ts": "",
    // the same loader, but the vendored wrapper is also named in full from the file: that names the one
    "src/core/named.ts": 'const WRAPPER = "../../vendor/core/index.ts"; const w = await import(join(dir, basename(WRAPPER)));',
    // the call spells the directories before it: it names the file whose path ends the way they do, and no other index.ts
    "t/reads-root.test.ts": 'readFileSync(join(SERVER, "src", "index.ts"), "utf8");',
    "t/reads-up.test.ts": 'readFileSync(join(HERE, "..", "vendor", "core", "index.ts"), "utf8");',
    // a path written in full is a path that ends the file's path, as for any name; a ".." in the middle of the segments is resolved
    "t/full.test.ts": 'readFileSync("src/index.ts");',
    "t/middle.test.ts": 'readFileSync(join(HERE, "x", "..", "vendor", "core", "index.ts"));',
    "t/variable.test.ts": 'readFileSync(join(ROOT, name, "index.ts"));', // the segment before the name is not a string: it names none
    // a name nothing else shares still names its file
    "lone/run.mjs": 'spawn("node", ["main.mjs"]);',
    "lone/main.mjs": "",
    "solo/run.mjs": 'import(join(dir, "index.mjs"));',
    "solo/dir/index.mjs": "",
  };
  const g = graphOf(src);
  const who = (f) => [...(g.importers.get(f) ?? [])].sort();
  assert.deepEqual(who("src/index.ts"), ["t/full.test.ts", "t/reads-root.test.ts"]); // before: src/core/loader.ts was an importer of the root, so every test that reached the loader reached the whole server
  assert.deepEqual(who("src/boot.ts"), ["src/index.ts"]);
  assert.deepEqual(who("vendor/core/index.ts"), ["src/core/named.ts", "t/middle.test.ts", "t/reads-up.test.ts"]);
  assert.deepEqual(who("src/post/index.ts"), []); // t/variable.test.ts names none
  assert.deepEqual(who("lone/main.mjs"), ["lone/run.mjs"]);
  assert.deepEqual(who("solo/dir/index.mjs"), ["solo/run.mjs"]); // one file has the name: the one it names
});
