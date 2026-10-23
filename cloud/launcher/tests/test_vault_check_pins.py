"""Pins for three limits of the vault access check's wrapper (ask #920): a nested .git is walked, the entry cap is exact, an exit code outside the five is 'unknown-code' and stops (40). Each pin has a mutant that must fail it."""
import os, stat, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import test_cloud_worker as base  # noqa: E402
cw = base.cw
SRC = cw.__file__


def mutant(old, new):
    """The launcher module with ONE edit, loaded as a fresh module."""
    text = open(SRC).read()
    assert text.count(old) == 1, old
    mod = {"__file__": SRC, "__name__": "cw_mutant"}
    sys.modules["cw_mutant"] = type(sys)("cw_mutant")
    exec(compile(text.replace(old, new), SRC, "exec"), mod)
    class M:
        def __getattr__(self, k):
            return mod[k]

        def __setattr__(self, k, v):
            mod[k] = v
    return M()


class Pins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vcpin-")
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "checkout")
        os.makedirs(os.path.join(self.root, "scripts", "v2"), mode=0o700)
        self.put(os.path.join("scripts", "v2", "cw_vault_firewall.py"), "")
        for d in (self.tmp.name, self.root, os.path.join(self.root, "scripts"), os.path.join(self.root, "scripts", "v2")):
            os.chmod(d, 0o700)

    def put(self, rel, text="x"):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o600)

    def bad_git(self):
        os.makedirs(os.path.join(self.root, "sub", ".git"), mode=0o700)
        path = os.path.join(self.root, "sub", ".git", "evil.py")   # group-writable: must be seen
        open(path, "w").close()
        os.chmod(path, 0o666)

    def test_nested_git_is_walked_top_git_is_skipped(self):
        self.bad_git()
        self.assertIsNotNone(cw.vault_check_dir_problem(self.root))
        os.makedirs(os.path.join(self.root, ".git"), mode=0o700)
        top = os.path.join(self.root, ".git", "evil.py")
        open(top, "w").close()
        os.chmod(top, 0o666)
        os.unlink(os.path.join(self.root, "sub", ".git", "evil.py"))
        self.assertIsNone(cw.vault_check_dir_problem(self.root))   # only the top-level one is skipped

    def test_mutant_skips_nested_git(self):
        self.bad_git()
        m = mutant('if here == checkout and name in VAULT_CHECK_SKIP:', 'if name in VAULT_CHECK_SKIP:')
        self.assertIsNone(m.vault_check_dir_problem(self.root))      # the mutant lets it through ...
        self.assertIsNotNone(cw.vault_check_dir_problem(self.root))  # ... the real code refuses

    def test_entry_cap_is_exact(self):
        # the walk counts: scripts, scripts/v2, the module = 3 entries; add 2 files = 5
        self.put("a"); self.put("b")
        old = cw.VAULT_CHECK_MAX_ENTRIES
        self.addCleanup(setattr, cw, "VAULT_CHECK_MAX_ENTRIES", old)
        cw.VAULT_CHECK_MAX_ENTRIES = 5
        self.assertIsNone(cw.vault_check_dir_problem(self.root))      # at the cap: accepted
        self.put("c")
        self.assertEqual(cw.vault_check_dir_problem(self.root), "has more than 5 entries")   # one over: refused

    def test_mutant_cap_off_by_one(self):
        self.put("a"); self.put("b")
        for new in ("count >= VAULT_CHECK_MAX_ENTRIES", "count > VAULT_CHECK_MAX_ENTRIES + 1"):
            m = mutant("count > VAULT_CHECK_MAX_ENTRIES:", new + ":")
            m.VAULT_CHECK_MAX_ENTRIES = 5
            if new.startswith("count >="):
                self.assertIsNotNone(m.vault_check_dir_problem(self.root))   # refuses a checkout AT the cap
            else:
                self.put("c")
                self.assertIsNone(m.vault_check_dir_problem(self.root))      # accepts one over

    def run_with_code(self, mod, code):
        fake = os.path.join(self.tmp.name, "fakepy")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\nexit %d\n" % code)
        os.chmod(fake, 0o700)
        old = mod.VAULT_CHECK_PYTHON
        mod.VAULT_CHECK_PYTHON = fake
        try:
            return mod.run_vault_check({"CW_VAULT_CHECK_DIR": self.root, "PATH": "/usr/bin:/bin"})
        finally:
            mod.VAULT_CHECK_PYTHON = old

    def test_unknown_code_outcome_and_stop(self):
        r = self.run_with_code(cw, 7)
        self.assertEqual((r.code, r.outcome, r.effective), (7, "unknown-code", 40))
        self.assertFalse(cw.VAULT_CHECK_CODES[r.effective][1])       # treated as 40: the launch does not continue
        self.assertEqual(cw.VAULT_CHECK_CODES[r.effective][0], "failed")
        self.assertEqual(self.run_with_code(cw, 10).outcome, "added")

    def test_mutants_unknown_code(self):
        for old, new in (('else "unknown-code"', 'else "failed"'), ("VAULT_CHECK_FALLBACK = 40", "VAULT_CHECK_FALLBACK = 0")):
            m = mutant(old, new)
            r = self.run_with_code(m, 7)
            self.assertFalse((r.outcome, r.effective) == ("unknown-code", 40), old)


if __name__ == "__main__":
    unittest.main()
