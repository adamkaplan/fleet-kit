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
    redirect = None  # a Location to answer with as a 307 (it would re-send the POST and its Authorization)
    pad_to = None  # pad the JSON answer with trailing spaces to exactly this many bytes
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        Stub.seen.append((self.path, self.headers.get("Authorization", ""), json.loads(body)))
        if Stub.redirect:
            self.send_response(307)
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
        for var in ("CW_APP_ID_CMD", "CW_APP_INSTALLATION_CMD", "CW_APP_KEY_CMD"):
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

    def test_a_redirect_is_refused_and_nothing_reaches_the_other_host(self):
        for code_url in ("http://127.0.0.1:%d/leak" % self.other.server_port,):
            Stub.redirect = code_url
            p = self.run_mint()
            self.assertNotEqual(p.returncode, 0)
            self.assertEqual(p.stdout, "")
            self.assertIn("redirect", p.stderr)
            self.assertEqual(Other.seen, [])

    def test_bad_usage(self):
        self.assertEqual(self.run_mint("no-slash").returncode, 2)
        for bad in ("a/..", "../b", ".", "a/b/c", "a/", "/b", "a/./b"):
            self.assertEqual(self.run_mint(bad).returncode, 2, bad)
        self.assertEqual(Stub.seen, [])


if __name__ == "__main__":
    unittest.main()
