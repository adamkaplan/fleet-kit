"""Cloud review: the daemon's side of the cloud channel (#1149): a Kubernetes TokenReview reviewer and an inbox adapter.

The reviewer posts a TokenReview (audience set) for the caller's token, authenticated by the daemon's own projected
service account token; no key is stored and no token is kept or logged. The inbox adapter hands each message to ONE
function (the daemon's `engine.send`) so the delivery rules (note, wake, hold, riders) are not copied; it dedupes by key.
"""
import json
import ssl
import threading
import urllib.request

API = "https://kubernetes.default.svc"
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


def make_reviewer(api=API, sa_dir=SA_DIR, opener=None):
    """reviewer(token, audience) -> the TokenReview status dict. Raises when the cluster cannot be asked."""
    def reviewer(token, audience):
        with open(sa_dir + "/token") as handle:
            own = handle.read().strip()
        body = json.dumps({"apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview",
                           "spec": {"token": token, "audiences": [audience]}}).encode()
        request = urllib.request.Request(api + "/apis/authentication.k8s.io/v1/tokenreviews", data=body, method="POST",
                                         headers={"Authorization": "Bearer " + own, "Content-Type": "application/json"})
        if opener is not None:
            reply = opener(request)
        else:
            context = ssl.create_default_context(cafile=sa_dir + "/ca.crt")
            reply = urllib.request.urlopen(request, timeout=5, context=context)
        with reply:
            status = json.loads(reply.read().decode()).get("status")
        return status if isinstance(status, dict) else {}
    return reviewer


class Inbox:
    """put_if_absent(recipient, dedupe_key, item): deliver once through `send(sender, recipient, text)`."""
    def __init__(self, send):
        self.send, self.seen, self.lock = send, set(), threading.Lock()

    def put_if_absent(self, recipient, dedupe_key, item):
        with self.lock:
            if (recipient, dedupe_key) in self.seen:
                return False
            self.send(item["sender"], recipient, item["text"])   # raises: not marked, the sender retries with the same key
            self.seen.add((recipient, dedupe_key))
            return True
