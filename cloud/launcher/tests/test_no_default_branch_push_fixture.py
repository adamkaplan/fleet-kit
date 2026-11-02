"""Ask #1709, scenario S6: no fixture pushes to the default branch (a pod's git wrapper refuses that push). Offline; reads only."""
import ast, glob, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SELF = os.path.basename(__file__)
# A push is found by PARSING the call (ast), not by a list of spellings: the destination of its refspec decides.
_PUSH = "push"
# The ONE named exclusion: the guard's list of refusal cases holds such lines as DATA (cases the guard must refuse), not as a push.
EXCLUDED = "cloud/worker/tests/test_guard.py"


def _strings(node):
    """The string constants of one positional argument (inside a list or tuple literal too); other arguments give none."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [v for e in node.elts for v in _strings(e)]
    return []


def _destination(refspec):
    d = refspec.split(":", 1)[1] if ":" in refspec else refspec
    d = d[1:] if d.startswith("+") else d
    if ":" not in refspec and refspec.startswith("+"):
        d = refspec[1:]
    return d[len("refs/heads/"):] if d.startswith("refs/heads/") else d


def scan(name, text):
    """Findings of one file's text, each 'file: line N: <line>'. A text that does not parse is a finding of its own kind."""
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return ["%s: line %s: does not parse" % (name, e.lineno)]
    lines, out = text.splitlines(), []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        strs = [v for a in node.args for v in _strings(a)]
        if _PUSH not in strs:
            continue
        rest = [v for v in strs[strs.index(_PUSH) + 1:] if not v.startswith("-")]
        refspecs = rest[1:]   # the first is the remote
        hit = any(_destination(r) in ("main", "master") for r in refspecs)
        if hit:
            out.append("%s: line %d: %s" % (name, node.lineno, lines[node.lineno - 1].strip()))
    return out


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
        return text, mutant, mutant.splitlines().index(old) + 1, applied

    def test_mutants(self):
        n1 = "cloud/launcher/tests/test_cloud_worker.py"
        old1 = '    g("' + _PUSH + '", "origin", "HEAD' + ':main", cwd=work)'
        t1, m1, l1, a1 = self._mutate(n1, 'g("fetch", work, "HEAD:refs/heads/main", cwd=origin)', old1)
        n2 = "cloud/launcher/tests/test_cloud_prestop.py"
        old2 = '        sh("git", "-C", self.work, "' + _PUSH + '", "-q", "origin", "HEAD' + ':main")'
        t2, m2, l2, a2 = self._mutate(n2, 'sh("git", "-C", self.origin, "fetch", "-q", self.work, "HEAD:refs/heads/main")', old2)
        control = scan(n1, t1) + scan(n2, t2)
        f1, f2 = scan(n1, m1), scan(n2, m2)
        print("S6 mutants: control finds %d, mutant 1 finds %d, mutant 2 finds %d, each with the file and line named, mutations applied %s"
              % (len(control), len(f1), len(f2), all([a1, a2])))
        self.assertEqual(len(control), 0)
        self.assertEqual(len(f1), 1)
        self.assertEqual(len(f2), 1)
        self.assertIn("test_cloud_worker.py: line %d" % l1, f1[0])
        self.assertIn("test_cloud_prestop.py: line %d" % l2, f2[0])

    def test_spellings_and_other_branches(self):
        self.assertEqual(self.spellings(), (4, 0))

    def spellings(self):
        found = [bool(scan("x.py", "f(%s)" % c)) for c in (
            '"push", "origin", "main"', '"push", "origin", "HEAD:refs/heads/master"',
            '"push", "-q", "origin", "+HEAD:main"', '"push", "origin", "refs/heads/main"')]
        other = [bool(scan("x.py", "f(%s)" % c)) for c in (
            '"push", "origin", "feature"', '"push", "origin", "HEAD:refs/heads/feature"', '"push", "-u", "origin", "HEAD:feature"')]
        self.assertEqual((sum(found), sum(other)), (4, 0))
        self.assertEqual(scan("x.py", "def ("), ["x.py: line 1: does not parse"])
        self.assertEqual(scan("x.py", 'f("push", "origin")'), [])
        self.assertFalse(scan("x.py", 'f("push", "origin", name)'))
        self.assertTrue(scan("x.py", 'f(["push", "origin", "main"])'))
        return sum(found), sum(other)

    def test_guard_reading_one_spelling_fails_the_plain_check(self):
        counts = self.spellings()
        def plain(fn):
            if not fn("x.py", 'f("push", "origin", "main")'):
                raise AssertionError("a push to main spelled plainly was not found")
        with open(os.path.abspath(__file__) if False else os.path.join(HERE, SELF), encoding="utf-8") as h:
            src = h.read()
        rule = '        hit = any(_destination(r) in ("main", "master") for r in refspecs)'
        self.assertEqual(src.count("\n" + rule + "\n"), 1)
        mutated = src.replace(rule, '        hit = any(r == "HEAD:main" for r in refspecs)')
        applied = mutated != src
        def load(text):
            ns = {"__file__": os.path.join(HERE, SELF), "__name__": "scratch_guard"}
            exec(compile(text, "scratch_guard", "exec"), ns)
            return ns["scan"]
        plain(load(src))   # the control, first
        control = 1
        msg = ""
        try:
            plain(load(mutated))
        except AssertionError as e:
            msg = str(e)
        self.assertEqual(msg, "a push to main spelled plainly was not found")
        print("S6: spellings found %d of 4, other branches found %d of 3; control passes %d of 1, mutant fails 1 of 1 with the text, mutation applied %s"
              % (counts[0], counts[1], control, applied))


if __name__== "__main__":
    unittest.main()
