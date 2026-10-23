"""Cloud channel server: an HTTP listener in front of cloud_channel.handle (slice 3 of the plan on #1059).

Only the listener lives here; the reviewer and the inbox are passed in, and nothing is wired into the daemon.

    POST /message   JSON body {"sender", "recipient", "key", "text"}, header `Authorization: Bearer <token>`.

Transport stance: plain HTTP, no TLS here. The real boundary is the cluster network (a NetworkPolicy admitting only agent
pods to this port). A bad token is judged in cloud_channel.handle (the token review and the sender-to-token check); this
module only refuses a request that carries no bearer token at all.
Address: FLEET_CLOUD_CHANNEL_LISTEN may name any host, loopback or not: a listener inside a pod must bind the pod address.
Nothing is defaulted, so an unset variable binds nothing.

What is read, in order, and what is refused at each step (every refusal made before the body is read closes the connection):
    1. Request line and headers only. Any path but /message: 404; any method but POST: 405.
    2. No Authorization bearer header: 401, before anything of the body is read (so an oversized or malformed body on an
       unauthenticated request still gives 401).
    3. Any Transfer-Encoding header: 400 (only a Content-Length body is accepted). A Content-Length that is not plain digits: 400.
       A declared length over MAX_BODY: 413. Neither is read.
    4. Exactly Content-Length bytes (at most MAX_BODY) are read; a missing Content-Length means an empty body, which is a 400.
       A body that stalls or is short for longer than REQUEST_TIMEOUT seconds on one read, or whose connection has lived
       longer than TOTAL_DEADLINE seconds in all (headers and body, so a trickling client cannot hold a slot): 408.
       (A deadline that falls while headers are still arriving drops the connection without a reply.)
    5. A body that is not a JSON object: 400. A sender or recipient that is not a string: 400. A sender or recipient that is
       reserved (RESERVED, compared stripped and case-folded): 403. The token is judged first: the request is run through
       cloud_channel.handle with an empty body, and a 401 or 503 from that is the answer, so an unauthenticated caller never
       sees a 400 or 403 for names and writes no such audit line.
    6. cloud_channel.handle decides the rest (token, identity, delivery).
Concurrency: at most MAX_HANDLERS requests are handled at once; one more is audited and closed (503, no reply body, so the accept thread never waits on a client). Handler threads are daemons.
The listener starts only when configured: `listen_address(environ)` gives None for unset or malformed, and `serve` then
refuses to start. No token or key is stored here and none is ever echoed or logged.
"""
import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cloud_channel

MAX_BODY = 32768
RESERVED = frozenset({"captain", "switchboard", "fleet-switchboard", "hub", "admin"})
LISTEN_ENV = "FLEET_CLOUD_CHANNEL_LISTEN"
REQUEST_TIMEOUT = 5.0   # per socket read
TOTAL_DEADLINE = 10.0   # wall clock for one whole connection, headers and body together
MAX_HANDLERS = 32
BEARER = cloud_channel.BEARER


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, max_handlers, audit=lambda line: None):
        super().__init__(address, handler)
        self.audit = audit
        self.max_handlers = max_handlers
        self._slots = threading.Semaphore(max_handlers)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            # no reply on the accept thread (a slow client must not stall accepting): audit, then close without a body
            self.audit("refused status=503 reason=busy: at the cap of %d concurrent requests" % self.max_handlers)
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def listen_address(environ):
    """(host, port) from FLEET_CLOUD_CHANNEL_LISTEN, or None when it is unset or malformed."""
    value = (environ.get(LISTEN_ENV) or "").strip()
    match = re.fullmatch(r"([A-Za-z0-9.:-]+):([0-9]{1,5})", value)
    if not match or not 0 < int(match.group(2)) < 65536:
        return None
    return match.group(1), int(match.group(2))


def make_server(address, reviewer, inbox, audit, max_handlers=None):
    """A server bound to address (not started). Raises ValueError when address is None: not configured, no listener."""
    if address is None:
        raise ValueError("cloud channel listener is not configured (%s)" % LISTEN_ENV)

    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT
        deadline = TOTAL_DEADLINE
        expired = False

        def _expire(self):
            self.expired = True
            self.close_connection = True
            try:   # wakes a blocked read (it then sees end of input); the write side stays open for the 408
                self.connection.shutdown(socket.SHUT_RD)
            except OSError:
                pass

        def handle(self):
            timer = threading.Timer(self.deadline, self._expire)
            timer.daemon = True
            timer.start()
            try:
                super().handle()
            finally:
                timer.cancel()

        def log_message(self, *args):   # the default line could carry request data
            pass

        def _send(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _refuse_early(self, status, error, reason):
            audit("refused status=%d reason=%s" % (status, reason))
            self.close_connection = True
            self._send(status, {"status": status, "error": error})

        def do_GET(self):
            self.close_connection = True
            self._send(405, {"status": 405, "error": "method not allowed"})
        do_PUT = do_DELETE = do_PATCH = do_GET

        def do_POST(self):
            if self.path != "/message":
                self.close_connection = True
                return self._send(404, {"status": 404, "error": "not found"})
            header = self.headers.get("Authorization")
            if not (header and header.startswith(BEARER) and header[len(BEARER):].strip()):
                return self._refuse_early(401, "no token", "no token")
            if self.headers.get("Transfer-Encoding") is not None:
                return self._refuse_early(400, "transfer-encoding not accepted", "transfer-encoding")
            length = (self.headers.get("Content-Length", "0") or "0").strip()
            if not (length.isascii() and length.isdigit()):
                return self._refuse_early(400, "bad length", "bad length")
            declared = int(length)
            if declared > MAX_BODY:
                return self._refuse_early(413, "body too large", "body too large")
            try:
                raw = self.rfile.read(declared)
                if len(raw) < declared and self.expired:
                    raise TimeoutError("deadline")
                if len(raw) < declared:
                    raise TimeoutError("short body")
            except (TimeoutError, socket.timeout):
                return self._refuse_early(408, "request timeout", "body timeout")
            try:
                body = json.loads(raw.decode("utf-8"))
            except ValueError:
                body = None
            refusal = None
            if isinstance(body, dict):
                names = (body.get("sender"), body.get("recipient"))
                if not all(isinstance(n, str) for n in names):
                    refusal = (400, "sender and recipient must be strings", "sender or recipient is not a string")
                elif any(n.strip().casefold() in RESERVED for n in names):
                    refusal = (403, "reserved name", "reserved name")
            if refusal:
                # judge the token first: handle() with an empty body stops right after authentication (403 for a good
                # token, 401/503 otherwise); only an unauthenticated verdict is returned, with its own audit line
                seen = []
                probe = cloud_channel.handle({"authorization": header, "body": {}}, reviewer, inbox, seen.append)
                if probe["status"] in (401, 503):
                    for line in seen:
                        audit(line)
                    return self._send(probe["status"], probe)
                audit("refused status=%d reason=%s" % (refusal[0], refusal[2]))
                return self._send(refusal[0], {"status": refusal[0], "error": refusal[1]})
            result = cloud_channel.handle({"authorization": header, "body": body}, reviewer, inbox, audit)
            self._send(result["status"], result)

    return _Server(address, Handler, max_handlers or MAX_HANDLERS, audit)


def serve(environ, reviewer, inbox, audit):
    """Start listening when configured (blocks); returns False without binding anything when it is not."""
    address = listen_address(environ)
    if address is None:
        return False
    make_server(address, reviewer, inbox, audit).serve_forever()
    return True
