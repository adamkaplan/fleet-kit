// Admission: planSteps turns an area of areas.json (a kind, `tests` globs) into the steps of its kind's recipe, against a real git tree: {tests} names the area's own test globs, a step marked `optional`
// whose list is empty is skipped (a recipe asks for the live and PostgreSQL files of every area; most areas have none), and one that is not optional and lists nothing is still a fault of the project's file.
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { spawnSync } from "node:child_process";
import { planSteps } from "./plan.mjs";

const git = (dir, ...a) => assert.equal(spawnSync("git", a, { cwd: dir, encoding: "utf8" }).status, 0, a.join(" "));
function tree(files) {
  const dir = mkdtempSync(join(tmpdir(), "plan-areas-"));
  git(dir, "init", "-q");
  for (const f of files) {
    mkdirSync(dirname(join(dir, f)), { recursive: true });
    writeFileSync(join(dir, f), "");
  }
  git(dir, "add", "-A");
  git(dir, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x");
  return dir;
}

const recipes = {
  server: [
    { id: "tests:{name}", dir: ".", select: "reach", list: ["git", "ls-files", "--", ":(glob){tests}"], cmd: ["node", "--test"], exclude: ["**/*-live*"] },
    { id: "tests:{name}-live", dir: ".", optional: true, list: ["bash", "-c", "git ls-files -- \"${@/#/:(glob)}\" | grep -e '-live' || true", "bash", "{tests}"], cmd: ["node", "--test"] },
    { id: "tests:{name}-pg", dir: ".", optional: true, list: ["bash", "-c", "git ls-files -- \"${@/#/:(glob)}\" | grep -e '-pg' || true", "bash", "{tests}"], cmd: ["node", "--test"] },
    { id: "arch", first: true, dir: ".", cmd: ["true"] },
  ],
};
const areas = [
  { name: "acp", kind: "server", paths: ["server/src/acp/**"], tests: ["server/tests/acp/**"] },
  { name: "chat", kind: "server", paths: ["server/src/chat/**"], tests: ["server/tests/chat/**"] },
];

test("a recipe's {tests} lists the area's own test files, an empty optional step is skipped, the kind rides on every step", () => {
  const dir = tree(["server/tests/acp/a.test.ts", "server/tests/acp/b-live.test.ts", "server/tests/chat/c.test.ts"]);
  const was = process.cwd();
  process.chdir(dir);
  try {
    const plan = (names) => planSteps({ areas, recipes }, { areas: { names } }, { base: "HEAD" }).steps;
    const acp = plan(["acp"]);
    assert.deepEqual(acp.map((s) => s.id), ["tests:acp", "tests:acp-live", "arch"]);
    assert.deepEqual(acp[0].files, ["server/tests/acp/a.test.ts"]); // the -live file is the live step's
    assert.deepEqual(acp[1].files, ["server/tests/acp/b-live.test.ts"]);
    assert.ok(acp.every((s) => s.kind === "server" && s.area === "acp"));
    // two areas of one kind share `arch` once; each has its own tests
    assert.deepEqual(plan(["acp", "chat"]).map((s) => s.id), ["tests:acp", "tests:acp-live", "arch", "tests:chat"]);
  } finally {
    process.chdir(was);
    rmSync(dir, { recursive: true, force: true });
  }
});

test("a step that is not optional and lists no file is still an error naming the area", () => {
  const dir = tree(["server/tests/chat/c.test.ts"]);
  const was = process.cwd();
  process.chdir(dir);
  try {
    assert.throws(() => planSteps({ areas, recipes }, { areas: { names: ["acp"] } }, { base: "HEAD" }), /area acp: step tests:acp lists no test file/);
  } finally {
    process.chdir(was);
    rmSync(dir, { recursive: true, force: true });
  }
});
