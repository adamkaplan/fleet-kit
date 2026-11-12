import test from "node:test";
import assert from "node:assert/strict";
import plugin, { clampSpec, noteOf, CAP_MS, NOTE } from "./index.js";

test("S2: six specs", () => {
  const cases = [[{ timeout: 60000 }, 60000], [{ timeout: CAP_MS }, CAP_MS], [{ timeout: CAP_MS + 1 }, CAP_MS], [{ timeout: 12 * CAP_MS }, CAP_MS],
    [{ background: false }, CAP_MS], [{ background: true }, CAP_MS]];
  for (const [extra, want] of cases) assert.equal(clampSpec({ command: "x", ...extra }).timeout, want);
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
  const lines = []; const orig = console.log; console.log = (m) => lines.push(m);
  try {
    await plugin.setup({}); // no shell api: no throw
    await plugin.setup({ shell: { hook: () => { throw new Error("SECRET-TEXT"); } } });
  } finally { console.log = orig; }
  assert.deepEqual(lines, ["cw-tool-cap: shell hooks unavailable, calls are NOT capped", "cw-tool-cap: shell hooks unavailable, calls are NOT capped"]);
  lines.length = 0; console.log = (m) => lines.push(m);
  try { await plugin.setup({ shell: { hook: (n) => { if (n === "run.after") throw new Error("SECRET-TEXT"); } } }); } finally { console.log = orig; }
  assert.equal(lines.length, 1); assert.ok(!lines[0].includes("SECRET-TEXT"));
});
test("S4: note", () => {
  const out = noteOf("command timed out after 300000 ms");
  assert.ok(/300000|5 minutes/.test(NOTE) && NOTE.includes("cw-run"));
  assert.ok(out.endsWith(NOTE));
  assert.equal(noteOf("ok"), "ok");
  assert.equal(noteOf(out), out);
  assert.ok(!/process\.env|TOKEN|SECRET/.test(NOTE));
});
test("a hook that throws at call time does not crash the call", async () => {
  const hooks = {};
  await plugin.setup({ shell: { hook: (n, f) => { hooks[n] = f; } } });
  const spec = Object.freeze({ command: "x", timeout: 9e9 }); // assignment throws in strict mode
  const lines = []; const orig = console.log; console.log = (m) => lines.push(m);
  try { await hooks["create.before"](spec); } finally { console.log = orig; }
  assert.deepEqual(spec, { command: "x", timeout: 9e9 });
  assert.equal(lines.length, 1); assert.ok(lines[0].startsWith("cw-tool-cap:"));
});
