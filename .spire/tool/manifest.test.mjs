// Admission: the project manifest (manifest.mjs) reads .spire/box.json and .spire/land.json as ONE view: the union of their fields, land.json over box.json, and a file that is not JSON is an error
// naming it (the box script says it too, and does not start). There is no schema: a field nothing reads is ignored, except that an unknown TOP-LEVEL key of land.json is refused naming it (a typo in the land
// config silently unseating the security reviewer: `sensitve` for `sensitive`, `helds` for `held`). Oracle: the REAL example files load unchanged; broken JSON and a misspelled gate key are refused naming the file and the key.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { loadConfig } from "./config.mjs";
import { LAND_KEYS, loadManifest } from "./manifest.mjs";

const repo = join(fileURLToPath(import.meta.url), "../..");
const real = {
  box: join(repo, "examples/spire-venue/box.json"),
  land: join(repo, "examples/spire-venue/land.json"),
};
const read = (f) => JSON.parse(readFileSync(f, "utf8"));

/** A copy of both real files with `edit(box, land)` applied; returns their paths. */
function copies(edit) {
  const dir = mkdtempSync(join(tmpdir(), "manifest-test-"));
  const box = read(real.box);
  const land = read(real.land);
  edit(box, land);
  writeFileSync(join(dir, "box.json"), JSON.stringify(box));
  writeFileSync(join(dir, "land.json"), JSON.stringify(land));
  return {
    dir,
    box: join(dir, "box.json"),
    land: join(dir, "land.json"),
  };
}

test("the two real files load as one view, every field of both present", () => {
  const view = loadManifest([real.box, real.land]);
  for (const f of Object.keys(read(real.box))) {
    assert.deepEqual(view[f], read(real.box)[f]);
  }
  for (const f of Object.keys(read(real.land))) {
    assert.deepEqual(view[f], read(real.land)[f]);
  }
});

test("land's own config load reads the same view (nothing lost)", async () => {
  const cfg = await loadConfig(real.land, join(repo, "tool/fixtures/project"));
  assert.equal(cfg.version, read(real.land).version);
  assert.equal(cfg.services[0].start, read(real.box).services[0].start);
  assert.ok(cfg.isTest("server/tests/x.test.ts"));
});

test("a project may carry only one of the two files", () => {
  assert.equal(
    loadManifest([real.land, join(tmpdir(), "no-such-box.json")]).version,
    1,
  );
});

for (const [name, edit, key] of [
  ["sensitive misspelled", (_b, l) => { l.sensitve = l.sensitive; delete l.sensitive; }, "sensitve"],
  ["held misspelled", (_b, l) => (l.helds = []), "helds"],
  ["the removed classes", (_b, l) => (l.classes = []), "classes"],
  ["the removed council table", (_b, l) => (l.council = []), "council"],
]) {
  test(`an unknown top-level key of land.json is refused, named with its file: ${name}`, () => {
    const c = copies(edit);
    assert.throws(() => loadManifest([c.box, c.land]), (e) => e.message.includes(key) && e.message.includes("land.json") && /top-level key/.test(e.message));
  });
}

test("every top-level key of the real land.json files is known", () => {
  for (const f of [real.land, join(repo, ".spire/land.json")]) {
    assert.deepEqual(Object.keys(read(f)).filter((k) => !LAND_KEYS.includes(k)), [], f);
  }
});

test("a file that is not JSON is an error naming it, from the module and from the box script; a field nothing reads is ignored", () => {
  const c = copies(() => {});
  writeFileSync(c.land, "{ not json");
  assert.throws(() => loadManifest([c.box, c.land]), /manifest: cannot read .*land\.json/);
  const ok = copies((b) => (b.anythingAtAll = "x")); // a field of box.json nothing reads is ignored
  assert.equal(loadManifest([ok.box, ok.land]).anythingAtAll, "x");
  const r = spawnSync(join(repo, "tool/box"), ["up"], {
    cwd: repo,
    encoding: "utf8",
    env: { ...process.env, BOX_CONFIG: c.box, LAND_CONFIG: c.land, BOX_HOME: join(c.dir, "home") },
  });
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /cannot read .*land\.json/);
});

test("the example project's box listens where BOX_BIND_HOST says (its app), so a box in a shared workroom instance can be reached over the instance's network", () => {
  const cfg = read(real.box);
  const svc = (name) => cfg.services.find((s) => s.name === name);
  assert.match(svc("app").start, /SPIRE_LISTEN_HOST="\$BOX_BIND_HOST"/);
  assert.equal(svc("app").env?.SPIRE_LISTEN_HOST, undefined, "no second, literal address");
});
