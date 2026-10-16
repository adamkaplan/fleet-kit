#!/usr/bin/env python3
"""A stub headless server on loopback, for the driver tests. usage: stub_server.py PORT.
STUB_LOG (file): one line per event. STUB_PID_FILE: our pid. STUB_MODE: ok (default) | never-healthy | busy-forever."""
import http.server
import json
import os
import sys

MODE = os.environ.get("STUB_MODE", "ok")
LOG = os.environ.get("STUB_LOG")
polls = {"n": 0}


def log(msg):
    if LOG:
        with open(LOG, "a") as f:
            f.write(msg + "\n")


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.headers.get("Authorization", "").startswith("Basic "):
            return self.send(401)
        if MODE == "unauthorized":
            return self.send(401)
        if self.path == "/api/info":
            return self.send(503 if MODE == "never-healthy" else 200, {"data": {"healthy": True}})
        if self.path == "/api/session/active":
            polls["n"] += 1
            busy = MODE == "busy-forever" or (polls["n"] <= 2 and os.path.exists(LOG or "") and "prompt" in open(LOG).read())
            if MODE == "bad-active":
                return self.send(200, {"data": "nope"})
            if MODE == "list-active":
                return self.send(200, {"data": [{"id": "ses_1"}]})
            return self.send(200, {"data": {"ses_1": {"type": "running"}} if busy else {}})
        if self.path == "/api/session":  # sessions as the V2 list returns them: time.updated in epoch milliseconds
            old, new = {"id": "ses_0", "time": {"created": 1700000000000, "updated": 1700000100000}}, {"id": "ses_1", "time": {"created": 1700000000000, "updated": 1700000500000}}
            return self.send(200, {"data": [] if MODE == "no-sessions" else [old, new], "cursor": {}} if MODE != "bad-list" else {"oops": 1})
        if self.path == "/api/session/ses_1/message":
            return self.send(200, {"data": [{"type": "user", "text": "x"}, {"type": "assistant", "time": {"created": 1, "completed": 2}}]})
        self.send(404)

    def do_POST(self):
        if not self.headers.get("Authorization", "").startswith("Basic "):
            return self.send(401)
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/api/session":
            log("session")
            return self.send(200, {"data": {"id": "ses_1"}})
        if self.path == "/api/session/ses_1/prompt":
            log("prompt " + json.loads(data)["text"].strip())
            return self.send(204)
        self.send(404)


if os.environ.get("STUB_PID_FILE"):
    with open(os.environ["STUB_PID_FILE"], "w") as f:
        f.write(str(os.getpid()))
http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
