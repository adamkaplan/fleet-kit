"""The in-cluster token service (option (i) of ask #1582): the ONLY holder of the App key.

It serves one caller (the in-cluster reaper's service account, checked by a TokenReview), one role (worker) and
only repositories on the launcher's own allow-list. It mints through app_token.mint (unchanged). The key is read by
app_token's own command from a Secret file only this workload mounts. Nothing here logs, audits or answers with
the key or a signed value. Nothing here is applied to a cluster by this change.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROLE = "worker"
REAPER_IDENTITY = "system:serviceaccount:cw:cw-cloud-reaper"


class Refused(Exception):
    pass


def serve(body, bearer, env, review, mint, log, audit):
    """Answer one request. review(bearer) -> the caller's user name or None (the TokenReview); mint(repo) -> token.
    Returns (status, answer dict). Lines to log and audit hold names only, never a token."""
    from cloud_worker import check_repo_allowed, CloudError
    who = None
    try:
        who = review(bearer)
        if who != env.get("CW_TOKEN_SERVICE_CALLER", REAPER_IDENTITY):
            raise Refused("caller")
        try:
            req = json.loads(body)
        except ValueError:
            raise Refused("body")
        if not isinstance(req, dict) or set(req) != {"repository", "role"} or not isinstance(req["repository"], str):
            raise Refused("body")
        if req["role"] != ROLE:
            raise Refused("role")
        try:
            check_repo_allowed(req["repository"], env)
        except CloudError:
            raise Refused("repository")
        token = mint(req["repository"])
    except Refused as e:
        log("refused: %s" % e)
        audit("%d refused %s %s" % (time.time(), who and "caller" or "unknown", e))
        return 403, {"error": "refused"}
    except SystemExit:
        log("mint failed")
        audit("%d mint-failed" % time.time())
        return 502, {"error": "mint failed"}
    audit("%d served %s" % (time.time(), req["repository"]))
    return 200, {"token": token}


SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


def kube_review(bearer):
    """The caller's user name by a TokenReview against the cluster's API (this workload's own service account makes the call)."""
    import ssl
    import urllib.request
    with open(SA_DIR + "/token") as f:
        mine = f.read().strip()
    body = json.dumps({"apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview", "spec": {"token": bearer}}).encode()
    req = urllib.request.Request("https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews", data=body, method="POST",
                                 headers={"Authorization": "Bearer " + mine, "Content-Type": "application/json"})
    ctx = ssl.create_default_context(cafile=SA_DIR + "/ca.crt")
    with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
        st = json.loads(r.read(65536)).get("status", {})
    return (st.get("user") or {}).get("username") if st.get("authenticated") else None


def run_server(env=None, port=8080):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import app_token
    env = dict(os.environ if env is None else env)

    def log(line):
        print("token-service: " + line, file=sys.stderr, flush=True)

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            auth = self.headers.get("Authorization") or ""
            status, answer = serve(self.rfile.read(n), auth[7:] if auth.startswith("Bearer ") else "", env, kube_review, app_token.mint, log, log)
            out = json.dumps(answer).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    HTTPServer(("", port), H).serve_forever()


if __name__ == "__main__":
    run_server()
