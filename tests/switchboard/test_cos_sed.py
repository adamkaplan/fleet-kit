"""#1012: the Chief of Staff may read with `sed` in three narrow forms and can never write, run or edit in place with it.
The matcher reads the rule list as the permission engine does (whole-value glob, the last match wins, a trailing ` *` also matches the bare command)."""
import json
import re
import unittest
from pathlib import Path

from support.paths import REPO

PATH = REPO / "agents" / "opencode" / "chief-of-staff.md"
BEFORE = REPO / "tests" / "switchboard" / "fixtures" / "chief-of-staff.before-sed.md"


def rules_of(text):
    lines = text.split("\n---\n", 1)[0].splitlines()
    out = []
    for line in lines[lines.index("permissions:") + 1:]:
        if line.lstrip().startswith("#"):
            continue
        m = re.fullmatch(r"  - \{action: (.+?), resource: (\".*\"), effect: (allow|deny)\}", line)
        assert m, line
        out.append((m.group(1), json.loads(m.group(2)), m.group(3)))
    return out


def verdict(rules, command, action="shell"):
    effect = "deny"
    for a, pattern, e in rules:
        rx = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
        if pattern.endswith(" *"):
            rx = rx[:-len(re.escape(" ") + ".*")] + "(?: .*)?"
        if a in ("*", action) and re.fullmatch(rx, command, re.S):
            effect = e
    return effect


ALLOWED = (
    "sed -n '10,20p' /var/log/app.log", "sed -n '1,5p' notes.md", "sed -n '/ERROR/p' app.log", "sed -n '/timeout/p' /var/log/app.log",
    "sed -n 's/foo/bar/p' app.log", "sed -n '100,200p' src/main.py",
    "sed -n '3,9p'", "sed -n '/ERROR/p'", "sed -n 's/a/b/p'",   # standard input, in a pipe
)
DENIED = (
    "sed -i 's/a/b/' f", "sed -i.bak 's/a/b/' f", "sed --in-place 's/a/b/' f", "sed --in-place=.bak 's/a/b/' f", "sed -ni '1,5p' f", "sed -n -i '1,5p' f",
    "sed -n '1,5p' -i f", "sed -n '1,5p' f -i", "sed -n '1,5p' --in-place=.bak f", "sed -n '1,5p' -s f", "sed -n '1,5p' --separate f", "sed -s -n '1,5p' f",
    "sed -n '1,5p' -f script.sed f", "sed -n -f script.sed f", "sed -n --file=script.sed f", "sed -n -e '1p' -e '2p' f", "sed -n '1p' -e 'w out' f",
    "sed -n '1,5w out.txt' f", "sed -n '1,5W out.txt' f", "sed -n '1,5wout p' f", "sed -n '/a/w out.txt' f", "sed -n '/a/W out.txt' f", "sed -n '/a/ w out.txt' f",
    "sed -n '/a/!w out.txt' f", "sed -n '1~2w a,p' f",
    "sed -n 's/a/b/w out.txt' f", "sed -n 's/a/b/e' f", "sed -n 's/a/b/pe' f", "sed -n '/a/e id' f", "sed -n '1,5e id' f", "sed -n '1,5p;e id' f",
    "sed -n '/a/r /etc/passwd' f", "sed -n '/a/R /etc/passwd' f", "sed -n '1,5r /etc/passwd' f", "sed -n '1,5R /etc/passwd' f",
    "sed -n '/a/p;w out' f", "sed -n '1,5p;w out' f", "sed -n '/a/{p;w out}' f", "sed -n '/a/p\nw out' f", "sed -n '1,5p\nw out' f",
    "sed -n \"1,5p\" f", "sed -n 1,5p f", "sed -n '1,5p' f > out", "sed -n '1,5p' f >> out", "sed -n '1,5p' > out", "sed -n '1,5p' <<EOF", "sed -n '/a/p' <<'EOF'",
    "sed 's/a/b/' f", "sed -e '1p' f", "sed -n '/a/y/b/c/p' f", "sed -n '/a/z' f", "sed -n '/a/F' f", "sed -n '/a/=' f", "sed '1d' f",
)


class TestChiefOfStaffSed(unittest.TestCase):
    def setUp(self):
        self.rules = rules_of(PATH.read_text())

    def test_every_allowed_form_is_allowed(self):
        for command in ALLOWED:
            with self.subTest(command=command):
                self.assertEqual(verdict(self.rules, command), "allow")

    def test_every_writing_or_running_spelling_is_denied(self):
        for command in DENIED:
            with self.subTest(command=command):
                self.assertEqual(verdict(self.rules, command), "deny")

    def test_the_sed_denies_come_after_the_allows(self):
        effects = [e for a, r, e in self.rules if a == "shell" and r.startswith("sed")]
        self.assertIn("allow", effects)
        self.assertIn("deny", effects)
        last_allow = max(i for i, (a, r, e) in enumerate(self.rules) if e == "allow")
        sed_denies = [i for i, (a, r, e) in enumerate(self.rules) if r.startswith("sed") and e == "deny"]
        self.assertTrue(all(i > last_allow for i in sed_denies))

    ONLY = {   # each deny and a form only it refuses (so dropping it lets that form through)
        "sed *;*": "sed -n '/a/;/p' f", "sed *\n*": "sed -n '/a/\n/p' f", "sed *{*": "sed -n '/a/{/p' f", "sed *>*": "sed -n '/a/p' f>o", "sed *<*": "sed -n '/a/p' <f",
        "sed * -*": "sed -n '/a/p' -z f", "sed -n '?*/ *'*": "sed -n '/a/ /p' f", "sed -n '?*/!*'*": "sed -n '/a/!/p' f",
    }

    def test_a_mutant_that_drops_a_deny_or_loosens_an_allow_fails(self):
        only = dict(self.ONLY)
        for letter in "wWerRyzF=":
            only["sed -n '?*/%s*'*" % letter] = "sed -n '/a/%s/p' f" % letter
        for letter in "wWerR":
            only["sed -n '*,*%s*'*" % letter] = "sed -n '1,5%s/p' f" % letter
            only["sed -n '*%s*,*'*" % letter] = "sed -n '1~2%s,p' f" % letter
        denies = [i for i, (a, r, e) in enumerate(self.rules) if r.startswith("sed") and e == "deny"]
        self.assertEqual({self.rules[i][1] for i in denies}, set(only), "every sed deny has its own example")
        for i in denies:
            mutant = self.rules[:i] + self.rules[i + 1:]
            with self.subTest(dropped=self.rules[i][1]):
                self.assertEqual(verdict(self.rules, only[self.rules[i][1]]), "deny")
                self.assertEqual(verdict(mutant, only[self.rules[i][1]]), "allow")
        loose = [(a, "sed *" if r.startswith("sed -n '") and e == "allow" else r, e) for a, r, e in self.rules]
        self.assertTrue(any(verdict(loose, c) == "allow" for c in DENIED), "loosened allows are caught")

    def test_nothing_else_in_the_definition_changes(self):
        now = PATH.read_text().splitlines(keepends=True)
        kept = [l for l in now if "sed" not in l.split("resource:")[-1][:12] and not l.lstrip().startswith("# sed")]
        self.assertEqual("".join(kept), BEFORE.read_text())

    def test_the_documentation_lists_the_forms_and_why_sed_n_alone_is_not_read_only(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        self.assertIn("sed -n '<first>,<last>p' <file>", text)
        self.assertIn("`sed -n` alone is not read-only", text)


if __name__ == "__main__":
    unittest.main()
