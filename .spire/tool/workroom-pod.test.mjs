// Admission: an agent on a host or a pod (a plain container that has the repo and its manifest) runs the SAME workroom command
// and the same land from the same manifest, with NO venue: create (`box up`), attach two clients (`box land --box ID`, `box capture --box ID`), land a change inside the instance
// against its services on a fresh database, destroy by handle (`box down ID`), nothing left. The project is NOT Spire: the "tally" app (an api whose database is a data file).
// Oracle: the real box, the real land, the real vendored lander and a real bare remote; nothing of the venue (no server, no PostgreSQL, no Docker stack) runs. The second run does
// the same in a plain container (skipped, and the skip says why, on a host with no Docker or no image: CI's runners have both).
// It would fail if land started a second api or database server, ran its checks on the instance's own database (not a fresh one), left the fresh database behind, restarted the
// instance, let a second client see another instance, or if `box down` left a process, a state dir, a handle or a database.
import assert from "node:assert/strict";
import { execFileSync, spawn, spawnSync } from "node:child_process";
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repo = join(fileURLToPath(import.meta.url), "../..");
const IMAGE = process.env.POD_TEST_IMAGE ?? "node:22-bookworm";
const GIT = {
  GIT_AUTHOR_NAME: "t",
  GIT_AUTHOR_EMAIL: "t@t",
  GIT_COMMITTER_NAME: "t",
  GIT_COMMITTER_EMAIL: "t@t",
};
const git = (cwd, ...a) =>
  execFileSync("git", a, {
    cwd,
    env: { ...process.env, ...GIT },
    encoding: "utf8",
  });

const API = `import { createServer } from "node:http";
import { existsSync, readFileSync } from "node:fs";
const boot = Math.random().toString(36).slice(2);
const rows = () => (existsSync(process.env.DATABASE_URL) ? JSON.parse(readFileSync(process.env.DATABASE_URL, "utf8")) : []);
createServer((req, res) => res.end(JSON.stringify({ boot, rows: rows() }))).listen(Number(process.env.PORT), "127.0.0.1");
`;
// the project's check: run by land inside the instance, on a database that is a FRESH one beside the instance's own, against the instance's app
const CHECK = `import { existsSync, readFileSync, writeFileSync } from "node:fs";
const url = process.env.LAND_INSTANCE_URL, inst = process.env.LAND_INSTANCE_DB, db = process.env.DATABASE_URL;
const bad = (why) => { console.error("tally check: " + why); process.exit(1); };
if (!url || !inst) bad("not inside an instance");
if (db === inst || !existsSync(db)) bad("the database is not a fresh one beside the instance's own");
if (readFileSync(db, "utf8") !== "") bad("the fresh database is not empty");
writeFileSync(db, '["land"]');
const seen = await fetch(url).then((r) => r.json());
writeFileSync(process.env.TALLY_PROOF + "/land.json", JSON.stringify({ boot: seen.boot, rows: seen.rows, db, inst }));
`;
// the project's database recipe: its own (wrap) and a fresh one beside the instance's (--attach); nothing of PostgreSQL
const DB = `#!/usr/bin/env bash
if [ "$1" = --attach ]; then shift 2; db=\${LAND_INSTANCE_DB:?}.fresh.$$; : >"$db"; trap 'rm -f "$db"' EXIT; DATABASE_URL=$db "$@"; exit $?; fi
shift; d=$(mktemp -d); trap 'rm -rf "$d"' EXIT; DATABASE_URL=$d/db.json "$@"
`;
const LAND_CONFIG = {
  version: 1,
  paths: { tests: ["**/*.test.mjs"], prose: ["**/*.md"], notProd: ["**/*.md"] },
  sensitive: { globs: [], nameGlobs: [], always: [] },
  areas: [
    {
      name: "tally",
      paths: ["**"],
      steps: [
        {
          id: "tally",
          dir: ".",
          environment: "tallydb",
          cmd: ["node", "check.mjs"],
        },
      ],
    },
  ],
  environments: {
    tallydb: {
      wrap: ["bash", "{root}/scripts/tally-db.sh", "--"],
      attach: ["bash", "{root}/scripts/tally-db.sh", "--attach", "--"],
      bundle: { have: ["true"] },
      ifMissing: "fail",
    },
  },
};
const BOX_CONFIG = {
  box: 1,
  wrap: { environment: "tallydb", exports: ["DATABASE_URL"], instance: "DATABASE_URL" },
  services: [
    {
      name: "api",
      start: "node api.mjs",
      portEnv: "PORT",
      ready: { path: "/", seconds: 20 },
    },
  ],
  seed: 'echo \'["seed"]\' > "$DATABASE_URL"',
  capture: {
    scene: "tally",
    run: 'curl -sf "$BOX_URL" > "$BOX_OUT/seen.json"',
  },
};

// a bare remote whose main holds the real lander, the real box and the tally project; a clone of it, and a change to land
function world() {
  const dir = mkdtempSync(join(tmpdir(), "workroom-"));
  const seed = join(dir, "seed"),
    bare = join(dir, "remote.git"),
    c = join(dir, "clone");
  mkdirSync(join(seed, "scripts"), { recursive: true });
  cpSync(join(repo, "tool"), join(seed, ".spire/tool"), {
    recursive: true,
    filter: (s) => !s.endsWith(".test.mjs"),
  });
  mkdirSync(join(seed, ".spire/review"), { recursive: true }); // this project removes the whole council: the landing here needs no model
  for (const role of ["inspector", "steward", "architect", "designer", "warden"]) writeFileSync(join(seed, ".spire/review", `${role}.md`), `---\nrole: ${role}\ncalls: []\n---\nremoved by this project\n`);
  writeFileSync(join(seed, "api.mjs"), API);
  writeFileSync(join(seed, "check.mjs"), CHECK);
  writeFileSync(join(seed, "scripts/tally-db.sh"), DB);
  writeFileSync(
    join(seed, ".spire/land.json"),
    JSON.stringify(LAND_CONFIG),
  );
  writeFileSync(
    join(seed, ".spire/box.json"),
    JSON.stringify(BOX_CONFIG),
  );
  writeFileSync(
    join(seed, "tally.mjs"),
    "export const tally = (xs) => xs.length;\n",
  );
  git(seed, "init", "-q", "-b", "main");
  git(seed, "add", "-A");
  git(seed, "commit", "-qm", "seed");
  git(dir, "clone", "-q", "--bare", seed, bare);
  git(dir, "clone", "-q", bare, c);
  writeFileSync(
    join(c, "tally.mjs"),
    "export const tally = (xs) => xs.length + 0;\n",
  );
  git(c, "commit", "-qam", "tally: a change to land");
  for (const d of ["home", "tmp", "proof"]) {
    mkdirSync(join(dir, d));
  }
  return {
    dir,
    c,
    home: join(dir, "home"),
    tmp: join(dir, "tmp"),
    proof: join(dir, "proof"),
    bare,
  };
}

// what a runner runs: the host (an agent on a host) or a plain container with the repo mounted (a pod). Both take the same argv and the same environment.
const runner = (w, pod) => {
  const env = {
    BOX_HOME: w.home,
    TMPDIR: w.tmp,
    TALLY_PROOF: w.proof,
    BOX_ID: "",
    LAND_INSTANCE_URL: "",
    LAND_INSTANCE_DB: "",
    ...GIT,
  };
  return (argv, extra = {}) =>
    new Promise((resolve) => {
      const all = { ...env, ...extra };
      const [cmd, args, opts] = pod
        ? [
            "docker",
            [
              "exec",
              "-w",
              w.c,
              ...Object.entries(all).flatMap(([k, v]) => ["-e", `${k}=${v}`]),
              pod,
              ...argv,
            ],
            {},
          ]
        : [
            argv[0],
            argv.slice(1),
            { cwd: w.c, env: { ...process.env, ...all } },
          ];
      const p = spawn(cmd, args, opts);
      let stdout = "",
        stderr = "";
      p.stdout.on("data", (d) => (stdout += d));
      p.stderr.on("data", (d) => (stderr += d));
      p.on("close", (status) => resolve({ status, stdout, stderr }));
    });
};

async function flow(w, sh) {
  const box = (...a) => sh(["bash", ".spire/tool/box", ...a]);
  const left = () =>
    existsSync(w.home)
      ? readdirSync(w.home).filter((n) => n.startsWith("box-"))
      : [];
  const get = async (url) =>
    JSON.parse((await sh(["curl", "-sf", url])).stdout);
  // CREATE, from the manifest
  const up = await box("up");
  assert.equal(up.status, 0, up.stderr);
  const env = readFileSync(up.stdout.trim(), "utf8");
  const [, url] = /export BOX_URL=(\S+)/.exec(env),
    [, id] = /export BOX_ID=(\S+)/.exec(env),
    [, inst] = /export DATABASE_URL=(\S+)/.exec(env);
  const before = await get(url);
  assert.deepEqual(before.rows, ["seed"], "the instance is seeded");
  // ATTACH two clients at once: a land (the writer) and a reader taking a capture from the same instance
  const [landed, shot] = await Promise.all([
    box(
      "land",
      "--box",
      id,
    ),
    box("capture", "--box", id, "--out", join(w.proof, "shot")),
  ]);
  assert.equal(shot.status, 0, shot.stderr);
  assert.equal(landed.status, 0, landed.stdout + landed.stderr);
  const last = JSON.parse(landed.stdout.trim().split("\n").at(-1));
  assert.equal(last.result, "landed", landed.stdout);
  assert.match(
    git(w.bare, "log", "-1", "--format=%B", "main"),
    /^Land-Checks: areas=tally .*tally:/m,
    "the checks ran and the commit on main says so",
  );
  assert.equal(
    git(w.bare, "rev-parse", "main").trim(),
    last.sha,
    "the change is on main",
  );
  // both clients saw the ONE instance, and land's check ran on a fresh database beside the instance's own
  const proof = JSON.parse(readFileSync(join(w.proof, "land.json"), "utf8"));
  assert.equal(
    proof.boot,
    before.boot,
    "land's check reached the instance's own api",
  );
  assert.equal(
    JSON.parse(readFileSync(join(w.proof, "shot/seen.json"), "utf8")).boot,
    before.boot,
    "and the second client the same one",
  );
  assert.deepEqual(
    proof.rows,
    ["seed"],
    "the instance's data was not touched by the check's writes",
  );
  assert.equal(proof.inst, inst);
  assert.notEqual(proof.db, inst, "the check's database is not the instance's");
  assert.equal(
    existsSync(proof.db),
    false,
    "the fresh database was dropped when the run ended",
  );
  assert.deepEqual(
    await get(url),
    before,
    "no restart, no second api: the same boot, the same rows",
  );
  assert.deepEqual(left(), [id], "no second box");
  assert.equal(git(w.c, "status", "--porcelain", "--untracked-files=no"), "");
  // DESTROY by its handle
  const down = await box("down", id);
  assert.equal(down.status, 0, down.stderr);
  assert.deepEqual(left(), []);
  assert.equal(
    (await sh(["curl", "-sf", url])).status !== 0,
    true,
    "the api is gone",
  );
  assert.equal(existsSync(dirname(inst)), false, "and the database with it");
  assert.deepEqual(
    readdirSync(w.tmp),
    [],
    "nothing is left in the scratch directory",
  );
  assert.equal(existsSync(`${w.home}.handles`), false, "no handle");
}

test("a host agent: the same box and the same land from the same manifest, with no venue; create, attach two clients, land on a fresh database, destroy, nothing left", async () => {
  const w = world();
  try {
    await flow(w, runner(w));
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

const docker = (...a) => spawnSync("docker", a, { encoding: "utf8" });
const podOk =
  docker("info").status === 0 &&
  (docker("image", "inspect", IMAGE).status === 0 ||
    docker("pull", "-q", IMAGE).status === 0);
test(
  "a pod: the same, in a plain container that has the repo and the manifest",
  {
    skip: podOk
      ? false
      : `no Docker, or no ${IMAGE} (POD_TEST_IMAGE): CI's runners have both`,
  },
  async () => {
    const w = world();
    const pod = `pod-${process.pid}`;
    try {
      const id = process.getuid(),
        gid = process.getgid();
      const made = docker(
        "run",
        "-d",
        "--rm",
        "--name",
        pod,
        "--label",
        `spire.run=${pod}`,
        "--user",
        `${id}:${gid}`,
        "-e",
        "HOME=/tmp",
        "-v",
        `${w.dir}:${w.dir}`,
        IMAGE,
        "sleep",
        "600",
      );
      assert.equal(made.status, 0, made.stderr);
      await flow(w, runner(w, pod));
    } finally {
      docker("rm", "-f", "-v", pod);
      rmSync(w.dir, { recursive: true, force: true });
    }
  },
);
