"""The send path's endpoint route (#1131): a message to an agent that holds no pane goes to the endpoint its registry row holds,
through the cloud channel client (bin/cloud_client.py). The pane route is not here and is not changed.

    route(state_dir, name) -> ("pane", None) | ("endpoint", url) | ("none", why) | ("half", why)

Nothing is sent on "none" or "half". The endpoint is an address only: the token never comes from it.
"""
import json
import urllib.request
from types import SimpleNamespace


def route(registry, state_dir, name):
    """How a send to `name` goes, by the registry alone. A half read or an unknown agent sends nothing and says why."""
    try:
        reg = registry.read(state_dir)
    except Exception as err:
        return "half", "the registry could not be read (%s): nothing sent" % type(err).__name__
    if not reg.get("whole"):
        return "half", "the registry was only partly read: nothing sent to %s" % name
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


def http_post(url, timeout=20):
    def post(request):
        data = json.dumps(request["body"]).encode()
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": request["authorization"], "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as reply:
                return {"status": reply.status}
        except urllib.error.HTTPError as err:
            return {"status": err.code}
    return post


def deliver(client, url, sender, name, text, post=None, **kwargs):
    """One send through the cloud client to `url`. Returns (ok, line)."""
    if not url.lower().startswith(("http://", "https://")):
        return False, "the registry endpoint is not an http(s) address: nothing sent"
    return client.send(sender, name, text, post or http_post(url), **kwargs)
