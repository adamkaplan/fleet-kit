#!/usr/bin/env node
// attest.mjs: the record on a landed commit, after the gate. The one owner of how `push` rewrites a commit's Land-Checks.
//   node attest.mjs restamp BASE LINE   in the current worktree: rewrite the commits BASE..HEAD with the Land-Checks trailer = LINE and print the new tip. It does NOT move HEAD, touch the index or the working tree
//                                       (the gate leaves its installs there): it builds the commits with `commit-tree`, each with the tree, the parent chain, the author, the committer and the dates of the commit it
//                                       replaces. The message is the only thing that differs, so every tree is the one the gate saw. The tip gets LINE; an earlier commit of the push gets LINE + ` part-of=<12 hex of the tip's tree>`.
//   `attestedBy(commits)` (pure) says, for commits oldest first, which carry a record of the tree that landed (below).
//   node attest.mjs [--brief] [--all] [--json] [RANGE]   (`spire attest`) the audit: for each commit of RANGE (default: the last 300 of origin/main) the trailers against `git rev-parse <sha>^{tree}`, the push group and the
//                                       Override; prints the attested share and the commits that are not attested. It only reads and prints: the exit is 0 (2 for a RANGE git cannot read). `--brief`: the three lines
//                                       a landing prints; `--all`: every commit that is not attested (default: the first 20); `--json`: the numbers.
//                                       `--statuses`: also READ the commit statuses (land/checks, land/council; statuses.mjs) of each push tip (one GET each, with the App's statuses:read token or GITHUB_TOKEN) and say per commit
//                                       whether its record is in the trailers, in the statuses, in both or in neither (the dual write of janus-infra/janus-platform#1009). A token that cannot be had is said; the trailer audit is printed all the same.
import { pathToFileURL } from "node:url";
import { parseRepo } from "./app-push.mjs";
import { readAccess, readStatuses, statusesAttest } from "./statuses.mjs";
import { execFileSync } from "./versions.mjs";

const git = (args, opts = {}) => execFileSync("git", args, { encoding: "utf8", maxBuffer: 1 << 26, ...opts });

/** `message` with its Land-Checks line replaced by `line` (the last one, wherever it is in the message: a body that goes on after the trailers is real; any other is dropped); added as a trailer when it has none. Every other line is kept. */
export function withChecks(message, line) {
  const lines = message.split("\n");
  const at = lines.flatMap((l, i) => (l.startsWith("Land-Checks: ") ? [i] : []));
  if (!at.length) return git(["interpret-trailers", "--if-exists", "replace", "--trailer", `Land-Checks: ${line}`], { input: message });
  lines[at.at(-1)] = `Land-Checks: ${line}`;
  return lines.filter((_, i) => !at.slice(0, -1).includes(i)).join("\n");
}

/** `line` for an earlier commit of a push: the gate's line, and the tree of the push's tip it was gated with. Its own tree was never gated alone: the unit of attestation is the push. */
export const partOf = (line, tipTree) => `${line.replace(/\s+part-of=\S+/, "")} part-of=${tipTree.slice(0, 12)}`;

/** Rewrite BASE..HEAD of the current worktree, oldest first: the tip's Land-Checks is `line`, an earlier commit's is `line` + `part-of=` (the tip's tree). Returns the new tip. Only the message changes: the tree of every commit is the one it had. */
export function restamp(base, line) {
  let parent = base;
  const all = git(["rev-list", "--reverse", "--topo-order", `${base}..HEAD`]).split("\n").filter(Boolean);
  const tipTree = git(["rev-parse", "HEAD^{tree}"]).trim();
  for (const c of all) {
    const [an, ae, ad, cn, ce, cd, tree] = git(["log", "-1", "--date=raw", "--format=%an%n%ae%n%ad%n%cn%n%ce%n%cd%n%T", c]).split("\n");
    const env = { ...process.env, GIT_AUTHOR_NAME: an, GIT_AUTHOR_EMAIL: ae, GIT_AUTHOR_DATE: ad, GIT_COMMITTER_NAME: cn, GIT_COMMITTER_EMAIL: ce, GIT_COMMITTER_DATE: cd };
    parent = git(["commit-tree", tree, "-p", parent, "-F", "-"], { input: withChecks(git(["log", "-1", "--format=%B", c]), c === all.at(-1) ? line : partOf(line, tipTree)), env }).trim();
  }
  return parent;
}

const TREE = /(?:^|\s)tree=([0-9a-f]{12,40})(?:\s|$)/;
const PART_OF = /(?:^|\s)part-of=([0-9a-f]{12,40})(?:\s|$)/;
/** The value of the last line `Key: value` of the message, or null. Anywhere in the message, as the evidence gate on main reads it (a body that goes on after the trailers is real: 3 of the last 300 commits of spire-venue). */
export const trailerValue = (body, key) => [...(body ?? "").matchAll(new RegExp(`^${key}: +(.*\\S)\\s*$`, "gm"))].at(-1)?.[1] ?? null;

/**
 * Which of `commits` (oldest first; each {sha, tree: the full tree id, body}) carry a record of the tree that landed. One answer each: {status, reason?}.
 *  - "tree":  its Land-Checks `tree=` is the commit's own tree.
 *  - "push":  its Land-Checks says `part-of=T` and the commits after it that say the same end in one whose own tree is T and that carries `tree=` of it: the push's tip attests it. HONEST LIMIT: the trees
 *             between the push's commits were never gated one by one; the unit of attestation is the push.
 *  - "stale": a record that names another tree (a tree that never landed). "none": no Land-Checks. "open": part-of a push whose tip is not in `commits`.
 */
/** Index of the tip of the push commit `i` belongs to: itself when its Land-Checks is not `part-of`, else the first later commit that does not say the same part-of; -1 when the range ends first. */
export function tipIndex(commits, i) {
  const part = PART_OF.exec(trailerValue(commits[i].body, "Land-Checks") ?? "")?.[1];
  if (!part) return i;
  let j = i + 1;
  while (j < commits.length && PART_OF.exec(trailerValue(commits[j].body, "Land-Checks") ?? "")?.[1] === part) j++;
  return j >= commits.length ? -1 : j;
}

export function attestedBy(commits) {
  const own = (c) => {
    const named = TREE.exec(trailerValue(c.body, "Land-Checks") ?? "")?.[1];
    return named && c.tree.startsWith(named);
  };
  return commits.map((c, i) => {
    const line = trailerValue(c.body, "Land-Checks");
    if (!line) return { status: "none", reason: "no Land-Checks record" };
    if (own(c)) return { status: "tree" };
    const part = PART_OF.exec(line)?.[1];
    if (part) {
      const j = tipIndex(commits, i);
      if (j < 0) return { status: "open", reason: `part of a push whose tip (tree ${part.slice(0, 12)}) is not in the range` };
      if (own(commits[j]) && commits[j].tree.startsWith(part)) return { status: "push" };
      return { status: "stale", reason: `part of a push tree ${part.slice(0, 12)} that its tip does not have` };
    }
    return { status: "stale", reason: `the record names tree ${TREE.exec(line)?.[1]?.slice(0, 12) ?? "(none)"}, the commit's tree is ${c.tree.slice(0, 12)}` };
  });
}

/**
 * Where each commit's record is, once the statuses are read (`statusBySha`: the combined status of each push tip, { sha: [{ context, state, description }] }): "both" (trailer and status), "trailer", "status" or "neither". The status of a commit
 * is its own (its sha's statuses name its tree) or, for a commit of a push, its tip's (the same unit of attestation as the trailers: the push).
 */
function recordsOf(commits, rows, statusBySha) {
  return commits.map((c, i) => {
    const trailer = rows[i].status === "tree" || rows[i].status === "push";
    const own = statusesAttest(statusBySha[c.sha], c.tree);
    const j = own.ok ? i : tipIndex(commits, i);
    const viaTip = !own.ok && j > i && statusesAttest(statusBySha[commits[j].sha], commits[j].tree).ok;
    const status = own.ok || viaTip;
    return { sha: c.sha, subject: c.subject, record: trailer && status ? "both" : trailer ? "trailer" : status ? "status" : "neither", statusReason: status ? undefined : own.reason };
  });
}

/** The numbers of an audit: `commits` as for attestedBy, oldest first. A push's tip is a commit that is not part-of another; "tips" are what a Workroom reads as its base (its attestedGreen asks for `tree=` of the base's own tree). */
export function summarize(commits, statusBySha = null) {
  const rows = attestedBy(commits).map((r, i) => ({ sha: commits[i].sha, subject: commits[i].subject, ...r, override: trailerValue(commits[i].body, "Override"), part: PART_OF.test(trailerValue(commits[i].body, "Land-Checks") ?? "") }));
  const n = (f) => rows.filter(f).length;
  const byCommit = statusBySha ? recordsOf(commits, rows, statusBySha) : null;
  const attested = n((r) => r.status === "tree" || r.status === "push");
  return {
    total: rows.length,
    attested,
    byTree: n((r) => r.status === "tree"),
    byPush: n((r) => r.status === "push"),
    share: rows.length ? attested / rows.length : 1,
    tips: n((r) => !r.part),
    tipsAttested: n((r) => !r.part && r.status === "tree"),
    overrides: rows.filter((r) => r.override).map((r) => ({ sha: r.sha, reason: r.override, attested: r.status === "tree" || r.status === "push" })),
    notAttested: rows.filter((r) => r.status !== "tree" && r.status !== "push").map((r) => ({ sha: r.sha, subject: r.subject, status: r.status, reason: r.reason, override: r.override })),
    ...(byCommit ? { byCommit, statuses: { read: true, ...Object.fromEntries([["both", "both"], ["trailerOnly", "trailer"], ["statusOnly", "status"], ["neither", "neither"]].map(([k, v]) => [k, byCommit.filter((c) => c.record === v).length])) } } : {}),
  };
}

/** The audit as text. `brief`: the share, its split and at most three commits that are not attested; `all`: every one (else 20). */
export function report(sum, { range = "the range", brief = false, all = false } = {}) {
  const pct = (a, b) => (b ? `${((100 * a) / b).toFixed(1)}%` : "-");
  const out = [`attest: ${sum.attested}/${sum.total} commits of ${range} carry a record of the tree that landed (${pct(sum.attested, sum.total)})`];
  out.push(`  ${sum.byTree} by their own tree, ${sum.byPush} by their push's tip (part-of), ${sum.notAttested.length} not attested; push tips ${sum.tipsAttested}/${sum.tips} (${pct(sum.tipsAttested, sum.tips)}: what a Workroom's base check can skip its run for)`);
  if (sum.statuses) {
    const st = sum.statuses;
    out.push(`  statuses: ${st.both} both, ${st.trailerOnly} trailer only, ${st.statusOnly} status only, ${st.neither} neither (the statuses are read from GitHub for each push tip: land/checks naming the commit's tree, and land/council)`);
    const lacking = sum.byCommit.filter((c) => c.record !== "both");
    for (const c of lacking.slice(0, brief ? 3 : all ? Infinity : 20)) out.push(`  ${c.record === "neither" ? "no record" : `${c.record} only`.replace("trailer only", "trailer only (no status)").replace("status only", "status only (no trailer)")} ${c.sha.slice(0, 12)}${c.statusReason && c.record !== "status" ? ` (${c.statusReason})` : ""} | ${c.subject.slice(0, 70)}`);
  }
  if (sum.overrides.length) out.push(`  overrides: ${sum.overrides.length}${brief ? "" : ` (${sum.overrides.map((o) => `${o.sha.slice(0, 12)}${o.attested ? "" : " [no record]"}: ${o.reason}`).join("; ")})`}`);
  const shown = sum.notAttested.slice(0, brief ? 3 : all ? Infinity : 20);
  for (const r of shown) out.push(`  not attested ${r.sha.slice(0, 12)} ${r.status}: ${r.reason}${r.override ? ` (Override: ${r.override})` : ""} | ${r.subject.slice(0, 70)}`);
  if (shown.length < sum.notAttested.length) out.push(`  ... and ${sum.notAttested.length - shown.length} more: spire attest --all`);
  if (!brief) out.push("  limit: a commit inside a push is attested by the push's tip; the trees between a push's commits were never gated one by one (the unit of attestation is the push). A record is the lander's message on the commit, not a signature.");
  return out.join("\n");
}

/** The statuses of each push tip of `commits`, read from GitHub: { sha: [...] }; null (and a line on stderr) when they cannot be read: the trailer audit goes on without them. */
async function statusesOfTips(commits, env = process.env) {
  try {
    const { owner, repo } = parseRepo(git(["remote", "get-url", "origin"]));
    const access = await readAccess({ env, owner, repo });
    if (access.why) throw new Error(access.why);
    return await readStatuses({ env, owner, repo, shas: commits.filter((_, i) => tipIndex(commits, i) === i).map((c) => c.sha), access });
  } catch (e) {
    process.stderr.write(`attest: statuses not read: ${String(e.message).split("\n")[0].slice(0, 200)}\n`);
    return null;
  }
}

/** The commits of `rangeArgs` (git log arguments), oldest first. */
export function readCommits(rangeArgs) {
  return git(["log", "--reverse", "--format=%H%x00%T%x00%s%x00%B%x1e", ...rangeArgs])
    .split("\x1e")
    .map((r) => r.replace(/^\n/, ""))
    .filter(Boolean)
    .map((r) => {
      const [sha, tree, subject, ...body] = r.split("\0");
      return { sha, tree, subject, body: body.join("\0") };
    });
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  const argv = process.argv.slice(2);
  const flags = argv.filter((a) => a.startsWith("--"));
  const args = argv.filter((a) => !a.startsWith("--"));
  if (argv[0] === "restamp" && argv.length === 3) {
    process.stdout.write(`${restamp(argv[1], argv[2])}\n`);
  } else if (argv[0] === "restamp" || args.length > 1 || flags.some((f) => !["--brief", "--all", "--json", "--statuses"].includes(f))) {
    process.stderr.write("usage: attest.mjs restamp BASE LINE | [--brief] [--all] [--json] [--statuses] [RANGE]\n");
    process.exit(2);
  } else {
    const has = (ref) => {
      try {
        return git(["rev-parse", "--verify", "-q", ref]).trim() !== "";
      } catch {
        return false;
      }
    };
    const rangeArgs = args.length ? [args[0]] : ["-300", has("origin/main") ? "origin/main" : "HEAD"];
    try {
      const commits = readCommits(rangeArgs);
      const sum = summarize(commits, flags.includes("--statuses") ? await statusesOfTips(commits) : null);
      const range = args.length ? args[0] : `${rangeArgs[1]} (last 300)`;
      process.stdout.write(`${flags.includes("--json") ? JSON.stringify(sum) : report(sum, { range, brief: flags.includes("--brief"), all: flags.includes("--all") })}\n`);
    } catch (e) {
      process.stderr.write(`attest: cannot read ${rangeArgs.join(" ")}: ${String(e.stderr ?? e.message).trim().split("\n")[0]}\n`);
      process.exit(2);
    }
  }
}
