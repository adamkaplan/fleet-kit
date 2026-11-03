"""Guard (ask #2004): a test file of this directory that opens a file and never closes it prints a ResourceWarning only when warnings are errors, so
nothing failed when the leaks were written. Each file in the table is run, one after the other, in a subprocess under `-W error::ResourceWarning`; a leaked file fails with
`a file was left open:`. S3: one case per file. S4: a scratch copy of one guarded file with one bare `open(path).read()` put back fails the guard."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
GUARDED = (
    "test_app_token.py",
    "test_chain_time.py",
    "test_cloud_end_alert.py",
    "test_cloud_pr_preflight.py",
    "test_cloud_preflight_wired.py",
    "test_cloud_quick_selector.py",
    "test_cloud_turn_end.py",
    "test_cloud_worker.py",
    "test_cluster_token_refresh.py",
    "test_cw_names.py",
    "test_driver.py",
    "test_git_token_retry.py",
    "test_issues_read.py",
    "test_m_cap.py",
    "test_mint_retry.py",
    "test_mint_roles.py",
    "test_model_credential.py",
    "test_orchestrator_agent.py",
    "test_preflight.py",
    "test_reaper_managed.py",
    "test_run_record.py",
    "test_stack_model_key.py",
    "test_stop_cause.py",
    "test_stop_no_checkout.py",
    "test_token_at_limit.py",
    "test_vault_check_pins.py",
)
LEAK = re.compile(r"^ResourceWarning: unclosed ", re.M)
LIMIT = 900


def run(path, cwd):
    """One file under the strict warning flag, run to its end: (exit status, output)."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
    p = subprocess.run([sys.executable, "-W", "error::ResourceWarning", path], cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=LIMIT)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def check(name, text):
    """Raise the anchored failure when the output of a run names a leaked file."""
    n = len(LEAK.findall(text))
    if n:
        raise AssertionError("a file was left open: %s: %d ResourceWarning lines\n%s" % (name, n, "\n".join(l for l in text.splitlines() if "ResourceWarning: unclosed " in l)))
    return n


def mutate(text, old, new):
    """`text` with the one `old` replaced by `new`; the computation of `applied` is from the two texts."""
    assert text.count(old) == 1, "%r occurs %d times" % (old, text.count(old))
    mutated = text.replace(old, new)
    applied = len(mutated) - len(text) == len(new) - len(old) and mutated != text
    assert applied, "the mutation was not applied"
    return mutated


class GuardEachFile(unittest.TestCase):
    def test_s3_each_listed_file_leaves_nothing_open(self):
        results = {n: run(os.path.join(HERE, n), ROOT) for n in GUARDED}
        for n in GUARDED:
            rc, text = results[n]
            with self.subTest(file=n):
                self.assertEqual(check(n, text), 0)
                print("S3 %s: 0 warnings" % n)
                self.assertEqual(rc, 0, "the file exited %s:\n%s" % (rc, text[-1500:]))


class MutantLeak(unittest.TestCase):
    NAME = "test_cloud_end_alert.py"
    OLD = "pathlib.Path(self.cursor).read_text()"
    NEW = "open(self.cursor).read()"

    def scratch(self, mutated):
        d = tempfile.mkdtemp(prefix="open-guard-")
        self.addCleanup(shutil.rmtree, d, True)
        tests = os.path.join(d, "cloud", "launcher", "tests")
        os.makedirs(tests)
        for link in ("bin", "docs"):
            os.symlink(os.path.join(ROOT, link), os.path.join(d, link))
        with open(os.path.join(HERE, self.NAME), encoding="utf-8") as f:
            text = f.read()
        if mutated is not None:
            text = mutate(text, *mutated)
        path = os.path.join(tests, self.NAME)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path, d

    def test_s4_control_first_passes(self):
        path, d = self.scratch(None)
        rc, text = run(path, d)
        self.assertEqual(check("control", text), 0)
        self.assertEqual(rc, 0, text[-1500:])

    def test_s4_one_bare_open_put_back_fails_with_the_anchored_text(self):
        path, d = self.scratch((self.OLD, self.NEW))
        rc, text = run(path, d)
        with self.assertRaisesRegex(AssertionError, r"\Aa file was left open: "):
            check("mutant", text)

    def test_s4_a_no_op_mutation_fails_the_applied_computation(self):
        with self.assertRaisesRegex(AssertionError, "the mutation was not applied"):
            self.scratch((self.OLD, self.OLD))

    def test_s4_the_text_occurs_once(self):
        with open(os.path.join(HERE, self.NAME), encoding="utf-8") as f:
            self.assertEqual(f.read().count(self.OLD), 1)


if __name__ == "__main__":
    unittest.main()
