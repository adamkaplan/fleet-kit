// areas.mjs: which of the project's test AREAS a change touches. Pure: paths in, area names out.
// The project's file declares each area (`areas` in .spire/areas.json, else in .spire/land.json): the path globs it owns (`paths`, and `tests`: the test files of the area, owned too) and the steps that run ITS tests (its own `steps`, and the recipe of its `kind`: stepsOf). A change runs EVERY test of each area it touches,
// nothing narrower and nothing else: there is no map, no selection by import or by what a test read, no size cap.
//   a path that matches an area's globs (and none of its `exclude` globs, nor the project's top-level `exclude`, which docs does not obey: prose is docs') touches that area; a path in two areas touches both
//   a path in no area runs EVERYTHING of the closest enclosing area (the longest directory a glob of its `paths` starts in; `tests` never enclose), the areas that tie all run
//   a path with no enclosing area is UNMAPPED: it runs the FULL suite (every area). Never a silent skip.
import { matcher } from "./config.mjs";

/** The directory a glob starts in: its segments up to the first one with a wildcard ("app/src/**" -> "app/src"; "**\/*.md" and "package.json" -> ""). */
export function staticDir(glob) {
  const segs = glob.split("/");
  const out = [];
  for (const s of segs.slice(0, -1)) {
    if (/[*?{[]/.test(s)) {
      break;
    }
    out.push(s);
  }
  return out.join("/");
}

const isDocs = (a) => a.kind === "docs" || a.name === "docs";

/**
 * The steps of an area: the recipe of its `kind` (cfg.recipes[kind], a list of steps) with `{name}` replaced by the area's name in every string and `{tests}` by the area's `tests` globs (a word that holds it is repeated once per glob:
 * `":(glob){tests}"` is one git pathspec per glob, a word that is only `{tests}` is the globs themselves), then the area's own `steps`. An area of the old form (`steps` and no kind) is its own steps. Pure; the recipe is never changed.
 * A fault of the project's file is an error naming the area: a kind with no recipe, a recipe that needs `tests` the area does not declare, an area that checks nothing.
 */
export function stepsOf(area, recipes = {}) {
  const own = area.steps ?? [];
  let fromKind = [];
  if (area.kind !== undefined) {
    const recipe = recipes[area.kind];
    if (!Array.isArray(recipe)) {
      throw new Error(`area ${area.name}: kind ${area.kind} has no recipe (land.json \`recipes\` names: ${Object.keys(recipes).join(", ") || "none"})`);
    }
    const tests = area.tests ?? [];
    const fill = (v) => {
      if (typeof v === "string") {
        return v.replaceAll("{name}", area.name);
      }
      if (Array.isArray(v)) {
        return v.flatMap((x) => {
          if (typeof x === "string" && x.includes("{tests}")) {
            if (!tests.length) {
              throw new Error(`area ${area.name}: the recipe of kind ${area.kind} reads the area's \`tests\` and the area declares none`);
            }
            return tests.map((g) => fill(x.replaceAll("{tests}", g)));
          }
          return [fill(x)];
        });
      }
      if (v && typeof v === "object") {
        return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, fill(x)]));
      }
      return v;
    };
    fromKind = recipe.map(fill);
  }
  if (!fromKind.length && !own.length) {
    throw new Error(`area ${area.name} has no step: a change to it would check nothing (give it a \`kind\` with a recipe, or \`steps\`)`);
  }
  return [...fromKind, ...own];
}

/**
 * @param {{areas: {name: string, kind?: string, paths: string[], tests?: string[], exclude?: string[]}[], exclude?: string[]}} cfg
 * @param {string[]} paths the changed paths (deleted ones too)
 * @returns {{names: string[], full: boolean, unmapped: string[], byArea: Record<string, string[]>}}
 *   names: the touched areas in the project's order; full: every area runs because a path is unmapped; unmapped: those paths
 */
export function touchedAreas(cfg, paths) {
  const areas = cfg.areas.map((a) => ({
    name: a.name,
    owns: matcher([...a.paths, ...(a.tests ?? [])]),
    excluded: matcher([...(a.exclude ?? []), ...(isDocs(a) ? [] : (cfg.exclude ?? []))]),
    dirs: a.paths.map(staticDir).filter(Boolean),
  }));
  const touched = new Set();
  const byArea = Object.fromEntries(areas.map((a) => [a.name, []]));
  const unmapped = [];
  for (const p of paths) {
    let hit = areas.filter((a) => a.owns(p) && !a.excluded(p));
    if (!hit.length) {
      // closest enclosing area: the one whose glob starts in the deepest directory that contains the path
      let best = 0;
      for (const a of areas) {
        for (const d of a.dirs) {
          if (p.startsWith(`${d}/`) && d.length > best) {
            best = d.length;
          }
        }
      }
      hit = best
        ? areas.filter((a) =>
            a.dirs.some((d) => d.length === best && p.startsWith(`${d}/`)),
          )
        : [];
    }
    if (!hit.length) {
      unmapped.push(p);
    }
    for (const a of hit) {
      touched.add(a.name);
      byArea[a.name].push(p);
    }
  }
  const full = unmapped.length > 0;
  const names = areas
    .map((a) => a.name)
    .filter((n) => full || touched.has(n));
  return { names, full, unmapped, byArea };
}
