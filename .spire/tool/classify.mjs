#!/usr/bin/env node
// classify.mjs BASE HEAD: which test AREAS BASE..HEAD touches, which paths are held for a person and which council members its paths call for.
// Prints one JSON object: {prod, files, prodFiles, areas: {names, full, unmapped, byArea}, held: [{path, reason}], council}.
// The MECHANISM only: every project fact (the areas, the sensitive paths, the held paths) is read from the project's file, .spire/land.json
// (config.mjs). The production lines (added + deleted outside tests, prose and lock files) are reported, never a limit. What runs is the areas (areas.mjs).
import { relative } from "node:path";
import { fileURLToPath } from "node:url";
import { touchedAreas } from "./areas.mjs";
import { loadConfig, matcher } from "./config.mjs";
import { loadReviewers } from "./reviewers.mjs";
import { applyPins, execFileSync } from "./versions.mjs";

const git = (...a) =>
  execFileSync("git", a, { encoding: "utf8", maxBuffer: 1 << 28 });

export function classify(cfg, paths, numstat, heldRules = cfg.held ?? [], reviewers = new Map()) {
  const isTest = cfg.isTest;
  const isProd = (p) =>
    cfg.reachesChecks([p]) && !isTest(p) && !cfg.isNotProd(p);
  const prodLines = numstat
    .filter((r) => isProd(r.path))
    .reduce((n, r) => n + r.lines, 0);
  const prodPaths = paths.filter(isProd);

  // the test areas the change touches (areas.mjs): every test of each runs, nothing narrower
  const areas = touchedAreas(cfg, paths);

  // the council: the members the change's PATHS call for: each reviewer's own `calls` (reviewers.mjs; every default member calls `@all`: a project removes or narrows one in its own file), never the change's size.
  // A reviewer is called when a path of the change matches one of its globs, or a token: `@all` (any path at all: tests and docs too), `@prod` (code that reaches the checks, not a test or prose) or `@sensitive` (the project's
  // sensitive paths: its law files, authority, credentials, the lander). A test file calls for no member through a glob or `@prod`/`@sensitive`; a project's file with `calls: []` seats nobody.
  const always = matcher(cfg.sensitive?.always);
  const sensitiveGlobs = matcher(cfg.sensitive?.globs);
  const sensitiveNames = matcher(cfg.sensitive?.nameGlobs, true);
  const isSensitive = (p) =>
    always(p) || (!isTest(p) && (sensitiveGlobs(p) || sensitiveNames(p)));
  const named = { "@all": () => true, "@prod": isProd, "@sensitive": isSensitive };
  const council = [];
  for (const r of reviewers.values()) {
    const ms = r.calls.map((g) => named[g] ?? ((m) => (p) => !isTest(p) && m(p))(matcher([g])));
    if (paths.some((p) => ms.some((m) => m(p))) && !council.includes(r.role)) {
      council.push(r.role);
    }
  }
  // the paths the project's file holds for a person (`held`): land stops at once and prints the reason; nothing is skipped
  const held = heldRules.flatMap((h) => {
    const m = matcher(h.paths);
    return paths.filter(m).map((path) => ({ path, reason: h.reason }));
  });
  return {
    prod: prodLines,
    files: paths.length,
    prodFiles: prodPaths.length,
    areas,
    held,
    council,
  };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  applyPins();
  const cfg = await loadConfig();
  const [base, head] = process.argv.slice(2);
  const paths = cfg.parseNul(git("diff", "--name-only", "-z", base, head));
  const numstat = git("diff", "--numstat", base, head)
    .split("\n")
    .filter(Boolean)
    .map((l) => {
      const [a, d, ...p] = l.split("\t");
      return { path: p.join("\t"), lines: (Number(a) || 0) + (Number(d) || 0) };
    });
  if (!cfg.areas?.length) {
    process.stderr.write(
      "classify: the project's file declares no test areas (`areas`): nothing would be checked\n",
    );
    process.exit(2);
  }
  // the hold is the BASE's: a change that edits the project's file to drop a hold is judged by the file it would drop it from (the law that judges a change is read from the base, never from the change)
  let heldRules = [];
  try {
    const rel = relative(git("rev-parse", "--show-toplevel").trim(), cfg.path);
    heldRules = JSON.parse(git("show", `${base}:${rel}`)).held ?? [];
  } catch {
    // the base has no such file: it holds nothing
  }
  // the reviewers are the BASE's too: a change cannot add or retarget the reviewer that judges it (a project's own definitions, over the tool's defaults)
  const reviewers = loadReviewers({
    read: (p) => {
      try {
        return git("show", `${base}:${p}`);
      } catch {
        return null;
      }
    },
    list: (d) => {
      try {
        return git("ls-tree", "--name-only", base, `${d}/`).split("\n").filter(Boolean).map((n) => n.slice(d.length + 1));
      } catch {
        return [];
      }
    },
  });
  console.log(JSON.stringify(classify(cfg, paths, numstat, heldRules, reviewers)));
}
