"""Quick-run budget (ask #1577): fake clock, three fake test files (5, 7 and 9 seconds), and two mutants."""
import importlib.machinery, importlib.util, os, re, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
PRE = os.path.join(ROOT, "bin", "cloud-pr-preflight")
SECS = {"t_a.py": 5, "t_b.py": 7, "t_c.py": 9}


def load(path):
    sys.path.insert(0, os.path.join(ROOT, "bin"))
    loader = importlib.machinery.SourceFileLoader("pre_" + str(abs(hash(path))), path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Fake:
    def __init__(self, fail_on_branch=()):
        self.now = 0.0
        self.fail = set(fail_on_branch)
        self.calls = []

    def clock(self):
        return self.now

    def run(self, root, rel, tmo, sel=None):
        self.calls.append((os.path.basename(root), rel))
        self.now += SECS[rel]
        return 1 if (rel in self.fail and os.path.basename(root) == "branch") else 0


class Budget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="budget-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for d in ("branch", "default"):
            os.mkdir(os.path.join(self.tmp, d))
            for n in SECS:
                open(os.path.join(self.tmp, d, n), "w").close()

    def go(self, total, fail=(), path=PRE):
        m = load(path)
        m.QUICK_TOTAL = total
        f = Fake(fail)
        lines = []
        names = sorted(SECS)
        res = m.run_files(names, {}, os.path.join(self.tmp, "branch"), os.path.join(self.tmp, "default"), 100, True,
                          clock=f.clock, run=f.run, out=lines.append)
        return m, f, lines, res

    def test_every_file_prints_its_seconds_and_a_fitting_budget_runs_all(self):
        m, f, lines, (verdicts, failed, not_run) = self.go(1000)
        self.assertEqual(len(verdicts), 3)
        self.assertEqual(not_run, [])
        self.assertFalse(failed)
        got = [re.findall(r"(\d+\.\d)s", ln) for ln in lines]
        self.assertEqual(got, [["5.0", "0.0"], ["7.0", "0.0"], ["9.0", "0.0"]])
        self.assertEqual(sum("NOT RUN" in ln for ln in lines), 0)

    def test_a_failing_file_prints_the_seconds_of_both_copies(self):
        m, f, lines, (verdicts, failed, not_run) = self.go(1000, fail=("t_b.py",))
        self.assertEqual(re.findall(r"(\d+\.\d)s", lines[1]), ["7.0", "7.0"])

    def check_budget(self, m, res):
        verdicts, failed, not_run = res
        self.assertEqual(not_run, ["t_c.py"])
        self.assertFalse(failed)
        self.assertNotIn(m.BUDGET_STATUS, (0, 1, 2))
        self.assertEqual(len(verdicts), 2)

    def check_failure(self, lines, res):
        verdicts, failed, not_run = res
        self.assertTrue(failed)
        self.assertEqual(not_run, [])
        self.assertEqual([v[0] for v in verdicts if v[3].startswith("FAIL")], ["t_b.py"])
        self.assertFalse([ln for ln in lines if "budget" in ln])

    def test_spent_budget_names_the_file_not_run_and_has_its_own_status(self):
        m, f, lines, res = self.go(10)
        self.check_budget(m, res)

    def test_failing_file_is_named_as_the_failure_and_not_as_the_budget(self):
        m, f, lines, res = self.go(1000, fail=("t_b.py",))
        self.check_failure(lines, res)

    def test_a_passing_file_is_not_run_on_the_clean_copy(self):
        m, f, lines, res = self.go(1000)
        self.assertEqual([c for c in f.calls if c[0] == "default"], [])

    def test_the_documentation_names_the_budget_its_setting_and_what_a_spent_budget_means(self):
        with open(os.path.join(ROOT, "cloud", "README.md")) as fh:
            text = fh.read()
        self.assertIn("budget of 240 seconds by default", text)
        self.assertIn("CLOUD_PREFLIGHT_QUICK_TOTAL", text)
        self.assertIn("A spent budget means that some selected files were not run, not that a test failed", text)

    def mutant(self, old, new):
        with open(PRE) as fh:
            src = fh.read()
        self.assertIn(old, src)  # the mutation is applied
        p = os.path.join(self.tmp, "mutant")
        with open(p, "w") as fh:
            fh.write(src.replace(old, new))
        return p

    def test_mutant_that_treats_a_spent_budget_as_a_pass_is_caught(self):
        p = self.mutant("BUDGET_STATUS = 3", "BUDGET_STATUS = 0")
        m, f, lines, res = self.go(10, path=p)
        with self.assertRaises(AssertionError):
            self.check_budget(m, res)

    def test_mutant_that_reports_a_failing_file_as_a_spent_budget_is_caught(self):
        p = self.mutant('v = "FAIL: branch exit %s, default exit %s" % (b, d); failed = True',
                        'v = "NOT RUN (budget)"; failed = False; not_run.append(rel)')
        m, f, lines, res = self.go(1000, fail=("t_b.py",), path=p)
        with self.assertRaises(AssertionError):
            self.check_failure(lines, res)


if __name__ == "__main__":
    unittest.main()
