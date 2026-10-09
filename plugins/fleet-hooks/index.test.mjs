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
const input = () => ({ sessionID: "ses_1", action: "shell", resources: ["ls"], effect: "allow" });
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
  const state = { shadowUntil: 0, inflight: 0, dropped: 0 };
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
