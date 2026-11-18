// Admission: this file shows that the lander pushes as the fleet's GitHub App when FLEET_APP_ID is set: it mints a short-lived installation token (a JWT signed with the key, exchanged at the API for a token of
// the one repository, contents:write) and pushes with it; that an app that is on and cannot be used says "app not configured" and stops, with nothing pushed and no other identity tried; and that neither the key
// nor the token is ever printed (git's trace on, a refusal, an error) and the key never reaches a child of the lander (the gate's own environment). It would fail if the mint, the push's use of the token, the stop,
// the masking or the scrub broke. Oracle: the real `push` of a copy of this tool against a stub of GitHub (fixtures/app-stub.mjs: the App's API, and `git http-backend` where a push needs the token's header).
// The key is generated here, once, for the test; no real key, token or endpoint is used.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { API_URL_ENV, APP_ID_ENV, APP_KEY_ENV, appMode } from "./app-env.mjs";
import { main, makeJwt, mask, mintToken, normalizeKey, parseRepo, pushEnv } from "./app-push.mjs";
import { BASIC, KEY, TOKEN, APP_ID, dropped, gitEnv, git, keypair, leaks, run, world } from "./fixtures/app-world.mjs";
import { stepEnv } from "./step-env.mjs";

const tool = fileURLToPath(new URL(".", import.meta.url));
test("mint: a JWT signed with the key (RS256, iat 60 s back, exp within 10 min, iss the app) finds the repository's installation and asks for a token of that one repository with contents:write only", async () => {
  const w = await world();
  try {
    const { token, secrets } = await mintToken({ env: { [APP_ID_ENV]: APP_ID, [APP_KEY_ENV]: KEY.pem, [API_URL_ENV]: w.stub.url }, owner: "o", repo: "r" });
    assert.equal(token, TOKEN);
    assert.deepEqual(w.stub.calls.jwtErrors, [], "the stub checked the signature, the issuer and the times");
    assert.deepEqual(w.stub.calls.api.map((c) => `${c.method} ${c.path}`), ["GET /repos/o/r/installation", "POST /app/installations/4242/access_tokens"]);
    assert.deepEqual(w.stub.calls.api[1].asked, { repositories: ["r"], permissions: { contents: "write" } });
    assert.ok(secrets.includes(TOKEN) && secrets.includes(KEY.pem));
    const jwt = makeJwt(APP_ID, KEY.pem, 1000);
    assert.deepEqual(JSON.parse(Buffer.from(jwt.split(".")[1], "base64url")), { iat: 940, exp: 1540, iss: APP_ID });
  } finally { await w.done(); }
});

test("mint: no id, no key, a key that is not a key, an app that is not installed, a key the app does not have: each is 'app not configured' or a plain refusal, and none prints the key", async () => {
  const w = await world({ installed: false });
  const other = keypair();
  try {
    const base = { [API_URL_ENV]: w.stub.url };
    const fails = async (env, re) => {
      const e = await mintToken({ env: { ...base, ...env }, owner: "o", repo: "r" }).then(() => null, (x) => x);
      assert.ok(e, "must fail");
      assert.match(e.message, re);
      assert.deepEqual(leaks(e.message), []);
    };
    await fails({ [APP_KEY_ENV]: KEY.pem }, /app not configured: FLEET_APP_ID is not set/);
    await fails({ [APP_ID_ENV]: APP_ID }, /app not configured: FLEET_APP_PRIVATE_KEY is missing or empty/);
    await fails({ [APP_ID_ENV]: APP_ID, [APP_KEY_ENV]: "  \n" }, /app not configured: FLEET_APP_PRIVATE_KEY is missing or empty/);
    await fails({ [APP_ID_ENV]: APP_ID, [APP_KEY_ENV]: "not a key" }, /app not configured: FLEET_APP_PRIVATE_KEY is not a usable RSA private key/);
    await fails({ [APP_ID_ENV]: APP_ID, [APP_KEY_ENV]: KEY.pem }, /app not configured: GitHub answered 404 for the installation of o\/r.*is the app installed there/);
    await fails({ [APP_ID_ENV]: APP_ID, [APP_KEY_ENV]: other.pem }, /GitHub answered 401/);
    assert.deepEqual(w.stub.calls.jwtErrors, ["bad signature"]);
    await fails({ [APP_ID_ENV]: APP_ID, [APP_KEY_ENV]: KEY.pem, [API_URL_ENV]: "http://127.0.0.1:1" }, /cannot reach 127\.0\.0\.1:1/);
  } finally { await w.done(); }
});

test("the pieces: app mode is the id alone; a key on one line with \\n escapes is repaired; the owner and repository of any remote form; mask removes the key, a token and a PEM", () => {
  assert.equal(appMode({}), false);
  assert.equal(appMode({ [APP_ID_ENV]: "  " }), false);
  assert.equal(appMode({ [APP_ID_ENV]: "1" }), true);
  assert.equal(appMode({ [APP_KEY_ENV]: "k" }), false, "a key without an id does not turn the app on (one switch)");
  assert.equal(normalizeKey(KEY.pem.trim().replaceAll("\n", "\\n")), KEY.pem);
  for (const u of ["git@github-janus:janus-infra/spire-land.git", "ssh://git@github.com/janus-infra/spire-land.git", "https://github.com/janus-infra/spire-land", "https://x@github.com/janus-infra/spire-land.git/"]) assert.deepEqual(parseRepo(u), { owner: "janus-infra", repo: "spire-land" }, u);
  const m = mask(`a ${TOKEN} b ${["ghs", "_ABCDEFGHIJKLMNOPQRSTUVWX"].join("")} ${KEY.pem} ${KEY.pem.split("\n")[1]}`, [TOKEN, KEY.pem]);
  assert.deepEqual(leaks(m), []);
  assert.doesNotMatch(m, /ghs_|BEGIN/);
  // the environment of the push: the header is in git's config variables (not argv, not a file), the key and the traces are gone, the caller's own GIT_CONFIG_* entries stay
  const e = pushEnv({ [APP_KEY_ENV]: "k", GIT_TRACE: "1", GIT_CURL_VERBOSE: "1", GIT_ASKPASS: "x", GIT_CONFIG_COUNT: "1", GIT_CONFIG_KEY_0: "user.name", GIT_CONFIG_VALUE_0: "n", PATH: "/bin" }, "https://github.com", TOKEN);
  assert.deepEqual([e[APP_KEY_ENV], e.GIT_TRACE, e.GIT_CURL_VERBOSE, e.GIT_ASKPASS], [undefined, undefined, undefined, undefined]);
  assert.deepEqual([e.GIT_CONFIG_COUNT, e.GIT_CONFIG_KEY_0, e.GIT_CONFIG_KEY_1, e.GIT_CONFIG_VALUE_1, e.GIT_CONFIG_KEY_2], ["3", "user.name", "http.https://github.com/.extraheader", BASIC.replace(/^/, "Authorization: "), "credential.helper"]);
});

test("push (the real lander, app mode on): the commit lands on main by the App's token, the stub saw exactly that credential, the gate's environment has no key, and nothing printed holds a secret (git's trace on)", async () => {
  const w = await world();
  try {
    const tip = git(w.clone, "rev-parse", "HEAD");
    const r = await w.push();
    const out = `${r.stdout}\n${r.stderr}`;
    assert.equal(r.status, 0, out);
    assert.equal(w.main_(), tip, "main of the remote is the pushed commit");
    const pushes = w.stub.calls.git.filter((c) => c.push);
    assert.ok(pushes.length >= 2, "the push went through the stub's git remote");
    assert.deepEqual([...new Set(pushes.map((c) => c.auth))], [BASIC], "the one credential given is the installation token's");
    assert.deepEqual(w.stub.calls.jwtErrors, []);
    assert.deepEqual(leaks(out), [], "no key, JWT or token in the output");
    assert.match(r.stdout, /^push: pushed as app stub-app by an installation token for o\/r \(repository_selection=selected repositories=r permissions=contents:write expires_at=\S+\); no human identity$/m);
    const gateEnv = readFileSync(w.envOut, "utf8");
    assert.doesNotMatch(gateEnv, /FLEET_APP_PRIVATE_KEY/);
    assert.deepEqual(leaks(gateEnv), [], "the gate (a child of the lander) never held the key");
    assert.match(gateEnv, /FLEET_APP_ID=5803159/, "the id is not a secret: it stays");
    // and the clone's own state holds nothing: no token in its configuration, no remote rewritten
    assert.deepEqual(leaks(readFileSync(join(w.clone, ".git/config"), "utf8")), []);
    assert.equal(git(w.clone, "remote", "get-url", "origin"), `${w.stub.url}/o/r.git`);
  } finally { await w.done(); }
});

test("push: app mode on and the key missing, or the app not installed, or a key the app does not know: 'app not configured', exit 1, BEFORE the gate, nothing pushed, no other identity tried", async () => {
  for (const [name, extra, re] of [
    ["no key", { [APP_KEY_ENV]: "" }, /app not configured: FLEET_APP_PRIVATE_KEY is missing or empty/],
    ["a bad key", { [APP_KEY_ENV]: "garbage" }, /app not configured: FLEET_APP_PRIVATE_KEY is not a usable RSA private key/],
    ["a key of another app", { [APP_KEY_ENV]: keypair().pem }, /GitHub answered 401/],
  ]) {
    const w = await world();
    try {
      const before = w.main_();
      const r = await w.push(extra);
      const out = `${r.stdout}\n${r.stderr}`;
      assert.equal(r.status, 1, `${name}: ${out}`);
      assert.match(out, /app not configured|GitHub answered 401/, name);
      assert.match(out, re, name);
      assert.match(out, /no fallback to another identity/, name);
      assert.equal(w.main_(), before, `${name}: nothing was pushed`);
      assert.deepEqual(w.stub.calls.git.filter((c) => c.push), [], `${name}: no push was even attempted (no ssh, no anonymous try)`);
      assert.throws(() => readFileSync(w.envOut), /ENOENT/, `${name}: the gate never ran`);
      assert.deepEqual(leaks(out), [], name);
    } finally { await w.done(); }
  }
  const w = await world({ installed: false });
  try {
    const r = await w.push();
    assert.equal(r.status, 1);
    assert.match(`${r.stdout}${r.stderr}`, /app not configured: GitHub answered 404.*is the app installed there/);
    assert.equal(w.stub.calls.git.filter((c) => c.push).length, 0);
  } finally { await w.done(); }
});

test("push: app mode OFF (no FLEET_APP_ID) is the host's own plain push, as before: no token is minted; a key without an id is said, never used", async () => {
  const w = await world();
  try {
    const r = await run(join(w.clone, ".spire/tool/push"), [], { cwd: w.clone, env: dropped({ ...gitEnv, [APP_KEY_ENV]: KEY.pem, APP_TEST_ENVOUT: w.envOut }) });
    const out = `${r.stdout}\n${r.stderr}`;
    assert.equal(r.status, 1, "this stub refuses an unauthenticated push: a plain refusal, not retried");
    assert.match(out, /FLEET_APP_PRIVATE_KEY is set but FLEET_APP_ID is not/);
    assert.deepEqual(w.stub.calls.api, [], "no token was asked for");
    assert.ok(w.stub.calls.git.some((c) => c.push && c.auth === null), "the plain git push went out with no App credential");
    assert.deepEqual(leaks(out), []);
  } finally { await w.done(); }
});

test("the key leaves the environment: app-key.sh takes it into an unexported variable, and a step never gets it even when it names it", () => {
  const sh = spawnSync("bash", ["-c", `. ${JSON.stringify(join(tool, "lib/app-key.sh"))}; app_key_take; echo "in-shell=\${fleet_app_key:+yes}"; env | grep -c ${APP_KEY_ENV}; env | grep -c fleet_app_key`], { env: { PATH: process.env.PATH, [APP_KEY_ENV]: "secret-material" }, encoding: "utf8" });
  assert.equal(sh.stdout, "in-shell=yes\n0\n0\n", "held by the shell, in no child's environment");
  assert.deepEqual(stepEnv({ PATH: "/bin", [APP_KEY_ENV]: "k", [APP_ID_ENV]: "1" }, [APP_KEY_ENV, APP_ID_ENV]), { PATH: "/bin", [APP_ID_ENV]: "1" });
});

test("main(): a bad command is exit 2; check is silent and 0 when the app is off", async () => {
  const lines = [];
  assert.equal(await main(["nope"], {}, (t) => lines.push(t)), 2);
  assert.equal(await main(["check", ".", "origin"], {}, (t) => lines.push(t)), 0);
  assert.equal(lines.length, 1);
});
