"""#1549: the push rules of cloud/worker/opencode-worker.json match main and master only as a whole ref name or the last part of a ref.
Offline: the matcher is the repository's own (the one of test_guard_table.py: re.fullmatch of the pattern with '*' as '.*', re.S), not
opencode's; the live check is a tester's job in a real pod. Mutants run on a scratch copy of the file."""
import functools
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


@functools.lru_cache(maxsize=None)
def _rx(pat):
    return re.compile(".*".join(re.escape(x) for x in pat.split("*")), re.S)


def matches(pat, line):
    return _rx(pat).fullmatch(line) is not None


PREFIX = "*git*push*"
# characters that can be part of a ref-name word: a word next to one of them is a name and stays allowed
NAME = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
# '/' after the word continues a name (main/x); '/' before the word makes it the last part of a ref.
# '*' and '?' are left out: opencode reads them as wildcards, so no rule can name either one literally (reported, not a rule).
# '${B:-main}' stays allowed: its '-' before the word is a name character, the same as in 'release-main'.
PUNCT = [c for c in map(chr, range(33, 127)) if c not in NAME and c not in "/?*"]
SEPARATORS = [" ", "\t", "\r", "\n"]
AFTER = PUNCT + SEPARATORS
BEFORE = AFTER + ["/"]
# note: Main and MAIN stay allowed on purpose: the remote's ref names are case-sensitive


def generate(word):
    """The push rules of one word: a closer before it, then a closer after it or the end of the command, in a fixed order."""
    out = {}
    for b in BEFORE:
        out[PREFIX + b + word] = "deny"
        for a in AFTER:
            out[PREFIX + b + word + a + "*"] = "deny"
    return out


def generate_all():
    out = {}
    for w in WORDS:
        out.update(generate(w))
    return out


def write_conf(conf):
    """Put the generated rules at the END of both blocks, after every other rule."""
    for b in blocks(conf):
        keep = {k: v for k, v in b.items() if not k.startswith(PREFIX)}
        b.clear()
        b.update(keep)
        b.update(generate_all())
    return conf


def push_patterns(bash):
    return [p for p in bash if p.startswith(PREFIX)]


def hit(bash, line):
    return any(matches(p, line) for p in bash if "git*push*" in p and bash[p] == "deny" and "push.default" not in p)


def hit_new(bash, line):
    return any(matches(p, line) for p in push_patterns(bash))


def load():
    with open(CONF) as f:
        return json.load(f)


def forms():
    out = []
    for w in WORDS:
        refs = [w, "refs/heads/" + w, "HEAD:" + w, "HEAD:refs/heads/" + w, "src:" + w, w + ":" + w, ":" + w, "x:" + w]
        for r in refs:
            for q in ("%s", "'%s'", '"%s"'):
                out.append("git push origin " + q % r)
        out += ["git push -u origin " + w, "git push origin HEAD:" + w + " ", "git push origin " + w + ";",
                "git push --force origin x", "git push -f origin x", "git push --force-with-lease origin x",
                "git push origin +x:y", "git push --all origin", "git push --mirror origin",
                "git push --force origin " + w, "git push -f origin " + w, "git push origin +" + w,
                "git push origin refs/heads/" + w + " && echo ok",
                "git push origin :" + w + " ; echo ok",
                "git\tpush\torigin\t" + w, "git push origin " + w + "\n", "git push origin\n" + w, "git\npush\rorigin\r" + w,
                "git push origin HEAD:" + w + "\t", "git push origin " + w + "\r", "git push origin " + w + "#x",
                "git push origin " + w + ">/dev/null", "git push origin {" + w + ",x}", "git push origin " + w + "^", "git push origin " + w + "~1",
                "git push origin " + w + ",x", "git push origin " + w + "=1", "git push origin (" + w + "\\"]
        # one form per closer character on each side, the other side closed too
        out += ["git push origin a" + c + w + ";" for c in BEFORE]
        out += ["git push origin ;" + w + c + "x" for c in AFTER]
        for pre in ("git fetch", "true"):
            c = "git push origin " + w
            out += [pre + " ; " + c, pre + " && " + c, pre + " || " + c, pre + " | " + c, "( " + c + " )", "(" + c + ")",
                    "echo $(" + c + ")", "echo `" + c + "`", "( " + c + "; true)", "echo $(" + c + ")x"]
    return sorted(set(out))


LATER_RULE = ["gh api x/issues/1/comments ; git push origin main", "gh api x/issues/1/comments ; git push origin master",
               "gh api x/pulls/1/comments ; git push origin HEAD:main"]


def order_problems(bash):
    """For each command that a later allow rule also matches, the LAST matching rule must be a push rule that blocks."""
    bad = []
    keys = list(bash)
    for line in LATER_RULE:
        m = [k for k in keys if matches(k, line)]
        if not any(bash[k] == "allow" and k != "*" for k in m if not k.startswith(PREFIX)):
            bad.append(("no later allow matched", line))
        if not m or not m[-1].startswith(PREFIX) or bash[m[-1]] != "deny":
            bad.append(("last match", line, m[-1:]))
    return bad


INNOCENT = [
    "git push origin preview/cockpit-sync-main-slice0", "git push origin mainline", "git push origin master-plan-slice",
    "git push origin x-main-y", "git push origin release-main", "git push origin main2", "git push origin feature-master",
    "git push -u origin HEAD:preview/cockpit-sync-main-slice0", "git push origin HEAD:refs/heads/mainline",
    "git push origin cw/main-guard", "git push origin cw/master-list; true", "git push origin 'x-main-y'",
    "git push origin \"release-master\"", "git push origin feat/mainframe && echo ok",
    "git push origin main/x", "git push origin main.bak", "git push origin main-fix", "git push origin main_x", "git push origin Main",
    "git push origin MAIN", "git push origin master/x", "git push origin master.bak", "git push origin x_master_y",
]


def innocent_hits(conf):
    return [(i, line) for i, b in enumerate(blocks(conf)) for line in INNOCENT if hit(b, line)]


def form_misses(conf):
    fs = forms()
    return [(i, x) for i, b in enumerate(blocks(conf)) for x in fs if not hit(b, x)]


def closer_misses(conf):
    """One form per closer character, for both words and both blocks, matched by the generated push rules alone."""
    out = []
    for i, b in enumerate(blocks(conf)):
        for w in WORDS:
            for c in BEFORE:
                if not hit_new(b, "git push origin a" + c + w + ";"):
                    out.append((i, w, "before", c))
                if not hit_new(b, "git push origin a" + c + w):
                    out.append((i, w, "end", c))
            for c in AFTER:
                if not hit_new(b, "git push origin ;" + w + c + "x"):
                    out.append((i, w, "after", c))
            if not hit_new(b, "git push origin :" + w):
                out.append((i, w, "delete"))
    return out


def delete_misses(conf):
    out = []
    for i, b in enumerate(blocks(conf)):
        for w in WORDS:
            for x in ("git push origin :" + w, "git push origin :" + w + " ; true", "git push origin ':" + w + "'"):
                if not hit(b, x):
                    out.append((i, x))
    return out


class Slices(unittest.TestCase):
    def setUp(self):
        self.conf = load()

    def test_s2_branches_with_the_word_match_no_rule(self):
        self.assertGreaterEqual(len(INNOCENT), 12)
        self.assertEqual(innocent_hits(self.conf), [])

    def test_s3_every_listed_form_is_matched_in_both_blocks(self):
        self.assertEqual(form_misses(self.conf), [])

    def test_s3b_every_closer_character_is_matched_for_both_words_in_both_blocks(self):
        self.assertEqual(len(BEFORE), len(AFTER) + 1)
        for c in "\t\r\n >#<}]^,~=(\\{[":
            self.assertIn(c, AFTER)
        self.assertNotIn("/", AFTER)
        self.assertEqual(closer_misses(self.conf), [])
        self.assertEqual(delete_misses(self.conf), [])

    def test_s4_two_blocks_carry_the_same_push_rules_and_the_family_is_listed(self):
        top, inner = blocks(self.conf)
        a = [(p, v) for p, v in top.items() if "git*push" in p]
        b = [(p, v) for p, v in inner.items() if "git*push" in p]
        self.assertEqual(a, b)
        for blk in (top, inner):
            starts = [k for k in blk if k.startswith(PREFIX)]
            self.assertGreater(len(starts), 0)
            for k in starts:
                self.assertEqual(blk[k], "deny", k)
        for p in FAMILY:
            self.assertEqual(top.get(p), "deny")
        self.assertNotIn("git*push*main*", top)
        self.assertNotIn("git*push*master*", top)

    def test_s4b_the_push_rules_are_the_generated_set_in_both_blocks(self):
        want = generate_all()
        for b in blocks(self.conf):
            self.assertEqual(push_patterns(b), list(want))
            self.assertEqual({k: b[k] for k in push_patterns(b)}, want)

    def test_s4c_the_push_rules_come_last_in_the_same_order_after_every_other_rule(self):
        for b in blocks(self.conf):
            keys = list(b)
            n = len(push_patterns(b))
            self.assertEqual(keys[-n:], push_patterns(b))
        self.assertEqual(order_problems(blocks(self.conf)[0]), [])
        self.assertEqual(order_problems(blocks(self.conf)[1]), [])

    def test_s4d_a_command_that_a_later_rule_matches_is_matched_last_by_a_push_rule(self):
        for b in blocks(self.conf):
            line = "gh api x/issues/1/comments ; git push origin main"
            m = [k for k in b if matches(k, line)]
            allow = [k for k in m if b[k] == "allow" and k != "*"]
            self.assertTrue(allow)
            self.assertGreater(list(b).index(m[-1]), list(b).index(allow[-1]))
            self.assertEqual(b[m[-1]], "deny")

    def test_s5_mutants_are_caught(self):
        text = open(CONF).read()

        def mutant():
            return json.loads(text)

        # one closer character of one word dropped in one block (after the word, then before it)
        for side, at in (("after", ">"), ("before", "#")):
            conf = mutant()
            b = conf["permission"]["bash"]
            if side == "after":
                drop = [k for k in b if k.startswith(PREFIX) and k.endswith("main" + at + "*")]
            else:
                drop = [k for k in b if k.startswith(PREFIX + at + "main")]
            self.assertGreater(len(drop), 0)
            for k in drop:
                del b[k]
            self.assertTrue(any(m[:3] == (0, "main", side) and m[3] == at for m in closer_misses(conf)))
            self.assertEqual([m for m in closer_misses(conf) if m[0] != 0 or m[1] != "main"], [])
        conf = mutant()
        key = PREFIX + " master"
        self.assertIn(key, conf["agent"]["worker"]["permission"]["bash"])
        del conf["agent"]["worker"]["permission"]["bash"][key]
        self.assertGreater(len(closer_misses(conf)), 0)
        # the delete form dropped, per word and per block
        for bi in (0, 1):
            for w in WORDS:
                conf = mutant()
                b = blocks(conf)[bi]
                drop = [k for k in b if k.startswith(PREFIX + ":" + w)]
                self.assertGreater(len(drop), 0)
                for k in drop:
                    del b[k]
                self.assertTrue(any(m[0] == bi and m[1] == w for m in closer_misses(conf)))
                self.assertGreater(len(delete_misses(conf)), 0)
        # the old substring rules restored
        conf = mutant()
        for b in blocks(conf):
            drop = [k for k in b if k.startswith(PREFIX)]
            self.assertGreater(len(drop), 0)
            for k in drop:
                del b[k]
            b.update({"git*push*main*": "deny", "git*push*master*": "deny"})
        self.assertGreater(len(innocent_hits(conf)), 0)
        # the push rules put before an allow rule
        conf = mutant()
        for b in blocks(conf):
            rules = {k: b.pop(k) for k in push_patterns(b)}
            items = list(b.items())
            at = [i for i, (k, v) in enumerate(items) if k == "gh api*/issues/*/comments *"][0]
            b.clear()
            b.update(dict(items[:at] + list(rules.items()) + items[at:]))
        self.assertGreater(len(order_problems(blocks(conf)[0])), 0)
        self.assertGreater(len(order_problems(blocks(conf)[1])), 0)

    def test_s6_the_wrapper_is_unchanged_and_the_documentation_says_it(self):
        r = subprocess.run(["git", "diff", "origin/integration", "--", "cloud/worker/guard.sh"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.stdout, "")
        doc = open(os.path.join(ROOT, "docs", "cloud-worker-interface.md")).read()
        self.assertIn("only as a whole ref name or as the last part of a ref", doc)
        self.assertIn("The git wrapper `cloud/worker/guard.sh` reads refs exactly", doc)


if __name__ == "__main__":
    if os.environ.get("WRITE_PUSH_RULES") == "1":
        out = json.dumps(write_conf(load()), indent=2, ensure_ascii=False) + "\n"
        with open(CONF, "w") as f:
            f.write(out)
    else:
        unittest.main()
