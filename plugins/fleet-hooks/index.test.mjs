// node --test plugins/fleet-hooks/index.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import plugin, { derivedPaths, resolveSettings } from "./index.js";

// A fake home with ~/.local/bin/fleet-switchboard -> <checkout>/bin/fleet-switchboard and <checkout>/shims.
function fakeHome() {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "fleet-hooks-")));
  const checkout = path.join(root, "checkout");
  fs.mkdirSync(path.join(checkout, "bin"), { recursive: true });
  fs.mkdirSync(path.join(checkout, "shims"));
  fs.writeFileSync(path.join(checkout, "bin", "fleet-switchboard"), "#!/bin/sh\n");
  const home = path.join(root, "home");
  fs.mkdirSync(path.join(home, ".local", "bin"), { recursive: true });
  fs.symlinkSync(path.join(checkout, "bin", "fleet-switchboard"), path.join(home, ".local", "bin", "fleet-switchboard"));
  return { root, checkout, home };
}

test("unset variables fall back to the linked switchboard and its shims", () => {
  const { checkout, home } = fakeHome();
  const got = resolveSettings({}, { home });
  assert.equal(got.bin, path.join(checkout, "bin", "fleet-switchboard"));
  assert.equal(got.shims, path.join(checkout, "shims"));
});

test("set variables win, even over a working link", () => {
  const { home } = fakeHome();
  const got = resolveSettings({ FLEET_SWITCHBOARD_BIN: "/opt/x/bin/sb", FLEET_SWITCHBOARD_SHIMS: "/opt/x/shims" }, { home });
  assert.equal(got.bin, "/opt/x/bin/sb");
  assert.equal(got.shims, "/opt/x/shims");
});

test("a set but relative variable turns that part off; it is not replaced by the fallback", () => {
  const { home } = fakeHome();
  const got = resolveSettings({ FLEET_SWITCHBOARD_BIN: "rel/sb", FLEET_SWITCHBOARD_SHIMS: "rel/shims" }, { home });
  assert.equal(got.bin, null);
  assert.equal(got.shims, null);
});

test("only the unset one falls back", () => {
  const { checkout, home } = fakeHome();
  const got = resolveSettings({ FLEET_SWITCHBOARD_BIN: "/opt/x/bin/sb" }, { home });
  assert.equal(got.bin, "/opt/x/bin/sb");
  assert.equal(got.shims, path.join(checkout, "shims"));
});

test("no link, or a dangling one, leaves the plugin off", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "fleet-hooks-"));
  assert.deepEqual(derivedPaths(root), { bin: null, shims: null });
  fs.mkdirSync(path.join(root, ".local", "bin"), { recursive: true });
  fs.symlinkSync(path.join(root, "gone"), path.join(root, ".local", "bin", "fleet-switchboard"));
  assert.deepEqual(derivedPaths(root), { bin: null, shims: null });
});

test("setup registers both hooks with an empty environment", async () => {
  const { home } = fakeHome();
  const hooks = [];
  const api = {
    shell: { hook: (name) => hooks.push("shell." + name) },
    permission: { hook: (name) => hooks.push("permission." + name) },
  };
  await plugin.setup(api, {}, { home });
  assert.deepEqual(hooks.sort(), ["permission.evaluate", "shell.create.before"]);
});

test("setup registers nothing with an empty environment and no link", async () => {
  const hooks = [];
  const api = {
    shell: { hook: (name) => hooks.push(name) },
    permission: { hook: (name) => hooks.push(name) },
  };
  await plugin.setup(api, {}, { home: fs.mkdtempSync(path.join(os.tmpdir(), "fleet-hooks-")) });
  assert.deepEqual(hooks, []);
});

// ---- #113: shadow mode does not wait
import { EventEmitter } from "node:events";
import { makeHandler, SHADOW_MAX_INFLIGHT, MODE_TTL_MS } from "./index.js";

function fakeSpawn(replyFor) {
  const calls = [];
  const spawnFn = (bin, args, opts) => {
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stdout.destroy = () => {};
    child.stdin = new EventEmitter();
    child.stdin.end = (text) => {
      calls.push({ env: opts.env, request: JSON.parse(text) });
      const reply = replyFor(calls.length);
      if (reply === "hang") return;
      setImmediate(() => {
        child.stdout.emit("data", JSON.stringify(reply) + "\n");
        child.emit("close", 0);
      });
    };
    child.kill = () => child.emit("close", null);
    child.unref = () => {};
    return child;
  };
  return { spawnFn, calls };
}
const input = (sessionID = "ses_1") => ({ sessionID, action: "shell", resources: ["ls"], effect: "allow" });
const SHADOW = { outcome: "allow", judged: true, shadow: true, reason: "x" };

test("unknown mode awaits and enforces a deny", async () => {
  const { spawnFn } = fakeSpawn(() => ({ outcome: "deny", judged: true, reason: "no" }));
  const h = makeHandler({ bin: "/x", spawnFn });
  const i = input();
  await h(i);
  assert.equal(i.effect, "deny");
});

test("a shadow reply sets the mode; later calls return without waiting even if the judge hangs", async () => {
  let n = 0;
  const { spawnFn, calls } = fakeSpawn(() => (++n === 1 ? SHADOW : "hang"));
  const h = makeHandler({ bin: "/x", spawnFn });
  const first = input();
  await h(first);
  assert.equal(first.effect, "allow");
  const t = Date.now();
  await h(input());
  assert.ok(Date.now() - t < 100);
  assert.equal(calls.length, 2, "the second call was still sent to the judge");
});

test("an enforcing (judged, not shadow) reply keeps awaiting", async () => {
  const { spawnFn, calls } = fakeSpawn(() => ({ outcome: "allow", judged: true, reason: "ok" }));
  const h = makeHandler({ bin: "/x", spawnFn });
  await h(input());
  const i = input();
  await h(i);
  assert.equal(calls.length, 2);
});

test("the mode expires and the next call awaits again", async () => {
  let clock = 1000;
  let n = 0;
  const { spawnFn } = fakeSpawn(() => (++n === 1 ? SHADOW : { outcome: "deny", judged: true, reason: "no" }));
  const h = makeHandler({ bin: "/x", spawnFn, now: () => clock });
  await h(input());
  clock += MODE_TTL_MS + 1;
  const i = input();
  await h(i);
  assert.equal(i.effect, "deny");
});

test("over the cap calls are dropped, counted, never blocked, and the next child is told", async () => {
  const { spawnFn, calls } = fakeSpawn((n) => (n === 1 ? SHADOW : "hang"));
  const state = {};
  const h = makeHandler({ bin: "/x", spawnFn, state });
  await h(input());
  for (let k = 0; k < SHADOW_MAX_INFLIGHT + 3; k++) await h(input());
  assert.equal(state.dropped, 3);
  assert.equal(state.inflight, SHADOW_MAX_INFLIGHT);
  state.inflight = 0; // the hung ones finish
  await h(input());
  assert.equal(calls.at(-1).env.FLEET_SWITCHBOARD_DROPPED, "3");
  assert.equal(state.dropped, 0);
});

test("errors in shadow are swallowed", async () => {
  let n = 0;
  const spawnFn = (...a) => {
    if (++n > 1) throw new Error("boom");
    return fakeSpawn(() => SHADOW).spawnFn(...a);
  };
  const h = makeHandler({ bin: "/x", spawnFn });
  await h(input());
  await h(input());
});

test("the mode is per session: another session's shadow reply does not make this one async", async () => {
  let n = 0;
  const { spawnFn, calls } = fakeSpawn(() => (++n === 1 ? SHADOW : { outcome: "deny", judged: true, reason: "no" }));
  const h = makeHandler({ bin: "/x", spawnFn });
  await h(input("ses_a"));
  const b = input("ses_b");
  await h(b);
  assert.equal(b.effect, "deny", "ses_b had no cached mode: it awaited and was enforced");
  assert.equal(calls.length, 2);
});

test("a flip is told to the next child once", async () => {
  let clock = 1000;
  let n = 0;
  const { spawnFn, calls } = fakeSpawn(() => (++n === 1 ? SHADOW : { outcome: "allow", judged: true, reason: "ok" }));
  const state = {};
  const h = makeHandler({ bin: "/x", spawnFn, now: () => clock, state });
  await h(input());
  clock += MODE_TTL_MS + 1;
  await h(input()); // enforcing now: a flip
  await h(input());
  const told = calls.map((c) => c.env.FLEET_SWITCHBOARD_MODE_FLIPS).filter(Boolean);
  assert.deepEqual(told, ["ses_1:shadow>enforcing"]);
});

// ---- #357: approximate tool-call tally for the health view
import { makeTally, TALLY_MINUTES, TALLY_SESSIONS, TALLY_HEARTBEAT_MS } from "./index.js";

test("every evaluation is counted per session and minute, judged or not", async () => {
  let clock = 60000 * 100;
  const tally = makeTally({ dir: "/nowhere", now: () => clock });
  const { spawnFn } = fakeSpawn(() => ({ outcome: "allow", reason: "ok" }));
  const h = makeHandler({ bin: "/x", spawnFn, tally, now: () => clock });
  for (let i = 0; i < 7; i++) await h({ ...input("ses_a"), action: "read" }); // an action the plugin does not forward
  clock += 60000;
  for (let i = 0; i < 2; i++) await h(input("ses_a"));
  await h(input("ses_b"));
  const body = tally.body();
  assert.deepEqual(body.sessions.ses_a, { 100: 7, 101: 2 });
  assert.deepEqual(body.sessions.ses_b, { 101: 1 });
});

test("the tally is bounded in minutes and sessions", () => {
  let clock = 0;
  const tally = makeTally({ dir: "/nowhere", now: () => clock });
  for (let i = 0; i < TALLY_SESSIONS + 50; i++) tally.count("ses_" + i);
  assert.equal(Object.keys(tally.body().sessions).length, TALLY_SESSIONS);
  tally.count("ses_old");
  clock += 60000 * (TALLY_MINUTES + 1);
  assert.equal(tally.body().sessions.ses_old, undefined);
});

test("a throwing or failing tally never changes the outcome", async () => {
  const tally = { count() { throw new Error("boom"); } };
  const { spawnFn } = fakeSpawn(() => ({ outcome: "deny", reason: "no", judged: true }));
  const h = makeHandler({ bin: "/x", spawnFn, tally });
  const i = input("ses_a");
  await h(i);
  assert.equal(i.effect, "deny");
});

test("flush writes only when something moved or the heartbeat is due, and survives a write error", async () => {
  let clock = 1000000;
  const writes = [];
  const tally = makeTally({ dir: "/d", now: () => clock, writeFile: async (f, c) => writes.push(f), rename: async () => {} });
  assert.equal(await tally.flush(), true); // first: heartbeat due
  assert.equal(await tally.flush(), false);
  tally.count("ses_a");
  assert.equal(await tally.flush(), true);
  clock += TALLY_HEARTBEAT_MS + 1;
  assert.equal(await tally.flush(), true);
  const bad = makeTally({ dir: "/d", now: () => clock, writeFile: async () => { throw new Error("ENOENT"); } });
  assert.equal(await bad.flush(), false);
});
