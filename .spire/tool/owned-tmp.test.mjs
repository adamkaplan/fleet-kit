// Admission: owned_tmp_reap removes the temp directories of KILLED runs (older than the TTL, owner not alive), runs the directory's own `.reap` first to stop what the run left there, and leaves a directory whose
// owner lives, that is too young, or whose reap fails. Oracle: the real owned-tmp.sh in bash over a scratch TMPDIR. It would fail if a live run's directory were removed, or a directory were deleted under a process its reap could not stop.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const lib = join(fileURLToPath(import.meta.url), "../owned-tmp.sh");

test("owned_tmp_reap removes a killed run's directory after its own .reap, and leaves a live, young or unreapable one", () => {
  const tmp = mkdtempSync(join(tmpdir(), "owned-tmp-test-"));
  const made = (name, { owner = "999999", old = true, reap } = {}) => {
    const d = join(tmp, `run.${name}`);
    mkdirSync(d);
    writeFileSync(join(d, ".owner"), `${owner}\n`);
    if (reap) {
      writeFileSync(join(d, ".reap"), `#!/bin/sh\n${reap}\n`);
      chmodSync(join(d, ".reap"), 0o755);
    }
    if (old) spawnSync("touch", ["-d", "3 hours ago", d]);
    return d;
  };
  try {
    const plain = made("plain");
    const reaped = made("reaped", { reap: `echo stopped > ${join(tmp, "reap-ran")}` });
    const stuck = made("stuck", { reap: "exit 1" });
    const live = made("live", { owner: String(process.pid) });
    const young = made("young", { old: false });
    const r = spawnSync("bash", ["-c", `. ${lib}; TMPDIR=${tmp} owned_tmp_reap run.`], { encoding: "utf8", env: { ...process.env, TMPDIR: tmp } });
    assert.equal(r.status, 0, r.stderr);
    assert.equal(existsSync(plain), false, "a killed run's directory goes");
    assert.equal(existsSync(reaped), false, "so does one whose reap succeeded");
    assert.equal(existsSync(join(tmp, "reap-ran")), true, "after its own reap ran");
    assert.equal(existsSync(stuck), true, "a reap that fails keeps the directory");
    assert.equal(existsSync(live), true, "a live owner's directory stays");
    assert.equal(existsSync(young), true, "a young directory stays");
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});
