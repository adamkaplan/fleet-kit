"""Ask #1709, scenario S6: no fixture pushes to the default branch (a pod's git wrapper refuses that push). Offline; reads only."""
import glob, os, re, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SELF = os.path.basename(__file__)
# The pattern is assembled from fragments so this file does not hold it as one string.
_PUSH = "pu" + "sh"
_TARGETS = ["HEAD" + ":" + b for b in ("main", "master")] + ["refs/heads/" + b for b in ("main", "master")]
# The ONE named exclusion: the guard's list of refusal cases holds such lines as DATA (cases the guard must refuse), not as a push.
EXCLUDED = "cloud/worker/tests/test_guard.py"
PATTERN = re.compile(r"[\"']" + _PUSH + r"[\"'].*[\"'](" + "|".join(re.escape(t) for t in _TARGETS) + r")[\"']")


def scan(name, text):
    """Findings of one file's text, each naming the file and 'line N'."""
    return ["%s: line %d: %s" % (name, i, l.strip()) for i, l in enumerate(text.splitlines(), 1) if PATTERN.search(l)]


def files():
    out = glob.glob(os.path.join(HERE, "*.py")) + glob.glob(os.path.join(ROOT, "cloud", "worker", "tests", "*.py"))
    return sorted(f for f in out if os.path.basename(f) != SELF and os.path.relpath(f, ROOT) != EXCLUDED)


class S6(unittest.TestCase):
    def test_real_tree_has_none(self):
        fs = files()
        excl = int(os.path.isfile(os.path.join(ROOT, EXCLUDED)))
        found = []
        for f in fs:
            with open(f, encoding="utf-8") as h:
                found += scan(os.path.relpath(f, ROOT), h.read())
        print("S6: fixtures pushing to the default branch in the real tree %d (files scanned %d, exclusions naming an existing file %d)" % (len(found), len(fs), excl))
        self.assertEqual(excl, 1, "the excluded file must exist")
        self.assertGreater(len(fs), 10)
        self.assertEqual(len(found), 0, "\n".join(found))

    def _read(self, name):
        with open(os.path.join(ROOT, name), encoding="utf-8") as h:
            return h.read()

    def _mutate(self, name, new_prefix, old):
        text = self._read(name)
        new_lines = [l for l in text.splitlines() if l.lstrip().startswith(new_prefix)]
        self.assertEqual(len(new_lines), 1)
        self.assertEqual(text.count(new_lines[0]), 1)
        mutant = text.replace(new_lines[0], old)
        applied = mutant != text and mutant.count(old) == 1
        self.assertTrue(applied)
        return text, mutant, mutant.splitlines().index(old) + 1

    def test_mutants(self):
        n1 = "cloud/launcher/tests/test_cloud_worker.py"
        old1 = '    g("' + _PUSH + '", "origin", "HEAD' + ':main", cwd=work)'
        t1, m1, l1 = self._mutate(n1, 'g("fetch", work, "HEAD:refs/heads/main", cwd=origin)', old1)
        n2 = "cloud/launcher/tests/test_cloud_prestop.py"
        old2 = '        sh("git", "-C", self.work, "' + _PUSH + '", "-q", "origin", "HEAD' + ':main")'
        t2, m2, l2 = self._mutate(n2, 'sh("git", "-C", self.origin, "fetch", "-q", self.work, "HEAD:refs/heads/main")', old2)
        control = scan(n1, t1) + scan(n2, t2)
        f1, f2 = scan(n1, m1), scan(n2, m2)
        print("S6 mutants: control finds %d, mutant 1 finds %d, mutant 2 finds %d, each with the file and line named, mutations applied %s"
              % (len(control), len(f1), len(f2), True))
        self.assertEqual(len(control), 0)
        self.assertEqual(len(f1), 1)
        self.assertEqual(len(f2), 1)
        self.assertIn("test_cloud_worker.py: line %d" % l1, f1[0])
        self.assertIn("test_cloud_prestop.py: line %d" % l2, f2[0])


if __name__== "__main__":
    unittest.main()
