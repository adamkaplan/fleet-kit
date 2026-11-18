#!/usr/bin/env node
// app-push.mjs: the one way the lander pushes, as the fleet's GitHub App instead of a person's identity. Used by `push` (main) and `land` (the LAND_VIA_PR branch, and an early check).
//   node app-push.mjs check DIR REMOTE            mint an installation token for REMOTE's repository and discard it: "is the app configured here?" (exit 0, or 70)
//   node app-push.mjs push DIR REMOTE REFSPEC     push REFSPEC; app mode mints a token and pushes with it (below), otherwise it is the plain `git push -q REMOTE REFSPEC` as always
// App mode is ON when FLEET_APP_ID is set (app-env.mjs). Then: the key (FLEET_APP_PRIVATE_KEY, given to this command alone) signs a JWT (RS256, iat now-60s, exp 9 min, iss = the app id); the repository's installation is
// found (GET /repos/{owner}/{repo}/installation) and an installation access token for that one repository, contents:write only, is made (POST /app/installations/{id}/access_tokens). The token is used for
// this one push, from memory: `git push <server>/<owner>/<repo>.git` with an `Authorization` header set in the environment of that git alone (never argv, never a file, never a URL), no hook (--no-verify), no credential helper,
// no prompt. A host that has the app on and cannot mint says "app not configured: <why>" and STOPS (exit 70): there is NO fallback to the human identity.
// The key, the JWT, the token and its header are masked in everything this prints (git's own output too), and git's trace variables are removed from its environment.
// Exit: 0 pushed | 70 the app is not configured or the token could not be made (nothing pushed) | git's own status when git refuses | 2 bad input.
import { createPrivateKey, createSign } from "node:crypto";
import { pathToFileURL } from "node:url";
import { API_URL_ENV, APP_ID_ENV, APP_KEY_ENV, SERVER_URL_ENV, appMode } from "./app-env.mjs";
import { execFileSync, spawn } from "./versions.mjs";

export const NOT_CONFIGURED = 70;
const API_VERSION = "2022-11-28";
const b64url = (b) => Buffer.from(b).toString("base64url");

/** A failure of the app's own setup or of minting: its message is safe to print (secrets are masked where it is printed too). */
export class AppError extends Error {
  /** `status`: the HTTP status GitHub answered, when the failure is its answer (statuses.mjs tells a refused permission from a broken network by it). */
  constructor(message, status) { super(message); this.status = status; }
}

/** `owner/repo` of a remote URL: https, ssh://, scp-like (`git@host:owner/repo.git`, an ssh alias included). */
export function parseRepo(url) {
  const parts = url.trim().replace(/\/+$/, "").replace(/\.git$/, "").split(/[/:]/);
  const [owner, repo] = parts.slice(-2);
  if (parts.length < 2 || !owner || !repo) throw new AppError("cannot tell the owner and the repository from the remote's URL");
  return { owner, repo };
}

/** The key as PEM: a key kept on one line with `\n` escapes (a secret store's habit) is repaired. */
export const normalizeKey = (raw) => (raw.includes("\n") || !raw.includes("\\n") ? raw : raw.replaceAll("\\n", "\n")).trim() + "\n";

/** The RS256 JWT of the app: iat = now - 60 s (clock drift), exp = now + 9 min (GitHub's limit is 10), iss = the app id. */
export function makeJwt(appId, pem, now = Math.floor(Date.now() / 1000)) {
  const data = `${b64url(JSON.stringify({ alg: "RS256", typ: "JWT" }))}.${b64url(JSON.stringify({ iat: now - 60, exp: now + 540, iss: String(appId) }))}`;
  return `${data}.${b64url(createSign("RSA-SHA256").update(data).sign(createPrivateKey(pem)))}`;
}

/** `text` with every secret removed: each given value, the lines of a PEM, any PEM block, and anything shaped like a GitHub token. */
export function mask(text, secrets = []) {
  let out = String(text);
  for (const s of secrets) for (const part of String(s).split(/\r?\n/)) if (part.length >= 8) out = out.replaceAll(part, "***");
  return out
    .replace(/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)/g, "***")
    .replace(/\bgh[a-z]_[A-Za-z0-9_]{20,}/g, "***");
}

/** One call of the GitHub API as the app (`bearer` = its JWT) or as an installation (`bearer` = its token): { status, json, message } with the message masked; a network failure is an AppError. */
export async function api(env, secrets, bearer, method, path, body) {
  const base = (env[API_URL_ENV] || "https://api.github.com").replace(/\/+$/, "");
  let res;
  try {
    res = await fetch(`${base}${path}`, {
      method,
      headers: { Authorization: `Bearer ${bearer}`, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION, "User-Agent": "spire-land", ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(30_000),
    });
  } catch (e) {
    throw new AppError(`cannot reach ${new URL(base).host} (${mask(e.cause?.code ?? e.name, secrets)})`);
  }
  const text = await res.text();
  let json = {};
  try { json = JSON.parse(text); } catch { /* not JSON: the status says it */ }
  return { status: res.status, json, message: mask(String(json.message ?? "").slice(0, 200), secrets) };
}

/**
 * Mint the installation access token for ONE repository, with `permissions` (default contents:write only: the token that pushes cannot post a status; statuses.mjs asks for its own). Returns { token, secrets, app, scope } (`app`: its slug when GitHub says it; `scope`: the token's own account of what it may do, printed by the push): `secrets` is everything to mask in output (the key, the JWT, the token, the header made of it).
 * Throws AppError("app not configured: ...") when the id or the key is missing or the key is not a private key, and AppError(...) when GitHub refuses.
 */
export async function mintToken({ env = process.env, owner, repo, now, permissions = { contents: "write" } }) {
  const appId = (env[APP_ID_ENV] ?? "").trim();
  const raw = env[APP_KEY_ENV] ?? "";
  const secrets = [raw];
  if (!appId) throw new AppError(`app not configured: ${APP_ID_ENV} is not set`);
  if (!raw.trim()) throw new AppError(`app not configured: ${APP_KEY_ENV} is missing or empty (run under: doppler run -p fleet -c <config> -- ...)`);
  const pem = normalizeKey(raw);
  secrets.push(pem);
  let jwt;
  try { jwt = makeJwt(appId, pem, now); } catch { throw new AppError(`app not configured: ${APP_KEY_ENV} is not a usable RSA private key`); }
  secrets.push(jwt);
  const inst = await api(env, secrets, jwt, "GET", `/repos/${owner}/${repo}/installation`);
  if (inst.status !== 200 || !Number.isInteger(inst.json.id)) throw new AppError(`app not configured: GitHub answered ${inst.status} for the installation of ${owner}/${repo}${inst.message ? ` (${inst.message})` : ""}: is the app installed there?`);
  const tok = await api(env, secrets, jwt, "POST", `/app/installations/${inst.json.id}/access_tokens`, { repositories: [repo], permissions });
  if (tok.status !== 201 || typeof tok.json.token !== "string") throw new AppError(`the installation token of ${owner}/${repo} was refused: GitHub answered ${tok.status}${tok.message ? ` (${tok.message})` : ""}`, tok.status);
  secrets.push(tok.json.token, Buffer.from(`x-access-token:${tok.json.token}`).toString("base64"));
  // who the app is (its slug), for the line the push prints; a failure to ask is not a failure to push
  const me = await api(env, secrets, jwt, "GET", "/app").catch(() => ({ status: 0, json: {} }));
  const j = tok.json;
  const scope = `repository_selection=${j.repository_selection ?? "?"} repositories=${(j.repositories ?? []).map((r) => r.name ?? r).join(",") || "?"} permissions=${Object.entries(j.permissions ?? {}).map(([k, v]) => `${k}:${v}`).join(",") || "?"} expires_at=${j.expires_at ?? "?"}`;
  return { token: tok.json.token, secrets, app: me.status === 200 && typeof me.json.slug === "string" ? me.json.slug : `app ${appId}`, scope };
}

const REMOVED = /^(GIT_TRACE\w*|GIT_CURL_VERBOSE|GIT_ASKPASS|SSH_ASKPASS|GIT_SSH_COMMAND|GIT_SSH)$/;

/** The environment of the one git that pushes: no secret but the token's header, in git's own configuration variables; nothing that traces or prompts. */
export function pushEnv(parent, serverUrl, token) {
  const env = Object.fromEntries(Object.entries(parent).filter(([k]) => k !== APP_KEY_ENV && !REMOVED.test(k)));
  const n = Number.parseInt(env.GIT_CONFIG_COUNT ?? "0", 10) || 0;
  const header = `Authorization: Basic ${Buffer.from(`x-access-token:${token}`).toString("base64")}`;
  const set = [["http." + serverUrl + "/.extraheader", header], ["credential.helper", ""]];
  set.forEach(([k, v], i) => { env[`GIT_CONFIG_KEY_${n + i}`] = k; env[`GIT_CONFIG_VALUE_${n + i}`] = v; });
  return { ...env, GIT_CONFIG_COUNT: String(n + set.length), GIT_TERMINAL_PROMPT: "0" };
}

const git = (dir, ...a) => execFileSync("git", ["-C", dir, ...a], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();

/** Run `git push` of the app's token; the output is masked as it passes. Resolves to git's exit status. */
function runPush(dir, url, refspec, env, secrets, write) {
  return new Promise((resolve) => {
    const child = spawn("git", ["-C", dir, "push", "-q", "--no-verify", url, refspec], { env, stdio: ["ignore", "pipe", "pipe"] });
    const pipe = (s) => { let buf = ""; s.setEncoding("utf8"); s.on("data", (d) => { buf += d; }); return () => buf; };
    const out = pipe(child.stdout), err = pipe(child.stderr);
    child.on("error", (e) => { write(`cannot run git: ${e.message}\n`); resolve(1); });
    child.on("close", (code) => { write(mask(out() + err(), secrets)); resolve(code ?? 1); });
  });
}

/** The CLI. `write` receives the (masked) text for stderr. Returns the exit status. */
export async function main(argv, env = process.env, write = (t) => process.stderr.write(t)) {
  const [cmd, dir, remote, refspec] = argv;
  if (!["check", "push"].includes(cmd) || !dir || !remote || (cmd === "push" && !refspec)) { write("usage: app-push.mjs check DIR REMOTE | push DIR REMOTE REFSPEC\n"); return 2; }
  if (!appMode(env)) {
    if ((env[APP_KEY_ENV] ?? "") !== "") write(`app-push: note: ${APP_KEY_ENV} is set but ${APP_ID_ENV} is not: the app is off, the host's own git identity is used\n`);
    if (cmd === "check") return 0;
    const r = await new Promise((res) => spawn("git", ["-C", dir, "push", "-q", remote, refspec], { env: Object.fromEntries(Object.entries(env).filter(([k]) => k !== APP_KEY_ENV)), stdio: "inherit" }).on("close", (c) => res(c ?? 1)));
    return r;
  }
  let minted;
  try {
    const { owner, repo } = parseRepo(git(dir, "remote", "get-url", remote));
    minted = { ...(await mintToken({ env, owner, repo })), owner, repo };
  } catch (e) {
    write(`app-push: ${mask(e instanceof AppError ? e.message : `app not configured: ${e.message}`, [env[APP_KEY_ENV] ?? ""])}\napp-push: nothing was pushed; there is no fallback to another identity\n`);
    return NOT_CONFIGURED;
  }
  if (cmd === "check") return 0;
  const server = (env[SERVER_URL_ENV] || "https://github.com").replace(/\/+$/, "");
  const rc = await runPush(dir, `${server}/${minted.owner}/${minted.repo}.git`, refspec, pushEnv(env, server, minted.token), minted.secrets, write);
  if (rc === 0) console.log(`push: pushed as app ${minted.app} by an installation token for ${minted.owner}/${minted.repo} (${minted.scope}); no human identity`);
  return rc;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) process.exitCode = await main(process.argv.slice(2));
