#!/usr/bin/env python3
"""Ask #2369 (S7, S8): with CW_LONG_RUN_DENY set the entrypoint adds, after every existing shell row, one deny row per pattern and then the
allow row for cw-run start; unset, the file is the template's. The order is proved with the repo's own evaluator (cw-shell-check: the LAST
matching row decides, the command split into its parts). Offline; dummy patterns."""
import json, os, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CLOUD = os.path.join(HERE, "..", "..")
ENTRYPOINT = os.path.join(CLOUD, "worker", "entrypoint.sh")
CHECK = os.path.join(CLOUD, "worker", "cw-shell-check")
PATTERNS = "*longcmd*,*other.sh*"
CONTROL = "the control must pass first"
ANCHOR = "a foreground long command was let through"


class Base(unittest.TestCase):
    entry = ENTRYPOINT

    def setUp(self):
        t = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        self.addCleanup(t.cleanup)
        self.d = os.path.realpath(t.name)
        self.tmpfs = os.path.join(self.d, "tmpfs"); self.share = os.path.join(self.d, "share")
        self.run_dir = os.path.join(self.d, "run"); self.home = os.path.join(self.d, "home"); self.bin = os.path.join(self.d, "bin"); self.rec = os.path.join(self.d, "rec")
        for x in (self.tmpfs, self.share, self.run_dir, self.home, self.bin, self.rec):
            os.mkdir(x)
        for src, name in (("guard.sh", "guard.sh"), ("opencode-worker.json", "opencode-worker.json"), ("agents/worker.md", "worker.md"), ("agents/orchestrator.md", "orchestrator.md")):
            shutil.copy(os.path.join(CLOUD, "worker", src), os.path.join(self.share, name))
        oc = os.path.join(self.bin, "opencode")
        shutil.copy(os.path.join(HERE, "fake_opencode.sh"), oc); os.chmod(oc, 0o755)

    def start(self, deny=None, entry=None):
        env = {"PATH": self.bin + ":/usr/bin:/bin", "HOME": self.home, "WORKER_DOCKER": "0", "CW_RUN_DIR": self.run_dir, "CW_TMP_DIR": self.tmpfs,
               "CW_SHARE_DIR": self.share, "GIT_CONFIG_NOSYSTEM": "1", "FAKE_OC_REC": self.rec}
        if deny is not None:
            env["CW_LONG_RUN_DENY"] = deny
        return subprocess.run(["sh", entry or self.entry, "true"], env=env, capture_output=True, text=True, timeout=60, cwd=self.d)

    def conf(self):
        return os.path.join(self.tmpfs, "config", "opencode", "opencode.json")

    def verdicts(self, command):
        r = subprocess.run([sys.executable, CHECK, "--list", self.conf(), command], capture_output=True, text=True)
        return r.returncode, r.stdout


class Rows(Base):
    def test_unset_is_the_template(self):
        r = self.start()
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.conf()) as f, open(os.path.join(self.share, "opencode-worker.json")) as t:
            got, tpl = f.read().split("\n", 1)[1], t.read().split("\n", 1)[1]
        self.assertEqual(got, tpl)

    def test_s7_rows_and_order(self):
        r = self.start(PATTERNS)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.load(open(self.conf()))
        with open(os.path.join(self.share, "opencode-worker.json")) as f:
            tpl = json.load(f)
        self.assertEqual(oct(os.stat(self.conf()).st_mode & 0o777), "0o600")
        for node, tnode in ((got["permission"], tpl["permission"]), (got["agent"]["worker"]["permission"], tpl["agent"]["worker"]["permission"])):
            rows, old = list(node["bash"].items()), list(tnode["bash"].items())
            self.assertEqual(rows[:len(old)], old, "an existing row changed")
            self.assertEqual(rows[len(old):], [("*longcmd*", "deny"), ("*other.sh*", "deny"), ("cw-run start*", "allow")])
            self.assertEqual(node["edit"], tnode["edit"])

    def test_decisions(self):
        self.assertEqual(self.start(PATTERNS).returncode, 0)
        for cmd, code in (("./run-longcmd --x", 1), ("cw-run start --name a -- ./run-longcmd", 0), ("cw-run start --name a -- ./run-longcmd; ./run-longcmd", 1),
                          ("cw-run start x; ./other.sh", 1), ("cw-run wait a 120", 0), ("ls", 0), ("echo hi && ./other.sh | tail -1", 1)):
            rc, out = self.verdicts(cmd)
            self.assertEqual(rc, code, "%s -> %s" % (cmd, out))

    def test_bad_pattern_refuses_the_start(self):
        for bad in ("a b", "a;b", "*x*,", "$(x)", "x\ny"):
            r = self.start(bad)
            self.assertEqual(r.returncode, 1, repr(bad))
            self.assertIn("CW_LONG_RUN_DENY", r.stderr)

    def test_glob_is_not_expanded_by_the_shell(self):
        open(os.path.join(self.d, "longcmd-file"), "w").close()
        self.assertEqual(self.start("*longcmd*").returncode, 0)
        self.assertIn('"*longcmd*": "deny"', open(self.conf()).read())
        self.assertNotIn("longcmd-file", open(self.conf()).read())


class Mutant(Base):
    """S8: a scratch entrypoint with the allow row put before the deny rows; the same decision check fails with the anchored text."""
    def decision_check(self):
        rc, out = self.verdicts("./run-longcmd --x")
        rc2, out2 = self.verdicts("cw-run start --name a -- ./run-longcmd")
        self.assertEqual((rc, rc2), (1, 0), ANCHOR + " (or the cw-run start form was refused): " + out + out2)

    def test_control_then_mutant(self):
        self.assertEqual(self.start(PATTERNS).returncode, 0)
        self.decision_check()   # the control: the unmutated entrypoint passes
        src = open(ENTRYPOINT).read()
        old = 'rows="$rows,\\\\n\\\\1\\"cw-run start*\\": \\"allow\\""'
        self.assertEqual(src.count(old), 1)
        new_rows = 'rows=",\\\\n\\\\1\\"cw-run start*\\": \\"allow\\"$rows"'
        mut = src.replace(old, new_rows)
        self.assertNotEqual(mut, src, "the mutation was a no-op")
        mpath = os.path.join(self.d, "entry-mutant.sh")
        with open(mpath, "w") as f:
            f.write(mut)
        shutil.rmtree(self.tmpfs); os.mkdir(self.tmpfs)
        self.assertEqual(self.start(PATTERNS, entry=mpath).returncode, 0)
        with self.assertRaises(AssertionError) as ctx:
            self.decision_check()
        self.assertIn(ANCHOR, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
