#!/usr/bin/env node
// spire.mjs: the dispatcher behind `.spire/tool/bin/spire`, the ONE entry point of the tool. It holds no logic of its own beyond choosing what to run:
//   help                    the one guide: the spire-land skill (.agents/skills/spire-land/SKILL.md of the project), word for word
//   land | box | council    the entries of this folder, arguments passed through, exit status = theirs
//   check [--retest] [BASE] the areas' tests of the change, with no council and no push (checks.sh; BASE defaults to origin/main)
//   areas [BASE] [--json]   which areas the change touches (classify.mjs, said in words)
//   capture [...]           box capture: the pictures of the screens at the head of the tree
//   workroom up | down      the shared environment of a Workroom: the project's own box (its `workroom.prepare` first), kept for a day
//   principles [ROLE]       the law a change is built and judged by: the project's .spire/principles.md in full, then each reviewer's viewpoint with the principle numbers it enforces (ROLE: that viewpoint whole)
//   doctor                  what this host needs, checked, and the fix for each problem (doctor.mjs); exit 1 when something is wrong
//   install | update        the installer (bin/spire-land of the spire-land source): copies this tool and the guide into a project
// Every child process starts through versions.mjs (the project's pinned tool versions first in PATH). A pin the host cannot honour refuses with the plain reason, as every entry does.
import { existsSync, readFileSync } from "node:fs";
import { constants } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { doctor, guideOf, treeOf } from "./doctor.mjs";
import { layout } from "./layout.mjs";
import { fromDisk, loadReviewers, PRINCIPLES_PATH } from "./reviewers.mjs";
import { PinError, spawn, spawnSync } from "./versions.mjs";

/** The subcommands, in the order the list is printed: [name, usage, what it does]. */
export const COMMANDS = [
  ["help", "help [COMMAND]", "print the one guide (or one command's usage)"],
  ["land", 'land [--retest] [--rebut "ROLE=PRINCIPLE: REASON"] [--dry-run]', "check, review and land your commits on main"],
  ["check", "check [--retest] [BASE]", "run the tests of the areas your change touches (no review, no push)"],
  ["areas", "areas [BASE] [--json]", "say which areas your change touches"],
  ["box", "box up|run|land|capture|refresh|reset|status|down ...", "the project's environment, as its .spire/box.json declares it (none declared: a checkout and nothing else)"],
  ["council", "council [--base REF] [--head REF] [--roles a,b]", "the review members your change's paths call for, alone"],
  ["capture", "capture [--scene S] [--out DIR] [--box ID]", "the pictures of the screens (box capture)"],
  ["workroom", "workroom up [--ttl-min N] | down [ID]", "the shared environment of a Workroom"],
  ["principles", "principles [ROLE]", "print the project's principles and the reviewers' viewpoints (read both before you build)"],
  ["doctor", "doctor", "check this host and project; print the fix for each problem"],
  ["install", "install [DIR] [--apply] [--source DIR]", "put this tool and its guide into a project (plan first; --apply writes)"],
  ["update", "update [DIR] [--apply] [--discard-edits] [--source DIR]", "replace them with the current ones (plan first; --apply writes)"],
];

const list = () => COMMANDS.map(([, usage, what]) => `  spire ${usage}\n      ${what}`).join("\n");
const out = (s) => process.stdout.write(`${s}\n`);
const err = (s) => process.stderr.write(`${s}\n`);

/** A plain refusal: the message, the exit code. */
class Refusal extends Error {
  constructor(message, code = 2) {
    super(message);
    this.code = code;
  }
}

/** Run a command with inherited stdio under the project's pins; resolves to its exit status. Signals sent to us go to the child. */
function exec(cmd, args, { env, cwd } = {}) {
  return new Promise((done) => {
    let child;
    try {
      child = spawn(cmd, args, { stdio: "inherit", env, cwd });
    } catch (e) {
      if (e instanceof PinError) {
        err(`spire-land: ${e.message}`);
        return done(1);
      }
      throw e;
    }
    const handlers = ["SIGINT", "SIGTERM", "SIGHUP"].map((s) => [s, () => child.kill(s)]);
    for (const [s, h] of handlers) process.on(s, h);
    child.on("error", (e) => {
      for (const [s, h] of handlers) process.off(s, h);
      err(`spire: cannot start ${cmd}: ${e.message}`);
      done(1);
    });
    child.on("exit", (code, sig) => {
      for (const [s, h] of handlers) process.off(s, h);
      done(code ?? 128 + (constants.signals[sig] ?? 1));
    });
  });
}

const entry = (name) => join(layout.tool, name);

// --- help ---

function help(args) {
  if (args[0]) {
    const found = COMMANDS.find(([name]) => name === args[0]);
    if (!found) throw new Refusal(`no such subcommand: ${args[0]}\nThe subcommands:\n${list()}`);
    out(`spire ${found[1]}\n  ${found[2]}\nThe whole guide: spire help`);
    return 0;
  }
  const guide = guideOf(layout);
  if (!guide) {
    throw new Refusal(
      `the guide (.agents/skills/spire-land/SKILL.md) is missing next to ${layout.tool}. It is installed with the tool: spire update --apply${"\n"}The subcommands:\n${list()}`,
      1,
    );
  }
  process.stdout.write(readFileSync(guide, "utf8"));
  return 0;
}

// --- areas ---

/** The base the change is measured from: the merge base with origin/main (what land and check use), or the ref the caller named. */
function baseOf(cwd, given) {
  const git = (...a) => spawnSync("git", a, { cwd, encoding: "utf8" });
  if (git("rev-parse", "--show-toplevel").status !== 0) throw new Refusal("not inside a git checkout");
  const ref = given ?? "origin/main";
  const mb = git("merge-base", ref, "HEAD");
  if (mb.status !== 0) {
    throw new Refusal(`cannot find the base: ${ref} is not a commit of this checkout${given ? "" : ". Fetch it: git fetch origin main"}`);
  }
  return mb.stdout.trim();
}

function areas(args) {
  const json = args.includes("--json");
  const rest = args.filter((a) => a !== "--json");
  if (rest.length > 1 || rest.some((a) => a.startsWith("-"))) throw new Refusal("usage: spire areas [BASE] [--json]");
  const cwd = treeOf(process.cwd()) ?? process.cwd();
  const base = baseOf(cwd, rest[0]);
  const r = spawnSync("node", [entry("classify.mjs"), base, "HEAD"], { cwd, encoding: "utf8", maxBuffer: 1 << 28 });
  if (r.status !== 0) {
    throw new Refusal(`cannot classify the change: ${(r.stderr || r.stdout).trim().split("\n").slice(-3).join("\n")}`, 1);
  }
  const info = JSON.parse(r.stdout.trim().split("\n").pop());
  if (json) {
    out(JSON.stringify(info));
    return 0;
  }
  const a = info.areas;
  out(`The change (${base.slice(0, 10)}..HEAD): ${info.files} files, ${info.prod} production lines.`);
  if (!info.files) out("Nothing differs from the base: nothing would land.");
  else if (a.full) {
    out(`Areas that run: ALL of them (the full suite), because these paths are in no area: ${a.unmapped.slice(0, 8).join(", ")}${a.unmapped.length > 8 ? ", ..." : ""}`);
  } else out(`Areas that run: ${a.names.join(", ") || "none"}. Every test of each of them runs, nothing else.`);
  if (!a.full) for (const [name, paths] of Object.entries(a.byArea)) if (paths.length) out(`  ${name}: ${paths.length} path${paths.length === 1 ? "" : "s"}`);
  out(info.council.length ? `Council members the paths call for: ${info.council.join(", ")}.` : "No path calls for a council member (docs, tests): no review.");
  for (const h of info.held) out(`HELD for a person: ${h.path}: ${h.reason}. land stops at once with exit 22.`);
  return info.held.length ? 22 : 0;
}

// --- principles ---

/** The law, from the project's checkout: its principles in full, then the viewpoints (the project's over the tool's defaults) with the numbers they enforce and the paths that call them. */
function principles(args) {
  if (args.length > 1 || args.some((a) => a.startsWith("-"))) throw new Refusal("usage: spire principles [ROLE]");
  const src = fromDisk(layout.root);
  const text = src.read(PRINCIPLES_PATH);
  if (text === null) {
    throw new Refusal(
      `the project has no ${PRINCIPLES_PATH}: its law is a project-owned file, made from the default when the project began.\nStart it from the default: cp ${join(layout.tool, "defaults", "principles.md")} ${PRINCIPLES_PATH}, edit it for the project, commit it`,
      1,
    );
  }
  const reviewers = loadReviewers({ ...src, defaults: join(layout.tool, "defaults") });
  if (args[0]) {
    const r = reviewers.get(args[0]);
    if (!r) throw new Refusal(`no reviewer named ${args[0]} (reviewers: ${[...reviewers.keys()].join(", ")})`);
    out(`== viewpoint: ${r.role} (${r.source === "project" ? `the project's own, .spire/review/${r.role}.md` : "the tool's default"}) ==`);
    out(`calls: ${r.calls.join(", ") || "(none: nothing calls it)"}\nenforces: ${r.enforces.map((n) => `P${n}`).join(" ") || "(none named)"}${r.reads.length ? `\nreads: ${r.reads.join(", ")}` : ""}\n\n${r.body}`);
    return 0;
  }
  out(`== the project's principles (${PRINCIPLES_PATH}: the project's own law; a finding and a rebuttal cite a rule by its number, P3) ==\n`);
  out(text.trimEnd());
  out(`\n== the reviewers' viewpoints (what each looks for, which paths call it, which principles it enforces; spire principles ROLE prints one whole) ==\n`);
  for (const r of reviewers.values()) {
    const first = /^\*\*(.+?)\*\*/m.exec(r.body)?.[1] ?? "";
    out(`${r.role.padEnd(10)} ${r.source === "project" ? "project" : "default"}  calls: ${r.calls.join(" ") || "-"}  enforces: ${r.enforces.map((n) => `P${n}`).join(" ") || "-"}${first ? `\n           ${first}` : ""}`);
  }
  return 0;
}

// --- workroom ---

async function workroom(args) {
  const [sub, ...rest] = args;
  if (sub === "down") return exec(entry("box"), ["down", ...rest]);
  const usage = "usage: spire workroom up [--ttl-min N] | down [ID]";
  if (sub !== "up") throw new Refusal(usage);
  if (rest.length && (rest[0] !== "--ttl-min" || !/^\d+$/.test(rest[1] ?? "") || rest.length > 2)) throw new Refusal(usage);
  const cwd = treeOf(process.cwd()) ?? process.cwd();
  // what the project's box.json says a shared Workroom prepares first (`workroom.prepare`, the project's own command); nothing declared, nothing run
  let prepare = "";
  if (existsSync(layout.box)) {
    try {
      prepare = JSON.parse(readFileSync(layout.box, "utf8")).workroom?.prepare ?? "";
    } catch {
      // the box says why when it starts
    }
  }
  if (prepare) {
    const code = await exec("bash", ["-c", prepare], { cwd });
    if (code !== 0) {
      err(`spire: workroom.prepare of .spire/box.json failed (exit ${code}); nothing was started`);
      return code;
    }
  }
  // a Workroom's instance lives a day (the shared place; a box alone lives 2 hours) and, inside its one container, listens beyond loopback so the work item's specialists reach it
  const env = { ...process.env, BOX_TTL_MIN: rest[1] ?? process.env.BOX_TTL_MIN ?? "1440" };
  if (!process.env.BOX_BIND_HOST && existsSync(process.env.BOX_DOCKERENV ?? "/.dockerenv")) env.BOX_BIND_HOST = "0.0.0.0";
  return exec(entry("box"), ["up"], { cwd, env });
}

// --- install / update ---

const isSource = (d) => existsSync(join(d, "bin", "spire-land")) && existsSync(join(d, "tool", "VERSION"));

/** Where the installer is: a spire-land source checkout. --source, $SPIRE_LAND_SOURCE, this tool's own (when it runs from the source), else a refusal: the copy never names a repository. */
function installerSource(given, _cleanup) {
  for (const d of [given, process.env.SPIRE_LAND_SOURCE].filter(Boolean)) {
    if (!isSource(resolve(d))) throw new Refusal(`${d} is not a spire-land checkout (no bin/spire-land and tool/VERSION in it)`);
    return resolve(d);
  }
  if (isSource(resolve(layout.tool, ".."))) return resolve(layout.tool, "..");
  throw new Refusal("this copy does not say where the tool comes from (a PIN names no repository): pass --source DIR, a checkout of spire-land (or set SPIRE_LAND_SOURCE)");
}

async function install(cmd, args) {
  const usage = `usage: spire ${cmd} [DIR] [--apply]${cmd === "update" ? " [--discard-edits]" : ""} [--source DIR]`;
  let source;
  const pass = [];
  const dirs = [];
  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--source") {
      source = args[++i];
      if (!source) throw new Refusal("--source needs a directory");
    } else if (args[i] === "--apply" || (args[i] === "--discard-edits" && cmd === "update")) pass.push(args[i]);
    else if (args[i].startsWith("-")) throw new Refusal(`unknown option ${args[i]}\n${usage}`);
    else dirs.push(args[i]);
  }
  if (dirs.length > 1) throw new Refusal(usage);
  const target = resolve(dirs[0] ?? treeOf(process.cwd()) ?? layout.root);
  const cleanup = [];
  try {
    const src = installerSource(source, cleanup);
    return await exec(join(src, "bin", "spire-land"), [cmd, target, ...pass, "--source", src]);
  } finally {
    for (const f of cleanup) f();
  }
}

// --- main ---

/** Run one invocation; resolves to the exit status. */
export async function main(argv) {
  const [cmd, ...args] = argv;
  const cwd = process.cwd();
  switch (cmd) {
    case undefined:
    case "help":
    case "--help":
    case "-h":
      return help(args);
    case "land":
      return exec(entry("land"), args);
    case "box":
      return exec(entry("box"), args);
    case "council":
      return exec(entry("council"), args);
    case "capture":
      return exec(entry("box"), ["capture", ...args]);
    case "check": {
      const retest = args[0] === "--retest" ? ["--retest"] : [];
      const rest = args.slice(retest.length);
      if (rest.length > 1 || rest.some((a) => a.startsWith("-"))) throw new Refusal("usage: spire check [--retest] [BASE]");
      return exec(entry("checks.sh"), [...retest, rest[0] ?? "origin/main"], { cwd });
    }
    case "areas":
      return areas(args);
    case "workroom":
      return workroom(args);
    case "principles":
      return principles(args);
    case "doctor":
      if (args.length) throw new Refusal("usage: spire doctor");
      return doctor({ cwd, log: out });
    case "install":
    case "update":
      return install(cmd, args);
    default:
      throw new Refusal(`unknown subcommand: ${cmd}\nThe subcommands:\n${list()}\nThe whole guide: spire help`);
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    process.exitCode = await main(process.argv.slice(2));
  } catch (e) {
    if (e instanceof PinError) {
      err(`spire-land: ${e.message}`);
      process.exitCode = 1;
    } else if (e instanceof Refusal) {
      err(`spire: ${e.message}`);
      process.exitCode = e.code;
    } else throw e;
  }
}
