// config.mjs: the project's manifest (.spire/land.json, read with its box.json by manifest.mjs) and the glob matcher that reads it.
// The MECHANISM (classify.mjs, plan.mjs, run-steps.mjs, land) holds no project knowledge: which paths make which class, what a class runs, the size,
// the council per class, the environment recipe are all in the file. A project is its own file; Spire's is the first instance.
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";
import { layout } from "./layout.mjs";
import { loadManifest } from "./manifest.mjs";

/** The project root: the checkout the mechanism runs from (the parent of .spire/), which is not necessarily the tree under test. */
export const projectRoot = layout.root;

function expandBraces(g) {
  const m = /\{([^{}]*)\}/.exec(g);
  if (!m) {
    return [g];
  }
  return m[1]
    .split(",")
    .flatMap((alt) =>
      expandBraces(g.slice(0, m.index) + alt + g.slice(m.index + m[0].length)),
    );
}

/** A glob as a RegExp: `**` any path (`**\/` also none), `*` within a segment, `?` one character, `{a,b}` alternatives. */
export function globToRegExp(glob, nocase = false) {
  const parts = expandBraces(glob).map((g) => {
    let re = "";
    for (let i = 0; i < g.length; i++) {
      if (g.startsWith("**/", i)) {
        re += "(?:.*/)?";
        i += 2;
      } else if (g.startsWith("**", i)) {
        re += ".*";
        i += 1;
      } else if (g[i] === "*") {
        re += "[^/]*";
      } else if (g[i] === "?") {
        re += "[^/]";
      } else {
        re += g[i].replace(/[.+^${}()|[\]\\]/g, "\\$&");
      }
    }
    return re;
  });
  return new RegExp(`^(?:${parts.join("|")})$`, nocase ? "i" : "");
}

/** A predicate: does a path match any of these globs? */
export function matcher(globs = [], nocase = false) {
  const res = globs.map((g) => globToRegExp(g, nocase));
  return (p) => res.some((r) => r.test(p));
}

/** The one council setting a project can move: where its model registry is (the model classes: `{"classes": {"fast": {"model": ...}, ...}}`). The council's law is not configurable:
 *  the project's `.spire/principles.md` and its reviewers' viewpoints (`.spire/review/<role>.md` over the tool's defaults) are read by name (reviewers.mjs). */
export const REVIEW_DEFAULTS = {
  models: ".spire/review/models.json",
};

/** Load the project's configuration and build what the mechanism asks of it. `root` is the project root: where the project's own module (`readByChecks`) is found. */
export async function loadConfig(
  path = process.env.LAND_CONFIG ?? layout.land,
  root = projectRoot,
) {
  // the manifest: this file and its sibling box.json as ONE view (manifest.mjs)
  const cfg = loadManifest([
    process.env.BOX_CONFIG ?? join(dirname(path), "box.json"),
    path,
  ]);
  // the one project predicate that is code, not a pattern: which paths can reach a check at all (docs that CI's own scope skips). Named by the project.
  const rb = cfg.readByChecks;
  const mod = rb ? await import(pathToFileURL(join(root, rb.module))) : null;
  return {
    ...cfg,
    path,
    review: { ...REVIEW_DEFAULTS, ...(cfg.review ?? {}) },
    reachesChecks: (paths) => (mod ? mod[rb.export](paths) : true),
    parseNul: mod?.parseNul ?? ((b) => String(b).split("\0").filter(Boolean)),
    isTest: matcher(cfg.paths.tests),
    isProse: matcher(cfg.paths.prose),
    isNotProd: matcher(cfg.paths.notProd),
  };
}
