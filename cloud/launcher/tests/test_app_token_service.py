"""Ask #2002, scenario S3 (and S8): the service branch of app_token.py's mint against a fake service on loopback. No cluster, no vault."""
import http.server
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
MINT = os.path.join(LAUNCHER, "app_token.py")
TOKEN = "ghs_synthetic" + "Qw7Zx1" * 6
CALLER = "caller-synthetic-" + "Mn4" * 5
REAL = "acme-org/widget-service"
PERMS = {"contents": "write", "pull_requests": "write", "issues": "read", "metadata": "read"}
ORCH = {"issues": "write", "pull_requests": "read", "checks": "read", "metadata": "read"}
SKIP_CHECK = ('    check_permissions(ans.get("permissions"), role)\n    if not keep:\n        MINTED.clear()\n    return token\n\n\nUSAGE')
SKIPPED = '    if not keep:\n        MINTED.clear()\n    return token\n\n\nUSAGE'


def iso(seconds):
    import datetime as dt
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def answer(perms=PERMS, repo="widget-service", full=REAL, life=3500, token=TOKEN):
    return {"token": token, "expires_at": iso(life), "permissions": dict(perms),
            "repositories": [{"name": repo, "full_name": full, "owner": {"id": 9}}]}


class Fake(http.server.BaseHTTPRequestHandler):
    status, body = 201, {}
    raw = None   # when set, the answer's bytes as they are (a body that is not JSON)
    seen = []   # (path, Authorization, JSON body) of each POST
    callers = []   # the X-Caller-Token of each POST

    def log_message(self, *a):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        Fake.callers.append(self.headers.get("X-Caller-Token"))
        Fake.seen.append((self.path, self.headers.get("Authorization"), json.loads(raw)))
        out = Fake.raw if Fake.raw is not None else json.dumps(Fake.body).encode()
        self.send_response(Fake.status)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_DELETE(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


def via_route(argv, env):
    """Run the mint with the named route (a pipe, --fd): the result's stdout is what came through the route (the token); the real stdout must be empty."""
    r, w = os.pipe()
    try:
        p = subprocess.run(argv + ["--fd", str(w)], env=env, capture_output=True, text=True, timeout=60, pass_fds=(w,))
    finally:
        os.close(w)
    with os.fdopen(r) as f:
        token = f.read()
    assert p.stdout == "", "the mint wrote to standard output"
    return subprocess.CompletedProcess(p.args, p.returncode, token, p.stderr)


def apply_mutation(original, old, new):
    assert original.count(old) == 1, "the text to change does not occur exactly once"
    changed = original.replace(old, new)
    assert changed != original, "the mutation was not applied"
    assert old in original and old not in changed, "the mutation was not applied"
    return changed


class ServiceBranch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        cls.bin = os.path.join(d, "bin")
        os.makedirs(cls.bin)
        cls.azlog = os.path.join(d, "az.log")
        with open(os.path.join(cls.bin, "az"), "w") as f:
            f.write('#!/bin/sh\necho called >> "%s"\nexit 1\n' % cls.azlog)
        os.chmod(os.path.join(cls.bin, "az"), 0o700)
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), Fake)
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()
        cls.url = "http://127.0.0.1:%d/mint" % cls.srv.server_port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.t.join(5)
        cls.tmp.cleanup()

    def setUp(self):
        Fake.seen, Fake.callers, Fake.status, Fake.body, Fake.raw = [], [], 201, answer(), None

    def run_client(self, args=(REAL,), mint=MINT, url=None, **env):
        e = {k: v for k, v in os.environ.items() if not k.startswith("CW_")}
        e.update(PATH=self.bin + os.pathsep + os.environ["PATH"], CW_TOKEN_SERVICE_URL=url or self.url,
                 CW_TOKEN_SERVICE_CALLER_TOKEN_CMD="echo " + CALLER, CW_GITHUB_API_BASE="http://127.0.0.1:%d" % self.srv.server_port)
        e.update(env)
        return via_route([sys.executable, mint] + list(args), e)

    def test_s3_two_roles_print_one_token_and_nothing_else(self):
        for args, perms in (((REAL,), PERMS), ((REAL, "--role", "orchestrator"), ORCH)):
            Fake.body = answer(perms)
            p = self.run_client(args)
            self.assertEqual((p.returncode, p.stdout), (0, TOKEN), p.stderr)
            self.assertNotIn(CALLER, p.stdout + p.stderr)
            path, auth, body = Fake.seen[-1]
            self.assertEqual((path, auth, body["repo"]), ("/mint", "Bearer " + CALLER, REAL))
            self.assertEqual(Fake.callers[-1], "Bearer " + CALLER)   # S11 (a): the same token in the second header
            self.assertEqual(body["role"], "orchestrator" if len(args) > 1 else "worker")
            self.assertEqual(set(body), {"repo", "role"})
        self.assertFalse(os.path.exists(self.azlog), "the vault was read")
        print("S3: 2 of 2 roles print one token and nothing else, 0 vault reads")

    def test_s3_wrong_answers_are_refused(self):
        wrong = [answer(repo="other", full="acme-org/other"), answer(perms=dict(PERMS, administration="write")), answer(life=7200)]
        for body in wrong:
            Fake.body = body
            p = self.run_client()
            self.assertEqual((p.returncode, p.stdout), (1, ""), p.stderr)
            self.assertNotIn(TOKEN, p.stderr)
            self.assertNotIn(CALLER, p.stderr)
        self.assertFalse(os.path.exists(self.azlog))
        print("S3: %d of 3 wrong answers refused, no token printed" % len(wrong))

    def test_s3_exit_codes(self):
        for status, code in ((502, 75), (503, 75), (400, 1), (401, 1), (403, 1), (413, 1)):
            Fake.status, Fake.body = status, {}
            p = self.run_client()
            self.assertEqual((p.returncode, p.stdout), (code, ""), (status, p.stderr))
        s = http.server.HTTPServer(("127.0.0.1", 0), Fake)
        port = s.server_port
        s.server_close()   # nothing listens: a refused connection
        p = self.run_client(url="http://127.0.0.1:%d/mint" % port)
        self.assertEqual((p.returncode, p.stdout), (75, ""))
        self.assertFalse(os.path.exists(self.azlog))
        print("S3: exit 75 for the passing failure (502, 503, no connection) and 1 for each refusal")

    def test_s3_mutant_that_skips_the_answers_checks(self):
        with open(MINT) as f:
            original = f.read()
        scratch = os.path.join(self.tmp.name, "scratch")
        os.makedirs(scratch, exist_ok=True)
        shutil.copy(os.path.join(LAUNCHER, "token_mint.py"), scratch)

        def check(text):
            path = os.path.join(scratch, "app_token.py")
            with open(path, "w") as f:
                f.write(text)
            Fake.body = answer(perms=dict(PERMS, administration="write"))
            p = self.run_client(mint=path)
            if p.returncode == 0 and p.stdout == TOKEN:
                raise AssertionError("an unchecked answer was accepted: exit 0 and a token on standard output")
            return p.returncode
        self.assertEqual(check(original), 1)   # the control, run first
        with self.assertRaisesRegex(AssertionError, "an unchecked answer was accepted:"):
            check(apply_mutation(original, SKIP_CHECK, SKIPPED))
        with self.assertRaisesRegex(AssertionError, "the mutation was not applied"):
            apply_mutation(original, SKIP_CHECK, SKIP_CHECK)
        print("S3 mutant: control 1 of 1, the copy fails 1 of 1, the no-op mutation fails 1 of 1")

    def test_s8_no_fixture_value_in_any_output(self):
        p = self.run_client()
        self.assertEqual(p.stdout, TOKEN)
        self.assertNotIn(CALLER, p.stdout + p.stderr)
        self.assertEqual(p.stderr, "")
        print("S8: 0 caller-token occurrences in the client's output")


class ClientBranch(unittest.TestCase):
    """Ask #2002, scenario S11 (a) to (d): a fake https service with a private certificate made here by openssl (a 0700 directory, removed after)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()   # mode 0700
        d = cls.tmp.name
        cls.cert, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cls.cert, "-days", "2",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, capture_output=True)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cls.cert, key)
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), Fake)
        cls.srv.socket = ctx.wrap_socket(cls.srv.socket, server_side=True)
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()
        cls.port = cls.srv.server_port
        cls.server = "https://localhost:%d" % cls.port
        cls.exact = cls.server + "/api/v1/namespaces/cw/services/token-mint:8080/proxy/mint"
        cls.scratch = os.path.join(d, "scratch")
        os.makedirs(cls.scratch)
        shutil.copy(os.path.join(LAUNCHER, "token_mint.py"), cls.scratch)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.t.join(5)
        cls.tmp.cleanup()

    def setUp(self):
        Fake.seen, Fake.callers, Fake.status, Fake.body, Fake.raw = [], [], 201, answer(), None

    def run_client(self, mint=MINT, url=None, ca=True, **env):
        e = {k: v for k, v in os.environ.items() if not k.startswith("CW_")}
        e.update(CW_TOKEN_SERVICE_URL=url or self.exact, CW_TOKEN_SERVICE_CALLER_TOKEN_CMD="echo " + CALLER,
                 CW_CLOUD_SERVER=self.server, CW_CLOUD_NAMESPACE="cw")
        if ca:
            e["CW_CLOUD_CA_FILE"] = self.cert
        e.update(env)
        return via_route([sys.executable, mint, REAL], e)

    def test_s11_b_the_ca_file_is_trusted_and_without_it_the_service_is_refused(self):
        p = self.run_client()
        self.assertEqual((p.returncode, p.stdout), (0, TOKEN), p.stderr)
        self.assertEqual(Fake.callers[-1], "Bearer " + CALLER)
        n = len(Fake.seen)
        p = self.run_client(ca=False)
        self.assertEqual((p.returncode, p.stdout, len(Fake.seen)), (75, "", n), p.stderr)
        print("S11 (b): accepted with the CA file, refused without it: 2 of 2")

    def test_s11_c_a_url_that_is_not_the_exact_proxy_is_refused_before_a_request(self):
        base_path = "/api/v1/namespaces/cw/services/token-mint:8080/proxy/mint"
        urls = ["https://127.0.0.1:%d%s" % (self.port, base_path),                                        # another host
                self.server + "/api/v1/namespaces/cw/services/token-mint:8080/proxy/other",               # another path
                self.server + base_path.replace("namespaces/cw", "namespaces/other"),                     # another namespace
                self.server + base_path.replace("token-mint:8080", "other-svc:8080")]                     # another service name
        for url in urls:
            p = self.run_client(url=url)
            self.assertEqual((p.returncode, p.stdout), (1, ""), p.stderr)
            self.assertNotIn(CALLER, p.stderr)
        self.assertEqual(Fake.seen, [])
        print("S11 (c): %d of 4 foreign URLs refused, the fake recorded %d requests" % (len(urls), len(Fake.seen)))

    def test_s11_d_a_token_with_a_foreign_character_and_an_answer_that_is_not_json(self):
        for bad in (TOKEN + "\n", TOKEN[:10] + " " + TOKEN[10:], TOKEN[:10] + "\x01" + TOKEN[10:]):
            Fake.body = answer(token=bad)
            p = self.run_client()
            self.assertEqual((p.returncode, p.stdout), (1, ""), p.stderr)
        print("S11 (d): 3 of 3 tokens with a foreign character refused, nothing printed")
        Fake.status, Fake.raw, Fake.seen = 200, b"<html>not json</html>", []
        p = self.run_client()
        self.assertEqual((p.returncode, p.stdout, len(Fake.seen)), (1, "", 1), p.stderr)
        Fake.raw = None
        for status in (429, 500, 503):
            Fake.status, Fake.body = status, {}
            self.assertEqual(self.run_client().returncode, 75, status)
        p = self.run_client(CW_TOKEN_SERVICE_CALLER_TOKEN_CMD="exit 1")
        self.assertEqual((p.returncode, p.stdout), (75, ""))
        print("S11 (d): a 200 answer that is not JSON exits 1 (1 request); 429, 5xx and a failed caller login exit 75")

    def test_s11_the_undone_copies_fail_each_with_its_own_text(self):
        with open(MINT) as f:
            original = f.read()
        base_path = self.exact.replace("/proxy/mint", "/proxy/other")

        def run_copy(text):
            path = os.path.join(self.scratch, "app_token.py")
            with open(path, "w") as f:
                f.write(text)
            Fake.seen, Fake.callers, Fake.status, Fake.body, Fake.raw = [], [], 201, answer(), None
            return path

        def check_header(text):
            p = self.run_client(mint=run_copy(text))
            if Fake.callers[-1:] != ["Bearer " + CALLER]:
                raise AssertionError("the caller token was not sent in X-Caller-Token: %r" % Fake.callers[-1:])

        def check_ca(text):
            p = self.run_client(mint=run_copy(text))
            if p.returncode != 0:
                raise AssertionError("the CA file was not trusted: exit %d" % p.returncode)

        def check_url(text):
            self.run_client(mint=run_copy(text), url=base_path)
            if Fake.seen:
                raise AssertionError("a foreign service URL was used: %d request(s) sent" % len(Fake.seen))

        def check_chars(text):
            path = run_copy(text)
            Fake.body = answer(token=TOKEN + "\n")
            p = self.run_client(mint=path)
            if p.returncode == 0 or p.stdout:
                raise AssertionError("a token with a foreign character was accepted:")

        def check_json(text):
            path = run_copy(text)
            Fake.raw = b"not json"
            p = self.run_client(mint=path)
            if p.returncode != 1:
                raise AssertionError("an answer that is not JSON was retried: exit %d" % p.returncode)

        cases = [
            (' "X-Caller-Token": "Bearer " + bearer,', "", check_header, "the caller token was not sent in X-Caller-Token:"),
            ("        handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=cafile)))\n", "        pass\n",
             check_ca, "the CA file was not trusted:"),
            ("        if not server.netloc or url != exact:\n", "        if False:\n", check_url, "a foreign service URL was used:"),
            ('    if not re.fullmatch(r"[A-Za-z0-9_.-]+", token):\n', "    if False:\n", check_chars, "a token with a foreign character was accepted:"),
            ("    except ValueError:  # status 200", "    except OSError:  # status 200", check_json, "an answer that is not JSON was retried:"),
        ]
        for old, new, check, anchor in cases:
            check(original)   # the control, run first
            with self.assertRaisesRegex(AssertionError, anchor):
                check(apply_mutation(original, old, new))
            with self.assertRaisesRegex(AssertionError, "the mutation was not applied"):
                apply_mutation(original, old, old)
        print("S11 mutants: %d of %d undone copies fail with their own text, %d controls pass, the no-op fails the computation" % (len(cases), len(cases), len(cases)))


if __name__ == "__main__":
    unittest.main()
