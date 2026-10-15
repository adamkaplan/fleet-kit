#!/usr/bin/env python3
"""fleet-token-mint: mint a short-lived GitHub token inside the pod, at start and on demand.

POSTs to FLEET_TOKEN_ENDPOINT, authenticated by the pod's federated identity token (a projected file the
platform rotates), and writes the token to a tmpfs file (mode 0600). A reply that lives longer than 60 minutes
is refused. The token is never printed and never placed in the environment.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
import datetime as dt

MAX_SECONDS = 3600
IDENTITY_FILE = "/var/run/secrets/azure/tokens/azure-identity-token"
OUT_FILE = "/run/fleet-tmp/github-token"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None  # the identity token goes to the named endpoint only


def _open(req, timeout):
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


open_no_redirect = _open  # the one HTTP opener of the cloud programs: it never follows a redirect


def mint(endpoint, identity_file=IDENTITY_FILE, out_file=OUT_FILE, now=None, opener=_open):
    u = urllib.parse.urlparse(endpoint)
    if u.scheme != "https" and u.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError("the token endpoint must be https")
    with open(identity_file) as f:
        identity = f.read().strip()
    req = urllib.request.Request(endpoint, data=b"{}", method="POST",
                                 headers={"Authorization": "Bearer " + identity, "Content-Type": "application/json"})
    with opener(req, timeout=30) as r:
        reply = json.loads(r.read(65536))  # a reply is a few hundred bytes: read at most 64 KiB
    if not (isinstance(reply, dict) and isinstance(reply.get("token"), str) and isinstance(reply.get("expires_at"), str)):
        raise ValueError("the token endpoint gave an unusable reply")
    now = now or dt.datetime.now(dt.timezone.utc)
    expires = dt.datetime.strptime(reply["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    life = (expires - now).total_seconds()
    if life > MAX_SECONDS:
        raise ValueError("the minted token lives %d seconds, more than %d" % (life, MAX_SECONDS))
    if life <= 0:
        raise ValueError("the minted token is already expired")
    tmp = out_file + ".new"  # written whole, then renamed: a reader never sees a partial token
    try:
        os.unlink(tmp)  # a stale or planted file is never reused
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(reply["token"])
    os.replace(tmp, out_file)
    return int(life)


def main():
    try:
        print("token minted, lives %d seconds" % mint(os.environ["FLEET_TOKEN_ENDPOINT"]))
        return 0
    except (KeyError, ValueError, TypeError, OSError) as e:
        print("fleet-token-mint: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
