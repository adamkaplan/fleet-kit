// Admission: `.spire/tool/bin/spire` is the one entry point. help prints the one guide (the spire-land skill) word for word; an unknown subcommand is refused with the list; every subcommand
// dispatches to the right entry with its arguments and its exit status; doctor says what is wrong with a host and a project, in plain words with the fix, and passes on a good one;
// install and update put the tool and its guide into a project. It would fail if help printed anything but the guide, a subcommand reached the wrong entry or lost its status, doctor passed a project
// with a missing pin, remote, identity or file, or install ignored the guide.
// Oracle: real child processes (the real bin/spire) on a scratch project in a real git repository with a real (local) remote; the entries of the scratch copy are stubs that print what they were called with.
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { chmodSync, cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test, { after } from "node:test";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..");
const SKILL = join(repo, ".agents/skills/spire-land");
const scratch = realpathSync(mkdtempSync(join(tmpdir(), "spire-entry-test-")));
after(() => rmSync(scratch, { recursive: true, force: true }));

const NODE_DIR = dirname(process.execPath);
// a clean environment: nothing of the caller's git config, pins, boxes or lands leaks in; node stays on PATH
const baseEnv = () => {
  const env = {};
  for (const k of ["HOME", "PATH", "TMPDIR", "MISE_DATA_DIR", "XDG_DATA_HOME", "LANG"]) if (process.env[k] !== undefined) env[k] = process.env[k];
  return { ...env, PATH: `${NODE_DIR}:${env.PATH ?? "/usr/bin:/bin"}`, GIT_CONFIG_GLOBAL: "/dev/null", GIT_CONFIG_SYSTEM: "/dev/null", GIT_TERMINAL_PROMPT: "0" };
};
const git = (cwd, ...a) => execFileSync("git", a, { cwd, encoding: "utf8", env: { ...baseEnv(), GIT_AUTHOR_NAME: "t", GIT_AUTHOR_EMAIL: "t@t", GIT_COMMITTER_NAME: "t", GIT_COMMITTER_EMAIL: "t@t" }, stdio: ["ignore", "pipe", "pipe"] });
const put = (base, rel, text, mode) => {
  mkdirSync(dirname(join(base, rel)), { recursive: true });
  writeFileSync(join(base, rel), text);
  if (mode) chmodSync(join(base, rel), mode);
};
const LAND = {
  version: 1,
  paths: { tests: ["**/*.test.mjs"], prose: ["**/*.md"], notProd: ["**/*.md"] },
  sensitive: { globs: [], nameGlobs: [], always: [] },
  areas: [
    { name: "lib", paths: ["lib/**"], steps: [{ id: "t", dir: ".", cmd: ["true"] }] },
    { name: "docs", paths: ["docs/**"], steps: [{ id: "d", dir: ".", cmd: ["true"] }] },
  ],
};
let n = 0;
/** A project: a bare remote, a checkout with the real tool copied into .spire/tool (entries replaced by stubs), the guide, a land.json, one pushed commit. */
function project({ land = LAND, pin, stubs = true, identity = true, skill = true, principles = true } = {}) {
  const id = ++n;
  const remote = join(scratch, `remote-${id}.git`);
  const root = join(scratch, `project-${id}`);
  git(scratch, "init", "-q", "--bare", "-b", "main", remote);
  mkdirSync(root);
  git(root, "init", "-q", "-b", "main");
  git(root, "remote", "add", "origin", remote);
  if (identity) { git(root, "config", "user.name", "T"); git(root, "config", "user.email", "t@t"); }
  cpSync(join(repo, "tool"), join(root, ".spire/tool"), { recursive: true });
  if (skill) cpSync(SKILL, join(root, ".agents/skills/spire-land"), { recursive: true });
  if (stubs) {
    for (const f of ["land", "box", "council", "checks.sh"]) {
      put(join(root, ".spire/tool"), f, `#!/bin/sh\necho "STUB ${f} $*"\necho "ttl=$BOX_TTL_MIN bind=\${BOX_BIND_HOST:-}"\nexit \${STUB_EXIT:-0}\n`, 0o755);
    }
  }
  if (land) put(root, ".spire/land.json", JSON.stringify(land));
  if (principles) put(root, ".spire/principles.md", readFileSync(join(repo, "tool/defaults/principles.md"), "utf8")); // the project's own law: the Warden is called on its sensitive paths
  if (pin) put(root, ".node-version", `${pin}\n`);
  put(root, "lib/a.mjs", "export const a = 1;\n");
  git(root, "add", "-A");
  git(root, "commit", "-q", "-m", "init");
  git(root, "push", "-q", "origin", "main");
  git(root, "fetch", "-q", "origin");
  return { root, remote, spire: join(root, ".spire/tool/bin/spire") };
}
const spire = (p, args, { env = {}, cwd = p.root } = {}) => {
  const r = spawnSync(p.spire, args, { cwd, encoding: "utf8", env: { ...baseEnv(), ...env } });
  return { code: r.status, out: r.stdout, err: r.stderr };
};
const guide = readFileSync(join(SKILL, "SKILL.md"), "utf8");
const SUBCOMMANDS = ["help", "land", "check", "areas", "box", "council", "capture", "workroom", "principles", "doctor", "install", "update"];

test("the guide is a skill: name and description in its frontmatter, and the tool's README points at it", () => {
  assert.match(guide, /^---\nname: spire-land\ndescription: /);
  assert.match(readFileSync(join(here, "README.md"), "utf8"), /spire-land skill/);
  assert.ok(existsSync(join(here, "bin/spire")));
  assert.ok(statSync(join(here, "bin/spire")).mode & 0o100, "bin/spire is executable");
});

test("help prints the one guide word for word, with no arguments and with --help; help COMMAND prints that command's usage", () => {
  const p = project();
  for (const args of [["help"], [], ["--help"]]) {
    const r = spire(p, args);
    assert.equal(r.code, 0, r.err);
    assert.equal(r.out, guide);
  }
  const u = spire(p, ["help", "land"]);
  assert.equal(u.code, 0);
  assert.match(u.out, /^spire land /);
  const bad = spire(p, ["help", "nope"]);
  assert.equal(bad.code, 2);
  assert.match(bad.err, /no such subcommand: nope/);
  // the guide covers the commands a developer is told about
  for (const word of ["spire land", "spire doctor", "landed <sha>", "held", "--rebut"]) assert.ok(guide.includes(word), `the guide says ${word}`);
});

test("help without the guide says so and how to get it (exit 1), and still lists the subcommands", () => {
  const p = project({ skill: false });
  const r = spire(p, ["help"]);
  assert.equal(r.code, 1);
  assert.match(r.err, /guide .* is missing/);
  assert.match(r.err, /spire update --apply/);
  for (const s of SUBCOMMANDS) assert.ok(r.err.includes(`spire ${s}`), s);
});

test("an unknown subcommand is refused (exit 2) with the list of every subcommand", () => {
  const p = project();
  const r = spire(p, ["frobnicate"]);
  assert.equal(r.code, 2);
  assert.equal(r.out, "");
  assert.match(r.err, /unknown subcommand: frobnicate/);
  for (const s of SUBCOMMANDS) assert.ok(r.err.includes(`spire ${s}`), `the list names ${s}`);
});

test("every subcommand dispatches: the right entry, its arguments, and its exit status", () => {
  const p = project();
  const cases = [
    [["land", "--dry-run", "--slice", "1/2"], "STUB land --dry-run --slice 1/2"],
    [["box", "land", "--box", "b1"], "STUB box land --box b1"],
    [["council", "--roles", "warden"], "STUB council --roles warden"],
    [["capture", "--scene", "home"], "STUB box capture --scene home"],
    [["check"], "STUB checks.sh origin/main"],
    [["check", "--retest"], "STUB checks.sh --retest origin/main"],
    [["check", "--retest", "abc123"], "STUB checks.sh --retest abc123"],
    [["workroom", "down", "box-9"], "STUB box down box-9"],
  ];
  for (const [args, want] of cases) {
    const r = spire(p, args);
    assert.equal(r.code, 0, `${args.join(" ")}: ${r.err}`);
    assert.ok(r.out.split("\n").includes(want), `${args.join(" ")} printed ${JSON.stringify(r.out)}, wanted ${want}`);
  }
  // the entry's exit status is the command's
  for (const code of [13, 18, 22]) assert.equal(spire(p, ["land"], { env: { STUB_EXIT: String(code) } }).code, code);
  // bad usage of the dispatcher's own forms is refused, nothing is started
  assert.equal(spire(p, ["check", "--bogus"]).code, 2);
  assert.equal(spire(p, ["workroom"]).code, 2);
  assert.equal(spire(p, ["doctor", "x"]).code, 2);
});

test("workroom up: the instance lives a day by default, and a box that declares workroom.prepare runs it first (and stops when it fails)", () => {
  const p = project();
  const r = spire(p, ["workroom", "up"]);
  assert.equal(r.code, 0, r.err);
  assert.match(r.out, /STUB box up\nttl=1440 /);
  assert.match(spire(p, ["workroom", "up", "--ttl-min", "30"]).out, /ttl=30 /);
  assert.equal(spire(p, ["workroom", "up", "--ttl-min", "x"]).code, 2);
  put(p.root, ".spire/box.json", JSON.stringify({ box: 1, workroom: { prepare: "echo PREPARED" } }));
  const prepared = spire(p, ["workroom", "up"]);
  assert.match(prepared.out, /PREPARED\nSTUB box up/);
  put(p.root, ".spire/box.json", JSON.stringify({ box: 1, workroom: { prepare: "exit 7" } }));
  const failed = spire(p, ["workroom", "up"]);
  assert.equal(failed.code, 7);
  assert.doesNotMatch(failed.out, /STUB box up/);
});

test("areas says which areas a change touches, in words, and --json gives the raw answer", () => {
  const p = project();
  git(p.root, "checkout", "-q", "-b", "work");
  put(p.root, "docs/x.md", "hi\n");
  git(p.root, "add", "-A"); git(p.root, "commit", "-q", "-m", "docs");
  const r = spire(p, ["areas"]);
  assert.equal(r.code, 0, r.err);
  assert.match(r.out, /Areas that run: docs\. Every test of each of them runs, nothing else\./);
  assert.match(r.out, /Council members the paths call for: architect, designer, inspector, steward, warden\./); // the full council, on a docs change too
  put(p.root, "stray/y.mjs", "x\n");
  git(p.root, "add", "-A"); git(p.root, "commit", "-q", "-m", "stray");
  const full = spire(p, ["areas"]);
  assert.match(full.out, /ALL of them \(the full suite\), because these paths are in no area: stray\/y\.mjs/);
  const j = JSON.parse(spire(p, ["areas", "--json"]).out);
  assert.equal(j.areas.full, true);
  assert.deepEqual(j.areas.unmapped, ["stray/y.mjs"]);
  assert.equal(spire(p, ["areas", "nope"]).code, 2);
});

test("a held path makes areas exit 22 and name the reason", () => {
  const p = project({ land: { ...LAND, held: [{ paths: ["lib/**"], reason: "a person lands lib" }] } });
  git(p.root, "checkout", "-q", "-b", "work");
  put(p.root, "lib/b.mjs", "x\n");
  git(p.root, "add", "-A"); git(p.root, "commit", "-q", "-m", "lib");
  const r = spire(p, ["areas"]);
  assert.equal(r.code, 22);
  assert.match(r.out, /HELD for a person: lib\/b\.mjs: a person lands lib/);
});

// --- doctor ---

test("doctor passes a good project without pins (and says the host's tools are used), with the guide, the remote and push access checked", () => {
  const p = project();
  const r = spire(p, ["doctor"]);
  assert.equal(r.code, 0, r.out + r.err);
  assert.match(r.out, /ok\s+\.spire\/land\.json is valid: 2 areas \(lib, docs\)/);
  assert.match(r.out, /note\s+the project pins no tool versions/);
  assert.match(r.out, /ok\s+origin \(.*remote-\d+\.git\) answers; main is [0-9a-f]{10}/);
  assert.match(r.out, /ok\s+you commit as T <t@t>/);
  assert.match(r.out, /ok\s+you may push to origin/);
  assert.match(r.out, /ready: nothing is wrong/);
  assert.ok(!/PROBLEM/.test(r.out));
  // the dry-run probe pushed nothing
  assert.equal(git(p.root, "ls-remote", "origin").includes("spire-doctor-probe"), false);
});

test("doctor says what the project's box declares (nothing: a checkout), asks nothing of any bundle, and refuses a box.json that is not JSON", () => {
  const p = project();
  assert.match(spire(p, ["doctor"]).out, /note\s+the project has no \.spire\/box\.json: `box up` is a checkout and nothing else/);
  put(p.root, ".spire/box.json", JSON.stringify({ box: 1, wrap: { run: "true" }, services: [{ name: "a", start: "true" }] }));
  const wrapped = spire(p, ["doctor"]);
  assert.match(wrapped.out, /ok\s+\.spire\/box\.json is valid: 1 service\(s\), wrapped/);
  assert.doesNotMatch(wrapped.out, /bundle/i, "the tool knows no bundle to ask about");
  put(p.root, ".spire/box.json", "{ not json");
  const old = spire(p, ["doctor"]);
  assert.equal(old.code, 1);
  assert.match(old.out, /manifest: cannot read .*box\.json/);
});

test("doctor with a pin the host has: names the version it will use; with a pin it lacks: a PROBLEM with the install command, exit 1, no fallback", () => {
  const have = project({ pin: process.versions.node.split(".")[0] });
  const ok = spire(have, ["doctor"]);
  assert.equal(ok.code, 0, ok.out + ok.err);
  assert.match(ok.out, new RegExp(`ok\\s+pinned and installed here: node ${process.versions.node.split(".")[0]}\\.\\d+\\.\\d+`));

  const lack = project({ pin: "99.1.1" });
  const bad = spire(lack, ["doctor"]);
  assert.equal(bad.code, 1);
  assert.match(bad.out, /PROBLEM\s+node: the project pins 99\.1\.1 \(\.node-version\) and this host has no such version\./);
  assert.match(bad.out, /Install it: mise install node@99\.1\.1/);
  assert.match(bad.out, /never falls back to the host default/);
  assert.match(bad.out, /1 problem: fix it/);
});

test("doctor names each problem and its fix: no land.json, a remote that does not answer, no identity, a host that only edits, a tool edited by hand", () => {
  const none = project({ land: null });
  const r1 = spire(none, ["doctor"]);
  assert.equal(r1.code, 1);
  assert.match(r1.out, /PROBLEM\s+the project has no .*land\.json/);
  assert.match(r1.out, /fix: write it/);

  const remote = project();
  git(remote.root, "remote", "set-url", "origin", join(scratch, "does-not-exist.git"));
  const r2 = spire(remote, ["doctor"]);
  assert.equal(r2.code, 1);
  assert.match(r2.out, /PROBLEM\s+origin \(.*does-not-exist\.git\) does not answer/);
  assert.match(r2.out, /fix: check the URL and your credential: gh auth login/);

  const anon = project({ identity: false });
  const r3 = spire(anon, ["doctor"]);
  assert.equal(r3.code, 1);
  assert.match(r3.out, /PROBLEM\s+git does not know who you are \(user\.name missing, user\.email missing\)/);
  assert.match(r3.out, /fix: git config user\.name/);

  const bad = project();
  put(bad.root, ".spire/land.json", "{ not json");
  const r5 = spire(bad, ["doctor"]);
  assert.equal(r5.code, 1);
  assert.match(r5.out, /PROBLEM\s+the project's files are refused: manifest: cannot read .*land\.json/);
  put(bad.root, ".spire/land.json", JSON.stringify({ ...LAND, sensitve: { globs: ["lib/**"] } })); // a typo in a gate key: the Warden would be silently unseated
  const r5b = spire(bad, ["doctor"]);
  assert.equal(r5b.code, 1);
  assert.match(r5b.out, /PROBLEM\s+the project's files are refused: manifest: .*land\.json has a top-level key no reader of the tool uses: sensitve/);

  const council = project({ principles: false });
  assert.match(spire(council, ["doctor"]).out, /PROBLEM\s+the council cannot sit: the project has no \.spire\/principles\.md/);

  const outside = project();
  const away = join(scratch, "not-a-checkout");
  mkdirSync(away);
  const r6 = spire(outside, ["doctor"], { cwd: away });
  assert.equal(r6.code, 1);
  assert.match(r6.out, /PROBLEM\s+.* is not inside a git checkout/);
});

// --- the law ---

// Admission: `spire principles` PRINTS both halves of the law a change is built and judged by: the project's own principles (in full, labelled), then the reviewers' viewpoints (the project's over the tool's
// defaults) with the principle numbers they enforce and the paths that call them. It would fail if it printed anything of a file that is neither (a third file in .spire/review/, AGENTS.md), left a half out,
// let a project's viewpoint lose to the default, or went on without the project's file. Oracle: the real bin/spire on a scratch project with its own principles and one overriding viewpoint.
test("spire principles prints the project's principles and the reviewers' viewpoints with their principle numbers, labelled; a file that is neither is not printed", () => {
  const p = project();
  put(p.root, ".spire/principles.md", "# Principles (this project)\n\n1. **Kit one.** PROJECT-RULE-ONE\n2. **Kit two.** b\n");
  put(p.root, ".spire/review/inspector.md", '---\nrole: inspector\ncalls: ["src/**"]\nenforces: [1, 2]\n---\n**Does it do what the kit says?** PROJECT-VIEWPOINT-BODY\n');
  put(p.root, ".spire/review/extra.md", "THIRD-FILE-NOT-LAW\n");
  put(p.root, "AGENTS.md", "AGENTS-NOT-LAW\n");
  const r = spire(p, ["principles"]);
  assert.equal(r.code, 0, r.err);
  assert.match(r.out, /^== the project's principles \(\.spire\/principles\.md: /m);
  assert.ok(r.out.includes("1. **Kit one.** PROJECT-RULE-ONE"), "the project's principles, in full");
  assert.match(r.out, /^== the reviewers' viewpoints /m);
  assert.match(r.out, /^inspector\s+project\s+calls: src\/\*\*\s+enforces: P1 P2\n\s+Does it do what the kit says\?/m);
  assert.match(r.out, /^warden\s+default\s+calls: @all\s+enforces: P10 P11 P12$/m); // the tool's defaults stay
  assert.ok(r.out.indexOf("== the project's principles") < r.out.indexOf("== the reviewers' viewpoints"), "the law, then the viewpoints");
  for (const t of ["THIRD-FILE-NOT-LAW", "AGENTS-NOT-LAW", "PROJECT-VIEWPOINT-BODY"]) assert.ok(!r.out.includes(t), `${t} is not in the summary`);
  // one viewpoint whole: the project's, then a default
  const one = spire(p, ["principles", "inspector"]);
  assert.match(one.out, /== viewpoint: inspector \(the project's own, \.spire\/review\/inspector\.md\) ==/);
  assert.ok(one.out.includes("PROJECT-VIEWPOINT-BODY"));
  const def = spire(p, ["principles", "warden"]);
  assert.match(def.out, /== viewpoint: warden \(the tool's default\) ==/);
  assert.match(def.out, /Can this change be abused/);
  assert.equal(spire(p, ["principles", "nobody"]).code, 2);
  assert.equal(spire(p, ["principles", "a", "b"]).code, 2);
});

test("spire principles without the project's file says how to start one (exit 1); doctor calls it a problem when a reviewer would be called", () => {
  const p = project({ principles: false });
  const r = spire(p, ["principles"]);
  assert.equal(r.code, 1);
  assert.match(r.err, /no \.spire\/principles\.md: its law is a project-owned file/);
  assert.match(r.err, /cp .*defaults\/principles\.md \.spire\/principles\.md/);
  assert.match(spire(p, ["doctor"]).out, /PROBLEM\s+the council cannot sit: the project has no \.spire\/principles\.md/);
  put(p.root, ".spire/principles.md", "1. **x.** y\n");
  assert.match(spire(p, ["doctor"]).out, /ok\s+the council's law is there: \.spire\/principles\.md, and viewpoints for /);
  // a project that removed every member asks for no review: no law needed
  const none = project({ principles: false });
  for (const role of ["inspector", "steward", "architect", "designer", "warden"]) put(none.root, `.spire/review/${role}.md`, `---\nrole: ${role}\ncalls: []\n---\nNothing calls it.\n`);
  assert.match(spire(none, ["doctor"]).out, /note\s+no reviewer is called for any path/);
});

// --- install / update ---

/** A scratch spire-land source (a git repository): the real installer and pin.mjs, a tiny tool/ with the real bin/spire and spire.mjs, and a guide. */
function source() {
  const src = join(scratch, `source-${++n}`);
  mkdirSync(src);
  git(src, "init", "-q", "-b", "main");
  mkdirSync(join(src, "bin"));
  for (const f of ["spire-land", "spire-land.mjs"]) cpSync(join(repo, "bin", f), join(src, "bin", f));
  chmodSync(join(src, "bin/spire-land"), 0o755);
  cpSync(join(repo, "tool"), join(src, "tool"), { recursive: true, filter: (f) => !/\.test\.mjs$|fixtures/.test(f) });
  cpSync(SKILL, join(src, ".agents/skills/spire-land"), { recursive: true });
  git(src, "add", "-A");
  git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "source");
  return src;
}

test("install and update: plan first, --apply writes the tool and the guide, the installed copy's own bin/spire works, doctor passes it, and update finds the source by SPIRE_LAND_SOURCE", () => {
  const src = source();
  const target = join(scratch, `fresh-${++n}`);
  mkdirSync(target);
  git(target, "init", "-q", "-b", "main");
  const run = (args, extra = {}) => {
    const r = spawnSync(join(src, "tool/bin/spire"), args, { cwd: target, encoding: "utf8", env: { ...baseEnv(), ...extra } });
    return { code: r.status, out: r.stdout, err: r.stderr };
  };
  const plan = run(["install", "--source", src]);
  assert.equal(plan.code, 0, plan.err);
  assert.match(plan.out, /added\s+\.agents\/skills\/spire-land\/SKILL\.md/);
  assert.match(plan.out, /plan only: nothing was written/);
  assert.equal(existsSync(join(target, ".spire")), false);
  const done = run(["install", "--source", src, "--apply"]);
  assert.equal(done.code, 0, done.err);
  assert.equal(readFileSync(join(target, ".agents/skills/spire-land/SKILL.md"), "utf8"), guide);
  const installed = join(target, ".spire/tool/bin/spire");
  const help = spawnSync(installed, ["help"], { cwd: target, encoding: "utf8", env: baseEnv() });
  assert.equal(help.stdout, guide, "the installed copy prints the installed guide");
  const doc = spawnSync(installed, ["doctor"], { cwd: target, encoding: "utf8", env: baseEnv() });
  assert.match(doc.stdout, /ok\s+the tool .* matches its PIN, and so does the guide/);
  // the installed copy updates itself from a source it is told about, and refuses plainly when it is told none
  const noSource = spawnSync(installed, ["update"], { cwd: target, encoding: "utf8", env: baseEnv() });
  assert.equal(noSource.status, 2);
  assert.match(noSource.stderr, /does not say where the tool comes from \(a PIN names no repository\)/);
  const up = spawnSync(installed, ["update"], { cwd: target, encoding: "utf8", env: { ...baseEnv(), SPIRE_LAND_SOURCE: src } });
  assert.equal(up.status, 0, up.stderr);
  assert.match(up.stdout, /0 added, 0 changed, 0 removed, \d+ unchanged/);
  // a hand-edited guide is caught
  writeFileSync(join(target, ".agents/skills/spire-land/SKILL.md"), "edited\n");
  const drift = spawnSync(installed, ["doctor"], { cwd: target, encoding: "utf8", env: baseEnv() });
  assert.equal(drift.status, 1);
  assert.match(drift.stdout, /PROBLEM\s+the tool or its guide was edited by hand/);
  assert.match(drift.stdout, /fix: spire update --apply --discard-edits/);
  assert.equal(spawnSync(installed, ["update", "--source", "/nonexistent"], { cwd: target, encoding: "utf8", env: baseEnv() }).status, 2);
  assert.equal(spawnSync(installed, ["install", "--discard-edits"], { cwd: target, encoding: "utf8", env: baseEnv() }).status, 2);
});

test("a pin the host lacks refuses every command that runs project code, in plain words and exit 1 (no stack, nothing started); help, doctor and the refusals still answer", () => {
  const p = project({ pin: "99.1.1" });
  for (const args of [["land"], ["check"], ["box", "up"], ["council"], ["areas"], ["workroom", "up"]]) {
    const r = spire(p, args);
    assert.equal(r.code, 1, args.join(" "));
    assert.match(r.err, /^spire-land: node: the project pins 99\.1\.1 \(\.node-version\)/, args.join(" "));
    assert.match(r.err, /mise install node@99\.1\.1/);
    assert.ok(!/STUB/.test(r.out), "the entry was not started");
    assert.ok(!/\n\s+at /.test(r.err), "no stack trace");
  }
  assert.equal(spire(p, ["help"]).out, guide);
  assert.equal(spire(p, ["nope"]).code, 2);
});
