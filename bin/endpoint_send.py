"""The send path's endpoint route (#1131): a message to an agent that holds no pane goes to the endpoint its registry row holds,
through the cloud channel client (bin/cloud_client.py). The pane route is not here and is not changed.

    route(state_dir, name) -> ("pane", None) | ("endpoint", url) | ("none", why) | ("half", why)

Nothing is sent on "none" or "half". The endpoint is an address only: the token never comes from it.
"""
import json
import re
import urllib.error
import urllib.request
from types import SimpleNamespace


def route(registry, state_dir, name):
    """How a send to `name` goes, by the registry alone. A half read or an unknown agent sends nothing and says why."""
    try:
        reg = registry.read(state_dir)
    except Exception as err:
        return "half", "the registry could not be read (%s): nothing sent" % type(err).__name__, False
    if not reg.get("whole"):
        found = registry.route_for((reg.get("agents") or {}).get(name))
        return "half", "the registry was only partly read: nothing sent to %s" % name, bool(found and found["via"] == "endpoint")
    row = reg["agents"].get(name)
    found = registry.route_for(row)
    if found is None:
        return "none", "%s is not in the registry with a pane or an endpoint: nothing sent" % name
    if found["via"] == "pane":
        return "pane", None
    return "endpoint", found["to"], row.get("state")


def blocked_reason(name, state, hold_reason):
    """The delivery rule held-while-blocked, with the pane route's own wording."""
    if state != "blocked":
        return None
    return hold_reason(SimpleNamespace(name=name, conflict=None, problem=None, status="blocked"))


MAX_TEXT = 16 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # a redirect is never followed: the bearer goes to the endpoint named, and nowhere else


def endpoint_ok(url):
    """https, or http only for an in-cluster service host (.svc, .svc.cluster.local, or a single label)."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if parts.scheme == "https":
        return bool(host)
    if parts.scheme == "http":
        return bool(host) and (host.endswith(".svc") or host.endswith(".svc.cluster.local") or re.fullmatch(r"[a-z0-9-]+", host) is not None)
    return False


def http_post(url, timeout=20):
    def post(request):
        data = json.dumps(request["body"]).encode()
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": request["authorization"], "Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as reply:
                return {"status": reply.status}
        except urllib.error.HTTPError as err:
            if 300 <= err.code < 400:
                return {"status": err.code, "error": "the endpoint answered with a redirect"}   # a named refusal, not retried
            return {"status": err.code}
    return post


def deliver(client, url, sender, name, text, post=None, **kwargs):
    """One send through the cloud client to `url`. Returns (ok, line)."""
    if not endpoint_ok(url):
        return False, "the registry endpoint is not an https address (http only for an in-cluster service host): nothing sent"
    if len(text.encode()) > MAX_TEXT:
        return False, "the text is over %d bytes: nothing sent" % MAX_TEXT
    return client.send(sender, name, text, post or http_post(url), **kwargs)
