"""endpoint_liveness (#1181): the liveness of a row that has no pane to judge. One bounded request to the row's endpoint, the same read `fleet-switchboard read NAME`
makes (a session's newest message). The answer is `alive` with the time it answered, or `unknown`: NEVER `dead`. A failed, slow, refused or partial answer is
`unknown` and changes nothing. The credential is the cloud channel's own token file (bin/cloud_client.py), never a value stored in the row.

    check(name, row, ...) -> {"verdict": "alive", "at": epoch} | {"verdict": "unknown", "why": text}
    check_all(rows, ...)  -> {name: answer}: one request per row, all at once, never waiting longer than LIVENESS_SECONDS in all.
"""
import importlib.util
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

LIVENESS_SECONDS = 5   # the one named time limit: a request, and the whole pass's wait for all of them


def _client():
    path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "cloud_client.py")
    spec = importlib.util.spec_from_file_location("cloud_client_for_liveness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _http_get(url, authorization, timeout):
    request = urllib.request.Request(url, method="GET", headers={"Authorization": authorization, "Accept": "application/json"})
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as reply:
        return reply.status, reply.read(65537)


def address_ok(url):
    """The bearer goes to https, or to http only for loopback or an in-cluster host. Anything else is unknown without a request."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        parts.port
    except ValueError:
        return False
    if parts.scheme == "https":
        return bool(host)
    return parts.scheme == "http" and bool(host) and (host in ("localhost", "127.0.0.1", "::1") or host.endswith(".svc") or host.endswith(".svc.cluster.local")
                                                       or re.fullmatch(r"[a-z0-9-]+", host) is not None)


def unknown(why):
    return {"verdict": "unknown", "why": why}


def check(name, row, limit=LIVENESS_SECONDS, token_path=None, get=None, clock=time.time):
    """One request. Never raises, never says dead."""
    try:
        endpoint = row.get("endpoint") if isinstance(row, dict) else None
        session = row.get("session") if isinstance(row, dict) else None
        if not isinstance(endpoint, str) or not endpoint:
            return unknown("no endpoint")
        if not address_ok(endpoint):
            return unknown("the endpoint is not an https or in-cluster address")
        client = _client()
        token = client._read_token(token_path or os.environ.get("FLEET_CLOUD_TOKEN_FILE") or client.TOKEN_PATH)
        target = endpoint.rstrip("/") + "/session/" + urllib.parse.quote(str(session or name), safe="") + "/message?limit=1&order=desc"
        status, raw = (get or _http_get)(target, "Bearer " + token, limit)
        if status != 200 or len(raw) > 65536:
            return unknown("the endpoint answered %s" % status)
        body = json.loads(raw.decode("utf-8"))
        if not isinstance(body, (list, dict)):
            return unknown("the answer is not a read")
        return {"verdict": "alive", "at": int(clock())}
    except Exception as err:   # failed, slow, refused, partial, no token: unknown
        return unknown(type(err).__name__)


def check_all(rows, limit=LIVENESS_SECONDS, token_path=None, get=None, clock=time.time):
    """{name: answer} for {name: row}: one request per row in its own thread, the pass waits at most `limit` seconds in all; a row not answered by then is unknown."""
    out = {name: unknown("no answer within %ss" % limit) for name in rows}
    lock = threading.Lock()

    def run(name, row):
        answer = check(name, row, limit, token_path, get, clock)
        with lock:
            out[name] = answer
    threads = [threading.Thread(target=run, args=(n, r), daemon=True) for n, r in sorted(rows.items())]
    for t in threads:
        t.start()
    end = time.monotonic() + limit
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    with lock:
        return dict(out)
