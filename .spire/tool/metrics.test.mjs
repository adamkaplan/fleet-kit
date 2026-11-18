// Admission: the pure parts of the measurement record (metrics.mjs): the first red of a run is the EARLIEST step that ended red, in whole seconds since the run's start; a line is one write that ends in a newline and
// survives a half-written neighbour. It would fail if the first red were the first listed (not the first to end) or if a damaged line stopped the reading of the others. Oracle: step lines as run-steps.mjs writes them.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { appendLine, landRecord, pushRecord, stepsOf } from "./metrics.mjs";

test("the first red is the earliest step to end red, in seconds since the run's start; the others are listed with their state", () => {
  const lines = [
    { id: "tests:app", at: 1100, took_s: 100, state: "red" },
    { id: "lint", at: 1003.4, took_s: 3, state: "red" },
    { id: "style", at: 1001, took_s: 1, state: "ran" },
    { id: "build", at: 1002, took_s: null, state: "kept" },
  ];
  assert.deepEqual(stepsOf(lines, 1000).first_red, { step: "lint", s: 3 });
  assert.deepEqual(stepsOf(lines, 1000).steps.map((s) => s.state), ["red", "red", "ran", "kept"]);
  assert.equal(stepsOf([lines[2]], 1000).first_red, null);
  // a step that ends 2.7 s after a whole-second start is in second 2, never 3: the run's own total (whole seconds, floored) cannot be smaller than it
  assert.equal(stepsOf([{ id: "x", at: 1002.7, took_s: 1, state: "red" }], 1000).first_red.s, 2);
});

test("a record is a line of its own; a half-written line of a killed run never hides the ones around it", () => {
  const dir = mkdtempSync(join(tmpdir(), "metrics-"));
  try {
    const f = join(dir, "metrics.jsonl");
    appendLine(f, { a: 1 });
    writeFileSync(f, `${readFileSync(f, "utf8")}{"half":\n`, "utf8");
    appendLine(f, { b: 2 });
    assert.equal(readFileSync(f, "utf8").split("\n").filter(Boolean).length, 3);
    const steps = join(dir, "steps.jsonl");
    writeFileSync(steps, `{"id":"x","at":10,"took_s":1,"state":"red"}\n{"id":"y","at"\n`);
    const r = landRecord({ tmp: dir, run: "10-1", start: "9", exit: "13", result: "checks-failed", kt0: "9" }, 20);
    assert.equal(r.checks.first_red.s, 1);
    assert.equal(r.checks.steps.length, 1);
    assert.equal(r.wall_s, 11);
    assert.equal(r.commission, null);
    assert.equal(landRecord({ run: "1-1", start: "9", exit: "130" }, 20).result, "interrupted", "a run that ended with no result of its own was interrupted");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("a push line: the gate's seconds of each attempt, the first red, the second it landed; empty values are null, never a made-up number", () => {
  const r = pushRecord({ start: "100", land_run: "90-1", result: "landed", exit: "0", commits: "2", commit: "abc", attempts: "2", walls: "60,40", first_red: "null", landed_at: "260", gate_s: "40" }, 270);
  assert.deepEqual(r.walls, [60, 40]);
  assert.equal(r.first_red, null);
  assert.equal(r.total_s, 170);
  assert.equal(r.landed_at, 260);
  const red = pushRecord({ start: "100", result: "gate", exit: "13", attempts: "1", walls: "9", first_red: '{"step":"lint","s":3}', landed_at: "" }, 112);
  assert.deepEqual(red.first_red, { step: "lint", s: 3 });
  assert.equal(red.landed_at, null);
  assert.equal(red.land_run, null);
});
