// node --test plugins/fleet-hooks/index.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import plugin, { derivedPaths, resolveSettings, makeShellHook, makeTmpResolver, ensureAgentTmp, headlessNameOf, tmpRootOf } from "./index.js";

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

test("setup wires the tally into the evaluate hook, and only when judging is configured", async () => {
  const { home } = fakeHome();
  const seen = [];
  const tally = { count: (s) => seen.push(s), flush: async () => false };
  let evaluate;
  const api = { shell: { hook() {} }, permission: { hook: (name, fn) => { evaluate = fn; } } };
  await plugin.setup(api, {}, { home, tally });
  await evaluate({ sessionID: "ses_w", action: "read", effect: "allow" });
  assert.deepEqual(seen, ["ses_w"]);
  const off = [];
  await plugin.setup({ permission: { hook: () => off.push(1) }, shell: { hook() {} } }, {}, { home: fs.mkdtempSync(path.join(os.tmpdir(), "fleet-hooks-")), tally });
  assert.deepEqual(off, []); // no switchboard found: nothing registered, nothing counted
});

test("the tally's temporary file has an unpredictable name and is created exclusively", async () => {
  const names = [];
  const flags = [];
  const mk = () => makeTally({ dir: "/d", now: () => 1000000, writeFile: async (f, c, o) => { names.push(f); flags.push(o.flag); }, rename: async () => {} });
  await mk().flush();
  await mk().flush();
  assert.notEqual(names[0], names[1]);
  assert.deepEqual(flags, ["wx", "wx"]);
});

import { stateDirOf } from "./index.js";
test("stateDirOf: an absolute XDG_STATE_HOME wins; a relative or unset one falls back to the home", () => {
  assert.equal(stateDirOf({ XDG_STATE_HOME: "/xdg/state" }, "/home/user"), "/xdg/state/fleet-switchboard");
  assert.equal(stateDirOf({ XDG_STATE_HOME: "relative" }, "/home/user"), "/home/user/.local/state/fleet-switchboard");
  assert.equal(stateDirOf({}, "/home/user"), "/home/user/.local/state/fleet-switchboard");
});

test("a failed rename removes the temporary file", async () => {
  const removed = [];
  const tally = makeTally({ dir: "/d", now: () => 1000000, writeFile: async () => {}, rename: async () => { throw new Error("EXDEV"); }, unlink: async (f) => removed.push(f) });
  assert.equal(await tally.flush(), false);
  assert.equal(removed.length, 1);
  assert.match(removed[0], /tool-rate\.json\.\d+\.[0-9a-f]{12}\.tmp$/);
});

// ---- #290 slice 6: the plugin's pushes
import { makeEventSender, eventOf, sessionOf, wireEvents, EVENT_QUEUE_MAX } from "./index.js";

function fakeSocket({ fail = false, written }) {
  const handlers = {};
  const socket = {
    on: (name, fn) => { handlers[name] = fn; return socket; },
    end: (text, cb) => { written.push(text); cb && cb(); },
    destroy() {},
  };
  setImmediate(() => (fail ? handlers.error && handlers.error(new Error("ENOENT")) : handlers.connect && handlers.connect()));
  return socket;
}
// Wait for a condition by yielding to the event loop; the iteration cap is only a failure bound, never what orders the test.
async function until(condition) {
  for (let n = 0; n < 10000 && !condition(); n += 1) await new Promise((resolve) => setImmediate(resolve));
}
const tick = () => until(() => false || true);

test("1000 events with no socket return at once, throw nothing, and the first later message carries the count dropped", async () => {
  const written = [];
  let failing = true;
  const sender = makeEventSender({ dir: "/nowhere", connect: () => fakeSocket({ fail: failing, written }) });
  for (let n = 0; n < 1000; n += 1) assert.doesNotThrow(() => sender.push("idle", "ses_a"));   // every call returned without waiting for any socket
  await until(() => sender.dropped() >= 1000);   // the queue-full drops and the failed connections are all counted
  failing = false;
  sender.push("idle", "ses_a");
  await until(() => written.length >= 1);
  assert.equal(written.length >= 1, true);
  const last = JSON.parse(written[written.length - 1]);
  assert.equal(last.dropped, 1000, "the next message carries the number of events dropped");
  assert.equal(sender.dropped(), 0, "the count was told, so it is owed no more");
});

test("a message holds only the fixed fields: no text, and a bad session or event name is never sent", async () => {
  const written = [];
  const sender = makeEventSender({ dir: "/nowhere", connect: () => fakeSocket({ written }) });
  assert.equal(sender.push("prompt", "ses_b", { person: true, id: "msg_1", text: "SECRET PROMPT" }), true);
  assert.equal(sender.push("nonsense", "ses_b"), false);
  assert.equal(sender.push("idle", "not-a-session"), false);
  await until(() => written.length >= 1);
  assert.equal(written.length, 1);
  const message = JSON.parse(written[0]);
  assert.deepEqual(Object.keys(message).sort(), ["at", "event", "id", "kind", "person", "sessionID", "v"]);
  assert.ok(!written[0].includes("SECRET"));
});

test("only the facts named are pushed: a compaction hook, an idle event and a user message; anything else pushes nothing", async () => {
  const pushed = [];
  const sender = { push: (...args) => pushed.push(args) };
  let compaction;
  const events = [
    { type: "session.idle", data: { sessionID: "ses_c" } },
    { type: "message.created", data: { sessionID: "ses_c", role: "user", id: "msg_9" } },
    { type: "message.created", data: { sessionID: "ses_c", role: "assistant", id: "msg_8" } },
    { type: "session.deleted", data: { sessionID: "ses_c" } },
  ];
  const api = {
    session: { hook: (name, fn) => { if (name === "compaction") compaction = fn; } },
    event: { subscribe: async function* () { yield* events; } },
  };
  wireEvents(api, sender);
  compaction({ sessionID: "ses_c" });
  await until(() => pushed.length >= 3);
  assert.deepEqual(pushed.map((p) => p[0]).sort(), ["compaction", "idle", "prompt"]);
  assert.equal(eventOf({ type: "session.updated" }), null);
  assert.equal(sessionOf({ durable: { aggregateID: "ses_d" } }), "ses_d");
});

test("setup with an api that offers none of it, or with events switched off, registers nothing new", async () => {
  const { home } = fakeHome();
  const made = [];
  const sender = { push: (...a) => made.push(a) };
  await plugin.setup({ shell: { hook() {} }, permission: { hook() {} } }, {}, { home, sender });
  assert.deepEqual(made, []);
  let hooked = 0;
  await plugin.setup({ session: { hook: () => { hooked += 1; } }, shell: { hook() {} }, permission: { hook() {} } }, { FLEET_SWITCHBOARD_PLUGIN_EVENTS: "off" }, { home, sender });
  assert.equal(hooked, 0);
});

// ---- #846: a private TMPDIR for a launched headless worker, through the shell environment
function tmpWorld(workers = {}) {
  const base = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "fleet-hooks-tmp-")));
  const stateDir = path.join(base, "state");
  const root = path.join(base, "tmp-root");
  fs.mkdirSync(path.join(stateDir, "headless"), { recursive: true, mode: 0o700 });
  for (const [name, session] of Object.entries(workers)) {
    fs.writeFileSync(path.join(stateDir, "headless", name + ".json"), JSON.stringify({ worker: name, session, kind: "local", url: null, created: 1 }), { mode: 0o600 });
  }
  const lines = [];
  const tmpFor = makeTmpResolver({ stateDir, root, warn: (l) => lines.push(l) });
  const hook = makeShellHook("/kit/shims", tmpFor);
  const run = (env) => { const spec = { command: "true", env }; hook(spec); return spec.env; };
  return { base, stateDir, root, lines, run, tmpFor };
}
const baseEnv = (session) => ({ PATH: "/usr/bin", HOME: "/home/user", TMPDIR: "/var/shared", LANG: "C", ...(session ? { OPENCODE_SESSION_ID: session } : {}) });

test("S1: two launched headless workers get two different private directories, mode 0700", () => {
  const w = tmpWorld({ alpha: "ses_aaa1", beta: "ses_bbb2" });
  const a = w.run(baseEnv("ses_aaa1")).TMPDIR;
  const b = w.run(baseEnv("ses_bbb2")).TMPDIR;
  assert.equal(a, path.join(w.root, "alpha"));
  assert.equal(b, path.join(w.root, "beta"));
  assert.notEqual(a, b);
  for (const d of [a, b]) assert.equal(fs.statSync(d).mode & 0o777, 0o700);
  assert.equal(w.run(baseEnv("ses_aaa1")).TMPDIR, a); // a second command: the same directory
  assert.deepEqual(w.lines, []);
});

test("S2: a terminal worker, an unlaunched session and every other variable are unchanged", () => {
  const w = tmpWorld({ alpha: "ses_aaa1" });
  const withShims = (env) => ({ ...env, PATH: "/kit/shims:/usr/bin" });
  const terminal = { ...baseEnv("ses_aaa1"), FLEET_AGENT_TMPDIR: "/own/dir" }; // has a record, but the pane's own TMPDIR wins
  assert.deepEqual(w.run(terminal), withShims(terminal));
  const stranger = baseEnv("ses_zzz9");
  assert.deepEqual(w.run(stranger), withShims(stranger));
  const none = baseEnv(null);
  assert.deepEqual(w.run(none), withShims(none));
  const headless = w.run({ ...baseEnv("ses_aaa1"), EXTRA: "x" });
  const expected = { ...baseEnv("ses_aaa1"), EXTRA: "x", PATH: "/kit/shims:/usr/bin", TMPDIR: path.join(w.root, "alpha") };
  assert.deepEqual(headless, expected); // only TMPDIR differs from the shims-only environment
});

test("S3 and S7: a directory that cannot be made, or a symlink at the path, keeps the shared TMPDIR, says it once, never throws", () => {
  const w = tmpWorld({ alpha: "ses_aaa1", beta: "ses_bbb2" });
  fs.writeFileSync(w.root, "a file where the root should be"); // mkdir under it fails
  assert.equal(w.run(baseEnv("ses_aaa1")).TMPDIR, "/var/shared");
  assert.equal(w.run(baseEnv("ses_aaa1")).TMPDIR, "/var/shared");
  assert.equal(w.lines.length, 1); // one line per session, not one per command
  assert.match(w.lines[0], /shared temp directory/);
  fs.unlinkSync(w.root);
  fs.mkdirSync(w.root);
  const elsewhere = fs.mkdtempSync(path.join(w.base, "elsewhere-"));
  fs.symlinkSync(elsewhere, path.join(w.root, "beta"));
  const spec = { command: "true", env: baseEnv("ses_bbb2") };
  assert.doesNotThrow(() => makeShellHook("/kit/shims", w.tmpFor)(spec));
  assert.equal(spec.env.TMPDIR, "/var/shared");
  assert.match(spec.env.PATH, /^\/kit\/shims/); // the command still runs, with its shims
  assert.equal(w.lines.length, 2);
  assert.deepEqual(fs.readdirSync(elsewhere), []); // nothing written through the link
  assert.ok(fs.lstatSync(path.join(w.root, "beta")).isSymbolicLink());
});

test("S4: a hostile agent name is refused and creates nothing", () => {
  const w = tmpWorld();
  for (const name of ["a/b", "../evil", "..", ".", "a\0b", "a".repeat(200), "", "Upper", "cos", "planrev-x"]) {
    assert.throws(() => ensureAgentTmp(w.root, name), undefined, JSON.stringify(name));
  }
  assert.equal(fs.existsSync(w.root) ? fs.readdirSync(w.root).length : 0, 0);
  assert.equal(fs.existsSync(path.join(w.base, "evil")), false);
  // a record file or a record's content cannot smuggle a name either
  fs.writeFileSync(path.join(w.stateDir, "headless", "bad.json"), JSON.stringify({ worker: "../evil", session: "ses_x1", kind: "local" }), { mode: 0o600 });
  assert.equal(headlessNameOf(w.stateDir, "ses_x1"), null);
});

test("S7: a command through the hook completes with its own environment even when the directory fails", () => {
  const w = tmpWorld({ alpha: "ses_aaa1" });
  fs.writeFileSync(w.root, "x");
  const spec = { command: "exit 3", cwd: "/", env: baseEnv("ses_aaa1") };
  makeShellHook("/kit/shims", w.tmpFor)(spec);
  assert.equal(spec.command, "exit 3"); // nothing is blocked or rewritten
  assert.equal(spec.env.TMPDIR, "/var/shared");
});

test("S8: a session the switchboard did not launch creates no directory and keeps TMPDIR", () => {
  const w = tmpWorld({ alpha: "ses_aaa1" });
  const env = baseEnv("ses_other");
  assert.equal(w.run(env).TMPDIR, "/var/shared");
  assert.equal(fs.existsSync(w.root), false);
  // a record that is not private, not ours-by-name or a symlink is not a launch record
  fs.chmodSync(path.join(w.stateDir, "headless", "alpha.json"), 0o644);
  assert.equal(w.run(baseEnv("ses_aaa1")).TMPDIR, "/var/shared");
  assert.equal(fs.existsSync(w.root), false);
});

test("U1: the plugin wires the shell hook with the resolver, and an api-less resolver leaves PATH handling as before", async () => {
  const hooks = {};
  const w = tmpWorld({ alpha: "ses_aaa1" });
  await plugin.setup({ shell: { hook: (n, f) => { hooks[n] = f; } }, permission: { hook() {} } }, { FLEET_SWITCHBOARD_SHIMS: "/kit/shims" }, { home: w.base, sender: null, tmpFor: w.tmpFor });
  const spec = { env: baseEnv("ses_aaa1") };
  hooks["create.before"](spec);
  assert.equal(spec.env.TMPDIR, path.join(w.root, "alpha"));
  assert.equal(spec.env.PATH, "/kit/shims:/usr/bin");
  assert.equal(tmpRootOf({ XDG_DATA_HOME: "/data" }, "/h"), "/data/fleet-switchboard/profile/tmp");
  assert.equal(tmpRootOf({}, "/h"), "/h/.local/share/fleet-switchboard/profile/tmp");
  const off = { env: baseEnv("ses_aaa1") };
  makeShellHook("/kit/shims")(off); // no resolver: the old behaviour, byte for byte
  assert.deepEqual(off.env, { ...baseEnv("ses_aaa1"), PATH: "/kit/shims:/usr/bin" });
});

test("#846: a retired worker's directory is not made again, a loose directory is repaired, and bad records are not launch records", () => {
  const w = tmpWorld({ alpha: "ses_aaa1", cos: "ses_ccc3" });
  const dir = path.join(w.root, "alpha");
  fs.mkdirSync(dir, { recursive: true, mode: 0o755 });
  fs.chmodSync(dir, 0o755);
  assert.equal(w.run(baseEnv("ses_aaa1")).TMPDIR, dir);
  assert.equal(fs.statSync(dir).mode & 0o777, 0o700); // repaired
  fs.rmdirSync(dir);
  fs.unlinkSync(path.join(w.stateDir, "headless", "alpha.json")); // what retire does
  assert.equal(w.run(baseEnv("ses_aaa1")).TMPDIR, "/var/shared");
  assert.equal(fs.existsSync(dir), false);
  assert.equal(w.run(baseEnv("ses_ccc3")).TMPDIR, "/var/shared"); // the Chief of Staff keeps the shared directory
  assert.equal(fs.existsSync(path.join(w.root, "cos")), false);
  fs.writeFileSync(path.join(w.stateDir, "headless", "big.json"), JSON.stringify({ worker: "big", session: "ses_big1", kind: "local", pad: "x".repeat(9000) }), { mode: 0o600 });
  assert.equal(headlessNameOf(w.stateDir, "ses_big1"), null);
  const link = tmpWorld();
  const real = path.join(link.base, "real-headless");
  fs.renameSync(path.join(link.stateDir, "headless"), real);
  fs.symlinkSync(real, path.join(link.stateDir, "headless"));
  fs.writeFileSync(path.join(real, "sly.json"), JSON.stringify({ worker: "sly", session: "ses_sly1", kind: "local" }), { mode: 0o600 });
  assert.equal(headlessNameOf(link.stateDir, "ses_sly1"), null);
});

test("#846: a session with no record is looked up again only after the negative window", () => {
  const w = tmpWorld();
  let t = 1000;
  const lines = [];
  const tmpFor = makeTmpResolver({ stateDir: w.stateDir, root: w.root, now: () => t, warn: (l) => lines.push(l) });
  assert.equal(tmpFor({ OPENCODE_SESSION_ID: "ses_late1" }), null);
  fs.writeFileSync(path.join(w.stateDir, "headless", "late.json"), JSON.stringify({ worker: "late", session: "ses_late1", kind: "local" }), { mode: 0o600 });
  assert.equal(tmpFor({ OPENCODE_SESSION_ID: "ses_late1" }), null); // inside the window
  t += 11000;
  assert.equal(tmpFor({ OPENCODE_SESSION_ID: "ses_late1" }), path.join(w.root, "late"));
});
