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
  M4 the shim hook never calls `before`        -> the behavioural run of shims/gh sees 3 POSTs, not 6
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from support.core import Base

REPO = Path(__file__).resolve().parents[2]
MODULE = REPO / "shims" / "land_status.py"
GH_SHIM = REPO / "shims" / "gh"
PARITY = REPO / "tools" / "land-status-parity"
TOKEN = "ghs_PLANTEDTOKEN0123456789abcdefghijkl"
HEAD, MERGED = "a" * 40, "b" * 40
SLUG = "acme/app"
CONTEXTS = ("land/gates", "land/reviews", "land/merge-gate")


def shim_module(source=None):
    module = types.ModuleType("gh_shim_land_status_under_test")
    module.__file__ = str(GH_SHIM)
    exec(compile(source if source is not None else GH_SHIM.read_text(), str(GH_SHIM), "exec"), module.__dict__)
    return module


def load(source=None, **attrs):
    module = types.ModuleType("land_status_under_test")
    exec(compile(source if source is not None else MODULE.read_text(), str(MODULE), "exec"), module.__dict__)
    for name, value in attrs.items():
        setattr(module, name, value)
    return module


class FakeGitHub:
    def __init__(self):
        self.requests, self.fail_statuses, self.permissions = [], False, {"statuses": "write"}
        self.revoked, self.redirect, self.repositories, self.installation = set(), False, [{"name": "app"}], 7
        outer = self
        self.token, self.minted = None, 0   # None: every mint answers a fresh token

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                outer.requests.append((self.command, self.path, body, self.headers.get("Authorization")))
                code, answer = 404, {}
                used = (self.headers.get("Authorization") or "").split(" ", 1)[-1]
                if used in outer.revoked:
                    code, answer = 401, {"message": "Bad credentials"}
                elif outer.redirect:
                    self.send_response(302)
                    self.send_header("Location", "http://evil.example/x")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                elif self.command == "GET" and self.path.endswith("/installation"):
                    code, answer = 200, {"id": outer.installation}
                elif self.command == "POST" and self.path == "/app/installations/7/access_tokens":
                    outer.minted += 1
                    code, answer = 201, {"token": outer.token if outer.token is not None else "%s-%d" % (TOKEN, outer.minted), "permissions": outer.permissions, "repositories": outer.repositories}
                elif self.command == "POST" and "/statuses/" in self.path:
                    code, answer = (500, {"message": "boom " + TOKEN}) if outer.fail_statuses else (201, {})
                elif self.command == "DELETE" and self.path == "/installation/token":
                    outer.revoked.add(used)
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


class LandStatusCase(Base):
    def setUp(self):
        super().setUp()
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
        self.secrets = secrets
        self.spool = []
        self.fake_gh = self.tmp / "gh"
        self.set_gh(MERGED)

    def load(self, source=None, **attrs):
        """The module under test, configured through its attributes (never the environment)."""
        settings = {"SECRET_DIR": str(self.secrets), "API_BASE": self.api.base}
        settings.update(attrs)
        return load(source, **settings)

    def set_gh(self, oid):
        self.fake_gh.write_text("#!/bin/sh\n%s\n" % ('echo %s' % oid if oid else "exit 1"))
        self.fake_gh.chmod(0o755)

    def spool_fn(self, *row):
        self.spool.append(row)

    def run_both(self, module=None):
        module = module or self.load()
        listed = {SLUG}
        module.dual_write_before(SLUG, 12, HEAD, self.spool_fn, listed)
        module.dual_write_after(SLUG, 12, HEAD, str(self.fake_gh), {}, self.spool_fn, listed)


class TestLandStatusShim(LandStatusCase):
    def test_S1_three_contexts_on_the_head_then_three_on_the_merge_commit_and_the_token_is_revoked(self):
        self.run_both()
        self.assertEqual(self.api.statuses(), [(HEAD, c, "success") for c in CONTEXTS] + [(MERGED, c, "success") for c in CONTEXTS])
        revokes = [r for r in self.api.requests if r[0] == "DELETE"]
        self.assertEqual([r[1] for r in revokes], ["/installation/token"] * 2)
        self.assertEqual(self.spool, [])
        mint = [r for r in self.api.requests if r[1] == "/app/installations/7/access_tokens"][0]
        self.assertEqual(mint[2], {"repositories": ["app"], "permissions": {"statuses": "write"}})
        self.assertTrue(all(r[3].startswith("token " + TOKEN) for r in self.api.requests if "/statuses/" in r[1]))

    def test_S1_an_unlisted_repository_writes_nothing(self):
        self.load().dual_write_before("acme/other", 1, HEAD, self.spool_fn, {SLUG})
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
        self.run_both(self.load(SECRET_DIR=str(self.tmp / "absent")))
        self.assertEqual(self.api.requests, [], "no secret directory and an API base: configured by the base, minting has nothing to read")
        self.assertTrue(self.spool[0][4].startswith("before: app credentials unreadable"), self.spool)

    def test_S2_a_response_without_a_merge_commit_is_failed_open(self):
        self.set_gh(None)
        self.run_both()
        self.assertEqual(len(self.api.statuses()), 3)
        self.assertEqual(self.spool, [("land_status", SLUG, 12, "merge", "after: no mergeCommit.oid")])

    def test_S2_an_unreachable_api_is_failed_open_with_the_exception_class_only(self):
        self.api.close()
        self.load(TOKEN=TOKEN).dual_write_before(SLUG, 12, HEAD, self.spool_fn, {SLUG})
        self.assertEqual(len(self.spool), 1)
        self.assertNotIn(TOKEN, json.dumps(self.spool))
        self.assertNotIn("127.0.0.1", json.dumps(self.spool))

    def test_S2_a_non_local_api_base_is_ignored(self):
        for base in ("http://evil.example", "http://127.0.0.1.evil.example", "http://127.0.0.1:@evil.example", "https://127.0.0.1", "http://u:p@127.0.0.1"):
            with self.subTest(base=base):
                self.assertEqual(load(API_BASE=base).api_base(), "https://api.github.com")
        self.assertEqual(load(API_BASE="http://localhost:99").api_base(), "http://localhost:99")

    def test_M1_the_post_merge_write_is_dropped_and_S1_sees_it(self):
        self.run_both()
        self.assertEqual(len(self.api.statuses()), 6, "control")
        self.api.requests.clear()
        source = MODULE.read_text()
        self.assertEqual(source.count("write_all(repo, oid,"), 1)
        self.run_both(self.load(source.replace("write_all(repo, oid,", "(lambda *a: None)(repo, oid,")))
        self.assertEqual(len(self.api.statuses()), 3, "the mutant writes only the head")

    # ---- unit tests of the module's own checks
    def test_a_bad_repository_is_refused_before_any_request(self):
        mod = self.load()
        for bad in ("owner/..", "../repo", "owner/a/b", "", ".hidden/repo", "owner/repo.git", "owner/repo.", None, 5):
            with self.subTest(repo=bad):
                self.assertFalse(mod.valid_repo(bad))
                with self.assertRaises(mod.StatusError):
                    mod.mint(bad)
                with self.assertRaises(mod.StatusError):
                    mod.post_statuses(bad, HEAD, TOKEN)
        self.assertEqual(self.api.requests, [])
        self.assertTrue(mod.valid_repo("acme/app-1.x"))

    def test_a_redirect_is_refused_and_the_token_goes_nowhere_else(self):
        self.api.redirect = True
        mod = self.load(TOKEN=TOKEN)
        mod.dual_write_before(SLUG, 12, HEAD, self.spool_fn, {SLUG})
        self.assertEqual(self.spool[0][4], "before: POST HTTP 302")
        self.assertEqual(len(self.api.statuses()), 1, "one attempt, never retried elsewhere")
        self.assertEqual([r for r in self.api.requests if r[1].startswith("http")], [])

    def test_signing_failures_are_failed_open(self):
        for name, body in (("fails", "#!/bin/sh\nexit 1\n"), ("silent", "#!/bin/sh\nexit 0\n")):
            with self.subTest(openssl=name):
                fake = self.tmp / ("openssl-" + name)
                fake.write_text(body)
                fake.chmod(0o755)
                mod = self.load(OPENSSL=str(fake))
                with self.assertRaises(mod.StatusError):
                    mod.sign_jwt("123", "key")
        mod = self.load(OPENSSL=str(self.tmp / "absent"))
        with self.assertRaises(mod.StatusError):
            mod.sign_jwt("123", "key")
        self.assertTrue(self.load().sign_jwt("123", (self.secrets / "cw-app-key").read_text()).count(".") == 2, "control: the real openssl signs")

    def test_openssl_runs_with_an_empty_environment_from_a_fixed_path(self):
        seen = {}
        def fake_run(argv, **kwargs):
            seen.update(argv=argv, env=kwargs.get("env"))
            return types.SimpleNamespace(returncode=0, stdout=b"sig")
        mod = self.load()
        with mock.patch.object(mod.subprocess, "run", fake_run):
            mod.sign_jwt("123", "key")
        self.assertEqual(seen["env"], {})
        self.assertIn(seen["argv"][0], mod.OPENSSL_CANDIDATES)

    def test_mint_refuses_a_bad_installation_token_or_repository_list(self):
        cases = (("installation", True), ("installation", "7"), ("installation", 0), ("token", ""), ("repositories", []),
                 ("repositories", [{"name": "other"}]), ("repositories", [{"name": "app"}, {"name": "x"}]), ("repositories", None))
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.api.requests.clear()
                self.api.token, self.api.repositories, self.api.installation = TOKEN, [{"name": "App"}], 7
                if field == "installation":
                    self.api.installation = value
                elif field == "token":
                    self.api.token = value
                else:
                    self.api.repositories = value
                mod = self.load()
                with self.assertRaises(mod.StatusError):
                    mod.mint(SLUG)
                if field == "repositories":
                    self.assertIn(TOKEN, self.api.revoked, "a token that was minted and refused is revoked")
        self.api.token, self.api.repositories, self.api.installation = TOKEN, [{"name": "App"}], 7
        self.assertEqual(self.load().mint(SLUG), TOKEN, "control: the repository name matches case-insensitively")

    def test_each_phase_mints_and_revokes_its_own_token_and_a_supplied_token_is_never_revoked(self):
        self.run_both()
        mints = [r for r in self.api.requests if r[1] == "/app/installations/7/access_tokens"]
        self.assertEqual(len(mints), 2)
        self.assertEqual(len([r for r in self.api.requests if r[0] == "DELETE"]), 2)
        self.assertEqual(self.spool, [], "a token revoked after phase one would answer 401 in phase two")
        self.api.requests.clear()
        self.api.revoked.clear()
        self.load(TOKEN=TOKEN).dual_write_before(SLUG, 12, HEAD, self.spool_fn, {SLUG})
        self.assertEqual(len(self.api.statuses()), 3)
        self.assertEqual([r for r in self.api.requests if r[0] == "DELETE"], [])
        self.assertEqual(self.api.revoked, set())

    def test_the_secret_directory_comes_from_the_hosts_config_file(self):
        home = self.tmp / "home"
        (home / ".config" / "fleet-switchboard").mkdir(parents=True)
        (home / ".config" / "fleet-switchboard" / "cloud.env").write_text("OTHER=1\nCW_SECRET_DIR='%s'\n" % self.secrets)
        self.assertEqual(load(HOME_DIR=str(home)).secret_dir(), str(self.secrets))
        (home / ".config" / "fleet-switchboard" / "cloud.env").write_text("CW_SECRET_DIR=/x\n" + "#" * 70000)
        self.assertIsNone(load(HOME_DIR=str(home)).secret_dir(), "oversize: refused")
        self.assertIsNone(load(HOME_DIR=str(self.tmp / "nohome")).secret_dir())

    def test_the_environment_moves_nothing(self):
        with mock.patch.dict(os.environ, {"FLEET_LAND_STATUS_API_BASE": self.api.base, "FLEET_LAND_STATUS_TOKEN": TOKEN, "CW_SECRET_DIR": str(self.secrets)}):
            mod = load(HOME_DIR=str(self.tmp / "nohome"))
            self.assertEqual(mod.api_base(), "https://api.github.com")
            self.assertIsNone(mod.secret_dir())
            self.assertFalse(mod.is_configured())

    def test_oversize_credentials_and_responses_are_refused(self):
        (self.secrets / "cw-app-key").write_text("k" * 70000)
        mod = self.load()
        with self.assertRaises(mod.StatusError) as caught:
            mod.mint(SLUG)
        self.assertEqual(str(caught.exception), "app credentials oversize")
        mod = self.load(MAX_RESPONSE_BYTES=1)
        with self.assertRaises(mod.StatusError) as caught:
            mod.call("GET", "/repos/x/installation", "t", expect=(200,))
        self.assertEqual(str(caught.exception), "GET response oversize")

    # ---- the hook in shims/gh, run for real
    def run_shim(self, code=0, bound=True, app_dirs=False, source=None, args=None, header_head=True):
        calls = self.tmp / "calls"
        calls.write_text("")
        self.fake_gh.write_text("#!/bin/sh\necho \"$*\" >> %s\ncase \"$1 $2\" in 'pr view') echo %s;; *) exit %d;; esac\n" % (calls, MERGED, code))
        self.fake_gh.chmod(0o755)
        shim = shim_module(source)
        shim.STATUS_MODULE[:] = [self.load(), True]
        given = args or ["pr", "merge", "12", "-R", SLUG, "--squash"]
        def gate(a, lookup, gh=None):
            if bound:
                shim.GATE_BOUND[:] = [(SLUG, 12, HEAD)]
            return ("args", list(a) + ["--match-head-commit", HEAD])
        patches = dict(real_gh=lambda: str(self.fake_gh), warn_shared_body_file=lambda a: None, hold_probe=lambda *a: (None, None, None),
                       body_probe=lambda *a: None, sign_plan=lambda *a: None, review_probe=lambda *a: None, gate_probe=gate,
                       merge_gate_probe=lambda *a: None, account_env=lambda *a, **k: None, audit_write=lambda *a, **k: None,
                       warn_text=lambda a: None, count_call=lambda a: None, read_scope=lambda *a: None,
                       merge_gate_repos=lambda: [SLUG], gate_repos=lambda: [SLUG], cache_drop=lambda a: None, budget_low=lambda a: None,
                       low_budget_read=lambda a: None, gate_spool=self.spool_fn)
        shim.APP_DIRS[:] = ["/nonexistent"] if app_dirs else []
        class Exec(Exception):
            pass
        def fake_exec(path, argv, *rest):
            subprocess.run(argv, check=False)   # what an exec'd gh would do: run, and nothing of ours runs after it
            raise Exec()
        with mock.patch.multiple(shim, **patches), mock.patch.object(shim.os, "execv", lambda p, a: fake_exec(p, a)), \
                mock.patch.object(shim.os, "execve", lambda p, a, e: fake_exec(p, a)):
            try:
                result = shim.run(list(given))
            except Exec:
                result = 0
        self.spool_seen = list(self.spool)
        return shim, result, [line for line in calls.read_text().splitlines()]

    def test_M4_the_shim_writes_before_and_after_a_merge_that_exits_zero(self):
        shim, code, calls = self.run_shim(0)
        self.assertEqual(code, 0, self.spool)
        self.assertEqual(self.api.statuses(), [(HEAD, c, "success") for c in CONTEXTS] + [(MERGED, c, "success") for c in CONTEXTS])
        self.assertEqual(calls[0], "pr merge 12 -R %s --squash --match-head-commit %s" % (SLUG, HEAD))
        self.assertEqual(calls[1].split()[:3], ["pr", "view", "12"])
        self.assertEqual(self.spool, [])

    def test_M4_a_shim_that_drops_the_before_write_is_seen(self):
        source = GH_SHIM.read_text()
        old = "status[0].dual_write_before(status[1], status[2], status[3], gate_spool, status[4])"
        self.assertEqual(source.count(old), 1)
        self.assertEqual(len(self.run_shim(0)[0].GATE_BOUND), 1)
        self.assertEqual(len(self.api.statuses()), 6, "control")
        self.api.requests.clear()
        self.run_shim(0, source=source.replace(old, "pass"))
        self.assertEqual(len(self.api.statuses()), 3, "the mutant writes only after the merge")

    def test_a_failed_merge_writes_before_but_not_after(self):
        shim, code, calls = self.run_shim(1)
        self.assertEqual(code, 1)
        self.assertEqual(self.api.statuses(), [(HEAD, c, "success") for c in CONTEXTS])
        self.assertEqual([c for c in calls if c.startswith("pr view")], [])

    def test_without_a_gate_verified_head_no_status_is_written(self):
        shim, code, calls = self.run_shim(0, bound=False, args=["pr", "merge", "12", "-R", SLUG, "--squash", "--match-head-commit", HEAD])
        self.assertEqual(code, 0)
        self.assertEqual(self.api.requests, [])
        self.assertIsNone(shim.status_plan(["pr", "merge", "12", "-R", SLUG], None))
        for args in (["pr", "view", "12", "-R", SLUG], ["issue", "close", "3", "-R", SLUG]):
            self.assertIsNone(shim.status_plan(args, None))
        shim.GATE_BOUND[:] = [(SLUG, 12, HEAD)]
        with mock.patch.multiple(shim, merge_gate_repos=lambda: [SLUG], gate_repos=lambda: [SLUG]):
            self.assertIsNotNone(shim.status_plan(["pr", "merge", "12", "-R", SLUG], None), "control: a bound head is planned")
        shim.GATE_BOUND[:] = [("../x/y", 12, HEAD)]
        self.assertIsNone(shim.status_plan(["pr", "merge", "12"], None))
        shim.GATE_BOUND[:] = [(SLUG, 12, "short")]
        self.assertIsNone(shim.status_plan(["pr", "merge", "12"], None))

    def test_the_child_gh_is_tracked_when_a_token_directory_exists(self):
        children = []
        real_popen = subprocess.Popen
        def spy(*a, **k):
            child = real_popen(*a, **k)
            if str(a[0][0]) == str(self.fake_gh):
                children.append(child)
            return child
        with mock.patch("subprocess.Popen", spy):
            shim_, code, calls = self.run_shim(0, app_dirs=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.api.statuses()), 6)
        self.assertTrue(children, "gh ran as a tracked child (Popen), not in place of the shim")
        self.assertGreaterEqual(len(children), 1)
        self.assertEqual(len(shim_.APP_CHILD), 1)


class TestLandStatusParity(LandStatusCase):
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

    def fixture_run(self, text, *extra):
        path = self.tmp / "raw.json"
        path.write_text(text)
        return subprocess.run([sys.executable, str(PARITY), "--fixture", str(path)] + list(extra), capture_output=True, text=True)

    def stale_commit(self, sha, latest_state="failure", latest_login="fleet-app[bot]"):
        statuses = []
        for context in CONTEXTS:   # newest first, as GitHub lists them
            statuses.append({"context": context, "state": latest_state, "creator": {"login": latest_login}})
            statuses.append({"context": context, "state": "success", "creator": {"login": "fleet-app[bot]"}})
        return {"sha": sha, "statuses": statuses}

    def test_only_the_latest_status_of_a_context_counts(self):
        for state, login, line in (("failure", "fleet-app[bot]", "state is failure, not success"), ("success", "malicious-user", "creator malicious-user is not the bot")):
            with self.subTest(state=state, login=login):
                done = self.run_parity([self.commit("c" * 40), self.stale_commit("d" * 40, state, login)])
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn(line, done.stdout)
        done = self.run_parity([self.stale_commit("d" * 40, "success", "fleet-app[bot]")])
        self.assertEqual(done.returncode, 0, "control: a newest status that is the bot's success passes")

    def test_a_hostile_repo_or_count_is_refused_before_gh_runs(self):
        marker = self.tmp / "gh-ran"
        fake = self.tmp / "bin"
        fake.mkdir()
        (fake / "gh").write_text("#!/bin/sh\ntouch %s\nexit 1\n" % marker)
        (fake / "gh").chmod(0o755)
        env = dict(os.environ, PATH="%s:%s" % (fake, os.environ.get("PATH", "")))
        for args in (["--repo", "owner/.."], ["--repo", "../repo"], ["--repo", "a/b/c"], ["--repo", "owner/repo?x=1"], ["--repo", "o/r", "--count", "0"],
                     ["--repo", "o/r", "--count", "500"], ["--repo", "o/r", "--count", "-1"]):
            with self.subTest(args=args):
                done = subprocess.run([sys.executable, str(PARITY)] + args, capture_output=True, text=True, env=env)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertFalse(marker.exists())

    def test_a_malformed_fixture_is_exit_two(self):
        for text in ("", "not json", "{}", "[]", "[1]", '["x"]', "[null]"):
            with self.subTest(text=text):
                done = self.fixture_run(text)
                self.assertEqual(done.returncode, 2, done.stdout)
        self.assertEqual(self.fixture_run(json.dumps([self.commit("c" * 40)])).returncode, 0, "control")
        self.assertEqual(self.fixture_run(json.dumps([self.commit("c" * 40)] * 1001)).returncode, 2, "over 1000 commits")

    def test_from_api_reads_commits_and_statuses_through_gh(self):
        fake = self.tmp / "bin"
        fake.mkdir()
        good = json.dumps({"statuses": [{"context": c, "state": "success", "creator": {"login": "fleet-app[bot]"}} for c in CONTEXTS]})
        (fake / "gh").write_text("#!/bin/sh\ncase \"$2\" in\n*/commits?per_page=*) echo '[{\"sha\": \"%s\"}, {\"sha\": \"%s\"}]';;\n"
                                 "*/status) echo '%s';;\n*) exit 1;;\nesac\n" % ("c" * 40, "d" * 40, good))
        (fake / "gh").chmod(0o755)
        env = dict(os.environ, PATH="%s:%s" % (fake, os.environ.get("PATH", "")))
        done = subprocess.run([sys.executable, str(PARITY), "--repo", SLUG, "--count", "2"], capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("PARITY OK (2 commits checked)", done.stdout)
        (fake / "gh").write_text("#!/bin/sh\ncase \"$2\" in\n*/commits?per_page=*) echo '[{\"sha\": \"../x\"}]';;\n*) exit 1;;\nesac\n")
        done = subprocess.run([sys.executable, str(PARITY), "--repo", SLUG], capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 2, "a sha that is not 40 hex is never put in a path")

    def test_a_hostile_login_is_printed_without_control_characters(self):
        hostile = "bad\x1b[31muser"
        done = self.run_parity([self.commit("c" * 40, login=hostile)])
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("\x1b", done.stdout + done.stderr)
        self.assertIn("creator bad?[31muser is not the bot", done.stdout)


class TestLandStatusRoles(Base):
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
