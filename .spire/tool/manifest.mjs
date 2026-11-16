// manifest.mjs: the project manifest, ONE view of the project's two files (.spire/box.json, .spire/land.json).
// The MECHANISM (land, box) names no project: it reads this view. The view is the union of the fields of the files that exist; a file that is not JSON is an error naming it.
// There is no schema: a field nothing reads is ignored, and docs/BOX.md and references/project.md say what the readers use, with ONE exception: land.json's TOP-LEVEL keys are checked against the list below.
// ACCEPTED TRADE-OFF (the captain's call, slice S4): nothing nested is checked. A typo inside `sensitive` or a `held` entry (`globe` for `globs`) loads as an empty gate; it can only land through a change to .spire/land.json, which is under .spire/** and is read by the Warden on the way in. A nested check is the schema again, which is deleted.
// It protects a typo in the land config (`sensitve`, `helds`) silently unseating the security reviewer or the hold: a misspelled gate key would otherwise read as no gate at all. If the same field is in both files, land.json (read last) wins.
import { existsSync, readFileSync } from "node:fs";
import { basename } from "node:path";
import { pathToFileURL } from "node:url";

/** The top-level keys of land.json (`_` is prose): the only check this file makes on a field. */
export const LAND_KEYS = ["_", "version", "readByChecks", "paths", "sensitive", "review", "held", "areas", "checks", "environments", "warm", "principles", "lint"];

/** Load the files that exist as ONE view: the union of their fields, a later file over an earlier one. `files` is [path, ...]; a path that does not exist is skipped (the caller says which it requires). */
export function loadManifest(files) {
  const view = {};
  for (const f of files.filter((p) => p && existsSync(p))) {
    let obj;
    try {
      obj = JSON.parse(readFileSync(f, "utf8"));
    } catch (e) {
      throw new Error(`manifest: cannot read ${f}: ${e.message}`);
    }
    if (basename(f) === "land.json") {
      const unknown = Object.keys(obj).filter((k) => !LAND_KEYS.includes(k));
      if (unknown.length) {
        throw new Error(`manifest: ${f} has ${unknown.length === 1 ? "a top-level key" : "top-level keys"} no reader of the tool uses: ${unknown.join(", ")} (known: ${LAND_KEYS.join(", ")}). A misspelled gate key would silently drop the gate: fix the name, or delete the key`);
      }
    }
    Object.assign(view, obj);
  }
  return view;
}

// CLI (box, a shell script, reads its config through this): `manifest.mjs FILE...` prints the one view as JSON, or the error and exit 1.
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href
) {
  try {
    console.log(JSON.stringify(loadManifest(process.argv.slice(2))));
  } catch (e) {
    console.error(e.message);
    process.exit(1);
  }
}
