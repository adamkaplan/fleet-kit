"""Commit-status dual-write (#2560): `shims/land_status.py`, its hook in `shims/gh`, and `tools/land-status-parity`. Offline: a fake GitHub API on 127.0.0.1.

| input / external call                              | where it is faked                                              |
|----------------------------------------------------|----------------------------------------------------------------|
| GET  /repos/<r>/installation                       | FakeGitHub (answers {"id": 7}; checks a Bearer JWT is sent)    |
| POST /app/installations/7/access_tokens            | FakeGitHub (answers the planted token and `permissions`)       |
| POST /repos/<r>/statuses/<sha>                     | FakeGitHub (recorded; HTTP 500 when `fail_statuses`)           |
| DELETE /installation/token                         | FakeGitHub (204; recorded)                                     |
| `gh pr view N -R r --json mergeCommit --jq ...`    | a fake gh script printing a 40-hex oid (or nothing)            |
| cw-app-id, cw-app-key in CW_SECRET_DIR             | a temporary directory with a key made by `openssl genrsa`      |
| the gate spool                                      | a list the test hands in as `spool_fn`                         |

Run-by-hand mutants (each has a passing control: the unmutated source passes the same assertion first):
  M1 the post-merge write is dropped          -> S1 sees 3 POSTs, not 6
  M2 the token is not narrowed-checked         -> S2/permissions case lets a wider token through
  M3 the parity checker ignores `creator`      -> S3 forged creator exits 0
  M4 the shim hook never calls `before`        -> the hook pin fails
"""
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODULE = REPO / "shims" / "land_status.py"
GH_SHIM = REPO / "shims" / "gh"
PARITY = REPO / "tools" / "land-status-parity"
TOKEN = "ghs_PLANTEDTOKEN0123456789abcdefghijkl"
HEAD, MERGED = "a" * 40, "b" * 40
SLUG = "acme/app"
CONTEXTS = ("land/gates", "land/reviews", "land/merge-gate")


def load(source=None):
    module = types.ModuleType("land_status_under_test")
    exec(compile(source if source is not None else MODULE.read_text(), str(MODULE), "exec"), module.__dict__)
    return module


class FakeGitHub:
    def __init__(self):
        self.requests, self.fail_statuses, self.permissions = [], False, {"statuses": "write"}
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                outer.requests.append((self.command, self.path, body, self.headers.get("Authorization")))
                code, answer = 404, {}
                if self.command == "GET" and self.path.endswith("/installation"):
                    code, answer = 200, {"id": 7}
                elif self.command == "POST" and self.path == "/app/installations/7/access_tokens":
                    code, answer = 201, {"token": TOKEN, "permissions": outer.permissions}
                elif self.command == "POST" and "/statuses/" in self.path:
                    code, answer = (500, {"message": "boom " + TOKEN}) if outer.fail_statuses else (201, {})
                elif self.command == "DELETE" and self.path == "/installation/token":
                    code = 204
                self.send_response(code)
                self.send_header("Content-Length", "0" if code == 204 else str(len(json.dumps(answer))))
                self.end_headers()
                if code != 204:
                    self.wfile.write(json.dumps(answer).encode())

            do_GET = do_POST = do_DELETE = handle_any

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def statuses(self):
        return [(p.rsplit("/", 1)[1], b["context"], b["state"]) for m, p, b, _ in self.requests if m == "POST" and "/statuses/" in p]


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.api = FakeGitHub()
        self.addCleanup(self.api.close)
        secrets = self.tmp / "secrets"
        secrets.mkdir()
        (secrets / "cw-app-id").write_text("123\n")
        made = subprocess.run(["openssl", "genrsa", "2048"], capture_output=True, text=True)
        self.assertEqual(made.returncode, 0)
        (secrets / "cw-app-key").write_text(made.stdout)
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for name in ("FLEET_LAND_STATUS_TOKEN", "CW_SECRET_DIR"):
            os.environ.pop(name, None)
        os.environ.update({"CW_SECRET_DIR": str(secrets), "FLEET_LAND_STATUS_API_BASE": self.api.base})
        self.spool = []
        self.fake_gh = self.tmp / "gh"
        self.set_gh(MERGED)

    def set_gh(self, oid):
        self.fake_gh.write_text("#!/bin/sh\n%s\n" % ('echo %s' % oid if oid else "exit 1"))
        self.fake_gh.chmod(0o755)

    def spool_fn(self, *row):
        self.spool.append(row)

    def run_both(self, module=None):
        module = module or load()
        listed = {SLUG}
        module.dual_write_before(SLUG, 12, HEAD, self.spool_fn, listed)
        module.dual_write_after(SLUG, 12, HEAD, str(self.fake_gh), dict(os.environ), self.spool_fn, listed)


class TestLandStatusShim(Case):
    def test_S1_three_contexts_on_the_head_then_three_on_the_merge_commit_and_the_token_is_revoked(self):
        self.run_both()
        self.assertEqual(self.api.statuses(), [(HEAD, c, "success") for c in CONTEXTS] + [(MERGED, c, "success") for c in CONTEXTS])
        revokes = [r for r in self.api.requests if r[0] == "DELETE"]
        self.assertEqual([r[1] for r in revokes], ["/installation/token"] * 2)
        self.assertEqual(self.spool, [])
        mint = [r for r in self.api.requests if r[1] == "/app/installations/7/access_tokens"][0]
        self.assertEqual(mint[2], {"repositories": ["app"], "permissions": {"statuses": "write"}})
        self.assertTrue(all(r[3] == "token " + TOKEN for r in self.api.requests if "/statuses/" in r[1]))

    def test_S1_an_unlisted_repository_writes_nothing(self):
        load().dual_write_before("acme/other", 1, HEAD, self.spool_fn, {SLUG})
        self.assertEqual(self.api.requests, [])

    def test_S1_a_wider_token_is_refused_and_revoked(self):   # control for M2
        self.api.permissions = {"statuses": "write", "contents": "write"}
        self.run_both()
        self.assertEqual(self.api.statuses(), [])
        self.assertEqual(len(self.spool), 2)
        self.assertIn("token permissions not narrowed", self.spool[0][4])

    def test_S2_http_500_on_a_status_is_failed_open_without_the_token_in_the_note(self):
        self.api.fail_statuses = True
        self.run_both()
        self.assertEqual([row[:4] for row in self.spool], [("land_status", SLUG, 12, "merge")] * 2)
        self.assertEqual(self.spool[0][4], "before: POST HTTP 500")
        self.assertNotIn(TOKEN, json.dumps(self.spool))
        self.assertEqual([r[0] for r in self.api.requests].count("DELETE"), 2, "revoked even though a status failed")

    def test_S2_a_missing_secret_directory_and_a_missing_key_are_failed_open(self):
        os.environ["CW_SECRET_DIR"] = str(self.tmp / "absent")
        self.run_both()
        self.assertEqual(self.api.requests, [], "no secret directory and an API base: configured by the base, minting has nothing to read")
        self.assertTrue(self.spool[0][4].startswith("before: app credentials unreadable"), self.spool)

    def test_S2_a_response_without_a_merge_commit_is_failed_open(self):
        self.set_gh(None)
        self.run_both()
        self.assertEqual(len(self.api.statuses()), 3)
        self.assertEqual(self.spool, [("land_status", SLUG, 12, "merge", "after: no mergeCommit.oid")])

    def test_S2_an_unreachable_api_is_failed_open_with_the_exception_class_only(self):
        self.api.close()
        os.environ["FLEET_LAND_STATUS_TOKEN"] = TOKEN
        load().dual_write_before(SLUG, 12, HEAD, self.spool_fn, {SLUG})
        self.assertEqual(len(self.spool), 1)
        self.assertNotIn(TOKEN, json.dumps(self.spool))
        self.assertNotIn("127.0.0.1", json.dumps(self.spool))

    def test_S2_a_non_local_api_base_is_ignored(self):
        os.environ["FLEET_LAND_STATUS_API_BASE"] = "http://evil.example"
        self.assertEqual(load().api_base(), "https://api.github.com")
        os.environ["FLEET_LAND_STATUS_API_BASE"] = "http://127.0.0.1.evil.example"
        self.assertEqual(load().api_base(), "https://api.github.com")

    def test_M1_the_post_merge_write_is_dropped_and_S1_sees_it(self):
        self.run_both()
        self.assertEqual(len(self.api.statuses()), 6, "control")
        self.api.requests.clear()
        source = MODULE.read_text()
        self.assertEqual(source.count("write_all(repo, oid,"), 1)
        self.run_both(load(source.replace("write_all(repo, oid,", "(lambda *a: None)(repo, oid,")))
        self.assertEqual(len(self.api.statuses()), 3, "the mutant writes only the head")

    def test_M4_the_shim_hook_runs_before_and_after_the_real_call(self):
        source = GH_SHIM.read_text()
        body = source[source.index("status = status_plan("):]
        order = [body.index(w) for w in ("dual_write_before(", "subprocess.run([gh] + args, env=env).returncode", "dual_write_after(")]
        self.assertEqual(order, sorted(order))
        self.assertIn("if status and code == 0:", body)

    def test_the_hook_does_not_change_the_guard_pins(self):
        source = GH_SHIM.read_text()
        self.assertEqual(len(re.findall(r"\bsb_argv\(", source)), 7, "6 calls and the def")   # the def plus the 6 calls: unchanged by this slice
        import ast
        named = [n.name for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
                 and ("'merge'" in ast.get_source_segment(source, n) or '"merge"' in ast.get_source_segment(source, n))]
        self.assertEqual(len(named), 8)


class TestLandStatusParity(Case):
    def commit(self, sha, contexts=CONTEXTS, state="success", login="fleet-app[bot]"):
        return {"sha": sha, "statuses": [{"context": c, "state": state, "creator": {"login": login}} for c in contexts]}

    def run_parity(self, commits, *extra, parity=PARITY):
        path = self.tmp / "fixture.json"
        path.write_text(json.dumps(commits))
        return subprocess.run([sys.executable, str(parity), "--fixture", str(path)] + list(extra), capture_output=True, text=True)

    def test_S3_a_complete_bot_fixture_is_parity_ok(self):
        done = self.run_parity([self.commit("c" * 40), self.commit("d" * 40)])
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertIn("OK cccccccccccc: land/gates, land/reviews, land/merge-gate by fleet-app[bot]", done.stdout)
        self.assertIn("PARITY OK (2 commits checked)", done.stdout)

    def test_S3_each_broken_fixture_names_the_sha_and_the_context(self):
        cases = ((self.commit("e" * 40, CONTEXTS[:2]), "MISSING eeeeeeeeeeee: land/merge-gate (no such status)"),
                 (self.commit("f" * 40, state="failure"), "MISSING ffffffffffff: land/gates (state is failure, not success)"),
                 (self.commit("1" * 40, login="malicious-user"), "MISSING 111111111111: land/reviews (creator malicious-user is not the bot)"))
        for commit, line in cases:
            with self.subTest(line=line):
                done = self.run_parity([self.commit("c" * 40), commit])
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn(line, done.stdout)
                self.assertNotIn("PARITY OK", done.stdout)

    def test_S3_the_named_bot_must_match_exactly(self):
        self.assertEqual(self.run_parity([self.commit("c" * 40)], "--bot", "fleet-app[bot]").returncode, 0)
        self.assertEqual(self.run_parity([self.commit("c" * 40)], "--bot", "other[bot]").returncode, 1)

    def test_M3_a_checker_that_ignores_the_creator_passes_the_forged_fixture(self):
        forged = [self.commit("1" * 40, login="malicious-user")]
        self.assertEqual(self.run_parity(forged).returncode, 1, "control")
        mutant = self.tmp / "parity-mutant"
        source = PARITY.read_text()
        old = "return login == bot if bot else isinstance(login, str) and login.endswith(\"[bot]\")"
        self.assertEqual(source.count(old), 1)
        mutant.write_text(source.replace(old, "return True"))
        self.assertEqual(self.run_parity(forged, parity=mutant).returncode, 0, "the mutant accepts a forged creator: S3 sees it")


class TestLandStatusRoles(unittest.TestCase):
    def test_U1_no_pod_role_gets_statuses(self):
        import ast
        launcher = REPO / "cloud" / "launcher"
        tree = ast.parse((launcher / "app_token.py").read_text())
        table = next(n.value for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "PERMISSIONS_BY_ROLE" for t in n.targets))
        roles = ast.literal_eval(table)
        for role in ("worker", "orchestrator"):
            with self.subTest(role=role):
                self.assertIn(role, roles)
                self.assertNotIn("statuses", roles[role])
        service = (launcher / "token_service.py").read_text()
        self.assertNotIn("statuses", service, "token_service.py (which reads the table from app_token) names no statuses permission")
        self.assertNotIn("statuses", ast.get_source_segment(tree and (launcher / "app_token.py").read_text(), table))


if __name__ == "__main__":
    unittest.main()
