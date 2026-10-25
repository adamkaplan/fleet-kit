"""Ask #1022 slice A: the puller step cw-stack-pull with a fake exchange and a recording fake docker (S4, S8, S9, S10, S16), and the image files."""
import importlib.machinery
import importlib.util
import io
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
_loader = importlib.machinery.SourceFileLoader("cw_stack_pull", os.path.join(ROOT, "cloud", "worker", "cw-stack-pull"))
sp = importlib.util.module_from_spec(importlib.util.spec_from_loader("cw_stack_pull", _loader))
_loader.exec_module(sp)

HOST = "acme-registry.azurecr.io"
IMG = HOST + "/acme/widget-service:1"
TOKEN = "sentinel-" + "projected-" + "aaaa1111"
ACCESS = "sentinel-" + "entra-" + "bbbb2222"
REFRESH = "sentinel-" + "registry-" + "cccc3333"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tok = os.path.join(self.tmp.name, "token")
        self.base = os.path.join(self.tmp.name, "tmpfs")
        os.makedirs(self.tok)
        os.makedirs(self.base)
        for n, v in (("registry-host", HOST), ("client-id", "11111111-2222-3333-4444-555555555555"), ("tenant-id", "66666666-7777-8888-9999-000000000000"), ("token", TOKEN)):
            with open(os.path.join(self.tok, n), "w") as f:
                f.write(v)
        self.posts, self.dockers, self.modes = [], [], []
        self.entra = (200, {"access_token": ACCESS})
        self.acr = (200, {"refresh_token": REFRESH})
        self.during = None

    def post(self, url, fields):
        self.posts.append((url, dict(fields)))
        return self.entra if "login.microsoftonline.com" in url else self.acr

    def runner(self, argv, input=None, env=None, **kw):
        self.dockers.append({"argv": list(argv), "input": input, "env": dict(env)})
        cfg = env["DOCKER_CONFIG"]
        if argv[1] == "login":
            p = os.path.join(cfg, "config.json")
            with open(os.open(p, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
                f.write("{}")
        self.modes.append((stat.S_IMODE(os.stat(cfg).st_mode), [stat.S_IMODE(os.stat(os.path.join(cfg, n)).st_mode) for n in os.listdir(cfg)]))
        if self.during and argv[1] == "pull":
            self.during()
        return subprocess.CompletedProcess(argv, 0, "", "")

    def go(self, images=(IMG,)):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = sp.main(list(images), token_dir=self.tok, cred_base=self.base, post=self.post, runner=self.runner)
        return code, out.getvalue() + err.getvalue()


def check_no_volume_args(tc, argvs):
    for argv in argvs:
        for el in argv:
            tc.assertNotEqual(el, "-v")
            tc.assertFalse(str(el).startswith("--volume"), el)


class Pull(Fixture):
    def test_success_uses_stdin_and_pulls(self):   # S9, S4
        code, out = self.go()
        self.assertEqual(code, 0, out)
        login = [d for d in self.dockers if d["argv"][1] == "login"][0]
        self.assertEqual(login["input"], REFRESH)
        self.assertIn("--password-stdin", login["argv"])
        pulls = [d for d in self.dockers if d["argv"][1] == "pull"]
        self.assertEqual(len(pulls), 1)
        blob = repr([d["argv"] for d in self.dockers]) + repr([d["env"] for d in self.dockers])
        for bad in (REFRESH, ACCESS, TOKEN, self.tok):
            self.assertNotIn(bad, blob)
        check_no_volume_args(self, [d["argv"] for d in self.dockers])
        for secret in (TOKEN, ACCESS, REFRESH):
            self.assertNotIn(secret, out)
        self.assertEqual(self.posts[0][1]["client_assertion"], TOKEN)
        self.assertTrue(self.posts[1][0].startswith("https://" + HOST))

    def test_volume_check_mutants(self):
        for extra in ("-v", "--volume=x"):
            argv = ["docker", "pull", "img"]
            mutated = argv + [extra]
            self.assertIn(extra, mutated)
            with self.assertRaises(AssertionError):
                check_no_volume_args(self, [mutated])
        check_no_volume_args(self, [["docker", "pull", "img"]])

    def test_success_ignores_host_environment(self):
        with mock.patch.dict(os.environ, {"FOO": "a-vb", "BAR": "--volume=z"}):
            self.test_success_uses_stdin_and_pulls()

    def test_another_host_is_refused_without_a_pull(self):   # S9
        code, out = self.go(["other.azurecr.io/x:1"])
        self.assertEqual(code, 2)
        self.assertEqual(self.dockers, [])
        self.assertEqual(self.posts, [])

    def test_option_shaped_and_no_arguments_are_refused(self):
        self.assertEqual(self.go(["-v"])[0], 2)
        self.assertEqual(self.go([])[0], 2)

    def test_the_hostname_is_not_read_from_the_environment(self):   # S16
        os.environ["CW_STACK_REGISTRY"] = "other.azurecr.io"
        self.addCleanup(os.environ.pop, "CW_STACK_REGISTRY", None)
        self.assertEqual(self.go(["other.azurecr.io/x:1"])[0], 2)
        self.assertEqual(self.go()[0], 0)

    def test_no_mounted_token_is_refused(self):
        os.remove(os.path.join(self.tok, "token"))
        self.assertEqual(self.go()[0], 2)


class Failures(Fixture):
    def check(self, owner_words):
        code, out = self.go()
        self.assertEqual(code, 1)
        self.assertIn(owner_words, out)
        self.assertEqual([d for d in self.dockers if d["argv"][1] == "pull"], [])
        for secret in (TOKEN, ACCESS, REFRESH, "raw-body"):
            self.assertNotIn(secret, out)
        self.assertEqual(os.listdir(self.base), [])

    def test_expired_or_refused_subject(self):   # S10
        for status in (400, 401):
            self.posts.clear()
            self.entra = (status, {"error_description": "raw-body " + TOKEN})
            self.check("federated credential for service account cw-stack-puller")
            self.assertEqual(len(self.posts), 1)   # no silent retry

    def test_refused_registry_exchange(self):   # S10
        self.acr = (401, {"error": "raw-body"})
        self.check("AcrPull")

    def test_network_failure(self):
        self.entra = (0, {})
        self.check("federated credential")


class Cleanup(Fixture):
    def test_mode_0600_during_and_gone_after_in_all_cases(self):   # S8
        code, _ = self.go()
        self.assertEqual(code, 0)
        self.assertTrue(any(m[0] == 0o700 and 0o600 in m[1] for m in self.modes), self.modes)
        self.assertTrue(all(set(m[1]) <= {0o600} for m in self.modes))
        self.assertEqual(os.listdir(self.base), [])
        self.acr = (401, {})
        self.assertEqual(self.go()[0], 1)
        self.assertEqual(os.listdir(self.base), [])

    def test_a_failing_pull_and_sigterm_leave_nothing(self):   # S8
        def boom():
            raise RuntimeError("pull blew up")
        self.during = boom
        with self.assertRaises(RuntimeError):
            self.go()
        self.assertEqual(os.listdir(self.base), [])
        self.during = lambda: os.kill(os.getpid(), signal.SIGTERM)
        code, _ = self.go()
        self.assertEqual(code, 1)
        self.assertEqual(os.listdir(self.base), [])


class Transport(unittest.TestCase):
    def test_an_http_address_is_refused(self):
        with self.assertRaises(sp.Refused) as c:
            sp.http_post("http://login.example.internal/token", {"a": "b"})
        self.assertEqual(c.exception.code, 2)

    def test_a_redirect_is_not_followed(self):
        import http.server
        import threading
        hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/first":
                    self.send_response(302)
                    self.send_header("Location", "/second")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")

            do_GET = do_POST

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url = "http://127.0.0.1:%d/first" % srv.server_port
        # http_post refuses http, so drive the same opener it builds
        req = sp.urllib.request.Request(url, data=b"client_assertion=x", method="POST")
        with self.assertRaises(sp.urllib.error.HTTPError) as c:
            sp.urllib.request.build_opener(sp._NoRedirect).open(req, timeout=5)
        self.assertEqual(c.exception.code, 302)
        self.assertEqual(hits, ["/first"])
        # and a mutant opener that follows does reach /second
        sp.urllib.request.build_opener().open(sp.urllib.request.Request(url, data=b"x", method="POST"), timeout=5)
        self.assertEqual(hits[-1], "/second")

    def test_http_post_builds_its_opener_with_the_no_redirect_handler(self):
        seen = []
        real = sp.urllib.request.build_opener

        def spy(*handlers):
            seen.extend(handlers)
            raise OSError("stop")
        sp.urllib.request.build_opener = spy
        try:
            self.assertEqual(sp.http_post("https://login.example.internal/t", {}), (0, {}))
        finally:
            sp.urllib.request.build_opener = real
        self.assertEqual(seen, [sp._NoRedirect])


class Modes(Fixture):
    def test_directory_0700_and_config_0600_even_when_the_defaults_are_looser(self):
        real_mk, real_run = sp.tempfile.mkdtemp, self.runner

        def loose_mkdtemp(**kw):
            d = real_mk(**kw)
            os.chmod(d, 0o755)
            return d

        def loose_runner(argv, input=None, env=None, **kw):
            if argv[1] == "login":
                with open(os.path.join(env["DOCKER_CONFIG"], "config.json"), "w") as f:
                    f.write("{}")
                os.chmod(os.path.join(env["DOCKER_CONFIG"], "config.json"), 0o644)
            return real_run(argv, input=input, env=env, **kw)
        sp.tempfile.mkdtemp = loose_mkdtemp
        self.addCleanup(setattr, sp.tempfile, "mkdtemp", real_mk)
        self.runner = loose_runner
        code, _ = self.go()
        self.assertEqual(code, 0)
        pull = [m for d, m in zip(self.dockers, self.modes) if d["argv"][1] == "pull"][0]
        self.assertEqual(pull, (0o700, [0o600]))


class ImageFiles(unittest.TestCase):
    def test_dockerfile_directory_sudoers_and_script(self):   # S4, S16
        with open(os.path.join(ROOT, "cloud", "worker", "Dockerfile")) as f:
            text = f.read()
        self.assertEqual(len(re.findall(r"install -d -m 0700 -o root -g root /opt/cw-registry\b", text)), 1)
        rules = re.findall(r"echo 'worker ALL=\(root\) NOPASSWD: ([^']*)'", text)
        self.assertEqual(len(rules), 1)
        self.assertEqual(sorted(x.strip() for x in rules[0].split(",")), ["/usr/local/bin/cw-stack-pull", "/usr/local/bin/start-dockerd"])
        self.assertIn("Defaults:worker env_reset", text)
        self.assertIn("COPY worker/cw-stack-pull /usr/local/bin/cw-stack-pull", text)
        with open(os.path.join(ROOT, "cloud", "worker", "cw-stack-pull")) as f:
            src = f.read()
        self.assertNotIn("os.environ", src)   # the step reads nothing from the caller's environment
        self.assertNotRegex(src, r'"-v"|--volume')


if __name__ == "__main__":
    unittest.main()
