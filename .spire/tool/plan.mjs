// plan.mjs: the steps a change's checks run, from the project's areas (areas.json, else land.json) and the recipes of land.json and the classification (classify.mjs's JSON: the areas the change touches).
// Every step of every touched area runs, in the file's order; a test step (`list`) runs EVERY test file its command lists. Pure but for the file system and git it is asked
// about: no project fact lives here. A step is {id, area, label, dir, argv, environment, retry}; a step that cannot be planned is an error, never a skip.
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, posix } from "node:path";
import { layout } from "./layout.mjs";
import { matcher } from "./config.mjs";
import { stepsOf } from "./areas.mjs";
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

/** The ONE way a test step is restricted to some of its files (--retest: the failed ones; a carried step: the change's own): a copy with only the files `keep` accepts, argv ending with them in the same order. */
export function restrict(step, keep) {
  const tail = step.argv.length - step.files.length; // argv ends with the step's files, in the order of step.files
  const at = step.files.map((f) => keep(f));
  const files = step.files.filter((_, i) => at[i]);
  return { ...step, files, label: `${step.id}(${files.length})`, argv: [...step.argv.slice(0, tail), ...step.argv.slice(tail).filter((_, i) => at[i])] };
}

/**
 * runnerInputs({ step, environments, tracked, root }) -> string[]: the tracked files EVERY test of a test step runs under, whatever the test imports: the files its command names (its runner, the files it preloads with --import, a
 * `--flag=file`), its list command's text, its environment's recipe, and the tracked files the step's `runsUnder` globs (land.json) name: the project's own harness. A change to one of them can change every test of the step, so a selection by reach never narrows it (carry.mjs).
 *   step: a planned test step (dir, argv with its test files last, files, listKey); environments: land.json's `environments`; tracked: Set of the tree's tracked paths; root: the project root (absolute), to read an absolute path of the command
 */
export function runnerInputs({ step, environments, tracked, root }) {
  const words = [
    ...step.argv.slice(0, step.argv.length - (step.files?.length ?? 0)),
    ...(step.listKey ?? []),
    ...Object.values(environments?.[step.environment] ?? {}).flatMap((v) => [v].flat(2).flatMap((x) => (typeof x === "string" ? [x] : Object.values(x ?? {}).flat()))),
  ];
  const found = new Set();
  for (const word of words) {
    for (const raw of String(word).replaceAll("{root}/", "").split(/[\s"'`=;|&(){}<>,$]+/)) {
      if (!raw) continue;
      const rel = raw.startsWith(`${root}/`) ? raw.slice(root.length + 1) : raw;
      for (const c of [posix.normalize(posix.join(step.dir === "." ? "" : step.dir, rel)), posix.normalize(rel)]) {
        if (tracked.has(c)) found.add(c);
      }
    }
  }
  const harness = matcher(step.runsUnder ?? []);
  for (const f of tracked) if (harness(f)) found.add(f);
  return [...found];
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
    for (const s of stepsOf(area, cfg.recipes)) {
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
        kind: area.kind,
        label: s.id,
        dir: s.dir,
        environment: s.environment,
        env: s.env,
        pass: s.pass,
        after: s.after,
        first: s.first === true,
        select: s.select,
        deepFor: s.deepFor,
        runsUnder: s.runsUnder,
        reads: s.reads,
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
        if (!files.length && s.optional) {
          continue; // a recipe asks every area for its live and PostgreSQL files: an area with none has none to run
        }
        if (!files.length) {
          throw new Error(
            `area ${area.name}: step ${s.id} lists no test file (${s.list.join(" ")}): an area with no tests is a fault in the project's file`,
          );
        }
        const only = ctx.retest?.[s.id];
        let planned = {
          ...step,
          label: `${s.id}(${files.length})`,
          argv: [...expand(s.cmd), ...files.map((f) => strip(s, f))],
          retry: "test",
          files,
          // the unexpanded command template: the key of the earlier pass (see stepName in run-steps.mjs)
          cmdKey: s.cmd,
          listKey: s.list,
        };
        if (Array.isArray(only) && only.length) {
          planned = restrict(planned, (f) => only.includes(strip(s, f)));
          if (!planned.files.length) {
            continue; // the failed file is gone from the tree: nothing of it to re-run
          }
        }
        steps.push(planned);
        continue;
      }
      // cmdKey: the unexpanded command template, the key of the earlier pass of a step that names what it reads (carry.mjs, a step that is not a test)
      steps.push({ ...step, argv: expand(s.cmd), cmdKey: s.cmd });
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
