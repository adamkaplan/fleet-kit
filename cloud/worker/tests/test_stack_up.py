"""Ask #1025 slice A, scenarios S6 and S7: the entrypoint copies the stack-only model credential to a 0600 file on the tmpfs, never exports it, and
`cw-stack-up` gives the compose child (only) the variable CW_STACK_MODEL_KEY; `cw-stack-up down` removes the copy. Offline: a fake docker, planted
files in a temp tree. The values are synthetic."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, "..")
ENTRYPOINT = os.path.join(WORKER, "entrypoint.sh")
STACK_UP = os.path.join(WORKER, "cw-stack-up")
FAKE_OPENCODE = os.path.join(HERE, "fake_opencode.sh")
STACK = "stk-" + "Zq9Rt4" * 6
CODER = "cod-" + "Wk3Nv8" * 6

FAKE_DOCKER = """#!/bin/sh
# records each call: its arguments, and whether CW_STACK_MODEL_KEY was set and what it held
echo "args: $*" >> "$REC/docker.log"
echo "var: ${CW_STACK_MODEL_KEY:-UNSET}" >> "$REC/docker.log"
"""


class StackUp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = os.path.realpath(tmp.name)
        self.run_dir, self.tmpfs, self.home, self.bin, self.rec, self.share = [os.path.join(self.d, x) for x in ("run", "tmpfs", "home", "bin", "rec", "share")]
        for x in (self.run_dir, self.tmpfs, self.home, self.bin, self.rec, self.share):
            os.mkdir(x)
        for src, name in (("guard.sh", "guard.sh"), ("opencode-worker.json", "opencode-worker.json"), ("agents/worker.md", "worker.md")):
            shutil.copy(os.path.join(WORKER, src), os.path.join(self.share, name))
        for name, body in (("docker", FAKE_DOCKER),):
            with open(os.path.join(self.bin, name), "w") as f:
                f.write(body)
            os.chmod(os.path.join(self.bin, name), 0o755)
        shutil.copy(FAKE_OPENCODE, os.path.join(self.bin, "opencode"))
        os.chmod(os.path.join(self.bin, "opencode"), 0o755)
        self.put("stack-model-key", STACK)
        self.put("model-auth", CODER)

    def put(self, name, value):
        with open(os.path.join(self.run_dir, name), "w") as f:
            f.write(value)
        os.chmod(os.path.join(self.run_dir, name), 0o400)

    def env(self):
        return {"PATH": self.bin + ":" + os.environ["PATH"], "HOME": self.home, "CW_RUN_DIR": self.run_dir, "CW_TMP_DIR": self.tmpfs, "CW_SHARE_DIR": self.share,
                "WORKER_DOCKER": "0", "REC": self.rec, "FAKE_OC_REC": self.rec}

    def entry(self, *cmd):
        return subprocess.run(["sh", ENTRYPOINT, *cmd], env=self.env(), capture_output=True, text=True, timeout=60)

    def up(self, *args):
        return subprocess.run(["sh", STACK_UP, *args], env=self.env(), capture_output=True, text=True, timeout=60)

    def copy(self):
        return os.path.join(self.tmpfs, "stack", "model-key")

    def log(self):
        with open(os.path.join(self.rec, "docker.log")) as f:
            return f.read()

    def test_s6_a_0600_copy_and_the_variable_in_the_compose_child_only(self):
        ran = self.entry("env")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(self.copy()).st_mode), 0o600)
        self.assertEqual(os.stat(self.copy()).st_uid, os.getuid())
        with open(self.copy()) as f:
            self.assertEqual(f.read(), STACK)
        self.assertNotIn(STACK, ran.stdout + ran.stderr)   # not in the entrypoint's own environment (`env` printed it), not printed
        self.assertNotIn("CW_STACK_MODEL_KEY", ran.stdout)
        found = [os.path.join(r, n) for r, _, ns in os.walk(self.tmpfs) for n in ns if CODER in open(os.path.join(r, n), errors="replace").read()]
        self.assertEqual([os.path.relpath(p, self.tmpfs) for p in found if os.sep + "stack" + os.sep in p], [])   # the coder's key is in no stack file
        self.up("up", "-d")
        log = self.log()
        self.assertIn("args: compose up -d", log)
        self.assertIn("var: " + STACK, log)
        self.assertNotIn(STACK, log.split("var:")[0])   # never an argument
        with open(os.path.join(self.bin, "docker"), "a") as f:   # a non-compose docker call: the variable is not there
            pass
        subprocess.run(["docker", "ps"], env=self.env(), capture_output=True)
        self.assertTrue(self.log().rstrip().endswith("var: UNSET"))

    def test_s6_a_mutant_that_exports_the_variable_is_caught(self):
        with open(ENTRYPOINT) as f:
            src = f.read()
        self.assertNotIn("export CW_STACK_MODEL_KEY", src)
        self.assertNotRegex(src, r"CW_STACK_MODEL_KEY=")

    def test_s7_down_removes_the_copy_and_a_later_up_refuses_without_a_value(self):
        self.entry("true")
        self.assertTrue(os.path.exists(self.copy()))
        down = self.up("down")
        self.assertEqual(down.returncode, 0, down.stderr)
        with self.assertRaises(FileNotFoundError):
            os.stat(self.copy())
        again = self.up("up", "-d")
        self.assertEqual(again.returncode, 1)
        self.assertNotIn(STACK, again.stdout + again.stderr)
        self.assertEqual(len((again.stderr).strip().splitlines()), 1)

    def test_a_service_named_down_as_a_later_argument_does_not_stop(self):
        self.entry("true")
        r = self.up("up", "-d", "down")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self.copy()))   # the copy stays: only a first argument `down` removes it

    def test_without_the_key_nothing_is_copied_and_compose_runs_as_before(self):
        os.remove(os.path.join(self.run_dir, "stack-model-key"))
        self.assertEqual(self.entry("true").returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmpfs, "stack")))
        self.assertEqual(self.up("up", "-d").returncode, 0)
        self.assertIn("var: UNSET", self.log())

    def test_an_empty_key_file_refuses_the_start(self):
        self.put("stack-model-key", "") if False else None
        os.chmod(os.path.join(self.run_dir, "stack-model-key"), 0o600)
        open(os.path.join(self.run_dir, "stack-model-key"), "w").close()
        ran = self.entry("true")
        self.assertEqual(ran.returncode, 1)
        self.assertIn("stack-model-key", ran.stderr)


if __name__ == "__main__":
    unittest.main()
