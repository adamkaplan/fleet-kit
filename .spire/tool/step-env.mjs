// step-env.mjs: the environment a step of the checks runs in. ONE place: run-steps.mjs asks here for every step.
// A step starts from NOTHING and gets only what a test may need (BASE), the names the step itself says it reads from the host (`pass` of the step in land.json: a browser's
// path, a name only that step reads) and, for a step that names an environment, what that environment's recipe gives it (recipeEnv). Whatever else the caller's shell
// carries (a Workroom image's own settings, a relay's address, the box's server, a developer's port) never reaches a test: a test is told what it needs by its own step (`env`
// of land.json) or its own recipe, never by the room. This is what a landing is judged on: the same tree is green or red the same way in every shell.
import { APP_KEY_ENV } from "./app-env.mjs";
export const BASE = [
  "PATH", // the pinned tool directories are put first by versions.mjs
  "HOME", // the config homes of git, npm, node, python and the caches are under it
  "TMPDIR",
  "XDG_RUNTIME_DIR", // the user's session: `systemctl --user`, a rootless daemon, and a host's cgroup guard around a test run (systemd-run --user) cannot find the user manager without it
  "LANG",
];
/**
 * The families of names kept whole: the locale (LC_ALL, LC_CTYPE, ...) and git's configuration (GIT_CONFIG_NOSYSTEM, GIT_CONFIG_SYSTEM, GIT_CONFIG_GLOBAL, GIT_CONFIG_COUNT and its KEY_n/VALUE_n): a Workroom
 * configures every git of its room through them (the system file off, the author in the environment), so a test that runs git in a scratch repository sees the git the room has, not the disk's own file
 * (measured: pod-setup-script.test.ts is red in a room without them, because the image's /etc/gitconfig names an author).
 */
const BASE_PREFIXES = ["LC_", "GIT_CONFIG_"];

/** `parent` reduced to what a step may inherit (the fleet App's private key never, not even when a step's `pass` names it): BASE, the families above and the names the step passes (`pass`). Never mutates `parent`. */
export function stepEnv(parent, pass = []) {
  const keep = new Set([...BASE, ...pass]);
  return Object.fromEntries(
    Object.entries(parent).filter(([k]) => k !== APP_KEY_ENV && (keep.has(k) || BASE_PREFIXES.some((p) => k.startsWith(p)))),
  );
}

/**
 * What a step that names an environment is given beyond that: the variables the box's wrap provides (`LAND_BOX_EXPORTS`, the names wrap.exports of box.json declares), LAND_INSTANCE_URL (inside a
 * workroom instance: its app, which the step checks) and, for a recipe that has an `attach` form, LAND_INSTANCE_DB (the instance's database server URL, read from the environment and never from argv).
 */
export function recipeEnv(parent, recipe) {
  const names = [...(parent.LAND_BOX_EXPORTS ?? "").split(/\s+/).filter(Boolean), "LAND_INSTANCE_URL"];
  if (recipe?.attach) {
    names.push("LAND_INSTANCE_DB");
  }
  return Object.fromEntries(names.filter((k) => parent[k] !== undefined).map((k) => [k, parent[k]]));
}
