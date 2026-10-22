"""Cloud channel server: an HTTP listener in front of cloud_channel.handle (slice 3 of the plan on #1059).

Only the listener lives here; the reviewer and the inbox are passed in, and nothing is wired into the daemon.

    POST /message   JSON body {"sender", "recipient", "key", "text"}, header `Authorization: Bearer <token>`.

Rules:
    - No Authorization bearer header: 401, before the body is read.
    - A body over MAX_BODY bytes (declared or actually sent): 413, and no more than MAX_BODY+1 bytes are read.
    - A body that is not a JSON object: 400. Any other path 404, any other method 405.
    - Names reserved by the system (RESERVED) may be neither sender nor recipient: 403.
    - The listener starts only when configured: `listen_address(environ)` reads FLEET_CLOUD_CHANNEL_LISTEN ("host:port");
      unset or malformed gives None and `serve` refuses to start. No default address, and no token or key is stored here.
"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cloud_channel

MAX_BODY = 32768
RESERVED = frozenset({"captain", "switchboard", "fleet-switchboard", "hub", "admin"})
LISTEN_ENV = "FLEET_CLOUD_CHANNEL_LISTEN"
BEARER = "Bearer "


def listen_address(environ):
    """(host, port) from FLEET_CLOUD_CHANNEL_LISTEN, or None when it is unset or malformed."""
    value = (environ.get(LISTEN_ENV) or "").strip()
    match = re.fullmatch(r"([A-Za-z0-9.:-]+):([0-9]{1,5})", value)
    if not match or not 0 < int(match.group(2)) < 65536:
        return None
    return match.group(1), int(match.group(2))


def make_server(address, reviewer, inbox, audit):
    """A server bound to address (not started). Raises ValueError when address is None: not configured, no listener."""
    if address is None:
        raise ValueError("cloud channel listener is not configured (%s)" % LISTEN_ENV)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):   # the default line could carry request data
            pass

        def _send(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._send(405, {"status": 405, "error": "method not allowed"})
        do_PUT = do_DELETE = do_PATCH = do_GET

        def do_POST(self):
            if self.path != "/message":
                return self._send(404, {"status": 404, "error": "not found"})
            header = self.headers.get("Authorization")
            if not (header and header.startswith(BEARER) and header[len(BEARER):].strip()):
                audit("refused status=401 reason=no token")
                self.close_connection = True
                return self._send(401, {"status": 401, "error": "no token"})
            try:
                declared = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                declared = -1
            if declared < 0:
                self.close_connection = True
                return self._send(400, {"status": 400, "error": "bad length"})
            if declared > MAX_BODY:
                audit("refused status=413 reason=body too large")
                self.close_connection = True
                return self._send(413, {"status": 413, "error": "body too large"})
            raw = self.rfile.read(declared)
            try:
                body = json.loads(raw.decode("utf-8"))
            except ValueError:
                body = None
            if not isinstance(body, dict):
                audit("refused status=400 reason=body is not a JSON object")
                return self._send(400, {"status": 400, "error": "no body"})
            if body.get("sender") in RESERVED or body.get("recipient") in RESERVED:
                audit("refused status=403 reason=reserved name")
                return self._send(403, {"status": 403, "error": "reserved name"})
            result = cloud_channel.handle({"authorization": header, "body": body}, reviewer, inbox, audit)
            self._send(result["status"], result)

    return ThreadingHTTPServer(address, Handler)


def serve(environ, reviewer, inbox, audit):
    """Start listening when configured (blocks); returns False without binding anything when it is not."""
    address = listen_address(environ)
    if address is None:
        return False
    make_server(address, reviewer, inbox, audit).serve_forever()
    return True
