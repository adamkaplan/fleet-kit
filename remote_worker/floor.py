"""The transport for the Floor service, version 1 (outbound only): commands go into a storage queue, state comes back through a blob container.

Neither side accepts a connection. This caller adds a JSON command to the queue `cmd-<floor>-<caller>` and reads `state.json` and
`msg/<session>.json` from the container `floor-<floor>-<caller>`, both over HTTPS with a shared access signature (SAS) token. Standard library only.

Outside calls (the only ones in this package), each one HTTPS request to a fixed host built from the storage account name:
- `POST https://<account>.queue.core.windows.net/<queue>/messages?messagettl=86400&<queue SAS>`: add one command (SAS permission add only).
- `PUT https://<account>.blob.core.windows.net/<container>/payload/<cmd_id>.json?<container SAS>`: a command's large fields (written before the command).
- `GET .../state.json` (conditional, `If-None-Match`) and `GET .../msg/<session>.json`: read state and the last message.

The two SAS query strings (queue: add only; container: read, create, write; the caller never deletes) are read once at construction from two files whose
paths are configuration values (`queue_sas_file`, `container_sas_file`; a file must not be readable by group or others). Every message is added with
`messagettl=86400`: the queue's own default is 7 days. The account, floor id and caller id are configuration too. A token is sent only in the query of a request to the fixed hosts (a redirect is never
followed) and is never logged, printed or put in an error or a repr. The HTTP function is injectable, so tests make no network call.

Numbering: the transport numbers each session's commands 1, 2, 3 (launch is 1) and keeps the numbers in memory, so a new process that wants to
send to a session an earlier process started calls `adopt(handle, next_seq)`. A command that could not be added is kept, and added again (same
`cmd_id`, same `seq`) before the next command of that session, so no number is skipped. A command with no entry in `acks` after
`RESEND_SECONDS` is added again, unchanged, by `status`.
"""
import base64
import calendar
import hashlib
import json
import os
import re
import stat
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Mapping, Optional, Tuple
from xml.sax.saxutils import escape

from .transport import STATES, TERMINAL_STATES, TransportError, WorkerHandle, WorkerSpec, WorkerStatus

MAX_QUEUE_MESSAGE = 48 * 1024     # a command over this moves its brief, text and env to a payload blob (the queue's limit is 64 KiB)
MAX_TEXT = 256 * 1024             # `brief` and `text` are at most this
MAX_RESPONSE = 4 << 20
MAX_SAS_FILE = 8192
RESEND_SECONDS = 60.0
HEARTBEAT_SECONDS = 90.0          # a `written_at` older than this means Floor is unreachable
MESSAGE_TTL = 86400               # the SENDER sets the 24 h time to live on every message (the queue's default is 7 days)
ACK_TTL_SECONDS = float(MESSAGE_TTL)   # a command not acked by then is gone
API_VERSION = "2021-08-06"
ID_RE = re.compile(r"^[a-z0-9]{1,16}$")
ACCOUNT_RE = re.compile(r"^[a-z0-9]{3,24}$")
SESSION_RE = re.compile(r"^ses_[0-9a-f]{16}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SAS_RE = re.compile(r"^[A-Za-z0-9%=&_.:+/~-]{10,2048}$")
# (method, url, headers, body, timeout) -> (status, response headers with lower-case names, body)
HttpFn = Callable[[str, str, Mapping[str, str], Optional[bytes], float], Tuple[int, Mapping[str, str], bytes]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def default_http(method, url, headers, body, timeout):
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read(MAX_RESPONSE + 1)


def load_sas_file(path: str) -> str:
    """Read one SAS query string from a text file. Refuses a file that is not a regular file, is large, or is readable by others."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise TransportError("a SAS file cannot be opened") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_SAS_FILE:
            raise TransportError("a SAS file is not a small regular file")
        if st.st_mode & 0o077:
            raise TransportError("a SAS file must not be readable by group or others")
        raw = os.read(fd, MAX_SAS_FILE + 1)
    finally:
        os.close(fd)
    try:
        v = raw.decode("utf-8").strip().lstrip("?")
    except UnicodeDecodeError:
        raise TransportError("a SAS file is not text") from None
    if not SAS_RE.match(v):
        raise TransportError("a SAS file does not hold a SAS query string")
    return v


def _hex16(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:16]


class FloorTransport:
    name = "floor"

    def __init__(self, account: str, floor_id: str, caller_id: str, queue_sas_file: str, container_sas_file: str, git_base: str,
                 http: HttpFn = default_http, timeout: float = 30.0, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, min_read_seconds: float = 1.0):
        if not ACCOUNT_RE.match(account or ""):
            raise TransportError("the storage account name must be 3 to 24 lower-case letters and digits")
        if not ID_RE.match(floor_id or "") or not ID_RE.match(caller_id or ""):
            raise TransportError("the floor id and the caller id must be 1 to 16 lower-case letters and digits")
        if not isinstance(git_base, str) or not re.match(r"^https://[A-Za-z0-9.-]+(:\d+)?(/[A-Za-z0-9._~/-]*)?$", git_base):
            raise TransportError("git base must be an https URL with no credentials")
        self._queue_sas, self._container_sas = load_sas_file(queue_sas_file), load_sas_file(container_sas_file)
        self._queue_url = "https://%s.queue.core.windows.net/cmd-%s-%s/messages" % (account, floor_id, caller_id)
        self._blob_url = "https://%s.blob.core.windows.net/floor-%s-%s" % (account, floor_id, caller_id)
        self._caller = caller_id
        self._git_base = git_base.rstrip("/")
        self._http, self._timeout, self._clock, self._wall, self._min_read = http, timeout, clock, wall, min_read_seconds
        self._seq: Dict[str, int] = {}                 # session id -> last number used
        self._unsent: Dict[str, List[dict]] = {}       # session id -> commands not yet added to the queue, in order
        self._sent: Dict[str, dict] = {}               # cmd id -> {"sid", "message", "at"} while it has no ack
        self._ids: Dict[Tuple[str, str], str] = {}     # (session id, message id) -> cmd id: a repeated message id sends nothing more
        self._results: Dict[str, str] = {}             # cmd id -> its ack
        self._state: Optional[dict] = None
        self._etag = ""
        self._read_at: Optional[float] = None
        self._last: Dict[str, WorkerStatus] = {}

    def __repr__(self):
        return "FloorTransport(%s)" % self._blob_url

    # ---- the one place each kind of request is made ----
    def _request(self, method: str, url: str, extra: Mapping[str, str], body: Optional[bytes], ok: Tuple[int, ...]) -> Tuple[int, Mapping[str, str], bytes]:
        headers = {"x-ms-version": API_VERSION, "Accept": "application/json"}
        headers.update(extra)
        try:
            status, rhead, raw = self._http(method, url, headers, body, self._timeout)
        except (OSError, ValueError) as e:
            raise TransportError("the rendezvous could not be reached (%s)" % type(e).__name__, retryable=True) from None
        if len(raw) > MAX_RESPONSE:
            raise TransportError("the rendezvous answer is too large")
        if status in ok:
            return status, rhead, raw
        raise TransportError("the rendezvous refused the call: status %d" % status,
                             retryable=status in (408, 429, 500, 502, 503, 504))

    def _add_message(self, message: str) -> None:
        body = ("<QueueMessage><MessageText>%s</MessageText></QueueMessage>" % escape(message)).encode("ascii")
        self._request("POST", "%s?messagettl=%d&%s" % (self._queue_url, MESSAGE_TTL, self._queue_sas), {"Content-Type": "application/xml"}, body, ok=(201,))

    def _put_blob(self, name: str, data: bytes) -> None:
        self._request("PUT", "%s/%s?%s" % (self._blob_url, name, self._container_sas),
                      {"x-ms-blob-type": "BlockBlob", "Content-Type": "application/json"}, data, ok=(201,))

    def _get_blob(self, name: str, etag: str = "") -> Tuple[int, Mapping[str, str], bytes]:
        extra = {"If-None-Match": etag} if etag else {}
        return self._request("GET", "%s/%s?%s" % (self._blob_url, name, self._container_sas), extra, None, ok=(200, 304, 404))

    # ---- commands ----
    def _sid(self, handle: WorkerHandle) -> str:
        if handle.transport != self.name or not SESSION_RE.match(handle.worker_id):
            raise TransportError("not a session handle of this transport")
        return handle.worker_id

    def _now(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._wall()))

    def _build(self, sid: str, op: str, cmd_id: str, seq: int, fields: Mapping[str, object]) -> dict:
        """One queue message and, when it is large, the payload blob that goes first. The message body is the command JSON in base64."""
        cmd = {"v": 1, "cmd_id": cmd_id, "op": op, "session_id": sid, "seq": seq, "at": self._now()}
        cmd.update(fields)
        payload = None
        if len(json.dumps(cmd).encode("utf-8")) > MAX_QUEUE_MESSAGE:
            moved = {k: cmd.pop(k) for k in ("brief", "text", "env") if k in cmd}
            payload = ("payload/%s.json" % cmd_id, json.dumps(moved).encode("utf-8"))
            cmd["payload"] = payload[0]
        message = base64.b64encode(json.dumps(cmd).encode("utf-8")).decode("ascii")
        if len(message) > 64 * 1024:
            raise TransportError("the command is over the queue's size limit")
        return {"sid": sid, "cmd_id": cmd_id, "message": message, "payload": payload}

    def _deliver(self, c: dict) -> None:
        """Add one built command: the payload blob first, then the message. Repeating it is safe (same `cmd_id`)."""
        if c["payload"]:
            self._put_blob(*c["payload"])
        self._add_message(c["message"])
        self._sent[c["cmd_id"]] = {"sid": c["sid"], "message": c["message"], "at": self._clock()}

    def _flush(self, sid: str) -> None:
        pending = self._unsent.get(sid, [])
        while pending:
            self._deliver(pending[0])
            pending.pop(0)

    def _issue(self, sid: str, op: str, cmd_id: str, seq: int, fields: Mapping[str, object]) -> None:
        c = self._build(sid, op, cmd_id, seq, fields)
        self._unsent.setdefault(sid, []).append(c)
        self._flush(sid)   # a failure leaves it in `_unsent`, so the next call adds it again with the same numbers

    def _next_seq(self, sid: str) -> int:
        self._seq[sid] = self._seq.get(sid, 0) + 1
        return self._seq[sid]

    def adopt(self, handle: WorkerHandle, next_seq: int) -> None:
        """Take over a session an earlier process started: the next command of it is numbered `next_seq` (2 or more)."""
        sid = self._sid(handle)
        if isinstance(next_seq, bool) or not isinstance(next_seq, int) or next_seq < 2:
            raise TransportError("next_seq must be a whole number of 2 or more")
        self._seq[sid] = next_seq - 1

    def next_seq(self, handle: WorkerHandle) -> int:
        """The number the next command of this session will get (for a caller that keeps it between processes and gives it back to `adopt`)."""
        sid = self._sid(handle)
        return self._seq.get(sid, 1) + 1

    def start(self, spec: WorkerSpec) -> WorkerHandle:
        # the session id and the launch command id come from the worker name, so a repeated start (even from a new process) is the same launch
        sid = "ses_" + _hex16(self._caller, spec.name)
        if len(spec.brief.encode("utf-8")) > MAX_TEXT:
            raise TransportError("the brief is over the service's 256 KiB limit")
        if sid not in self._seq:
            self._seq[sid] = 1
            fields = {"repo": "%s/%s.git" % (self._git_base, spec.repo), "ref": spec.branch, "brief": spec.brief}
            if spec.env:
                fields["env"] = dict(spec.env)   # kept only in the queue message (and a payload blob when large); never logged
            self._issue(sid, "launch", "c-" + _hex16("launch", sid), 1, fields)
        else:
            self._flush(sid)
        return WorkerHandle(name=spec.name, worker_id=sid, transport=self.name)

    def send(self, handle: WorkerHandle, text: str, message_id: Optional[str] = None) -> None:
        sid = self._sid(handle)
        if len(text.encode("utf-8")) > MAX_TEXT:
            raise TransportError("message is over the service's 256 KiB limit")
        last = self._last.get(sid)
        if last is not None and last.state in TERMINAL_STATES:
            raise TransportError("worker has ended")
        if sid not in self._seq:
            raise TransportError("this transport did not start that session: call adopt first")
        if message_id:
            known = self._ids.get((sid, message_id))
            if known:
                self._flush(sid)
                return
            cmd_id = "c-" + _hex16("send", sid, message_id)
            self._ids[(sid, message_id)] = cmd_id
        else:
            cmd_id = "c-" + _hex16("send", sid, str(self._wall()), str(self._seq[sid]))
        self._issue(sid, "send", cmd_id, self._next_seq(sid), {"text": text})

    def stop(self, handle: WorkerHandle, reason: str) -> None:
        """Add a retire command. A session already known to have ended gets nothing. The reason is not sent: the command takes none."""
        sid = self._sid(handle)
        last = self._last.get(sid)
        if last is not None and last.state in TERMINAL_STATES:
            return
        if sid not in self._seq:
            raise TransportError("this transport did not start that session: call adopt first")
        cmd_id = "c-" + _hex16("retire", sid)
        if cmd_id in self._sent or cmd_id in self._results or any(c["cmd_id"] == cmd_id for c in self._unsent.get(sid, [])):
            self._flush(sid)
            return
        self._issue(sid, "retire", cmd_id, self._next_seq(sid), {})

    # ---- state ----
    def _read_state(self) -> dict:
        now = self._clock()
        if self._state is not None and self._read_at is not None and now - self._read_at < self._min_read:
            return self._state
        status, rhead, raw = self._get_blob("state.json", self._etag)
        self._read_at = now
        if status == 304 and self._state is not None:
            return self._state
        if status == 404:
            return self._state or {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise TransportError("state.json is not JSON") from None
        if not isinstance(data, dict) or data.get("v") != 1:
            raise TransportError("state.json is not version 1")
        self._state, self._etag = data, rhead.get("etag", "")
        return data

    def _settle(self, state: dict) -> None:
        """Record the acks, then add again every command that has none after RESEND_SECONDS (the same message)."""
        acks = state.get("acks") if isinstance(state.get("acks"), dict) else {}
        now = self._clock()
        for cmd_id, entry in list(self._sent.items()):
            ack = acks.get(cmd_id)
            if isinstance(ack, str):
                self._results[cmd_id] = ack[:60]
                del self._sent[cmd_id]
            elif now - entry["at"] > ACK_TTL_SECONDS:
                del self._sent[cmd_id]
            elif now - entry["at"] >= RESEND_SECONDS:
                self._add_message(entry["message"])
                entry["at"] = now

    def command_result(self, cmd_id: str) -> str:
        """`applied`, `duplicate`, `rejected:<code>`, or `pending` (not acked yet). Read from the last `status` call."""
        return self._results.get(cmd_id, "pending")

    def floor_alive(self) -> bool:
        """True while `state.json` is fresher than 90 s. False means Floor is unreachable: do not retire or relaunch on that alone."""
        written = (self._state or {}).get("written_at")
        try:
            t = calendar.timegm(time.strptime(str(written), "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            return False
        return self._wall() - t <= HEARTBEAT_SECONDS

    def status(self, handle: WorkerHandle) -> WorkerStatus:
        sid = self._sid(handle)
        self._flush(sid)
        state = self._read_state()
        self._settle(state)
        s = (state.get("sessions") or {}).get(sid)
        if not isinstance(s, dict):
            launch = self._results.get("c-" + _hex16("launch", sid), "pending")
            if launch.startswith("rejected"):
                raise TransportError("the launch was refused: %s" % launch)
            if sid in self._last:   # a finished session is dropped from the state after 24 h: keep what was seen
                return self._last[sid]
            return WorkerStatus(state="queued", busy=True)
        st = s.get("state")
        if st not in STATES:
            raise TransportError("the service answered with an unknown state")
        sha = s.get("landed_sha") or ""
        if sha and not SHA_RE.match(str(sha)):
            raise TransportError("the service answered with a landed sha that is not 40 hex characters")
        out = WorkerStatus(state=st, busy=st in ("queued", "running"), last_update=str(s.get("last_update") or ""), landed_sha=str(sha))
        self._last[sid] = out
        return out

    def last_message(self, handle: WorkerHandle) -> str:
        sid = self._sid(handle)
        status, _, raw = self._get_blob("msg/%s.json" % sid)
        if status == 404:
            return ""
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise TransportError("the last-message blob is not JSON") from None
        text = data.get("text") if isinstance(data, dict) else None
        return text if isinstance(text, str) else ""

    def close(self) -> None:
        self._queue_sas = self._container_sas = ""
