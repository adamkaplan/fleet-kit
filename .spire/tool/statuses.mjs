#!/usr/bin/env node
// statuses.mjs: the attestation as COMMIT STATUSES (janus-infra/janus-platform#1009), the dual write of the rollout: the Land-Checks and Land-Council trailers stay in the commit message and the same record is posted on the commit as the
// statuses `land/checks` and `land/council` by the fleet's GitHub App (a ruleset that requires them pins `integration_id` to the App, so a token of another app or a person cannot set them). Used by `push` (post) and by attest.mjs (read).
//   node statuses.mjs post DIR REMOTE SHA   post both on SHA with a token made for this alone (statuses:write, no contents: the token that pushes cannot post and this one cannot push). The record is read from the environment:
//                                           LAND_STATUS_CHECKS (the Land-Checks line), LAND_STATUS_COUNCIL (the Land-Council line), LAND_STATUS_OVERRIDE (the Override reason, if any), FLEET_LAND_STATUS_URL (target_url, optional).
//                                           Exit: 0 posted | 71 the App may not post statuses (the installation has no statuses permission: GitHub refuses the token 422, or the POST 403) | 70 the app is not configured | 1 any other failure.
// What a status is: state `success`, `context` land/checks | land/council, `description` at most 140 characters: for land/checks the Land-Checks line with its `tree=<12 hex>` moved to the front (the gate's line ends with it; attest reads it, so no cut may take it) and `(override: <reason|none>)`;
// for land/council the Land-Council line. Both on the TIP of the push, the commit that becomes main's tip: an earlier commit of the push is attested by its tip's (part-of), as with the trailers.
// Reading: GET /repos/{o}/{r}/commits/{sha}/status (the combined status: the newest of each context), one call per push tip. A status read this way says WHO posted it only by the ruleset that refused the others: attest reports what is there, it does not verify the poster.
import { pathToFileURL } from "node:url";
import { APP_KEY_ENV, TARGET_URL_ENV, appMode } from "./app-env.mjs";
import { AppError, NOT_CONFIGURED, api, mask, mintToken, parseRepo } from "./app-push.mjs";
import { execFileSync } from "./versions.mjs";

export const NOT_PERMITTED = 71;
export const CHECKS_CONTEXT = "land/checks";
export const COUNCIL_CONTEXT = "land/council";
const MAX_DESCRIPTION = 140; // GitHub's limit

const oneLine = (s) => String(s ?? "").replace(/\s+/g, " ").trim();
const cut = (s, n) => (s.length <= n ? s : `${s.slice(0, Math.max(0, n - 3))}...`);

/** The statuses of a record: [{ context, state, description }]. The checks line keeps its `tree=` at the front and the override at the end; what does not fit of the line is cut with "...". */
export function statusesOf({ checks, council, override }) {
  const tail = ` (override: ${cut(oneLine(override) || "none", 40)})`;
  // the Land-Checks line of the gate ends with `tree=<12 hex>` (areas=... tests=... total=... tree=...): the tree goes to the FRONT, so no cut can take it
  const line = oneLine(checks);
  const tree = /(?:^|\s)(tree=[0-9a-f]{12,40})(?=\s|$)/.exec(line)?.[1];
  const front = tree ? `${tree} ${oneLine(line.replace(tree, ""))}` : line;
  return [
    { context: CHECKS_CONTEXT, state: "success", description: `${cut(front, MAX_DESCRIPTION - tail.length)}${tail}` },
    { context: COUNCIL_CONTEXT, state: "success", description: cut(oneLine(council), MAX_DESCRIPTION) },
  ];
}

const TREE = /(?:^|\s)tree=([0-9a-f]{12,40})(?:\s|$)/;
/** Does `statuses` (the combined status's list: { context, state, description }) attest the commit whose full tree id is `tree`? { ok: true } | { ok: false, reason }. */
export function statusesAttest(statuses, tree) {
  const by = new Map((statuses ?? []).map((s) => [s.context, s]));
  const checks = by.get(CHECKS_CONTEXT), council = by.get(COUNCIL_CONTEXT);
  if (!checks) return { ok: false, reason: `no ${CHECKS_CONTEXT} status` };
  if (checks.state !== "success") return { ok: false, reason: `${CHECKS_CONTEXT} is ${checks.state}` };
  const named = TREE.exec(checks.description ?? "")?.[1];
  if (!named || !tree.startsWith(named)) return { ok: false, reason: `${CHECKS_CONTEXT} names tree ${named?.slice(0, 12) ?? "(none)"}, the commit's tree is ${tree.slice(0, 12)}` };
  if (!council) return { ok: false, reason: `no ${COUNCIL_CONTEXT} status` };
  if (council.state !== "success") return { ok: false, reason: `${COUNCIL_CONTEXT} is ${council.state}` };
  return { ok: true };
}

/** Post the record as statuses on `sha`. Returns the statuses posted. Throws AppError: `.permitted === false` when the App may not post statuses, else its message says why. */
export async function postStatuses({ env = process.env, owner, repo, sha, record }) {
  let minted;
  try {
    minted = await mintToken({ env, owner, repo, permissions: { statuses: "write" } });
  } catch (e) {
    if (e instanceof AppError && e.status === 422) throw Object.assign(new AppError(`the App has no statuses permission on ${owner}/${repo}: ${e.message}`, 422), { permitted: false });
    throw e;
  }
  const url = (env[TARGET_URL_ENV] ?? "").trim();
  const posted = statusesOf(record);
  for (const s of posted) {
    const r = await api(env, minted.secrets, minted.token, "POST", `/repos/${owner}/${repo}/statuses/${sha}`, { ...s, ...(url ? { target_url: url } : {}) });
    if (r.status === 201) continue;
    const why = `GitHub answered ${r.status} for ${s.context} on ${sha.slice(0, 12)}${r.message ? ` (${r.message})` : ""}`;
    throw Object.assign(new AppError(r.status === 403 ? `the App may not post statuses on ${owner}/${repo}: ${why}` : why, r.status), r.status === 403 ? { permitted: false } : {});
  }
  return { posted, app: minted.app };
}

/** A token to READ statuses: the App's (statuses:read) in app mode, else GITHUB_TOKEN / GH_TOKEN, else null. Returns { token, secrets } | { why } (why is printable). */
export async function readAccess({ env = process.env, owner, repo }) {
  if (appMode(env)) {
    try {
      const m = await mintToken({ env, owner, repo, permissions: { statuses: "read" } });
      return { token: m.token, secrets: m.secrets };
    } catch (e) {
      if (!(e instanceof AppError)) throw e;
      return { why: e.status === 422 ? "the App has no statuses permission" : mask(e.message, [env[APP_KEY_ENV] ?? ""]) };
    }
  }
  const t = (env.GITHUB_TOKEN || env.GH_TOKEN || "").trim();
  return t ? { token: t, secrets: [t] } : { why: "no token (no app configured, no GITHUB_TOKEN / GH_TOKEN)" };
}

/** The combined status of each of `shas`: { [sha]: [{ context, state, description }] } (8 at a time). A sha GitHub does not answer for is left out. */
export async function readStatuses({ env = process.env, owner, repo, shas, access }) {
  const out = {};
  const queue = [...shas];
  await Promise.all(Array.from({ length: Math.min(8, queue.length) }, async () => {
    for (let sha = queue.shift(); sha; sha = queue.shift()) {
      const r = await api(env, access.secrets, access.token, "GET", `/repos/${owner}/${repo}/commits/${sha}/status`);
      if (r.status === 200 && Array.isArray(r.json.statuses)) out[sha] = r.json.statuses;
    }
  }));
  return out;
}

const git = (dir, ...a) => execFileSync("git", ["-C", dir, ...a], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();

/** The CLI. `write` receives the (masked) text for stderr; `out` stdout. Returns the exit status. */
export async function main(argv, env = process.env, write = (t) => process.stderr.write(t), out = (t) => process.stdout.write(t)) {
  const [cmd, dir, remote, sha] = argv;
  if (cmd !== "post" || !dir || !remote || !/^[0-9a-f]{40}$/.test(sha ?? "")) { write("usage: statuses.mjs post DIR REMOTE SHA\n"); return 2; }
  const record = { checks: env.LAND_STATUS_CHECKS, council: env.LAND_STATUS_COUNCIL, override: env.LAND_STATUS_OVERRIDE };
  if (!oneLine(record.checks) || !oneLine(record.council)) { write("statuses: no Land-Checks and Land-Council to post\n"); return 2; }
  const secrets = [env[APP_KEY_ENV] ?? ""];
  if (!appMode(env)) { write("statuses: app not configured: FLEET_APP_ID is not set (statuses are posted by the App only)\n"); return NOT_CONFIGURED; }
  try {
    const { owner, repo } = parseRepo(git(dir, "remote", "get-url", remote));
    const r = await postStatuses({ env, owner, repo, sha, record });
    out(`push: statuses: ${CHECKS_CONTEXT} and ${COUNCIL_CONTEXT} posted on ${sha.slice(0, 12)} by app ${r.app}\n`);
    return 0;
  } catch (e) {
    if (!(e instanceof AppError)) { write(`statuses: ${mask(e.message, secrets)}\n`); return 1; }
    write(`statuses: ${mask(e.message, secrets)}\n`);
    if (e.permitted === false) return NOT_PERMITTED;
    return /^app not configured/.test(e.message) ? NOT_CONFIGURED : 1;
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) process.exitCode = await main(process.argv.slice(2));
