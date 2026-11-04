"""cw-shell-check answers equal the permission rows applied in order (last match wins). Offline."""
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.dirname(HERE)
TOOL = os.path.join(WORKER, "cw-shell-check")
REAL_LIST = os.path.join(WORKER, "opencode-worker.json")
BY_ID_ROWS = ("gh api*/issues/comments/*", "gh api*/issues/comments/* *", "gh api*/issues/comments/*?*")


def _fixture_list():
    # a copy of the repository's list without the three by-id rows, written once per test process
    with open(REAL_LIST) as fh:
        doc = json.load(fh)
    removed = 0
    for node in (doc, doc["agent"]["worker"]):
        bash = node["permission"]["bash"]
        for row in BY_ID_ROWS:
            if row in bash:
                del bash[row]
                removed += 1
    assert removed == 6, "the by-id row was not found (%d of 6 removals)" % removed
    path = os.path.join(tempfile.mkdtemp(prefix="cw-shell-check-"), "opencode-worker.json")
    with open(path, "w") as fh:
        json.dump(doc, fh, indent=2)
    return path


LIST = _fixture_list()

COMPOUND = ("cd /work && git clone https://github.com/o/r.git repo 2>&1 | tail -2; "
            "cd repo && git fetch -q && git merge origin/main 2>&1 | tail -2; wc -l docs/x.md; "
            "gh api repos/o/r/issues/comments/6610186177 --jq .body | head -5")
PARTS = ["cd /work", "git clone https://github.com/o/r.git repo 2>&1", "tail -2", "cd repo", "git fetch -q",
         "git merge origin/main 2>&1", "tail -2", "wc -l docs/x.md",
         "gh api repos/o/r/issues/comments/6610186177 --jq .body", "head -5"]
COMMANDS = [COMPOUND] + PARTS + ["gh api repos/o/r/issues/7/comments", "gh issue view 7 --comments",
            "gh api repos/o/r/issues/7/comments -X PATCH -f body=x", "git clone https://github.com/o/r.git"]


def read_rows(path):
    with open(path) as fh:
        return list(json.load(fh)["agent"]["worker"]["permission"]["bash"].items())


def order_check(rows, part):
    answer = ("allow", "(no row)")
    for pattern, action in rows:
        if fnmatch.fnmatchcase(part, pattern):
            answer = (action, pattern)
    return answer


def expected_parts(command):
    # independent split of the listed commands: they use only `;`, `&&` and `|` as separators
    return [t.strip() for t in re.split(r";|&&|\|", command) if t.strip()]


def run_tool(listfile, command):
    p = subprocess.run([sys.executable, TOOL, "--list", listfile, command], capture_output=True, text=True)
    return p.returncode, p.stdout.splitlines()


def disagreements(listfile):
    return disagreements_against(listfile, read_rows(listfile))


def disagreements_against(listfile, rows):
    bad = []
    for command in COMMANDS:
        code, lines = run_tool(listfile, command)
        want = [order_check(rows, part) for part in expected_parts(command)]
        got = []
        for line, part in zip(lines, expected_parts(command)):
            head, _, tail = line.partition(" : ")
            verdict, _, row = head.partition(" ")
            if tail != part:
                bad.append("%s: part %r printed as %r" % (command, part, tail))
            got.append((verdict, row))
        if len(lines) != len(want):
            bad.append("%s: %d parts printed, %d expected" % (command, len(lines), len(want)))
        if got != want:
            bad.append("%s: got %r want %r" % (command, got, want))
        want_code = 1 if any(w[0] == "deny" for w in want) else 0
        if code != want_code:
            bad.append("%s: exit %d want %d" % (command, code, want_code))
    return bad


DENY = '          "gh api*/issues*": "deny",\n'
ALLOW = '          "gh api*/issues/*/comments": "allow",\n'


def mutate(text):
    """Move the deny row `gh api*/issues*` to just after the allow row of the comment list route (worker block).

    Returns (mutated text, applied); applied is computed from the two texts: the deny row left its place and now
    stands after the allow row."""
    assert text.count(DENY) == 1, "the deny row must occur once"
    assert text.count(ALLOW) == 1, "the allow row must occur once"
    new = text.replace(DENY, "", 1).replace(ALLOW, ALLOW + DENY, 1)
    return new, new != text and new.index(ALLOW) < new.index(DENY) and text.index(DENY) < text.index(ALLOW)


class ShellCheck(unittest.TestCase):
    def test_answers_equal_the_order_check(self):
        bad = disagreements(LIST)
        self.assertEqual(bad, [], "cw-shell-check disagrees with the order check:\n" + "\n".join(bad))

    def test_the_by_id_read_is_denied_and_the_list_route_allowed(self):
        self.assertEqual(run_tool(LIST, PARTS[8])[0], 1)
        self.assertEqual(run_tool(LIST, "gh api repos/o/r/issues/7/comments")[0], 0)
        self.assertEqual(run_tool(LIST, COMPOUND)[0], 1)

    def test_usage_error(self):
        p = subprocess.run([sys.executable, TOOL], capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)


D = "gh api repos/o/r/issues/comments/1"
# step 1 (live run in a pod, 3 November 2026): (form, command, whole command refused by the runtime?)
LIVE = [
    ("a backticks", "echo part-one-ran; echo `%s`" % D, True),
    ("b substitution in double quotes", 'echo part-one-ran; echo "$(%s)"' % D, True),
    ("c if", "if true; then %s; fi" % D, True),
    ("d while", "while false; do %s; done" % D, True),
    ("e for", "for i in 1; do %s; done" % D, True),
    ("f braces", "{ echo part-one-ran; %s; }" % D, True),
    ("g bang", "! %s" % D, True),
    ("h time", "time %s" % D, False),
    ("i assignment", "VAR=1 %s" % D, False),
    ("j env", "env %s" % D, False),
    ("k single quotes", "echo '%s'" % D, False),
    ("l bash -c", "bash -c '%s'" % D, False),
    ("m case", "case x in x) %s;; esac" % D, True),
]


def run_plain(command):
    p = subprocess.run([sys.executable, TOOL, "--list", LIST, command], capture_output=True, text=True)
    return p.returncode, p.stdout


class LiveForms(unittest.TestCase):
    def test_every_measured_form(self):
        for form, command, refused in LIVE:
            with self.subTest(form=form):
                code, out = run_plain(command)
                self.assertEqual(code, 1 if refused else 0, "%s: %s" % (form, out))
                self.assertEqual("deny" in out.split(), refused, form)
                self.assertNotIn("unsure", out, form)

    def test_exit_statuses(self):
        self.assertEqual(run_plain("echo hi")[0], 0)
        self.assertEqual(run_plain(D)[0], 1)
        self.assertEqual(subprocess.run([sys.executable, TOOL], capture_output=True).returncode, 2)
        self.assertEqual(run_plain('echo "abc')[0], 3)

    def test_unterminated_quote_is_unsure_not_allow(self):
        code, out = run_plain('echo "abc')
        self.assertEqual(code, 3)
        self.assertIn("unsure : an unterminated quote", out)
        self.assertEqual(run_plain("echo (ls")[0], 3)
        self.assertEqual(run_plain("echo `ls")[0], 3)


# step 1, continuation (live run in a pod, 3 November 2026): (form, command, whole command refused by the runtime?)
CONT = [
    ("a after &&", "echo part-one-ran &&\\\n" + D + " --jq .body", True),
    ("b inside the word sequence", "echo part-one-ran; gh \\\napi repos/o/r/issues/comments/1", False),
    ("c inside a word", "echo part-one-ran; gh api repos/o/r/issues/comm\\\nents/1", True),
    ("d double quotes", 'echo "a\\\nb"', False),
    ("d single quotes", "echo 'a\\\nb'", False),
]


class Continuation(unittest.TestCase):
    def test_every_measured_form(self):
        for form, command, refused in CONT:
            with self.subTest(form=form):
                code, out = run_plain(command)
                self.assertEqual(code, 1 if refused else 0, "%s: %s" % (form, out))
                self.assertEqual("deny" in out.split(), refused, form)

    def test_a_shows_the_denied_part_without_the_continuation(self):
        out = run_plain(CONT[0][1])[1]
        self.assertIn("deny gh api*/issues* : " + D + " --jq .body", out.splitlines())


class ContinuationMutant(unittest.TestCase):
    LINE = '            elif c == "\\\\" and s[i + 1:i + 2] == "\\n" and not "".join(cur).strip():\n'

    def case_a(self, text):
        fd, path = tempfile.mkstemp(suffix=".py")
        self.addCleanup(os.unlink, path)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        p = subprocess.run([sys.executable, path, "--list", LIST, CONT[0][1]], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1, "continuation case a: the tool must deny: " + p.stdout)

    def mutate(self, text):
        self.assertEqual(text.count(self.LINE), 1)
        return text.replace(self.LINE, '            elif False:\n')

    def test_control_passes(self):
        with open(TOOL) as fh:
            self.case_a(fh.read())

    def test_without_the_join_case_a_fails(self):
        with open(TOOL) as fh:
            text = fh.read()
        new = self.mutate(text)
        self.assertTrue(new != text and len(text) - len(new) == len(self.LINE) - len('            elif False:\n'),
                        "the mutation was not applied")
        with self.assertRaises(AssertionError) as ctx:
            self.case_a(new)
        self.assertIn("continuation case a: the tool must deny", str(ctx.exception))

    def test_noop_mutation_fails_the_computation(self):
        with open(TOOL) as fh:
            text = fh.read()
        with self.assertRaises(AssertionError):
            self.assertTrue(text.replace(self.LINE, self.LINE) != text, "the mutation was not applied")


class NewCodeMutant(unittest.TestCase):
    BRANCH = 'elif c in "`(" or (c == "$" and s[i + 1:i + 2] == "("):'

    def scratch_tool(self, text):
        fd, path = tempfile.mkstemp(suffix=".py")
        self.addCleanup(os.unlink, path)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        return path

    def backtick_case(self, tool):
        command = LIVE[0][1]
        p = subprocess.run([sys.executable, tool, "--list", LIST, command], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1, "backtick case: the tool must deny: " + p.stdout)

    def test_control_passes(self):
        with open(TOOL) as fh:
            self.backtick_case(self.scratch_tool(fh.read()))

    def test_without_the_backtick_branch_the_case_fails(self):
        with open(TOOL) as fh:
            text = fh.read()
        self.assertEqual(text.count(self.BRANCH), 1)
        new = text.replace(self.BRANCH, 'elif c == "(" or (c == "$" and s[i + 1:i + 2] == "("):')
        self.assertTrue(new != text and new.count("`(") == text.count("`(") - 1, "the mutation was not applied")
        with self.assertRaises(AssertionError) as ctx:
            self.backtick_case(self.scratch_tool(new))
        self.assertIn("backtick case: the tool must deny", str(ctx.exception))

    def test_noop_mutation_fails_the_computation(self):
        with open(TOOL) as fh:
            text = fh.read()
        with self.assertRaises(AssertionError):
            self.assertTrue(text.replace(self.BRANCH, self.BRANCH) != text, "the mutation was not applied")


class Mutant(unittest.TestCase):
    def scratch(self, text):
        fd, path = tempfile.mkstemp(suffix=".json")
        self.addCleanup(os.unlink, path)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        return path

    def text(self):
        with open(LIST) as fh:
            return fh.read()

    def test_control_passes(self):
        self.assertEqual(disagreements(self.scratch(self.text())), [])

    def test_mutant_fails_with_the_anchored_text(self):
        original = self.text()
        mutated, applied = mutate(original)
        self.assertTrue(applied, "the mutation was not applied")
        bad = disagreements_against(self.scratch(mutated), read_rows(LIST))
        self.assertTrue(bad, "the mutant was not detected")
        with self.assertRaises(AssertionError) as ctx:
            self.assertEqual(bad, [], "cw-shell-check disagrees with the order check:\n" + "\n".join(bad))
        self.assertIn("cw-shell-check disagrees with the order check:", str(ctx.exception))

    def test_noop_mutation_fails_the_computation(self):
        original = self.text()
        with self.assertRaises(AssertionError):
            # a no-op "mutation" (the text unchanged) must not count as applied
            self.assertTrue(original != original, "the mutation was not applied")

class RealListByIdRead(unittest.TestCase):
    def test_real_list_allows_the_by_id_read_and_denies_its_write_form(self):
        read = subprocess.run([sys.executable, TOOL, "--list", REAL_LIST, "gh api repos/o/r/issues/comments/1"],
                              capture_output=True, text=True)
        self.assertEqual(read.returncode, 0, read.stdout)
        write = subprocess.run([sys.executable, TOOL, "--list", REAL_LIST,
                                "gh api repos/o/r/issues/comments/1 -X PATCH -f body=x"],
                               capture_output=True, text=True)
        self.assertEqual(write.returncode, 1, write.stdout)


if __name__ == "__main__":
    unittest.main()
