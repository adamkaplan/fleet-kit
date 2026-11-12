import test from "node:test";
import assert from "node:assert/strict";
import plugin, { clampSpec, noteOf, CAP_MS, NOTE } from "./index.js";

test("S2: six specs", () => {
  const cases = [[60000, 60000], [300000, 300000], [300001, 300000], [3600000, 300000], [undefined, 300000], [undefined, 300000]];
  for (const [given, want] of cases) {
    const spec = { command: "x", background: given === undefined };
    if (given !== undefined) spec.timeout = given;
    assert.equal(clampSpec(spec).timeout, want);
  }
});
test("S2: odd values and a background spec with a timeout", () => {
  for (const t of [0, -5, NaN, Infinity, "9", null]) assert.equal(clampSpec({ timeout: t }).timeout, CAP_MS);
  assert.equal(clampSpec({ background: true, timeout: 1000 }).timeout, 1000);
  assert.equal(clampSpec({ background: true, timeout: 9e9 }).timeout, CAP_MS);
});
test("hooks registered through the plugin", async () => {
  const hooks = {};
  await plugin.setup({ shell: { hook: (n, f) => { hooks[n] = f; } } });
  const spec = { timeout: 9e9 }; await hooks["create.before"](spec);
  assert.equal(spec.timeout, CAP_MS);
  const res = { output: "command timed out" }; await hooks["run.after"](res);
  assert.ok(res.output.endsWith(NOTE));
  await plugin.setup({ shell: { hook: (n, f) => { if (n === "run.after") throw new Error("no"); hooks.x = f; } } }); // an unknown hook name never breaks the clamp
  assert.ok(hooks.x);
  await plugin.setup({}); // no shell api: no throw
});
test("S4: note", () => {
  const out = noteOf("command timed out after 300000 ms");
  assert.ok(/300000|5 minutes/.test(NOTE) && NOTE.includes("cw-run"));
  assert.ok(out.endsWith(NOTE));
  assert.equal(noteOf("ok"), "ok");
  assert.equal(noteOf(out), out);
  assert.ok(!/process\.env|TOKEN|SECRET/.test(NOTE));
});
