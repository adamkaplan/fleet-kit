"""#1354 scenario S12: the REAL scope probe of the refresh meets a 307 redirect and does not follow it.

Two local servers on free loopback ports (two threads). The first answers every request with a 307 to the second; the second counts what it
receives and answers a good scope. Only the address of the request is moved to the first server (the probe's address is fixed in the code);
the probe itself is the launcher's own method. A mutant (a scratch copy of the probe that follows the redirect) is RUN in a child process
and must fail the same check with exit 1. No token is a literal here.
"""
import http.server
import inspect
import json
import os
import subprocess
import sys
import textwrap
import threading
import types
import unittest
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import cloud_worker as cw  # noqa: E402

REPO = "owner/name"
GOOD = {"repositories": [{"full_name": REPO, "permissions": {"push": True, "pull": True}}]}


def _serve(handler):
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def mutant_probe():
    """A scratch copy of the probe whose opener follows redirects. The edit is asserted applied."""
    src = textwrap.dedent(inspect.getsource(cw.Launcher.scope_probe))
    old = "token_mint.open_no_redirect(req, SCOPE_PROBE_SECONDS)"
    assert old in src, "the mutation did not apply"
    src = src.replace(old, "urllib.request.urlopen(req, timeout=SCOPE_PROBE_SECONDS)")
    ns = dict(vars(cw))
    exec(src, ns)
    return ns["scope_probe"]


def run_case(mutant=False):
    """Returns (answer of the probe, requests the second server received)."""
    seen = []

    class Second(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = json.dumps(GOOD).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    second = _serve(Second)
    target = "http://127.0.0.1:%d/second" % second.server_address[1]

    class First(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(307)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    first = _serve(First)
    first_url = "http://127.0.0.1:%d/first" % first.server_address[1]
    real_request = urllib.request.Request

    class Moved(real_request):
        def __init__(self, url, *a, **k):
            super().__init__(first_url if str(url).startswith("https://api.github.com/") else url, *a, **k)

    L = cw.Launcher.__new__(cw.Launcher)
    if mutant:
        L.scope_probe = types.MethodType(mutant_probe(), L)
    try:
        with mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"}), mock.patch("urllib.request.Request", Moved):
            answer = L._scope_is_same("marker-" + "x" * 8, REPO)
    finally:
        first.shutdown()
        second.shutdown()
    return answer, len(seen)


class ProbeRedirect(unittest.TestCase):
    def test_s12_the_real_probe_answers_no_on_a_redirect_and_the_second_server_sees_nothing(self):
        answer, got = run_case()
        print("S12: second-server requests %d, probe answer %s" % (got, "yes" if answer else "no"))
        self.assertEqual(got, 0, "%d requests received by the second server where 0 are allowed" % got)
        self.assertIs(answer, False)

    def test_s12_a_probe_that_follows_the_redirect_fails_the_same_check(self):
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "mutant"], capture_output=True, text=True)
        print("S12 mutant: exit %d, %s" % (r.returncode, (r.stdout.strip().splitlines() or [""])[-1]))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("second-server requests 1", r.stdout)


if __name__ == "__main__":
    if sys.argv[1:] == ["mutant"]:
        answer, got = run_case(mutant=True)
        print("second-server requests %d, probe answer %s" % (got, "yes" if answer else "no"))
        sys.exit(0 if (got == 0 and answer is False) else 1)
    unittest.main()
