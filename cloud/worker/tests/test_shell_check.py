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
LIST = os.path.join(WORKER, "opencode-worker.json")

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



if __name__ == "__main__":
    unittest.main()
