// Admission: .spire/tool/box brings an app environment up from a project's .spire/box.json, runs a command in it, captures, refreshes and destroys
// everything it created, on every exit path. Oracle: the real box script on a small real git repository with a real (tiny) http app; the wrapper
// that stands for a wrapped server only sets DATABASE_URL (the venue's own scripts have their own tests, in examples/spire-venue).
// It would fail if the box left its state dir or its app running after `run`/`down`, let the caller's environment reach the app, or took a
// failed start for a success.
import assert from "node:assert/strict";
import { execFileSync, spawn, spawnSync } from "node:child_process";
import { createServer } from "node:http";
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
// a test run from inside a box (box land) must not see that box: nested boxes are strangers
const inherited = Object.fromEntries(
  Object.entries(process.env).filter(
    ([k]) =>
      k !== "NODE_TEST_CONTEXT" &&
      !k.startsWith("BOX_") &&
      !k.startsWith("LAND_INSTANCE_"),
  ),
);

const APP = `import { createServer } from "node:http";
const id = Math.random().toString(36).slice(2);
if (process.env.BOX_TEST_DIE) process.exit(3);
createServer((req, res) => res.end(JSON.stringify({ id, db: process.env.DATABASE_URL, secret: process.env.BOX_TEST_SECRET ?? null, pool: process.env.HOST_POOL, bind: process.env.BOX_BIND_HOST }))).listen(Number(process.env.PORT), process.env.BOX_BIND_HOST ?? "127.0.0.1");
`;

function world(extra = {}) {
  const dir = mkdtempSync(join(tmpdir(), "box-test-"));
  const home = join(dir, "home");
  writeFileSync(join(dir, "app.mjs"), APP);
  writeFileSync(
    join(dir, "wrap.sh"),
    'shift; export DATABASE_URL=db://u@127.0.0.1:1/x; exec "$@"\n',
  );
  mkdirSync(join(dir, ".spire"));
  writeFileSync(
    join(dir, ".spire/box.json"),
    JSON.stringify({
      box: 1,
      wrap: { ensure: "true", run: "bash wrap.sh", exports: ["DATABASE_URL"], instance: "DATABASE_URL" },
      setup: [
        { name: "marker", when: "[ ! -f marker ]", run: "echo hi > marker" },
      ],
      services: [
        {
          name: "app",
          start: "node app.mjs",
          portEnv: "PORT",
          env: { HOST_POOL: "local" },
          ready: { path: "/", seconds: 20 },
          restarts: 1,
        },
      ],
      seed: 'curl -sf "$BOX_URL/" > "$BOX_DIR/seeded"',
      capture: {
        scene: "one",
        run: 'echo "$BOX_SCENE $BOX_URL" > "$BOX_OUT/scene.txt"',
      },
      ...extra,
    }),
  );
  execFileSync("git", ["init", "-q"], { cwd: dir });
  const run = (args, env = {}) =>
    spawnSync("bash", [box, ...args], {
      cwd: dir,
      env: { ...inherited, BOX_HOME: home, BOX_CONFIG: join(dir, ".spire/box.json"), LAND_CONFIG: join(dir, ".spire/land.json"), ...env },
      encoding: "utf8",
      timeout: 120_000,
    });
  const left = () =>
    existsSync(home)
      ? readdirSync(home).filter((n) => n.startsWith("box-"))
      : [];
  return {
    dir,
    home,
    run,
    left,
    done: () => rmSync(dir, { recursive: true, force: true }),
  };
}

const get = (url) => fetch(url).then((r) => r.json());
const closed = (url) =>
  fetch(url).then(
    () => false,
    () => true,
  );

test("run: the command sees a seeded, answering app; afterwards nothing is left", async () => {
  const w = world();
  try {
    const probe = `node -e 'fetch(process.env.BOX_URL).then(r=>r.json()).then(j=>console.log("URL="+process.env.BOX_URL+" DB="+j.db+" SECRET="+j.secret+" POOL="+j.pool))' ; test -f "$BOX_DIR/seeded" && echo SEEDED`;
    const r = w.run(["run", "--", "bash", "-c", probe], {
      BOX_TEST_SECRET: "leak",
    });
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.stdout, /DB=db:\/\/u@127\.0\.0\.1:1\/x/);
    assert.match(
      r.stdout,
      /SECRET=null/,
      "the caller's environment never reaches the app",
    );
    assert.match(r.stdout, /POOL=local/);
    assert.match(r.stdout, /SEEDED/);
    assert.deepEqual(w.left(), []);
    assert.ok(
      await closed(/URL=(\S+)/.exec(r.stdout)[1]),
      "the app is stopped",
    );
    assert.ok(existsSync(join(w.dir, "marker")), "a setup step ran");
  } finally {
    w.done();
  }
});

test("a box advertises BOX_ADVERTISE_HOST to its clients and tells its services BOX_BIND_HOST: the env file is what an attached specialist reads, and the advertised addresses answer (shared workroom instance)", async () => {
  const w = world();
  try {
    // 127.0.0.2 is an address of this host other than the one the services talk on: a box bound to 0.0.0.0 answers there too
    const up = w.run(["up"], {
      BOX_BIND_HOST: "0.0.0.0",
      BOX_DOCKERENV: process.execPath,
      BOX_ADDRS: "10.0.0.5",
      BOX_ADVERTISE_HOST: "127.0.0.2",
    });
    assert.equal(up.status, 0, up.stderr);
    const env = readFileSync(up.stdout.trim(), "utf8");
    assert.match(env, /BOX_URL=http:\/\/127\.0\.0\.2:\d+/);
    assert.match(env, /BOX_URL_APP=http:\/\/127\.0\.0\.2:\d+/);
    assert.match(env, /DATABASE_URL=\S*@127\.0\.0\.2:1\/x/);
    // the advertised address is the one a client uses: it answers
    const url = /BOX_URL=(\S+)/.exec(env)[1];
    assert.equal((await get(url)).bind, "0.0.0.0");
    assert.equal(w.run(["down"]).status, 0);
    // by default nothing is rewritten
    const plain = w.run(["up"]);
    const e2 = readFileSync(plain.stdout.trim(), "utf8");
    assert.match(e2, /BOX_URL=http:\/\/127\.0\.0\.1:\d+/);
    w.run(["down"]);
    // an address clients are told that the services do not listen on is refused before anything is made
    const bad = w.run(["up"], { BOX_ADVERTISE_HOST: "10.9.8.7" });
    assert.equal(bad.status, 2);
    assert.match(bad.stderr, /only when BOX_BIND_HOST is 0\.0\.0\.0/);
    assert.deepEqual(w.left(), []);
  } finally {
    w.done();
  }
});

test("a box bound to 0.0.0.0 comes up and answers on this host too; any other bind is refused before anything is made", async () => {
  const w = world();
  try {
    const up = w.run(["up"], {
      BOX_BIND_HOST: "0.0.0.0",
      BOX_DOCKERENV: process.execPath,
      BOX_ADDRS: "10.0.0.5",
      BOX_ADVERTISE_HOST: "127.0.0.1",
    });
    assert.equal(up.status, 0, up.stderr);
    const env = readFileSync(up.stdout.trim(), "utf8");
    const port = /BOX_PORT=(\d+)/.exec(env)[1];
    assert.equal((await get(`http://127.0.0.1:${port}`)).bind, "0.0.0.0");
    assert.equal(w.run(["down"]).status, 0);
    // on a bare host (no container marker) 0.0.0.0 is refused: it would serve every network the host is on
    const bare = w.run(["up"], {
      BOX_BIND_HOST: "0.0.0.0",
      BOX_DOCKERENV: "/nonexistent",
    });
    assert.equal(bare.status, 2);
    assert.match(bare.stderr, /container with ONE address/);
    const two = w.run(["up"], {
      BOX_BIND_HOST: "0.0.0.0",
      BOX_DOCKERENV: process.execPath,
      BOX_ADDRS: "10.0.0.5 10.0.1.5",
    });
    assert.equal(two.status, 2, "a container on two networks is refused too");
    const bad = w.run(["up"], { BOX_BIND_HOST: "10.1.2.3" });
    assert.equal(bad.status, 2);
    assert.match(bad.stderr, /BOX_BIND_HOST is 127\.0\.0\.1/);
    assert.deepEqual(w.left(), []);
  } finally {
    w.done();
  }
});

test("run: the exit status is the command's, and the box is destroyed on a failure too", () => {
  const w = world();
  try {
    const r = w.run(["run", "--", "bash", "-c", "exit 7"]);
    assert.equal(r.status, 7);
    assert.deepEqual(w.left(), []);
  } finally {
    w.done();
  }
});

test("up, refresh, status, down: the app restarts on refresh; down removes everything and stops the app", async () => {
  const w = world();
  try {
    const up = w.run(["up"]);
    assert.equal(up.status, 0, up.stderr);
    const env = readFileSync(up.stdout.trim(), "utf8");
    const url = /BOX_URL=(\S+)/.exec(env)[1];
    const first = (await get(url)).id;
    assert.match(
      w.run(["status"]).stdout,
      new RegExp(url.replaceAll(".", "\\.")),
    );
    assert.equal(w.run(["refresh"]).status, 0);
    assert.notEqual(
      (await get(url)).id,
      first,
      "a new app process answers after refresh",
    );
    const down = w.run(["down"]);
    assert.equal(down.status, 0, down.stderr);
    assert.deepEqual(w.left(), []);
    assert.ok(await closed(url));
  } finally {
    w.done();
  }
});

// Admission: `box land` inside an instance that is already up starts and destroys nothing: the land it runs sees that instance's app and database server, and a failing run
// leaves the instance answering. It would fail if `box land --box` made a second box, tore the instance down on a failure, or ran with no instance named. Oracle: the real box
// script and a real box and app; the land it runs is a stub that reports what it was given (land's own tests prove what land does with it).
test("land --box / --url: inside an instance that is up; a second box is never made and a failing run leaves it usable", async () => {
  const w = world();
  const kit = mkdtempSync(join(tmpdir(), "box-kit-"));
  try {
    for (const f of ["box", "manifest.mjs", "versions.sh", "versions.mjs"]) {
      writeFileSync(join(kit, f), readFileSync(join(box, "..", f)), {
        mode: 0o755,
      });
    }
    writeFileSync(
      join(kit, "land"),
      '#!/usr/bin/env bash\necho "BOX=$BOX_ID URL=$LAND_INSTANCE_URL DB=$LAND_INSTANCE_DB ARGS=$*"; exit "${LAND_STUB_EXIT:-0}"\n',
      { mode: 0o755 },
    );
    const own = (args, env = {}) =>
      spawnSync("bash", [join(kit, "box"), ...args], {
        cwd: w.dir,
        env: { ...inherited, BOX_HOME: w.home, BOX_CONFIG: join(w.dir, ".spire/box.json"), LAND_CONFIG: join(w.dir, ".spire/land.json"), ...env },
        encoding: "utf8",
        timeout: 120_000,
      });
    const up = own(["up"]);
    assert.equal(up.status, 0, up.stderr);
    const env = readFileSync(up.stdout.trim(), "utf8");
    const url = /BOX_URL=(\S+)/.exec(env)[1];
    const id = /BOX_ID=(\S+)/.exec(env)[1];
    const failed = own(["land", "--box", id, "--slice", "1/2"], {
      LAND_STUB_EXIT: "13",
    });
    assert.equal(failed.status, 13, failed.stderr);
    assert.match(
      failed.stdout,
      new RegExp(
        `BOX=${id} URL=${url.replaceAll(".", "\\.")} DB=db://u@127\\.0\\.0\\.1:1/x ARGS=--slice 1/2`,
      ),
    );
    assert.equal(
      (await get(url)).pool,
      "local",
      "the failed run left the app answering",
    );
    assert.deepEqual(w.left(), [id], "and the one box it was");
    const named = own(["land"], { BOX_ID: id });
    assert.equal(named.status, 0, named.stderr);
    assert.match(
      named.stdout,
      new RegExp(`BOX=${id} `),
      "BOX_ID alone is attaching",
    );
    assert.deepEqual(w.left(), [id], "no second box");
    // a caller whose shell still names a box (BOX_ID) is not inside it when it names another instance
    const other = own(["land", "--url", "http://127.0.0.1:9"], {
      BOX_ID: id,
      LAND_INSTANCE_DB: "db://a@h:5/d",
    });
    assert.equal(other.status, 0, other.stderr);
    assert.match(
      other.stdout,
      /BOX= URL=http:\/\/127\.0\.0\.1:9 DB=db:\/\/a@h:5\/d/,
    );
    const viaEnv = own(["land", "--url", "http://127.0.0.1:9"], {
      LAND_INSTANCE_DB: "db://e@h:6/d",
    });
    assert.match(
      viaEnv.stdout,
      /URL=http:\/\/127\.0\.0\.1:9 DB=db:\/\/e@h:6\/d/,
      "the database server's URL may come by the environment, off argv",
    );
    assert.equal(
      own(["land", "--url", "http://127.0.0.1:9"]).status,
      2,
      "the database server goes with the URL",
    );
    const argv = own([
      "land",
      "--url",
      "http://127.0.0.1:9",
      "--db",
      "db://s@h:5/d",
    ]);
    assert.equal(argv.status, 2, "a secret URL is never taken from argv");
    assert.doesNotMatch(
      argv.stdout + argv.stderr,
      /db:\/\/s@h/,
      "and never echoed",
    );
    assert.equal(
      own(["land", "--box", id, "--url", "http://x"], { LAND_INSTANCE_DB: "y" })
        .status,
      2,
      "an instance is named once",
    );
    assert.deepEqual(w.left(), [id]);
    assert.equal(own(["down", id]).status, 0);
    assert.deepEqual(w.left(), []);
  } finally {
    rmSync(kit, { recursive: true, force: true });
    w.done();
  }
});

test("capture with no box up is a one-shot box: it captures and leaves nothing", () => {
  const w = world();
  try {
    const out = join(w.dir, "shots");
    const r = w.run(["capture", "--out", out]);
    assert.equal(r.status, 0, r.stderr);
    assert.match(
      readFileSync(join(out, "scene.txt"), "utf8"),
      /^one http:\/\/127\.0\.0\.1:\d+/,
    );
    assert.deepEqual(w.left(), []);
  } finally {
    w.done();
  }
});

// Admission: `box capture --url` takes its pictures from an instance that is ALREADY running (one the box did not start) and starts no second stack. Oracle: a real http server
// stands for the instance, the project's PostgreSQL recipe, setup step and services would each leave a marker if they ran, and the real box script runs the capture.
test("capture --url: the pictures come from the running instance; no wrap, setup, service or state is made", async () => {
  const w = world({
    wrap: { ensure: "touch ensured", run: "bash wrap.sh", exports: ["DATABASE_URL"] },
    capture: {
      scene: "one",
      run: 'echo "$BOX_SCENE $BOX_URL $DATABASE_URL" > "$BOX_OUT/scene.txt"; curl -sf "$BOX_URL/" > "$BOX_OUT/served.json"',
    },
  });
  let hits = 0;
  const instance = createServer((_req, res) => {
    hits += 1;
    res.end('{"instance":"already-running"}');
  });
  await new Promise((r) => instance.listen(0, "127.0.0.1", r));
  const url = `http://127.0.0.1:${instance.address().port}`;
  try {
    const out = join(w.dir, "shots");
    const r = await new Promise((done) => {
      const c = spawn("bash", [box, "capture", "--url", url, "--out", out], {
        cwd: w.dir,
        env: {
          ...inherited,
          BOX_HOME: w.home,
        BOX_CONFIG: join(w.dir, ".spire/box.json"),
        LAND_CONFIG: join(w.dir, ".spire/land.json"),
          BOX_CONFIG: join(w.dir, ".spire/box.json"),
          LAND_CONFIG: join(w.dir, ".spire/land.json"),
          DATABASE_URL: "db://x@127.0.0.1:1/y",
        },
      });
      let err = "";
      c.stderr.on("data", (d) => (err += d));
      c.on("close", (status) => done({ status, err }));
    });
    assert.equal(r.status, 0, r.err);
    assert.equal(
      readFileSync(join(out, "scene.txt"), "utf8"),
      `one ${url} db://x@127.0.0.1:1/y\n`,
    );
    assert.equal(
      readFileSync(join(out, "served.json"), "utf8"),
      '{"instance":"already-running"}',
    );
    assert.equal(hits, 1, "the capture asked the running instance, once");
    assert.equal(
      existsSync(join(w.dir, "ensured")),
      false,
      "the wrap was not asked for",
    );
    assert.equal(existsSync(join(w.dir, "marker")), false, "no setup step ran");
    assert.equal(existsSync(w.home), false, "the box made no state at all");
    const both = w.run(["capture", "--url", url, "--box", "box-x"]);
    assert.equal(both.status, 2, "an instance is named once");
  } finally {
    instance.close();
    w.done();
  }
});

test("capture: a relative --out is the caller's directory, not the repository root", () => {
  const w = world();
  try {
    const cwd = mkdtempSync(join(tmpdir(), "box-cwd-"));
    const r = spawnSync("bash", [box, "capture", "--out", "shots"], {
      cwd,
      env: {
        ...inherited,
        BOX_HOME: w.home,
        BOX_CONFIG: join(w.dir, ".spire/box.json"),
        LAND_CONFIG: join(w.dir, ".spire/land.json"),
        GIT_DIR: join(w.dir, ".git"),
        GIT_WORK_TREE: w.dir,
      },
      encoding: "utf8",
    });
    assert.equal(r.status, 0, r.stderr);
    assert.ok(existsSync(join(cwd, "shots/scene.txt")));
    rmSync(cwd, { recursive: true, force: true });
  } finally {
    w.done();
  }
});

test("a start that fails is a failure, with the app's words, and leaves nothing", () => {
  const w = world({
    services: [
      {
        name: "app",
        start: "node app.mjs",
        portEnv: "PORT",
        env: { BOX_TEST_DIE: "1" },
        ready: { path: "/", seconds: 20 },
        restarts: 0,
      },
    ],
  });
  try {
    const r = w.run(["up"]);
    assert.notEqual(r.status, 0);
    assert.match(r.stderr, /FAILED/);
    assert.deepEqual(w.left(), []);
  } finally {
    w.done();
  }
});

test("what the wrap needs, when it cannot be had, stops the box before anything is created (never a skip)", () => {
  const w = world({
    wrap: { ensure: "echo no bundle >&2; false", run: "bash wrap.sh" },
  });
  try {
    const r = w.run(["run", "--", "true"]);
    assert.notEqual(r.status, 0);
    assert.match(r.stderr, /never runs without it/);
    assert.deepEqual(w.left(), []);
  } finally {
    w.done();
  }
});

test("the wrap by land's own recipe: one definition, made when missing, a failure when it cannot be", () => {
  const w = world({ wrap: { environment: "pg", exports: ["DATABASE_URL"] } });
  const recipe = (extra) => {
    const f = join(w.dir, "land.json");
    writeFileSync(
      f,
      JSON.stringify({
        environments: {
          pg: {
            wrap: ["bash", "{root}/wrap.sh", "--"],
            bundle: {
              have: ["test", "-e", "{root}/made"],
              path: ["echo", "/b"],
              ...extra,
            },
            ifMissing: "fail",
          },
        },
      }),
    );
    return { LAND_CONFIG: f };
  };
  try {
    const made = w.run(
      ["run", "--", "true"],
      recipe({ ensure: ["touch", "{root}/made"] }),
    );
    assert.equal(made.status, 0, made.stderr);
    assert.ok(
      existsSync(join(w.dir, "made")),
      "the recipe's ensure ran because the bundle was missing",
    );
    rmSync(join(w.dir, "made"));
    const refused = w.run(["run", "--", "true"], recipe({ ensure: ["false"] }));
    assert.notEqual(refused.status, 0);
    assert.deepEqual(w.left(), []);
  } finally {
    w.done();
  }
});

test("a project that declares nothing gets a checkout and nothing else: no box.json, an empty one, and `{}` all come up at once, start no process and install nothing", () => {
  const w = world();
  try {
    // a checkout of another stack (a Rust crate beside a Swift package): the box assumes neither and leaves the tree exactly as it found it
    mkdirSync(join(w.dir, "src"));
    writeFileSync(join(w.dir, "Cargo.toml"), '[package]\nname = "hello"\nversion = "0.1.0"\n');
    writeFileSync(join(w.dir, "src/main.rs"), 'fn main() { println!("hello"); }\n');
    writeFileSync(join(w.dir, "Package.swift"), '// swift-tools-version:5.9\nimport PackageDescription\nlet package = Package(name: "Hello")\n');
    const tree = () => execFileSync("find", [".", "-path", "./.git", "-prune", "-o", "-print"], { cwd: w.dir, encoding: "utf8" }).split("\n").sort().join("\n");
    const before = tree();
    for (const recipe of [null, "{}", '{"box":1}']) {
      if (recipe === null) rmSync(join(w.dir, ".spire/box.json"));
      else writeFileSync(join(w.dir, ".spire/box.json"), recipe);
      const t = Date.now();
      const up = w.run(["up"]);
      assert.equal(up.status, 0, up.stderr);
      assert.match(up.stderr, /declares nothing.*a checkout and nothing else/);
      assert.ok(Date.now() - t < 10_000, "ready in seconds");
      const env = readFileSync(up.stdout.trim(), "utf8");
      assert.deepEqual(
        env.split("\n").filter(Boolean).map((l) => /^export (\w+)=/.exec(l)[1]),
        ["BOX_ID", "BOX_DIR", "PATH"],
        "the box's own variables and nothing else",
      );
      const dir = /BOX_DIR=(\S+)/.exec(env)[1];
      assert.deepEqual(
        readdirSync(dir).sort(),
        ["env", "ready", "serve.log", "tmp"],
        "no service, no log, no setup of the project's",
      );
      assert.equal(w.run(["down"]).status, 0);
      assert.deepEqual(w.left(), []);
      const r = w.run(["run", "--", "bash", "-c", 'echo "in $PWD with ${BOX_URL:-no url}"']);
      assert.equal(r.status, 0, r.stderr);
      assert.match(r.stdout, /with no url/);
    }
    assert.equal(existsSync(join(w.dir, "marker")), false, "no setup step ran");
    assert.equal(tree(), before, "up, run and down changed nothing in the tree (no target/, .build/, node_modules/)");
  } finally {
    w.done();
  }
});

test("`environment` is plain data every command and service of the box gets; a name that is no variable is refused", async () => {
  const w = world({ environment: { BOX_TEST_PLAIN: "a b", ANOTHER: "x" }, setup: [{ name: "see", run: 'echo "$BOX_TEST_PLAIN" > seen' }] });
  const bad = world({ environment: { "no-dash": "x" } });
  try {
    const up = w.run(["up"]);
    assert.equal(up.status, 0, up.stderr);
    assert.equal(readFileSync(join(w.dir, "seen"), "utf8"), "a b\n", "a setup step sees it");
    const env = readFileSync(up.stdout.trim(), "utf8");
    assert.match(env, /export BOX_TEST_PLAIN=a\\\ b/, "a client of the box reads it from the env file");
    w.run(["down"]);
    const r = bad.run(["up"]);
    assert.notEqual(r.status, 0);
    assert.match(r.stderr, /a variable name is letters, digits and _/);
  } finally {
    w.done();
    bad.done();
  }
});

test("a launcher that cannot start fails at once with the plain reason (command not found), never as a readiness timeout", () => {
  const w = world({
    services: [{ name: "app", start: "no-such-launcher-xyz --serve", portEnv: "PORT", ready: { path: "/", seconds: 60 }, restarts: 0 }],
  });
  const prep = world({
    services: [{ name: "app", prepare: "no-such-launcher-xyz", start: "node app.mjs", portEnv: "PORT", ready: { path: "/", seconds: 60 } }],
  });
  const step = world({ setup: [{ name: "build", run: "no-such-launcher-xyz" }] });
  const wrapped = world({ wrap: { run: "no-such-launcher-xyz", exports: [] } });
  try {
    for (const [x, words] of [[w, /FAILED: app exited before it was ready \(a command it runs was not found\).*not found/], [prep, /the app prepare step failed \(a command it runs was not found\)/], [step, /setup step build failed \(a command it runs was not found\)/], [wrapped, /the supervisor crashed \(a command it runs was not found\) \(wrap\.run: no-such-launcher-xyz\)/]]) {
      const t = Date.now();
      const r = x.run(["up"]);
      assert.notEqual(r.status, 0);
      assert.match(r.stderr, words);
      assert.ok(Date.now() - t < 20_000, "at once, not after the 60 s the probe would wait");
      assert.deepEqual(x.left(), []);
    }
  } finally {
    for (const x of [w, prep, step, wrapped]) x.done();
  }
});

test("a recipe the reader refuses (a wrap naming no environment of land.json) stops the box at once, never a half-read recipe", () => {
  const w = world({ wrap: { environment: "no-such-entry" } });
  try {
    const r = w.run(["up"]);
    assert.notEqual(r.status, 0);
    assert.match(r.stderr, /wrap\.environment no-such-entry is not in environments/);
    assert.match(r.stderr, /cannot read/);
    assert.deepEqual(w.left(), []);
  } finally {
    w.done();
  }
});

test("a box whose state dir is gone is still stopped by down, from its handle", async () => {
  const w = world();
  try {
    const up = w.run(["up"]);
    assert.equal(up.status, 0, up.stderr);
    const stateDir = up.stdout.trim().replace(/\/env$/, "");
    const url = /BOX_URL=(\S+)/.exec(readFileSync(up.stdout.trim(), "utf8"))[1];
    assert.ok(
      existsSync(w.home + ".handles"),
      "the handle lives outside the state dir",
    );
    rmSync(stateDir, { recursive: true, force: true });
    const down = w.run(["down", stateDir.split("/").pop()]);
    assert.equal(down.status, 0, down.stderr);
    assert.deepEqual(w.left(), []);
    assert.ok(await closed(url), "the app is stopped");
  } finally {
    w.done();
  }
});

// Admission: `box land` with no instance named (a one-shot box) works on a recipe that declares no services and no wrap: the generic checkout-only box. It would fail
// if the land setup read a variable only a declared wrap or service defines (WRAP_EXPORTS unbound). Oracle: the real box script, a real git repo with an empty recipe, a land stub.
test("land on a recipe with no services and no wrap: a checkout-only one-shot box", () => {
  const dir = mkdtempSync(join(tmpdir(), "box-bare-"));
  const kit = mkdtempSync(join(tmpdir(), "box-kit-"));
  try {
    for (const f of ["box", "manifest.mjs", "versions.sh", "versions.mjs"]) {
      writeFileSync(join(kit, f), readFileSync(join(box, "..", f)), { mode: 0o755 });
    }
    writeFileSync(join(kit, "land"), '#!/usr/bin/env bash\necho "LANDED EXPORTS=[$LAND_BOX_EXPORTS] ARGS=$*"\n', { mode: 0o755 });
    mkdirSync(join(dir, ".spire"));
    writeFileSync(join(dir, ".spire/box.json"), JSON.stringify({ box: 1 }));
    execFileSync("git", ["init", "-q"], { cwd: dir });
    const r = spawnSync("bash", [join(kit, "box"), "land", "--slice", "1/2"], {
      cwd: dir,
      env: { ...inherited, BOX_HOME: join(dir, "home"), BOX_CONFIG: join(dir, ".spire/box.json"), LAND_CONFIG: join(dir, ".spire/land.json") },
      encoding: "utf8",
      timeout: 120_000,
    });
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.stdout, /LANDED EXPORTS=\[\] ARGS=--slice 1\/2/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
    rmSync(kit, { recursive: true, force: true });
  }
});

test("run: the box outlives its TTL while the caller runs (a long land keeps its PostgreSQL) and its logs are kept; up's TTL still ends a forgotten box", async () => {
  const w = world();
  try {
    const keep = join(w.dir, "state");
    const probe = `sleep 6; node -e 'fetch(process.env.BOX_URL).then(r=>r.json()).then(j=>console.log("ALIVE "+j.id),()=>console.log("GONE"))'`;
    const r = w.run(["run", "--", "bash", "-c", probe], {
      BOX_TTL_MIN: "0",
      LAND_STATE_DIR: keep,
    });
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.stdout, /ALIVE/, "a TTL of 0 would have ended the box before the caller did");
    assert.ok(existsSync(join(keep, "box-logs/app.log")), "the box's logs are kept in the land's state dir");
    assert.deepEqual(w.left(), []);
    const u = w.run(["up"], { BOX_TTL_MIN: "0" });
    assert.equal(u.status, 0, u.stderr);
    for (let i = 0; i < 40 && w.left().length; i++) await new Promise((res) => setTimeout(res, 250));
    assert.deepEqual(w.left(), [], "up has no owner: its TTL ends it");
    // a `box up` the one-shot's own command makes is not owned by that run: its TTL still ends it
    w.run(["run", "--", "bash", "-c", `bash ${box} up >/dev/null; sleep 4`], { BOX_TTL_MIN: "0" });
    for (let i = 0; i < 40 && w.left().length; i++) await new Promise((res) => setTimeout(res, 250));
    assert.deepEqual(w.left(), [], "the nested box is not owned by the run: its TTL ended it");
    // a caller killed without its trap leaves no box behind
    const k = spawn("bash", [box, "run", "--", "sleep", "600"], { cwd: w.dir, env: { ...inherited, BOX_HOME: w.home, BOX_CONFIG: join(w.dir, ".spire/box.json"), LAND_CONFIG: join(w.dir, ".spire/land.json") } });
    for (let i = 0; i < 80 && !w.left().length; i++) await new Promise((res) => setTimeout(res, 250));
    assert.equal(w.left().length, 1, "the run's box is up");
    k.kill("SIGKILL");
    for (let i = 0; i < 40 && w.left().length; i++) await new Promise((res) => setTimeout(res, 250));
    assert.deepEqual(w.left(), [], "the owner is gone: the box stops itself");
  } finally {
    w.done();
  }
});
