// doctor.mjs: `spire doctor`. What the tool needs on this host and in this project, checked, in plain words, with the fix for each problem.
// It REPLACES every "host / gateway / remote" sentence of a specialist's instructions: one command says what is wrong and what to run.
//   the tool (its copy matches its PIN) | git and the shell tools | mise | the project's pins, resolved on this host | the project's .spire/ files
//   | the box recipe | this host can run the box | the remote answers | the push identity (name, email, and a dry-run push to the remote)
// Output: one line per finding, `ok`, `note` or `PROBLEM` (with `fix:` under it). Exit 0 when there is no PROBLEM, 1 otherwise. A note never fails.
// Every process starts through versions.mjs; doctor marks the pins as resolved (or as refused: then nothing is pinned, and the problem says so) before it runs any.
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { layoutOf } from "./layout.mjs";
import { loadManifest } from "./manifest.mjs";
import { fromDisk, loadReviewers, PRINCIPLES_PATH } from "./reviewers.mjs";
import { readPin, verify } from "./pin.mjs";
import { PinError, resolvePins, spawnSync, startupLine } from "./versions.mjs";

/** The git top of `cwd`, or null when it is not inside a checkout. */
export function treeOf(cwd) {
  // asking git where the checkout is needs no pins (and must work when the host cannot honour them): the call is marked pinned-to-nothing for its own duration only
  const marks = ["SPIRE_PINNED_ROOT", "SPIRE_PINNED_BIN", "SPIRE_PINNED"];
  const neutral = process.env.SPIRE_PINNED_ROOT === undefined;
  if (neutral) Object.assign(process.env, { SPIRE_PINNED_ROOT: cwd, SPIRE_PINNED_BIN: "", SPIRE_PINNED: "" });
  try {
    const r = spawnSync("git", ["rev-parse", "--show-toplevel"], { cwd, encoding: "utf8" });
    return r.status === 0 ? r.stdout.trim() : null;
  } finally {
    if (neutral) for (const k of marks) delete process.env[k];
  }
}

/** The one guide: the skill the tool was installed with, in the project (or beside the tool when it runs from the spire-land source). null when it is not there. */
export function guideOf(layout) {
  for (const base of [layout.root, layout.spire]) {
    const f = join(base, ".agents", "skills", "spire-land", "SKILL.md");
    if (existsSync(f)) return f;
  }
  return null;
}

const first = (text) => String(text ?? "").trim().split("\n").filter(Boolean).pop() ?? "";
const plain = (url) => url.replace(/\/\/[^/@]*@/, "//"); // never print a credential

/**
 * @param {{cwd?: string, log?: (line: string) => void, tool?: string, env?: NodeJS.ProcessEnv}} o `tool`: the tool folder to check (default: the one this file is in)
 * @returns {number} the exit status
 */
export function doctor({ cwd = process.cwd(), log = console.log, tool, env = process.env } = {}) {
  const layout = tool ? layoutOf(tool) : layoutOf();
  const findings = [];
  const add = (level, text, fix) => findings.push({ level, text, fix });
  const ok = (t) => add("ok", t);
  const note = (t, f) => add("note", t, f);
  const problem = (t, f) => add("PROBLEM", t, f);
  const top = treeOf(cwd);
  const root = top ?? layout.root;

  // --- pins: resolved first, because every process below runs under them (a refusal pins nothing, and says so) ---
  let pinned = null;
  let pinProblem = null;
  try {
    pinned = resolvePins(root, env);
  } catch (e) {
    if (!(e instanceof PinError)) throw e;
    pinProblem = e.message;
  }
  process.env.SPIRE_PINNED_ROOT = root;
  process.env.SPIRE_PINNED_BIN = (pinned?.bins ?? []).join(":");
  process.env.SPIRE_PINNED = (pinned?.tools ?? []).map((t) => `${t.tool}@${t.version}`).join(" ");
  if (pinned?.bins.length) process.env.PATH = [...pinned.bins, process.env.PATH].join(":");
  const run = (cmd, args, opts = {}) => spawnSync(cmd, args, { cwd: root, encoding: "utf8", timeout: 30_000, ...opts });
  const has = (cmd) => run("sh", ["-c", `command -v ${cmd}`]).status === 0;

  // --- the tool itself ---
  const version = existsSync(join(layout.tool, "VERSION")) ? readFileSync(join(layout.tool, "VERSION"), "utf8").trim() : "unknown";
  if (!readPin(layout.tool)) {
    note(`the tool ${version} at ${layout.tool} is not an installed copy (it has no PIN): fine in the spire-land source, not in a project`, "spire install --apply");
  } else {
    const v = verify(layout.tool);
    if (v.ok) ok(`the tool ${v.pin.version} (${v.pin.commit.slice(0, 12)}) at ${layout.tool} matches its PIN${v.pin.skill ? ", and so does the guide" : ""}`);
    else problem(`the tool or its guide was edited by hand, or an update was interrupted: ${v.reason}`, "spire update --apply --discard-edits   (it replaces both folders whole)");
    if (v.ok && !v.pin.skill) note("this install predates the guide (its PIN has no skill): `spire help` has nothing to print", "spire update --apply");
  }
  if (!guideOf(layout)) problem("the guide (.agents/skills/spire-land/SKILL.md) is missing", "spire update --apply   (it is installed with the tool)");
  if (top && top !== layout.root) note(`you are in ${top}, but this tool belongs to ${layout.root} (it reads that project's .spire/ files)`);

  // --- git and the shell tools ---
  const g = run("git", ["--version"]);
  if (g.status !== 0) problem("git is not installed", "install git (pacman -S git, apt install git, or: mise use -g git)");
  else ok(first(g.stdout));
  for (const [cmd, why] of [["bash", "the entries are bash scripts"], ["curl", "the box probes its services with it"], ["flock", "land and box lock with it (util-linux)"]]) {
    if (!has(cmd)) problem(`${cmd} is not installed (${why})`, `install ${cmd === "flock" ? "util-linux" : cmd}`);
  }
  if (!top) problem(`${cwd} is not inside a git checkout`, "run it from the project's checkout (a worktree is fine)");

  // --- mise, and the pins ---
  const m = run("mise", ["--version"]);
  const mise = m.status === 0;
  if (mise) ok(`mise ${first(m.stdout).split(" ")[0]} (installs the versions the project pins)`);
  if (pinProblem) {
    problem(pinProblem, mise ? undefined : "install mise first: https://mise.jdx.dev/getting-started.html, then the command above");
  } else if (!pinned.tools.length) {
    note("the project pins no tool versions, so commands run with the host's own node and tools", "pin node in .node-version (for example 22.23.4), then run mise install");
  } else {
    ok(startupLine(pinned).replace(/^spire-land: pins: /, "pinned and installed here: "));
  }
  if (!mise && !pinProblem) note("mise is not installed: it is what installs a pinned version this host lacks", "https://mise.jdx.dev/getting-started.html");

  // --- the project's own files (.spire/) ---
  let manifest = null;
  if (!existsSync(layout.land)) {
    problem(`the project has no ${layout.land}: it says which areas, checks and review the project has`, "write it (the spire-land skill, references/project.md, describes it) and commit it");
  } else {
    try {
      manifest = loadManifest([layout.box, layout.land]);
      const names = (manifest.areas ?? []).map((a) => a.name);
      if (!names.length) problem("land.json declares no areas: nothing would be checked (land refuses)", "declare at least one area with its steps");
      else ok(`.spire/land.json is valid: ${names.length} area${names.length === 1 ? "" : "s"} (${names.join(", ")})`);
    } catch (e) {
      problem(`the project's files are refused: ${e.message}`, "fix the file it names (.spire/land.json or .spire/box.json): it is not valid JSON");
    }
  }
  if (manifest) {
    if (!existsSync(layout.box)) note("the project has no .spire/box.json: `box up` is a checkout and nothing else (fine for a project that needs no more)");
    else ok(`.spire/box.json is valid: ${(manifest.services ?? []).length} service(s)${manifest.wrap ? ", wrapped" : ""}`);
    // the review a council needs, when any path can call for one: the project's principles, and a viewpoint for every reviewer called (the project's own, else the tool's default)
    try {
      const reviewers = loadReviewers({ ...fromDisk(layout.root), defaults: join(layout.tool, "defaults") });
      const called = [...reviewers.values()].filter((r) => r.calls.length).map((r) => r.role);
      const missing = called.filter((r) => !reviewers.has(r));
      if (!called.length) note("no reviewer is called for any path (no viewpoint has `calls`): changes land with no review");
      else if (!existsSync(join(layout.root, PRINCIPLES_PATH))) {
        problem(`the council cannot sit: the project has no ${PRINCIPLES_PATH} (its law: a project-owned file)`, `copy the default: cp ${join(layout.tool, "defaults", "principles.md")} ${PRINCIPLES_PATH}; edit it for the project; commit it`);
      } else if (missing.length) {
        problem(`the council cannot sit: no viewpoint for ${missing.join(", ")}`, `write .spire/review/<role>.md with \`role: <role>\` in its front matter (the defaults are in ${join(layout.tool, "defaults", "review")})`);
      } else ok(`the council's law is there: ${PRINCIPLES_PATH}, and viewpoints for ${called.join(", ")} (${[...reviewers.values()].filter((r) => r.source === "project").map((r) => r.role).join(", ") || "none"} the project's own, the rest the tool's defaults)`);
    } catch (e) {
      problem(`the council cannot sit: ${e.message}`, "fix the viewpoint file it names");
    }
    if (manifest.readByChecks && !existsSync(join(layout.root, manifest.readByChecks.module))) {
      problem(`land.json names ${manifest.readByChecks.module} (readByChecks) and it is not in the checkout`, "run it from a full checkout of the project, or fix the path");
    }
  }

  // --- the remote and the push identity ---
  if (top) {
    const u = run("git", ["remote", "get-url", "origin"]);
    if (u.status !== 0) {
      problem("the checkout has no remote called origin: land fetches and pushes origin main", "git remote add origin <url of the project's repository>");
    } else {
      const url = plain(u.stdout.trim());
      const ls = run("git", ["ls-remote", "origin", "refs/heads/main"], { env: { ...process.env, GIT_TERMINAL_PROMPT: "0" }, timeout: 30_000 });
      if (ls.status !== 0) {
        problem(`origin (${url}) does not answer: ${first(ls.stderr) || "timed out"}`, "check the URL and your credential: gh auth login; gh auth setup-git (or the host's git gateway)");
      } else if (!ls.stdout.trim()) problem(`origin (${url}) has no branch main`, "land pushes to main: create it, or point origin at the right repository");
      else ok(`origin (${url}) answers; main is ${ls.stdout.trim().slice(0, 10)}`);
    }
    const name = run("git", ["config", "user.name"]).stdout.trim();
    const email = run("git", ["config", "user.email"]).stdout.trim();
    if (!name || !email) problem(`git does not know who you are (user.name ${name ? "set" : "missing"}, user.email ${email ? "set" : "missing"}): commits and the attestation need it`, 'git config user.name "Your Name"; git config user.email you@example.com');
    else ok(`you commit as ${name} <${email}>`);
    if (u.status === 0) {
      if (run("git", ["rev-parse", "--verify", "-q", "HEAD"]).status !== 0) note("the checkout has no commit yet, so a push cannot be tried");
      else {
        // a dry run of a push to a branch that is never created: the remote asks for write credentials to answer it
        const p = run("git", ["push", "--dry-run", "--no-verify", "origin", "HEAD:refs/heads/spire-doctor-probe"], { env: { ...process.env, GIT_TERMINAL_PROMPT: "0" }, timeout: 45_000 });
        if (p.status === 0) ok("you may push to origin (a dry run was accepted; nothing was pushed)");
        else problem(`origin refuses a push from here: ${first(p.stderr) || "timed out"}`, "your credential cannot write to the repository: gh auth login; gh auth setup-git, or ask for write access");
      }
    }
  }

  const bad = findings.filter((f) => f.level === "PROBLEM").length;
  log(`spire doctor: ${root}`);
  for (const f of findings) {
    log(`  ${f.level.padEnd(7)} ${f.text}`);
    if (f.fix) log(`          fix: ${f.fix}`);
  }
  log(bad ? `${bad} problem${bad === 1 ? "" : "s"}: fix ${bad === 1 ? "it" : "them"}, then run spire doctor again.` : "ready: nothing is wrong; spire land should work from here.");
  return bad ? 1 : 0;
}
