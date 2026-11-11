// Admission: the base disk is built from the manifest's `warm` section, for ANY project, and a workroom starts from it without installing what the image holds.
// The project here is NOT Spire: a tiny Python service (`tally`) whose dependency is a wheel it vendors, so the build needs no network beyond the toolchain image.
// Oracle: the real base.mjs against a real Docker daemon (skipped, and the skip says why, on a host with none: CI's runners have one and run it). It would fail if the build ignored
// the manifest, the same manifest built twice, a warm start installed what the image holds, a lock file that differs from the image's were not installed, a command did not see the set's
// env, `run` left its container, or `down` removed a container the tool did not make.
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { tagOf, warmOf } from "./base.mjs";

const base = join(fileURLToPath(import.meta.url), "../base.mjs");
const TAG = process.env.BASE_TEST_FROM ?? "python:3.12-slim";
const env = Object.fromEntries(
  Object.entries(process.env).filter(([k]) => k !== "NODE_TEST_CONTEXT"),
);
const docker = (...a) => spawnSync("docker", a, { encoding: "utf8" });
const have =
  docker("info").status === 0 &&
  (docker("image", "inspect", TAG).status === 0 ||
    docker("pull", "-q", TAG).status === 0);
// the manifest pins the toolchain by digest: the one the daemon resolved the tag to
const FROM = have
  ? docker(
      "image",
      "inspect",
      "-f",
      "{{index .RepoDigests 0}}",
      TAG,
    ).stdout.trim()
  : "";
const PIN = `x@sha256:${"0".repeat(64)}`;

const WHEEL = `import zipfile, sys
with zipfile.ZipFile(sys.argv[1], "w") as z:
    z.writestr("tally_lib/__init__.py", "def tally(xs):\\n    return {x: xs.count(x) for x in sorted(set(xs))}\\n")
    z.writestr("tally_lib-1.0.dist-info/METADATA", "Metadata-Version: 2.1\\nName: tally-lib\\nVersion: 1.0\\n")
    z.writestr("tally_lib-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\\nGenerator: test\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n")
    z.writestr("tally_lib-1.0.dist-info/RECORD", "")
`;

function project() {
  const dir = mkdtempSync(join(tmpdir(), "base-"));
  mkdirSync(join(dir, ".spire"));
  mkdirSync(join(dir, "vendor"));
  writeFileSync(join(dir, "wheel.py"), WHEEL);
  execFileSync("python3", [
    join(dir, "wheel.py"),
    join(dir, "vendor/tally_lib-1.0-py3-none-any.whl"),
  ]);
  writeFileSync(join(dir, "requirements.txt"), "tally_lib==1.0\n");
  writeFileSync(
    join(dir, "test_tally.py"),
    `import unittest\nfrom tally_lib import tally\n\nclass T(unittest.TestCase):\n    def test_tally(self):\n        self.assertEqual(tally(["a", "b", "a"]), {"a": 2, "b": 1})\n`,
  );
  writeFileSync(
    join(dir, ".spire/box.json"),
    JSON.stringify({
      box: 1,
      warm: {
        from: FROM,
        install: "true",
        deps: [
          {
            name: "py",
            files: [
              "requirements.txt",
              "vendor/tally_lib-1.0-py3-none-any.whl",
            ],
            run: 'pip install -q --no-index --find-links vendor --target "$WARM_DIR/lib" -r requirements.txt',
            env: { PYTHONPATH: "$WARM_DIR/lib" },
          },
        ],
      },
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
  git("commit", "-q", "-m", "tally");
  return { dir, git };
}
const cli = (dir, ...a) =>
  spawnSync("node", [base, ...a], { cwd: dir, env, encoding: "utf8" });
const made = (r) => r.stdout.trim();

test(
  "the base disk: one build from the manifest, a warm start installs nothing, a different lock file installs its set, `down` destroys by name",
  { skip: !have && `no Docker daemon, or ${TAG} cannot be had` },
  () => {
    const { dir, git } = project();
    const tag = made(cli(dir, "build"));
    const names = [];
    try {
      assert.match(
        tag,
        /^workroom-base:[0-9a-f]{12}$/,
        "the build names the image by its content",
      );
      assert.match(
        cli(dir, "build").stderr,
        /nothing to do/,
        "the same manifest and lock files build nothing twice",
      );
      assert.notEqual(
        tagOf(dir, { ...warmOf(dir), install: "echo other" }),
        tag,
        "a changed warm section is another image",
      );

      const t = Date.now();
      const warm = cli(dir, "up");
      const wms = Date.now() - t;
      assert.equal(warm.status, 0, warm.stderr);
      names.push(made(warm));
      assert.match(warm.stderr, /warm py/);
      assert.doesNotMatch(
        warm.stderr,
        /install py/,
        "the image holds the set: a workroom installs nothing",
      );
      const seen = cli(
        dir,
        "exec",
        names[0],
        "--",
        "python",
        "-c",
        "import tally_lib; print(tally_lib.__file__)",
      );
      assert.match(
        seen.stdout,
        /^\/opt\/warm\/py\/lib\/tally_lib/,
        "the command sees the baked set through the set's env",
      );
      assert.equal(
        cli(dir, "exec", names[0], "--", "python", "-m", "unittest", "-q")
          .status,
        0,
        "the project's own check runs in the workroom",
      );

      // the tree's lock file differs from the one the image was built from: that set, and only that set, is installed now
      writeFileSync(
        join(dir, "requirements.txt"),
        "# bumped\ntally_lib==1.0\n",
      );
      git("commit", "-qam", "lock");
      const diff = cli(dir, "up", "--image", tag);
      assert.equal(diff.status, 0, diff.stderr);
      names.push(made(diff));
      assert.match(diff.stderr, /install py/);
      assert.match(
        cli(
          dir,
          "exec",
          names[1],
          "--",
          "python",
          "-c",
          "import tally_lib; print(tally_lib.__file__)",
        ).stdout,
        /^\/tmp\/warm\/py\/lib\//,
      );

      const c = Date.now();
      const cold = cli(dir, "up", "--cold");
      const cms = Date.now() - c;
      assert.equal(cold.status, 0, cold.stderr);
      names.push(made(cold));
      assert.match(
        cold.stderr,
        /install py/,
        "a cold start installs everything",
      );
      assert.equal(
        cli(dir, "exec", names[2], "--", "python", "-m", "unittest", "-q")
          .status,
        0,
      );
      console.log(`# start in ${wms} ms warm (image ready), ${cms} ms cold`);

      for (const n of names) {
        assert.equal(cli(dir, "down", n).status, 0);
        assert.notEqual(
          docker("inspect", n).status,
          0,
          "down removes the container it made",
        );
      }
      const run = cli(dir, "run", "--", "sh", "-c", "exit 7");
      assert.equal(run.status, 7, "run: the exit status is the command's");
      assert.equal(
        docker(
          "ps",
          "-aq",
          "--filter",
          "label=io.spire.workroom=1",
          "--filter",
          "ancestor=" + tag,
        ).stdout.trim(),
        "",
        "run leaves no container",
      );

      // run-hygiene: a stranger's container, labelled by this test and removed with -v below
      const stranger = docker(
        "run",
        "-d",
        "--rm",
        "--label",
        "spire.run=base-test",
        "--entrypoint",
        "sleep",
        tag,
        "60",
      ).stdout.trim();
      names.push(stranger);
      assert.equal(
        cli(dir, "down", stranger).status,
        2,
        "down refuses a container this tool did not make",
      );
      assert.equal(docker("inspect", stranger).status, 0);
    } finally {
      for (const n of names) {
        docker("rm", "-f", "-v", n);
      }
      docker("rmi", "-f", tag);
      rmSync(dir, { recursive: true, force: true });
    }
  },
);

test("a manifest whose warm section cannot make a base is refused with the field", () => {
  const dir = mkdtempSync(join(tmpdir(), "base-"));
  mkdirSync(join(dir, ".spire"));
  const refused = (warm, re) => {
    writeFileSync(
      join(dir, ".spire/box.json"),
      JSON.stringify({ box: 1, warm }),
    );
    assert.throws(() => warmOf(dir), re);
  };
  refused({ deps: [] }, /pinned by digest/);
  refused({ from: "python:3.12-slim" }, /pinned by digest/);
  refused(
    { from: PIN, deps: [{ name: "a", files: ["../x"], run: "true" }] },
    /inside the repository/,
  );
  rmSync(dir, { recursive: true, force: true });
});
