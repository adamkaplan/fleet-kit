"""Ask #2002, scenarios S2 and S8: the token service's handler with a fake GitHub (loopback) and a fake review. No cluster, no network."""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
SERVICE = os.path.join(os.path.dirname(HERE), "token_service.py")
import test_app_token as base  # noqa: E402  (the stored fake GitHub: Stub, TOKEN, PERMS, iso)
import token_service  # noqa: E402

ALLOWED = "acme-org/widget-service,acme-org/other-repo"
OPERATOR = "caller-synthetic-operator"
OTHER = "caller-synthetic-worker"
OPERATOR_USER = "system:serviceaccount:cw:cw-operator"
ORCH_PERMS = {"issues": "write", "pull_requests": "read", "checks": "read", "metadata": "read"}
CHECK_BLOCK = ('    if scheme != "Bearer" or not token:\n'
               '        return 401, None, "no caller token"\n'
               '    try:\n'
               '        authenticated, user = review(token)\n'
               '    except ReviewUnavailable:\n'
               '        return 502, None, "the caller review could not be asked"\n'
               '    if authenticated is not True:\n'
               '        return 401, None, "the caller token is not accepted"\n'
               '    if user != "system:serviceaccount:%s:cw-operator" % (env.get("CW_CLOUD_NAMESPACE") or "cw"):\n'
               '        return 403, None, "the caller is not the operator account"\n')


def fake_review(token):
    if token == OPERATOR:
        return True, OPERATOR_USER
    if token == OTHER:
        return True, "system:serviceaccount:cw:cw-worker"
    return False, None


def apply_mutation(original, old, new):
    """The changed text, with the mutation proved applied from the two texts."""
    assert original.count(old) == 1, "the text to change does not occur exactly once"
    changed = original.replace(old, new)
    assert changed != original, "the mutation was not applied"
    assert old in original and old not in changed, "the mutation was not applied"
    return changed


def load(text):
    ns = {"__file__": SERVICE, "__name__": "token_service_copy"}
    exec(compile(text, "token_service_copy", "exec"), ns)
    return ns


class Served(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        cls.keyfile = os.path.join(d, "k.pem")
        subprocess.run(["openssl", "genrsa", "-out", cls.keyfile, "2048"], check=True, capture_output=True)
        os.chmod(cls.keyfile, 0o600)
        with open(cls.keyfile) as f:
            cls.keymark = [l for l in f.read().splitlines() if l and "-----" not in l][1]
        cls.before = sorted(os.listdir(d))
        cls.github = base.http.server.HTTPServer(("127.0.0.1", 0), base.Stub)
        cls.gt = threading.Thread(target=cls.github.serve_forever, daemon=True)
        cls.gt.start()
        cls.patch = mock.patch.dict(os.environ, {
            "CW_APP_ID_CMD": "echo 12345", "CW_APP_KEY_CMD": "cat '%s'" % cls.keyfile, "CW_OPENSSL": "openssl",
            "CW_GITHUB_API_BASE": "http://127.0.0.1:%d" % cls.github.server_port, "CW_ALLOWED_REPOS": ALLOWED})
        cls.patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        cls.github.shutdown()
        cls.gt.join(5)
        cls.tmp.cleanup()

    def setUp(self):
        base.Stub.seen, base.Stub.gets, base.Stub.deletes = [], [], []
        base.Stub.installs = {"acme-org": 678}
        base.Stub.lookup = base.Stub.raw = base.Stub.pad_to = base.Stub.redirect = None
        self.answer = {"token": base.TOKEN, "expires_at": base.iso(3500), "permissions": dict(base.PERMS),
                       "repositories": [{"name": "widget-service", "full_name": base.REAL, "owner": {"id": 9}}]}
        base.Stub.answer = self.answer
        self.log = []

    def serve(self, module=None, review=fake_review):
        mod = module or vars(token_service)
        srv = mod["make_server"](review, port=0, host="127.0.0.1", log=self.log.append)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()

        def stop():
            srv.shutdown()
            srv.server_close()
            t.join(5)
        self.addCleanup(stop)
        return srv.server_port

    def post(self, port, body, token=OPERATOR, raw=None, header="X-Caller-Token"):
        data = raw if raw is not None else json.dumps(body).encode()
        h = {"Content-Type": "application/json"}
        if token:
            h[header] = "Bearer " + token
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            c.request("POST", "/mint", data, h)
            r = c.getresponse()
            return r.status, r.read().decode()
        finally:
            c.close()

    def requests(self, port):
        """The eight requests (a) to (h): name to (status, text)."""
        ok = {"repo": base.REAL, "role": "worker"}
        out = {}
        out["a"] = self.post(port, ok)
        base.Stub.answer = dict(self.answer, permissions=dict(ORCH_PERMS))
        out["b"] = self.post(port, {"repo": base.REAL, "role": "orchestrator"})
        out["c"] = self.post(port, {"repo": "evil-org/other", "role": "worker"})
        out["d"] = self.post(port, {"repo": base.REAL, "role": "admin"})
        out["e"] = self.post(port, ok, token=None)
        out["f"] = self.post(port, ok, token=OTHER)
        out["g"] = self.post(port, None, raw=json.dumps({"repo": base.REAL, "role": "worker", "pad": "x" * 5000}).encode())
        out["h"] = self.post(port, {"repo": base.REAL, "role": "worker", "expires_in": 7200})
        out["h2"] = self.post(port, {"repo": base.REAL, "role": "worker", "permissions": {"administration": "write"}})
        return out

    def test_s2_the_eight_requests(self):
        port = self.serve()
        r = self.requests(port)
        a, b = json.loads(r["a"][1]), json.loads(r["b"][1])
        self.assertEqual((r["a"][0], r["b"][0]), (200, 200))
        self.assertEqual((a["token"], a["permissions"], a["expires_at"], a["repositories"]),
                         (base.TOKEN, base.PERMS, self.answer["expires_at"], self.answer["repositories"]))
        self.assertEqual((b["token"], b["permissions"]), (base.TOKEN, ORCH_PERMS))
        want = {"c": 403, "d": 400, "e": 401, "f": 403, "g": 413, "h": 400, "h2": 400}
        for k, status in want.items():
            self.assertEqual(r[k][0], status, k)
            self.assertEqual(len(r[k][1].strip().splitlines()), 1, k)
            self.assertNotIn(base.TOKEN, r[k][1])
        refused = [k for k in "cdefgh" if r[k][0] >= 400]
        self.assertEqual(len(refused), 6)
        self.assertEqual([p for p in base.Stub.seen if p[0].endswith("access_tokens")].__len__(), 2)  # nothing was minted for a refusal
        print("S2: 2 of 2 allowed served, %d of 6 refused with one line each" % len(refused))
        # S8: no key, token or caller token in a log line; the token only in the two answers; no file written
        text = "\n".join(self.log)
        for secret in (self.keymark, base.TOKEN, OPERATOR, OTHER, "Bearer"):
            self.assertNotIn(secret, text)
        self.assertEqual(len(self.log), 9)
        self.assertEqual(sum(v[1].count(base.TOKEN) for v in r.values()), 2)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), self.before)
        print("S8: 0 key or token occurrences in %d log lines, the token in 2 answers only, 0 files written" % len(self.log))

    def test_s11_a_request_with_only_authorization_is_refused(self):
        port = self.serve()
        self.assertEqual(self.post(port, {"repo": base.REAL, "role": "worker"}, header="Authorization")[0], 401)
        self.assertEqual(self.post(port, {"repo": base.REAL, "role": "worker"})[0], 200)
        print("S11 (a): a request with only Authorization is refused 401: 1 of 1")

    def test_review_or_github_unavailable_is_502(self):
        def broken(token):
            raise token_service.ReviewUnavailable()
        port = self.serve(review=broken)
        self.assertEqual(self.post(port, {"repo": base.REAL, "role": "worker"})[0], 502)
        port2 = self.serve()
        base.Stub.lookup = (500, {"message": "x"})
        self.assertEqual(self.post(port2, {"repo": base.REAL, "role": "worker"})[0], 502)

    def test_s2_mutant_without_the_caller_check(self):
        with open(SERVICE) as f:
            original = f.read()

        def check(module):
            port = self.serve(module)
            status, _ = self.post(port, {"repo": base.REAL, "role": "worker"}, token=None)
            if status == 200:
                raise AssertionError("a request without a caller token was served: status %d" % status)
            return status
        self.assertEqual(check(load(original)), 401)  # the control, run first
        mutant = load(apply_mutation(original, CHECK_BLOCK, ""))
        with self.assertRaisesRegex(AssertionError, "a request without a caller token was served:"):
            check(mutant)
        with self.assertRaisesRegex(AssertionError, "the mutation was not applied"):
            apply_mutation(original, CHECK_BLOCK, CHECK_BLOCK)
        print("S2 mutant: control 1 of 1, the copy fails 1 of 1, the no-op mutation fails 1 of 1")


if __name__ == "__main__":
    unittest.main()
