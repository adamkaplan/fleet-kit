"""#1549: the push rules of cloud/worker/opencode-worker.json match main and master only as a whole ref name or the last part of a ref.
Offline: the matcher is the repository's own (the one of test_guard_table.py: re.fullmatch of the pattern with '*' as '.*', re.S), not
opencode's; the live check is a tester's job in a real pod. Mutants run on a scratch copy of the file."""
import json
import os
import re
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(WORKER))
CONF = os.path.join(WORKER, "opencode-worker.json")

# the family: every other substring rule over text a person chooses, with its decision
FAMILY = {
    "git*push*--force*": "kept: the long flag is found anywhere in the command",
    "git*push* -f*": "kept: a '-f' word is rare in a branch name and the cost of a miss is a force push",
    "git*push*+*": "kept: a plus in a branch name is rare and the cost of a miss is a force push",
    "git*push*--mirror*": "kept: the flag text is never part of a branch name",
    "git*push*--all*": "kept: the flag text is never part of a branch name",
}
WORDS = ("main", "master")


def blocks(conf):
    top = conf["permission"]["bash"]
    inner = conf["agent"]["worker"]["permission"]["bash"]
    return top, inner


def matches(pat, line):
    return re.fullmatch(".*".join(re.escape(x) for x in pat.split("*")), line, re.S) is not None


def push_patterns(bash):
    return [p for p in bash if "git*push*" in p and p not in FAMILY and "push.default" not in p]


def hit(bash, line):
    return any(matches(p, line) for p in bash if "git*push*" in p and bash[p] == "deny" and "push.default" not in p)


def load():
    with open(CONF) as f:
        return json.load(f)


def forms():
    out = []
    for w in WORDS:
        refs = [w, "refs/heads/" + w, "HEAD:" + w, "HEAD:refs/heads/" + w, "src:" + w, w + ":" + w, ":" + w]
        for r in refs:
            for q in ("%s", "'%s'", '"%s"'):
                out.append("git push origin " + q % r)
        out += ["git push -u origin " + w, "git push origin HEAD:" + w + " ", "git push origin " + w + ";",
                "git push --force origin x", "git push -f origin x", "git push --force-with-lease origin x",
                "git push origin +x:y", "git push --all origin", "git push --mirror origin",
                "git push --force origin " + w, "git push -f origin " + w, "git push origin +" + w,
                "git push origin refs/heads/" + w + " && echo ok",
                "git push origin :" + w + " ; echo ok"]
        for pre in ("git fetch", "true"):
            c = "git push origin " + w
            out += [pre + " ; " + c, pre + " && " + c, pre + " || " + c, pre + " | " + c, "( " + c + " )", "(" + c + ")",
                    "echo $(" + c + ")", "echo `" + c + "`", "( " + c + "; true)", "echo $(" + c + ")x"]
    return sorted(set(out))


INNOCENT = [
    "git push origin preview/cockpit-sync-main-slice0", "git push origin mainline", "git push origin master-plan-slice",
    "git push origin x-main-y", "git push origin release-main", "git push origin main2", "git push origin feature-master",
    "git push -u origin HEAD:preview/cockpit-sync-main-slice0", "git push origin HEAD:refs/heads/mainline",
    "git push origin cw/main-guard", "git push origin cw/master-list; true", "git push origin 'x-main-y'",
    "git push origin \"release-master\"", "git push origin feat/mainframe && echo ok",
]


class Slices(unittest.TestCase):
    def setUp(self):
        self.conf = load()

    def test_s2_branches_with_the_word_match_no_rule(self):
        self.assertGreaterEqual(len(INNOCENT), 12)
        n = 0
        for b in blocks(self.conf):
            for line in INNOCENT:
                if hit(b, line):
                    n += 1
                    print("MATCHED", line)
        self.assertEqual(n, 0)

    def test_s3_every_listed_form_is_matched_in_both_blocks(self):
        fs = forms()
        self.assertGreaterEqual(len(fs), 30)
        for b in blocks(self.conf):
            miss = [x for x in fs if not hit(b, x)]
            self.assertEqual(miss, [])

    def test_s4_two_blocks_carry_the_same_push_rules_and_the_family_is_listed(self):
        top, inner = blocks(self.conf)
        a = [(p, v) for p, v in top.items() if "git*push" in p]
        b = [(p, v) for p, v in inner.items() if "git*push" in p]
        self.assertEqual(a, b)
        for p in FAMILY:
            self.assertEqual(top.get(p), "deny")
        extra = [p for p in push_patterns(top) if not any(w in p for w in WORDS)]
        self.assertEqual(extra, [])
        self.assertNotIn("git*push*main*", top)
        self.assertNotIn("git*push*master*", top)

    def test_s5_mutants_are_caught(self):
        text = open(CONF).read()
        old = {"git*push*main*": "deny", "git*push*master*": "deny"}
        for name, mut, want in (("old", None, "s2"), ("nodelete", None, "s3")):
            conf = json.loads(text)
            for b in blocks(conf):
                keep = {k: v for k, v in b.items()}
                if name == "old":
                    drop = [k for k in keep if "git*push*" in k and any(w in k for w in WORDS)]
                else:
                    drop = [k for k in keep if re.match(r"\*git\*push\*:(main|master)", k)]
                self.assertGreater(len(drop), 0)
                for k in drop:
                    del b[k]
                if name == "old":
                    b.update(old)
                self.assertNotEqual(b, keep)
            if want == "s2":
                self.assertGreater(sum(hit(b, x) for b in blocks(conf) for x in INNOCENT), 0)
            else:
                self.assertTrue(any(not hit(b, x) for b in blocks(conf) for x in forms()))

    def test_s6_the_wrapper_is_unchanged_and_the_documentation_says_it(self):
        r = subprocess.run(["git", "diff", "origin/integration", "--", "cloud/worker/guard.sh"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.stdout, "")
        doc = open(os.path.join(ROOT, "docs", "cloud-worker-interface.md")).read()
        self.assertIn("only as a whole ref name or as the last part of a ref", doc)
        self.assertIn("The git wrapper `cloud/worker/guard.sh` reads refs exactly", doc)


if __name__ == "__main__":
    unittest.main()
