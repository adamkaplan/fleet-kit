// reach.mjs: what a file REACHES, from the tree's own text. The ONE definition of "this test could notice a change to that file" for a file the coverage map cannot speak for. Pure: the tracked paths and a reader in, a graph out.
// An edge `importer -> file` exists when
//   1. a source file (js/ts/mjs/mts/cjs/cts/jsx/tsx, sh, py) imports, requires or re-exports a RELATIVE specifier (or the "@/" alias of a src folder) that resolves to a tracked file (.js is also tried as .ts, the usual spelling of a TypeScript import);
//   2. a source file names a tracked file in a string: its basename ("owned-docker-child.ts"), or a path that ends the file's path ("tests/fixtures/a.json", "./.spire/land.json"); a file with no extension (Dockerfile, PIN) counts as named when its name stands alone as a word; a name written relative (`./index.ts`, `../x/a.json`) names the file at that path from the file itself, and, when there is none, a basename that more than one file has names none; a bare `index.ts` that more than one file has names none either (the one name every directory has: a loader that joins it to a directory it computes names no one of them), unless the call spells the segments before it as strings, only those directly before it (`join(root, "src", "index.ts")`: the file whose path ends `src/index.ts`; `join(root, src, "index.ts")` names none); a path written in full ("src/index.ts") is a path that ends the file's path, as for any name. How a test reads a fixture, a script, a config.
//   3. a source file loads files it cannot spell as a literal specifier, and the directory is the registry: a template import (`./scenes/${n}.mjs`) links to every tracked file under its literal directory that ends the way the template does; a computed import() (`import(pathToFileURL(join(dir, "scenes", f)))`) links to every code file under each directory of at most WILD_DIR_MAX modules that the file names as a plain word in a string ("scenes"). Only a LOADER is followed so (what a test then runs); a file that merely READS a directory as text (a scanner of the tree) is not: the whole-tree guards are the step that runs those whole. Over-linking is accepted (a test is selected that need not be); under-linking is the miss the oracle counts.
// A DEPENDENCY file (a lock file or package.json) reaches every file under its own directory: the tests of that package, never the tests of another (reaching()).
// What the text cannot see (a path built at run time, a directory read whole, a plugin found by a glob) is not reached: it is the residual risk LAYOUT.md names, and the release-candidate suite before every roll is its net.
import { posix } from "node:path";

export const LOCK_FILES = [
  "package-lock.json",
  "npm-shrinkwrap.json",
  "yarn.lock",
  "pnpm-lock.yaml",
  "bun.lock",
  "bun.lockb",
  "Cargo.lock",
  "go.sum",
  "poetry.lock",
  "Pipfile.lock",
  "uv.lock",
  "Gemfile.lock",
  "composer.lock",
];
const DEPENDENCY_FILES = [...LOCK_FILES, "package.json"];
// the files that pin the toolchain every process of the tree runs under: a change to one is a change to every test. versions.mjs is the one that reads them; carry.test.mjs fails when a name here is not one it reads
export const PIN_FILES = [".node-version", ".nvmrc", "mise.toml", ".mise.toml", ".tool-versions"];

// a computed import() links a directory of at most this many modules ("scenes"), never a tree
const WILD_DIR_MAX = 60;
const INDEX_NAME = /^index\.[A-Za-z]\w{0,8}$/;
const SOURCE = /\.(?:[cm]?[jt]sx?|sh|py)$/;
/** A file that is CODE: what the text of an import, a require or a name in a string can reach (js/ts/mjs/mts/cjs/cts/jsx/tsx, sh, py). Any other file that is not prose is DATA: only a name in some source's text can read it. */
export const isCode = (f) => SOURCE.test(f);
/** A lock file or package.json: it reaches every file under its own directory (reaching()). */
export const isDependencyFile = (f) => DEPENDENCY_FILES.includes(posix.basename(f));
// one statement only: its specifiers are `{...}`, `* as ns` or a default (with an optional `, {...}`), so a type-only import never swallows the statement after it
const TYPE_ONLY = /\b(?:import|export)\s+type\s+(?:\{[^}]*\}|\*(?:\s+as\s+\w+)?|\w+(?:\s*,\s*\{[^}]*\})?)\s*from\s*(["'])[^"'\n]+\1;?/g;
const SPECIFIER = /(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)(["'`])([^"'`\n]+)\1/g;
const NAMED = /[\w@.+/-]*[\w-]\.[A-Za-z]\w{0,8}/g;
const SWAP = { ".js": [".ts", ".tsx"], ".mjs": [".mts"], ".cjs": [".cts"], ".jsx": [".tsx"] };
const EXTENSIONS = [".ts", ".tsx", ".mts", ".cts", ".js", ".mjs", ".cjs", ".jsx", ".json"];

// a whole-line comment names files to a reader, not to a run: its words are not references
// (a line that starts a comment: `//`, `/*`, a block comment's ` * ` body, `#`; a continuation line of code that starts with `*` followed by a space is taken for one: the accepted blind spot)
const withoutComments = (text) => text.replace(/^[ \t]*(?:\/\/|\/\*|\*(?:[ \t\/]|$)|#(?![!]))[^\n]*$/gm, "");

const need = (name, v, type) => {
  const ok = type === "array" ? Array.isArray(v) : typeof v === type;
  if (!ok) throw new Error(`reach: missing input ${name}`);
};

// the tracked files a specifier (already joined to a path without "./") can mean
const resolveSpecifier = (p, tracked) => {
  const out = [];
  const add = (c) => {
    if (tracked.has(c)) out.push(c);
  };
  add(p);
  const ext = posix.extname(p);
  for (const e of SWAP[ext] ?? []) add(p.slice(0, -ext.length) + e);
  for (const e of EXTENSIONS) add(p + e);
  for (const e of EXTENSIONS) add(`${p}/index${e}`);
  return out;
};

/**
 * buildGraph({ files, read, isProse }) -> { importers: Map<file, Set<file>>, files: Set<file> (every tracked path), isProse }
 *   files: every tracked path of the tree, repo-relative with "/"
 *   read: (path) => the file's text, or null when it cannot be read (it is then a file that reaches nothing)
 *   isProse: (path) => true for a file that is only read by people (the project's `paths.prose`): no test reaches it, so a name in a string or a comment is no edge to it
 */
export const buildGraph = ({ files, read, isProse }) => {
  need("files", files, "array");
  need("read", read, "function");
  need("isProse", isProse, "function");
  const tracked = new Set(files.filter((f) => !isProse(f)));
  const importers = new Map();
  const edge = (from, to) => {
    if (from === to) return;
    if (!importers.has(to)) importers.set(to, new Set());
    importers.get(to).add(from);
  };
  const byBase = new Map(); // basename -> tracked paths
  const bare = new Map(); // a name without extension (Dockerfile, PIN) -> tracked paths
  for (const f of tracked) {
    const b = posix.basename(f);
    if (!byBase.has(b)) byBase.set(b, []);
    byBase.get(b).push(f);
    if (!b.includes(".")) {
      if (!bare.has(b)) bare.set(b, []);
      bare.get(b).push(f);
    }
  }
  const escaped = [...bare.keys()].map((k) => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const bareWord = escaped.length ? new RegExp(`(?<![\\w.-])(${escaped.join("|")})(?![\\w.-])`, "g") : null;
  // every directory name chain ("src", "app/src", "scenes") -> the tracked files below any directory so named, built when first asked
  let dirIndex;
  const under = (chain) => {
    if (!dirIndex) {
      dirIndex = new Map();
      for (const u of tracked) {
        const segs = u.split("/").slice(0, -1);
        for (let i = 0; i < segs.length; i++) for (let j = i; j < segs.length; j++) {
          const k = segs.slice(i, j + 1).join("/");
          if (!dirIndex.has(k)) dirIndex.set(k, []);
          dirIndex.get(k).push(u);
        }
      }
    }
    return dirIndex.get(chain) ?? [];
  };
  const below = (dir) => (dir ? under(dir).filter((u) => u.startsWith(`${dir}/`)) : []);
  for (const f of files) {
    if (!SOURCE.test(f) || isProse(f)) continue;
    let text = read(f);
    if (typeof text !== "string") continue;
    const dir = posix.dirname(f);
    text = withoutComments(text).replace(TYPE_ONLY, ""); // erased at run time: it reaches nothing
    const computed = /\bimport\(\s*[^'"`)\s]/.test(text);
    for (const [, quote, spec] of text.matchAll(SPECIFIER)) {
      if (quote === "`" && spec.includes("${")) {
        // a template import: every file under its literal directory that ends the way the template does
        const lit = spec.slice(0, spec.indexOf("${")).replace(/[^/]*$/, "").replace(/\/$/, "");
        const ending = spec.slice(spec.lastIndexOf("}") + 1);
        if (lit.startsWith("./") || lit.startsWith("../") || lit === "." || lit === "..") for (const t of below(posix.normalize(posix.join(dir, lit)))) if (t.endsWith(ending)) edge(f, t);
        continue;
      }
      if (spec.startsWith("./") || spec.startsWith("../")) {
        for (const t of resolveSpecifier(posix.normalize(posix.join(dir, spec)), tracked)) edge(f, t);
      } else if (spec.startsWith("@/")) {
        // the alias of a src folder: every folder above the importer that has one
        for (let a = dir; ; a = posix.dirname(a)) {
          for (const t of resolveSpecifier(posix.join(a === "." ? "" : a, "src", spec.slice(2)), tracked)) edge(f, t);
          if (a === "." || a === "/") break;
        }
      }
    }
    if (computed) {
      // a computed import() looks in a directory it names as a plain word: a directory of a handful of modules ("scenes"), never a tree ("server", "src")
      for (const [, w] of text.matchAll(/["']([a-z][\w-]{2,})["']/g)) {
        const dirs = new Map();
        for (const u of under(w)) {
          if (!isCode(u)) continue;
          const i = u.indexOf(`/${w}/`);
          const d = i >= 0 ? u.slice(0, i) : u.startsWith(`${w}/`) ? "" : null; // "" is a directory at the repository root
          if (d === null) continue;
          if (!dirs.has(d)) dirs.set(d, []);
          dirs.get(d).push(u);
        }
        for (const list of dirs.values()) if (list.length <= WILD_DIR_MAX) for (const u of list) edge(f, u);
      }
    }
    for (const found of text.matchAll(NAMED)) {
      const token = found[0];
      const clean = token.replace(/^(?:\.{1,2}\/)+/, "");
      const base = posix.basename(clean);
      if (token !== clean) {
        // written relative to the file (`./index.ts`, `../lib/x.json`): the file it names beside this one, exactly; a bare basename that many files share (index.ts) names none else
        const exact = resolveSpecifier(posix.normalize(posix.join(dir, token)), tracked).filter((t) => posix.basename(t) === base);
        if (exact.length) {
          for (const t of exact) edge(f, t);
          continue;
        }
        if (clean === base && (byBase.get(base)?.length ?? 0) > 1) continue;
      }
      if (token === clean && !clean.includes("/") && INDEX_NAME.test(base) && (byBase.get(base)?.length ?? 0) > 1) {
        // every directory has an index.ts: the name alone names none. Written as the last of a call's string segments (`join(root, "src", "index.ts")`) it names the file whose path ends the way those segments do
        const tail = /(?:(["'`])[\w@.+-]+\1\s*,\s*)+$/.exec(text.slice(Math.max(0, found.index - 160), found.index - 1));
        const path = tail ? posix.normalize(`${[...tail[0].matchAll(/["'`]([\w@.+-]+)["'`]/g)].map((x) => x[1]).join("/")}/${base}`).replace(/^(?:\.\.?\/)+/, "") : null;
        if (path) for (const t of byBase.get(base)) if (t === path || t.endsWith(`/${path}`)) edge(f, t);
        continue;
      }
      for (const t of byBase.get(base) ?? []) {
        if (base === clean || t === clean || t.endsWith(`/${clean}`)) edge(f, t);
      }
    }
    if (bareWord) for (const [, word] of text.matchAll(bareWord)) for (const t of bare.get(word)) edge(f, t);
  }
  return { importers, files: new Set(files), isProse };
};

/**
 * reaching(graph, candidates, touched) -> Set<candidate>: the candidates (test files) that are one of the touched files, reach one (transitively through imports and named files) or sit in the package of a touched dependency file.
 */
export const reaching = (graph, candidates, touched) => {
  need("graph", graph, "object");
  need("candidates", candidates, "array");
  need("touched", touched, "array");
  const seen = new Set();
  const queue = [...touched];
  for (const t of touched) {
    if (!isDependencyFile(t)) continue;
    const dir = posix.dirname(t);
    for (const c of candidates) if (dir === "." || c.startsWith(`${dir}/`)) queue.push(c);
  }
  while (queue.length) {
    const f = queue.pop();
    if (seen.has(f)) continue;
    seen.add(f);
    for (const i of graph.importers.get(f) ?? []) if (!seen.has(i)) queue.push(i);
  }
  return new Set(candidates.filter((c) => seen.has(c)));
};
