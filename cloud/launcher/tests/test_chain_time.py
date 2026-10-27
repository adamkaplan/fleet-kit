"""Ask #1390: the chain's parallel list. Every entry still runs (the parallel list is a subset of the chain), a red parallel entry fails the run,
and a parallel entry that is not in the chain is refused. Offline; scratch trees under the temp dir."""
import os, subprocess, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_chain_list as base

PARALLEL = base.SCRIPT + ".parallel"


def parallel():
    return [l for l in base.slurp(PARALLEL).split("\n") if l]


class RealTree(unittest.TestCase):
    def test_every_parallel_entry_is_in_the_chain_once(self):
        listed, par = base.entries(), parallel()
        self.assertEqual(len(par), len(set(par)))
        self.assertGreaterEqual(len(par), 3)
        self.assertEqual([e for e in par if e not in listed], [])
        self.assertNotIn(base.FIXED_LAST, par)

    def test_every_parallel_entry_uses_its_own_temporary_directory_and_no_fixed_port(self):
        for e in parallel():
            src = base.slurp(os.path.join(base.ROOT, e))
            self.assertRegex(src, r"mkdtemp|TemporaryDirectory|tempfile|stub_support|import Base|base\.Base", e)
            self.assertNotRegex(src, r"bind\(\(\"[0-9.]+\", [1-9]", e)


class Parallel(base.Scratch):
    def tree(self, names, par=(), **kw):
        d = super().tree(names, tests=kw.pop("tests", tuple(names)), **kw)
        with open(os.path.join(d, "bin", "test-cloud-launcher.parallel"), "w") as f:
            f.write("".join("cloud/launcher/tests/%s\n" % n for n in par))
        return d

    def test_parallel_entries_all_run_and_the_run_passes(self):
        names = ["test_a.py", "test_b.py", "test_c.py", "test_d.py", "test_e.py"]
        p, ran = self.run_chain(self.tree(names, par=names[1:]))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(ran, names)
        self.assertIn("ran 6 entries", p.stdout)
        for n in names[1:]:   # each parallel entry's own RUN and TIME lines come whole
            self.assertIn("RUN cloud/launcher/tests/%s\n" % n, p.stdout)

    def test_mutant_a_red_parallel_entry_fails_the_run_and_is_named(self):
        names = ["test_a.py", "test_bad.py", "test_c.py"]
        p, _ = self.run_chain(self.tree(names, par=["test_bad.py"]))
        self.assertEqual(p.returncode, 1)
        self.assertIn("FAILED: cloud/launcher/tests/test_bad.py", p.stdout)
        self.assertIn("RESULT: FAIL", p.stdout)

    def test_mutant_a_parallel_entry_missing_from_the_chain_is_refused(self):
        names = ["test_a.py", "test_b.py"]
        d = self.tree(names, par=["test_b.py", "test_gone.py"])
        p, _ = self.run_chain(d)
        self.assertEqual(p.returncode, 1)
        self.assertIn("parallel entry cloud/launcher/tests/test_gone.py is not in bin/test-cloud-launcher.chain", p.stdout)

    def test_mutant_an_entry_dropped_from_the_chain_but_kept_in_the_parallel_list_is_refused(self):
        d = self.tree(["test_a.py", "test_b.py"], par=["test_b.py"], tests=("test_a.py", "test_b.py", "test_c.py"))
        with open(os.path.join(d, "bin", "test-cloud-launcher.chain"), "w") as f:
            f.write("cloud/launcher/tests/test_a.py\n" + base.FIXED_LAST + "\n")
        p, ran = self.run_chain(d)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(ran, [])


if __name__ == "__main__":
    unittest.main()
