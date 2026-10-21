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
PERMS = {"contents": "write", "pull_requests": "write", "metadata": "read"}
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
            status, body = (200, {"id": inst, "account": {"login": parts[2]}, "repository_selection": "selected", "suspended_at": None}) if inst else (404, {"message": "Not Found"})
        out = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

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
        Stub.installs = {"acme-org": 678, "second-acct": 222}
        Stub.lookup = None
        Stub.pad_to = None
        Stub.redirect = None
        Other.seen = []
        Stub.answer = {"token": TOKEN, "expires_at": iso(3500), "repositories": [{"name": "widget-service", "full_name": REAL}],
                       "permissions": dict(PERMS)}
        if os.path.exists(self.argv_log):
            os.remove(self.argv_log)

    def run_mint(self, repo=REAL, **env):
        e = dict(os.environ, CW_APP_ID_CMD="echo 12345", CW_APP_INSTALLATION_CMD="echo 678",
                 CW_APP_KEY_CMD="cat '%s'" % self.keyfile, CW_GITHUB_API_BASE=self.base, CW_OPENSSL=self.wrap)
        e.update(env)
        return subprocess.run([sys.executable, MINT, repo], env=e, capture_output=True, text=True, timeout=60)

    def test_prints_only_the_token_and_signs_a_valid_short_jwt(self):
        p = self.run_mint()
        self.assertEqual((p.returncode, p.stdout), (0, TOKEN), p.stderr)
        path, auth, body = Stub.seen[0]
        self.assertEqual((path, body), ("/app/installations/678/access_tokens", {"repositories": ["widget-service"]}))
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
        cases = [dict(PERMS, issues="read"), dict(PERMS, administration="write"), dict(PERMS, contents="admin"),
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
        Stub.answer["permissions"] = dict(PERMS, issues="read")
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
            Stub.answer["repositories"] = [{"name": repo.split("/")[1], "full_name": repo}]
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
        self.assertEqual(Stub.seen[0][2], {"repositories": ["widget-service"]})
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
        "another account": {"id": 5, "account": {"login": "evil-org"}},
        "a list of two": [{"id": 5, "account": {"login": "acme-org"}}, {"id": 6, "account": {"login": "acme-org"}}],
        "a list of one": [{"id": 5, "account": {"login": "acme-org"}}],
        "an id that is not a number": {"id": "5", "account": {"login": "acme-org"}},
        "an id that is a boolean": {"id": True, "account": {"login": "acme-org"}},
        "an id of zero": {"id": 0, "account": {"login": "acme-org"}},
        "no id": {"account": {"login": "acme-org"}},
        "no account": {"id": 5},
        "an account that is not an object": {"id": 5, "account": "acme-org"},
        "a login that is not text": {"id": 5, "account": {"login": 7}},
        "a suspended installation": {"id": 5, "account": {"login": "acme-org"}, "suspended_at": "2026-01-01T00:00:00Z"},
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
        Stub.lookup = (200, {"id": 5, "account": {"login": "ACME-Org"}})
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
        ("always the first installation", "inst = find_installation(target, jwt)", "inst = 111", None),
        ("no owner check", "if not isinstance(login, str) or login.lower() != owner.lower():", "if False:", "another account"),
        ("case-sensitive owner check", "login.lower() != owner.lower()", "login != owner", "case"),
        ("no id check", "if isinstance(inst, bool) or not isinstance(inst, int) or inst <= 0:", "if False:", "an id of zero"),
        ("no shape check", "if not isinstance(ans, dict):", "if False:", "a string"),
        ("no suspended check", 'if ans.get("suspended_at"):', "if False:", "a suspended installation"),
    ]

    def run_copy(self, src_text, repo=REAL):
        d = tempfile.mkdtemp(dir=self.tmp.name)
        for n in ("token_mint.py",):
            with open(os.path.join(os.path.dirname(MINT), n)) as f, open(os.path.join(d, n), "w") as g:
                g.write(f.read())
        path = os.path.join(d, "app_token.py")
        with open(path, "w") as f:
            f.write(src_text)
        e = dict(os.environ, CW_APP_ID_CMD="echo 12345", CW_APP_KEY_CMD="cat '%s'" % self.keyfile, CW_GITHUB_API_BASE=self.base, CW_OPENSSL=self.wrap)
        return subprocess.run([sys.executable, path, repo], env=e, capture_output=True, text=True, timeout=60)

    def test_s8_each_mutant_is_caught_and_the_real_mint_passes_the_same_checks(self):
        with open(MINT) as f:
            real = f.read()
        two = {"acme-org": 111, "second-acct": 222}

        def second_account_ok(text):
            Stub.installs, Stub.lookup, Stub.seen = dict(two), None, []
            Stub.answer["repositories"] = [{"name": "other-service", "full_name": "second-acct/other-service"}]
            p = self.run_copy(text, "second-acct/other-service")
            return p.returncode == 0 and self.posts() == ["/app/installations/222/access_tokens"]

        def refuses_cleanly(text, answer):
            """True when the build refuses this lookup answer with its own one-line message and makes no token request."""
            Stub.lookup, Stub.seen = (200, self.BAD_LOOKUPS[answer]) if answer != "case" else (200, {"id": 5, "account": {"login": "ACME-Org"}}), []
            Stub.answer["repositories"] = [{"name": "widget-service", "full_name": REAL}]
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
