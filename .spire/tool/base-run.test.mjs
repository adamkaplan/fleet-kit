// `base run` leaves no workroom container when the land tool stops it: SIGTERM, SIGINT or SIGHUP to its process group. The real base.mjs
// runs against a fake docker (fixtures/base-world.mjs); the command runs as a child, so the signal handlers run (spawnSync held them).
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { existsSync, readdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test, { after } from "node:test";
import { scratch, world } from "./fixtures/base-world.mjs";

const base = join(fileURLToPath(import.meta.url), "../base.mjs");
after(() => scratch.forEach((d) => rmSync(d, { recursive: true, force: true })));

test("a base run whose command cannot be started says so and does not die with an unhandled error", () => {
  const w = world();
  w.lock(1);
  assert.equal(w.cli("build").status, 0);
  const r = spawnSync(process.execPath, [base, "run", "--", "true"], { cwd: w.project, env: { ...w.env, FAKE_VANISH: "1" }, encoding: "utf8" });
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /base: cannot run the command: /);
  assert.doesNotMatch(r.stderr, /Unhandled 'error' event/);
});

for (const [sig, code] of [["SIGTERM", 143], ["SIGINT", 130], ["SIGHUP", 129]]) {
  test(`a base run stopped by ${sig} (the land tool's stop, to the whole process group) leaves no workroom container`, async () => {
    const w = world();
    w.lock(1);
    assert.equal(w.cli("build").status, 0);
    const child = spawn(process.execPath, [base, "run", "--", "sleep", "60"], { cwd: w.project, env: w.env, detached: true, stdio: "ignore" });
    const ended = new Promise((resolve) => child.on("exit", (status) => resolve(status)));
    for (let i = 0; i < 400 && !existsSync(join(w.state, "at-exec")); i += 1) {
      await new Promise((r) => setTimeout(r, 50));
    }
    assert.ok(existsSync(join(w.state, "at-exec")), "the command must be running in the workroom");
    assert.equal(readdirSync(join(w.state, "container")).length, 1, "the workroom is up");
    process.kill(-child.pid, sig);
    assert.equal(await ended, code);
    assert.deepEqual(readdirSync(join(w.state, "container")), []);
  });
}
