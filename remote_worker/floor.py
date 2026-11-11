"""The transport for the Floor service: HTTP and JSON, one bearer token per caller. Written against the service's transport interface v0
(its `docs/transport.md`): launch, send, retire, activity, last message.

Outside calls (the only ones in this package): one HTTP request per transport call to `<base_url>/v1/...`. The bearer token is read once, at
construction, from the environment variable the caller names; it is sent only in the Authorization header to the configured base URL (a
redirect is never followed, so it cannot go elsewhere), and is never logged, printed or put in an error. The HTTP function is injectable so
tests make no network call.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Mapping, Optional, Tuple

from .transport import STATES, TransportError, WorkerHandle, WorkerSpec, WorkerStatus

MAX_BODY = 1 << 20          # the service refuses a request body over 1 MiB
MAX_MESSAGE = 256 * 1024    # and a message text over 256 KiB
MAX_RESPONSE = 4 << 20      # a response larger than this is refused
SESSION_RE = re.compile(r"^ses_[0-9a-f]{16}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HttpFn = Callable[[str, str, Mapping[str, str], Optional[bytes], float], Tuple[int, bytes]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def default_http(method, url, headers, body, timeout):
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as e:
        return e.code, e.read(MAX_RESPONSE + 1)


class FloorTransport:
    name = "floor"

    def __init__(self, base_url: str, token_env: str, git_base: str, environ: Optional[Mapping[str, str]] = None,
                 http: HttpFn = default_http, timeout: float = 30.0):
        u = urllib.parse.urlsplit(base_url)
        if u.scheme not in ("https", "http") or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ("", "/"):
            raise TransportError("base URL must be https://HOST[:PORT] with no path or credentials")
        if u.scheme == "http" and u.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise TransportError("an http base URL is allowed only for a loopback address")
        g = urllib.parse.urlsplit(git_base)
        if g.scheme != "https" or not g.hostname or g.username or g.password or g.query or g.fragment:
            raise TransportError("git base must be an https URL with no credentials")
        token = ((environ if environ is not None else os.environ).get(token_env) or "").strip()
        if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise TransportError("the token variable %s is unset or not a plain token" % token_env)
        self._base = "%s://%s" % (u.scheme, u.netloc)
        self._git_base = git_base.rstrip("/")
        self._token = token
        self._http = http
        self._timeout = timeout

    def _call(self, method: str, path: str, payload: Optional[dict] = None, ok: Tuple[int, ...] = (200,)) -> dict:
        """The one place a request is made."""
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            if len(body) > MAX_BODY:
                raise TransportError("request is over the service's 1 MiB limit")
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            status, raw = self._http(method, self._base + "/v1" + path, headers, body, self._timeout)
        except (OSError, ValueError) as e:   # refused, timed out, DNS: the service may still be fine
            raise TransportError("the service could not be reached (%s)" % type(e).__name__, retryable=True) from None
        if len(raw) > MAX_RESPONSE:
            raise TransportError("the service's answer is too large")
        data: dict = {}
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                if status in ok:
                    raise TransportError("the service answered with text that is not JSON (status %d)" % status) from None
                parsed = None
            if isinstance(parsed, dict):
                data = parsed
        if status in ok:
            return data
        err = data.get("error") if isinstance(data.get("error"), dict) else {}
        code = str(err.get("code", ""))[:40]
        code = code if re.fullmatch(r"[a-z_]+", code) else "unknown"
        raise TransportError("the service refused the call: status %d, code %s" % (status, code),
                             retryable=status in (429, 502, 503, 504))

    @staticmethod
    def _sid(handle: WorkerHandle) -> str:
        if handle.transport != "floor" or not SESSION_RE.match(handle.worker_id):
            raise TransportError("not a session handle of this transport")
        return handle.worker_id

    def start(self, spec: WorkerSpec) -> WorkerHandle:
        data = self._call("POST", "/sessions", {
            "client_key": spec.name,   # a repeat start of the same name returns the first session
            "repo": "%s/%s.git" % (self._git_base, spec.repo),
            "ref": spec.branch,
            "brief": spec.brief,
            "title": spec.name,
        }, ok=(200, 201))
        sid = data.get("session_id")
        if not isinstance(sid, str) or not SESSION_RE.match(sid):
            raise TransportError("the service answered without a valid session id")
        return WorkerHandle(name=spec.name, worker_id=sid, transport=self.name)

    def send(self, handle: WorkerHandle, text: str, message_id: Optional[str] = None) -> None:
        if len(text.encode("utf-8")) > MAX_MESSAGE:
            raise TransportError("message is over the service's 256 KiB limit")
        payload = {"text": text}
        if message_id:
            payload["message_id"] = message_id
        self._call("POST", "/sessions/%s/messages" % self._sid(handle), payload, ok=(202,))

    def status(self, handle: WorkerHandle) -> WorkerStatus:
        d = self._call("GET", "/sessions/%s/activity" % self._sid(handle))
        state = d.get("state")
        if state not in STATES:
            raise TransportError("the service answered with an unknown state")
        sha = d.get("landed_sha") or ""
        if sha and not SHA_RE.match(str(sha)):
            raise TransportError("the service answered with a landed sha that is not 40 hex characters")
        return WorkerStatus(state=state, busy=bool(d.get("busy")), last_update=str(d.get("last_update") or ""), landed_sha=str(sha))

    def last_message(self, handle: WorkerHandle) -> str:
        d = self._call("GET", "/sessions/%s/last-message" % self._sid(handle))
        text = d.get("text")
        return text if isinstance(text, str) else ""

    def stop(self, handle: WorkerHandle, reason: str) -> None:
        """Retire the session. A session already gone (404) counts as stopped. The reason is not sent: the service's retire takes none."""
        try:
            self._call("DELETE", "/sessions/%s" % self._sid(handle))
        except TransportError as e:
            if "status 404" in e.reason:
                return
            raise

    def close(self) -> None:
        self._token = ""
