// reviewers.mjs: the council's viewpoints (the reviewers) and the principles they enforce: the ONLY two things the council reads (LAYOUT.md "The law").
//   principles   the project's own `.spire/principles.md` (numbered rules, written once; a copy of spire-land's default made when the project began, edited by the project since).
//   viewpoints   one SHORT definition per reviewer: what it looks for, which paths call it (`calls`), which principle numbers it enforces (`enforces`), the files it reads (`reads`).
//                The tool ships defaults (tool/defaults/review/<role>.md); the project's `.spire/review/<role>.md` replaces the default of that name whole, or adds a reviewer.
//                A file in `.spire/review/` is a definition only when its front matter says `role: <its own name>`; any other file there is not read.
// Nothing here runs a model or a command: `read(path)` and `list(dir)` are given (the base commit, a working tree, a test's map), so the same rules serve the council, classify,
// doctor and `spire principles`.
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { layout } from "./layout.mjs";

/** Where the defaults are: the tool's own folder (replaced whole on update, so the default of a viewpoint is always the tool's current one). */
export const DEFAULTS = join(layout.tool, "defaults");
export const DEFAULT_PRINCIPLES = join(DEFAULTS, "principles.md");
/** The project's files, as repository paths (the council reads them from the base commit). */
export const PRINCIPLES_PATH = ".spire/principles.md";
export const REVIEW_DIR = ".spire/review";

/** A definition: the front matter's `role`, `calls`, `enforces`, `reads` (each a JSON list on one line: `calls: ["@prod"]`, `enforces: [3, 5]`) and the body that follows it. Null when there is none. */
export function parseDefinition(md) {
  const m = /^---\n([\s\S]*?)\n---\n/.exec(String(md ?? ""));
  if (!m) return null;
  const f = {};
  for (const line of m[1].split("\n")) {
    const kv = /^(\w+):\s*(.*?)\s*$/.exec(line);
    if (kv) f[kv[1]] = kv[2];
  }
  const list = (key) => {
    if (f[key] === undefined) return [];
    try {
      const v = JSON.parse(f[key]);
      if (!Array.isArray(v)) throw new Error("not a list");
      return v;
    } catch {
      throw new Error(`a viewpoint's \`${key}:\` is a JSON list on one line (for example ${key}: ["a", "b"]); got ${f[key]}`);
    }
  };
  if (!f.role) return null;
  return { role: f.role, calls: list("calls").map(String), enforces: list("enforces").map(Number), reads: list("reads").map(String), body: md.slice(m[0].length).trim() };
}

/** The reviewers of a project: every default, replaced by the project's file of the same name; plus the project's own. Map role -> {role, calls, enforces, reads, body, source}.
 *  `read(path)` returns a repository file's text or null; `list(dir)` its file names (or []); `defaults` is the folder of the tool's defaults. */
export function loadReviewers({ read, list, defaults = DEFAULTS }) {
  const out = new Map();
  const defDir = join(defaults, "review");
  if (existsSync(defDir)) {
    for (const name of readdirSync(defDir).filter((n) => n.endsWith(".md")).sort()) {
      const d = parseDefinition(readFileSync(join(defDir, name), "utf8"));
      if (d) out.set(d.role, { ...d, source: "default" });
    }
  }
  for (const name of list(REVIEW_DIR).filter((n) => n.endsWith(".md")).sort()) {
    let d;
    try {
      d = parseDefinition(read(`${REVIEW_DIR}/${name}`));
    } catch (e) {
      throw new Error(`${REVIEW_DIR}/${name}: ${e.message}`);
    }
    if (d && d.role === name.slice(0, -3)) out.set(d.role, { ...d, source: "project" });
  }
  return out;
}

/** The numbered rules of a principles text: `N. **Name.** text` (a markdown numbered list); the first occurrence of a number wins. [{n, name}] */
export function rulesOf(principles) {
  const seen = new Map();
  for (const m of String(principles ?? "").matchAll(/^[ \t]*(\d+)\.[ \t]+(.*)$/gm)) {
    const n = Number(m[1]);
    if (seen.has(n)) continue;
    const name = /^\*\*(.+?)\.?\*\*/.exec(m[2])?.[1] ?? m[2].slice(0, 60);
    seen.set(n, { n, name: name.trim() });
  }
  return [...seen.values()];
}

/** What a finding or rebuttal's cite names: `P3`, `3`, `principle 3`, `P3 Single source`, or the rule's bold name. Returns the canonical `P3` when the principles hold that number, else null. */
export function citeOf(principles, cite) {
  const rules = rulesOf(principles);
  const text = String(cite ?? "").replace(/\*\*/g, "").trim();
  const num = /^(?:p|principle|rule)?\s*#?(\d+)\b/i.exec(text);
  if (num) return rules.some((r) => r.n === Number(num[1])) ? `P${Number(num[1])}` : null;
  const low = text.toLowerCase().replace(/[.\s]+$/, "");
  const byName = low.length >= 3 && rules.find((r) => r.name.toLowerCase().replace(/[.\s]+$/, "") === low);
  return byName ? `P${byName.n}` : null;
}

/** The disk as `read`/`list` (the working tree of `root`). */
export const fromDisk = (root) => ({
  read: (p) => (existsSync(join(root, p)) ? readFileSync(join(root, p), "utf8") : null),
  list: (d) => (existsSync(join(root, d)) ? readdirSync(join(root, d)) : []),
});
