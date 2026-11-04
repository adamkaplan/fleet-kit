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


class ClientIsGetOnly(unittest.TestCase):
    """The pod's client allows the by-id read and refuses every write form and every near path."""

    @classmethod
    def setUpClass(cls):
        import importlib.machinery, importlib.util
        loader = importlib.machinery.SourceFileLoader("cw_gh_by_id", os.path.join(ROOT, "cloud/worker/cw-gh"))
        spec = importlib.util.spec_from_loader("cw_gh_by_id", loader)
        cls.m = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.m)

    def test_path_allowed(self):
        self.assertTrue(self.m.api_path_allowed("/repos/o/r/issues/comments/1"))
        self.assertTrue(self.m.api_path_allowed("/repos/o/r/issues/comments/1?per_page=1"))
        for bad in ("abc", "1/reactions", "1/../2"):
            self.assertFalse(self.m.api_path_allowed("/repos/o/r/issues/comments/" + bad), bad)

    def test_non_ascii_digit_ids_refused(self):
        base = "/repos/o/r/issues/comments/"
        self.assertTrue(self.m.api_path_allowed(base + "123"), "control: ascii id")
        for bad in ("\u0661\u0662\u0663", "\uff11\uff12\uff13"):
            self.assertFalse(self.m.api_path_allowed(base + bad), "non-ascii digit id allowed: " + repr(bad))

    def test_mutant_restoring_unicode_digits_fails_anchored(self):
        import importlib.machinery, importlib.util
        with open(os.path.join(ROOT, "cloud/worker/cw-gh")) as f:
            text = f.read()
        old, new = "issues/comments/[0-9]+", "issues/comments/\\d+"
        self.assertEqual(text.count(old), 1)
        noop_text = text.replace(old, old)
        self.assertEqual(noop_text, text, "the no-op mutation changed the text")
        mut = text.replace(old, new)
        self.assertEqual(len(mut), len(text) - len(old) + len(new), "the mutation was not applied")
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            res = {}
            for label, body in (("control", text), ("mutant", mut)):
                fp = os.path.join(d, label + "-cw-gh")
                with open(fp, "w") as f:
                    f.write(body)
                ld = importlib.machinery.SourceFileLoader("cw_gh_" + label, fp)
                m = importlib.util.module_from_spec(importlib.util.spec_from_loader(ld.name, ld))
                ld.exec_module(m)
                res[label] = m.api_path_allowed("/repos/o/r/issues/comments/\u0661\u0662\u0663")
            self.assertFalse(res["control"], "control must refuse the Arabic-Indic id")
            self.assertTrue(res["mutant"], "the mutant must allow the Arabic-Indic id (anchored: non-ascii digit id allowed)")

    def test_write_forms_refused_by_api_get(self):
        for extra in (["-X", "PATCH"], ["-f", "a=b"]):
            with self.assertRaises(self.m.Fail):
                self.m.api_get(["repos/o/r/issues/comments/1"] + extra)

    BASE = "repos/o/r/issues/comments/1"

    def refused_before_request(self, extra):
        calls = []
        real = self.m.request
        self.m.request = lambda *a, **k: calls.append(a) or b"{}"
        try:
            with self.assertRaises(self.m.Fail, msg=repr(extra)):
                self.m.api_get([self.BASE] + extra)
        finally:
            self.m.request = real
        self.assertEqual(calls, [], "a request was sent for " + repr(extra))

    def test_refused_x_mixed_case_patch(self):
        self.refused_before_request(["-X", "pAtch"])

    def test_refused_x_mixed_case_put(self):
        self.refused_before_request(["-X", "PuT"])

    def test_refused_x_mixed_case_delete(self):
        self.refused_before_request(["-X", "DeLeTe"])

    def test_refused_method_equals_patch(self):
        self.refused_before_request(["--method=patch"])

    def test_refused_x_attached_patch(self):
        self.refused_before_request(["-Xpatch"])

    def test_refused_x_equals_patch(self):
        self.refused_before_request(["-X=patch"])

    def test_refused_method_override_header(self):
        self.refused_before_request(["-H", "X-HTTP-Method-Override: PATCH"])

    def test_refused_hostname(self):
        self.refused_before_request(["--hostname", "example.com"])

    def test_path_refused_reactions(self):
        self.assertFalse(self.m.api_path_allowed("/repos/o/r/issues/comments/1/reactions"))

    def test_path_refused_extra_segment(self):
        self.assertFalse(self.m.api_path_allowed("/repos/o/r/issues/comments/1/extra"))

    def test_path_refused_dot_dot(self):
        self.assertFalse(self.m.api_path_allowed("/repos/o/r/issues/comments/1/../2"))


if __name__ == "__main__":
    unittest.main()
