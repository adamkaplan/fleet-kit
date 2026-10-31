# ------------------------------------------------------------- OpenCode v2

class V2Error(Exception):
    """A v2 call that did not return usable JSON.

    reason: "http" (v2 answered with an HTTP error), "exit" (non-zero exit, no
    HTTP status), "timeout", "bad_json", "unavailable" (could not run), "too_large" (a reply over the cap), "refused" (a real run under the test suite) or "backoff" (an optional
    read skipped while the service is slow).
    """

    def __init__(self, reason, status=None, detail=None):
        self.reason = reason
        self.status = status
        self.detail = detail
        text = reason if status is None else "%s %s" % (reason, status)
        super().__init__("%s: %s" % (text, detail) if detail else text)


def _one_line(text, limit=300):
    return decision_core.one_line(text, limit)


STATE_HEARTBEAT_SECONDS = 60   # a status file whose own timestamp is its liveness signal is rewritten at least this often, changed or not (`status` calls v2-calls stale past 120 s: a pass of 30 s fits twice)
HEARTBEAT_HEALTH = STATE_HEARTBEAT_SECONDS * 8   # pane-health.json: 8 minutes (nothing reads its times as liveness)
HEARTBEAT_SWEEP = STATE_HEARTBEAT_SECONDS * 4    # sweep.json and triage.json: 4 minutes (their `at` is shown as an absolute "as of")
HEARTBEAT_PR_STATE = 300                         # pr-state.json: 5 minutes
_STATE_LAST = {}               # path -> (stable text, monotonic time written): what this process last wrote


def state_write(path, text, stable=None, heartbeat=None, fsync=False, nofollow=False):
    """Write `text` to `path` (a private file: a temp file, mode 0600, renamed into place) ONLY WHEN IT CHANGED. Returns True when it wrote.

    `stable` is the text to compare: the content without its volatile fields (a timestamp that moves every pass), default `text` itself.
    The comparison is with what is on the DISK: the file's size and modification time are checked against what this process wrote (one stat), and
    a file that changed meanwhile (an outside writer, `retire --undo`) is read and compared with what was written, so an external rewrite is
    repaired at the next call, not at the heartbeat; a mere touch is not. A file that is missing is written. `heartbeat` (seconds) keeps a status
    whose own timestamp is read as liveness fresh: it is rewritten when its last write is older than that. A write nobody needs costs the disk
    a write, an fsync and a rename that every file watcher sees: about twenty files used to be rewritten every pass."""
    path = Path(path)
    key, clock = str(path), time.monotonic()
    stable = text if stable is None else stable
    last = _STATE_LAST.get(key)
    try:
        info = path.stat()
        if last is not None and last[0] == stable and (heartbeat is None or clock - last[1] < heartbeat):
            if (info.st_mtime_ns, info.st_size) == (last[3], last[4]):
                return False
            if path.read_text() == last[2]:   # touched, not rewritten: the content is what this process wrote
                _STATE_LAST[key] = (last[0], last[1], last[2], info.st_mtime_ns, info.st_size)
                return False
            # the file is not what was written: an outside writer changed it; fall through and write it again
        if last is None and heartbeat is None and info.st_size == len(text.encode("utf-8")) and path.read_text() == text:   # a file of another size is not even read
            _STATE_LAST[key] = (stable, clock, text, info.st_mtime_ns, info.st_size)
            return False
    except OSError:
        pass
    temp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    try:
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | (os.O_NOFOLLOW if nofollow else 0), 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            if fsync:
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(str(temp), str(path))
    except BaseException:
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise
    try:
        info = path.stat()
        _STATE_LAST[key] = (stable, clock, text, info.st_mtime_ns, info.st_size)
    except OSError:
        _STATE_LAST.pop(key, None)
    return True


def _without(body, *names):
    """`body` (a dict) as sorted JSON without the top-level `names`: the stable part of a status."""
    return json.dumps({k: v for k, v in body.items() if k not in names}, sort_keys=True)


CURSOR_LIMIT = 2000  # characters of one v2 cursor token we will pass back on an argv
MAX_READ_PAGES = 12   # pages of SYNTHETIC_PAGE messages `delivered_keys` reads before it holds (600 messages, newest first, stopping at the first old enough)
SYNTHETIC_PAGE = 50   # messages of one such read: a long history never answers a big page
DESCRIPTION_MAX = 200  # characters of the one line v2's TUI shows for a synthetic message (PR 13)


def valid_description(text):
    return isinstance(text, str) and bool(text.strip()) and len(text) <= DESCRIPTION_MAX \
        and "\n" not in text and "\r" not in text


V2_CALLS_KEEP_SECONDS = 600   # how long a call is remembered, for the calls-per-minute figures
V2_CALLS_KEEP_MAX = 50000     # and how many at most (a runaway cannot grow the list without bound)
UNDER_TEST_ENV = "FLEET_SWITCHBOARD_UNDER_TEST"   # set by the test suite: the real opencode is then never run
ALLOW_REAL_V2_ENV = "FLEET_SWITCHBOARD_ALLOW_REAL_V2"   # =1: a test that really means to talk to a service says so


def refuse_real_opencode_under_test(runner, binary):
    """Under the test suite, a call that would really run `opencode` (the real `subprocess.run`) is refused: a test that reached the live
    service once helped take it down (tens of thousands of spawns). A fake runner is unaffected, and so is a test that says
    FLEET_SWITCHBOARD_ALLOW_REAL_V2=1."""
    if os.environ.get(UNDER_TEST_ENV) == "1" and os.environ.get(ALLOW_REAL_V2_ENV) != "1" and runner is subprocess.run:
        raise V2Error("refused", None, "refusing to run the real %s under the test suite (set %s=1 to allow it in a test that means to)"
                      % (binary, ALLOW_REAL_V2_ENV))


# ---------------------------------------------------------------- the v2 transport: HTTP to the background service, and backpressure
#
# `opencode api <op>` starts a whole process per call (0.13 to 1.5 s measured, against 5 to 17 ms for the same call over HTTP, with identical
# JSON). The background service writes its address and password to `<state>/opencode/service.json` (mode 0600); the daemon talks to it directly
# over loopback, keeps one connection alive for reads, and falls back to the CLI on ANY trouble with the HTTP route (the CLI also starts a
# stopped service, so the fallback is also the recovery). Writes use a fresh connection and are never replayed after they may have been
# received. The password is sent only to a loopback address and is never logged, audited, printed or put in an error.
# The operation table was read from the service's own /openapi.json (OpenCode 2.0.24); an operation not in it, or a path parameter it does not
# know, goes through the CLI. FLEET_SWITCHBOARD_V2_CLI=1 turns the HTTP route off.
#
# Backpressure: V2Breaker watches the latency of every call. When calls are slow (p95 over V2_SLOW_SECONDS) or time out in a row it opens for an
# exponentially growing time (V2_BACKOFF_BASE .. V2_BACKOFF_MAX): OPTIONAL reads are skipped (V2Error "backoff": the callers already hold on a
# V2Error) while writes and the reads a delivery or a blocked worker needs go on; it resumes gradually (one probe at a time, V2_RECOVER_OK good
# probes to close). Fail mode: OPEN for essential calls on purpose (a wake must not be lost to a slow service), closed for optional reads.

V2_HTTP_OPERATIONS = {   # operation -> (method, path template, path parameters, query parameters)
    "server.info": ("GET", "/api/info", (), ()),
    "session.active": ("GET", "/api/session/active", (), ()),
    "session.create": ("POST", "/api/session", (), ()),
    "model.list": ("GET", "/api/model", (), ()),
    "session.get": ("GET", "/api/session/{sessionID}", ("sessionID",), ()),
    "session.remove": ("DELETE", "/api/session/{sessionID}", ("sessionID",), ()),
    "session.list": ("GET", "/api/session", (), ("limit", "order", "search", "parentID", "directory", "project", "subpath", "cursor")),
    "session.message.list": ("GET", "/api/session/{sessionID}/message", ("sessionID",), ("limit", "order", "cursor", "type")),
    "session.inbox.list": ("GET", "/api/session/{sessionID}/inbox", ("sessionID",), ()),
    "session.inbox.update": ("PATCH", "/api/session/{sessionID}/inbox/{inboxID}", ("sessionID", "inboxID"), ()),
    "session.interrupt": ("POST", "/api/session/{sessionID}/interrupt", ("sessionID",), ("resume",)),
    "session.permission.list": ("GET", "/api/session/{sessionID}/permission", ("sessionID",), ()),
    "session.permission.reply": ("POST", "/api/session/{sessionID}/permission/{requestID}/reply", ("sessionID", "requestID"), ()),
    "session.form.list": ("GET", "/api/session/{sessionID}/form", ("sessionID",), ()),
    "session.form.reply": ("POST", "/api/session/{sessionID}/form/{formID}/reply", ("sessionID", "formID"), ()),
    "session.prompt": ("POST", "/api/session/{sessionID}/prompt", ("sessionID",), ()),
    "session.synthetic": ("POST", "/api/session/{sessionID}/synthetic", ("sessionID",), ()),
    "experimental.session.import": ("POST", "/api/experimental/session/import", (), ()),
}
V2_ESSENTIAL_READS = ("session.get", "session.inbox.list", "session.permission.list", "session.form.list")   # discovery and the re-check before a write (session.get), a delivery and a blocked worker need these
V2_SERVICE_FILE_MAX = 4096        # bytes of service.json read
V2_HTTP_MAX_BODY = 16 * 1024 * 1024   # a reply larger than this is refused, not buffered (a transcript is read in bounded pages anyway)
V2_READ_LIMIT_MAX = 500           # messages per page, whatever a caller asks
V2_STREAM_LINE_MAX = 1024 * 1024  # one event line of the stream; a larger one is refused
V2_STREAM_IDLE_SECONDS = 1800     # a stream that said nothing for this long is dropped and opened again (the service sends no keep-alive); that reopen is NOT a gap
STREAM_OPENED = object()          # what stream_lines yields once the service answered
STREAM_IDLE = object()            # what it yields when it ends because the stream was quiet for the whole idle time (the next open is not a gap)
V2_SLOW_SECONDS = 8.0             # p95 of the recent calls above this opens the breaker
V2_SLOW_MIN_SAMPLES = 8
V2_SLOW_WINDOW = 20
V2_TIMEOUT_STREAK = 3             # timeouts in a row that open it
V2_BACKOFF_BASE = 10.0
V2_BACKOFF_MAX = 300.0
V2_RECOVER_OK = 3                 # good probes in a row that close it
V2_LINK_STATUS_FILE = "v2-link.json"
V2_LINK_WRITE_SECONDS = 60


class V2Unreachable(Exception):
    """The HTTP route cannot serve this call (no service file, a stale one, refused, a bad password twice): the caller uses the CLI."""


def v2_service_file(env):
    base = env.get("XDG_STATE_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".local", "state")
    return Path(base) / "opencode" / "service.json"


class V2Http:
    """One keep-alive connection for reads to the OpenCode service; see the section comment."""

    def __init__(self, service_file, timeout, alive=None, connect=None):
        self.file = Path(service_file)
        self.timeout = timeout
        self.alive = alive or self._pid_alive
        self.connect = connect or http.client.HTTPConnection   # (host, port, timeout=) -> a connection; a test passes an in-memory one
        self._info = None        # (host, port, authorization) once read; never logged
        self._conn = None
        self._lock = threading.Lock()

    @staticmethod
    def _pid_alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True   # not ours to signal, so it exists
        return True

    def base_url(self):
        """`http://host:port` of the service (never the password), or None when the service file cannot be read."""
        with self._lock:
            try:
                if self._info is None:
                    self._load()
                host, port, _ = self._info
                return "http://%s:%d" % (host if ":" not in host else "[%s]" % host, port)
            except Exception:
                return None

    def _read_file(self):
        """(host, port, password, pid) of the service file, read with every safety check. Raises V2Unreachable."""
        try:
            parent = os.lstat(str(self.file.parent))
            if parent.st_uid != os.getuid() or parent.st_mode & 0o022 or stat.S_ISLNK(parent.st_mode):
                raise ValueError("the directory is not the user's own and closed to others")   # whoever can write the directory can swap the file
            fd = os.open(str(self.file), os.O_RDONLY | os.O_NOFOLLOW)   # a symlink is refused, not followed
            with os.fdopen(fd, "rb") as handle:
                found = os.fstat(handle.fileno())
                if not stat.S_ISREG(found.st_mode) or found.st_uid != os.getuid() or found.st_mode & 0o077:
                    raise ValueError("not private to this user")   # the password in it is only trusted from a file only this user could write
                raw = handle.read(V2_SERVICE_FILE_MAX + 1)
            if len(raw) > V2_SERVICE_FILE_MAX:
                raise ValueError("too large")
            found = json.loads(raw.decode("utf-8"))
            url = urllib.parse.urlsplit(found["url"])
            password, pid = found["password"], found.get("pid")
            if url.scheme != "http" or url.hostname not in ("127.0.0.1", "::1") or not url.port:
                raise ValueError("not a loopback http address")   # the password goes nowhere else
            if not isinstance(password, str) or not 0 < len(password) <= 512 or re.search(r"[\x00-\x1f\x7f]", password):
                raise ValueError("no usable password")
            host, port = url.hostname, url.port
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise V2Unreachable("no usable service file") from None
        return host, port, password, pid

    def service_identity(self):
        """The service's process id from a FRESH read of its private service file (a restart changes it), or None when the file cannot be read or names a
        process that is gone. Nothing is cached and the password is never returned."""
        try:
            pid = self._read_file()[3]
        except V2Unreachable:
            return None
        return pid if isinstance(pid, int) and not isinstance(pid, bool) and self.alive(pid) else None

    def _load(self):
        host, port, password, pid = self._read_file()
        if isinstance(pid, int) and not self.alive(pid):
            raise V2Unreachable("the service file names a process that is gone")
        token = base64.b64encode(("opencode:" + password).encode("utf-8")).decode("ascii")
        self._info = (host, port, "Basic " + token)

    def stream_lines(self, path, idle_seconds=V2_STREAM_IDLE_SECONDS):
        """STREAM_OPENED once the service answered 200, then the lines of one SSE response (decoded, newline stripped), read on its OWN connection (never
        under the request lock). A quiet stretch of `idle_seconds` (the service sends no keep-alive) ends the generator like a closed stream. Raises V2Unreachable
        (no service, a refused connection, a password refused: use the old reads) or V2Error (any other status)."""
        with self._lock:
            if self._info is None:
                self._load()
            host, port, auth = self._info
        conn = self.connect(host if ":" not in host else "[%s]" % host, port, timeout=idle_seconds)
        try:
            try:
                conn.connect()
                conn.request("GET", path, headers={"Authorization": auth, "Accept": "text/event-stream"})
                response = conn.getresponse()
            except (OSError, http.client.HTTPException):
                with self._lock:
                    self._info = None   # it may have moved (a restart on a new port): read the file again at the next connect, as `request` does
                raise V2Unreachable("the service does not answer") from None
            if response.status == 401:
                with self._lock:
                    self._info = None   # the password changed: read the file again at the next connect
                raise V2Unreachable("the service refused the password")
            if response.status != 200:
                raise V2Error("http", response.status, "the event stream answered HTTP %d" % response.status)
            yield STREAM_OPENED   # the service answered 200: only now is this a connection
            while True:
                try:
                    raw = response.readline(V2_STREAM_LINE_MAX + 1)
                except socket.timeout:
                    yield STREAM_IDLE   # quiet for the whole idle time: ours to end, not the service's; the caller may reopen without marking a gap
                    return
                except (OSError, http.client.HTTPException):
                    return   # a read timeout included: a response is not read on after a timeout (the socket refuses), so it ends and the caller reconnects
                if not raw:
                    return
                if len(raw) > V2_STREAM_LINE_MAX:
                    raise V2Error("too_large", None, "an event line over %d bytes was refused" % V2_STREAM_LINE_MAX)
                yield raw.decode("utf-8", "replace").rstrip("\r\n")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def request(self, method, target, body=None):
        """(status, text). Raises V2Unreachable (use the CLI) or V2Error (timeout, too large, or a write whose fate is unknown)."""
        with self._lock:
            for attempt in (0, 1):
                if self._info is None:
                    self._load()
                try:
                    return self._once(method, target, body)
                except _V2Unauthorized:
                    self._info = None   # the service restarted with a new password: read the file again, once
                    self.close()
                    if attempt:
                        raise V2Unreachable("the service refused the password twice") from None
                except V2Error:
                    raise
                except (OSError, http.client.HTTPException) as err:
                    self.close()
                    if isinstance(err, socket.timeout):
                        raise V2Error("timeout", None, "no answer within %ss" % self.timeout) from None
                    if method != "GET" and getattr(err, "sent", True):
                        raise V2Error("unavailable", None, "the service dropped a %s (it may or may not have been received)" % method) from None
                    self._info = None   # it may have moved: read the file again, once
                    if attempt:
                        raise V2Unreachable("the service does not answer") from None
        raise V2Unreachable("the service does not answer")

    @staticmethod
    def _budget(conn, deadline):
        """Give the socket only the time the call has left (connect, send, status line, headers and body all share ONE deadline); none left is a timeout."""
        left = deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout("the call did not finish within its total time")
        sock = getattr(conn, "sock", None)
        if sock is not None and hasattr(sock, "settimeout"):
            sock.settimeout(left)

    @staticmethod
    def _read_body(response, conn, deadline):
        """The whole reply body, at most V2_HTTP_MAX_BODY + 1 bytes, within a TOTAL deadline: each socket operation has its own timeout, so a service
        that answers a byte at a time could otherwise hold the one client far longer than `timeout`."""
        chunks, size = [], 0
        while size <= V2_HTTP_MAX_BODY:
            V2Http._budget(conn, deadline)
            chunk = response.read(min(65536, V2_HTTP_MAX_BODY + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks)

    def _once(self, method, target, body):
        deadline = time.monotonic() + self.timeout
        host, port, auth = self._info
        reuse = method == "GET" and self._conn is not None
        conn = self._conn if reuse else self.connect(host if ":" not in host else "[%s]" % host, port, timeout=self.timeout)
        headers = {"Authorization": auth, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
        sent = False
        try:
            if conn.sock is None:
                conn.connect()
            sent = True   # from here on a write may have reached the service
            self._budget(conn, deadline)
            conn.request(method, target, body=data, headers=headers)
            self._budget(conn, deadline)
            response = conn.getresponse()
            raw = self._read_body(response, conn, deadline)
        except (OSError, http.client.HTTPException) as err:
            try:
                conn.close()
            except OSError:
                pass
            if conn is self._conn:
                self._conn = None
            err.sent = sent
            raise
        if len(raw) > V2_HTTP_MAX_BODY:
            conn.close()
            self._conn = None
            raise V2Error("too_large", None, "a reply over %d bytes was refused" % V2_HTTP_MAX_BODY)
        if response.status == 401:
            conn.close()
            self._conn = None
            raise _V2Unauthorized()
        if method == "GET" and not response.will_close:
            self._conn = conn
        else:
            conn.close()
            if conn is self._conn:
                self._conn = None
        return response.status, raw.decode("utf-8", "replace")


class _V2Unauthorized(Exception):
    pass


class V2Breaker:
    """Latency-driven backoff for the calls to the service (see the section comment). `allows(essential)` returns False (skip the call), True, or
    "probe" (go, and tell `record` or `abandon` it was the probe); `record(seconds, timed_out, probe)` returns an event or None:
    {"event": "v2.backoff", "episode", "reason", "seconds"} when it opens, {"event": "v2.resume", "episode"} when it closes. One lock guards all
    of it: the daemon has threads and one client."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.samples = []
        self.timeouts = 0
        self.state = "closed"      # closed | open (waiting) | probing
        self.until = 0.0
        self.level = 0
        self.good = 0
        self.episode = 0
        self.skipped = 0
        self.probe_out = False

    def p95(self):
        with self.lock:
            return self._p95()

    def _p95(self):
        if not self.samples:
            return None
        ordered = sorted(self.samples)
        return ordered[min(len(ordered) - 1, int(math.ceil(0.95 * len(ordered))) - 1)]

    def allows(self, essential):
        """Whether this call may go now. Essential calls always may; an optional one waits out the backoff, then one probe at a time."""
        with self.lock:
            if self.state == "closed":
                return True
            if essential:
                return True
            if self.state == "open" and self.clock() >= self.until and not self.probe_out:
                self.state, self.probe_out = "probing", True
                return "probe"
            if self.state == "probing" and not self.probe_out:
                self.probe_out = True
                return "probe"
            self.skipped += 1
            return False

    def abandon(self):
        """The probe call ended in something that is not an answer or a timeout (a bug, an interrupt): free the probe slot, learn nothing."""
        with self.lock:
            self.probe_out = False

    def record(self, seconds, timed_out, probe=False):
        with self.lock:
            return self._record(seconds, timed_out, probe)

    def _record(self, seconds, timed_out, probe):
        slow = timed_out or seconds > V2_SLOW_SECONDS
        if probe and self.state == "probing":
            self.probe_out = False
            if slow:
                self.level += 1
                self.state, self.good, self.until = "open", 0, self.clock() + self._wait()
                return {"event": "v2.backoff", "episode": self.episode, "reason": "a probe was slow or timed out", "seconds": int(self._wait())}
            self.good += 1
            if self.good >= V2_RECOVER_OK:
                self.state, self.level, self.good, self.samples, self.timeouts = "closed", 0, 0, [], 0
                return {"event": "v2.resume", "episode": self.episode}
            return None
        self.samples = (self.samples + [seconds])[-V2_SLOW_WINDOW:]
        self.timeouts = self.timeouts + 1 if timed_out else 0
        if self.state != "closed":
            return None   # an essential call during a backoff: it is recorded, it changes nothing (and is never mistaken for the probe)
        reason = None
        if self.timeouts >= V2_TIMEOUT_STREAK:
            reason = "%d calls in a row timed out" % self.timeouts
        elif len(self.samples) >= V2_SLOW_MIN_SAMPLES and self._p95() > V2_SLOW_SECONDS:
            reason = "p95 latency %.1f s is over %g s" % (self._p95(), V2_SLOW_SECONDS)
        if reason is None:
            return None
        self.episode += 1
        self.level, self.good, self.state = 0, 0, "open"
        self.until = self.clock() + self._wait()
        return {"event": "v2.backoff", "episode": self.episode, "reason": reason, "seconds": int(self._wait())}

    def _wait(self):
        return min(V2_BACKOFF_MAX, V2_BACKOFF_BASE * (2 ** self.level))


class V2Client:
    """The one client for OpenCode v2. A call goes over HTTP to the background service when it can (see the transport section above), else
    through `<binary> api <operation>`; every call is counted (one record, with its transport) and timed by the breaker."""

    DELIVERIES = ("queue", "steer")
    ORDERS = ("desc", "asc")

    def __init__(self, binary, env=None, runner=subprocess.run, timeout=20, clock=time.time, http=None, monotonic=time.monotonic, transport="auto"):
        self.binary = binary
        self.env = dict(env or {})
        self.runner = runner
        self.timeout = timeout
        self.clock = clock
        self.monotonic = monotonic   # durations of calls, for the breaker: never the wall clock
        self.breaker = V2Breaker(monotonic)
        self.on_event = None      # callable(name, detail): the engine's audit of `v2.backoff` and `v2.resume`
        self.state_dir = None     # set for the daemon: where `v2-link.json` (what `status` shows) is written
        self.calls = collections.deque(maxlen=V2_CALLS_KEEP_MAX)   # (time, operation, transport) of the last V2_CALLS_KEEP_SECONDS (and at most V2_CALLS_KEEP_MAX): the calls-per-minute figures
        self.calls_lock = threading.Lock()   # the daemon has threads (the judge socket, the forwarders): one list, one lock
        self.count_errors = 0   # accounting that failed; a call never fails for it
        self._link_written = 0.0
        self._link_lock = threading.Lock()
        if http is None and transport == "auto" and runner is subprocess.run and os.environ.get("FLEET_SWITCHBOARD_V2_CLI") != "1" and os.access(str(binary), os.X_OK):
            merged = dict(os.environ)
            merged.update(self.env)
            http = V2Http(v2_service_file(merged), timeout)   # a real run only: a fake runner stands for the CLI
        self.http = http

    def count_call(self, operation, transport="cli"):
        """Remember one call and the way it travelled ("http", "cli", or "cli-fallback": the CLI after the HTTP route could not serve it).
        NEVER raises: accounting is evidence, a v2 call must not fail because of it."""
        try:
            now = self.clock()
            with self.calls_lock:
                self.calls.append((now, operation, transport))
                while self.calls and self.calls[0][0] < now - V2_CALLS_KEEP_SECONDS:
                    self.calls.popleft()
        except Exception:
            self.count_errors += 1

    def _recent(self, window, now):
        now = self.clock() if now is None else now
        with self.calls_lock:
            remembered = list(self.calls)
        return [call for call in remembered if call[0] >= now - window]

    def calls_by_operation(self, window=60.0, now=None):
        """{operation: calls} over the last `window` seconds (this process's own calls, whatever the transport)."""
        return dict(collections.Counter(call[1] for call in self._recent(window, now)))

    def calls_by_transport(self, window=60.0, now=None):
        """{transport: calls} over the last `window` seconds: how many went over HTTP, through the CLI, or through the CLI after a fallback."""
        return dict(collections.Counter(call[2] for call in self._recent(window, now)))

    def _call(self, operation, params=None, data=None, envelope=False):
        refuse_real_opencode_under_test(self.runner, self.binary)
        ticket = self.breaker.allows(self._is_essential(operation, params))
        if not ticket:
            raise V2Error("backoff", None, "%s skipped: the service is slow, optional reads wait (v2.backoff)" % operation)
        started = self.monotonic()
        try:
            text = self._transport(operation, params, data)
        except V2Error as err:
            self._record(self.monotonic() - started, err.reason == "timeout", ticket == "probe")
            raise
        except BaseException:
            if ticket == "probe":
                self.breaker.abandon()   # not an answer: free the probe slot, or optional reads would wait for ever
            raise
        self._record(self.monotonic() - started, False, ticket == "probe")
        return self._decode(operation, text, envelope)

    def _is_essential(self, operation, params=None):
        spec = V2_HTTP_OPERATIONS.get(operation)
        if spec is None or spec[0] != "GET" or operation in V2_ESSENTIAL_READS:
            return True
        return operation == "session.message.list" and (params or {}).get("type") in ("synthetic", "user")   # the delivery rule's key read, and the person-is-typing read every observation (so every re-check before a write) makes

    def _record(self, seconds, timed_out, probe=False):
        event = self.breaker.record(seconds, timed_out, probe)
        if event is not None and self.on_event is not None:
            try:
                self.on_event(event["event"], {k: v for k, v in event.items() if k != "event"})
            except Exception:   # an audit trouble never fails a call, but it is counted
                self.count_errors += 1
        self._write_link(event is not None)

    def _write_link(self, forced=False):
        """What `status` shows of the link, written by the daemon only, at most once a minute unless the breaker changed."""
        if self.state_dir is None:
            return
        with self._link_lock:   # the daemon has threads: one writer at a time, and the once-a-minute check is part of it
            self._write_link_locked(forced)

    def _write_link_locked(self, forced):
        moment = self.clock()
        if not forced and moment - self._link_written < V2_LINK_WRITE_SECONDS:
            return
        self._link_written = moment
        by = self.calls_by_transport(V2_CALLS_KEEP_SECONDS)
        body = {"at": int(moment), "transport": "http" if self.http is not None else "cli",
                "fallbacks": by.get("cli-fallback", 0), "skipped": self.breaker.skipped,
                "breaker": self.breaker.state, "episodes": self.breaker.episode,
                "p95_ms": None if self.breaker.p95() is None else int(self.breaker.p95() * 1000)}
        try:
            state_write(Path(self.state_dir) / V2_LINK_STATUS_FILE, json.dumps(body, sort_keys=True), _without(body, "at"), STATE_HEARTBEAT_SECONDS)
        except OSError:
            self.count_errors += 1   # a status line is best effort; the failure is counted, and the line shows its own age

    def _transport(self, operation, params, data):
        """The reply text of one call: over HTTP when the service answers, else through the CLI (which also starts a stopped service)."""
        spec = V2_HTTP_OPERATIONS.get(operation)
        if self.http is not None and spec is not None:
            target = self._target(spec, params)
            if target is not None:
                try:
                    status, text = self.http.request(spec[0], target, data)
                except V2Unreachable:
                    self.count_call(operation, "cli-fallback")   # the HTTP route reached nothing: the CLI call is the one that counts
                    return self._cli(operation, params, data)
                except V2Error:
                    self.count_call(operation, "http")           # it reached the service (a timeout, a refused size, a dropped write)
                    raise
                self.count_call(operation, "http")
                if status >= 400:
                    raise V2Error("http", status, _one_line(text) or "HTTP %d" % status)
                return text
        self.count_call(operation, "cli")
        return self._cli(operation, params, data)

    @staticmethod
    def _target(spec, params):
        """The request path and query of one call, or None when a parameter is one the table does not know (then the CLI decides)."""
        method, template, path_names, query_names = spec
        given = {name: value for name, value in (params or {}).items() if value is not None}
        if set(given) - set(path_names) - set(query_names) or any(name not in given for name in path_names):
            return None
        path = template
        for name in path_names:
            path = path.replace("{%s}" % name, urllib.parse.quote(str(given[name]), safe=""))
        query = [(name, str(given[name])) for name in query_names if name in given]
        return path + ("?" + urllib.parse.urlencode(query) if query else "")

    def _cli(self, operation, params, data):
        argv = [self.binary, "api", operation]
        for name, value in (params or {}).items():
            if value is not None:
                argv += ["--param", "%s=%s" % (name, value)]
        if data is not None:
            argv += ["-d", json.dumps(data, separators=(",", ":"), sort_keys=True)]
        env = dict(os.environ)
        env.update(self.env)
        env.update(NO_CREDENTIAL_DIALOG_ENV)   # #1050: the service and a headless worker's tools never open a credential dialog
        # OpenCode v2's `api` command loses the end of a large reply when it writes to a pipe and exits
        # (a real 500 KB transcript arrived cut at 293 KB, inside a string, and every read failed with
        # bad_json). Into a file the reply is whole, so the reply goes to a file and is read back.
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as sink:
            try:
                result = self.runner(argv, stdout=sink, stderr=subprocess.PIPE, text=True, timeout=self.timeout,
                                     env=env, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                raise V2Error("timeout", None, "%s did not answer within %ss" % (operation, self.timeout)) from None
            except OSError as err:
                raise V2Error("unavailable", None, "cannot run %s: %s" % (self.binary, err.strerror or err)) from None
            stdout = result.stdout
            if stdout is None:  # a real run wrote to the file
                sink.seek(0)
                stdout = sink.read()
        stdout = stdout or ""
        stderr = result.stderr or ""
        if result.returncode != 0:
            match = re.search(r"\bHTTP (\d{3})\b", stderr)
            if match:
                # v2 put the error body on stdout and `HTTP <code> <reason>` on stderr.
                raise V2Error("http", int(match.group(1)), _one_line(stdout) or _one_line(stderr))
            raise V2Error("exit", None, _one_line(stderr) or _one_line(stdout) or "exit %d" % result.returncode)
        return stdout

    @staticmethod
    def _decode(operation, stdout, envelope):
        if not (stdout or "").strip():
            return None
        try:
            body = json.loads(stdout)
        except ValueError:
            raise V2Error("bad_json", None, "%s returned non-JSON: %s" % (operation, _one_line(stdout, 120))) from None
        if isinstance(body, dict) and "data" in body:
            return body if envelope else body["data"]  # PR 8: paging needs the cursor beside the data
        return body

    @staticmethod
    def _limit(limit):
        """A page of a transcript is never larger than V2_READ_LIMIT_MAX messages, whatever a caller asks."""
        return min(limit, V2_READ_LIMIT_MAX) if isinstance(limit, int) and not isinstance(limit, bool) else limit

    def service_identity(self):
        """The service's process id (changes when it restarts), or None (the CLI transport, no readable service file, or the process is gone)."""
        return self.http.service_identity() if self.http is not None and hasattr(self.http, "service_identity") else None

    def service_url(self):
        """Where the service is, `http://127.0.0.1:<port>`, or None (the CLI transport, or no readable service file)."""
        return self.http.base_url() if self.http is not None and hasattr(self.http, "base_url") else None

    def server_info(self):
        return self._call("server.info")

    def model_list(self):
        """`model.list` (GET /api/model): the LOCAL service's models, `[{providerID, id, variants: [{id}]}]` (#1642). A read of the service, never of a provider."""
        return self._call("model.list")

    def session_create(self, agent, title, directory, metadata, permissions=None, model=None):
        if not os.path.isabs(directory):
            raise ValueError("directory must be absolute: %r" % directory)
        data = {"agent": agent, "title": title, "location": {"directory": directory},
                "metadata": metadata}
        if permissions is not None:
            data["permissions"] = permissions
        if model is not None:   # #1642: {providerID, id[, variant]}, only when a launch chose one
            data["model"] = model
        return self._call("session.create", data=data)

    def session_get(self, session_id):
        return self._call("session.get", {"sessionID": session_id})

    def session_active(self):
        return self._call("session.active")

    def session_remove(self, session_id):
        """`session.remove`: delete a session (DELETE /api/session/{sessionID}, an empty answer; 404 SessionNotFoundError when it is gone).
        Used only to undo a launch that failed after it created its session."""
        return self._call("session.remove", {"sessionID": session_id})

    def message_list(self, session_id, limit=None, order=None, type=None, cursor=None):
        if order is not None and order not in self.ORDERS:
            raise ValueError("order must be one of %s" % ", ".join(self.ORDERS))
        return self._call("session.message.list", {
            "sessionID": session_id, "limit": self._limit(limit), "order": order, "type": type, "cursor": cursor})

    def message_page(self, session_id, limit=None, type=None, cursor=None):
        """PR 8: one page of `session.message.list` with its cursor: (messages, cursor).

        `cursor` is v2's `{"previous": .., "next": ..}` object, or None when the
        answer carries none. A cursor argument is v2's opaque token and fixes the
        order, so no `order` is taken here (v2: do not combine them).
        """
        if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > CURSOR_LIMIT):
            raise ValueError("cursor must be v2's opaque token (a non-empty string), got %r" % (cursor,))
        body = self._call("session.message.list", {
            "sessionID": session_id, "limit": self._limit(limit), "type": type, "cursor": cursor}, envelope=True)
        if isinstance(body, dict):
            found = body.get("cursor")
            return (body["data"] if isinstance(body.get("data"), list) else []), \
                (found if isinstance(found, dict) else None)
        return (body if isinstance(body, list) else []), None

    def message_pages(self, session_id, limit, type=None, max_pages=1):
        """PR 8: the pages of one listing, newest first, following `cursor.next`.

        UNVERIFIED: that `next` moves to older messages in a newest-first
        listing; the Lab's paging spike must show it. Yields (messages, more):
        `more` is True while v2 names a next cursor, False when it says there is
        none, None when the answer carries no cursor at all. The walk ends after
        a False or None, or after `max_pages`. A continuation that is empty or
        repeats a message already seen means the cursor does not walk the
        listing as assumed: that raises, and the caller holds.
        """
        cursor, seen = None, set()
        for number in range(max_pages):
            page, found = self.message_page(session_id, limit, type, cursor)
            ids = [m.get("id") for m in page if isinstance(m, dict)]
            if number and not page:
                # v2 names a `next` cursor even when the page it just gave held the whole listing (measured:
                # 104 messages at limit 200); following it answers an empty page. That is the end, not a
                # cursor that fails to walk older.
                yield [], False
                return
            if number and any(i in seen for i in ids):
                raise V2Error("bad_json", None, "session.message.list: the next page repeated "
                                                "a message; the cursor does not walk older as assumed")
            seen.update(ids)
            cursor = found.get("next") if found else None
            more = None if found is None else bool(cursor)
            yield page, more
            if not more:
                return

    def synthetic(self, session_id, text, metadata, resume, delivery, message_id, description=None):
        """`description` is the one human line the TUI shows for this message (`session.synthetic` takes it,
        measured in PR 13): at most DESCRIPTION_MAX characters, one line. Every message the switchboard writes
        carries one."""
        if description is not None and not valid_description(description):
            raise ValueError("description must be one non-empty line of at most %d characters: %r"
                             % (DESCRIPTION_MAX, description))
        if not isinstance(resume, bool):
            raise ValueError("resume must be true or false")
        if delivery not in self.DELIVERIES:
            raise ValueError("delivery must be one of %s" % ", ".join(self.DELIVERIES))
        if not isinstance(message_id, str) or not message_id.startswith("msg_"):
            raise ValueError("message id must start with msg_: %r" % message_id)
        body = {"id": message_id, "text": text, "metadata": metadata, "resume": resume, "delivery": delivery}
        if description is not None:
            body["description"] = description
        return self._call("session.synthetic", {"sessionID": session_id}, body)

    def interrupt(self, session_id, resume=False):
        """`session.interrupt`: abort the active execution (the Escape key). VERIFIED in a throwaway v2 2.0.24:
        the in-flight tool call is cut (state error, aborted), the answer is {"interrupted": true} (false when
        idle), and with `resume` false nothing runs after it: a steering input waiting stays in the inbox."""
        return self._call("session.interrupt", {"sessionID": session_id, "resume": "true" if resume else "false"})

    def prompt(self, session_id, text, metadata=None, delivery=None, resume=None):
        if delivery is not None and delivery not in self.DELIVERIES:
            raise ValueError("delivery must be one of %s" % ", ".join(self.DELIVERIES))
        data = {"text": text}
        for name, value in (("metadata", metadata), ("delivery", delivery), ("resume", resume)):
            if value is not None:
                data[name] = value
        return self._call("session.prompt", {"sessionID": session_id}, data)

    def inbox_list(self, session_id):
        return self._call("session.inbox.list", {"sessionID": session_id})

    def permission_list(self, session_id):
        """The session's pending permission requests (S2: `session.permission.list`
        works with the session id alone; `permission.request.list` needs a location)."""
        return self._call("session.permission.list", {"sessionID": session_id})

    def form_list(self, session_id):
        """The session's pending forms (questions): `session.form.list`, an array of {id, sessionID, title,
        fields: [{key, type, title, ...}]} (VERIFIED in a throwaway v2 2.0.24, PR 18)."""
        return self._call("session.form.list", {"sessionID": session_id})

    def permission_reply(self, session_id, request_id, decision, message=None):
        """PR 18: answer a pending permission request: `decision` is `once`, `always` or `reject`, `message` a
        reason (VERIFIED in a throwaway v2 2.0.24: POST /api/session/{sessionID}/permission/{requestID}/reply,
        body {"decision", "message"}; an empty answer; 404 PermissionNotFoundError once answered)."""
        if decision not in ("once", "always", "reject"):
            raise ValueError("decision must be once, always or reject")
        data = {"decision": decision}
        if message:
            data["message"] = message
        return self._call("session.permission.reply", {"sessionID": session_id, "requestID": request_id}, data)

    def form_reply(self, session_id, form_id, answer):
        """PR 18: answer a pending form (a question): `answer` maps each field's key to its value (VERIFIED in a
        throwaway v2 2.0.24: POST /api/session/{sessionID}/form/{formID}/reply, body {"answer": {key: value}};
        an empty answer; the form then reads `state.status` answered)."""
        return self._call("session.form.reply", {"sessionID": session_id, "formID": form_id}, {"answer": dict(answer)})

    def inbox_update(self, session_id, inbox_id, delivery):
        if delivery not in self.DELIVERIES:
            raise ValueError("delivery must be one of %s" % ", ".join(self.DELIVERIES))
        return self._call("session.inbox.update", {"sessionID": session_id, "inboxID": inbox_id},
                          {"delivery": delivery})
