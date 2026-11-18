// The base images leave nothing behind: a build that succeeds removes the older ones. The real base.mjs runs against a fake docker
// (fixtures/base-world.mjs). Oracle: after five builds with five lock files at most three workroom-base tags are left (the newest) and an
// image a container holds survives; an image whose age is unknown is kept; nothing without the label is ever offered to `rmi`, and `rmi`
// is never forced; a build that is already built prunes nothing.
import assert from "node:assert/strict";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import test, { after } from "node:test";
import { scratch, world } from "./fixtures/base-world.mjs";

after(() => scratch.forEach((d) => rmSync(d, { recursive: true, force: true })));

test("five builds with five lock files leave at most three base images of the project, the newest; an unlabelled image, another project's and one a container holds are not removed, and nothing is forced", () => {
  const w = world();
  // an image of another kind on the same host, and an older base held by a workroom
  mkdirSync(join(w.state, "image"), { recursive: true });
  writeFileSync(join(w.state, "image", "other0001"), JSON.stringify({ id: "other0001", ref: "other:latest", labels: [], created: "2020-01-01T00:00:00Z" }));
  // a base of ANOTHER project on the same host, older than everything: never this project's to remove
  writeFileSync(join(w.state, "image", "alien001"), JSON.stringify({ id: "alien001", ref: "workroom-base:alien0000001", labels: ["io.spire.workroom.base=1", "io.spire.workroom.base.project=aaaaaaaaaaaa"], created: "2019-01-01T00:00:00Z" }));
  const tags = [];
  for (let n = 1; n <= 5; n += 1) {
    w.lock(n);
    const r = w.cli("build");
    assert.equal(r.status, 0, r.stderr);
    tags.push(r.stdout.trim());
    if (n === 1) {
      // a workroom still running from the first base
      const first = w.images().find((i) => i.ref === tags[0]);
      mkdirSync(join(w.state, "container"), { recursive: true });
      writeFileSync(join(w.state, "container", "workroom-held"), JSON.stringify({ image: first.id }));
    }
  }
  const left = w.images().map((i) => i.ref).sort();
  assert.ok(left.includes("other:latest"), "an image without the base label is never removed");
  assert.ok(left.includes("workroom-base:alien0000001"), "another project's base is never removed");
  assert.ok(left.includes(tags[0]), "a base a workroom holds is refused by rmi and kept");
  assert.deepEqual(
    left.filter((r) => r.startsWith("workroom-base:") && r !== tags[0] && r !== "workroom-base:alien0000001").sort(),
    [tags[2], tags[3], tags[4]].sort(),
    "the new tag and the newest two others are kept; the rest is gone",
  );
  const rmi = w.calls().filter((c) => c[0] === "rmi");
  assert.ok(rmi.length >= 2, "older bases were offered to rmi");
  for (const c of rmi) {
    assert.equal(c.length, 2, "rmi takes the image id alone: never -f");
    assert.ok(!["other0001", "alien001"].includes(c[1]), "an unlabelled image or another project's is never offered");
  }
});

test("an image whose age cannot be read is kept and said, never ranked as the oldest", () => {
  const w = world();
  w.lock(1);
  mkdirSync(join(w.state, "image"), { recursive: true });
  for (const [id, created] of [["old00001", "bad"], ["old00002", "2020-01-01T00:00:00Z"], ["old00003", "2020-01-02T00:00:00Z"], ["old00004", "2020-01-03T00:00:00Z"]]) {
    writeFileSync(join(w.state, "image", id), JSON.stringify({ id, ref: `workroom-base:${id}`, labels: ["io.spire.workroom.base=1", w.mark()], created }));
  }
  w.lock(2);
  const r = w.cli("build");
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stderr, /kept workroom-base:old00001: its age is not known/);
  const left = w.images().map((i) => i.id);
  assert.ok(left.includes("old00001"), "the unreadable one is kept");
  assert.ok(!left.includes("old00002"), "the oldest readable one beyond the newest two is removed");
});

test("a prune that cannot list the images fails the build in words, and the rerun finds the image built", () => {
  const w = world();
  w.lock(1);
  writeFileSync(join(w.state, "ls-fails"), "");
  const r = w.cli("build");
  assert.equal(r.status, 1);
  assert.match(r.stderr, /is built, but the older base images could not be listed to remove: Cannot connect/);
  rmSync(join(w.state, "ls-fails"));
  const again = w.cli("build");
  assert.equal(again.status, 0, again.stderr);
  assert.match(again.stderr, /nothing to do/);
});

test("a build that is already built prunes nothing", () => {
  const w = world();
  w.lock(1);
  assert.equal(w.cli("build").status, 0);
  const before = w.calls().length;
  const again = w.cli("build");
  assert.match(again.stderr, /nothing to do/);
  assert.ok(w.calls().slice(before).every((c) => c[0] !== "rmi" && !(c[0] === "image" && c[1] === "ls")));
});
