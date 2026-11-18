// failedFilesOf reads the failed test files out of a runner's spec output. A failing `todo` test is not one:
// Node 22 marks it `✖ title (Nms) # reason`, Node 26 marks it `⚠ title (Nms) # reason`.
import assert from "node:assert/strict";
import test from "node:test";

import { failedFilesOf } from "./failed-files.mjs";

test("a real failure names its file; a failing todo does not, on either Node's marker", () => {
  const out = [
    "test at ios/b.mjs:2:1", "✖ red b (0.3ms)",
    "test at ios/d.mjs:3:1", "✖ gap (0.1ms) # later",
    "test at ios/e.mjs:3:1", "⚠ gap (0.1ms) # later",
    "✖ ios/whole.test.mjs (5ms)",
  ].join("\n");
  assert.deepEqual(failedFilesOf(out).sort(), ["ios/b.mjs", "ios/whole.test.mjs"]);
});
