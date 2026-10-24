#!/usr/bin/env python3
"""cloud_cli: the pod-side command to send and report through the cloud channel (uses bin/cloud_client.py).

    cloud_cli.py send RECIPIENT TEXT...
    cloud_cli.py report KIND TEXT...

Environment: FLEET_AGENT_NAME (sender), FLEET_BOSS (report target), FLEET_CLOUD_CHANNEL_URL (http URL of the channel),
FLEET_CLOUD_TOKEN_FILE (optional; default is the projected token path),
FLEET_CLOUD_ALLOW_HTTP_HOST (optional; the one non-loopback host that may be sent the token over plain http).
Prints one line. Exit 0 delivered, 1 refused, 2 bad usage or missing environment. Never prints the token.
"""
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cloud_client  # noqa: E402

KINDS = ("done", "failed", "blocked", "question", "working", "paused", "withdrawn")
MAX_REPORT = 300
MAX_SEND = 2000
TIMEOUT = 10


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # refuse every redirect: the token goes to the configured host only


OPENER = urllib.request.build_opener(_NoRedirect)


def make_post(url):
    def post(request):
        data = json.dumps(request["body"]).encode()
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": request["authorization"], "Content-Type": "application/json"})
        try:
            with OPENER.open(req, timeout=TIMEOUT) as answer:
                status, raw = answer.status, answer.read(65536)
        except urllib.error.HTTPError as err:
            status = err.code
            try:
                raw = err.read(65536)
            except Exception:
                raw = b""
        except Exception as err:
            return {"status": 0, "error": type(err).__name__}
        try:
            body = json.loads(raw.decode() or "{}")
        except ValueError:
            body = {}
        out = dict(body) if isinstance(body, dict) else {}
        out["status"] = status
        if status != 200:
            out["error"] = str(out.get("error", out.get("reason", "")))
        return out
    return post


def _http_allowed(parts, env):
    """The bearer token goes over plain http only to a loopback host, or to the host named by FLEET_CLOUD_ALLOW_HTTP_HOST."""
    try:
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    try:
        if ipaddress.ip_address(host).is_loopback:
            return True
    except ValueError:
        pass
    return host == "localhost" or (bool(host) and host == env.get("FLEET_CLOUD_ALLOW_HTTP_HOST", "").strip().lower())


def _usage(line):
    print(line)
    return 2


def main(argv, env=None, post=None):
    env = os.environ if env is None else env
    args = list(argv)
    if len(args) < 3 or args[0] not in ("send", "report"):
        return _usage("usage: cloud_cli.py send RECIPIENT TEXT... | report KIND TEXT...")
    cmd, first, text = args[0], args[1], " ".join(args[2:]).strip()
    if not text:
        return _usage("cloud_cli: empty text")
    sender = env.get("FLEET_AGENT_NAME", "").strip()
    if not sender:
        return _usage("cloud_cli: FLEET_AGENT_NAME is not set")
    url = env.get("FLEET_CLOUD_CHANNEL_URL", "").strip()
    try:
        parts = urllib.parse.urlparse(url)
    except ValueError:
        return _usage("cloud_cli: FLEET_CLOUD_CHANNEL_URL must be set to an http or https URL")
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return _usage("cloud_cli: FLEET_CLOUD_CHANNEL_URL must be set to an http or https URL")
    if "@" in parts.netloc:
        return _usage("cloud_cli: FLEET_CLOUD_CHANNEL_URL must not contain user information")
    if parts.scheme == "http" and not _http_allowed(parts, env):
        return _usage("cloud_cli: plain http to a host that is not loopback is refused (set FLEET_CLOUD_ALLOW_HTTP_HOST to that host to allow it)")
    if cmd == "send" and len(text) > MAX_SEND:
        return _usage("cloud_cli: text over %d characters" % MAX_SEND)
    token_path = env.get("FLEET_CLOUD_TOKEN_FILE") or cloud_client.TOKEN_PATH
    post = post or make_post(url)
    if cmd == "send":
        ok, err = cloud_client.send(sender, first, text, post, token_path=token_path)
        done = "sent to %s" % first
    else:
        if first not in KINDS:
            return _usage("cloud_cli: kind must be one of " + " ".join(KINDS))
        if len(text) > MAX_REPORT:
            return _usage("cloud_cli: report over %d characters" % MAX_REPORT)
        boss = env.get("FLEET_BOSS", "").strip()
        if not boss:
            return _usage("cloud_cli: FLEET_BOSS is not set")
        ok, err = cloud_client.report(sender, boss, "%s: %s" % (first, text), post, token_path=token_path)
        done = "reported %s to %s" % (first, boss)
    if ok:
        print(done)
        return 0
    print((err or "cloud channel: refused").splitlines()[0])
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
