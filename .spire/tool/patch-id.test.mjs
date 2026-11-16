import { test } from "node:test";
import assert from "node:assert/strict";
import { patchId } from "./patch-id.mjs";

const diff = (b) => `diff --git a/f b/f\nindex 1..2 100644\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+${b}\n`;

test("patchId: equal diffs agree, unequal differ, empty is empty", () => {
  assert.equal(patchId(diff("x")), patchId(diff("x").replace("index 1..2", "index 3..4")));
  assert.notEqual(patchId(diff("x")), patchId(diff("y")));
  assert.match(patchId(diff("x")), /^[0-9a-f]{40}/);
  assert.equal(patchId(""), "");
});

test("patchId: a failed git patch-id throws, never an empty id", () => {
  const saved = process.env.PATH;
  process.env.PATH = "/nonexistent";
  try {
    assert.throws(() => patchId(diff("x")));
  } finally {
    process.env.PATH = saved;
  }
});
