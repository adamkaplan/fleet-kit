#!/usr/bin/env python3
"""A stub headless server on loopback, for the driver tests. usage: stub_server.py PORT.
STUB_LOG (file): one line per event. STUB_PID_FILE: our pid. STUB_DRIP (seconds): the record's message read drips one byte per interval. STUB_MODE: ok (default) | never-healthy | busy-forever | die (exit 3 at start)."""
import http.server
import json
import os
import socketserver
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
            if LOG:   # a file beside the log: the driver's poll and the launcher's record are two processes
                with open(LOG + ".m", "a") as f:
                    f.write("x")
                with open(LOG + ".m") as f:
                    polls["m"] = len(f.read())
            else:
                polls["m"] = polls.get("m", 0) + 1
            if MODE == "messages-down" and polls["m"] > 1:
                return self.send(500)   # the first read (the poll that sees the session finish) works; the transcript read after it fails
            if os.environ.get("STUB_DRIP") and polls["m"] > 1:   # headers, then one byte every STUB_DRIP seconds: a read that never finishes
                import time
                self.send_response(200)
                self.send_header("Content-Length", str(8 * 1024 * 1024))
                self.end_headers()
                try:
                    while True:
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(float(os.environ["STUB_DRIP"]))
                except OSError:
                    pass
                return
            if os.environ.get("STUB_MESSAGES_RAW") and polls["m"] > 1:   # a hostile body, as bytes, for the record's read only
                raw = open(os.environ["STUB_MESSAGES_RAW"], "rb").read()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except OSError:
                    pass
                return
        if self.path == "/api/session/ses_1/message" and os.environ.get("STUB_MESSAGES"):
            return self.send(200, {"data": json.load(open(os.environ["STUB_MESSAGES"]))})   # a canned transcript
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

if MODE == "die":  # for the helper's own test: exit at start with an error on stderr
    sys.stderr.write("planted stub error\n")
    sys.exit(3)


class Server(http.server.ThreadingHTTPServer):
    # HTTPServer.server_bind calls socket.getfqdn(host): a reverse DNS lookup that can stall for many seconds on a hosted runner,
    # during which the port is bound but nothing accepts. The stub has no use for the name.
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[0], self.server_address[1]


Server(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
