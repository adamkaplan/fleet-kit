"""The idle nudge treats /work/results.txt as finished only when it is NEWER than the pod's last user message. Stub pods, no cluster."""
import inspect
import os
import sys
import textwrap
import threading
import http.server
import json
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_idle_nudge as tn  # noqa: E402

cw = tn.cw
CONTROL_SRC = textwrap.dedent(inspect.getsource(cw.Launcher.send_idle_nudges))
OLD_IF = 'if seen != "no" and not results_not_finished(seen):'


def mutant(old, new):
    src = CONTROL_SRC.replace(old, new)
    assert src != CONTROL_SRC, "the mutation was not applied"
    ns = {}
    exec(compile(src, "<mutant>", "exec"), cw.__dict__, ns)
    return ns["send_idle_nudges"]


def pod_named(name):
    p = tn.pod()
    p["metadata"]["name"] = name
    p["metadata"]["annotations"]["cw/worker"] = name.split("-")[0]
    return p


def stub(answer):
    return tn.Stub(results=answer)


def one_pass(fn, s, pods):
    import contextlib
    import io
    with contextlib.redirect_stderr(io.StringIO()):
        fn(s, pods, tn.NOW)


class TestReused(unittest.TestCase):
    fn = staticmethod(cw.Launcher.send_idle_nudges)

    def test_reused_pod_nudged_once_then_not_again(self):
        s = stub("100.0 200.0 1")   # the file is older than the last prompt
        one_pass(self.fn, s, [tn.pod()])
        self.assertEqual((len(s.sent), len(s.audits)), (1, 1), "a re-used pod was not nudged")
        one_pass(self.fn, s, [tn.pod(marked=True)])
        self.assertEqual(len(s.sent), 1)

    def test_newer_file_without_the_marker_nudged_once(self):
        s = stub("300.0 200.0 0")
        one_pass(self.fn, s, [tn.pod()])
        self.assertEqual((len(s.sent), len(s.audits)), (1, 1), "a re-used pod was not nudged")
        one_pass(self.fn, s, [tn.pod(marked=True)])
        self.assertEqual(len(s.sent), 1)

    def test_finished_pod_not_nudged(self):
        s = stub("300.0 200.0 1")
        one_pass(self.fn, s, [tn.pod()])
        self.assertEqual((s.sent, s.audits, s.marks), ([], [], []))

    def test_unreadable_time_not_nudged_and_next_pod_handled(self):
        for answer in ("unreadable", "100.0 unreadable", "100.0 unreadable 1", "garbage", "yes", "fail"):
            s = stub(answer)
            one_pass(self.fn, s, [tn.pod()])
            self.assertEqual((s.sent, s.audits, s.marks), ([], [], []), answer)
        answers = {"a-pod": "fail", "b-pod": "100.0 200.0 1"}
        s = tn.Stub()

        def _exec(name, *cmd, **kw):
            if answers[name] == "fail":
                raise cw.CloudError("exec failed")
            return answers[name] + "\n"
        s.kube.exec = _exec
        s.activity = lambda name: (False, tn.NOW - tn.dt.timedelta(minutes=20))
        one_pass(self.fn, s, [pod_named("a-pod"), pod_named("b-pod")])
        self.assertEqual(len(s.sent), 1, "the pass did not go on to the next pod")


FAKE_KUBECTL = """#!/usr/bin/env python3
import os, subprocess, sys
a = sys.argv[1:]
i = a.index("--")
cmd = a[i + 1:]
assert cmd[:2] == ["python3", "-c"], cmd
code = cmd[2].replace("/work/", os.environ["FAKE_WORK"] + "/")
sys.exit(subprocess.run([sys.executable, "-c", code]).returncode)
"""


class Api(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") is None:
            self.send_response(401)
            self.end_headers()
            return
        if self.path == "/api/session":
            data = [{"id": "s1"}]
        else:
            data = [{"id": "m1", "text": "", "time": {"created": int(self.server.prompt * 1000)}, "type": "user"},
                    {"agent": "", "content": "", "cost": 0, "finish": "", "id": "m2", "model": "", "rawFinish": "", "time": {"created": 9999999999999}, "tokens": {}, "type": "assistant"},
                    {"id": "m3", "outcome": "", "time": {"created": 9999999999998}, "type": "idle"}]
        body = json.dumps({"cursor": {}, "data": data}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)


class TestRealExec(unittest.TestCase):
    def run_real(self, marker, newer):
        with tempfile.TemporaryDirectory() as d:
            kc = os.path.join(d, "kubectl")
            with open(kc, "w") as f:
                f.write(FAKE_KUBECTL)
            os.chmod(kc, 0o755)
            work = os.path.join(d, "work")
            os.mkdir(work)
            with open(os.path.join(d, "pw"), "w") as f:
                f.write("x\n")
            res = os.path.join(work, "results.txt")
            with open(res, "w") as f:
                f.write("done things\n" + ("STATUS: done\n\n" if marker else "more\n"))
            srv = http.server.HTTPServer(("127.0.0.1", 0), Api)
            srv.prompt = time.time() - 1000
            os.utime(res, (srv.prompt + (500 if newer else -500),) * 2)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                env = {"CW_KUBECTL": kc, "CW_KUBE_CONTEXT": "c", "FAKE_WORK": work, "CW_ACTIVITY_PORT": str(srv.server_port),
                       "CW_ACTIVITY_PASSWORD_FILE": os.path.join(d, "pw")}
                s = tn.Stub()
                s.kube = cw.Kube(env)
                s.kube.annotate = lambda n, u, **kv: s.marks.append(kv)
                one_pass(cw.Launcher.send_idle_nudges, s, [tn.pod()])
                return s
            finally:
                srv.shutdown()
                srv.server_close()

    def test_newer_file_with_the_marker_is_finished(self):
        s = self.run_real(True, True)
        self.assertEqual((s.sent, s.audits, s.marks), ([], [], []))

    def test_probe_prints_a_number_for_the_real_shape(self):
        out = self.probe(cw.RESULTS_PROBE_CODE)
        self.assertNotIn("unreadable", out)
        self.assertEqual(len(out.split()), 3)

    def test_mutant_role_only_prints_unreadable(self):
        code = cw.RESULTS_PROBE_CODE.replace('m.get("type") == "user" or ', "")
        self.assertNotEqual(code, cw.RESULTS_PROBE_CODE)
        self.assertEqual(self.probe(cw.RESULTS_PROBE_CODE).split()[1].replace(".", "").isdigit(), True)  # control first
        self.assertIn("unreadable", self.probe(code))

    def probe(self, code):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            res = os.path.join(d, "results.txt")
            open(res, "w").write("x\nSTATUS: done\n")
            open(os.path.join(d, "pw"), "w").write("x\n")
            srv = http.server.HTTPServer(("127.0.0.1", 0), Api)
            srv.prompt = time.time() - 1000
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                env = dict(os.environ, CW_ACTIVITY_PORT=str(srv.server_port), CW_ACTIVITY_PASSWORD_FILE=os.path.join(d, "pw"))
                return subprocess.run([sys.executable, "-c", code.replace("/work/", d + "/")], env=env, capture_output=True, text=True).stdout
            finally:
                srv.shutdown()
                srv.server_close()

    def test_control_real_path_nudges_without_marker_or_when_older(self):
        for marker, newer in ((False, True), (True, False)):
            s = self.run_real(marker, newer)
            self.assertEqual((len(s.sent), len(s.audits)), (1, 1), "a re-used pod was not nudged")


class TestMutant(unittest.TestCase):
    def test_control_first(self):
        s = stub("300.0 200.0 0")
        one_pass(cw.Launcher.send_idle_nudges, s, [tn.pod()])
        self.assertEqual(len(s.sent), 1)

    def test_mutant_without_the_comparison_fails(self):
        s = stub("300.0 200.0 0")
        one_pass(mutant(OLD_IF, 'if seen != "no":'), s, [tn.pod()])
        with self.assertRaisesRegex(AssertionError, "a re-used pod was not nudged"):
            self.assertEqual(len(s.sent), 1, "a re-used pod was not nudged")


if __name__ == "__main__":
    unittest.main()
