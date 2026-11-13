// A project's `readByChecks` module, for the tests: which paths can reach a check at all (the docs a project's CI skips). A real project names its own (land.json `readByChecks`).
export const SKIPPED_PREFIXES = ["measure/", "notes/"];
export const parseNul = (b) => String(b).split("\0").filter(Boolean);
/** True when at least one of the paths can reach a check: not a skipped prefix, not prose. */
export const reachesCode = (paths) =>
  paths.some((p) => !SKIPPED_PREFIXES.some((s) => p.startsWith(s)));
