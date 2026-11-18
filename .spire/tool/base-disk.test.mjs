// Admission: the LOCAL part of the base disk's agreement with its outside callers (`up --link-only` and the behind-the-lock sentence), with no Docker daemon and no network.
//   - the base build is unchanged for a project with no registry: the same tag and the same Dockerfile (so the same image) as before the change;
//   - `base up --link-only` links the image's sets, reinstalls only a set whose lock file changed, keeps that install, says the image is behind the lock, never replaces a real file;
// Oracle: the real base.mjs (a fake docker command for the build, a real shell for the install). It would fail if the build's Dockerfile or tag moved, 
// a baked set were reinstalled, a changed set were not, or the link replaced a file.
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readlinkSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import {
  behindLock,
  fingerprintOf,
  tagOf,
  warmOf,
} from "./base.mjs";

const base = join(fileURLToPath(import.meta.url), "../base.mjs");
const FROM = `x@sha256:${"0".repeat(64)}`;
const env = Object.fromEntries(
  Object.entries(process.env).filter(
    ([k]) => k !== "NODE_TEST_CONTEXT" && !k.startsWith("SPIRE_PINNED"),
  ),
);
const scratch = [];
const tmp = () => {
  const d = mkdtempSync(join(tmpdir(), "base-disk-"));
  scratch.push(d);
  return d;
};
test.after(() => {
  for (const d of scratch) {
    rmSync(d, { recursive: true, force: true });
  }
});

/** A project with one set (py) and, optionally, a second one (js); committed. */
function project({ two = false } = {}) {
  const dir = tmp();
  mkdirSync(join(dir, ".spire"));
  writeFileSync(join(dir, "requirements.txt"), "tally==1.0\n");
  writeFileSync(join(dir, "package-lock.json"), "{}\n");
  const deps = [
    {
      name: "py",
      files: ["requirements.txt"],
      run: 'mkdir -p "$WARM_DIR/lib" && cp requirements.txt "$WARM_DIR/lib/seen"',
      env: { PYTHONPATH: "$WARM_DIR/lib" },
      link: { "vendor/lib": "lib" },
    },
  ];
  if (two) {
    deps.push({
      name: "js",
      files: ["package-lock.json"],
      run: 'mkdir -p "$WARM_DIR/node_modules"',
      link: { node_modules: "node_modules" },
    });
  }
  writeFileSync(
    join(dir, ".spire/box.json"),
    JSON.stringify({
      box: 1,
      warm: { from: FROM, install: "true", deps },
    }),
  );
  const git = (...a) =>
    execFileSync(
      "git",
      ["-C", dir, "-c", "user.name=t", "-c", "user.email=t@t", ...a],
      { stdio: "pipe" },
    );
  git("init", "-q");
  git("add", "-A");
  git("commit", "-q", "-m", "x");
  return { dir, git };
}
const cli = (dir, args, extra = {}) =>
  spawnSync("node", [base, ...args], {
    cwd: dir,
    env: { ...env, ...extra },
    encoding: "utf8",
  });

// A docker that has no daemon behind it: it records the Dockerfile `build` is given, says nothing is built yet, and reports an architecture.
function fakeDocker(arch = "amd64") {
  const d = tmp();
  const bin = join(d, "docker");
  writeFileSync(
    bin,
    `#!/bin/sh\ncase "$1" in\n version) echo ${arch};;\n image) [ "$2" = ls ] && exit 0; exit 1;;\n build) cat > "${join(d, "Dockerfile")}"; echo fake;;\nesac\n`,
  );
  chmodSync(bin, 0o755);
  return { bin, dockerfile: join(d, "Dockerfile") };
}

// What the code BEFORE this change produced for `project()`: the tag and the Dockerfile the image is built from (recorded from the unchanged code at the commit before it).
const OLD_TAG = "workroom-base:";
const OLD_DOCKERFILE = `FROM ${FROM}
USER root
RUN ["sh","-c","true"]
COPY py/ /ctx/py/
RUN ["sh","-c","mkdir -p '/opt/warm/py' && mkdir -p '/opt/warm/py/.' && cp '/ctx/py/requirements.txt' '/opt/warm/py/requirements.txt' && cd '/opt/warm/py' && WARM_DIR='/opt/warm/py' && export WARM_DIR && mkdir -p \\"$WARM_DIR/lib\\" && cp requirements.txt \\"$WARM_DIR/lib/seen\\" && cat '/opt/warm/py/requirements.txt' | sha256sum | cut -d' ' -f1 >/opt/warm/py/.fingerprint && chmod -R a+rX /opt/warm/py && rm -rf /ctx"]
RUN ["sh","-c","install -d -o 65532 -g 65532 /workspace"]
LABEL io.spire.workroom.base=1
`;
// the one label the prune added: the project's own mark (its root commit), so a prune never reaches another project's images
const projectLabel = (dir) =>
  `LABEL io.spire.workroom.base.project=${execFileSync("git", ["-C", dir, "rev-list", "--max-parents=0", "HEAD"], { encoding: "utf8" }).trim().slice(0, 12)}\n`;

test("U8: with no registry the build is what it was: the same tag and Dockerfile", () => {
  const { dir } = project();
  const tags = new Set();
  {
    const f = fakeDocker();
    const r = cli(dir, ["build"], { DOCKER: f.bin });
    assert.equal(r.status, 0, r.stderr);
    tags.add(r.stdout.trim());
    assert.equal(
      readFileSync(f.dockerfile, "utf8"),
      OLD_DOCKERFILE + projectLabel(dir),
      "the Dockerfile (so the image) is the one the build made before the change, and the project's mark",
    );
  }
  assert.equal(tags.size, 1, "one tag");
  const [tag] = tags;
  assert.match(tag, new RegExp(`^${OLD_TAG}[0-9a-f]{12}$`));
  assert.equal(tag, tagOf(dir, warmOf(dir)));
  // the tag is a function of the manifest and the lock files only: the architecture and the pins are not in it
  writeFileSync(join(dir, ".node-version"), "22\n");
  assert.equal(tagOf(dir, warmOf(dir)), tag, "a pin file does not rename a built image");
});

test("link-only: a baked set is linked and not installed; a set whose lock file changed is installed alone, kept for the next call, and the image is said to be behind the lock", () => {
  const { dir } = project({ two: true });
  const baked = tmp();
  const bake = (name, d) => {
    mkdirSync(join(baked, name), { recursive: true });
    writeFileSync(join(baked, name, ".fingerprint"), `${fingerprintOf(dir, d)}\n`);
    mkdirSync(join(baked, name, d.link ? Object.values(d.link)[0] : "x"), { recursive: true });
  };
  const warm = warmOf(dir);
  bake("py", warm.deps[0]);
  bake("js", warm.deps[1]);
  const run = () => cli(dir, ["up", "--link-only"], { BASE_WARM_ROOT: baked });

  const all = run();
  assert.equal(all.status, 0, all.stderr);
  assert.match(all.stderr, /warm py/);
  assert.match(all.stderr, /warm js/);
  assert.doesNotMatch(all.stderr, /install|behind the lock/, "the image holds both: nothing is installed or said");
  assert.equal(readlinkSync(join(dir, "vendor/lib")), join(baked, "py/lib"));
  assert.equal(readlinkSync(join(dir, "node_modules")), join(baked, "js/node_modules"));
  assert.match(all.stdout, new RegExp(`export WARM_DIR='${baked}/py'\nexport PYTHONPATH="\\$WARM_DIR/lib"\n`));
  assert.equal(existsSync(join(dir, ".git/spire-land/warm")), false, "nothing was installed under the git dir");

  // the lock file of py moves on (uncommitted: the checkout is what is linked); js does not
  writeFileSync(join(dir, "requirements.txt"), "tally==2.0\n");
  const one = run();
  assert.equal(one.status, 0, one.stderr);
  assert.match(one.stderr, /install py/);
  assert.match(one.stderr, /warm js/);
  assert.doesNotMatch(one.stderr, /install js/, "only the changed set is installed");
  assert.ok(one.stderr.includes(behindLock(["py"])));
  const own = join(dir, ".git/spire-land/warm/py");
  assert.equal(readlinkSync(join(dir, "vendor/lib")), join(own, "lib"));
  assert.equal(readFileSync(join(own, "lib/seen"), "utf8"), "tally==2.0\n", "installed from the checkout's own file");
  assert.equal(readlinkSync(join(dir, "node_modules")), join(baked, "js/node_modules"));

  const again = run();
  assert.match(again.stderr, /kept py/, "the same lock file is not installed twice");
  assert.doesNotMatch(again.stderr, /install py|behind the lock/);
  assert.equal(again.stdout, one.stdout);
});

test("link-only: a failed install leaves nothing a later call would take for the set; a real file at a link is never replaced; --cold and --image are refused", () => {
  const { dir } = project();
  const baked = tmp();
  const run = (...a) => cli(dir, ["up", "--link-only", ...a], { BASE_WARM_ROOT: baked });

  const refused = run("--cold");
  assert.equal(refused.status, 2);
  assert.equal(run("--image", "t").status, 2);

  writeFileSync(join(dir, "requirements.txt"), "tally==3.0\n");
  const box = JSON.parse(readFileSync(join(dir, ".spire/box.json"), "utf8"));
  box.warm.deps[0].run = "exit 3";
  writeFileSync(join(dir, ".spire/box.json"), JSON.stringify(box));
  const bad = run();
  assert.equal(bad.status, 1);
  assert.match(bad.stderr, /installing py failed/);
  assert.equal(existsSync(join(dir, ".git/spire-land/warm/py")), false);
  assert.equal(existsSync(join(dir, "vendor/lib")), false, "no link to a set that does not exist");

  box.warm.deps[0].run = 'mkdir -p "$WARM_DIR/lib"';
  writeFileSync(join(dir, ".spire/box.json"), JSON.stringify(box));
  mkdirSync(join(dir, "vendor"));
  writeFileSync(join(dir, "vendor/lib"), "mine\n");
  const real = run();
  assert.equal(real.status, 1);
  assert.match(real.stderr, /exists and is not a link/);
  assert.equal(readFileSync(join(dir, "vendor/lib"), "utf8"), "mine\n");
  assert.equal(lstatSync(join(dir, "vendor/lib")).isSymbolicLink(), false);
});
