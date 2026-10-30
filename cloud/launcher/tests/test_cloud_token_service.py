"""The token service (ask #1582, S14): one request served, three blocked, and a planted key never appears in an answer, the log or the audit.
Fakes only: a fake TokenReview, a fake mint, no network, no vault."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import token_service  # noqa: E402

PLANTED = "PLANTED-APP-KEY-0123456789"
ENV = {"CW_ALLOWED_REPOS": "acme/widgets,acme/gadgets"}
CALLER = token_service.REAPER_IDENTITY


def run(body, bearer="reaper-token"):
    log, audit, minted = [], [], []

    def review(b):
        return {"reaper-token": CALLER, "other-token": "system:serviceaccount:cw:cw-worker"}.get(b)

    def mint(repo):
        minted.append(repo)
        return "ghs_fake_installation_token"

    out = token_service.serve(json.dumps(body), bearer, ENV, review, mint, log.append, audit.append)
    return out, log, audit, minted


class TestServe(unittest.TestCase):
    def test_one_served_three_blocked_no_key(self):
        cases = [
            ({"repository": "acme/widgets", "role": "worker"}, "reaper-token", 200),
            ({"repository": "acme/other", "role": "worker"}, "reaper-token", 403),
            ({"repository": "acme/widgets", "role": "admin"}, "reaper-token", 403),
            ({"repository": "acme/widgets", "role": "worker"}, "other-token", 403),
        ]
        served = blocked = 0
        for body, bearer, want in cases:
            (status, answer), log, audit, minted = run(body, bearer)
            self.assertEqual(status, want)
            served += status == 200
            blocked += status == 403
            if status == 403:
                self.assertEqual(minted, [])
            blob = json.dumps(answer) + "\n".join(log) + "\n".join(audit)
            self.assertNotIn(PLANTED, blob)
            if status == 200:
                self.assertNotIn("ghs_fake", "\n".join(log) + "\n".join(audit))
        self.assertEqual((served, blocked), (1, 3))

    def test_extra_fields_and_bad_bodies_are_blocked(self):
        for body in ({"repository": "acme/widgets", "role": "worker", "key": PLANTED}, {"repository": 5, "role": "worker"}):
            (status, _), log, audit, minted = run(body)
            self.assertEqual((status, minted), (403, []))
            self.assertNotIn(PLANTED, "\n".join(log + audit))


class TestSocketTimeout(unittest.TestCase):
    def test_a_silent_client_is_cut_and_the_next_request_is_served(self):
        import http.client
        import socket
        import threading
        import time
        self.assertEqual(token_service.SOCKET_TIMEOUT, 10)
        old = token_service.SOCKET_TIMEOUT
        token_service.SOCKET_TIMEOUT = 1   # the same code path, a shorter wait for the test
        try:
            srv = token_service.make_server(ENV, 0, review=lambda b: CALLER, mint=lambda r: "ghs_fake", log=lambda l: None)
        finally:
            token_service.SOCKET_TIMEOUT = old
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        silent = socket.create_connection(("127.0.0.1", port))   # sends nothing
        try:
            began = time.time()
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            c.request("POST", "/", body=json.dumps({"repository": "acme/widgets", "role": "worker"}), headers={"Authorization": "Bearer x"})
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            self.assertLess(time.time() - began, 8)
        finally:
            silent.close()
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
