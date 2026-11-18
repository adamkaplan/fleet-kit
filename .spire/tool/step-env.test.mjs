// Admission: a step's environment is the base, the families of names kept whole, the step's own `pass` names and (for a recipe) the box's exports: nothing else of the caller's shell.
// Oracle: step-env.mjs on a caller's environment that carries what a Workroom or a box puts there. (The whole path through run-steps.mjs is checks.test.mjs.)
import assert from "node:assert/strict";
import test from "node:test";
import { recipeEnv, stepEnv } from "./step-env.mjs";

const shell = {
  PATH: "/bin",
  HOME: "/home/x",
  TMPDIR: "/tmp/x",
  LANG: "C.UTF-8",
  LC_ALL: "C.UTF-8",
  GIT_CONFIG_NOSYSTEM: "1",
  GIT_CONFIG_COUNT: "1",
  GIT_CONFIG_KEY_0: "user.name",
  GIT_CONFIG_VALUE_0: "n",
  GIT_TERMINAL_PROMPT: "0",
  SPIRE_EXECUTION_WORKER: "0",
  POSTGRES_PORT: "5999",
  OPENROUTER_BASE_URL: "http://127.0.0.1:7500",
  HOST_SECRETS: "OPENROUTER_API_KEY",
  DATABASE_URL: "db://caller/db",
  CHROME_PATH: "/opt/chrome",
};

test("a step gets the base, the locale and git's configuration, never the rest of the caller's shell", () => {
  assert.deepEqual(stepEnv(shell), {
    PATH: "/bin",
    HOME: "/home/x",
    TMPDIR: "/tmp/x",
    LANG: "C.UTF-8",
    LC_ALL: "C.UTF-8",
    GIT_CONFIG_NOSYSTEM: "1",
    GIT_CONFIG_COUNT: "1",
    GIT_CONFIG_KEY_0: "user.name",
    GIT_CONFIG_VALUE_0: "n",
  });
});

test("a name the step passes reaches it; one it does not pass does not; the caller's object is not changed", () => {
  const before = { ...shell };
  assert.equal(stepEnv(shell, ["CHROME_PATH"]).CHROME_PATH, "/opt/chrome");
  assert.equal(stepEnv(shell, ["CHROME_PATH"]).POSTGRES_PORT, undefined);
  assert.equal(stepEnv(shell, ["NOT_SET"]).NOT_SET, undefined, "a name the caller has not set is not invented");
  assert.deepEqual(shell, before);
});

test("a recipe step also gets the box's exports and, with an attach form, the instance's database server; no other name of the caller's", () => {
  const caller = { ...shell, LAND_BOX_EXPORTS: "DATABASE_URL BOX_X", BOX_X: "x", LAND_INSTANCE_DB: "db://i/db", LAND_INSTANCE_URL: "http://i", LAND_STATE_DIR: "/s" };
  assert.deepEqual(recipeEnv(caller, {}), { DATABASE_URL: "db://caller/db", BOX_X: "x", LAND_INSTANCE_URL: "http://i" });
  assert.deepEqual(recipeEnv(caller, { attach: ["x"] }), { DATABASE_URL: "db://caller/db", BOX_X: "x", LAND_INSTANCE_URL: "http://i", LAND_INSTANCE_DB: "db://i/db" });
  assert.deepEqual(recipeEnv({ PATH: "/bin" }, { attach: ["x"] }), {});
});
