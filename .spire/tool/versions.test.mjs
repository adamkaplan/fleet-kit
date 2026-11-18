// Admission: every command spire-land runs uses the PROJECT'S pinned tool versions, never the host's defaults. These tests show: the pins are read from the project's own files
// (.node-version, .nvmrc, mise.toml, .tool-versions, package.json); on a host whose default node is NOT the pinned one the pinned node is what a child process runs (a real child
// process, started through the real versions.mjs, shell entry and spawn wrappers); a pin the host cannot honour is REFUSED with the tool, the pin, what is installed and how to install
// it, and nothing runs (no fallback to the host default); a process running another node than the pinned one is re-run under it; a startup line names the resolved versions; and
// no code of the tool outside versions.mjs starts a process on its own (a raw child_process import fails the grep), every shell entry pins before it runs anything.
// They would fail if a pin were ignored, if the host default were used when the pin is missing, if a spawn site bypassed the resolver, or if an entry ran before pinning.
// Oracle: real child processes on real files. Hosts are built in a temp dir: a default node 26 on PATH and node 22/25 installed where mise keeps them (MISE_DATA_DIR); each "node" is a
// stub that reports its version (the resolver is what is under test) and runs the real node for anything else; the last test uses the REAL installed node versions when two are present.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { readPins, satisfies } from "./versions.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const PINS = join(here, "versions.mjs");
const VERSIONS_SH = join(here, "versions.sh");

const tmp = mkdtempSync(join(tmpdir(), "pins-test-"));
test.after(() => rmSync(tmp, { recursive: true, force: true }));
let n = 0;
const fresh = (name) => {
  const d = join(tmp, `${name}-${++n}`);
  mkdirSync(d, { recursive: true });
  return d;
};

/** A "node" that says it is `version` and runs the real node for any real work. */
function stubNode(dir, version) {
  mkdirSync(join(dir, "bin"), { recursive: true });
  const f = join(dir, "bin", "node");
  writeFileSync(
    f,
    `#!/bin/sh\ncase "$1" in --version|-v) echo v${version}; exit 0;; -p) echo ${version}; exit 0;; esac\nexec ${process.execPath} "$@"\n`,
  );
  chmodSync(f, 0o755);
  return join(dir, "bin");
}

/** The directories a test host's PATH ends with: where its own python3 is, whatever the process running the test has on its PATH. */
const HOST_PATH = "/usr/bin:/bin";

/** A host: default node 26.6.0 first in PATH; node 22.77.2, 22.77.3 and 25.0.0 installed under mise's layout. */
function host(versions = ["22.77.2", "22.77.3", "25.0.0"]) {
  const root = fresh("host");
  const hostbin = stubNode(join(root, "default"), "26.6.0");
  const data = join(root, "mise");
  for (const v of versions) {
    stubNode(join(data, "installs", "node", v), v);
  }
  // mise keeps short names as links to the real directories: the resolver must not count them twice
  symlinkSync("22.77.3", join(data, "installs", "node", "22"));
  const env = {
    PATH: `${hostbin}:${HOST_PATH}`,
    MISE_DATA_DIR: data,
    HOME: root,
    LANG: "C",
  };
  return { root, hostbin, data, env };
}

function project(files) {
  const dir = fresh("project");
  spawnSync("git", ["init", "-q"], { cwd: dir });
  for (const [f, text] of Object.entries(files)) {
    writeFileSync(join(dir, f), text);
  }
  return dir;
}

const run = (cmd, args, env, opts = {}) =>
  spawnSync(cmd, args, { encoding: "utf8", env, ...opts });
const pinsExec = (h, dir, ...cmd) =>
  run(process.execPath, [PINS, "exec", dir, "--", ...cmd], h.env);

test("a version satisfies an exact pin, a prefix, a wildcard and a range; text that is none of them is refused", () => {
  assert.ok(satisfies("22.77.3", "22.77.3"));
  assert.ok(satisfies("22.77.3", "v22"));
  assert.ok(satisfies("22.77.3", "22.77"));
  assert.ok(!satisfies("22.77.3", "22.76"));
  assert.ok(!satisfies("26.6.0", "22"));
  assert.ok(satisfies("22.77.3", "22.x"));
  assert.ok(satisfies("22.77.3", ">=22"));
  assert.ok(!satisfies("26.6.0", ">=22 <24"));
  assert.ok(satisfies("26.6.0", ">=22 <24 || ^26"));
  assert.ok(satisfies("22.77.3", "^22.1"));
  assert.ok(!satisfies("23.0.0", "^22.1"));
  assert.ok(satisfies("22.1.9", "~22.1.2"));
  assert.ok(!satisfies("22.2.0", "~22.1.2"));
  assert.ok(satisfies("1.2.3", "*"));
  assert.throws(() => satisfies("22.0.0", "lts/*"), /not a version or a version range/);
});

test("the pins are read from .node-version, .nvmrc, mise.toml, .tool-versions and package.json, in that order", () => {
  const dir = project({
    ".node-version": "v22.77.3\n",
    ".nvmrc": "22 # same\n",
    "mise.toml": `[tools]\nnode = "22.23"\npython = ["3.12", "3.11"]\ngo = { version = "1.22.1" }\n[env]\nnode = "bad"\n`,
    ".tool-versions": "# comment\nnode 22.77.3\nruby 3.3.0\n",
    "package.json": JSON.stringify({ engines: { node: ">=22 <23" }, volta: { node: "22.77.3" } }),
  });
  assert.deepEqual(
    readPins(dir).map((p) => `${p.tool} ${p.spec} <- ${p.source}`),
    [
      "node v22.77.3 <- .node-version",
      "node 22 <- .nvmrc",
      "node 22.23 <- mise.toml",
      "python 3.12 <- mise.toml",
      "go 1.22.1 <- mise.toml",
      "node 22.77.3 <- .tool-versions",
      "ruby 3.3.0 <- .tool-versions",
      "node 22.77.3 <- package.json volta.node",
      "node >=22 <23 <- package.json engines.node",
    ],
  );
});

test("a project pinning node 22 runs a child process under the highest installed 22, on a host whose default is 26", () => {
  const h = host();
  const dir = project({ ".node-version": "22\n" });
  const bare = run("node", ["--version"], h.env); // the host's own default
  assert.equal(bare.stdout.trim(), "v26.6.0");
  const r = pinsExec(h, dir, "sh", "-c", "node --version; echo $SPIRE_PINNED");
  assert.equal(r.status, 0, r.stderr);
  assert.equal(r.stdout, "v22.77.3\nnode@22.77.3\n");
  // the startup line names what every command of the run uses
  assert.match(r.stderr, /^spire-land: pins: node 22\.77\.3 \(pin 22 in \.node-version\)\n$/);
});

test("an exact pin is the exact version; a package.json engines range alone picks the highest installed version it allows", () => {
  const h = host();
  const exact = project({ ".node-version": "22.77.2\n" });
  assert.equal(pinsExec(h, exact, "node", "--version").stdout.trim(), "v22.77.2");
  const ranged = project({ "package.json": JSON.stringify({ engines: { node: ">=22 <25" } }) });
  assert.equal(pinsExec(h, ranged, "node", "--version").stdout.trim(), "v22.77.3"); // the highest installed that the range allows
});

test("a pin the host does not have is REFUSED with the tool, the pin, what is installed and how to install it, and nothing runs", () => {
  const h = host();
  const dir = project({ ".node-version": "22.99.0\n" });
  const marker = join(h.root, "ran");
  const r = pinsExec(h, dir, "sh", "-c", `touch ${marker}`);
  assert.equal(r.status, 1);
  assert.match(r.stderr, /node: the project pins 22\.99\.0 \(\.node-version\) and this host has no such version/);
  assert.match(r.stderr, /Installed: .*25\.0\.0, 22\.77\.3, 22\.77\.2\./); // the fake host's own versions; a real node the test host has on its PATH may be listed too
  assert.match(r.stderr, /Install it: mise install node@22\.99\.0\./);
  assert.match(r.stderr, /never falls back to the host default \(node \d+\.\d+\.\d+ on PATH\)/);
  assert.ok(!existsSync(marker), "the command must not run on the host's default");
});

test("pins that disagree are refused; a pin that is not a version is refused", () => {
  const h = host();
  const split = project({ ".node-version": "22\n", "package.json": JSON.stringify({ engines: { node: ">=24" } }) });
  const r = pinsExec(h, split, "node", "--version");
  assert.equal(r.status, 1);
  assert.match(r.stderr, /the project's pins disagree: 22 \(\.node-version\) is met by 22\.77\.3, 22\.77\.2, but >=24 \(package\.json engines\.node\)/);
  const alias = project({ ".nvmrc": "lts/*\n" });
  const a = pinsExec(h, alias, "node", "--version");
  assert.equal(a.status, 1);
  assert.match(a.stderr, /node: the project pins "lts\/\*" \(\.nvmrc\), which is not a version or a range/);
});

// the hosts' own /usr/bin may hold a real python3: the list of what is installed has its entries before, between and after the stubs'
// The host's own python3 is read from the PATH the test hosts have (HOST_PATH, not the one this process runs with: they differ where mise puts its own first), never hard-coded: it may be listed anywhere among the stubs' entries (`installed`); any other version there fails.
const hostPyVersion = (() => {
  const r = spawnSync("python3", ["--version"], { encoding: "utf8", env: { PATH: HOST_PATH } });
  return /Python (\d+(?:\.\d+)+)/.exec(`${r.stdout}${r.stderr}`)?.[1] ?? null;
})();
// The "Installed: a, b, c" list the refusal prints, as a pattern: the entries the test made, in order, with the host's own python3 (a PATH entry) allowed anywhere between them or after them (it sits wherever the host's PATH puts it, and may be the same version as a stub).
const installed = (...entries) => {
  const host = hostPyVersion ? `${hostPyVersion.replaceAll(".", "\\.")} \\(PATH\\)` : null;
  const gap = host ? `(?:${host}, )*` : "";
  return `${gap}${entries.join(`, ${gap}`)}${host ? `(?:, ${host})*` : ""}`;
};

test("a tool other than node is pinned from mise.toml and found in mise's installs, and refused when missing", () => {
  const h = host();
  const bin = join(h.data, "installs", "python", "3.12.4", "bin");
  mkdirSync(bin, { recursive: true });
  writeFileSync(join(bin, "python"), "#!/bin/sh\necho python 3.12.4\n", { mode: 0o755 });
  const dir = project({ "mise.toml": `[tools]\npython = "3.12"\n` });
  const r = pinsExec(h, dir, "python");
  assert.equal(r.stdout.trim(), "python 3.12.4", r.stderr);
  assert.match(r.stderr, /python 3\.12\.4 \(pin 3\.12 in mise\.toml\)/);
  const missing = project({ "mise.toml": `[tools]\npython = "3.99"\n` });
  const m = pinsExec(h, missing, "python");
  assert.equal(m.status, 1);
  assert.match(m.stderr, new RegExp(`python: the project pins 3\\.99 \\(mise\\.toml\\).*Installed: ${installed("3\\.12\\.4")}\\. Install it: mise install python@3\\.99\\.`));
});

/** A "python" that says it is `version`: `python3` (and `python`, when `both`) in `dir`; any other argument echoes the version, so a step shows which interpreter it ran. */
function stubPython(dir, version, { both = true } = {}) {
  mkdirSync(dir, { recursive: true });
  for (const name of both ? ["python3", "python"] : ["python3"]) {
    writeFileSync(
      join(dir, name),
      `#!/bin/sh\ncase "$1" in --version|-V) echo Python ${version};; *) echo "${name} ${version} $*";; esac\n`,
      { mode: 0o755 },
    );
  }
  return dir;
}

test("one python pin: every command's plain python3 is the pinned interpreter, on a host whose default python3 is another (mise's installs)", () => {
  const h = host();
  const sys = stubPython(join(h.root, "sys"), "3.14.8");
  stubPython(join(h.data, "installs", "python", "3.12.16", "bin"), "3.12.16");
  stubPython(join(h.data, "installs", "python", "3.12.2", "bin"), "3.12.2");
  // mise keeps short names ("3", "3.12") as links to the real install, and they sort before it: the pin 3.12.16 must still find the real 3.12.16
  symlinkSync("3.12.16", join(h.data, "installs", "python", "3.12"));
  symlinkSync("3.12.16", join(h.data, "installs", "python", "3"));
  const env = { ...h.env, PATH: `${sys}:${h.env.PATH}` };
  const dir = project({ ".tool-versions": "python 3.12.16\n" });
  const r = run(process.execPath, [PINS, "exec", dir, "--", "python3", "-m", "venv", "x"], env);
  assert.equal(r.stdout.trim(), "python3 3.12.16 -m venv x", r.stderr);
  assert.match(r.stderr, /python 3\.12\.16 \(pin 3\.12\.16 in \.tool-versions\)/);
  // the same through a child the tool starts (a step): nothing host-keyed, the PATH alone decides
  const viaSh = run(process.execPath, [PINS, "exec", dir, "--", "sh", "-c", "python3 -m venv y"], env);
  assert.equal(viaSh.stdout.trim(), "python3 3.12.16 -m venv y", viaSh.stderr);
  // no pin: today's behaviour, the host's python3
  const none = project({ ".node-version": "22\n" });
  const n = run(process.execPath, [PINS, "exec", none, "--", "python3", "x"], env);
  assert.equal(n.stdout.trim(), "python3 3.14.8 x", n.stderr);
});

test("a python pin the host cannot honour refuses, loudly, with how to install it, and never falls back to the host's python3", () => {
  const h = host();
  const sys = stubPython(join(h.root, "sys"), "3.14.8");
  stubPython(join(h.data, "installs", "python", "3.12.2", "bin"), "3.12.2");
  const env = { ...h.env, PATH: `${sys}:${h.env.PATH}` };
  const dir = project({ ".tool-versions": "python 3.12.88\n" });
  const r = run(process.execPath, [PINS, "exec", dir, "--", "python3", "x"], env);
  assert.equal(r.status, 1);
  assert.equal(r.stdout, "");
  assert.match(
    r.stderr,
    new RegExp(
      `python: the project pins 3\\.12\\.88 \\(\\.tool-versions\\) and this host has no such version\\. Installed: ${installed("3\\.14\\.8 \\(PATH\\)", "3\\.12\\.2")}\\. Install it: mise install python@3\\.12\\.88\\. Refusing to run: spire-land never falls back to the host default`,
    ),
  );
});

test("a python pin is met by the interpreter a box image's recipe puts on PATH (python3 only, no mise install), and by none other", () => {
  const h = host();
  const recipe = stubPython(join(h.root, "usr-local-bin"), "3.12.16", { both: false });
  const dir = project({ "mise.toml": `[tools]\npython = "3.12.16"\n` });
  const env = { ...h.env, PATH: `${recipe}:${h.env.PATH}` };
  const r = run(process.execPath, [PINS, "exec", dir, "--", "python3", "x"], env);
  assert.equal(r.stdout.trim(), "python3 3.12.16 x", r.stderr);
  assert.match(r.stderr, /python 3\.12\.16 \(pin 3\.12\.16 in mise\.toml, from PATH\)/);
  // another patch of the same minor is not the pin
  const other = project({ "mise.toml": `[tools]\npython = "3.12.88"\n` });
  const o = run(process.execPath, [PINS, "exec", other, "--", "python3", "x"], env);
  assert.equal(o.status, 1);
  assert.match(o.stderr, new RegExp(`python: the project pins 3\\.12\\.88.*Installed: ${installed("3\\.12\\.16 \\(PATH\\)")}\\. Install it: mise install python@3\\.12\\.88`));
});

// A pin SOURCE: the project's land.json names the file its version is the one production is built from (a Dockerfile's FROM line); the resolver reads it, there is no second pin file.
const LAND = (pins) => JSON.stringify({ version: 1, pins });
const EAR = [{ source: "ear/Dockerfile", from: "python" }];
function pythonHost() {
  const h = host();
  const sys = stubPython(join(h.root, "sys"), "3.14.8");
  stubPython(join(h.data, "installs", "python", "3.11.9", "bin"), "3.11.9");
  stubPython(join(h.data, "installs", "python", "3.12.16", "bin"), "3.12.16");
  return { ...h, env: { ...h.env, PATH: `${sys}:${h.env.PATH}` } };
}
function sourced(files, pins = EAR) {
  const dir = project({ ...files });
  mkdirSync(join(dir, ".spire"), { recursive: true });
  writeFileSync(join(dir, ".spire", "land.json"), LAND(pins));
  mkdirSync(join(dir, "ear"), { recursive: true });
  return dir;
}
const dockerfile = (dir, text, path = "ear/Dockerfile") => {
  mkdirSync(dirname(join(dir, path)), { recursive: true });
  writeFileSync(join(dir, path), text);
};
const py = (h, dir) => run(process.execPath, [PINS, "exec", dir, "--", "python3", "x"], h.env);

test("a pin source: the step's python3 is the version of the Dockerfile's FROM line, and follows the Dockerfile with no other edit", () => {
  const h = pythonHost();
  const dir = sourced({});
  dockerfile(dir, "# the ear\nFROM python:3.11-slim\nWORKDIR /app\n");
  const a = py(h, dir);
  assert.equal(a.stdout.trim(), "python3 3.11.9 x", a.stderr);
  assert.match(a.stderr, /python 3\.11\.9 \(pin 3\.11 in ear\/Dockerfile FROM python\)/);
  // production moves to 3.12: nothing else is edited
  dockerfile(dir, "FROM python:3.12-slim-bookworm AS app\n");
  const b = py(h, dir);
  assert.equal(b.stdout.trim(), "python3 3.12.16 x", b.stderr);
  // an exact tag is an exact pin; a registry path, --platform and a digest are the same line
  dockerfile(dir, "FROM --platform=$BUILDPLATFORM docker.io/library/python:3.11.9-slim@sha256:abc\n");
  assert.equal(py(h, dir).stdout.trim(), "python3 3.11.9 x");
  // a stage that builds from another stage, and other images, are not the python pin
  dockerfile(dir, "FROM node:22 AS web\nFROM python:3.11-slim AS base\nFROM base AS app\n");
  assert.equal(py(h, dir).stdout.trim(), "python3 3.11.9 x");
});

test("a pin source: the version the Dockerfile names that the host does not have refuses with how to install it, never the host's python3", () => {
  const h = pythonHost();
  const dir = sourced({});
  dockerfile(dir, "FROM python:3.10-slim\n");
  const r = py(h, dir);
  assert.equal(r.status, 1);
  assert.equal(r.stdout, "");
  assert.match(r.stderr, /python: the project pins 3\.10 \(ear\/Dockerfile FROM python\) and this host has no such version\..*Install it: mise install python@3\.10\. Refusing to run/);
});

test("a pin source that cannot be resolved refuses loudly and names the file: a missing file, no such FROM line, a tag that is no version, a bad entry", () => {
  const h = pythonHost();
  const refused = (dir, re) => {
    const r = py(h, dir);
    assert.equal(r.status, 1, r.stderr);
    assert.equal(r.stdout, "");
    assert.match(r.stderr, re);
  };
  refused(sourced({}), /spire-land: pin source ear\/Dockerfile \(FROM python\): no such file in the project/);
  const none = sourced({});
  dockerfile(none, "FROM node:22.23.4-bookworm-slim\nFROM ${BASE}\n");
  refused(none, /pin source ear\/Dockerfile \(FROM python\): no `FROM python:VERSION` line/);
  for (const [line, why] of [["FROM python:slim", /"slim" names no version/], ["FROM python:latest", /"latest" names no version/], ["FROM python:${PY}-slim", /"\$\{PY\}-slim" names no version/], ["FROM python@sha256:abc", /names no version \(it is pinned by digest only\)/], ["FROM python", /names no version/]]) {
    const d = sourced({});
    dockerfile(d, `${line}\n`);
    refused(d, new RegExp(`pin source ear/Dockerfile \\(FROM python\\), line 1: .*${why.source}`));
  }
  for (const bad of [[{ source: "ear/Dockerfile" }], [{ from: "python" }], [{ source: "../x/Dockerfile", from: "python" }], [{ source: "/etc/passwd", from: "python" }], ["ear/Dockerfile"], [{ source: "ear/Dockerfile", from: "py thon" }]]) {
    const d = sourced({}, bad);
    dockerfile(d, "FROM python:3.11\n");
    refused(d, /spire-land: pins in \.spire\/land\.json: entry 1 /);
  }
  const notArray = sourced({}, { source: "ear/Dockerfile", from: "python" });
  refused(notArray, /pins in \.spire\/land\.json: must be a list/);
});

test("a pin source is a file of the project (a link out of it refuses), and is read from the manifest the other readers read (LAND_CONFIG names another file)", () => {
  const h = pythonHost();
  const outside = fresh("outside");
  writeFileSync(join(outside, "Dockerfile"), "FROM python:3.11\n");
  const dir = sourced({});
  symlinkSync(join(outside, "Dockerfile"), join(dir, "ear", "Dockerfile"));
  const r = py(h, dir);
  assert.equal(r.status, 1);
  assert.match(r.stderr, /pin source ear\/Dockerfile \(FROM python\): the file is outside the project/);
  // a directory is no file, and a name that merely begins with two dots is inside the project
  const asDir = sourced({});
  mkdirSync(join(asDir, "ear", "Dockerfile"));
  assert.match(py(h, asDir).stderr, /pin source ear\/Dockerfile \(FROM python\): it is not a file/);
  const dots = sourced({}, [{ source: "..conf/Dockerfile", from: "python" }]);
  dockerfile(dots, "FROM python:3.11-slim\n", "..conf/Dockerfile");
  assert.equal(py(h, dots).stdout.trim(), "python3 3.11.9 x");
  // another land.json by LAND_CONFIG, as base.mjs and config.mjs honour it: its pins are the project's
  const plain = project({});
  dockerfile(plain, "FROM python:3.12-slim\n");
  const other = join(fresh("cfg"), "land.json");
  writeFileSync(other, LAND(EAR));
  const viaEnv = run(process.execPath, [PINS, "exec", plain, "--", "python3", "x"], { ...h.env, LAND_CONFIG: other });
  assert.equal(viaEnv.stdout.trim(), "python3 3.12.16 x", viaEnv.stderr);
  // a manifest that is not JSON refuses with the file named, never a silent host python
  writeFileSync(other, "{ not json");
  const bad = run(process.execPath, [PINS, "exec", plain, "--", "python3", "x"], { ...h.env, LAND_CONFIG: other });
  assert.equal(bad.status, 1);
  assert.match(bad.stderr, /cannot read the project's files to find its pin sources: manifest: cannot read .*land\.json/);
});

test("a pin source and the project's other pins of the tool must agree: the resolver refuses a Dockerfile that disagrees with .tool-versions, and node reads the same way", () => {
  const h = pythonHost();
  const dir = sourced({ ".tool-versions": "python 3.12.16\n" });
  dockerfile(dir, "FROM python:3.11-slim\n");
  const r = py(h, dir);
  assert.equal(r.status, 1);
  assert.match(r.stderr, /the project's pins disagree/);
  // node: several Dockerfiles, one resolution; one that names another version refuses
  const n = sourced({}, [{ source: "Dockerfile", from: "node" }, { source: "docker/b/Dockerfile", from: "node" }]);
  dockerfile(n, "FROM node:22.77.3-bookworm-slim@sha256:abc AS publishing\n", "Dockerfile");
  dockerfile(n, "FROM node:22.77.3-alpine\nFROM node:22.77.3-alpine AS run\n", "docker/b/Dockerfile");
  const ok = run(process.execPath, [PINS, "exec", n, "--", "node", "--version"], h.env);
  assert.equal(ok.stdout.trim(), "v22.77.3", ok.stderr);
  assert.match(ok.stderr, /node 22\.77\.3 \(pin 22\.77\.3 in Dockerfile FROM node\)/);
  dockerfile(n, "FROM node:22.77.2-alpine\n", "docker/b/Dockerfile");
  const bad = run(process.execPath, [PINS, "exec", n, "--", "node", "--version"], h.env);
  assert.equal(bad.status, 1);
  assert.match(bad.stderr, /the project's pins disagree/);
});

test("the pin source is read by every entry before anything runs (land refuses an unresolvable source with exit 1)", () => {
  const h = pythonHost();
  const dir = sourced({});
  dockerfile(dir, "FROM python:slim\n");
  for (const [entry, args] of [["land", []], ["box", ["up"]], ["checks.sh", ["origin/main"]]]) {
    const r = run(join(here, entry), args, h.env, { cwd: dir });
    assert.equal(r.status, 1, `${entry}: ${r.stderr}`);
    assert.match(r.stderr, /spire-land: pin source ear\/Dockerfile \(FROM python\), line 1/, entry);
  }
});

test("a project that pins nothing says so and runs the host's tools", () => {
  const h = host();
  const dir = project({ "README.md": "x" });
  const r = pinsExec(h, dir, "node", "--version");
  assert.equal(r.stdout.trim(), "v26.6.0");
  assert.match(r.stderr, /pins: none declared by the project/);
});

test("the shell entries' spire_versions pins the shell and everything it starts, says it once, and returns 1 with the reason on a refusal", () => {
  const h = host();
  const dir = project({ ".node-version": "22\n" });
  const sh = (body) => run("bash", ["-c", `. ${VERSIONS_SH}; ${body}`], { ...h.env, PATH: `${h.hostbin}:/usr/bin:/bin` });
  // node must be on PATH for versions.sh itself: the host's own default is what reads the pins
  const r = sh(`spire_versions ${dir} && spire_versions ${dir} && node --version && bash -c 'node --version'`);
  assert.equal(r.status, 0, r.stderr);
  assert.equal(r.stdout, "v22.77.3\nv22.77.3\n");
  assert.equal(r.stderr.match(/spire-land: pins:/g).length, 1, "one startup line for one project");
  const bad = project({ ".node-version": "22.99.0\n" });
  const b = sh(`spire_versions ${bad} || { echo refused-$?; exit 3; }; echo RAN`);
  assert.equal(b.status, 3);
  assert.match(b.stdout, /^refused-1\n$/);
  assert.match(b.stderr, /node: the project pins 22\.99\.0/);
});

test("the entries (land, council, checks.sh, box) refuse a missing pin before they run anything", () => {
  const h = host();
  const dir = project({ ".node-version": "22.99.0\n" });
  for (const [entry, args] of [["land", []], ["council", []], ["checks.sh", ["origin/main"]], ["box", ["up"]], ["box", ["land"]], ["box", ["run"]]]) {
    const r = run(join(here, entry), args, h.env, { cwd: dir });
    assert.equal(r.status, 1, `${entry}: ${r.stderr}`);
    assert.match(r.stderr, /spire-land: node: the project pins 22\.99\.0 \(\.node-version\) and this host has no such version/, entry);
    assert.doesNotMatch(r.stderr, /not in a git worktree/, entry);
  }
});

test("the spawn wrappers put the pins first in a child's PATH whatever env the caller gives, and resolve the pins themselves when nobody has", () => {
  const h = host();
  const dir = project({ ".node-version": "22\n" });
  const script = `
    import { execFileSync, spawnSync, spawn } from ${JSON.stringify(PINS)};
    const a = execFileSync("node", ["--version"], { encoding: "utf8" }).trim();
    const b = spawnSync("node", ["--version"], { encoding: "utf8", env: { PATH: "${h.hostbin}:/usr/bin:/bin" } }).stdout.trim();
    const c = await new Promise((res) => { let o = ""; const p = spawn("node", ["--version"], { env: { PATH: "${h.hostbin}:/usr/bin:/bin" } }); p.stdout.on("data", (d) => (o += d)); p.on("close", () => res(o.trim())); });
    console.log([a, b, c].join(" "));`;
  const r = run(process.execPath, ["--input-type=module", "-e", script], h.env, { cwd: dir });
  assert.equal(r.status, 0, r.stderr);
  assert.equal(r.stdout.trim(), "v22.77.3 v22.77.3 v22.77.3");
  // the same wrappers refuse, throwing, when the pin cannot be honoured: nothing is started on the default
  const bad = project({ ".node-version": "22.99.0\n" });
  const t = run(process.execPath, ["--input-type=module", "-e", script], h.env, { cwd: bad });
  assert.notEqual(t.status, 0);
  assert.match(t.stderr, /the project pins 22\.99\.0/);
});

test("a process that runs another node than the pinned one is re-run under the pinned node (applyPins), once", () => {
  const h = host();
  const dir = project({ ".node-version": "22\n" });
  const f = join(h.root, "entry.mjs");
  writeFileSync(
    f,
    `import { applyPins } from ${JSON.stringify(PINS)};\napplyPins();\nconsole.log("reexec=" + process.env.SPIRE_REEXEC, "pinned=" + process.env.SPIRE_PINNED);\n`,
  );
  const r = run(process.execPath, [f], h.env, { cwd: dir });
  assert.equal(r.status, 0, r.stderr);
  // the stub node 22 claims to be 22.77.3 and runs the real node: the second run is marked and does not loop
  assert.equal(r.stdout.trim(), "reexec=1 pinned=node@22.77.3");
  assert.equal(r.stderr.match(/spire-land: pins:/g).length, 1);
  const bad = project({ ".node-version": "22.99.0\n" });
  const b = run(process.execPath, [f], h.env, { cwd: bad });
  assert.equal(b.status, 1);
  assert.match(b.stderr, /Install it: mise install node@22\.99\.0/);
  assert.equal(b.stdout, "");
});

const SOURCE = readdirSync(here, { withFileTypes: true })
  .filter((e) => e.isFile() && !/\.test\.mjs$/.test(e.name))
  .map((e) => e.name);
test("every spawn site goes through the resolver: no code of the tool but versions.mjs touches child_process, and every shell entry pins first", () => {
  const raw = SOURCE.filter((f) => /child_process|\bexecSync\b|Bun\.spawn|process\.binding/.test(readFileSync(join(here, f), "utf8")));
  assert.deepEqual(raw.filter((f) => f !== "versions.mjs" && f !== "versions.test.mjs"), [], "a raw child_process use outside versions.mjs");
  // the .mjs that start processes import the wrappers
  for (const f of SOURCE.filter((x) => x.endsWith(".mjs") && x !== "versions.mjs")) {
    const src = readFileSync(join(here, f), "utf8");
    if (/\b(spawnSync|execFileSync|spawn)\(/.test(src.replace(/\/\/.*$/gm, ""))) {
      assert.match(src, /import \{[^}]*\b(spawnSync|execFileSync|spawn)\b[^}]*\} from "\.\/versions\.mjs"/, `${f} spawns without the resolver`);
    }
  }
  // the entries source versions.sh and call spire_versions BEFORE any other command of their own
  for (const [entry, before] of [["land", /git fetch/], ["box", /load_config/], ["council", /exec node/], ["checks.sh", /classify\.mjs/]]) {
    const src = readFileSync(join(here, entry), "utf8").split("\n").filter((l) => !l.startsWith("#")).join("\n");
    const at = src.search(/spire_versions "/);
    assert.ok(at > 0, `${entry} never pins`);
    const first = src.search(before);
    assert.ok(at < first, `${entry} pins after ${before}`);
  }
});

const REAL = join(process.env.MISE_DATA_DIR ?? join(homedir(), ".local/share/mise"), "installs", "node");
const realNodes = existsSync(REAL)
  ? readdirSync(REAL).filter((d) => /^\d+\.\d+\.\d+$/.test(d))
  : [];
const majors = new Set(realNodes.map((v) => v.split(".")[0]));
test("with two real installed node versions: the pinned one runs a real script on a host whose default is the other", { skip: majors.size < 2 && "needs two real node majors installed under mise" }, () => {
  const [pinned, other] = [...majors];
  const pinnedV = realNodes.filter((v) => v.startsWith(`${pinned}.`)).sort((a, b) => a.localeCompare(b, undefined, { numeric: true })).at(-1);
  const otherV = realNodes.find((v) => v.startsWith(`${other}.`));
  const root = fresh("real");
  const data = join(root, "mise");
  mkdirSync(join(data, "installs", "node"), { recursive: true });
  for (const v of [pinnedV, otherV]) {
    symlinkSync(join(REAL, v), join(data, "installs", "node", v));
  }
  const env = { PATH: `${join(REAL, otherV, "bin")}:/usr/bin:/bin`, MISE_DATA_DIR: data, HOME: root };
  const dir = project({ ".node-version": `${pinned}\n` });
  const f = join(root, "v.mjs");
  writeFileSync(f, `import { applyPins } from ${JSON.stringify(PINS)};\napplyPins();\nconsole.log(process.versions.node);`);
  // run by the OTHER node (the host default): it re-runs itself under the pinned one
  const r = run(join(REAL, otherV, "bin", "node"), [f], env, { cwd: dir });
  assert.equal(r.status, 0, r.stderr);
  assert.equal(r.stdout.trim(), pinnedV);
  const child = run(process.execPath, [PINS, "exec", dir, "--", "node", "-p", "process.versions.node"], env);
  assert.equal(child.stdout.trim(), pinnedV, child.stderr);
});

test("a pin already resolved to a directory outside mise and PATH-default survives the re-resolution for another worktree (node, python)", () => {
  const h = host();
  const odd = fresh("usr-local"); // like /usr/local/bin on the engineer image: not mise, not the host default
  stubNode(join(odd, "x"), "22.99.1");
  const oddBin = join(odd, "x", "bin");
  writeFileSync(join(oddBin, "python"), "#!/bin/sh\necho Python 3.12.9\n", { mode: 0o755 });
  const dir = project({ "mise.toml": `[tools]\nnode = "22.99.1"\npython = "3.12.9"\n` });
  const env = {
    ...h.env,
    PATH: `${oddBin}:${h.env.PATH}`,
    SPIRE_PINNED_ROOT: "/elsewhere",
    SPIRE_PINNED_BIN: `${oddBin}:${oddBin}`,
    SPIRE_PINNED: "node@22.99.1 python@3.12.9",
  };
  const r = run(process.execPath, [PINS, "exec", dir, "--", "sh", "-c", "node --version; python"], env);
  assert.equal(r.status, 0, r.stderr);
  assert.equal(r.stdout.trim(), "v22.99.1\nPython 3.12.9");
  // without the earlier pin the same dir is not known: it is still refused (no guessing)
  const cold = run(process.execPath, [PINS, "exec", dir, "--", "node", "--version"], { ...h.env, PATH: h.env.PATH });
  assert.equal(cold.status, 1);
});

test("a carried pin is probed and matched: a stale version, a forged record, and bins in another order than the pins are not trusted", () => {
  const h = host();
  const odd = fresh("carried");
  const nodeBin = stubNode(join(odd, "n"), "22.99.1");
  const pyBin = join(odd, "p");
  mkdirSync(pyBin, { recursive: true });
  writeFileSync(join(pyBin, "python"), "#!/bin/sh\necho Python 3.11.0\n", { mode: 0o755 });
  const dir = project({ "mise.toml": `[tools]\nnode = "22.99.1"\npython = "3.12.9"\n` });
  const base = { ...h.env, PATH: `${nodeBin}:${pyBin}:${h.env.PATH}`, SPIRE_PINNED_ROOT: "/elsewhere" };
  // forged: the record says python 3.12.9, the binary says 3.11.0 -> refused; node (probed) is still found
  const forged = run(process.execPath, [PINS, "exec", dir, "--", "python"], {
    ...base, SPIRE_PINNED_BIN: `${nodeBin}:${pyBin}`, SPIRE_PINNED: "node@22.99.1 python@3.12.9",
  });
  assert.equal(forged.status, 1);
  assert.match(forged.stderr, /python: the project pins 3\.12\.9.*Installed: (?:(?!3\.12\.9)\d+(?:\.\d+)+ \(PATH\)(?:, )?)*\. Install it/); // probed: the forged directory is not listed under the recorded version
  // stale: another project pinned node 22.99.1; this one pins 22.99.2, which that directory is not -> refused
  const other = project({ ".node-version": "22.99.2\n" });
  const stale = run(process.execPath, [PINS, "exec", other, "--", "node", "--version"], {
    ...base, SPIRE_PINNED_BIN: nodeBin, SPIRE_PINNED: "node@22.99.1",
  });
  assert.equal(stale.status, 1);
  assert.match(stale.stderr, /node: the project pins 22\.99\.2.*Installed: .*22\.99\.1 \(PATH\)/); // the carried dir was found and probed, then refused by the pin
  // a version that only shares a prefix with the binary's own is not the binary's version: python@3.12.1 against "Python 3.12.10"
  writeFileSync(join(pyBin, "python"), "#!/bin/sh\necho Python 3.12.10\n", { mode: 0o755 });
  const prefix = project({ "mise.toml": `[tools]\npython = "3.12.1"\n` });
  const pre = run(process.execPath, [PINS, "exec", prefix, "--", "python"], {
    ...base, SPIRE_PINNED_BIN: pyBin, SPIRE_PINNED: "python@3.12.1",
  });
  assert.equal(pre.status, 1);
  // the version read is the FIRST one the binary prints: 3.12.10 is not 3.12.1 even when 3.12.1 appears later
  writeFileSync(join(pyBin, "python"), "#!/bin/sh\necho Python 3.12.10 built from 3.12.1\n", { mode: 0o755 });
  assert.equal(
    run(process.execPath, [PINS, "exec", prefix, "--", "python"], { ...base, SPIRE_PINNED_BIN: pyBin, SPIRE_PINNED: "python@3.12.1" }).status,
    1,
  );
  // order: node only is pinned, bins listed in the other order than the records are paired by index: node's record gets python's dir -> refused naming node
  const nodeOnly = project({ ".node-version": "22.99.1\n" });
  const swapped = run(process.execPath, [PINS, "exec", nodeOnly, "--", "node", "--version"], {
    ...base, PATH: h.env.PATH, SPIRE_PINNED_BIN: `${pyBin}:${nodeBin}`, SPIRE_PINNED: "node@22.99.1 python@3.12.9",
  });
  assert.equal(swapped.status, 1);
  assert.match(swapped.stderr, /node: the project pins 22\.99\.1/);
  // and the carry alone finds it: node is on no PATH, only in the record
  const carried = run(process.execPath, [PINS, "exec", nodeOnly, "--", "node", "--version"], {
    ...base, PATH: h.env.PATH, SPIRE_PINNED_BIN: `${nodeBin}:${pyBin}`, SPIRE_PINNED: "node@22.99.1 python@3.11.0",
  });
  assert.equal(carried.stdout.trim(), "v22.99.1", carried.stderr);
});
