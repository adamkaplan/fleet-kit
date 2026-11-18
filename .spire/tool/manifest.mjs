// manifest.mjs: the project manifest, ONE view of the project's three files (.spire/box.json, .spire/land.json, .spire/areas.json).
// The MECHANISM (land, box) names no project: it reads this view. The view is the union of the fields of the files that exist; a file that is not JSON is an error naming it.
// There is no schema: a field nothing reads is ignored, and docs/BOX.md and references/project.md say what the readers use, with ONE exception: land.json's TOP-LEVEL keys are checked against the list below.
// ACCEPTED TRADE-OFF (the captain's call, slice S4): nothing nested is checked. A typo inside `sensitive` or a `held` entry (`globe` for `globs`) loads as an empty gate; it can only land through a change to .spire/land.json, which is under .spire/** and is read by the Warden on the way in. A nested check is the schema again, which is deleted.
// It protects a typo in the land config (`sensitve`, `helds`) silently unseating the security reviewer or the hold: a misspelled gate key would otherwise read as no gate at all. If the same field is in two files, the later file wins.
// areas.json (the project's areas: what each is, the paths and tests it owns, its kind; read last) lends the view ONLY `areas` (which REPLACE land.json's: a project declares its areas in one place), `exclude` (paths no area but docs owns) and `harnesses` (the browser-harness policy of the app); its `_` and `version` are its own. Its top-level keys are checked like land.json's.
import { existsSync, readFileSync } from "node:fs";
import { basename } from "node:path";
import { pathToFileURL } from "node:url";

/** The top-level keys of land.json (`_` is prose): the only check this file makes on a field. */
export const LAND_KEYS = ["_", "version", "readByChecks", "paths", "sensitive", "review", "held", "areas", "checks", "environments", "warm", "principles", "lint", "pins", "recipes"];

/** The top-level keys of areas.json: the same one check (`_` and `version` are its own, the other three join the view). */
export const AREAS_KEYS = ["_", "version", "areas", "exclude", "harnesses"];
const AREAS_LENDS = ["areas", "exclude", "harnesses"];

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
    const known = { "land.json": LAND_KEYS, "areas.json": AREAS_KEYS }[basename(f)];
    if (known) {
      const unknown = Object.keys(obj).filter((k) => !known.includes(k));
      if (unknown.length) {
        throw new Error(`manifest: ${f} has ${unknown.length === 1 ? "a top-level key" : "top-level keys"} no reader of the tool uses: ${unknown.join(", ")} (known: ${known.join(", ")}). A misspelled gate key would silently drop the gate: fix the name, or delete the key`);
      }
    }
    Object.assign(view, basename(f) === "areas.json" ? Object.fromEntries(Object.entries(obj).filter(([k]) => AREAS_LENDS.includes(k))) : obj);
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
