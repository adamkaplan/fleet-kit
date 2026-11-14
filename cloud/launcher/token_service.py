#!/usr/bin/env python3
"""token-service: a small HTTP service that mints the GitHub App installation token for the operator account (ask #2002).

POST /mint with the JSON body {"repo": "OWNER/NAME", "role": "worker"|"orchestrator"} and the header X-Caller-Token: Bearer <cluster token> (the API server removes Authorization of a proxied request).
The caller is REVIEWED (the cluster's TokenReview API): only the account system:serviceaccount:<namespace>:cw-operator is served.
The repository must be on CW_ALLOWED_REPOS (the launcher's own check), the role in PERMISSIONS_BY_ROLE, the body at most 4096 bytes,
and the body carries no other key. The answer is GitHub's own: the token, the repositories, the permissions and the expiry.
The App id and key are read by the commands CW_APP_ID_CMD and CW_APP_KEY_CMD (default: cat of the files mounted from the cluster Secret).
Refusals: 400 bad body or role, 401 no usable caller token, 403 repository or caller not allowed, 413 body too large, 502 GitHub or the
review could not be asked. Each has ONE line naming the reason and no value. The log has the method, the status and the reason only.
No file is written. Python standard library only.
"""
import datetime as dt
import http.server
import json
import os
import ssl
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app_token  # noqa: E402
import cloud_worker  # noqa: E402  (check_repo_allowed: the launcher's one allow-list check; importing it has no side effect)

MAX_BODY = 4096
KEYS = {"repo", "role"}
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
REVIEW_URL = "https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews"


class ReviewUnavailable(Exception):
    """The review could not be asked (a 502)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def cluster_review(token, url=REVIEW_URL, sa_dir=SA_DIR):
    """Ask the cluster who owns token: (authenticated, username). Raises ReviewUnavailable when it cannot be asked."""
    try:
        with open(os.path.join(sa_dir, "token")) as f:
            own = f.read().strip()
        ctx = ssl.create_default_context(cafile=os.path.join(sa_dir, "ca.crt"))
        body = json.dumps({"apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview", "spec": {"token": token}}).encode()
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Authorization": "Bearer " + own, "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), _NoRedirect)
        with opener.open(req, timeout=15) as r:
            ans = json.loads(r.read(1_000_000))
        status = ans.get("status") or {}
        user = (status.get("user") or {}).get("username")
        return status.get("authenticated") is True, user if isinstance(user, str) else None
    except Exception:
        raise ReviewUnavailable()


def mint_answer(target, role):
    """GitHub's own answer for ONE repository and the role's permissions (the steps of app_token.mint, which returns only the token).
    Exits through app_token.die() on any failure (SystemExit): the caller maps it to a 502."""
    repo = target.split("/", 1)[1]
    app_id = app_token.vault("CW_APP_ID_CMD", "cw-app-id").strip()
    if not app_id.isdigit():
        app_token.die("the app id must be a number")
    key = app_token.vault("CW_APP_KEY_CMD", "cw-app-key")
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    b64 = app_token.b64
    head = b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = b64(json.dumps({"iat": now - 30, "exp": now + 9 * 60, "iss": app_id}, separators=(",", ":")).encode())
    signing = "%s.%s" % (head, claims)
    jwt = signing + "." + b64(app_token.sign(key, signing.encode()))
    del key
    inst, _owner_id = app_token.find_installation(target, jwt)
    ans = app_token.github_json("POST", "/app/installations/%d/access_tokens" % inst, jwt,
                                {"repositories": [repo], "permissions": dict(app_token.PERMISSIONS_BY_ROLE[role])}, role=role)
    if not isinstance(ans, dict) or not isinstance(ans.get("token"), str) or not ans["token"]:
        app_token.die("the answer has no token")
    app_token.check_permissions(ans.get("permissions"), role)
    return ans


def handle(headers, body_length, read_body, review, env=None, mint_fn=None):
    """One request to /mint: (status, answer dict or None, reason line). `review(token)` returns (authenticated, username)."""
    env = os.environ if env is None else env
    mint_fn = mint_fn or mint_answer
    if body_length is None or body_length < 0:
        return 400, None, "the request has no usable length"
    if body_length > MAX_BODY:
        return 413, None, "the body is larger than %d bytes" % MAX_BODY
    raw = read_body(body_length)
    auth = headers.get("X-Caller-Token") or ""
    scheme, _, token = auth.partition(" ")
    token = token.strip()
    if scheme != "Bearer" or not token:
        return 401, None, "no caller token"
    try:
        authenticated, user = review(token)
    except ReviewUnavailable:
        return 502, None, "the caller review could not be asked"
    if authenticated is not True:
        return 401, None, "the caller token is not accepted"
    if user != "system:serviceaccount:%s:cw-operator" % (env.get("CW_CLOUD_NAMESPACE") or "cw"):
        return 403, None, "the caller is not the operator account"
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return 400, None, "the body is not JSON"
    if not isinstance(body, dict) or set(body) != KEYS:
        return 400, None, "the body must carry exactly repo and role"
    repo, role = body["repo"], body["role"]
    if not isinstance(role, str) or role not in app_token.PERMISSIONS_BY_ROLE:
        return 400, None, "unknown role"
    if not isinstance(repo, str) or not app_token.valid_target(repo):
        return 400, None, "the repository is not OWNER/NAME"
    try:
        cloud_worker.check_repo_allowed(repo, env)
    except cloud_worker.CloudError:
        return 403, None, "the repository is not on the allowed list"
    try:
        ans = mint_fn(repo, role)
    except SystemExit:
        return 502, None, "GitHub could not be asked or refused the mint"
    return 200, ans, "served"


def make_server(review, port=8080, host="0.0.0.0", log=None, env=None, mint_fn=None, timeout=30):
    """A threading server (daemon threads). `log(line)` receives 'METHOD STATUS reason' and nothing else (default: stderr)."""
    emit = log or (lambda line: print(line, file=sys.stderr, flush=True))

    class Handler(http.server.BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(timeout)   # a request that promises more than it sends is dropped

        def log_message(self, *a):
            pass

        def _send(self, status, payload, reason):
            out = json.dumps(payload).encode() if payload is not None else (reason + "\n").encode()
            emit("%s %d %s" % (self.command, status, reason))   # logged before the client can see the answer
            self.send_response(status)
            self.send_header("Content-Type", "application/json" if payload is not None else "text/plain")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_POST(self):
            if self.path != "/mint":
                return self._send(404, None, "not found")
            try:
                n = int(self.headers.get("Content-Length"))
            except (TypeError, ValueError):
                n = None
            if n is not None and MAX_BODY < n <= 1_000_000:
                self.rfile.read(n)   # drained and dropped, so the refusal reaches the caller
            status, payload, reason = handle(self.headers, n, self.rfile.read, review, env, mint_fn)
            self._send(status, payload, reason)

        def do_GET(self):
            self._send(405, None, "method not allowed")

        do_PUT = do_DELETE = do_PATCH = do_GET

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            pass   # silences the standard library's report of an aborted connection: the service's own log line is the only one

    return Server((host, port), Handler)


def main():
    os.environ.setdefault("CW_APP_ID_CMD", "cat /etc/token-mint/app-id")
    os.environ.setdefault("CW_APP_KEY_CMD", "cat /etc/token-mint/app-key")
    make_server(cluster_review).serve_forever()


if __name__ == "__main__":
    main()
