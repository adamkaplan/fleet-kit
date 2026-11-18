// app-world.mjs: the world the tests of the App's push (app-push.test.mjs) and of the commit statuses (statuses.test.mjs) share: a key made for the test, the stub of GitHub (app-stub.mjs), a bare repository
// o/r it serves (main = the tool + a stub gate) and a clone whose origin is the stub's http remote with one commit to land. Nothing here is a real key, token or endpoint.
import { execFileSync, spawn } from "node:child_process";
import { generateKeyPairSync } from "node:crypto";
import { chmodSync, cpSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { API_URL_ENV, APP_ID_ENV, APP_KEY_ENV, SERVER_URL_ENV } from "../app-env.mjs";
import { startStub } from "./app-stub.mjs";

const tool = fileURLToPath(new URL("..", import.meta.url));
export const keypair = () => {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  return { pem: privateKey.export({ type: "pkcs8", format: "pem" }), pub: publicKey.export({ type: "spki", format: "pem" }) };
};
export const KEY = keypair();
export const TOKEN = "stub-installation-token-0123456789abcdef";
export const APP_ID = "5803159";
export const BASIC = `Basic ${Buffer.from(`x-access-token:${TOKEN}`).toString("base64")}`;
export const gitEnv = { GIT_AUTHOR_NAME: "t", GIT_AUTHOR_EMAIL: "t@t", GIT_COMMITTER_NAME: "t", GIT_COMMITTER_EMAIL: "t@t", GIT_TERMINAL_PROMPT: "0" };
export const git = (cwd, ...a) => execFileSync("git", a, { cwd, env: { ...process.env, ...gitEnv }, encoding: "utf8" }).trim();

/** Everything of the secrets that must never be printed: the key's lines, a JWT's shape, the token, the header made of it. */
export const leaks = (text) => [...KEY.pem.split("\n").filter((l) => l.length > 8), TOKEN, BASIC.slice(6), "eyJhbGciOiJSUzI1NiI"].filter((s) => text.includes(s));

// the stub, a bare repository o/r (main with the tool and a stub gate) it serves, and a clone of it over the stub's http (its origin)
export async function world({ installed = true, stubOptions = {}, seedFiles = {} } = {}) {
  const dir = mkdtempSync(join(tmpdir(), "app-push-"));
  const root = join(dir, "srv");
  const bare = join(root, "o", "r.git");
  mkdirSync(bare, { recursive: true });
  git(bare, "init", "-q", "--bare", "-b", "main");
  git(bare, "config", "http.receivepack", "true");
  const seed = join(dir, "seed");
  mkdirSync(join(seed, ".spire"), { recursive: true });
  cpSync(tool, join(seed, ".spire/tool"), { recursive: true });
  // the project's gate, a stub: it writes the environment it was run in (so a test can see what a child of the lander is given) and is green
  writeFileSync(join(seed, ".spire/tool/checks.sh"), '#!/bin/sh\nenv > "$APP_TEST_ENVOUT"\nexit 0\n');
  chmodSync(join(seed, ".spire/tool/checks.sh"), 0o755);
  writeFileSync(join(seed, "README.md"), "seed\n");
  git(seed, "init", "-q", "-b", "main");
  git(seed, "add", "-A");
  git(seed, "commit", "-qm", "seed");
  git(seed, "push", "-q", bare, "main");
  const stub = await startStub({ root, publicKey: KEY.pub, appId: APP_ID, token: TOKEN, repos: installed ? ["o/r"] : [], ...stubOptions });
  const clone = join(dir, "clone");
  git(dir, "clone", "-q", bare, clone);
  git(clone, "remote", "set-url", "origin", `${stub.url}/o/r.git`); // from here on, origin is the stub's http remote
  writeFileSync(join(clone, "change.txt"), "a change\n");
  git(clone, "add", "-A");
  git(clone, "commit", "-qm", "a change");
  const envOut = join(dir, "gate-env.txt");
  const env = (extra = {}) => ({ ...process.env, ...gitEnv, [APP_ID_ENV]: APP_ID, [APP_KEY_ENV]: KEY.pem, [API_URL_ENV]: stub.url, [SERVER_URL_ENV]: stub.url, APP_TEST_ENVOUT: envOut, GIT_TRACE: "1", GIT_CURL_VERBOSE: "1", GIT_TRACE_CURL: "1", ...extra });
  const push = (extra) => run(join(clone, ".spire/tool/push"), [], { cwd: clone, env: env(extra) });
  const main_ = () => git(bare, "rev-parse", "main");
  return { dir, bare, clone, stub, env, push, envOut, main_, done: async () => { await stub.close(); rmSync(dir, { recursive: true, force: true }); } };
}
// the stub is in THIS process: a child that talks to it must be started asynchronously (a sync spawn would block the stub it waits for)
export const run = (cmd, args, opts) => new Promise((ok) => {
  const c = spawn(cmd, args, opts);
  let stdout = "", stderr = "";
  c.stdout.on("data", (d) => { stdout += d; });
  c.stderr.on("data", (d) => { stderr += d; });
  c.on("close", (status) => ok({ status, stdout, stderr }));
});
export const dropped = (extra) => { const e = { ...process.env, ...extra }; for (const k of [APP_ID_ENV, APP_KEY_ENV]) if (!(k in extra)) delete e[k]; return e; };

