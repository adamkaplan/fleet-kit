"""Offline tests for cloud/launcher/app_token.py: a loopback stub for the GitHub API, a throwaway RSA key, real openssl."""
import base64
import datetime as dt
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MINT = os.path.join(os.path.dirname(HERE), "app_token.py")
TOKEN = "ghs_synthetic" + "Ab3Xy9" * 6
REAL = "acme-org/widget-service"
PERMS = {"contents": "write", "pull_requests": "write", "issues": "read", "metadata": "read"}
PLANTED = "planted-secret-value-7Qz"


def iso(seconds):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Stub(http.server.BaseHTTPRequestHandler):
    answer = {}
    redirect_code = 307
    redirect = None  # a Location to answer with as a 307 (it would re-send the POST and its Authorization)
    pad_to = None  # pad the JSON answer with trailing spaces to exactly this many bytes
    seen = []
    gets = []  # the lookups: (path, Authorization)
    installs = {}  # the fake's installations: account login (lowercase) to id
    retry_after = None
    raw = None  # a raw lookup body
    deletes = []  # the revocations: (path, Authorization)
    delete_status = 204
    delete_redirect = None
    lookup = None  # a (status, JSON body) for every lookup, instead of the account table

    def log_message(self, *a):
        pass

    def do_GET(self):
        Stub.gets.append((self.path, self.headers.get("Authorization", "")))
        if Stub.lookup is not None:
            status, body = Stub.lookup
        else:
            parts = self.path.split("/")  # /repos/OWNER/REPO/installation
            inst = Stub.installs.get(parts[2].lower()) if len(parts) == 5 and parts[1] == "repos" else None
            status, body = (200, {"id": inst, "account": {"id": 9, "login": parts[2]}, "repository_selection": "selected", "suspended_at": None}) if inst else (404, {"message": "Not Found"})
        out = Stub.raw if Stub.raw is not None else json.dumps(body).encode()
        self.send_response(status)
        if Stub.retry_after:
            self.send_header("Retry-After", Stub.retry_after)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_DELETE(self):
        Stub.deletes.append((self.path, self.headers.get("Authorization", "")))
        self.send_response(307 if Stub.delete_redirect else Stub.delete_status)
        if Stub.delete_redirect:
            self.send_header("Location", Stub.delete_redirect)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        Stub.seen.append((self.path, self.headers.get("Authorization", ""), json.loads(body)))
        if Stub.redirect:
            self.send_response(Stub.redirect_code)
            self.send_header("Location", Stub.redirect)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        out = json.dumps(Stub.answer).encode()
        if Stub.pad_to:
            out = out + b" " * (Stub.pad_to - len(out))
        self.send_response(201)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class Other(http.server.BaseHTTPRequestHandler):
    seen = []  # any request that reached the second port

    def log_message(self, *a):
        pass

    def do_POST(self):
        Other.seen.append((self.path, self.headers.get("Authorization")))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    do_GET = do_POST


# The recursion limit of json's C scanner differs by platform and Python build, so a deep answer may parse or raise.
# This wrapper makes the raise certain: json.loads of a nested array raises the class named in $RAISE_CLASS.
RAISE_ON_NESTING = ("import json, runpy, sys\n"
                    "real = json.loads\n"
                    "def loads(s, *a, **k):\n"
                    "    head = s[:2] if isinstance(s, str) else bytes(s[:2]).decode('latin-1')\n"
                    "    if head == '[[':\n"
                    "        raise {'RecursionError': RecursionError('maximum recursion depth exceeded'), 'ValueError': ValueError('bad'),\n"
                    "               'UnicodeDecodeError': UnicodeDecodeError('utf-8', b'x', 0, 1, 'bad'), 'MemoryError': MemoryError()}[__import__('os').environ['RAISE_CLASS']]\n"
                    "    return real(s, *a, **k)\n"
                    "json.loads = loads\n"
                    "path = sys.argv[1]\n"
                    "sys.path.insert(0, __import__('os').path.dirname(path))\n"
                    "sys.argv = sys.argv[1:]\n"
                    "runpy.run_path(path, run_name='__main__')\n")


class Mint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        d = cls.tmp.name
        cls.keyfile = os.path.join(d, "k.pem")  # a test fixture only; the minter is given a command that prints it
        subprocess.run(["openssl", "genrsa", "-out", cls.keyfile, "2048"], check=True, capture_output=True)
        subprocess.run(["openssl", "rsa", "-in", cls.keyfile, "-pubout", "-out", os.path.join(d, "pub.pem")], check=True, capture_output=True)
        with open(cls.keyfile) as f:
            cls.key = f.read()
        cls.keymark = [l for l in cls.key.splitlines() if l and "-----" not in l][1]
        cls.argv_log = os.path.join(d, "openssl.log")
        wrap = os.path.join(d, "openssl-spy")  # logs argv and environment, then runs the real openssl
        with open(wrap, "w") as f:
            f.write('#!/bin/sh\n{ echo "ARGV $*"; env; } >> "%s"\nexec openssl "$@"\n' % cls.argv_log)
        os.chmod(wrap, 0o700)
        cls.wrap = wrap
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_port
        cls.other = http.server.HTTPServer(("127.0.0.1", 0), Other)
        threading.Thread(target=cls.other.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.other.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        Stub.seen = []
        Stub.gets = []
        Stub.deletes = []
        Stub.delete_status = 204
        Stub.delete_redirect = None
        Stub.installs = {"acme-org": 678, "second-acct": 222}
        Stub.lookup = None
        Stub.raw = None
        Stub.pad_to = None
        Stub.redirect = None
        Other.seen = []
        Stub.answer = {"token": TOKEN, "expires_at": iso(3500), "repositories": [{"name": "widget-service", "full_name": REAL, "owner": {"id": 9}}],
                       "permissions": dict(PERMS)}
        if os.path.exists(self.argv_log):
            os.remove(self.argv_log)

    def run_mint(self, repo=REAL, runner=None, **env):
        e = dict(os.environ, CW_APP_ID_CMD="echo 12345", CW_APP_INSTALLATION_CMD="echo 678",
                 CW_APP_KEY_CMD="cat '%s'" % self.keyfile, CW_GITHUB_API_BASE=self.base, CW_OPENSSL=self.wrap)
        e.update(env)
        argv = [sys.executable, "-c", runner, MINT, repo] if runner else [sys.executable, MINT, repo]
        return subprocess.run(argv, env=e, capture_output=True, text=True, timeout=60)

    def test_prints_only_the_token_and_signs_a_valid_short_jwt(self):
        p = self.run_mint()
        self.assertEqual((p.returncode, p.stdout), (0, TOKEN), p.stderr)
        path, auth, body = Stub.seen[0]
        self.assertEqual((path, body), ("/app/installations/678/access_tokens", {"repositories": ["widget-service"], "permissions": PERMS}))
        head, claims, sig = auth.split()[1].split(".")
        pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        c = json.loads(pad(claims))
        self.assertEqual(c["iss"], "12345")
        self.assertLessEqual(c["exp"] - c["iat"], 9 * 60 + 30)
        sigfile = os.path.join(self.tmp.name, "sig")
        with open(sigfile, "wb") as f:
            f.write(pad(sig))
        v = subprocess.run(["openssl", "dgst", "-sha256", "-verify", os.path.join(self.tmp.name, "pub.pem"), "-signature", sigfile],
                           input=(head + "." + claims).encode(), capture_output=True)
        self.assertEqual(v.returncode, 0)

    def test_key_and_token_never_in_argv_env_files_or_stderr(self):
        p = self.run_mint()
        self.assertEqual(p.returncode, 0)
        with open(self.argv_log) as f:
            spy = f.read()
        self.assertIn("ARGV dgst -sha256 -sign /dev/fd/", spy)
        for secret in (self.keymark, TOKEN):
            self.assertNotIn(secret, spy)  # openssl's argv and environment
            self.assertNotIn(secret, p.stderr)
        for n in os.listdir(self.tmp.name):  # no new file holds the token or the key (the fixture key file is the test's own)
            fp = os.path.join(self.tmp.name, n)
            if os.path.isfile(fp) and n != "k.pem":
                with open(fp, errors="replace") as f:
                    body = f.read()
                self.assertNotIn(TOKEN, body)
                self.assertNotIn(self.keymark, body)

    def test_refuses_another_or_extra_repository(self):
        for repos in ([{"name": "other-service", "full_name": "acme-org/other-service"}],
                      [{"name": "widget-service"}, {"name": "other-service"}], [], [{"name": "widget-service", "full_name": "evil-org/widget-service"}]):
            Stub.answer["repositories"] = repos
            p = self.run_mint()
            self.assertNotEqual(p.returncode, 0)
            self.assertEqual(p.stdout, "")
            self.assertNotIn(TOKEN, p.stderr)

    def test_refuses_a_long_lived_or_unreadable_expiry(self):
        for exp in (iso(62 * 60 + 30), iso(86400), "tomorrow", None):
            Stub.answer["expires_at"] = exp
            p = self.run_mint()
            self.assertNotEqual(p.returncode, 0)
            self.assertEqual(p.stdout, "")
            self.assertNotIn(TOKEN, p.stderr)

    def test_refuses_extra_higher_or_missing_permissions(self):
        cases = [dict(PERMS, issues="write"), dict(PERMS, administration="write"), dict(PERMS, contents="admin"),
                 dict(PERMS, metadata="write"), {"contents": "write", "metadata": "read"},
                 {"contents": "write", "pull_requests": "write"}, {}, None, ["contents"]]
        for perms in cases:
            if perms is None:
                del Stub.answer["permissions"]
            else:
                Stub.answer["permissions"] = perms
            p = self.run_mint()
            self.assertNotEqual(p.returncode, 0, perms)
            self.assertEqual(p.stdout, "")
            self.assertNotIn(TOKEN, p.stderr)
            self.assertEqual(len(p.stderr.strip().splitlines()), 1)
        Stub.answer["permissions"] = dict(PERMS, issues="write")
        self.assertIn("issues", self.run_mint().stderr)

    def test_a_planted_secret_in_the_commands_env_appears_nowhere(self):
        for ok in (True, False):  # the refusal path too
            Stub.answer["permissions"] = dict(PERMS) if ok else dict(PERMS, administration="write")
            p = self.run_mint(PLANTED_SECRET=PLANTED, CW_APP_ID_CMD="echo 12345; : $PLANTED_SECRET")
            self.assertEqual(p.stdout, TOKEN if ok else "")
            self.assertNotIn(PLANTED, p.stdout + p.stderr)
            for n in os.listdir(self.tmp.name):
                fp = os.path.join(self.tmp.name, n)
                if os.path.isfile(fp) and n not in ("openssl.log", "k.pem"):  # the spy's own log is the fixture
                    with open(fp, errors="replace") as f:
                        self.assertNotIn(PLANTED, f.read())

    def test_the_answer_size_cap_refuses_one_byte_over_and_accepts_the_cap(self):
        Stub.pad_to = 1_000_000
        p = self.run_mint()
        self.assertEqual((p.returncode, p.stdout), (0, TOKEN), p.stderr)  # exactly at the cap is read
        Stub.pad_to = 1_000_001
        p = self.run_mint()
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")
        self.assertIn("larger than", p.stderr)
        self.assertNotIn(TOKEN, p.stderr)

    def test_the_vault_output_size_cap_refuses_an_oversized_output_and_accepts_a_normal_one(self):
        big = "python3 -c 'print(\"1\" * 1000001)'"
        for var in ("CW_APP_ID_CMD", "CW_APP_KEY_CMD"):  # the installation id is no longer read from the vault
            p = self.run_mint(**{var: big})
            self.assertNotEqual(p.returncode, 0, var)
            self.assertEqual(p.stdout, "")
            self.assertIn("more than", p.stderr)
        self.assertEqual(self.run_mint().stdout, TOKEN)

    def test_non_loopback_base_and_failing_vault_are_refused(self):
        for env in ({"CW_GITHUB_API_BASE": "http://example.com"}, {"CW_GITHUB_API_BASE": "https://127.0.0.1"},
                    {"CW_APP_KEY_CMD": "false"}, {"CW_APP_KEY_CMD": "echo not-a-key"}):
            p = self.run_mint(**env)
            self.assertNotEqual(p.returncode, 0, env)
            self.assertEqual(p.stdout, "")
        self.assertEqual(Stub.seen, [])

    def test_the_vault_default_needs_the_env(self):
        e = {k: v for k, v in os.environ.items() if not k.startswith("CW_")}
        p = subprocess.run([sys.executable, MINT, REAL], env=e, capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn("CW_VAULT_SUBSCRIPTION", p.stderr)

    def test_no_redirect_is_followed_whatever_its_status_or_target(self):
        for code in (301, 302, 307, 308):
            for target in ("http://127.0.0.1:%d/leak" % self.other.server_port,  # another host (port)
                           "http://127.0.0.1:%d/leak" % self.srv.server_port,  # the same host
                           "/leak"):  # relative
                Stub.redirect, Stub.redirect_code, Stub.seen = target, code, []
                p = self.run_mint()
                self.assertNotEqual(p.returncode, 0, (code, target))
                self.assertEqual(p.stdout, "")
                self.assertIn("HTTP %d" % code, p.stderr)
                self.assertIn("redirect", p.stderr)
                self.assertNotIn(TOKEN, p.stderr)
                self.assertEqual(Other.seen, [])
                self.assertEqual(len(Stub.seen), 1, "only the first request was made")

    HOSTILE = ["acme-org/widget-service/", "acme-org/widget-service?x=1", "acme-org/widget-service#f", "acme-org/widget%2Fservice",
               "acme%2Dorg/widget-service", "acme-org/widget service", "acme-org/widget-service\n", "acme-org\n/widget-service",
               "acme-org/..", "acme-org/...", "acme-org/widget.git", "-acme/widget", "acme-/widget", "ac--me/widget", ".acme/widget",
               "ac_me/widget", "ac.me/widget", "a" * 40 + "/widget", "acme/" + "r" * 101, "acme/\u00e9", "\u00e9cme/widget", "acme-org@evil/x",
               "acme-org:80/x", "acme-org\\widget", "", "/", "acme-org//widget", "acme-org/widget/..", "acme-org/a;b", "acme-org/a b",
               "acme-org/widget-service.", "acme-org./widget", "\u212aite/widget", "acme-org/widget-ser\u212aice", "\u0131nc/widget", "\uff41cme/widget",
               "acme/\uff57idget", "\u0130nc/widget", "acme-org/wid\u0131get"]

    def test_valid_target_refuses_each_hostile_shape_and_accepts_the_normal_ones(self):
        sys.path.insert(0, os.path.dirname(MINT))
        import app_token
        for bad in self.HOSTILE:
            self.assertFalse(app_token.valid_target(bad), repr(bad))
        for good in ("acme-org/widget-service", "a/b", "a1-b2/.github", "a" * 39 + "/" + "r" * 100, "Acme/Widget_x.y-z"):
            self.assertTrue(app_token.valid_target(good), good)
        self.assertFalse(app_token.valid_target(None))

    def test_a_hostile_target_is_refused_with_usage_and_no_request(self):
        for bad in self.HOSTILE:
            if bad and "\n" not in bad and bad.isascii():
                self.assertEqual(self.run_mint(bad).returncode, 2, bad)
        self.assertEqual((Stub.seen, Stub.gets), ([], []))

    def test_owner_check_is_by_login_case_insensitive_and_a_renamed_owner_is_refused(self):
        # the stub answers for the owner in the path; a lookup naming the account as it is now (renamed) is not the owner asked for
        Stub.lookup = (200, {"id": 5, "account": {"id": 9, "login": "acme-org-renamed"}})
        p = self.run_mint()
        self.assertEqual((p.returncode, p.stdout, Stub.seen), (1, "", []))
        Stub.lookup = (200, {"id": 5, "account": {"id": 9, "login": "ACME-ORG"}})
        Stub.answer["repositories"][0]["owner"] = {"id": 9}
        self.assertEqual(self.run_mint().stdout, TOKEN)

    def test_a_non_ascii_login_that_folds_to_the_owner_is_refused(self):
        for owner_ok_login in ("\u212a" + "ite-org", "acme-or\u0131", "\uff41cme-org", "acme-org."):
            Stub.lookup, Stub.seen = (200, {"id": 5, "account": {"login": owner_ok_login}}), []
            p = self.run_mint("kite-org/widget-service" if owner_ok_login.startswith("\u212a") else REAL)
            self.assertEqual((p.returncode, p.stdout, Stub.seen), (1, "", []), repr(owner_ok_login))

    def test_a_parse_that_raises_any_exception_class_is_a_named_refusal_with_no_mint(self):
        # the raise is injected (see RAISE_ON_NESTING), so each class is exercised on every platform
        for cls in ("RecursionError", "ValueError", "UnicodeDecodeError", "MemoryError"):
            Stub.lookup, Stub.raw, Stub.seen = (200, None), b"[" * 10 + b"]" * 10, []
            p = self.run_mint(runner=RAISE_ON_NESTING, RAISE_CLASS=cls)
            self.assertEqual((p.returncode, p.stdout, Stub.seen), (75, "", []), cls)
            self.assertIn("nothing was minted", p.stderr, cls)
            self.assertIn("not usable JSON", p.stderr, cls)
            self.assertNotIn("Traceback", p.stderr, cls)

    def test_a_genuinely_hostile_deep_array_is_refused_with_no_mint(self):
        """Whether 10000 levels raise RecursionError or parse depends on the platform and Python build, so both documented
        refusals are accepted: the parse refusal ("not usable JSON") or the wrong-shape refusal. Never a crash, never a mint."""
        Stub.lookup, Stub.raw, Stub.seen = (200, None), b"[" * 10000 + b"]" * 10000, []
        p = self.run_mint()
        self.assertEqual((p.returncode, p.stdout, Stub.seen), (1, "", []))
        self.assertTrue("not usable JSON" in p.stderr or "something other than one installation" in p.stderr, p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_a_nested_answer_that_parses_is_refused_as_the_wrong_shape(self):
        Stub.lookup, Stub.raw, Stub.seen = (200, None), b"[" * 50 + b"]" * 50, []
        p = self.run_mint()  # no wrapper: 50 levels parse on every platform
        self.assertEqual((p.returncode, p.stdout, Stub.seen), (1, "", []))
        self.assertIn("something other than one installation", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_a_request_that_times_out_is_a_named_refusal(self):
        sys.path.insert(0, os.path.dirname(MINT))
        import app_token
        orig = app_token.open_url

        def slow(req, timeout=30):
            raise TimeoutError("timed out")
        app_token.open_url = slow
        try:
            with self.assertRaises(SystemExit) as cm:
                app_token.github_json("GET", "/x", "jwt")
        finally:
            app_token.open_url = orig
        self.assertEqual(cm.exception.code, 1)

    def test_a_rate_limit_says_so_and_when_to_retry(self):
        for hdr, want in (("30", "Retry after 30 seconds"), ("Wed, 21 Oct 2026 07:28:00 GMT", "Retry later"), (None, "Retry later")):
            Stub.lookup, Stub.seen = (429, {"message": "rate limit"}), []
            Stub.retry_after = hdr
            p = self.run_mint()
            Stub.retry_after = None
            self.assertEqual((p.returncode, p.stdout, Stub.seen), (1, "", []))
            for w in ("rate limit", "refused", "nothing was minted", want):
                self.assertIn(w, p.stderr, (hdr, w))
            self.assertNotIn("GMT", p.stderr)

    def test_owner_id_of_the_token_repository_must_match_the_installation_account(self):
        Stub.lookup = (200, {"id": 5, "account": {"id": 9, "login": "acme-org"}})
        Stub.answer["repositories"][0]["owner"] = {"id": 10}
        p = self.run_mint()
        self.assertEqual((p.returncode, p.stdout), (1, ""))
        self.assertIn("not owned by", p.stderr)

    def test_bad_usage(self):
        self.assertEqual(self.run_mint("no-slash").returncode, 2)
        for bad in ("a/..", "../b", ".", "a/b/c", "a/", "/b", "a/./b"):
            self.assertEqual(self.run_mint(bad).returncode, 2, bad)
        self.assertEqual(Stub.seen, [])

    # --- the installation is looked up (the plan's S1 to S8, U3) ---

    def posts(self):
        return [p for p, _, _ in Stub.seen]

    def test_s1_a_repository_of_each_of_two_accounts_mints_for_its_own_installation(self):
        Stub.installs = {"acme-org": 111, "second-acct": 222}
        for repo, inst in ((REAL, 111), ("second-acct/other-service", 222)):
            Stub.seen = []
            Stub.answer["repositories"] = [{"name": repo.split("/")[1], "full_name": repo, "owner": {"id": 9}}]
            p = self.run_mint(repo, CW_APP_INSTALLATION_CMD="echo 111")  # a stored id naming only one of them
            self.assertEqual((p.returncode, p.stdout), (0, TOKEN), p.stderr)
            self.assertEqual(self.posts(), ["/app/installations/%d/access_tokens" % inst])

    def test_s2_the_lookup_is_one_get_with_the_app_jwt_then_one_post(self):
        p = self.run_mint()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual([g[0] for g in Stub.gets], ["/repos/acme-org/widget-service/installation"])
        auth = Stub.gets[0][1].split()
        self.assertEqual((auth[0], len(auth[1].split("."))), ("Bearer", 3))
        self.assertEqual(Stub.seen[0][1], Stub.gets[0][1])
        self.assertEqual(Stub.seen[0][2], {"repositories": ["widget-service"], "permissions": PERMS})
        self.assertEqual(len(Stub.seen), 1)

    def test_s3_a_repository_without_an_installation_is_refused_saying_so_with_no_token_request(self):
        p = self.run_mint("third-acct/widget-service")
        self.assertEqual((p.returncode, p.stdout, Stub.seen), (1, "", []))
        self.assertIn("not installed", p.stderr.strip().splitlines()[-1])

    def test_s3_the_launcher_refuses_before_any_pod_with_that_line(self):
        import importlib
        import types
        from unittest import mock
        bin_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "bin")
        sys.path.insert(0, bin_dir)
        try:
            cw = importlib.import_module("cloud_worker")
        finally:
            sys.path.pop(0)
        env = {"CW_APP_ID_CMD": "echo 12345", "CW_APP_KEY_CMD": "cat '%s'" % self.keyfile, "CW_GITHUB_API_BASE": self.base, "CW_OPENSSL": self.wrap}
        cluster = types.SimpleNamespace(clean=lambda text, limit=300: text)
        with mock.patch.dict(os.environ, env), mock.patch.dict(cw.H, {"_one_line": lambda text, limit: text[:limit]}):
            os.environ.pop("CW_GIT_TOKEN_CMD", None)
            with self.assertRaises(cw.Refusal) as ctx:
                cw.git_token("third-acct/widget-service", cluster)
        self.assertIn("not installed", str(ctx.exception))
        self.assertTrue(str(ctx.exception).endswith("nothing was created"))

    def test_s4_no_installation_secret_or_command_is_needed_or_run(self):
        marker = os.path.join(self.tmp.name, "marker")
        p = self.run_mint(CW_APP_INSTALLATION_CMD="touch '%s'; echo 678" % marker)
        self.assertEqual((p.returncode, p.stdout), (0, TOKEN), p.stderr)
        self.assertFalse(os.path.exists(marker))
        env = {k: v for k, v in os.environ.items() if k != "CW_APP_INSTALLATION_CMD"}
        env.update(CW_APP_ID_CMD="echo 12345", CW_APP_KEY_CMD="cat '%s'" % self.keyfile, CW_GITHUB_API_BASE=self.base, CW_OPENSSL=self.wrap)
        p = subprocess.run([sys.executable, MINT, REAL], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual((p.returncode, p.stdout), (0, TOKEN), p.stderr)

    def test_s5_the_stored_id_is_ignored_even_when_not_a_number(self):
        Stub.installs = {"acme-org": 222}
        for stored in ("echo 999", "echo not-a-number"):
            Stub.seen = []
            p = self.run_mint(CW_APP_INSTALLATION_CMD=stored)
            self.assertEqual((p.returncode, p.stdout), (0, TOKEN), p.stderr)
            self.assertEqual(self.posts(), ["/app/installations/222/access_tokens"])

    BAD_LOOKUPS = {
        "another account": {"id": 5, "account": {"id": 9, "login": "evil-org"}},
        "a list of two": [{"id": 5, "account": {"id": 9, "login": "acme-org"}}, {"id": 6, "account": {"id": 9, "login": "acme-org"}}],
        "a list of one": [{"id": 5, "account": {"id": 9, "login": "acme-org"}}],
        "an id that is not a number": {"id": "5", "account": {"id": 9, "login": "acme-org"}},
        "an id that is a boolean": {"id": True, "account": {"id": 9, "login": "acme-org"}},
        "an id of zero": {"id": 0, "account": {"id": 9, "login": "acme-org"}},
        "no id": {"account": {"id": 9, "login": "acme-org"}},
        "no account": {"id": 5},
        "an account that is not an object": {"id": 5, "account": "acme-org"},
        "a login that is not text": {"id": 5, "account": {"id": 9, "login": 7}},
        "a suspended installation": {"id": 5, "account": {"id": 9, "login": "acme-org"}, "suspended_at": "2026-01-01T00:00:00Z"},
        "a string": "installation",
    }

    def test_s6_u3_a_bad_lookup_answer_is_refused_with_no_token_request_and_no_secret(self):
        for name, body in self.BAD_LOOKUPS.items():
            Stub.lookup, Stub.seen = (200, body), []
            p = self.run_mint()
            self.assertEqual((p.returncode, p.stdout, Stub.seen), (1, "", []), name)
            self.assertEqual(len(p.stderr.strip().splitlines()), 1, name)
            for secret in (TOKEN, self.keymark, Stub.gets[-1][1].split()[1]):
                self.assertNotIn(secret, p.stderr, name)

    def test_s6_the_owner_may_differ_only_in_case(self):
        Stub.lookup = (200, {"id": 5, "account": {"id": 9, "login": "ACME-Org"}})
        p = self.run_mint()
        self.assertEqual((p.returncode, p.stdout), (0, TOKEN), p.stderr)
        self.assertEqual(self.posts(), ["/app/installations/5/access_tokens"])

    def test_u3_a_lookup_that_fails_is_refused_with_empty_stdout_and_no_secret(self):
        for name, status, body in (("404", 404, {}), ("301", 301, {}), ("403", 403, {}), ("500", 500, {})):
            Stub.lookup, Stub.seen = (status, body), []
            p = self.run_mint()
            self.assertEqual((p.returncode, p.stdout, Stub.seen), (1, "", []), name)
            self.assertNotIn(Stub.gets[-1][1].split()[1], p.stderr)
            self.assertNotIn(self.keymark, p.stderr)
        Stub.lookup = (301, {})
        self.assertIn("redirect", self.run_mint().stderr)
        down = self.run_mint(CW_GITHUB_API_BASE="http://127.0.0.1:1")  # a network error
        self.assertEqual((down.returncode, down.stdout), (1, ""))
        self.assertIn("failed", down.stderr)

    def test_a_token_call_for_an_unselected_repository_says_to_add_it_to_the_installation(self):
        Stub.redirect = None
        orig = Stub.do_POST

        def refuse(handler):
            handler.rfile.read(int(handler.headers.get("Content-Length", 0)))
            handler.send_response(422)
            handler.send_header("Content-Length", "2")
            handler.end_headers()
            handler.wfile.write(b"{}")
        Stub.do_POST = refuse
        try:
            p = self.run_mint()
        finally:
            Stub.do_POST = orig
        self.assertEqual((p.returncode, p.stdout), (1, ""))
        self.assertIn("add it to the installation", p.stderr)

    # --- mutants: a copy of the mint with one check broken must fail the same tests (S8) ---

    MUTANTS = [  # (name, text to replace, replacement, the lookup answer the broken build wrongly accepts)
        ("always the first installation", "inst, owner_id = find_installation(target, jwt)", "inst, owner_id = 111, None", None),
        ("no owner check", "if not valid_owner(login) or login.lower() != owner.lower():", "if False:", "another account"),
        ("case-sensitive owner check", "login.lower() != owner.lower()", "login != owner", "case"),
        ("no id check", "if not is_id(inst):", "if False:", "an id of zero"),
        ("no shape check", "if not isinstance(ans, dict):", "if False:", "a string"),
        ("no owner id check", 'owner.get("id") != owner_id', "False", "owner id"),
        ("no repository list check", "len(repos) != 1 or ", "", "second repo"),
        ("plain lower on an unvalidated login", "not valid_owner(login) or ", "", "kelvin"),
        ("catches only ValueError", "except Exception:", "except ValueError:", "nested"),
        ("no suspended check", 'if ans.get("suspended_at"):', "if False:", "a suspended installation"),
    ]

    def run_copy(self, src_text, repo=REAL, runner=None):
        d = tempfile.mkdtemp(dir=self.tmp.name)
        for n in ("token_mint.py",):
            with open(os.path.join(os.path.dirname(MINT), n)) as f, open(os.path.join(d, n), "w") as g:
                g.write(f.read())
        path = os.path.join(d, "app_token.py")
        with open(path, "w") as f:
            f.write(src_text)
        e = dict(os.environ, CW_APP_ID_CMD="echo 12345", CW_APP_KEY_CMD="cat '%s'" % self.keyfile, CW_GITHUB_API_BASE=self.base, CW_OPENSSL=self.wrap, RAISE_CLASS="RecursionError")
        argv = [sys.executable, "-c", runner, path, repo] if runner else [sys.executable, path, repo]
        return subprocess.run(argv, env=e, capture_output=True, text=True, timeout=60)

    def mint_refused_variant(self, text, answer):
        """True when the build refuses a mint answer (a second repository, or an owner id that differs) with no token on stdout."""
        Stub.raw = None
        Stub.lookup, Stub.seen = (200, {"id": 5, "account": {"id": 9, "login": "acme-org"}}), []
        repo = {"name": "widget-service", "full_name": REAL, "owner": {"id": 10 if answer == "owner id" else 9}}
        Stub.answer["repositories"] = [repo] if answer == "owner id" else [repo, {"name": "other-service", "full_name": "acme-org/other-service"}]
        p = self.run_copy(text)
        return p.returncode == 1 and p.stdout == "" and p.stderr.startswith("app-token: ") and "Traceback" not in p.stderr

    def test_s8_each_mutant_is_caught_and_the_real_mint_passes_the_same_checks(self):
        with open(MINT) as f:
            real = f.read()
        two = {"acme-org": 111, "second-acct": 222}

        def second_account_ok(text):
            Stub.installs, Stub.lookup, Stub.seen = dict(two), None, []
            Stub.answer["repositories"] = [{"name": "other-service", "full_name": "second-acct/other-service", "owner": {"id": 9}}]
            p = self.run_copy(text, "second-acct/other-service")
            return p.returncode == 0 and self.posts() == ["/app/installations/222/access_tokens"]

        def refuses_cleanly(text, answer):
            """True when the build refuses this lookup answer with its own one-line message and makes no token request."""
            Stub.raw = None
            if answer == "nested":
                Stub.lookup, Stub.raw, Stub.seen = (200, None), b"[" * 10000 + b"]" * 10000, []
                p = self.run_copy(text, runner=RAISE_ON_NESTING)
                return p.returncode == 1 and p.stderr.startswith("app-token: ") and "Traceback" not in p.stderr
            if answer == "kelvin":
                Stub.lookup, Stub.seen = (200, {"id": 5, "account": {"id": 9, "login": "\u212a" + "ite-org"}}), []
                p = self.run_copy(text, "kite-org/widget-service")
                return p.returncode == 1 and p.stderr.startswith("app-token: ") and Stub.seen == []
            if answer in ("owner id", "second repo"):
                return self.mint_refused_variant(text, answer)
            Stub.lookup, Stub.seen = (200, self.BAD_LOOKUPS[answer]) if answer != "case" else (200, {"id": 5, "account": {"id": 9, "login": "ACME-Org"}}), []
            Stub.answer["repositories"] = [{"name": "widget-service", "full_name": REAL, "owner": {"id": 9}}]
            p = self.run_copy(text)
            return p.returncode == 1 and p.stderr.startswith("app-token: ") and "Traceback" not in p.stderr and Stub.seen == []

        self.assertTrue(second_account_ok(real))
        self.assertTrue(refuses_cleanly(real, "another account"))
        self.assertFalse(refuses_cleanly(real, "case"))  # accepted: the owner may differ in case
        for name, old, new, answer in self.MUTANTS:
            self.assertIn(old, real, name)
            mutant = real.replace(old, new, 1)
            self.assertNotEqual(mutant, real, name)
            if answer is None:
                self.assertFalse(second_account_ok(mutant), name)
            elif answer == "case":
                self.assertTrue(refuses_cleanly(mutant, answer), name)  # the real build accepts; the mutant refuses
            else:
                self.assertTrue(refuses_cleanly(real, answer), name)
                self.assertFalse(refuses_cleanly(mutant, answer), name)  # the real build refuses; the mutant does not

    # --- the family: every hostile shape x every field the code validates, and a missing id refuses ---

    @staticmethod
    def call_sites(src):
        """(validator, enclosing function, argument text) for every call of valid_owner or valid_target: the fields the code validates."""
        import ast
        sites = set()
        for fn in ast.walk(ast.parse(src)):
            if isinstance(fn, ast.FunctionDef):
                for n in ast.walk(fn):
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("valid_owner", "valid_target"):
                        sites.add((n.func.id, fn.name, ast.unparse(n.args[0])))
        return sites

    # field -> (the call site it is validated at, the text whose removal drops that validator)
    FIELDS = {
        "installation account.login": (("valid_owner", "find_installation", "login"), "not valid_owner(login) or "),
        "token repository full_name": (("valid_target", "mint", "full"), "valid_target(full) and "),
        "the target argument": (("valid_target", "main", "argv[0]"), " or not valid_target(argv[0])"),
        "the owner part of any target": (("valid_owner", "valid_target", "owner"), "valid_owner(owner) and "),
    }
    # the shapes a removed validator MUST be caught by (the others are also stopped by the comparison behind it)
    PINNED_BY = {"installation account.login": {"kelvin sign"}, "token repository full_name": {"kelvin sign"},
                 "the target argument": {"kelvin sign", "fullwidth letters", "trailing dot", "newline"},
                 "the owner part of any target": {"kelvin sign", "fullwidth letters", "trailing dot", "newline"}}
    # shape -> (hostile text, the ASCII owner it imitates); the imitated owner is the target's
    SHAPES = {
        "kelvin sign": ("\u212ame-org", "kme-org"),
        "dotless i": ("acme-or\u0131", "acme-orI"),
        "turkish i": ("\u0130cme-org", "icme-org"),
        "fullwidth letters": ("\uff41cme-org", "acme-org"),
        "trailing dot": ("acme-org.", "acme-org"),
        "double dot": ("acme..org", "acme-org"),
        "percent-encoded slash": ("acme%2Forg", "acme-org"),
        "newline": ("acme-org\n", "acme-org"),
    }

    def row_refuses(self, text, field, shape):
        """True when the build refuses this hostile shape in this field the way the real mint does: no token, no token request."""
        bad, ascii_owner = self.SHAPES[shape]
        target = ascii_owner + "/widget-service"
        Stub.raw, Stub.seen, Stub.installs = None, [], {ascii_owner.lower(): 5}
        Stub.lookup = (200, {"id": 5, "account": {"id": 9, "login": ascii_owner}})
        Stub.answer["repositories"] = [{"name": "widget-service", "full_name": target, "owner": {"id": 9}}]
        if field == "installation account.login":
            Stub.lookup = (200, {"id": 5, "account": {"id": 9, "login": bad}})
        elif field == "token repository full_name":
            Stub.answer["repositories"][0]["full_name"] = bad + "/widget-service"
        elif field in ("the target argument", "the owner part of any target"):
            target = bad + "/widget-service"
        p = self.run_copy(text, target)
        if field in ("the target argument", "the owner part of any target"):  # the owner part is reached through main's valid_target
            return p.returncode == 2 and p.stdout == "" and Stub.seen == []
        return p.returncode != 0 and p.stdout == "" and p.stderr.startswith("app-token: ") and "Traceback" not in p.stderr

    def test_every_hostile_shape_in_every_validated_field_is_refused_and_each_validator_is_pinned(self):
        with open(MINT) as f:
            real = f.read()
        self.assertEqual(self.call_sites(real), {site for site, _ in self.FIELDS.values()},
                         "a validator call site has no row in FIELDS (or a row names a call that is gone)")
        for field, (site, snippet) in self.FIELDS.items():
            self.assertIn(snippet, real, field)
            mutant = real.replace(snippet, "", 1)
            for shape in self.SHAPES:
                self.assertTrue(self.row_refuses(real, field, shape), (field, shape))
            caught = {sh for sh in self.SHAPES if not self.row_refuses(mutant, field, sh)}
            self.assertTrue(self.PINNED_BY[field] <= caught, "removing the validator at %s is not caught by %s" % (site, self.PINNED_BY[field] - caught))
        unanchored = real.replace("OWNER_RE.fullmatch(o)", "OWNER_RE.match(o)", 1)  # a weakened validator body, not a removed call
        self.assertNotEqual(unanchored, real)
        for shape in ("trailing dot", "newline"):
            self.assertFalse(self.row_refuses(unanchored, "the target argument", shape), shape)

    ID_SHAPES = {  # (what GitHub's answers carry) -> the named refusal
        "no account id": ("account", {"login": "acme-org"}, "no usable account id"),
        "account id a string": ("account", {"id": "9", "login": "acme-org"}, "no usable account id"),
        "account id a bool": ("account", {"id": True, "login": "acme-org"}, "no usable account id"),
        "account id a float": ("account", {"id": 9.0, "login": "acme-org"}, "no usable account id"),
        "account id zero": ("account", {"id": 0, "login": "acme-org"}, "no usable account id"),
        "no owner": ("owner", None, "no usable owner id"),
        "owner a list": ("owner", [{"id": 9}], "no usable owner id"),
        "owner a string": ("owner", "acme-org", "no usable owner id"),
        "no owner id": ("owner", {"login": "acme-org"}, "no usable owner id"),
        "owner id a string": ("owner", {"id": "9"}, "no usable owner id"),
        "owner id a bool": ("owner", {"id": True}, "no usable owner id"),
        "owner id a float": ("owner", {"id": 9.0}, "no usable owner id"),
        "owner id null": ("owner", {"id": None}, "no usable owner id"),
    }

    def id_row(self, text, where, value):
        Stub.raw, Stub.seen = None, []
        account = {"id": 9, "login": "acme-org"}
        repo = {"name": "widget-service", "full_name": REAL, "owner": {"id": 9}}
        if where == "account":
            Stub.lookup = (200, {"id": 5, "account": value})
        else:
            Stub.lookup = (200, {"id": 5, "account": account})
            if value is None:
                del repo["owner"]
            else:
                repo["owner"] = value
        Stub.answer["repositories"] = [repo]
        return self.run_copy(text)

    ID_MUTANTS = [  # skip-when-missing, brought back
        ("skip a missing account id", "    if not is_id(owner_id):\n", "    if owner_id is not None and not is_id(owner_id):\n",
         {"no account id"}),
        ("skip a missing owner id", 'if not isinstance(owner, dict) or not is_id(owner.get("id")):',
         'if isinstance(owner, dict) and owner.get("id") is not None and not is_id(owner.get("id")):',
         {"no owner", "no owner id", "owner id null"}),
    ]
    IS_ID_MUTANTS = [  # one per clause of is_id, each caught by the shape that clause exists for
        ("is_id lets a bool through", " and not isinstance(x, bool)", "", {"account id a bool", "owner id a bool"}),
        ("is_id lets a float through", "isinstance(x, int) and ", "", {"account id a float", "owner id a float"}),
        ("is_id lets zero through", " and x > 0", "", {"account id zero"}),
    ]

    def test_a_missing_or_mistyped_id_is_a_named_refusal_and_nothing_is_minted(self):
        with open(MINT) as f:
            real = f.read()
        for name, (where, value, reason) in self.ID_SHAPES.items():
            p = self.id_row(real, where, value)
            self.assertEqual((p.returncode, p.stdout), (1, ""), name)
            self.assertEqual(p.stderr.strip().count("\n"), 0, name)
            self.assertIn(reason, p.stderr, name)
            self.assertNotIn(TOKEN, p.stderr, name)
        p = self.id_row(real, "owner", {"id": 9})
        self.assertEqual((p.returncode, p.stdout), (0, TOKEN), p.stderr)  # the normal answer still mints
        for name, old, new, want in self.ID_MUTANTS:
            self.assertIn(old, real, name)
            mutant = real.replace(old, new, 1)
            held = {n for n, (w, v, r) in self.ID_SHAPES.items() if r not in self.id_row(mutant, w, v).stderr}
            self.assertTrue(want <= held, "%s: rows %s did not notice" % (name, want - held))
        for name, old, new, want in self.IS_ID_MUTANTS:
            self.assertIn(old, real, name)
            mutant = real.replace(old, new, 1)
            held = {n for n, (w, v, r) in self.ID_SHAPES.items() if r not in self.id_row(mutant, w, v).stderr}
            self.assertTrue(want <= held, "%s: rows %s did not notice" % (name, want - held))

    # --- a refusal after the mint revokes the unprinted token ---

    REFUSED_AFTER_MINT = {  # name -> how to break the token answer (each is a refusal that happens after GitHub minted)
        "second repository": lambda a: a.update(repositories=a["repositories"] * 2),
        "another repository": lambda a: a["repositories"][0].update(name="other-service"),
        "full_name missing": lambda a: a["repositories"][0].pop("full_name"),
        "full_name another owner": lambda a: a["repositories"][0].update(full_name="evil-org/widget-service"),
        "owner id missing": lambda a: a["repositories"][0].pop("owner"),
        "owner id another": lambda a: a["repositories"][0].update(owner={"id": 10}),
        "long-lived": lambda a: a.update(expires_at=iso(86400)),
        "extra permission": lambda a: a["permissions"].update(issues="write"),
    }

    def revoking_run(self, text, mutate, **stub):
        Stub.deletes, Stub.seen = [], []
        for k, v in stub.items():
            setattr(Stub, k, v)
        Stub.lookup = (200, {"id": 5, "account": {"id": 9, "login": "acme-org"}})
        mutate(Stub.answer)
        return self.run_copy(text)

    def test_every_refusal_after_the_mint_revokes_the_token_and_never_shows_it(self):
        with open(MINT) as f:
            real = f.read()
        for name, mutate in self.REFUSED_AFTER_MINT.items():
            p = self.revoking_run(real, mutate)
            self.assertEqual((p.returncode, p.stdout), (1, ""), name)
            self.assertEqual(Stub.deletes, [("/installation/token", "Bearer " + TOKEN)], name)
            self.assertEqual(p.stderr.strip().count("\n"), 0, name)
            self.assertTrue(p.stderr.strip().endswith("The token was revoked."), (name, p.stderr))
            self.assertNotIn(TOKEN, p.stderr + p.stdout, name)
            self.setUp()

    def test_a_failed_or_redirected_revocation_is_said_and_does_not_mask_the_refusal(self):
        with open(MINT) as f:
            real = f.read()
        for how in ({"delete_status": 500}, {"delete_status": 404}, {"delete_status": 200}, {"delete_redirect": "http://127.0.0.1:%d/x" % self.other.server_port}):
            self.setUp()
            p = self.revoking_run(real, self.REFUSED_AFTER_MINT["second repository"], **how)
            self.assertEqual((p.returncode, p.stdout), (1, ""), how)
            self.assertIn("does not cover exactly the one target repository", p.stderr)  # the refusal itself still comes first
            self.assertIn("Revocation failed: the token expires within an hour and was never printed.", p.stderr)
            self.assertNotIn(TOKEN, p.stderr + p.stdout)
            self.assertEqual(Other.seen, [], how)  # a redirect is not followed

    def test_a_minted_token_that_passes_every_check_is_not_revoked_and_a_refusal_before_the_mint_has_nothing_to_revoke(self):
        p = self.run_mint()
        self.assertEqual((p.returncode, p.stdout, Stub.deletes), (0, TOKEN, []))
        Stub.lookup = (200, {"id": 5, "account": {"login": "acme-org"}})
        self.assertEqual((self.run_mint().returncode, Stub.deletes), (1, []))

    REVOKE_MUTANTS = [  # name, text to replace, replacement
        ("no revoke", "if MINTED:", "if False:"),
        ("revoke only for the repository check", "    if MINTED:\n        token = MINTED.pop()", "    if MINTED and 'repository' in msg:\n        token = MINTED.pop()"),
        ("the token in the message", 'msg += (" The token was revoked."', 'msg += (" The token was revoked. " + token'),
        ("revoked said without asking", "return r.status == 204", "return True"),
        ("never registered", "    MINTED.append(token)\n", ""),
    ]

    def test_each_revocation_mutant_fails_a_row(self):
        with open(MINT) as f:
            real = f.read()
        for name, old, new in self.REVOKE_MUTANTS:
            self.assertIn(old, real, name)
            mutant = real.replace(old, new, 1)
            broken = False
            for row, mutate in self.REFUSED_AFTER_MINT.items():
                self.setUp()
                p = self.revoking_run(mutant, mutate, delete_status=200 if name == "revoked said without asking" else 204)
                ok = (p.returncode == 1 and Stub.deletes == [("/installation/token", "Bearer " + TOKEN)] and TOKEN not in p.stderr
                      and (p.stderr.strip().endswith("The token was revoked.") if name != "revoked said without asking" else "Revocation failed" in p.stderr))
                broken = broken or not ok
            self.assertTrue(broken, name)
        self.setUp()

    def test_s9_the_documents_do_not_ask_for_the_installation_id(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
        for rel in ("docs/cloud-operator-quickstart.md", "cloud/launcher/README.md"):
            with open(os.path.join(root, rel)) as f:
                text = f.read()
            for line in text.splitlines():
                if "INSTALLATION_CMD" in line or "installation-id" in line:
                    self.assertIn("obsolete", line, rel + ": " + line[:80])
            self.assertIn("/installation", text, rel)
            self.assertRegex(text, r"(?i)install(ed)? .{0,40}on the account\s+that owns", rel)


if __name__ == "__main__":
    unittest.main()
