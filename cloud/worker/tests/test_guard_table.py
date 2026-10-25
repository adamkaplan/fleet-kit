"""#1083: the ONE table cloud/worker/guard-table.tsv. Every row runs through the guard layer (guard.sh plus the allow and deny patterns of
opencode-worker.json, last match wins) and through the client (cw-gh against a loopback stub): they must agree. Mutants: a config that opens
the whole issues path, and a client that opens it, each make a row fail. Offline; the token is synthetic."""
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.dirname(HERE)
TABLE = os.path.join(WORKER, "guard-table.tsv")
GUARD = os.path.join(WORKER, "guard.sh")
CONF = os.path.join(WORKER, "opencode-worker.json")
CW_GH = os.path.join(WORKER, "cw-gh")
TOKEN = "synthetic-" + "Zp4Rt9" * 6
NEW_IDS = {"issue-comments-read", "pull-reviews-read", "pull-comments-read", "api-jq-filter", "issues-other-path"}


def load_conf():
    with open(CONF) as f:
        return json.load(f)


def rows():
    out = []
    for line in open(TABLE).read().splitlines():
        if line and not line.startswith("#"):
            f = line.split("\t")
            assert len(f) == 7, line
            out.append(dict(zip(("id", "kind", "tool", "method", "path", "why", "example"), f)))
    return out


def conf_decision(conf, line):
    bash = conf["permission"]["bash"]
    assert bash == conf["agent"]["worker"]["permission"]["bash"]
    res = "allow"
    for pat, v in bash.items():   # opencode: the last matching rule wins
        if re.fullmatch(".*".join(re.escape(x) for x in pat.split("*")), line, re.S):
            res = v
    return res


def guard_denies(example, conf=None):
    args = example.split()
    r = subprocess.run(["sh", GUARD, "gh"] + args, capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
    if r.returncode == 3:
        return True
    assert r.returncode == 0, r.stderr
    return conf_decision(conf or load_conf(), "gh " + example) == "deny"


class Rec(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Rec.seen.append((self.command, self.path))
        out = json.dumps([{"body": "hello", "user": {"login": "alice"}, "created_at": "t"}]).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_POST = do_PATCH = do_PUT = do_DELETE = do_GET


class Table(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Rec)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_port
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.addClassCleanup(cls.srv.shutdown)

    def client(self, example, script=CW_GH):
        Rec.seen = []
        env = {"PATH": "/usr/bin:/bin", "CW_GH_API_BASE": self.base, "GH_TOKEN": TOKEN, "HOME": "/nonexistent"}
        return subprocess.run([sys.executable, script] + example.split(), capture_output=True, text=True, env=env, timeout=60, stdin=subprocess.DEVNULL)

    def test_the_table_holds_the_1083_rows_and_a_place_for_the_later_asks(self):
        ids = {r["id"] for r in rows()}
        self.assertTrue(NEW_IDS <= ids, NEW_IDS - ids)
        self.assertEqual(len(ids), len(rows()))   # no id twice
        text = open(TABLE).read()
        self.assertIn("ask 1166 rows: add below this line", text)
        self.assertIn("ask 1200 rows: add below this line", text)

    def test_u2_the_guard_and_the_client_agree_on_every_row(self):
        for r in rows():
            if r["id"].startswith("pr-edit-"):
                continue   # ask 1200: these rows write; tests/test_pr_edit.py runs them through both
            with self.subTest(row=r["id"]):
                denied = r["kind"] == "denied"
                self.assertEqual(guard_denies(r["example"]), denied, "guard: " + r["id"])
                p = self.client(r["example"])
                if denied:
                    self.assertEqual(p.returncode, 2, (r["id"], p.stderr))
                    self.assertEqual([x for x in Rec.seen if x[0] != "GET"], [])
                    self.assertNotIn("issues/7/comments", " ".join(x[1] for x in Rec.seen) if r["id"] != "issues-write-post" else "")
                else:
                    self.assertEqual(p.returncode, 0, (r["id"], p.stderr))
                    self.assertTrue(Rec.seen and all(m == "GET" for m, _ in Rec.seen))
                self.assertNotIn(TOKEN, p.stdout + p.stderr)

    def test_s8_the_three_reads_pass_and_each_write_and_other_path_is_denied(self):
        by = {r["id"]: r for r in rows()}
        for i in ("issue-comments-read", "pull-reviews-read", "pull-comments-read"):
            self.assertFalse(guard_denies(by[i]["example"]), i)
        for i in ("issues-write-post", "issues-write-patch", "issues-write-delete", "issues-other-path", "issues-labels-path", "pull-merge", "pull-review-write"):
            self.assertTrue(guard_denies(by[i]["example"]), i)

    def test_s9_the_jq_filter_applies_client_side_and_the_write_flags_stay_refused(self):
        for q in ("--jq", "-q"):
            p = self.client("api repos/acme-org/widget-service/issues/7/comments %s .[0].body" % q)
            self.assertEqual((p.returncode, p.stdout), (0, "hello\n"), p.stderr)
            self.assertEqual(len(Rec.seen), 1)
        for r in rows():
            if r["id"].startswith("api-write-flags"):
                self.assertTrue(guard_denies(r["example"]), r["id"])
                self.assertEqual(self.client(r["example"]).returncode, 2)

    def test_s12_no_new_token_permission_appears(self):
        # the rows name no permission and no token file; the role permission table (cloud/token-table.txt) is not touched by this ask
        r = subprocess.run(["git", "diff", "--quiet", "origin/HEAD", "--", "cloud/token-table.txt"], cwd=WORKER, capture_output=True)
        self.assertIn(r.returncode, (0, 128))   # 128: no origin/HEAD in this checkout
        for row in rows():
            self.assertEqual(row["tool"], "gh")

    def test_s10_a_mutant_that_opens_the_whole_issues_path_fails(self):
        conf = load_conf()
        conf["permission"]["bash"] = dict(conf["permission"]["bash"])
        del conf["permission"]["bash"]["gh api*/issues*"]
        conf["agent"]["worker"]["permission"]["bash"] = conf["permission"]["bash"]
        bad = [r["id"] for r in rows() if r["kind"] == "denied" and r["method"] == "GET" and "issues" in r["path"]
               and not guard_denies(r["example"], conf)]
        self.assertIn("issues-other-path", bad)   # the changed row: the agreement test would fail on it

    def test_s10_a_client_that_opens_the_whole_issues_path_fails_the_agreement(self):
        src = open(CW_GH).read()
        old = "        return bool(API_ISSUE_COMMENTS.fullmatch(p))"
        self.assertIn(old, src)
        path = os.path.join(self.tmp.name, "cw-gh-mutant")
        open(path, "w").write(src.replace(old, "        return True", 1))
        self.assertEqual(self.client("api repos/acme-org/widget-service/issues/7", script=path).returncode, 0)   # the mutant answers where the table says no
        self.assertEqual(self.client("api repos/acme-org/widget-service/issues/7").returncode, 2)

    def test_m1_the_client_refuses_a_path_that_is_not_normal_and_makes_no_request(self):
        for bad in ("repos/acme-org/widget-service/./issues/1", "repos/acme-org/widget-service//issues/1", "repositories/1/issues/1",
                    "repos/acme-org/widget-service/issues/1/../1", "repos/acme-org/widget-service/%2e/issues/1",
                    "repos/acme-org/widget-service/issues%2F1", "repos/acme-org/widget-service/%2E%2E/issues/1",
                    "repos/acme-org/widget-service/issues/1/comments//", "user"):
            with self.subTest(path=bad):
                p = self.client("api " + bad)
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertEqual(Rec.seen, [])

    def test_m1_a_mutant_without_the_normalisation_lets_those_paths_through(self):
        src = open(CW_GH).read()
        old = "    if not api_path_clean(path):   # normalisation check BEFORE the allow check\n        return False\n"
        self.assertIn(old, src)
        path = os.path.join(self.tmp.name, "cw-gh-nonorm")
        open(path, "w").write(src.replace(old, "", 1))
        for bad in ("repos/acme-org/widget-service/./issues/1", "repos/acme-org/widget-service//issues/1", "repositories/1/issues/1"):
            p = self.client("api " + bad, script=path)
            self.assertEqual(p.returncode, 0, bad)   # the mutant sends the GET: the test above would fail on it
            self.assertEqual(len(Rec.seen), 1)

    FLAG_PATTERNS = {"-f": "gh api* -f*", "-F": "gh api* -F*", "--input": "gh api*--input*", "graphql": "gh api*graphql*"}

    def table_deny_patterns(self):
        """The permission patterns that the table's denied rows need in opencode-worker.json (the method rows and the flag rows)."""
        out = set()
        for r in rows():
            if r["kind"] != "denied":
                continue
            if r["method"] in ("POST", "PATCH", "PUT", "DELETE"):
                out.add("gh api*%s*" % r["method"])
            if r["path"] in self.FLAG_PATTERNS:
                out.add(self.FLAG_PATTERNS[r["path"]])
        return out

    def missing_deny_rows(self, conf):
        """Table deny rows absent from either bash block, and method deny rows of the file that the table does not list."""
        want = self.table_deny_patterns()
        blocks = (conf["permission"]["bash"], conf["agent"]["worker"]["permission"]["bash"])
        missing = [(i, w) for i, b in enumerate(blocks) for w in sorted(want) if b.get(w) != "deny"]
        extra = [k for k, v in blocks[0].items() if v == "deny" and re.fullmatch(r"gh api\*(POST|PATCH|PUT|DELETE)\*", k) and k not in want]
        return missing, extra

    def test_m2_every_deny_row_of_the_table_is_in_the_permission_file_and_back(self):
        self.assertEqual(self.missing_deny_rows(load_conf()), ([], []))
        self.assertEqual(len(self.table_deny_patterns()), 8)

    def test_m2_a_mutant_that_drops_a_deny_row_is_seen(self):
        for drop in ("gh api*POST*", "gh api*graphql*"):
            conf = json.loads(json.dumps(load_conf()))
            del conf["permission"]["bash"][drop]
            del conf["agent"]["worker"]["permission"]["bash"][drop]
            missing, _ = self.missing_deny_rows(conf)
            self.assertEqual(sorted(w for _, w in missing), [drop, drop], drop)

    def test_low_lower_and_capitalised_method_words_are_denied_in_the_shell_layer_and_by_the_client(self):
        for m in ("post", "Post", "patch", "Patch", "put", "Put", "delete", "Delete"):
            for fl in ("-X", "--method"):
                ex = "api %s %s repos/acme-org/widget-service/pulls/7/comments" % (fl, m)
                with self.subTest(ex=ex):
                    self.assertEqual(conf_decision(load_conf(), "gh " + ex), "deny")
                    self.assertEqual(self.client(ex).returncode, 2)
                    self.assertEqual(Rec.seen, [])
        self.assertEqual(conf_decision(load_conf(), "gh api -X GET repos/acme-org/widget-service/pulls/7/comments"), "allow")

    def test_low_an_unsupported_jq_expression_is_refused_before_any_request(self):
        for expr in (".[]|.body", "map(.id)", ".a|length", "$ENV", ".[0].body?"):
            p = self.client("api repos/acme-org/widget-service/issues/7/comments --jq " + expr)
            self.assertEqual(p.returncode, 2, expr)
            self.assertEqual(Rec.seen, [], expr)


if __name__ == "__main__":
    unittest.main()
