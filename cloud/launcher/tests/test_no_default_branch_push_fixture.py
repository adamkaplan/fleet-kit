"""Ask #1709, scenario S6: no fixture pushes to the default branch (a pod's git wrapper refuses that push). Offline; reads only."""
import glob, os, re, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SELF = os.path.basename(__file__)
# The pattern is assembled from fragments so this file does not hold it as one string.
_PUSH = "pu" + "sh"
_TARGETS = ["HEAD" + ":" + b for b in ("main", "master")] + ["refs/heads/" + b for b in ("main", "master")]
PATTERN = re.compile(r"[\"']" + _PUSH + r"[\"'].*[\"'](" + "|".join(re.escape(t) for t in _TARGETS) + r")[\"']")


def scan(name, text):
    """Findings of one file's text, each naming the file and 'line N'."""
    return ["%s: line %d: %s" % (name, i, l.strip()) for i, l in enumerate(text.splitlines(), 1) if PATTERN.search(l)]


def files():
    out = glob.glob(os.path.join(HERE, "*.py")) + glob.glob(os.path.join(ROOT, "cloud", "worker", "tests", "*.py"))
    return sorted(f for f in out if os.path.basename(f) != SELF)


class S6(unittest.TestCase):
    def test_real_tree_has_none(self):
        fs = files()
        found = []
        for f in fs:
            with open(f, encoding="utf-8") as h:
                found += scan(os.path.relpath(f, ROOT), h.read())
        print("S6: fixtures pushing to the default branch in the real tree %d (files scanned %d)" % (len(found), len(fs)))
        self.assertGreater(len(fs), 10)
        self.assertEqual(len(found), 0, "\n".join(found))

    def test_mutant_with_old_line_is_found(self):
        name = "cloud/launcher/tests/test_cloud_worker.py"
        with open(os.path.join(ROOT, name), encoding="utf-8") as h:
            text = h.read()
        old = '    g("' + _PUSH + '", "origin", "HEAD' + ':main", cwd=work)'
        new_lines = [l for l in text.splitlines() if l.startswith('    g("fetch", work, "HEAD:refs/heads/main", cwd=origin)')]
        self.assertEqual(len(new_lines), 1)
        self.assertEqual(text.count(new_lines[0]), 1)
        mutant = text.replace(new_lines[0], old)
        applied = mutant != text and mutant.count(old) == 1
        control = scan(name, text)
        found = scan(name, mutant)
        lineno = mutant.splitlines().index(old) + 1
        print("S6 mutant: control finds %d, mutant finds %d with the file and line named, mutation applied %s"
              % (len(control), len(found), applied))
        self.assertTrue(applied)
        self.assertEqual(len(control), 0)
        self.assertEqual(len(found), 1)
        self.assertIn("test_cloud_worker.py", found[0])
        self.assertIn("line %d" % lineno, found[0])


if __name__ == "__main__":
    unittest.main()
