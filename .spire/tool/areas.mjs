// areas.mjs: which of the project's test AREAS a change touches. Pure: paths in, area names out.
// The project's file (`areas` in .spire/land.json) declares each area: the path globs it owns and the steps that run ITS tests. A change runs EVERY test of each area it touches,
// nothing narrower and nothing else: there is no map, no selection by import or by what a test read, no size cap.
//   a path that matches an area's globs (and none of its `exclude` globs) touches that area; a path in two areas touches both
//   a path in no area runs EVERYTHING of the closest enclosing area (the longest directory a glob of the area starts in), the areas that tie all run
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

/**
 * @param {{areas: {name: string, paths: string[], exclude?: string[]}[]}} cfg
 * @param {string[]} paths the changed paths (deleted ones too)
 * @returns {{names: string[], full: boolean, unmapped: string[], byArea: Record<string, string[]>}}
 *   names: the touched areas in the project's order; full: every area runs because a path is unmapped; unmapped: those paths
 */
export function touchedAreas(cfg, paths) {
  const areas = cfg.areas.map((a) => ({
    name: a.name,
    owns: matcher(a.paths),
    excluded: matcher(a.exclude ?? []),
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
