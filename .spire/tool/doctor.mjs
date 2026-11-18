// doctor.mjs: `spire doctor`. What the tool needs on this host and in this project, checked, in plain words, with the fix for each problem.
// It REPLACES every "host / gateway / remote" sentence of a specialist's instructions: one command says what is wrong and what to run.
//   the tool (its copy matches its PIN) | git and the shell tools | mise | the project's pins, resolved on this host | the project's .spire/ files
//   | the box recipe | this host can run the box | the remote answers | the push identity (name, email, and a dry-run push to the remote)
// Output: findings grouped in categories (Host | Project | Council | Remote and access | Landing policies); each check is ✓ (OK), ⚠ (WARN) or ✗ (FAIL) with a `→ fix` line under it, then a numbered summary.
// On a terminal (and no NO_COLOR) it is boxed and coloured; otherwise plain `OK` / `WARN` / `FAIL` words. Exit 0 when there is no ✗, 1 otherwise. A ⚠ never fails.
// Landing policies are read from the host's rules-for-branch API and the ruleset details (landingPolicies below); a rule that cannot be read is a ⚠ "cannot determine", never a pass.
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
 * @param {{cwd?: string, log?: (line: string) => void, tool?: string, env?: NodeJS.ProcessEnv, api?: (path: string) => any, slug?: string, tty?: boolean}} o `tool`: the tool folder to check (default: the one this file is in); `api`, `columns`: the width long lines wrap to (default: the terminal's, else $COLUMNS, else 80); `slug`: the rules API and the repository it is asked about (default: `gh api`, the origin's GitHub slug)
 * @returns {number} the exit status
 */
export function doctor({ cwd = process.cwd(), log = console.log, tool, env = process.env, api, slug, tty = Boolean(process.stdout.isTTY), columns = process.stdout.columns || Number(env.COLUMNS) || 80 } = {}) {
  const layout = tool ? layoutOf(tool) : layoutOf();
  const findings = [];
  let cat = "Host";
  const add = (level, text, fix) => findings.push({ level, text, fix, cat });
  const ok = (t) => add("ok", t);
  const note = (t, f) => add("warn", t, f);
  const problem = (t, f) => add("fail", t, f);
  const top = treeOf(cwd);
  const root = top ?? layout.root;
  // the PROJECT is the checkout the command runs in (a clone of the tool may be run against another project); the tool's own folder is only what the tool checks of itself
  const proj = top ? { root: top, spire: join(top, ".spire"), land: join(top, ".spire", "land.json"), areas: join(top, ".spire", "areas.json"), box: join(top, ".spire", "box.json") } : layout;

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
  const inProject = Boolean(top) && (layout.tool === top || layout.tool.startsWith(top + "/"));
  if (!readPin(layout.tool)) {
    // a clone of the tool run against ANOTHER project is not that project's installed copy: nothing to say about it
    if (!top || inProject) note(`the tool ${version} at ${layout.tool} is not an installed copy (it has no PIN): fine in the spire-land source, not in a project`, "spire install --apply");
  } else {
    const v = verify(layout.tool);
    if (v.ok) ok(`the tool ${v.pin.version} (${v.pin.commit.slice(0, 12)}) at ${layout.tool} matches its PIN${v.pin.skill ? ", and so does the guide" : ""}`);
    else problem(`the tool or its guide was edited by hand, or an update was interrupted: ${v.reason}`, "spire update --apply --discard-edits   (it replaces both folders whole)");
    if (v.ok && !v.pin.skill) note("this install predates the guide (its PIN has no skill): `spire help` has nothing to print", "spire update --apply");
  }
  if (!guideOf({ root: proj.root, spire: layout.spire })) problem("the guide (.agents/skills/spire-land/SKILL.md) is missing", "spire update --apply   (it is installed with the tool)");

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
  cat = "Project";
  let manifest = null;
  if (!existsSync(proj.land)) {
    problem(`the project has no ${proj.land}: it says which areas, checks and review the project has`, "write it (the spire-land skill, references/project.md, describes it) and commit it");
  } else {
    try {
      manifest = loadManifest([proj.box, proj.land, proj.areas]);
      const names = (manifest.areas ?? []).map((a) => a.name);
      if (!names.length) problem("the project declares no areas (areas.json, else land.json): nothing would be checked (land refuses)", "declare at least one area with its steps (or its kind and the recipe of that kind)");
      else ok(`${existsSync(proj.areas) ? ".spire/areas.json" : ".spire/land.json"} is valid: ${names.length} area${names.length === 1 ? "" : "s"} (${names.join(", ")})`);
    } catch (e) {
      problem(`the project's files are refused: ${e.message}`, "fix the file it names (.spire/land.json or .spire/box.json): it is not valid JSON");
    }
  }
  if (manifest) {
    if (!existsSync(proj.box)) note("the project has no .spire/box.json: `box up` is a checkout and nothing else (fine for a project that needs no more)");
    else ok(`.spire/box.json is valid: ${(manifest.services ?? []).length} service(s)${manifest.wrap ? ", wrapped" : ""}`);
    cat = "Council";
    // the review a council needs, when any path can call for one: the project's principles, and a viewpoint for every reviewer called (the project's own, else the tool's default)
    try {
      const reviewers = loadReviewers({ ...fromDisk(proj.root), defaults: join(layout.tool, "defaults") });
      const called = [...reviewers.values()].filter((r) => r.calls.length).map((r) => r.role);
      const missing = called.filter((r) => !reviewers.has(r));
      if (!called.length) note("no reviewer is called for any path (no viewpoint has `calls`): changes land with no review");
      else if (!existsSync(join(proj.root, PRINCIPLES_PATH))) {
        problem(`the council cannot sit: the project has no ${PRINCIPLES_PATH} (its law: a project-owned file)`, `copy the default: cp ${join(layout.tool, "defaults", "principles.md")} ${PRINCIPLES_PATH}; edit it for the project; commit it`);
      } else if (missing.length) {
        problem(`the council cannot sit: no viewpoint for ${missing.join(", ")}`, `write .spire/review/<role>.md with \`role: <role>\` in its front matter (the defaults are in ${join(layout.tool, "defaults", "review")})`);
      } else ok(`the council's law is there: ${PRINCIPLES_PATH}, and viewpoints for ${called.join(", ")} (${[...reviewers.values()].filter((r) => r.source === "project").map((r) => r.role).join(", ") || "none"} the project's own, the rest the tool's defaults)`);
    } catch (e) {
      problem(`the council cannot sit: ${e.message}`, "fix the viewpoint file it names");
    }
    cat = "Project";
    if (manifest.readByChecks && !existsSync(join(proj.root, manifest.readByChecks.module))) {
      problem(`land.json names ${manifest.readByChecks.module} (readByChecks) and it is not in the checkout`, "run it from a full checkout of the project, or fix the path");
    }
  }

  // --- the remote and the push identity ---
  cat = "Remote and access";
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

  // --- the landing policies of the remote's default branch ---
  cat = "Landing policies";
  const originUrl = top ? run("git", ["remote", "get-url", "origin"]) : null;
  const ghApi = api ?? ((path) => {
    const r = run("gh", ["api", path], { env: { ...process.env, GH_PROMPT_DISABLED: "1" } });
    if (r.status !== 0) throw new Error(first(r.stderr) || "gh api failed");
    return JSON.parse(r.stdout);
  });
  for (const f of landingPolicies({ api: ghApi, slug: slug ?? slugOf(originUrl?.status === 0 ? originUrl.stdout.trim() : ""), actor: env.SPIRE_LAND_ACTOR })) findings.push({ ...f, cat });

  return render({ findings, root, log, tty: tty && !env.NO_COLOR, columns });
}

/** `owner/repo` of a GitHub remote URL (https or ssh), or null. */
export function slugOf(url) {
  const m = /github\.com[:/]+([^/\s]+)\/([^/\s]+?)(?:\.git)?\/?$/.exec(url);
  return m ? `${m[1]}/${m[2]}` : null;
}

const BLOCKING = new Set(["update", "required_status_checks", "required_deployments", "merge_queue", "workflows", "code_scanning", "pull_request"]);

/**
 * What the remote's rules say about landing on its default branch, as findings. `api(path)` returns parsed JSON or throws.
 * Every line names the ruleset (name and id) and where it lives: [org] or [repo]. A disabled ruleset is ignored and listed. Anything unreadable is a warn "cannot determine", never an ok.
 * @param {{api: (path: string) => any, slug: string|null, actor?: string, branch?: string}} o `actor`: the landing identity as `Type:id` (e.g. Integration:123), the bypass actor the landing runs as
 */
export function landingPolicies({ api, slug, actor, branch = "main" }) {
  const out = [];
  const ok = (text) => out.push({ level: "ok", text });
  const warn = (text, fix) => out.push({ level: "warn", text, fix });
  const fail = (text, fix) => out.push({ level: "fail", text, fix });
  const NAMES = ["history is never rewritten", "direct pushes need the Land-Checks and Land-Council trailers", "review is required for everyone but the landing identity", "the landing identity can push", "nobody else can bypass the rules", "disabled rulesets"];
  const cannot = (names, why, fix) => { for (const n of names) warn(`cannot determine: ${n} (${why})`, fix); };
  if (!slug) return cannot(NAMES.slice(0, 5), "origin is not a GitHub repository, so there are no rules to read"), out;
  let rules;
  try {
    rules = api(`repos/${slug}/rules/branches/${branch}`);
    if (!Array.isArray(rules)) throw new Error("not a list of rules");
  } catch (e) {
    cannot(NAMES.slice(0, 5), `the rules for ${branch} of ${slug} are unreadable: ${e.message}`, "authenticate with read access to the repository's rules: gh auth login");
    return out;
  }
  const where = (r) => (/^org/i.test(r.ruleset_source_type ?? "") ? "[org]" : "[repo]");
  const sets = new Map(); // id -> {name, tag, bypass|null, unreadable}
  const setOf = (r) => {
    if (sets.has(r.ruleset_id)) return sets.get(r.ruleset_id);
    const org = /^org/i.test(r.ruleset_source_type ?? "");
    const info = { name: undefined, tag: where(r), bypass: null, why: "" };
    try {
      const d = api(org ? `orgs/${r.ruleset_source ?? slug.split("/")[0]}/rulesets/${r.ruleset_id}` : `repos/${slug}/rulesets/${r.ruleset_id}`);
      info.name = d.name;
      info.bypass = Array.isArray(d.bypass_actors) ? d.bypass_actors : null;
      if (!info.bypass) info.why = "the ruleset's bypass list is not in the answer";
    } catch (e) {
      info.why = e.message;
    }
    info.label = `ruleset "${info.name ?? "?"}" #${r.ruleset_id} ${info.tag}`;
    sets.set(r.ruleset_id, info);
    return info;
  };
  const infos = rules.map((r) => ({ r, s: setOf(r) }));
  const allSets = [...sets.values()];
  const ofType = (t) => infos.filter(({ r }) => r.type === t);
  const names = (list) => [...new Set(list.map(({ s }) => s.label))].join(", ");
  const [type, id] = String(actor ?? "").split(":");
  const me = actor && id ? { type, id: Number(id) } : null;
  const NO_ACTOR = "the landing identity is not named: set SPIRE_LAND_ACTOR=<ActorType>:<id> (the ruleset bypass actor the landing runs as, e.g. Integration:123)";
  const mayBypass = (s) => s.bypass.some((b) => b.actor_type === me.type && Number(b.actor_id) === me.id && b.bypass_mode !== "pull_request");
  /** true | false | null (cannot tell) */
  const bypasses = (s) => (!me || !s.bypass ? null : mayBypass(s));
  const why = (list) => list.find(({ s }) => !s.bypass)?.s.why || "the landing identity is not named";

  // 1. never rewritten: decided by the rules alone (deletion and non_fast_forward), not by who may bypass them (that is line 5)
  const nff = ofType("non_fast_forward");
  const del = ofType("deletion");
  const lacking = [!nff.length && "force pushes (non_fast_forward)", !del.length && "deletion"].filter(Boolean);
  if (lacking.length) fail(`history can be rewritten: no active ruleset blocks ${lacking.join(" or ")} on ${branch} of ${slug}`, `add ${lacking.map((l) => (l.startsWith("force") ? "a non_fast_forward" : "a deletion")).join(" and ")} rule to a ruleset for ${branch}`);
  else ok(`history is never rewritten: force pushes and deletion are blocked by ${names([...nff, ...del])}`);

  // 2. trailers
  const pats = ofType("commit_message_pattern").filter(({ r }) => !r.parameters?.negate);
  const has = (t) => pats.filter(({ r }) => String(r.parameters?.pattern ?? "").includes(t));
  const missing = ["Land-Checks", "Land-Council"].filter((t) => !has(t).length);
  if (missing.length) fail(`a direct push is not required to carry ${missing.map((t) => `${t}:`).join(" and ")}: no active commit message rule asks for it on ${branch} of ${slug}`, "add a commit_message_pattern rule requiring the Land-Checks: and Land-Council: trailers");
  else ok(`direct pushes need the Land-Checks and Land-Council trailers: ${names([...has("Land-Checks"), ...has("Land-Council")])}`);

  // 3. review required, but not of the landing identity
  const prs = infos.filter(({ r }) => r.type === "pull_request" && Number(r.parameters?.required_approving_review_count ?? 0) >= 1);
  if (!prs.length) fail(`review is not required: no active pull_request rule asks for an approving review on ${branch} of ${slug}`, "add a pull_request rule with required approving reviews to a ruleset for the branch");
  else {
    const states = prs.map(({ s }) => bypasses(s));
    if (states.includes(false)) fail(`the landing identity must review like everyone: it does not bypass the pull_request rule of ${names(prs.filter(({ s }) => bypasses(s) === false))}`, "add the landing identity to that ruleset's bypass list (mode: always)");
    else if (states.includes(null)) warn(`cannot determine: review is required except for the landing identity (${why(prs)}) in ${names(prs)}`, me ? undefined : NO_ACTOR);
    else ok(`review is required, and the landing identity bypasses it: ${names(prs)}`);
  }

  // 4. the landing identity can push: every blocking rule is bypassed by it
  const blockers = infos.filter(({ r }) => BLOCKING.has(r.type) && !(r.type === "pull_request" && !prs.some((p) => p.r === r)));
  const stuck = blockers.filter(({ s }) => bypasses(s) === false);
  const unsure = blockers.filter(({ s }) => bypasses(s) === null);
  if (stuck.length) fail(`the landing identity cannot push: ${[...new Set(stuck.map(({ r, s }) => `${r.type} of ${s.label}`))].join(", ")} apply to it`, "add the landing identity to the bypass list of each ruleset named, or remove the rule");
  else if (unsure.length) warn(`cannot determine: the landing identity can push (${why(unsure)}) past ${names(unsure)}`, me ? undefined : NO_ACTOR);
  else ok(`the landing identity can push to ${branch}: no active rule blocks it${blockers.length ? ` (${names(blockers)} are bypassed)` : ""}`);

  // 5. a human who can bypass
  const unreadable = allSets.filter((s) => !s.bypass);
  const humans = [];
  for (const s of allSets) for (const b of s.bypass ?? []) if (!(me && b.actor_type === me.type && Number(b.actor_id) === me.id)) {
    const h = `${b.actor_type}:${b.actor_id ?? "?"} (${b.bypass_mode ?? "always"}) in ${s.label}`;
    if (!humans.includes(h)) humans.push(h); // each ruleset + actor once
  }
  if (unreadable.length) warn(`cannot determine: who else can bypass the rules (${unreadable[0].why}) in ${unreadable.map((s) => s.label).join(", ")}`);
  else if (humans.length) warn(`someone besides the landing identity can bypass the rules: ${humans.join("; ")}`, "remove them from the bypass list unless a person must be able to override");
  else if (!me && allSets.some((s) => s.bypass.length)) warn("cannot determine: who else can bypass the rules (the landing identity is not named)", NO_ACTOR);
  else ok(`nobody else can bypass the rules of ${branch} of ${slug}`);

  // 6. disabled rulesets are ignored, and listed
  try {
    const list = api(`repos/${slug}/rulesets?includes_parents=true`);
    if (!Array.isArray(list)) throw new Error("not a list of rulesets");
    const off = list.filter((x) => x.enforcement !== "active");
    const tag = (x) => `ruleset "${x.name}" #${x.id} ${/^org/i.test(x.source_type ?? "") ? "[org]" : "[repo]"} (${x.enforcement})`;
    ok(off.length ? `ignored, not active: ${off.map(tag).join(", ")}` : `no ruleset of ${slug} is disabled`);
  } catch (e) {
    warn(`cannot determine: which rulesets are disabled (${e.message})`, "authenticate with read access to the repository's rulesets: gh auth login");
  }
  return out;
}

const CATEGORIES = ["Host", "Project", "Council", "Remote and access", "Landing policies"];

/** `text` wrapped to `width` columns: the first line starts after `first` (already printed), every next line is indented by `hang` spaces. A word longer than a line stays whole. */
export function wrap(first, text, hang, width) {
  const lines = [];
  let cur = first;
  let empty = true;
  for (const w of String(text).split(/\s+/).filter(Boolean)) {
    if (!empty && cur.length + 1 + w.length > width) { lines.push(cur); cur = " ".repeat(hang) + w; } else cur += (empty ? "" : " ") + w;
    empty = false;
  }
  lines.push(cur);
  return lines;
}

/** Print the findings by category, a rule, and the numbered summary (every ✗ first, then ⚠); the exit status (1 when any ✗). */
function render({ findings, root, log, tty, columns = 80 }) {
  const width = Math.max(40, columns);
  const mark = tty ? { ok: "\x1b[32m✓\x1b[0m", warn: "\x1b[33m⚠\x1b[0m", fail: "\x1b[31m✗\x1b[0m" } : { ok: "OK  ", warn: "WARN", fail: "FAIL" };
  const arrow = tty ? "→" : "->";
  const dim = (t) => (tty ? `\x1b[2m${t}\x1b[0m` : t);
  const title = "spire doctor";
  // `lead`: what precedes the text (its visible width is `vis`); the continuation lines hang under the text
  const put = (lead, vis, text) => {
    const w = wrap(" ".repeat(vis), text, vis, width);
    w[0] = lead + w[0].slice(vis);
    for (const l of w) log(l);
  };
  const mw = tty ? 1 : 4; // the visible width of a mark
  if (tty) {
    log(`┌${"─".repeat(title.length + 4)}┐`);
    log(`│  ${title}  │`);
    log(`└${"─".repeat(title.length + 4)}┘`);
    log(dim(root));
  } else log(`${title}: ${root}`);
  for (const cat of CATEGORIES) {
    const mine = findings.filter((f) => f.cat === cat);
    if (!mine.length) continue;
    log("");
    log(tty ? `\x1b[1m◆ ${cat}\x1b[0m` : `== ${cat} ==`);
    for (const f of mine) {
      put(`  ${mark[f.level]} `, 3 + mw, f.text);
      if (f.fix) put(`      ${arrow} fix: `, 6 + arrow.length + 6, f.fix);
    }
  }
  const issues = [...findings.filter((f) => f.level === "fail"), ...findings.filter((f) => f.level === "warn")];
  const bad = findings.filter((f) => f.level === "fail").length;
  log("");
  log(dim((tty ? "─" : "-").repeat(width)));
  log(tty ? "\x1b[1m◆ Summary\x1b[0m" : "== Summary ==");
  log(dim((tty ? "─" : "-").repeat(width)));
  if (issues.length) {
    log(`Found ${issues.length} issue${issues.length === 1 ? "" : "s"} to address${bad ? ` (${bad} failing, listed first)` : " (warnings only: none fails)"}:`);
    issues.forEach((f, i) => {
      const n = `${i + 1}. `;
      put(`  ${n}${mark[f.level]} `, 3 + n.length + mw, f.text);
      if (f.fix) put(`     ${arrow} `, 5 + arrow.length + 1, f.fix);
    });
  }
  if (!bad) log("ready: nothing fails; spire land should work from here.");
  else log(`not ready: ${bad} failing ${bad === 1 ? "check" : "checks"} to fix first.`);
  return bad ? 1 : 0;
}
