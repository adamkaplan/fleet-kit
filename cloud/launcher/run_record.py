"""The run record: what a cloud worker did, kept on the operator's machine after the pod is gone (#638).

One function, `Recorder.record`, called by the launcher's `stop` BEFORE anything is deleted, so every caller of `stop`
(the run-task driver, the switchboard's retire, a person) gets a record. It opens its own port-forward to the pod, reads
the server password and the run secrets from the pod's own mounts (`kubectl exec ... cat /run/cw/...`; the operator role
can exec), reads the session's messages, scrubs every STRING VALUE of every message with the launcher's ONE `scrub`
(never the serialized line: the JSON stays JSON) and writes, under `<root>/<run id>/` (0700; files 0600):

  transcript.jsonl   one scrubbed JSON object per message, at most CW_RECORD_MAX_BYTES (default 5 MiB), then a marker line
  tally.json         tool calls by tool name and the turn count; no content
  run.json           worker, repository, start, end, exit reason, head sha when known, `transcript: unavailable` and why

`record` never raises (any Exception is caught: a record that fails must not keep a pod). ONE monotonic deadline bounds
the whole phase: CW_RECORD_SECONDS (default 90, hard maximum 240; STOP_SECONDS is 600). Every kubectl call is given only
the time that is left, the transcript is read in chunks with the clock checked on each, a watchdog closes the connection
at the deadline, and the port-forward is killed. At the deadline the record says `transcript: unavailable` with the reason
`record timed out` and the stop goes on to the end hook and the delete: a worker cannot prevent its own cleanup by being
slow. A KeyboardInterrupt or the driver's interrupt passes through.

The transcript is the worker's text, so it is hostile input: the body is size-capped and its bracket depth is checked by
a scan that parses nothing BEFORE `json.loads`; it is then walked with an explicit stack.
"""
import base64
import http.client
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time

MAX_BODY = 8 * 1024 * 1024      # a larger answer is not read (a cut JSON text cannot be parsed anyway)
MAX_DEPTH = 100                  # real messages nest a few dozen levels; deeper is refused before json.loads
TRANSCRIPT_CAP = 5 * 1024 * 1024
KEEP_DAYS = 30                   # prune-records default; CW_RECORD_KEEP_DAYS overrides
LINE_MAX = 200000
TOOL_PART_TYPES = {"tool", "tool-call", "tool_call", "tool-use", "tool_use", "tool-invocation"}
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}-\d{8}T\d{6}Z-[0-9a-f]{6}$")
RECORD_FILES = {"transcript.jsonl", "tally.json", "run.json"}
SECRET_FILES_MAX = 10
RECORD_SECONDS = 90.0            # the default overall deadline of the record phase (less than half of the 600 s stop)
RECORD_SECONDS_MAX = 240.0       # the hard maximum: CW_RECORD_SECONDS above it is cut to it
CHUNK = 64 * 1024
TIMED_OUT = "record timed out"


class RecordTimeout(Exception):
    pass


def record_seconds(env):
    try:
        v = float(env.get("CW_RECORD_SECONDS") or RECORD_SECONDS)
    except ValueError:
        v = RECORD_SECONDS
    if not v > 0:   # zero, negative or nan
        v = RECORD_SECONDS
    return min(v, RECORD_SECONDS_MAX)


class TooDeep(ValueError):
    pass


def depth_ok(body, limit=MAX_DEPTH):
    """True when no array/object in the JSON text `body` (bytes) nests deeper than `limit`. A bracket scan that skips
    string contents and parses nothing: a loop over the bytes, no recursion."""
    depth, in_str, esc = 0, False, False
    for c in body:
        if in_str:
            if esc:
                esc = False
            elif c == 0x5C:
                esc = True
            elif c == 0x22:
                in_str = False
        elif c == 0x22:
            in_str = True
        elif c in (0x5B, 0x7B):
            depth += 1
            if depth > limit:
                return False
        elif c in (0x5D, 0x7D):
            depth -= 1
    return True


def walk_scrub(node, scrub, names):
    """A scrubbed copy of `node`: each string value (and key) through `scrub`, tool names collected into `names`.
    Uses an explicit stack, so no depth can overflow the interpreter's stack."""
    root = [None]
    stack = [(node, root, 0)]
    while stack:
        cur, holder, key = stack.pop()
        if isinstance(cur, str):
            holder[key] = scrub(cur)
        elif isinstance(cur, dict):
            out = {}
            holder[key] = out
            if cur.get("type") in TOOL_PART_TYPES:
                n = cur.get("tool") or cur.get("name") or cur.get("toolName")
                names.append(n if isinstance(n, str) and n else "unknown")
            for k, v in cur.items():
                stack.append((v, out, scrub(k) if isinstance(k, str) else str(k)))
        elif isinstance(cur, list):
            out = [None] * len(cur)
            holder[key] = out
            for i, v in enumerate(cur):
                stack.append((v, out, i))
        else:
            holder[key] = cur
    return root[0]


def default_root(env):
    audit = env.get("CW_CLOUD_AUDIT") or os.path.join(
        env.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "cw-cloud", "audit.jsonl")
    return os.path.join(os.path.dirname(audit), "runs")


def private_root(root):
    """Make `root` (0700) and refuse a symbolic link there: neither the records nor the chmod may follow a link."""
    try:
        st = os.lstat(root)
    except FileNotFoundError:
        os.makedirs(root, mode=0o700, exist_ok=True)
        st = os.lstat(root)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise OSError("the record directory is a symbolic link or not a directory: refused")
    os.chmod(root, 0o700)
    return root


def write_private(directory, name, text):
    final = os.path.join(directory, name)
    part = final + ".part"   # a crash leaves only the .part, never a torn record file
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(part, final)


class Recorder:
    def __init__(self, kube, env, clock=time.monotonic):
        self.kube, self.env, self.clock = kube, env, clock
        self.deadline = None

    def left(self):
        """Seconds of the record phase that remain; raises RecordTimeout when none. No deadline set: effectively none."""
        if self.deadline is None:
            return 120.0
        r = self.deadline - self.clock()
        if r <= 0:
            raise RecordTimeout(TIMED_OUT)
        return r

    def _exec(self, pod, *cmd, **kw):
        return self.kube.exec(pod, *cmd, timeout=min(120.0, self.left()), **kw)

    def root(self, override=None):
        return override or self.env.get("CW_RUN_RECORD_DIR") or default_root(self.env)

    def _learn_secrets(self, pod):
        """Register the pod's run secrets (so the scrub knows them) and return the server password."""
        password = ""
        try:
            names = [n for n in self._exec(pod, "ls", "-1", "/run/cw").split()
                     if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", n)][:SECRET_FILES_MAX]
        except RecordTimeout:
            raise
        except Exception:
            self.left()   # a call cut by the deadline is a timeout, not "no secrets"
            names = []
        for n in names:
            try:
                text = self._exec(pod, "cat", "/run/cw/" + n)[:1024 * 1024]
            except RecordTimeout:
                raise
            except Exception:
                self.left()
                continue
            self.kube.add_secrets(text.strip())
            if n == "server-password":
                password = text.strip()
        return password

    def _api(self, port, password, path):
        """(status, body). Bounded by the record deadline: the socket never waits longer than what is left, the body is
        read in chunks with the clock checked on each, and a watchdog closes the connection at the deadline."""
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=min(30.0, self.left()))
        dog = None
        try:
            c.connect()
            sock = c.sock   # kept: an HTTP/1.0 answer detaches it from the connection once the headers are in
            dog = threading.Timer(self.left(), lambda: self._close_now(sock))
            dog.daemon = True
            dog.start()
            c.request("GET", path, headers={"Authorization": "Basic " + base64.b64encode(("opencode:" + password).encode()).decode()})
            r = c.getresponse()
            body = b""
            while len(body) <= MAX_BODY:
                self.left()
                chunk = r.read(min(CHUNK, MAX_BODY + 1 - len(body)))
                if not chunk:
                    break
                body += chunk
            return r.status, body
        except (OSError, http.client.HTTPException):
            self.left()   # a connection the watchdog closed is a timeout, not a plain failure
            raise
        finally:
            if dog is not None:
                dog.cancel()
            c.close()

    @staticmethod
    def _close_now(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _forward(self, pod, tmpdir):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        k = self.kube
        cmd = [k.bin] + (["--kubeconfig", k.kubeconfig] if k.kubeconfig else []) + ["--context", k.context, "-n", k.ns,
               "port-forward", "pod/" + pod, "%d:4096" % port, "--address", "127.0.0.1"]
        log = open(os.path.join(tmpdir, "pf.log"), "wb")
        pf = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=k.env, start_new_session=True)
        return pf, port, log

    @staticmethod
    def _json(body):
        if len(body) > MAX_BODY:
            raise TooDeep("the answer is over %d bytes" % MAX_BODY)
        if not depth_ok(body):
            raise TooDeep("the answer nests deeper than %d levels" % MAX_DEPTH)
        return json.loads(body)

    def read_messages(self, pod):
        """(messages, None) or (None, why). Never raises an Exception."""
        pf, log, tmp = None, None, None
        try:
            password = self._learn_secrets(pod)
            if not password:
                return None, "the server password could not be read from the pod"
            tmp = tempfile.mkdtemp(prefix="cw-rec-")
            pf, port, log = self._forward(pod, tmp)
            end = self.clock() + float(self.env.get("CW_RECORD_TIMEOUT") or 30)
            while True:
                try:
                    if self._api(port, password, "/api/info")[0] == 200:
                        break
                except (OSError, http.client.HTTPException):
                    pass
                if pf.poll() is not None or self.clock() >= end:
                    return None, "the pod's server could not be reached"
                time.sleep(min(0.3, self.left()))
            st, body = self._api(port, password, "/api/session")
            if st != 200:
                return None, "the server answered status %d" % st
            sessions = [s for s in (self._json(body).get("data") or []) if isinstance(s, dict) and isinstance(s.get("id"), str)]
            if not sessions:
                return None, "the server holds no session"

            def updated(s):
                t = s["time"].get("updated") if isinstance(s.get("time"), dict) else 0
                return t if isinstance(t, (int, float)) else 0
            sid = max(sessions, key=updated)["id"]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", sid):
                return None, "the session id is not usable"
            st, body = self._api(port, password, "/api/session/%s/message" % sid)
            if st != 200:
                return None, "the server answered status %d" % st
            msgs = self._json(body).get("data")
            if not isinstance(msgs, list):
                return None, "the messages were not a list"
            return msgs, None
        except RecordTimeout:
            return None, TIMED_OUT
        except TooDeep as e:
            return None, "the messages were refused: %s" % e
        except Exception:   # RecursionError, MemoryError, a bad shape, a closed socket: unavailable, and the stop goes on
            return None, "the messages could not be read"
        finally:
            if pf is not None:
                if pf.poll() is None:   # a reaped child's pid may already be another process's: signal only a live one
                    try:
                        os.killpg(pf.pid, 15)
                    except OSError:
                        pass
                try:
                    pf.wait(timeout=5)
                except Exception:
                    try:
                        os.killpg(pf.pid, 9)
                    except OSError:
                        pass
            if log is not None:
                log.close()
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)

    def head_sha(self, pod):
        try:
            out = self._exec(pod, "git", "-C", self.env.get("CW_WORKDIR") or "/home/worker", "rev-parse", "HEAD", keep_stdout=True)
        except Exception:
            return None
        sha = (out.strip().splitlines() or [""])[0]
        return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None

    def record(self, worker, pod, reason, repo="", started="", root=None):
        """Save the run's record. Returns the record directory ('' when none could be made). Never raises an Exception."""
        run_id = "%s-%s-%s" % (worker, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), secrets.token_hex(3))
        info = {"worker": worker, "repository": repo, "run_id": run_id, "start": started, "end": None,
                "exit_reason": "", "head_sha": None, "transcript": "unavailable", "reason": "the record was not finished"}
        directory = ""
        try:
            info["exit_reason"] = self.kube.scrub(reason, limit=300)
            base = private_root(self.root(root))
            directory = os.path.join(base, run_id)
            os.mkdir(directory, 0o700)
            self.deadline = self.clock() + record_seconds(self.env)
            try:
                msgs, why = self.read_messages(pod)
                if msgs is None:
                    info["reason"] = why
                else:
                    self._write_transcript(directory, msgs, info)
            finally:
                info["head_sha"] = self.head_sha(pod)
        except Exception as e:   # includes RecursionError and MemoryError; an interrupt (BaseException) passes through
            try:
                info["reason"] = "the record could not be written: %s" % self.kube.scrub(e, limit=200)
            except Exception:
                info["reason"] = "the record could not be written"
        finally:
            info["end"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if info["reason"] is None:
                del info["reason"]
            if directory and os.path.isdir(directory):
                try:
                    write_private(directory, "run.json", json.dumps(info, sort_keys=True) + "\n")
                except Exception:
                    pass
        return directory

    @staticmethod
    def add_hook(directory, hook):
        """A second atomic write of run.json once the end hook has answered (#829): adds `end_hook` (pushed, wip_branch,
        skipped, note, error, or why it did not run). Never raises; a record that cannot be updated keeps what it had."""
        try:
            path = os.path.join(directory, "run.json")
            with open(path) as f:
                info = json.load(f)
            info["end_hook"] = hook
            write_private(directory, "run.json", json.dumps(info, sort_keys=True) + "\n")
        except Exception:
            pass

    def _write_transcript(self, directory, msgs, info):
        cap = int(self.env.get("CW_RECORD_MAX_BYTES") or TRANSCRIPT_CAP)
        scrub = lambda s: self.kube.scrub(s, limit=2 * LINE_MAX * 2)   # above the input maximum: redaction growth is never cut
        lines, size, cut, msg_cut, tally, turns = [], 0, False, False, {}, 0
        for m in msgs:
            names = []
            line = json.dumps(walk_scrub(m, scrub, names), sort_keys=True)
            for n in names:
                n = self.kube.scrub(n, limit=64)
                tally[n] = tally.get(n, 0) + 1
            if isinstance(m, dict) and m.get("type") == "assistant":
                turns += 1
            if len(line) > LINE_MAX:   # a message is kept whole or replaced by a marker, never cut mid-value
                line = json.dumps({"truncated": True, "note": "MESSAGE CUT: over %d characters" % LINE_MAX})
                msg_cut = True
            if size + len(line.encode()) + 1 > cap and not cut:
                cut = True
                lines.append(json.dumps({"truncated": True, "note": "TRANSCRIPT CUT: over %d bytes; the rest is not kept" % cap}))
            if not cut:
                lines.append(line)
                size += len(line.encode()) + 1
        write_private(directory, "transcript.jsonl", "".join(x + "\n" for x in lines))
        write_private(directory, "tally.json", json.dumps({"tool_calls": tally, "turns": turns, "messages": len(msgs)}, sort_keys=True) + "\n")
        info["transcript"], info["reason"] = "saved", None
        info["transcript_truncated"] = cut or msg_cut


def prune(root, days, now=None):
    """Remove the per-run directories of this tool under `root` older than `days`. Only a real directory named like a
    run id and holding nothing but this tool's files (or their .part) is removed; a link, a stray file or a foreign
    directory is left alone. Returns the names removed."""
    now = now if now is not None else time.time()
    removed = []
    try:
        st = os.lstat(root)
    except OSError:
        return removed
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise OSError("the record directory is a symbolic link or not a directory: refused")
    ours = RECORD_FILES | {n + ".part" for n in RECORD_FILES}
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        try:
            s = os.lstat(path)
            if not RUN_ID_RE.match(name) or not stat.S_ISDIR(s.st_mode) or now - s.st_mtime < days * 86400:
                continue
            inside = os.listdir(path)
            if any(n not in ours or not stat.S_ISREG(os.lstat(os.path.join(path, n)).st_mode) for n in inside):
                continue
            for n in inside:
                os.unlink(os.path.join(path, n))
            os.rmdir(path)
            removed.append(name)
        except OSError:
            continue
    return removed
