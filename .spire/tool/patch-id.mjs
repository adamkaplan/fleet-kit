// The one patch-id of a change's diff: the council (verdicts carried by it) and the step runner (test passes carried by it) both use this, so they cannot disagree.
import { spawnSync } from "./versions.mjs";

/** The stable `git patch-id` of a diff text: "" for an empty diff; a failed git call throws (never an empty id for a real diff). */
export function patchId(diff) {
  if (!diff) return "";
  const r = spawnSync("git", ["patch-id", "--stable"], { input: diff, encoding: "utf8" });
  if (r.status !== 0) throw new Error(`git patch-id failed (status ${r.status}): ${r.stderr ?? ""}`);
  return r.stdout.split(" ")[0];
}
