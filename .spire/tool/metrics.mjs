// metrics.mjs: the lander's measurement record. ONE file per clone, `<git common dir>/spire-land/metrics.jsonl`, ONE JSON line appended at EVERY exit of `land` (written by the trap that runs at every exit,
// signals included) and of `push` (its own trap, `push`): the clock of a landing is read from these lines, never modelled. Nothing here is on the critical path: the line is written after the run's
// last line of output, in a few milliseconds, and a failure to write it never changes an exit code.
//   land line: {v, tool:"land", run, ts, start, wall_s, exit, result, commission, branch, subject, base, tip, tree, lines, files, checks:{wall_s, first_red:{step, s}|null, steps:[{id, s, state}]},
//               council:{start_s, end_s, result, findings}|null, landed:{sha}|null}
//   push line: {v, tool:"push", ts, start, land_run, result, exit, commits, commit, attempts, walls:[gate seconds of each attempt], first_red:{step, s}|null, landed_at, gate_s, total_s}
// `run` is `<start epoch>-<pid>` of the land; a push started by a land carries it as `land_run`, which joins the two. `commission` is LAND_COMMISSION of the environment (the venue's commission id: the join key to its facts).
// `s` of a first red is seconds since the run's start: the time to the first failure. A step's own lines (run-steps.mjs `noteStep`) are in the file LAND_STEPS_OUT, which land makes in its temp directory and push in its run directory.
// Usage: `node metrics.mjs land KEY=VALUE ...` (land) and `node metrics.mjs push KEY=VALUE ...` (push) write their line; `node metrics.mjs first-red STEPS_FILE START_EPOCH` prints the first red as JSON (push).
// A failure to write is silent on purpose: the venue reads the LAST line of a run's log as the lander's typed result, so a word printed after it would hide it. The line is the only thing the exit hooks add.
import { closeSync, fsyncSync, openSync, readFileSync, statSync, writeSync } from "node:fs";
import { pathToFileURL } from "node:url";

/** Append one line, durably: a single write on an O_APPEND descriptor (concurrent landers of a clone never interleave), then fsync. */
export function appendLine(file, record) {
  const fd = openSync(file, "a");
  try {
    writeSync(fd, `${JSON.stringify(record)}\n`);
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
}

const readLines = (file) => {
  try {
    return readFileSync(file, "utf8")
      .split("\n")
      .filter(Boolean)
      .flatMap((l) => {
        try {
          return [JSON.parse(l)];
        } catch {
          return []; // a half-written line of a killed run says nothing
        }
      });
  } catch {
    return [];
  }
};

/** The steps of a run from its step lines ({id, at (epoch seconds), took_s, state}), and the first red: the earliest step that ended red and its whole second since `start` (never rounded up: `start` is a whole second, so a rounded second could pass the run's own total). */
export function stepsOf(lines, start) {
  const reds = lines.filter((l) => l.state === "red").sort((a, b) => a.at - b.at);
  return {
    first_red: reds.length ? { step: reds[0].id, s: Math.max(0, Math.floor(reds[0].at - start)) } : null,
    steps: lines.map((l) => ({ id: l.id, s: l.took_s, state: l.state })),
  };
}

/** The council's record: when it started and ended (seconds since the run's start; null while it never ended), its own result and the findings its members gave. */
function councilOf(tmp, kt0, start) {
  const rcAt = (name) => {
    try {
      return Math.round(statSync(`${tmp}/${name}.rc`).mtimeMs / 1000);
    } catch {
      return null;
    }
  };
  if (kt0 === null) return null;
  const last = (() => {
    try {
      return JSON.parse(readFileSync(`${tmp}/council.out`, "utf8").trim().split("\n").at(-1));
    } catch {
      return null;
    }
  })();
  const ended = rcAt("council");
  return {
    start_s: kt0 - start,
    end_s: ended === null ? null : ended - start,
    result: typeof last?.result === "string" ? last.result : null,
    findings: Array.isArray(last?.members) ? last.members.reduce((n, m) => n + (Array.isArray(m.findings) ? m.findings.length : 0), 0) : null,
  };
}

/** The push line, from the key=value pairs `push` gives; `walls` is a comma list of seconds, `first_red` the JSON of `first-red`. */
export function pushRecord(a, now = Math.floor(Date.now() / 1000)) {
  const first = a.first_red && a.first_red !== "null" ? JSON.parse(a.first_red) : null;
  return {
    v: 1,
    tool: "push",
    ts: now,
    start: num(a.start),
    land_run: a.land_run || null,
    result: a.result,
    exit: num(a.exit),
    commits: num(a.commits),
    commit: a.commit || null,
    attempts: num(a.attempts),
    walls: (a.walls ?? "").split(",").filter(Boolean).map(Number),
    first_red: first,
    landed_at: num(a.landed_at),
    gate_s: num(a.gate_s),
    total_s: num(a.start) === null ? null : now - num(a.start),
  };
}

const num = (v) => (v !== undefined && v !== "" && Number.isFinite(Number(v)) ? Number(v) : null);

/** The land line, from the key=value pairs the land script gives (`tmp`: its temp directory, where the step lines, the council's output and the two exit-code files are). */
export function landRecord(a, now = Math.floor(Date.now() / 1000)) {
  const start = num(a.start) ?? now;
  const kt0 = num(a.kt0);
  const steps = a.tmp ? readLines(`${a.tmp}/steps.jsonl`) : [];
  let checksEnd = null;
  try {
    if (a.tmp && kt0 !== null) checksEnd = Math.round(statSync(`${a.tmp}/checks.rc`).mtimeMs / 1000);
  } catch {}
  return {
    v: 1,
    tool: "land",
    run: a.run,
    ts: now,
    start,
    wall_s: now - start,
    exit: num(a.exit),
    result: a.result || "interrupted",
    commission: a.commission || null,
    branch: a.branch || null,
    subject: a.subject || null,
    base: a.base || null,
    tip: a.tip || null,
    tree: a.tree || null,
    lines: num(a.lines),
    files: num(a.files),
    checks: kt0 === null ? null : { wall_s: checksEnd === null ? null : checksEnd - kt0, ...stepsOf(steps, start) },
    council: a.tmp ? councilOf(a.tmp, kt0, start) : null,
    landed: a.sha ? { sha: a.sha } : null,
  };
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  const [cmd, ...rest] = process.argv.slice(2);
  try {
    if (cmd === "land") {
      const a = Object.fromEntries(rest.map((kv) => [kv.slice(0, kv.indexOf("=")), kv.slice(kv.indexOf("=") + 1)]));
      appendLine(a.file, landRecord(a));
    } else if (cmd === "push") {
      const a = Object.fromEntries(rest.map((kv) => [kv.slice(0, kv.indexOf("=")), kv.slice(kv.indexOf("=") + 1)]));
      appendLine(a.file, pushRecord(a));
    } else if (cmd === "first-red") {
      process.stdout.write(`${JSON.stringify(stepsOf(readLines(rest[0]), Number(rest[1])).first_red)}\n`);
    }
  } catch {
    // a record that cannot be written never changes what the run was
  }
}
