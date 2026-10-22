"""Cloud hub: the cloud switchboard's front door (issue 1148; builds on cloud_channel.py and cloud_channel_server.py).

    POST /message    the channel server's route, unchanged: token review, sender pinned to the token, then DELIVERY here.
    GET  /decisions  the JSON object bin/decisions_pull.py accepts, read from the file named by FLEET_CLOUD_HUB_DECISIONS
                     ({"decisions": [], "errors": []} when the file is absent). The same token review guards it.

Identity: an agent's bearer token is posted to the in-cluster TokenReview API (the hub's own service account token and CA);
the authenticated user is mapped to the agent name by cloud_channel.agent_of (namespace pinned), never from a request body.
Delivery: FLEET_CLOUD_HUB_AGENTS names a JSON file {agent: {"url", "session", "password_file"[, "issue"]}}. A message becomes
a prompt on that session (POST /api/session/<session>/prompt, delivery "queue", metadata {"fleet": {"from", "issue"}}) with
Basic auth read from the password file, under a timeout. An agent not in the map is refused 404. A repeat key is a duplicate.
The listener starts only when FLEET_CLOUD_CHANNEL_LISTEN is set. Audit lines go to stdout as JSON lines; no token is logged.
The HTTP call to the cluster is an injected function http(url, headers, data, timeout) -> dict, so tests use a fake.
"""
import base64
import http.client
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request

import cloud_channel
import cloud_channel_server

AGENTS_ENV = "FLEET_CLOUD_HUB_AGENTS"
DECISIONS_ENV = "FLEET_CLOUD_HUB_DECISIONS"
AUDIENCE_ENV = "FLEET_CLOUD_CHANNEL_AUDIENCE"
API = "https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews"
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
TIMEOUT = 10.0
MAX_FILE = 8 * 1024 * 1024
SESSION = re.compile(r"[A-Za-z0-9_-]{1,100}")


class Refused(Exception):
    """The agent cannot be delivered to (not in the map)."""


def kube_http(url, headers, data, timeout, ca_file=SA_DIR + "/ca.crt"):
    """POST data (bytes) and return the decoded JSON answer; raises on any failure."""
    context = ssl.create_default_context(cafile=ca_file)
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout, context=context) as answer:
        return json.loads(answer.read(MAX_FILE).decode("utf-8"))


def make_reviewer(http=kube_http, audience=None, sa_dir=SA_DIR, api=API, timeout=TIMEOUT):
    """reviewer(token, audience) for cloud_channel.handle. `audience` (FLEET_CLOUD_CHANNEL_AUDIENCE) is what the token must be
    issued for; handle() passes its own constant, so the audience the cluster confirms is reported back under that name."""
    def reviewer(token, handle_audience):
        wanted = audience or handle_audience
        with open(os.path.join(sa_dir, "token")) as fh:
            own = fh.read().strip()
        body = {"apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview",
                "spec": {"token": token, "audiences": [wanted]}}
        answer = http(api, {"Authorization": "Bearer " + own, "Content-Type": "application/json"},
                      json.dumps(body).encode(), timeout)
        status = answer.get("status") if isinstance(answer, dict) else None
        status = dict(status) if isinstance(status, dict) else {}
        audiences = status.get("audiences")
        if isinstance(audiences, list) and wanted in audiences:
            status["audiences"] = list(audiences) + [handle_audience]
        return status
    return reviewer


def _read_json(path, default):
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_FILE + 1)
    except FileNotFoundError:
        return default
    if len(raw) > MAX_FILE:
        raise ValueError("file too large")
    return json.loads(raw.decode("utf-8"))


def _password(path):
    with open(path) as fh:
        password = fh.read().rstrip("\n")
    if not password or re.search(r"[\x00-\x1f\x7f]", password):
        raise ValueError("no usable password")
    return password


class Inbox:
    """put_if_absent delivers to the agent's OpenCode session. Unknown agent: Refused (answered 404 by the hub)."""

    def __init__(self, agents_path, timeout=TIMEOUT):
        self.agents_path = agents_path
        self.timeout = timeout
        self.seen = set()
        self.lock = threading.Lock()

    def put_if_absent(self, recipient, dedupe_key, item):
        agents = _read_json(self.agents_path, {}) if self.agents_path else {}
        record = agents.get(recipient) if isinstance(agents, dict) else None
        if not isinstance(record, dict):
            raise Refused(recipient)
        parts = urllib.parse.urlsplit(str(record.get("url")))
        session = str(record.get("session"))
        if parts.scheme not in ("http", "https") or not parts.hostname or not SESSION.fullmatch(session):
            raise ValueError("bad agent record")
        with self.lock:
            if (recipient, dedupe_key) in self.seen:
                return False
            self.seen.add((recipient, dedupe_key))
        try:
            self._post(parts, session, record, item)
        except BaseException:
            with self.lock:
                self.seen.discard((recipient, dedupe_key))
            raise
        return True

    def _post(self, parts, session, record, item):
        fleet = {"from": item["sender"], "key": item["key"]}
        if record.get("issue") is not None:
            fleet["issue"] = record["issue"]
        data = json.dumps({"text": item["text"], "metadata": {"fleet": fleet}, "delivery": "queue"}).encode()
        auth = base64.b64encode(("opencode:" + _password(record["password_file"])).encode()).decode()
        cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = cls(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), timeout=self.timeout)
        try:
            conn.request("POST", "/api/session/%s/prompt" % session, data,
                         {"Authorization": "Basic " + auth, "Content-Type": "application/json"})
            answer = conn.getresponse()
            answer.read(65536)
            if not 200 <= answer.status < 300:
                raise RuntimeError("agent answered %d" % answer.status)
        finally:
            conn.close()


def json_audit(stream=None):
    def audit(line):
        (stream or sys.stdout).write(json.dumps({"ts": round(time.time(), 3), "line": str(line)}) + "\n")
        (stream or sys.stdout).flush()
    return audit


def make_hub(address, reviewer, inbox, audit, decisions_path=None, max_handlers=None):
    """The channel server with the hub's two additions: GET /decisions and 404 for an agent not in the map."""
    server = cloud_channel_server.make_server(address, reviewer, inbox, audit, max_handlers)
    base = server.RequestHandlerClass
    local = threading.local()

    class Hub(base):
        def _send(self, status, payload):
            if status == 503 and getattr(local, "unknown", False):
                status, payload = 404, {"status": 404, "error": "unknown agent"}
                audit("refused status=404 reason=agent not in the map")
            local.unknown = False
            super()._send(status, payload)

        def do_POST(self):
            local.unknown = False
            super().do_POST()

        def do_GET(self):
            self.close_connection = True
            if self.path != "/decisions":
                return self._send(404, {"status": 404, "error": "not found"})
            header = self.headers.get("Authorization")
            if not (header and header.startswith(cloud_channel.BEARER) and header[len(cloud_channel.BEARER):].strip()):
                audit("refused status=401 reason=no token")
                return self._send(401, {"status": 401, "error": "no token"})
            seen = []
            probe = cloud_channel.handle({"authorization": header, "body": {}}, reviewer, inbox, seen.append)
            if probe["status"] != 403:   # 403 (sender mismatch on the empty body) is what an authenticated caller gets
                for line in seen:
                    audit(line)
                return self._send(probe["status"], probe)
            try:
                body = _read_json(decisions_path, {"decisions": [], "errors": []}) if decisions_path else {"decisions": [], "errors": []}
                if not isinstance(body, dict):
                    raise ValueError("not an object")
            except (OSError, ValueError):
                audit("refused status=503 reason=decisions unreadable")
                return self._send(503, {"status": 503, "error": "decisions unreadable"})
            audit("decisions served")
            self._send(200, body)

    # an unknown agent is told apart from a failing delivery by the inbox's own exception, noted per thread
    original = inbox.put_if_absent

    def noting(recipient, dedupe_key, item):
        try:
            return original(recipient, dedupe_key, item)
        except Refused:
            local.unknown = True
            raise
    inbox.put_if_absent = noting
    server.RequestHandlerClass = Hub
    return server


def serve(environ, reviewer=None, inbox=None, audit=None):
    """Start only when FLEET_CLOUD_CHANNEL_LISTEN is set (blocks); False, binding nothing, when it is not."""
    address = cloud_channel_server.listen_address(environ)
    if address is None:
        return False
    reviewer = reviewer or make_reviewer(audience=environ.get(AUDIENCE_ENV) or None)
    inbox = inbox or Inbox(environ.get(AGENTS_ENV))
    make_hub(address, reviewer, inbox, audit or json_audit(), environ.get(DECISIONS_ENV)).serve_forever()
    return True


if __name__ == "__main__":
    sys.exit(0 if serve(os.environ) else 2)
