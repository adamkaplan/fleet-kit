#!/usr/bin/env python3
"""The launch's own mint of the pod's git token is tried again when the token command exits with status 75, and only then (ask #1627).

Offline: fake token commands are small scripts in a temp dir, the clock is a list, GitHub is a loopback fake. The old behaviour is the old
git_token, kept below as text (a scratch module outside the repository). The launcher's twin (run_git_token_cmd) gets the same cases.
"""
import contextlib
import http.server
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
BIN_SRC = os.environ.get("GIT_TOKEN_RETRY_BIN") or os.path.join(ROOT, "bin", "cloud_worker.py")   # a mutant's scratch copy, in a child
MINT = os.path.join(ROOT, "cloud", "launcher", "app_token.py")
sys.path.insert(0, os.path.join(ROOT, "cloud", "worker", "tests"))
import stub_support  # noqa: E402

REPO = "acme-org/widget-service"
TOKEN = "ghs_TOKENVALUE0123456789"
OLD_GIT_TOKEN = '''
def git_token(repo, cluster, role="worker"):
    raw = os.environ.get("CW_GIT_TOKEN_CMD")
    argv = shlex.split(raw)
    env = dict(os.environ)
    ran = _capture(argv, env=dict(env, GH_REPO=repo))
    token = (ran[1] if ran and ran[0] == 0 else "").strip()
    if not token or len(token) > 4096:
        why = ""
        if ran and ran[2].strip():
            why = ": " + _one(cluster.clean(ran[2].strip().splitlines()[-1], 200), 200)
        raise Refusal("the git token command gave no usable token%s; nothing was created" % why)
    return token
'''


def plain_capture(argv, env=None, **kw):
    p = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


def load_bin(src_text=None):
    mod = types.ModuleType("git_token_retry_bin")
    mod.__file__ = os.path.join(ROOT, "bin", "cloud_worker.py")
    exec(compile(src_text or open(BIN_SRC).read(), mod.__file__, "exec"), mod.__dict__)
    mod._capture = plain_capture
    mod.H["_one_line"] = lambda text, limit=300: text[:limit]
    return mod


def load_launcher():
    path = os.path.join(ROOT, "cloud", "launcher", "cloud_worker.py")
    sys.path.insert(0, os.path.dirname(path))
    try:
        spec = importlib.util.spec_from_file_location("git_token_retry_launcher", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


class Fakes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="git-token-retry-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.n = 0

    def command(self, statuses, message="vault hiccup"):
        """A fake token command: the Nth run exits with statuses[N] (the last one repeats); status 0 prints the token."""
        self.n += 1
        counter = os.path.join(self.dir, "runs%d" % self.n)
        script = os.path.join(self.dir, "cmd%d.py" % self.n)
        with open(script, "w") as f:
            f.write("import sys\nc=%r\nopen(c,'a').write('x')\nn=len(open(c).read())\ns=%r\nst=s[min(n,len(s))-1]\n"
                    "if st==0:\n    sys.stdout.write(%r)\nelse:\n    sys.stderr.write(%r+'\\n')\nsys.exit(st)\n" % (counter, list(statuses), TOKEN, message))
        self.counter = counter
        return "%s %s" % (sys.executable, script)

    def runs(self):
        if not os.path.exists(self.counter):
            return 0
        with open(self.counter) as f:
            return len(f.read())

    def launch(self, mod, cmd):
        """Run the bin's git_token with CW_GIT_TOKEN_CMD=cmd; returns (token or None, refusal text, waits, stderr text)."""
        waits, err, token, refusal = [], io.StringIO(), None, ""
        mod.git_token_sleep = waits.append
        cluster = types.SimpleNamespace(clean=lambda text, n=200: text)
        with mock.patch.dict(os.environ, {"CW_GIT_TOKEN_CMD": cmd}), contextlib.redirect_stderr(err):
            try:
                token = mod.git_token(REPO, cluster)
            except mod.Refusal as e:
                refusal = str(e)
        return token, refusal, waits, err.getvalue()

    def launcher_run(self, cmd):
        lib = load_launcher()
        waits, err, token, refusal = [], io.StringIO(), None, ""
        with mock.patch.object(lib, "git_token_sleep", waits.append), contextlib.redirect_stderr(err):
            try:
                token = lib.run_git_token_cmd(cmd.split(" "), REPO, dict(os.environ))
            except lib.CloudError as e:
                refusal = str(e)
        return token, refusal, waits, err.getvalue()


class TestOldBehaviour(Fakes):
    def test_s2_the_old_function_runs_the_command_once(self):
        scratch = types.ModuleType("old_git_token_scratch")   # in memory, outside the repository
        cmd = self.command([75, 0])
        scratch.__dict__.update(os=os, shlex=__import__("shlex"), sys=sys, _capture=plain_capture, _one=lambda t, n=300: t[:n],
                                Refusal=type("Refusal", (Exception,), {}))
        exec(OLD_GIT_TOKEN, scratch.__dict__)
        with mock.patch.dict(os.environ, {"CW_GIT_TOKEN_CMD": cmd}):
            with self.assertRaises(scratch.Refusal) as ctx:
                scratch.git_token(REPO, types.SimpleNamespace(clean=lambda t, n=200: t))
        refused = "gave no usable token" in str(ctx.exception)
        self.assertEqual((self.runs(), refused), (1, True))
        print("S2: command runs %d, refusal text gave no usable token" % self.runs())


class KeyedGitHub(http.server.BaseHTTPRequestHandler):
    answer = (503, {}, b"{}")

    def log_message(self, *a):
        pass

    def do_GET(self):   # the installation lookup always succeeds: the failures under test are the token request's
        out = b'{"id": 678, "account": {"id": 9, "login": "acme-org"}, "repository_selection": "selected", "suspended_at": null}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        status, headers, body = KeyedGitHub.answer
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestExitStatuses(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="git-token-status-")
        cls.key = os.path.join(cls.tmp.name, "k.pem")
        subprocess.run(["openssl", "genrsa", "-out", cls.key, "2048"], check=True, capture_output=True)
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), KeyedGitHub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def mint(self, base=None, id_cmd="echo 12345"):
        env = dict(os.environ, CW_APP_ID_CMD=id_cmd, CW_APP_INSTALLATION_CMD="echo 678", CW_APP_KEY_CMD="cat '%s'" % self.key,
                   CW_GITHUB_API_BASE=base or self.base)
        return subprocess.run([sys.executable, MINT, REPO], env=env, capture_output=True, text=True, timeout=60)

    def test_s3_passing_failures_exit_75_and_refusals_keep_status_1(self):
        import json
        passing = []
        passing.append(self.mint(id_cmd="exit 3"))                                    # the vault command fails
        with socket_closed_port() as port:
            passing.append(self.mint(base="http://127.0.0.1:%d" % port))              # the network fails
        for status, headers in ((503, {}), (429, {}), (403, {"Retry-After": "3"})):
            KeyedGitHub.answer = (status, headers, b"{}")
            passing.append(self.mint())
        refused = []
        for status in (404, 422):
            KeyedGitHub.answer = (status, {}, b'{"message": "no"}')
            refused.append(self.mint())
        other = {"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z", "repositories": [{"name": "other", "full_name": "acme-org/other", "owner": {"id": 9}}],
                 "permissions": {"contents": "write", "pull_requests": "write", "issues": "read", "metadata": "read"}}
        KeyedGitHub.answer = (201, {}, json.dumps(other).encode())
        refused.append(self.mint())
        a = sum(p.returncode == 75 for p in passing)
        b = sum(p.returncode == 1 for p in refused)
        self.assertEqual((a, b), (5, 3), [p.returncode for p in passing + refused])
        self.assertTrue(all(TOKEN not in p.stdout + p.stderr for p in passing + refused))
        print("S3: passing failures with status 75 %d of 5, refusals with status 1 %d of 3" % (a, b))


@contextlib.contextmanager
def socket_closed_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    yield port


class TestTryAgainOnStatus75(Fakes):
    def test_s4_a_token_on_the_second_try_names_the_retry_without_a_value(self):
        good = 0
        lines = 0
        leaked = 0
        for run in (lambda c: self.launch(load_bin(), c), self.launcher_run):
            cmd = self.command([75, 0])
            token, refusal, waits, err = run(cmd)
            good += (token == TOKEN and waits == [2] and self.runs() == 2)
            lines += len(err.strip().splitlines())
            leaked += TOKEN in err or TOKEN in refusal
        self.assertEqual((good, lines, leaked), (2, 2, 0))
        self.assertIn("git token try 1 of 4 failed: vault hiccup; trying again in 2 seconds", err)
        print("S4: launches with a token on the second try %d of 1, retry lines %d, token values printed %d" % (good // 2, lines // 2, leaked))


class TestStatusOneIsNotTriedAgain(Fakes):
    def test_s5_other_statuses_run_once_and_a_custom_command_with_75_is_retried(self):
        once = 0
        for run in (lambda c: self.launch(load_bin(), c), self.launcher_run):
            for status in (1, 2):
                cmd = self.command([status, 0])
                token, refusal, waits, err = run(cmd)
                once += (self.runs() == 1 and token is None and waits == [] and "after" not in refusal and "gave no usable token" in refusal)
        self.assertEqual(once, 4)
        retried = 0
        for run in (lambda c: self.launch(load_bin(), c), self.launcher_run):
            token, refusal, waits, err = run(self.command([75, 0]))
            retried += (token == TOKEN and self.runs() == 2)
        self.assertEqual(retried, 2)
        print("S5: status 1 commands refused after exactly 1 try %d of 2, custom status 75 retried %d of 1" % (once // 2, retried // 2))


class TestFourTries(Fakes):
    def test_s6_four_tries_with_waits_2_5_10_and_one_refusal_naming_them(self):
        names = 0
        for run in (lambda c: self.launch(load_bin(), c), self.launcher_run):
            token, refusal, waits, err = run(self.command([75]))
            ok = (self.runs() == 4 and waits == [2, 5, 10] and token is None and "gave no usable token after 4 tries: vault hiccup" in refusal
                  and len(err.strip().splitlines()) == 3)
            names += ok
        self.assertEqual(names, 2)
        print("S6: tries 4, waits 2 5 10, refusals 1 naming 4 tries")


MUTANTS = (("retries a status 1", "if not ran or ran[0] != 75 or tries >= 4:", "if not ran or ran[0] == 0 or tries >= 4:", "TestStatusOneIsNotTriedAgain"),
           ("never retries", "if not ran or ran[0] != 75 or tries >= 4:", "if True:", "TestTryAgainOnStatus75"))


class TestMutants(unittest.TestCase):
    def test_s7_each_mutant_fails_its_scenario(self):
        failing = applied = 0
        for name, old, new, cls in MUTANTS:
            scratch = tempfile.mkdtemp(prefix="git-token-mutant-")
            self.addCleanup(shutil.rmtree, scratch, True)
            src = open(os.path.join(ROOT, "bin", "cloud_worker.py")).read()
            self.assertEqual(src.count(old), 1, name)
            path = os.path.join(scratch, "cloud_worker.py")
            open(path, "w").write(src.replace(old, new))
            applied += 1
            p = stub_support.start(self, [sys.executable, "-m", "unittest", "test_git_token_retry." + cls], cwd=HERE,
                                   env=dict(os.environ, GIT_TOKEN_RETRY_BIN=path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            p.communicate(timeout=120)
            self.assertEqual(p.returncode, 1, name)
            failing += 1
        self.assertEqual((failing, applied), (2, 2))
        print("S7: %d of 2 mutants fail, %d of 2 mutations applied" % (failing, applied))


class TestDocs(unittest.TestCase):
    def test_s8_three_sentences_are_in_both_documents(self):
        phrases = ("exits with status 75 for a passing failure", "retries the command only on status 75", "A custom token command is retried only when it exits with 75")
        found = 0
        for rel in ("cloud/launcher/README.md", "docs/cloud-worker-interface.md"):
            text = " ".join(open(os.path.join(ROOT, rel)).read().split())
            found += sum(p in text for p in phrases)
        self.assertEqual(found, 6)
        print("S8: %d of 3 sentences found" % (found // 2))


if __name__ == "__main__":
    unittest.main()
