"""The entrypoint writes the orchestrator's pod copy of its definition with question deny (fail closed); the worker's copy is unchanged."""
import os, re, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CLOUD = os.path.join(HERE, "..", "..")
ENTRYPOINT = os.path.join(CLOUD, "worker", "entrypoint.sh")
MSG = "orchestrator: the question permission line was not found exactly once; refusing to start"
ORCH = os.path.join(CLOUD, "worker", "agents", "orchestrator.md")


def rd(p):
    with open(p, "rb") as f:
        return f.read()


class Q(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        self.addCleanup(t.cleanup)
        self.d = os.path.realpath(t.name)
        self.tmpfs = os.path.join(self.d, "tmpfs"); self.share = os.path.join(self.d, "share")
        self.run_dir = os.path.join(self.d, "run"); self.home = os.path.join(self.d, "home"); self.bin = os.path.join(self.d, "bin")
        for x in (self.tmpfs, self.share, self.run_dir, self.home, self.bin):
            os.mkdir(x)
        for src, name in (("guard.sh", "guard.sh"), ("opencode-worker.json", "opencode-worker.json"), ("agents/worker.md", "worker.md"), ("agents/orchestrator.md", "orchestrator.md")):
            shutil.copy(os.path.join(CLOUD, "worker", src), os.path.join(self.share, name))
        oc = os.path.join(self.bin, "opencode")
        shutil.copy(os.path.join(HERE, "fake_opencode.sh"), oc); os.chmod(oc, 0o755)
        self.rec = os.path.join(self.d, "rec"); os.mkdir(self.rec)

    def start(self, agent, entry=ENTRYPOINT):
        env = {"PATH": self.bin + ":/usr/bin:/bin", "HOME": self.home, "WORKER_DOCKER": "0", "CW_RUN_DIR": self.run_dir,
               "CW_TMP_DIR": self.tmpfs, "CW_SHARE_DIR": self.share, "GIT_CONFIG_NOSYSTEM": "1", "FAKE_OC_REC": self.rec, "CW_AGENT": agent}
        return subprocess.run(["sh", entry, "true"], env=env, capture_output=True, text=True, timeout=60)

    def copy(self, agent):
        return os.path.join(self.tmpfs, "config", "opencode", "agents", agent + ".md")

    def set_defn(self, text):
        with open(os.path.join(self.share, "orchestrator.md"), "w") as f:
            f.write(text)

    def test_s3_orchestrator_copy_is_deny(self):
        r = self.start("orchestrator")
        self.assertEqual(r.returncode, 0, r.stderr)
        c = rd(self.copy("orchestrator")).decode()
        self.assertEqual(len(re.findall(r"^  question: deny$", c, re.M)), 1)
        self.assertNotIn("question: allow", c)
        self.assertEqual(c.replace("  question: deny", "  question: allow"), rd(ORCH).decode())

    def test_s3b_line_absent_or_twice_refuses(self):
        for name, text in (("absent", "---\npermission:\n  bash: allow\n---\nbody\n"),
                           ("twice", "---\npermission:\n  question: allow\n  question: allow\n---\nbody\n")):
            with self.subTest(name):
                shutil.rmtree(self.tmpfs); os.mkdir(self.tmpfs)
                self.set_defn(text)
                r = self.start("orchestrator")
                self.assertEqual(r.returncode, 1)
                self.assertIn(MSG, r.stderr)
                self.assertFalse(os.path.exists(self.copy("orchestrator")))
                self.assertFalse(os.path.exists(os.path.join(self.tmpfs, "config")))

    def test_s3c_worker_copy_unchanged(self):
        r = self.start("worker")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(rd(self.copy("worker")), rd(os.path.join(self.share, "worker.md")))

    def mutant(self, old, new):
        src = rd(ENTRYPOINT).decode()
        self.assertIn(old, src, "mutation not applicable")
        m = src.replace(old, new, 1)
        self.assertNotEqual(m, src, "mutation not applied")
        p = os.path.join(self.d, "entrypoint.sh")
        with open(p, "w") as f:
            f.write(m)
        return p

    def test_mutant_transform_removed(self):
        p = self.mutant("sed 's/^  question: allow$/  question: deny/' \"$defn\"", "cat \"$defn\"")
        self.assertEqual(self.start("orchestrator", p).returncode, 0)
        self.assertIn("question: allow", rd(self.copy("orchestrator")).decode())

    def test_mutant_transform_on_worker(self):
        p = self.mutant('  else\n    cat "$defn"', '  fi\n  if true; then\n    sed \'s/^/ /\' "$defn"')
        self.start("worker", p)
        self.assertNotEqual(rd(self.copy("worker")), rd(os.path.join(self.share, "worker.md")))

    def test_mutant_count_check_removed(self):
        p = self.mutant('!= 1 ]', '!= 1 ] && false')
        self.set_defn("---\npermission:\n  bash: allow\n---\n")
        r = self.start("orchestrator", p)
        self.assertNotEqual(r.stderr.strip(), MSG)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
