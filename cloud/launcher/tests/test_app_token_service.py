"""Ask #2002, scenario S3 (and S8): the service branch of app_token.py's mint against a fake service on loopback. No cluster, no vault."""
import http.server
import json
import os
import shutil
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
SKIP_CHECK = ('    check_permissions(ans.get("permissions"), role)\n    MINTED.clear()\n    return token\n\n\ndef mint(')
SKIPPED = '    MINTED.clear()\n    return token\n\n\ndef mint('


def iso(seconds):
    import datetime as dt
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def answer(perms=PERMS, repo="widget-service", full=REAL, life=3500, token=TOKEN):
    return {"token": token, "expires_at": iso(life), "permissions": dict(perms),
            "repositories": [{"name": repo, "full_name": full, "owner": {"id": 9}}]}


class Fake(http.server.BaseHTTPRequestHandler):
    status, body = 201, {}
    seen = []   # (path, Authorization, JSON body) of each POST

    def log_message(self, *a):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        Fake.seen.append((self.path, self.headers.get("Authorization"), json.loads(raw)))
        out = json.dumps(Fake.body).encode()
        self.send_response(Fake.status)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_DELETE(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


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
        Fake.seen, Fake.status, Fake.body = [], 201, answer()

    def run_client(self, args=(REAL,), mint=MINT, url=None, **env):
        e = {k: v for k, v in os.environ.items() if not k.startswith("CW_")}
        e.update(PATH=self.bin + os.pathsep + os.environ["PATH"], CW_TOKEN_SERVICE_URL=url or self.url,
                 CW_TOKEN_SERVICE_CALLER_TOKEN_CMD="echo " + CALLER, CW_GITHUB_API_BASE="http://127.0.0.1:%d" % self.srv.server_port)
        e.update(env)
        return subprocess.run([sys.executable, mint] + list(args), env=e, capture_output=True, text=True, timeout=60)

    def test_s3_two_roles_print_one_token_and_nothing_else(self):
        for args, perms in (((REAL,), PERMS), ((REAL, "--role", "orchestrator"), ORCH)):
            Fake.body = answer(perms)
            p = self.run_client(args)
            self.assertEqual((p.returncode, p.stdout), (0, TOKEN), p.stderr)
            self.assertNotIn(CALLER, p.stdout + p.stderr)
            path, auth, body = Fake.seen[-1]
            self.assertEqual((path, auth, body["repo"]), ("/mint", "Bearer " + CALLER, REAL))
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


if __name__ == "__main__":
    unittest.main()
