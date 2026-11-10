#!/usr/bin/env python3
"""The council gateway (ask #2322): a worker pod's model requests go through here, so a pod never holds the provider key.

Standard library only. It reads ONE settings file (JSON; the path is argv[1] or COUNCIL_GATEWAY_SETTINGS) and the provider key from the file the
settings name. It answers ONE route, POST /chat/completions (the OpenAI-style request the land tool's council sends), and forwards the body
UNCHANGED to <provider_url>/chat/completions with the header `Authorization: Bearer <key>` (the caller's own Authorization header is dropped).
The allow-list is exactly anthropic/claude-haiku-5.5 and anthropic/claude-sonnet-5.5. Any model id containing "opus" is banned: 403 with
the error "model banned from the council: opus", never forwarded. For sonnet the gateway FORCES reasoning.effort to "medium" (the caller's value is
overwritten; the body is re-encoded); haiku passes the caller's value and its body goes on unchanged. GET /usage (pods may read it) answers the
counts of forwarded requests per UTC day and per model: no body, no key.
Refused before anything is forwarded: any other path or method (403), a model outside the allow-list (403), a body over max_request_chars (413),
a bad body, a missing or over-large max_tokens, or a streaming request (400). Past daily_budget requests in one UTC day: 429 with the reason.
The day's count is in memory: a restart resets it. One count line per request goes to the log; it never holds a request body, an answer body or the key.
"""
import datetime
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PATH = "/chat/completions"
UPSTREAM_SECONDS = 155        # the council's client gives up at 150 s: nothing is buffered past 160 s
ANSWER_MAX_BYTES = 4 * 1024 * 1024
SOCKET_SECONDS = 30            # a client that stalls while sending its request is cut off
MAX_IN_FLIGHT = 32             # more requests than this at once get 503
READ_SECONDS = 30              # the whole request must arrive within this (a trickle holds nothing longer)
ALLOWED_FIELDS = ("model", "temperature", "max_tokens", "reasoning", "messages", "tools")   # exactly what the council sends; any other field is refused
EFFORT_RE = re.compile(r"[a-z]{1,16}")
BAN_WORD = "opus"
BAN_ERROR = "model banned from the council: " + BAN_WORD
FORCED_EFFORT = {"sonnet": "medium"}   # model-id word -> the effort the gateway writes whatever the caller sent
USAGE_PATH = "/usage"
SETTING_KEYS = ("models", "max_request_chars", "max_answer_tokens", "daily_budget", "provider_url", "key_file")
LOOPBACK_RE = re.compile(r"http://(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?(?:/.*)?")
URL_RE = re.compile(r"https?://[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?(?:/[A-Za-z0-9._/-]*)?")


class SettingsError(Exception):
    pass


def load_settings(path):
    """The settings as a dict, or SettingsError (the message names a key, never a value)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as err:
        raise SettingsError("the settings file cannot be read as JSON (%s)" % type(err).__name__) from None
    if not isinstance(data, dict):
        raise SettingsError("the settings file must hold one JSON object")
    for key in SETTING_KEYS:
        if key not in data:
            raise SettingsError("the setting %s is missing" % key)
    models = data["models"]
    if not (isinstance(models, list) and models and all(isinstance(m, str) and m for m in models)):
        raise SettingsError("models must be a non-empty list of model names")
    if any(BAN_WORD in m.lower() for m in models):
        raise SettingsError("models must not hold a banned model (%s)" % BAN_WORD)
    for key in ("max_request_chars", "max_answer_tokens", "daily_budget"):
        if type(data[key]) is not int or data[key] < 1:
            raise SettingsError("%s must be a whole number above 0" % key)
    if not (isinstance(data["provider_url"], str) and URL_RE.fullmatch(data["provider_url"])):
        raise SettingsError("provider_url must be an http or https URL without user, query or fragment")
    if not data["provider_url"].startswith("https://") and not LOOPBACK_RE.fullmatch(data["provider_url"]):
        raise SettingsError("provider_url must be https (http only for a loopback address): the key travels in it")
    if not (isinstance(data["key_file"], str) and os.path.isabs(data["key_file"])):
        raise SettingsError("key_file must be an absolute path")
    return data


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the key's header to another host: none is followed (the 3xx is an error)."""
    def redirect_request(self, *args, **kw):
        return None


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _no_constant(name):
    raise ValueError("not a JSON number")


class Gateway:
    def __init__(self, settings, log=None, clock=None, opener=None):
        self.s = settings
        self.log = log or (lambda line: (sys.stderr.write(line + "\n"), sys.stderr.flush()))
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc).date())
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
        self.slots = threading.BoundedSemaphore(MAX_IN_FLIGHT)
        self.lock = threading.Lock()
        self.day, self.count = None, 0
        self.usage = {}   # "YYYY-MM-DD" -> {model: forwarded-request count}

    def usage_body(self):
        """Counts per day and per model as JSON bytes: nothing else."""
        with self.lock:
            days = {d: dict(m) for d, m in self.usage.items()}
        return json.dumps({"days": days}, sort_keys=True).encode()

    def take(self, model="-"):
        """Count one request against today's budget: (allowed, number used today)."""
        with self.lock:
            today = self.clock()
            if today != self.day:
                self.day, self.count = today, 0
            if self.count >= self.s["daily_budget"]:
                return False, self.count
            self.count += 1
            per_day = self.usage.setdefault(today.isoformat(), {})
            per_day[model] = per_day.get(model, 0) + 1
            return True, self.count

    def key(self):
        with open(self.s["key_file"], encoding="utf-8") as f:
            return f.read().strip()

    def forward(self, body):
        """(status, answer bytes) from the provider; the key is added here and goes nowhere else."""
        try:
            req = urllib.request.Request(self.s["provider_url"].rstrip("/") + PATH, data=body, method="POST",
                                         headers={"Authorization": "Bearer " + self.key(), "content-type": "application/json"})
            with self.opener.open(req, timeout=UPSTREAM_SECONDS) as res:
                answer = res.read(ANSWER_MAX_BYTES + 1)
                return (res.status, answer) if len(answer) <= ANSWER_MAX_BYTES else (502, None)
        except urllib.error.HTTPError as err:
            if err.code in (400, 404, 408, 409, 413, 422, 429) or err.code >= 500:
                return err.code, ("the provider answered status %d" % err.code).encode()   # its own error text is not passed on
            return 502, None   # a redirect, an authorization refusal: the provider's text may speak of the key
        except (OSError, ValueError):
            return 502, None

    def handle(self, method, path, length, read_body):
        """(status, reason or None, body bytes or None, model or '-', used today). read_body(n) reads the request."""
        s = self.s
        if method == "GET" and path == USAGE_PATH:
            return 200, None, self.usage_body(), "-", 0
        if method != "POST" or path != PATH:
            return 403, "only POST " + PATH + " and GET " + USAGE_PATH + " are served", None, "-", 0
        if length is None:
            return 400, "a Content-Length is required", None, "-", 0
        if length > s["max_request_chars"] * 4:
            return 413, "the request is over the limit of %d characters" % s["max_request_chars"], None, "-", 0
        try:
            raw = read_body(length)
        except (OSError, TimeoutError):
            return 408, "the request did not arrive in time", None, "-", 0
        try:
            text = raw.decode("utf-8")
            data = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
        except (ValueError, RecursionError):
            return 400, "the body is not JSON", None, "-", 0
        if len(text) > s["max_request_chars"]:
            return 413, "the request is over the limit of %d characters" % s["max_request_chars"], None, "-", 0
        if not isinstance(data, dict):
            return 400, "the body must be a JSON object", None, "-", 0
        model = data.get("model")
        if isinstance(model, str) and BAN_WORD in model.lower():
            return 403, BAN_ERROR, None, "-", 0
        if not (isinstance(model, str) and model in s["models"]):
            return 403, "the model is not on the allow-list", None, "-", 0
        tokens = data.get("max_tokens")
        if type(tokens) is not int or not 1 <= tokens <= s["max_answer_tokens"]:
            return 400, "max_tokens must be a whole number from 1 to %d" % s["max_answer_tokens"], None, model, 0
        if data.get("stream"):
            return 400, "streaming is not served", None, model, 0
        reasoning = data.get("reasoning", {})
        if (any(f not in ALLOWED_FIELDS for f in data) or not isinstance(reasoning, dict) or set(reasoning) - {"effort"}
                or not (isinstance(reasoning.get("effort", "low"), str) and EFFORT_RE.fullmatch(reasoning.get("effort", "low")))):
            return 400, "the request holds a field the council does not send (allowed: %s; reasoning only effort)" % ", ".join(ALLOWED_FIELDS), None, model, 0
        if not self.slots.acquire(blocking=False):
            return 503, "too many requests at once", None, model, 0
        try:
            ok, used = self.take(model)
            if not ok:
                return 429, "the daily budget of %d requests is spent; it renews at 00:00 UTC" % s["daily_budget"], None, model, used
            for word, effort in FORCED_EFFORT.items():
                if word in model:
                    data["reasoning"] = {"effort": effort}
                    raw = json.dumps(data).encode()
            status, answer = self.forward(raw)
        finally:
            self.slots.release()
        if answer is None or (status >= 400 and answer.startswith(b"the provider answered")):
            note = answer.decode() if answer else "the provider could not be reached"
            return (status if answer else 502), note, None, model, used
        return status, None, answer, model, used


def make_handler(gw):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "council-gateway"
        timeout = SOCKET_SECONDS

        def log_message(self, *args):   # the library's own access line carries the path: silenced
            pass

        def read_whole(self, n):
            """n bytes of the request body within READ_SECONDS in all (not per read), else TimeoutError."""
            deadline, got = time.monotonic() + READ_SECONDS, b""
            while len(got) < n:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                self.connection.settimeout(left)
                chunk = self.rfile.read1(min(65536, n - len(got))) if hasattr(self.rfile, "read1") else self.rfile.read(min(65536, n - len(got)))
                if not chunk:
                    raise TimeoutError
                got += chunk
            return got

        def serve(self):
            raw_length = self.headers.get("Content-Length")
            length = int(raw_length) if raw_length and raw_length.isascii() and raw_length.isdigit() and len(raw_length) < 12 else None
            status, reason, answer, model, used = gw.handle(self.command, self.path, length, self.read_whole)
            if answer is None:
                answer = json.dumps({"error": {"message": reason}}).encode()
            self.close_connection = True   # a refusal may leave an unread body behind: one request per connection
            line = "council-gateway status=%d model=%s used_today=%d budget=%d bytes_in=%d" % (status, model, used, gw.s["daily_budget"], length or 0)
            gw.log(line)
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(answer)))
            self.send_header("connection", "close")
            self.end_headers()
            self.wfile.write(answer)

        do_POST = do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = serve
    return Handler


def make_server(settings, host="0.0.0.0", port=8080, **kw):
    gw = Gateway(settings, **kw)
    server = ThreadingHTTPServer((host, port), make_handler(gw))
    server.daemon_threads = True
    server.gateway = gw
    return server


def main(argv):
    path = argv[1] if len(argv) > 1 else os.environ.get("COUNCIL_GATEWAY_SETTINGS", "")
    try:
        settings = load_settings(path)
    except SettingsError as err:
        sys.stderr.write("council-gateway: refused to start: %s\n" % err)
        return 2
    port = int(os.environ.get("COUNCIL_GATEWAY_PORT", "8080"))
    server = make_server(settings, port=port)
    sys.stderr.write("council-gateway: listening on port %d\n" % port)
    sys.stderr.flush()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
