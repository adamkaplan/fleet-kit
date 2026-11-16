// plan.mjs: the steps a change's checks run, from the project's land.json (`areas`) and the classification (classify.mjs's JSON: the areas the change touches).
// Every step of every touched area runs, in the file's order; a test step (`list`) runs EVERY test file its command lists. Pure but for the file system and git it is asked
// about: no project fact lives here. A step is {id, area, label, dir, argv, environment, retry}; a step that cannot be planned is an error, never a skip.
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { layout } from "./layout.mjs";
import { matcher } from "./config.mjs";
import { execFileSync } from "./versions.mjs";

/** The changed files of BASE..HEAD matching a glob (default filter ACMR: added, copied, modified, renamed). */
export function changedFiles(base, glob, filter = "ACMR") {
  const m = matcher([glob]);
  return execFileSync(
    "git",
    ["diff", "--name-only", `--diff-filter=${filter}`, base, "HEAD"],
    { encoding: "utf8", maxBuffer: 1 << 28 },
  )
    .split("\n")
    .filter((f) => f && m(f));
}

export function planSteps(cfg, info, ctx) {
  const steps = [];
  const seen = new Set();
  // {tmp}: one scratch directory per run for a step's output, never a path inside the tree under test
  let tmp = "";
  const sub = (s) =>
    s
      .replaceAll("{base}", ctx.base)
      .replaceAll("{tool}", layout.tool)
      .replaceAll("{root}", layout.root)
      .replaceAll(
        "{tmp}",
        () => (tmp ||= mkdtempSync(join(tmpdir(), "land-step-"))),
      );
  const expand = (argv) =>
    argv.flatMap((a) =>
      typeof a === "string"
        ? [sub(a)]
        : changedFiles(ctx.base, a.changed, a.filter).map((f) =>
            a.strip && f.startsWith(a.strip) ? f.slice(a.strip.length) : f,
          ),
    );
  const strip = (s, f) => (s.strip && f.startsWith(s.strip) ? f.slice(s.strip.length) : f);
  const touched = new Set(info.areas.names);
  for (const area of cfg.areas.filter((a) => touched.has(a.name))) {
    for (const s of area.steps) {
      // one step runs once, though two touched areas carry it
      if (seen.has(s.id)) {
        continue;
      }
      seen.add(s.id);
      if (s.whenChanged) {
        // a gate that reads the changed files: one glob, {glob, filter}, or a list of either; it applies when any of them has a changed file
        const ws = [s.whenChanged]
          .flat()
          .map((w) => (typeof w === "string" ? { glob: w } : w));
        if (!ws.some((w) => changedFiles(ctx.base, w.glob, w.filter).length)) {
          continue;
        }
      }
      const step = {
        id: s.id,
        area: area.name,
        label: s.id,
        dir: s.dir,
        environment: s.environment,
        env: s.env,
        after: s.after,
      };
      if (s.list) {
        // a test step: every file the area's own command lists, none narrowed (a --retest run is the one exception: only the files that failed, see below)
        const drop = matcher(s.exclude ?? []);
        let files = execFileSync(sub(s.list[0]), s.list.slice(1).map(sub), {
          encoding: "utf8",
          maxBuffer: 1 << 28,
        })
          .split("\n")
          .filter((f) => f && !drop(f));
        if (!files.length) {
          throw new Error(
            `area ${area.name}: step ${s.id} lists no test file (${s.list.join(" ")}): an area with no tests is a fault in the project's file`,
          );
        }
        const only = ctx.retest?.[s.id];
        if (Array.isArray(only) && only.length) {
          files = files.filter((f) => only.includes(strip(s, f)));
          if (!files.length) {
            continue; // the failed file is gone from the tree: nothing of it to re-run
          }
        }
        steps.push({
          ...step,
          label: `${s.id}(${files.length})`,
          argv: [...expand(s.cmd), ...files.map((f) => strip(s, f))],
          retry: "test",
          files,
        });
        continue;
      }
      steps.push({ ...step, argv: expand(s.cmd) });
    }
  }
  // the project's opt-in to the lint defaults (`"lint": ["typescript"]` in land.json): one step per language, running the project's own copy of the language's one command (.spire/lint/<language>/run, in the tree under test)
  for (const lang of cfg.lint ?? []) {
    const id = `lint:${lang}`;
    if (!/^[a-z][a-z0-9-]*$/.test(lang)) {
      throw new Error(`lint: "${lang}" is not a language name of the tool's defaults/lint/`);
    }
    if (seen.has(id)) {
      throw new Error(`lint: a step of an area already has the id ${id}, which the "lint" opt-in makes: rename that step`);
    }
    seen.add(id);
    steps.push({ id, area: "lint", label: id, dir: ".", argv: [`.spire/lint/${lang}/run`] });
  }
  if (ctx.retest) {
    // --retest: only the steps that failed last time, with their prerequisites (a step that comes `after` another needs its output)
    const byId = new Map(steps.map((x) => [x.id, x]));
    const keep = new Set();
    const add = (id) => {
      if (keep.has(id) || !byId.has(id)) {
        return;
      }
      keep.add(id);
      for (const a of byId.get(id).after ?? []) {
        add(a);
      }
    };
    for (const id of Object.keys(ctx.retest)) {
      add(id);
    }
    return { steps: steps.filter((x) => keep.has(x.id)), notes: [] };
  }
  return { steps, notes: [] };
}
