#!/usr/bin/env python3
"""Offline test: the by-id comment read is allowed and every write form of it stays denied (both blocks)."""
import fnmatch, json, os, re, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
JSON = os.path.join(ROOT, "cloud/worker/opencode-worker.json")
BASE = "gh api repos/o/r/issues/comments/123"
READS = [BASE, BASE + " --jq .body", BASE + "?per_page=1"]
WRITES = [BASE + " " + w for w in (
    "-X PATCH", "-X DELETE", "-X patch", "--method PATCH", "--method delete",
    "-f body=x", "-F body=x", "--field body=x", "--raw-field body=x", "--input f")]
NEW = ('"gh api*/issues/comments/*"', '"gh api*/issues/comments/* *"', '"gh api*/issues/comments/*?*"')
POST_ROW = '"gh api*--input*": "deny",'


def blocks(text):
    d = json.loads(text)
    return {"global": d["permission"]["bash"], "worker": d["agent"]["worker"]["permission"]["bash"]}


def verdict(rules, cmd):
    v = None
    for pat, val in rules.items():
        if fnmatch.fnmatchcase(cmd, pat):
            v = val
    return v


def problems(text):
    out = []
    for name, rules in blocks(text).items():
        for c in READS:
            if verdict(rules, c) != "allow":
                out.append("a read form of the by-id call is not allowed: %s %s" % (name, c))
        for c in WRITES:
            if verdict(rules, c) != "deny":
                out.append("a write form of the by-id call is allowed: %s %s" % (name, c))
    return out


def position_computation(text):
    """True when, in every block, the three new rows come after the last write deny."""
    flags = []
    for name, rules in blocks(text).items():
        keys = list(rules)
        flags.append(all(keys.index(n.strip('"')) > keys.index("gh api*--input*") for n in NEW))
    return flags


def move_after_writes(text):
    """Move the three new rows of each block to just after the last write deny; asserts each shape occurs once per block."""
    out = text
    for ind in ("      ", "          "):
        rows = "".join("\n" + ind + n + ': "allow",' for n in NEW)
        assert out.count(rows) == 1, "the new rows must occur once per block"
        out = out.replace(rows, "")
        post = "\n" + ind + POST_ROW
        assert out.count(post) == 1, "the last write deny row must occur once per block"
        out = out.replace(post, post + rows)
    return out


def noop(text):
    return text


class ById(unittest.TestCase):
    def real(self):
        with open(JSON) as f:
            return f.read()

    def test_control_real_file(self):
        self.assertEqual(problems(self.real()), [])
        self.assertEqual(position_computation(self.real()), [False, False])

    def test_mutant_fails_with_anchored_text(self):
        text = self.real()
        self.assertEqual(problems(text), [], "control must pass first")
        mut = move_after_writes(text)
        self.assertNotEqual(mut, text, "the mutation was not applied")
        self.assertEqual(position_computation(mut), [True, True], "the mutation was not applied")
        found = problems(mut)
        self.assertTrue(any(p.startswith("a write form of the by-id call is allowed:") for p in found), found)

    def test_noop_mutation_fails_the_computation(self):
        text = self.real()
        self.assertNotEqual(position_computation(noop(text)), [True, True],
                            "the no-op mutation must fail the computation")


if __name__ == "__main__":
    unittest.main()
