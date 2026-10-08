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
