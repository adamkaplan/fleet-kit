// Admission: the box starts the services a project's manifest declares, once and in order, and offers a fast reset that restarts nothing. The project here is NOT Spire:
// a tiny "tally" app (an api with a data file for its database, and a worker that finds the api through the box) and nothing of this repository but the box script and the manifest
// schema. Oracle: the real box script on a small real git repository with real processes; the wrapper that stands for PostgreSQL only names the database (a file).
// It would fail if a service started before the one it needs, a reset restarted a service (the boot ids would change) or left the data as the writes made it, a refresh lost the data,
// or a service that never gets ready were taken for a start.
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const box = join(fileURLToPath(import.meta.url), "../box");
const inherited = Object.fromEntries(
  Object.entries(process.env).filter(
    ([k]) => k !== "NODE_TEST_CONTEXT" && !k.startsWith("BOX_"),
  ),
);

// the api: its rows are the data file (DATABASE_URL), its boot id says whether it was restarted
const API = `import { createServer } from "node:http";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
const boot = Math.random().toString(36).slice(2);
writeFileSync(process.env.WORKER_DIR + "/api-told", Object.keys(process.env).filter((k) => k.startsWith("BOX_URL_")).join());
const rows = () => (existsSync(process.env.DATABASE_URL) ? JSON.parse(readFileSync(process.env.DATABASE_URL, "utf8")) : []);
createServer((req, res) => {
  if (req.method === "POST") writeFileSync(process.env.DATABASE_URL, JSON.stringify([...rows(), req.url.slice(1)]));
  res.end(JSON.stringify({ boot, rows: rows() }));
}).listen(Number(process.env.PORT), "127.0.0.1");
`;
// the worker: it reaches the api at the URL the box gave it, notes that it did, keeps a cache file and its own boot id
const WORKER = `import { rmSync, writeFileSync } from "node:fs";
const boot = Math.random().toString(36).slice(2);
const seen = await fetch(process.env.BOX_URL_API).then((r) => r.json());
writeFileSync(process.env.WORKER_DIR + "/boot", boot);
writeFileSync(process.env.WORKER_DIR + "/cache", "warm");
writeFileSync(process.env.WORKER_DIR + "/told", Object.keys(process.env).filter((k) => k.startsWith("BOX_URL_")).join());
writeFileSync(process.env.WORKER_DIR + "/saw", seen.boot);
// stopped, it takes its readiness marker with it: a restart is not ready on the last run's
process.on("SIGTERM", () => {
  rmSync(process.env.WORKER_DIR + "/saw");
  process.exit(0);
});
setInterval(() => {}, 1000);
`;

function tally(extra = {}) {
  const dir = mkdtempSync(join(tmpdir(), "box-services-"));
  const home = join(dir, "home");
  mkdirSync(join(dir, ".spire"));
  mkdirSync(join(dir, "worker"));
  writeFileSync(join(dir, "api.mjs"), API);
  writeFileSync(join(dir, "worker.mjs"), WORKER);
  writeFileSync(
    join(dir, "wrap.sh"),
    `shift; export DATABASE_URL=${join(dir, "db.json")}; exec "$@"\n`,
  );
  writeFileSync(
    join(dir, ".spire/box.json"),
    JSON.stringify({
      box: 1,
      wrap: { ensure: "true", run: "bash wrap.sh", exports: ["DATABASE_URL"], instance: "DATABASE_URL" },
      services: [
        {
          name: "api",
          start: "node api.mjs",
          portEnv: "PORT",
          env: { WORKER_DIR: join(dir, "worker") },
          ready: { path: "/", seconds: 20 },
        },
        {
          name: "worker",
          start: "node worker.mjs",
          portEnv: "WORKER_PORT",
          env: { WORKER_DIR: join(dir, "worker") },
          ready: { cmd: "test -f worker/saw", seconds: 20 },
          restarts: 0,
          reset: "rm -f worker/cache",
        },
      ],
      seed: 'echo \'["seed"]\' > "$DATABASE_URL"; cp "$DATABASE_URL" "$BOX_DIR/seed.json"',
      reset: 'cp "$BOX_DIR/seed.json" "$DATABASE_URL"',
      ...extra,
    }),
  );
  execFileSync("git", ["init", "-q"], { cwd: dir });
  const run = (args) =>
    spawnSync("bash", [box, ...args], {
      cwd: dir,
      env: { ...inherited, BOX_HOME: home, BOX_CONFIG: join(dir, ".spire/box.json"), LAND_CONFIG: join(dir, ".spire/land.json") },
      encoding: "utf8",
      timeout: 120_000,
    });
  const read = (f) => readFileSync(join(dir, f), "utf8");
  const left = () =>
    existsSync(home)
      ? readdirSync(home).filter((n) => n.startsWith("box-"))
      : [];
  return {
    dir,
    run,
    read,
    left,
    done: () => rmSync(dir, { recursive: true, force: true }),
  };
}
const call = (url, method = "GET") =>
  fetch(url, { method, headers: { connection: "close" } }).then((r) =>
    r.json(),
  );

test("services start once and in order, and the fast reset puts the data back without restarting any of them", async () => {
  const w = tally();
  try {
    const up = w.run(["up"]);
    assert.equal(up.status, 0, up.stderr);
    assert.match(up.stderr, /api answers at http/);
    const env = readFileSync(up.stdout.trim(), "utf8");
    const url = /export BOX_URL=(\S+)/.exec(env)[1];
    assert.equal(
      /export BOX_URL_API=(\S+)/.exec(env)[1],
      url,
      "BOX_URL is the first service with a port",
    );
    const before = await call(url);
    assert.equal(
      w.read("worker/saw"),
      before.boot,
      "the worker found the api, already answering, at the URL the box gave it",
    );
    assert.deepEqual(before.rows, ["seed"]);
    assert.deepEqual((await call(`${url}/two`, "POST")).rows, ["seed", "two"]);
    const workerBoot = w.read("worker/boot");
    assert.equal(
      w.read("worker/told"),
      "BOX_URL_API",
      "a service is told of those before it",
    );
    assert.equal(w.read("worker/api-told"), "", "and not of those after it");

    const reset = w.run(["reset"]);
    assert.equal(reset.status, 0, reset.stderr);
    assert.match(reset.stderr, /reset worker .*\n.*reset data /);
    const after = await call(url);
    assert.deepEqual(after.rows, ["seed"], "the data is the seed again");
    assert.equal(after.boot, before.boot, "the api was not restarted");
    assert.equal(
      w.read("worker/boot"),
      workerBoot,
      "the worker was not restarted",
    );
    assert.equal(
      existsSync(join(w.dir, "worker/cache")),
      false,
      "the worker's own reset ran",
    );

    // refresh restarts every service, in order, on the same port and the same data
    await call(`${url}/three`, "POST");
    const refresh = w.run(["refresh"]);
    assert.equal(refresh.status, 0, refresh.stderr);
    const again = await call(url);
    assert.notEqual(again.boot, before.boot, "refresh restarted the api");
    assert.deepEqual(again.rows, ["seed", "three"], "on the same data");
    assert.notEqual(w.read("worker/boot"), workerBoot, "and the worker");
    assert.equal(
      w.read("worker/api-told"),
      "",
      "a restarted service is still told only of those before it",
    );

    assert.equal(w.run(["down"]).status, 0);
    assert.deepEqual(w.left(), []);
  } finally {
    w.done();
  }
});

test("a service that never gets ready is a failed start naming it, and leaves nothing", () => {
  const w = tally();
  try {
    const cfg = JSON.parse(w.read(".spire/box.json"));
    cfg.services[1].ready = { cmd: "false", seconds: 1 };
    writeFileSync(join(w.dir, ".spire/box.json"), JSON.stringify(cfg));
    const r = w.run(["up"]);
    assert.notEqual(r.status, 0);
    assert.match(r.stderr, /FAILED: worker was not ready within 1 s/);
    assert.deepEqual(w.left(), []);
  } finally {
    w.done();
  }
});

test("two services whose names make the same variable are refused at load", () => {
  const w = tally();
  try {
    const cfg = JSON.parse(w.read(".spire/box.json"));
    cfg.services = [
      { ...cfg.services[0], name: "api-x" },
      { ...cfg.services[0], name: "api_x" },
    ];
    writeFileSync(join(w.dir, ".spire/box.json"), JSON.stringify(cfg));
    const r = w.run(["up"]);
    assert.notEqual(r.status, 0);
    assert.match(r.stderr, /not a unique name.*BOX_URL_API_X/);
    assert.deepEqual(w.left(), []);
  } finally {
    w.done();
  }
});

test("a project that declares no reset, or no services, is told so", () => {
  const w = tally({ reset: undefined });
  try {
    const cfg = JSON.parse(w.read(".spire/box.json"));
    delete cfg.services[1].reset;
    writeFileSync(join(w.dir, ".spire/box.json"), JSON.stringify(cfg));
    assert.equal(w.run(["up"]).status, 0);
    const r = w.run(["reset"]);
    assert.equal(r.status, 2);
    assert.match(r.stderr, /nothing to reset/);
    w.run(["down"]);
    writeFileSync(
      join(w.dir, ".spire/box.json"),
      JSON.stringify({ box: 1, services: [] }),
    );
    const none = w.run(["up"]);
    assert.equal(none.status, 0, "no services is a box that is a checkout: it comes up");
    assert.match(none.stderr, /a checkout and nothing else/);
    w.run(["down"]);
  } finally {
    w.done();
  }
});

test("a project that declares no `wrap` gets none of what one exports: its box comes up and goes down with the services alone, and the caller's own value never reaches them", async () => {
  const w = tally();
  try {
    const cfg = JSON.parse(w.read(".spire/box.json"));
    delete cfg.wrap;
    delete cfg.seed;
    delete cfg.reset;
    cfg.services = [{ ...cfg.services[0], env: {} }];
    // the api reads its rows from DATABASE_URL: with no wrap there is none, and the api answers with none
    writeFileSync(
      join(w.dir, "api.mjs"),
      `import { createServer } from "node:http";
createServer((req, res) => res.end(JSON.stringify({ db: process.env.DATABASE_URL ?? null }))).listen(Number(process.env.PORT), "127.0.0.1");
`,
    );
    writeFileSync(join(w.dir, ".spire/box.json"), JSON.stringify(cfg));
    const up = w.run(["up"]);
    assert.equal(up.status, 0, up.stderr);
    const url = /export BOX_URL=(\S+)/.exec(
      readFileSync(up.stdout.trim(), "utf8"),
    )[1];
    assert.deepEqual(
      await call(url),
      { db: null },
      "no wrap: nothing is passed, not a crash",
    );
    assert.equal(w.run(["down"]).status, 0);
    assert.deepEqual(w.left(), []);
  } finally {
    w.done();
  }
});
