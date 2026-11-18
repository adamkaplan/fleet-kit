// Admission: a replay of the REAL gate logs of spire-venue#8358 (tool/fixtures/gate-logs-8358/, from branch gate-logs-8358; the path names are replaced by neutral ones, the shape, the counts, the .spire/ file and the step ids are the real ones, so the tool names no product path, P12): the change's files and the incoming files of two rechecks, and the
// steps their gate.log ran. The landed rule (import reading) carried nothing there: every test step said `re-run: unsure: ...`. The rule now is path overlap plus what the incoming files reach (carry.mjs, reach.mjs).
// They would fail if the test steps of those two changes were not carried over the real incoming lists, if one file of the change among the incoming ones did not run everything, or if a cheap step were carried.
// Oracle: the real lists and the real step ids, fed to the one decision (carry.mjs decide).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { decide } from "./carry.mjs";
import { buildGraph } from "./reach.mjs";

const DIR = join(dirname(fileURLToPath(import.meta.url)), "fixtures/gate-logs-8358");
// the tree of a replayed change: the files its logs name, read as empty (no import, no name in a string): what the real lists reach is judged on the paths alone
const RUNNER = ".spire/tool/run-steps.mjs"; // tracked, and it names nothing: no file of the lists is one the gate reads
const graphOf = (...lists) => buildGraph({ files: [...new Set([...lists.flat(), RUNNER])], read: () => "", isProse: (f) => f.endsWith(".md") });

const lists = (name) => {
  const out = { change: [], incoming: [] };
  let into = null;
  for (const l of readFileSync(join(DIR, `${name}.changed-files.txt`), "utf8").split("\n")) {
    if (l.startsWith("## change vs")) into = out.change;
    else if (l.startsWith("## incoming")) into = out.incoming;
    else if (l.startsWith("##")) into = null;
    else if (l && into) into.push(l);
  }
  return out;
};
const stepsOf = (name) =>
  readFileSync(join(DIR, `${name}.gate.log`), "utf8")
    .split("\n")
    .map((l) => /^land: (?:carried|re-run) (\S+): /.exec(l))
    .filter(Boolean)
    .map((m) => ({ id: m[1], retry: /^tests:/.test(m[1]) ? "test" : "step" }));

const REAL = {
  "c-archive2-land2": ["tests:app", "tests:server", "tests:pg"],
  "c-areapaths-land9": ["tests:server", "tests:scripts"],
};

for (const [name, tests] of Object.entries(REAL)) {
  const { change, incoming } = lists(name);
  const steps = stepsOf(name);
  // the step's test list: the change's own test files plus two the change does not touch
  const ownTests = change.filter((f) => /\.test\.ts$/.test(f));
  const files = [...ownTests, "n7/n20/other1.test.ts", "n7/n20/other2.test.ts"];
  const run = (inc, ch = change) => steps.map((step) => ({ step, got: decide({ step, earlier: true, incoming: inc, changed: ch, files, graph: graphOf(change, incoming, files), runner: RUNNER, own: ".spire/" }) }));

  test(`${name}: the real lists are read (${change.length} change files, ${incoming.length} incoming) and have no overlap`, () => {
    assert.ok(change.length >= 2 && incoming.length >= 30, `${change.length}/${incoming.length}`);
    assert.deepEqual(incoming.filter((f) => change.includes(f)), []);
    assert.deepEqual(steps.filter((s) => s.retry === "test").map((s) => s.id), tests);
  });

  test(`${name}: its ${tests.length} test steps are carried over the real incoming list; the cheap steps run again`, () => {
    const rows = run(incoming);
    assert.deepEqual(rows.filter((r) => r.got.carry).map((r) => r.step.id), tests);
    for (const r of rows.filter((x) => x.step.retry !== "test")) assert.match(r.got.reason, /not a test step/);
  });

  test(`${name}: the real incoming list plus one file of the change re-runs everything (0 carried)`, () => {
    for (const file of change) {
      const rows = run([...incoming, file]);
      assert.equal(rows.filter((r) => r.got.carry).length, 0, file);
      for (const r of rows.filter((x) => x.step.retry === "test")) assert.match(r.got.reason, /came in and the change touches it too/);
    }
  });
}

for (const name of Object.keys(REAL)) {
  const { change, incoming } = lists(name);
  test(`${name}: the change's own test files re-run, the rest of the step carries`, () => {
    const ownTests = change.filter((f) => /\.test\.ts$/.test(f));
    assert.ok(ownTests.length >= 1);
    const files = [...ownTests, "n7/n20/other1.test.ts"];
    const d = decide({ step: { id: "tests:server", retry: "test" }, earlier: true, incoming, changed: change, files, graph: graphOf(change, incoming, files), runner: RUNNER, own: ".spire/" });
    assert.equal(d.carry, true);
    assert.deepEqual(d.rerun, ownTests);
  });
}
