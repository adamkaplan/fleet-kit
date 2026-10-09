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

// ---- issue 105: read-only enforcement for a role (policy.enforce_roles)
import { makeHandler, requestOfUnlisted, MODE_TTL_MS } from "./index.js";

// A fake spawn: `replyFor(request)` is the JSON object the CLI prints (or null: no answer). Records every request.
function fakeSpawn(replyFor, seen) {
  return (_bin, _args, _opts) => {
    const handlers = {};
    const stdout = { on: (n, f) => { handlers["stdout." + n] = f; }, destroy() {} };
    const child = {
      stdout,
      stdin: { on() {}, end(text) {
        const request = JSON.parse(text);
        seen.push(request);
        const reply = replyFor(request);
        setImmediate(() => { if (reply) handlers["stdout.data"]?.(JSON.stringify(reply) + "\n"); else handlers.close?.(); });
      } },
      on: (n, f) => { handlers[n] = f; },
      kill() {}, unref() {},
    };
    return child;
  };
}

const input = (action, effect = "allow", sessionID = "ses_x", resources = ["x"]) => ({ action, effect, sessionID, resources });

test("a session that is not enforced: only the listed actions are forwarded, one probe, and the configured outcome stands", async () => {
  const seen = [];
  const handler = makeHandler({ bin: "/x", timeoutMs: 500, spawnFn: fakeSpawn((r) => ({ outcome: r.outcome, reason: "", judged: false }), seen) });
  const unlisted = input("somemcp_write");
  await handler(unlisted);
  assert.equal(unlisted.effect, "allow");
  assert.deepEqual(seen.map((r) => r.action ?? r.tool), ["probe"], "the probe asked once; the unlisted action was not forwarded");
  await handler(input("shell"));
  await handler(input("read"));
  assert.deepEqual(seen.map((r) => r.action ?? r.tool), ["probe", "shell"]);
});

test("an enforced session: every action is forwarded and an ask from the daemon is applied", async () => {
  const seen = [];
  const replyFor = (r) => r.action === "probe" ? { outcome: r.outcome, enforced: true, judged: false }
    : { outcome: r.action === "read" ? "allow" : "ask", reason: "Needs your yes", enforced: true, judged: true };
  const handler = makeHandler({ bin: "/x", timeoutMs: 500, spawnFn: fakeSpawn(replyFor, seen) });
  const write = input("somemcp_write");
  await handler(write);
  assert.equal(write.effect, "ask");
  assert.equal(write.message, "Needs your yes");
  const read = input("read");
  await handler(read);
  assert.equal(read.effect, "allow");
  assert.deepEqual(seen.map((r) => r.action ?? r.tool), ["probe", "somemcp_write", "read"]);
  assert.equal(seen[1].tool, "other");
});

test("an enforced session fails to ask when the judge does not answer, and never loosens a configured ask or deny", async () => {
  const seen = [];
  let down = false;
  const replyFor = (r) => r.action === "probe" ? { outcome: r.outcome, enforced: true } : (down ? null : { outcome: "allow", enforced: true });
  const handler = makeHandler({ bin: "/x", timeoutMs: 500, spawnFn: fakeSpawn(replyFor, seen) });
  await handler(input("shell"));   // learns the session is enforced
  down = true;
  const a = input("shell");
  await handler(a);
  assert.equal(a.effect, "ask");
  const b = input("somemcp_write", "deny");
  await handler(b);
  assert.equal(b.effect, "deny", "a configured deny is never loosened");
  const c = input("edit", "ask");
  await handler(c);
  assert.equal(c.effect, "ask");
});

test("a session whose probe gets no answer follows the configured rules, as before", async () => {
  const seen = [];
  const handler = makeHandler({ bin: "/x", timeoutMs: 200, spawnFn: fakeSpawn(() => null, seen) });
  const a = input("somemcp_write");
  await handler(a);
  assert.equal(a.effect, "allow");
});

test("the mode of a session is asked again after its time to live", async () => {
  const seen = [];
  let at = 1_000;
  const handler = makeHandler({ bin: "/x", timeoutMs: 500, now: () => at, spawnFn: fakeSpawn((r) => ({ outcome: r.outcome }), seen) });
  await handler(input("shell"));
  await handler(input("shell"));
  assert.equal(seen.filter((r) => r.action === "probe").length, 1);
  at += MODE_TTL_MS + 1;
  await handler(input("shell"));
  assert.equal(seen.filter((r) => r.action === "probe").length, 2);
});

test("an unlisted action goes as tool other with a sanitised action name", () => {
  assert.deepEqual(requestOfUnlisted({ sessionID: "ses_x", action: "mcp_write", effect: "allow", resources: ["a", 3, "b"] }),
    { sessionID: "ses_x", tool: "other", action: "mcp_write", arguments: "a\nb", outcome: "allow" });
  assert.equal(requestOfUnlisted({ sessionID: "ses_x", action: "a b;rm", effect: "allow" }).action, "unknown");
});
