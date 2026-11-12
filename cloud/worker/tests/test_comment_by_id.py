#!/usr/bin/env python3
"""Offline test: the by-id comment read passes the pod file (ask #2402: the file holds no row for it); the client refuses every write form."""
import fnmatch, json, os, re, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
JSON = os.path.join(ROOT, "cloud/worker/opencode-worker.json")
BASE = "gh api repos/o/r/issues/comments/123"
READS = [BASE, BASE + " --jq .body", BASE + "?per_page=1"]
WRITES = [BASE + " " + w for w in (
    "-X PATCH", "-X DELETE", "-X patch", "--method PATCH", "--method delete",
    "-f body=x", "-F body=x", "--field body=x", "--raw-field body=x", "--input f")]


def blocks(text):
    d = json.loads(text)
    return {"global": d["permission"]["bash"], "worker": d["agent"]["worker"]["permission"]["bash"]}


def verdict(rules, cmd):
    v = None
    for pat, val in rules.items():
        if fnmatch.fnmatchcase(cmd, pat):
            v = val
    return v


class ById(unittest.TestCase):
    """Ask #2402: the by-id read rows (and the write denies they were placed after) are gone from the file; the read still passes through its catch-all
    allow, the file names no by-id row, and the write forms are refused by the client (ClientIsGetOnly below), not by the file."""

    def real(self):
        with open(JSON) as f:
            return f.read()

    def test_the_read_forms_are_allowed_in_both_blocks_and_no_row_names_the_by_id_path(self):
        for name, rules in blocks(self.real()).items():
            self.assertEqual(rules, {"*": "allow"}, name)
            for c in READS:
                self.assertEqual(verdict(rules, c), "allow", "%s %s" % (name, c))
            self.assertEqual([k for k in rules if "comments" in k], [], name)

    def test_the_file_carries_no_deny_for_the_write_forms(self):
        # the replacement for the old row-order test: the writes are the client's to refuse (test_write_forms_refused_by_api_get)
        for name, rules in blocks(self.real()).items():
            for c in WRITES:
                self.assertEqual(verdict(rules, c), "allow", "%s %s" % (name, c))


class ClientIsGetOnly(unittest.TestCase):
    """The pod's client has no write form (only GET is implemented)."""

    @classmethod
    def setUpClass(cls):
        import importlib.machinery, importlib.util
        loader = importlib.machinery.SourceFileLoader("cw_gh_by_id", os.path.join(ROOT, "cloud/worker/cw-gh"))
        spec = importlib.util.spec_from_loader("cw_gh_by_id", loader)
        cls.m = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.m)


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


if __name__ == "__main__":
    unittest.main()
