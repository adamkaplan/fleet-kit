"""The Floor remote-worker transport (ask #2564): a repository that routes to Floor is worked by an ACA sandbox author, not by a pod.

Loaded by bin/cloud_inspect.py: its `bind` wraps `_launch`, `cmd_send`, `cmd_read`, `status_lines` and `cmd_retire` of bin/cloud_worker.py. Every wrapper
decides by the RECORD (`backend` is `floor` or a pod), never by the setting of the day. Settings (CW_REMOTE_BACKEND, CW_FLOOR_*, CW_POD_ROLLBACK_WINDOW)
live in cloud.env or the environment. The transport has five calls (submit, message, cancel, observe, result); commands are idempotent by key.
"""
import base64
import hashlib
import hmac
import html
import json
import os
import time
import re
import signal
import stat
import subprocess
import sys
import tempfile
import types
import urllib.error
import urllib.parse
import urllib.request
D: object   # set by the loader before this file runs
TRANSPORT = None   # a test seam: an object with submit, message, cancel, observe and result
ENDPOINT_URL = "https://floor.example.internal"   # the placeholder the record carries: the real URL stays in the private settings
TIMEOUT_SECONDS = 30
RESPONSE_MAX = 1 << 20
PAYLOAD_LIMIT = 60 * 1024   # a queue message whose base64 text is longer than this carries its brief or text in a payload blob
TOKEN_MAX = 512
DEFAULT_PROJECT_ID = "fleetkit"
CALLER_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
NOT_AUTHORS = ("orchestrator",)   # an agent that is not a sandbox author
AUTHORS = ("coder", "worker")
FLOOR_AUTHOR_PREAMBLE = """Floor Author Contract (brief in, commit out):
- Work in the repository checkout on branch %(branch)s (created from %(base)s).
- Use bounded local edits and git commit only: do NOT run git push, gh pr create, prove, verdict, or spire land in this sandbox.
- When your candidate commit is ready, commit it cleanly and report done with the candidate commit SHA so Floor's workflow and heavy executor run spire land and deliver it.

Task:
"""
DELIVERY_LINE = re.compile(
    r"\bgit\s+push\b|\bgh\s+pr\s+(?:create|edit|merge)\b|(?<![\w-])(?:prove|verdict)\s+(?:--|\S+@)|fleet-switchboard\s+heavy|nohup\b.*\bspire\s+land\b|"
    r"/work/land\.log|\bspire\s+land\b", re.I)
REMOVED_LINE = "(a delivery step stood here: Floor delivers the commit, so there is nothing to run for it)"

class FloorError(Exception):
    """A call of Floor that did not finish; `status` is the HTTP status or None, the text is already scrubbed."""
    def __init__(self, text, status=None):
        super().__init__(text)
        self.status = status

class UnavailableArtifactError(FloorError):
    """Floor answered, but an artifact it names (`art-...`) cannot be read now."""
    def __init__(self, artifact, status=503):
        super().__init__("the artifact %s is unavailable" % artifact, status)
        self.artifact = artifact

def env_of():
    return os.environ

def scrub(text, limit=200):
    """One line of `text` with every URL, host, bearer token, signature and long opaque string of the settings taken out."""
    text = str(text)
    env = env_of()
    url = (env.get("CW_FLOOR_URL") or "").strip()
    forms = [url, urllib.parse.urlsplit(url).netloc if url else ""]
    for key in ("CW_FLOOR_QUEUE_URL", "CW_FLOOR_BLOB_URL"):
        value = (env.get(key) or "").strip()
        forms += [value, urllib.parse.urlsplit(value).netloc if value else ""]
    for key in ("CW_FLOOR_QUEUE_SAS", "CW_FLOOR_BLOB_SAS"):
        value = (env.get(key) or "").strip().lstrip("?")
        forms += [value] if len(value) > 3 else []
        forms += [part.partition("=")[2] for part in value.split("&") if len(part.partition("=")[2]) >= 6]   # each parameter value of the SAS alone
    for key in ("CW_FLOOR_QUEUE_SAS_FILE", "CW_FLOOR_BLOB_SAS_FILE"):
        sfile = (env.get(key) or "").strip()
        if sfile:
            try:
                value = open(sfile).read(TOKEN_MAX).strip().lstrip("?")
                forms += [value] if len(value) > 3 else []
                forms += [part.partition("=")[2] for part in value.split("&") if len(part.partition("=")[2]) >= 6]
            except OSError:
                pass
    token_file = env.get("CW_FLOOR_TOKEN_FILE")
    if token_file:
        try:
            forms.append(open(token_file).read(TOKEN_MAX).strip())
        except OSError:
            pass
    for form in sorted((f for f in forms if f), key=len, reverse=True):
        text = text.replace(form, "<floor>")
    text = re.sub(r"https?://\S+", "<url>", text)
    text = re.sub(r"(?i)\b(sig|sv|se|sp|sas|token|access_token|key|bearer)([=:\s]+)[^&\s]+", r"\1\2<hidden>", text)
    text = re.sub(r"\b(?![0-9a-f]{40}\b|[0-9a-f]{64}\b)[A-Za-z0-9_+/=-]{40,}\b", "<hidden>", text)
    return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text).split())[:limit]

def _matches(entry, repo):
    entry, repo = entry.strip().lower(), repo.lower()
    if entry == "*":
        return True
    owner, _, name = repo.partition("/")
    e_owner, _, e_name = entry.partition("/")
    return bool(e_name) and e_owner == owner and e_name in ("*", name)

def route_repo(repo, config, env=None):
    """`floor` or `pod` for a repository. `CW_REMOTE_BACKEND=pod` is the rollback switch and wins; else `floor` when the backend is `floor` or the repository is in CW_FLOOR_REPOS; else `pod`. `config` is the switchboard's."""
    env = env_of() if env is None else env
    backend = (env.get("CW_REMOTE_BACKEND") or "").strip().lower()
    if backend == "pod":
        return "pod"
    if backend == "floor":
        return "floor"
    listed = [e for e in (env.get("CW_FLOOR_REPOS") or "").split(",") if e.strip()]
    return "floor" if isinstance(repo, str) and any(_matches(e, repo) for e in listed) else "pod"

def is_floor(record):
    """A record belongs to Floor by its own `backend`, never by the setting of the day."""
    return isinstance(record, dict) and record.get("backend") == "floor"

def floor_who():
    who = (env_of().get("CW_FLOOR_CALLER") or "fleet").strip()
    if not CALLER_RE.fullmatch(who):
        raise D["Refusal"]("CW_FLOOR_CALLER is not a caller id (a-z, 0-9, _ and -, up to 32); nothing was created")
    return who

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def _request(method, url, data=None, headers=(), missing_ok=False):
    """(status, body bytes) of one request: no redirect followed, 30 s, the answer cut at 1 MiB. An HTTP error is a FloorError (a 503 that names `art-...` is an UnavailableArtifactError); a 404 is returned as (404, b"") when `missing_ok`."""
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in headers:
        request.add_header(key, value)
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=TIMEOUT_SECONDS) as answer:
            status, raw = answer.status, answer.read(RESPONSE_MAX + 1)
    except urllib.error.HTTPError as err:
        if err.code == 404 and missing_ok:
            return 404, b""
        raw = err.read(RESPONSE_MAX + 1) if err.fp else b""
        found = re.search(r"\bart-[A-Za-z0-9]{1,40}\b", raw[:RESPONSE_MAX].decode("utf-8", "replace"))
        if err.code == 503 and found:
            raise UnavailableArtifactError(found.group(0)) from None
        raise FloorError("Floor answered HTTP %d" % err.code, err.code) from None
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise FloorError("Floor could not be reached (%s)" % scrub(type(err).__name__, 60)) from None
    if len(raw) > RESPONSE_MAX:
        raise FloorError("Floor's answer is larger than %d bytes" % RESPONSE_MAX)
    return status, raw

def _parse(raw):
    try:
        parsed = D["parse_bounded"](raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise FloorError("Floor's answer is not JSON") from None
    if not isinstance(parsed, dict):
        raise FloorError("Floor's answer is not an object")
    return parsed
FINISHED_STATES = ("retired", "done", "failed")

class HttpTransport:
    """The five calls of the Floor caller API over HTTP: a JSON body in, a bounded JSON answer out. Paths are under /v1/callers/<caller>/."""
    def __init__(self, base, token=None):
        self.base, self.token = base.rstrip("/"), token
    def _call(self, method, path, body=None):
        data = None if body is None else json.dumps(body, sort_keys=True).encode()
        headers = [("Accept", "application/json")] + ([("Content-Type", "application/json")] if data is not None else [])
        if self.token:
            headers.append(("Authorization", "Bearer " + self.token))
        return _parse(_request(method, self.base + path, data, headers)[1])
    def _root(self, caller):
        return "/v1/callers/" + urllib.parse.quote(caller, safe="")
    def submit(self, caller, cmd_id, **fields):
        return self._call("POST", self._root(caller) + "/submissions", dict(fields, cmd_id=cmd_id))
    def message(self, caller, work_id, cmd_id, seq, text):
        return self._call("POST", "%s/works/%s/messages" % (self._root(caller), urllib.parse.quote(work_id, safe="")), {"cmd_id": cmd_id, "seq": seq, "text": text})
    def cancel(self, caller, work_id, cmd_id, seq):
        return self._call("POST", "%s/works/%s/cancel" % (self._root(caller), urllib.parse.quote(work_id, safe="")), {"cmd_id": cmd_id, "seq": seq})
    def observe(self, caller, work_id):
        return self._call("GET", "%s/works/%s" % (self._root(caller), urllib.parse.quote(work_id, safe="")))
    def result(self, caller, work_id):
        return self._call("GET", "%s/works/%s/result" % (self._root(caller), urllib.parse.quote(work_id, safe="")))

def rfc3339(moment=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if moment is None else moment))

class QueueBlobTransport:
    """Floor's Queue + Blob v1 wire format. A command is JSON, base64 inside a <QueueMessage> POSTed to <queue>/messages; the state is published by Floor as state.json and msg/<session>.json under the blob URL. The work id is the session id. A command is never acknowledged on the call: `cancel` reads the published state and says `reserved` until the session is `retired`; a submit or a send is applied once by Floor (it acks a repeated cmd_id `duplicate`)."""
    def __init__(self, queue_url, queue_sas, blob_url, blob_sas):
        self.queue, self.queue_sas, self.blob, self.blob_sas = queue_url.rstrip("/"), (queue_sas or "").lstrip("?"), blob_url.rstrip("/"), (blob_sas or "").lstrip("?")
    @staticmethod
    def _url(base, path, sas):
        return base + path + ("?" + sas if sas else "")
    def _enqueue(self, command, payload=None):
        """Send `command` (a dict); `payload` holds the large fields (brief or text), which go in the message or, when it would be over 60 KiB, in a blob."""
        full = dict(command, **(payload or {}))
        text = base64.b64encode(json.dumps(full, sort_keys=True).encode("utf-8")).decode("ascii")
        if payload and len(text) > PAYLOAD_LIMIT:
            path = "payload/%s.json" % command["cmd_id"]
            blob = json.dumps(dict({"env": {}}, **payload), sort_keys=True).encode("utf-8")
            _request("PUT", self._url(self.blob, "/" + path, self.blob_sas), blob, [("x-ms-blob-type", "BlockBlob"), ("Content-Type", "application/json")])
            text = base64.b64encode(json.dumps(dict(command, payload=path), sort_keys=True).encode("utf-8")).decode("ascii")
        body = ("<QueueMessage><MessageText>%s</MessageText></QueueMessage>" % text).encode("ascii")
        _request("POST", self._url(self.queue, "/messages", self.queue_sas), body, [("Content-Type", "application/xml")])
    def _command(self, op, cmd_id, session_id, seq, **more):
        return dict({"v": 1, "cmd_id": cmd_id, "op": op, "session_id": session_id, "seq": int(seq), "at": rfc3339()}, **more)
    def _state(self):
        state = _parse(_request("GET", self._url(self.blob, "/state.json", self.blob_sas), None, [("Accept", "application/json")])[1])
        sessions, acks = state.get("sessions"), state.get("acks")
        if state.get("v") != 1 or not isinstance(sessions, dict):
            raise FloorError("the published state is not v1")
        state["floor_seq"] = state.get("seq", state.get("state_seq"))
        state["acks"] = acks if isinstance(acks, dict) else {}
        return state
    def _blob(self, session_id):
        status, raw = _request("GET", self._url(self.blob, "/msg/%s.json" % urllib.parse.quote(session_id, safe=""), self.blob_sas), None, [("Accept", "application/json")], missing_ok=True)
        return {} if status == 404 else _parse(raw)
    def submit(self, caller, cmd_id, session_id=None, repo=None, ref=None, brief="", mode=None, env=None, priority_class=None, preview=False, project_id=None, **ignored):
        pid = project_id or DEFAULT_PROJECT_ID
        pclass = priority_class or "production"
        extras = {"class": pclass, "priority_class": pclass} if pclass != "production" else {}
        command = self._command("launch", cmd_id, session_id, 1, repo="https://github.com/%s.git" % repo, ref=ref or "main", project_id=pid, **({"mode": mode} if mode else {}), **extras, **({"preview": True} if preview else {}))
        self._enqueue(command, dict({"brief": brief, "project_id": pid}, **({"env": env} if env else {})))
        return {"work_id": session_id, "session_id": session_id, "seq": 1}
    def message(self, caller, work_id, cmd_id, seq, text):
        self._enqueue(self._command("send", cmd_id, work_id, seq), {"text": text})
        return {"id": cmd_id, "seq": int(seq)}
    def cancel(self, caller, work_id, cmd_id, seq):
        self._enqueue(self._command("retire", cmd_id, work_id, seq))
        try:
            state = self._state()
        except FloorError:
            return {"cancelled": True, "reserved": True}   # sent, not yet seen applied: custody is not confirmed
        ack = state["acks"].get(cmd_id)
        if isinstance(ack, str) and ack.startswith("rejected"):
            raise FloorError("Floor rejected the retire (%s)" % scrub(ack, 80))
        finished = (state["sessions"].get(work_id) or {}).get("state") in FINISHED_STATES
        return {"cancelled": True, "reserved": not finished}
    def observe(self, caller, work_id):
        state = self._state()
        found = state["sessions"].get(work_id)
        found = found if isinstance(found, dict) else {}
        blob = self._blob(work_id)
        text = blob.get("text") if isinstance(blob.get("text"), str) else ""
        return {"manifest": blob.get("manifest"), "log_tail": blob.get("log_tail"), "state": found.get("state", "queued"), "busy": bool(found.get("busy")), "landed_sha": found.get("landed_sha"), "rounds": found.get("rounds", 0),
                "error": found.get("error"), "last_update": found.get("last_update"), "messages": [{"role": "assistant", "text": text}] if text else []}
    def result(self, caller, work_id):
        seen = self.observe(caller, work_id)
        return {"state": seen["state"], "landed_sha": seen["landed_sha"], "error": seen["error"], "manifest": seen["manifest"], "log_tail": seen["log_tail"]}

def _private_file(setting):
    path = (env_of().get(setting) or "").strip()
    if not path:
        return None
    try:
        found = os.lstat(path)
        if not stat.S_ISREG(found.st_mode) or found.st_uid != os.getuid() or found.st_mode & 0o077:
            raise D["Refusal"]("%s is not a regular 0600 file of the user's own; nothing was called" % setting)
        with open(path) as handle:
            return handle.read(TOKEN_MAX + 1).strip() or None
    except OSError:
        raise D["Refusal"]("%s cannot be read; nothing was called" % setting) from None

def _token():
    return _private_file("CW_FLOOR_TOKEN_FILE")

def _sas(val_key, file_key):
    val = (env_of().get(val_key) or "").strip()
    return val if val else _private_file(file_key)

def _base_problem(value):
    """Why a queue or blob URL (it may carry a path, the queue name) is refused, or None: the host part is judged as an endpoint is, and no query, fragment or credentials."""
    try:
        parts = urllib.parse.urlsplit(value)
        parts.port
    except ValueError:
        return "not a URL"
    if parts.query or parts.fragment:
        return "it must not carry a query or fragment (the SAS is a separate setting)"
    return D["endpoint_problem"]("%s://%s" % (parts.scheme, parts.netloc))

def transport():
    """The transport: the test seam, else an HTTP client of CW_FLOOR_URL (read from the private settings)."""
    if TRANSPORT is not None:
        return TRANSPORT
    D["use_settings"]()
    env = env_of()
    queue, blob = (env.get("CW_FLOOR_QUEUE_URL") or "").strip(), (env.get("CW_FLOOR_BLOB_URL") or "").strip()
    mode = (env.get("CW_FLOOR_TRANSPORT") or "").strip().lower() or ("queue" if queue and blob else "http")
    if mode not in ("queue", "http"):
        raise D["Refusal"]("CW_FLOOR_TRANSPORT is not queue or http; nothing was created")
    if mode == "queue":
        if not queue or not blob:
            raise D["Refusal"]("the queue transport needs CW_FLOOR_QUEUE_URL and CW_FLOOR_BLOB_URL in the private settings; nothing was created")
        for value in (queue, blob):
            problem = _base_problem(value)
            if problem:
                raise D["Refusal"]("a Floor queue or blob URL is refused: %s; nothing was created" % problem)
        return QueueBlobTransport(queue, _sas("CW_FLOOR_QUEUE_SAS", "CW_FLOOR_QUEUE_SAS_FILE"), blob, _sas("CW_FLOOR_BLOB_SAS", "CW_FLOOR_BLOB_SAS_FILE"))
    url = (env.get("CW_FLOOR_URL") or "").strip()
    if not url:
        raise D["Refusal"]("a repository routes to Floor but CW_FLOOR_URL is not set in the private settings; nothing was created")
    problem = D["endpoint_problem"](url)
    if problem:
        raise D["Refusal"]("CW_FLOOR_URL is refused: %s; nothing was created" % problem)
    return HttpTransport(url, _token())

def _call(function, *args, **kwargs):
    """One call of the transport; a failure is a CloudError whose text holds no URL, host or token."""
    try:
        return function(*args, **kwargs)
    except UnavailableArtifactError:
        raise
    except FloorError as err:
        raise D["CloudError"]("Floor: %s" % scrub(err)) from None
    except (OSError, ValueError, KeyError, TypeError) as err:
        raise D["CloudError"]("Floor: %s" % scrub("the call failed (%s)" % type(err).__name__)) from None

def verify_enabled(env=None):
    """True when Floor's verify workflows are on (CW_FLOOR_VERIFY_ENABLED is `1` or `true`)."""
    return ((env_of() if env is None else env).get("CW_FLOOR_VERIFY_ENABLED") or "").strip().lower() in ("1", "true", "yes")

def agents_enabled(env=None):
    """True when the Cloud Agents profiles are on (CW_FLOOR_AGENTS_ENABLED is `1`, `true` or `yes`)."""
    return ((env_of() if env is None else env).get("CW_FLOOR_AGENTS_ENABLED") or "").strip().lower() in ("1", "true", "yes")
AGENT_MODES = ("research-assistant", "orchestrator")

def verify_mode(args, brief):
    """`research-assistant` or `orchestrator` for those profiles (agents enabled), `verify-only` for a tester, `rehearse` for --no-commit or a rehearsal brief, else None (an author launch)."""
    if agents_enabled():
        for profile in AGENT_MODES:
            if getattr(args, "role", None) == profile or getattr(args, "agent", None) == profile:
                return profile
    if getattr(args, "role", None) == "tester":
        return "verify-only"
    if getattr(args, "no_commit", False) or str(brief or "").lstrip().startswith("REHEARSAL BRIEF"):
        return "rehearse"
    return None

def check_floor_admission(args, repo, brief, config):
    """Refuse (exit 2, nothing created) what Floor cannot work: a repository outside `land_repos`, and every role that is not a coder or worker author. With CW_FLOOR_VERIFY_ENABLED a tester, --no-commit or rehearsal launch is admitted as a verify mode (see `verify_mode`)."""
    if route_repo(repo, config) != "floor":
        return
    Refusal = D["Refusal"]
    if not D["H"]["uses_spire_land"](config, repo):
        raise Refusal("refused: %s lands through pull requests and gates (not in land_repos): Floor only supports spire land repositories until a "
                      "PR-and-gates workflow exists; nothing was created" % repo)
    agent, text = getattr(args, "agent", None), str(brief or "")
    profile = verify_mode(args, brief) if agents_enabled() else None
    if profile in AGENT_MODES:
        if getattr(args, "stack", False) or re.search(r"^\s*NEEDS:\s*docker\s*$", text, re.I | re.M):
            raise Refusal("refused: %s routes to Floor: --stack and NEEDS: docker are not valid for an ACA sandbox; nothing was created" % repo)
        return   # a persistent orchestrator is intended here
    if agent in NOT_AUTHORS or getattr(args, "persistent", False):
        raise Refusal("refused: %s routes to Floor: --agent orchestrator and --persistent are not Floor authors; nothing was created" % repo)
    if getattr(args, "role", None) == "reviewer" or str(agent or "").startswith("reviewer"):
        raise Refusal("refused: %s routes to Floor: reviewer pods are not launched separately (Floor runs council in the workflow); nothing was created" % repo)
    mode = verify_mode(args, brief)
    if mode and not verify_enabled():
        raise Refusal("refused: %s routes to Floor: --role tester, --no-commit and rehearsal pods have no standalone Floor author equivalent yet "
                      "(Floor runs checks on the heavy executor; CW_FLOOR_VERIFY_ENABLED turns the verify workflows on); nothing was created" % repo)
    if getattr(args, "stack", False) or re.search(r"^\s*NEEDS:\s*docker\s*$", text, re.I | re.M):
        raise Refusal("refused: %s routes to Floor: --stack and NEEDS: docker are not valid for an ACA author sandbox (heavy services run on Floor's "
                      "heavy executor pool); nothing was created" % repo)
    if mode:
        return   # no author runs: the agent name is not mapped
    if agent not in AUTHORS:
        raise Refusal("refused: %s routes to Floor: --agent %s has no Floor author mapping (only coder/worker authors route to Floor); nothing was created"
                      % (repo, str(agent)[:40]))
    if re.search(r"\bgh\s+pr\s+merge\b", text) and not re.search(r"\b(implement|write|fix|add|change|refactor|edit|commit|create)\b", text, re.I):
        raise Refusal("refused: %s routes to Floor: merger pods have no Floor equivalent (Floor delivers via spire land); nothing was created" % repo)

def translate_brief(brief, repo, branch, base=None):
    """The brief a pod was written for, as a Floor author reads it: the pod header, setup and landing line taken off, every line that runs a delivery command (push, pull request, prove, verdict, heavy, land) replaced by a plain note, and the author contract put first."""
    text = str(brief or "")
    held = D["H"]
    header = D["POD_HEADER"]
    if text.startswith(header):
        text = text[len(header):]
    values = {"repo": repo, "branch": branch}
    for setup in (D["H"]["setup_with_land"](D["SETUP_PREAMBLE"], {"land_repos": [repo]}, repo) % values, D["SETUP_PREAMBLE"] % values,
                  D["SETUP_PREAMBLE"][len(header):] % values):
        if text.startswith(setup):
            text = text[len(setup):]
            break
    land_line = held.get("LAND_SETUP_LINE")
    if land_line:
        text = text.replace(land_line, "")
    lines = [REMOVED_LINE if DELIVERY_LINE.search(line) else line for line in text.splitlines()]
    body = "\n".join(lines).strip("\n")
    return FLOOR_AUTHOR_PREAMBLE % {"branch": branch, "base": base or "the default branch"} + body + "\n"

def _hash(*parts):
    return hashlib.sha256("\0".join(str(p) for p in parts).encode()).hexdigest()[:16]

def session_key(who, name, repo):
    return "ses_" + hashlib.sha256(("%s\0%s\0%s" % (who, name, repo)).encode()).hexdigest()[:16]

def launch_key(who, session_id):
    return "c-" + hashlib.sha256(("%s\0launch\0%s" % (who, session_id)).encode()).hexdigest()[:16]

def send_key(session_id, seq, text):
    return "c-" + hashlib.sha256(("%s\0send\0%d\0%s" % (session_id, seq, text)).encode()).hexdigest()[:16]

def cancel_key(session_id, seq):
    return "c-" + _hash(session_id, "cancel", seq)

def submit_work(who, cmd_id, **fields):
    """The one submit of a launch; a failure raises before any record is written."""
    return _call(transport().submit, who, cmd_id, **fields)

def _stamp(seconds, now):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + seconds))

def _seconds(value, default):
    try:
        return int(value) if value and int(value) > 0 else default
    except (TypeError, ValueError):
        return default

def check_priority_tier(args, engine, runner, now, repo=None, config=None):
    """The class and preview of a Floor launch, checked before anything is sent."""
    pclass = getattr(args, "priority_class", None) or "production"
    if pclass not in ("outage", "production", "background"):
        raise D["Refusal"]("refused: --class must be outage, production or background; nothing was created")
    if getattr(args, "preview", False) and repo != (env_of().get("CW_FLOOR_PREVIEW_REPO") or (config or {}).get("intent_repo")):
        raise D["Refusal"]("refused: --preview is only supported on the configured preview repository (got %s); nothing was created" % repo)
    if pclass == "outage":
        kind = D["H"]["gates_caller_kind"](runner, now)
        by = kind[1] if len(kind) > 1 else "?"
        ok = kind[0] == "person" or (kind[0] == "agent" and (kind[1] == "cos" or (len(kind) > 2 and str(kind[2]).startswith("chief-of-"))))
        body = {"caller": by, "kind": kind[0], "priority_class": pclass}
        if not ok:
            D["_audit"](engine, "cloud.outage_refused", getattr(args, "name", "?"), body)
            raise D["Refusal"]("priority class outage is reserved for cos or the owner's shell (caller: %s); raise the outage to cos; nothing was created" % by)
        D["_audit"](engine, "cloud.outage_allowed", getattr(args, "name", "?"), body)
    return pclass

def launch_floor(args, runner, now, reserve, repo, branch, inner):
    """The Floor half of `launch --cloud`: everything is checked before the one submit, and the record is written only after Floor accepted it."""
    LaunchError, name = D["H"]["LaunchError"], args.name
    brief = D["H"]["read_brief"](args.brief_file, sys.stdin)
    args.brief_text = brief
    engine, state, config = D["H"]["build_engine"](runner, now, "cli")
    check_floor_admission(args, repo, brief, config)
    if not re.fullmatch(r"[a-z][a-z0-9]*-[0-9]+-[a-z0-9]+(?:-[a-z0-9]+){0,3}", str(name or "")):
        print("%s: warning: worker name %s does not follow <repo-short>-<issue>-<slug> (e.g. fk-1011-orders-in-context)" % (D["H"]["PROG"], name), file=sys.stderr)
    pclass = check_priority_tier(args, engine, runner, now, repo=repo, config=config)
    D["check_allowed"](repo)
    D["cloud_pr_labels"].cloud_base.check(args)   # a base that is not a branch name is refused before anything is sent
    reserve(state)
    existing = D["load_record"](state, name)
    if existing is not None and not is_floor(existing):
        return inner(args, runner, now, reserve)   # a name that holds a pod: the old launch-again rules answer
    who = floor_who()
    session_id = existing.get("session") if existing else None
    session_id = session_id if isinstance(session_id, str) and session_id else session_key(who, name, repo)
    base = getattr(args, "base", None) or "main"
    mode = verify_mode(args, brief)
    extra = {"mode": mode, "env": {"FLOOR_WORKFLOW": mode}} if mode else {}
    if mode in AGENT_MODES:
        extra["env"]["FLOOR_PROFILE"] = mode
        if mode == "orchestrator" and getattr(args, "charter", None):
            extra["env"]["FLOOR_CHARTER"] = str(args.charter)   # a verify launch: Floor runs it on the heavy executor, nothing is translated to author commits
    preview = bool(getattr(args, "preview", False))
    extra.update(**({"priority_class": pclass} if pclass != "production" else {}), **({"preview": True} if preview else {}))
    answer = submit_work(who, launch_key(who, session_id), session_id=session_id, repo=repo, ref=base, branch=branch, title=name,
                         brief=str(brief or "") if mode else translate_brief(brief, repo, branch, base), **extra)
    work_id = answer.get("work_id") if isinstance(answer, dict) else None
    if not isinstance(work_id, str) or not work_id:
        raise D["CloudError"]("Floor: the answer to the submit names no work")
    if existing is not None:   # a second launch of the name: the same work, nothing new
        D["cloud_launch_again"].RETURNED.add(name)
        print("exists: returned worker %s (Floor session %s)" % (name, session_id), file=sys.stderr)
        print(_json(D["cloud_launch_again"]._launch_line(existing, existing=True)))
        return D["H"]["EXIT_OK"]
    stamp = now()
    record = {"worker": name, "backend": "floor", "session": session_id, "work_id": work_id, "runtime_id": "floor/" + session_id, "url": ENDPOINT_URL,
              "seq": 1, "repo": repo, "branch": branch, "base": base, "agent": args.agent, **({"mode": mode} if mode else {}), "priority_class": pclass, **({"preview": True} if preview else {}),
              "idle_deadline": _stamp(_seconds(getattr(args, "idle", None), 3600), stamp), "hard_deadline": _stamp(_seconds(getattr(args, "hard", None), 8 * 3600), stamp)}
    if getattr(args, "reports_to", None):
        record.update(reports_to=args.reports_to)
    if isinstance(getattr(args, "shape", None), str) and args.shape:
        record["shape"] = args.shape
    issue = getattr(args, "issue", None)
    record["issue"] = None if issue is None else int(issue)
    labels = D["cloud_pr_labels"].labels_of(args)   # the labels only: the base is already in the record, and the setup text of the pods is not touched
    if labels:
        record["pr_labels"] = labels
    try:
        D["save_record"](state, record)
        if switchboard_authority() == "cluster":
            call_switchboard_v1("/v1/cloud-record", {"worker": record["worker"], "record": record})
    except (D["CloudError"], FloorError, OSError) as err:
        raise LaunchError("Floor accepted the work %s but its record could not be stored (%s); launch again to finish: the same work is returned" % (work_id, scrub(err, 80))) from None
    D["_audit"](engine, "cloud.created", name, {"runtime_id": record["runtime_id"], "backend": "floor", "idle_deadline": record["idle_deadline"], "hard_deadline": record["hard_deadline"]})
    D["_registry_note"](engine, "upsert", state, name, {"session": session_id, "repo": repo, "role": getattr(args, "role", None), "kind": "cloud", "run_id": work_id, "state": "working"}, address=ENDPOINT_URL)
    print(_json(D["cloud_launch_again"]._launch_line(record)))
    return D["H"]["EXIT_OK"]

def _json(value):
    return json.dumps(value, sort_keys=True)

def _launch_wrapper(inner):
    def _launch(args, runner, now, reserve):
        name, branch = getattr(args, "name", None), getattr(args, "branch", None)
        repo = D["H"]["launch_repo"](args)
        if (not D["valid_name"](name) or not repo or not branch or getattr(args, "resume", False) or getattr(args, "headless", False)
                or not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch or "") or str(branch).startswith("-")):
            return inner(args, runner, now, reserve)   # every usage error is the old code's, in its words
        D["use_settings"]()
        if route_repo(repo, None) != "floor":
            if (env_of().get("CW_POD_ROLLBACK_WINDOW") or "open").strip().lower() == "closed":
                raise D["Refusal"]("refused: the cloud pod backend is retired (CW_POD_ROLLBACK_WINDOW=closed); route %s to Floor (CW_REMOTE_BACKEND=floor)" % repo)
            return inner(args, runner, now, reserve)
        return launch_floor(args, runner, now, reserve, repo, branch, inner)
    return _launch

def _send_floor(args, runner, now, record):
    LaunchError, H = D["H"]["LaunchError"], D["H"]
    engine, state, config = H["build_engine"](runner, now, "cli")
    refusal = H["intent_refusal"](config, args.issue)
    if refusal:
        sys.stderr.write("%s: %s\n" % (H["PROG"], refusal))
        return H["EXIT_USAGE"]
    if args.interrupt:
        sys.stderr.write("%s: --interrupt is not supported for a Floor worker: nothing sent\n" % H["PROG"])
        return H["EXIT_USAGE"]
    text = " ".join(args.text).strip()
    if not text or len(text) > D["MESSAGE_MAX"]:
        sys.stderr.write("%s: %s\n" % (H["PROG"], "nothing to send" if not text else "the message is too long"))
        return H["EXIT_USAGE"]
    kind = H["gates_caller_kind"](runner, now)
    if kind[0] == "unknown":
        sys.stderr.write("%s: not sent: %s\n" % (H["PROG"], kind[1]))
        return H["EXIT_HELD"]
    sender = "operator" if kind[0] == "person" else kind[1]
    who, session_id = floor_who(), record["session"]
    seq = int(record.get("seq", 1)) + 1   # a send that failed before the record moved is asked again with the same seq and the same key
    receipt = _call(transport().message, who, record["work_id"], send_key(session_id, seq, text), seq, text)
    record["seq"] = seq
    D["save_record"](state, record)
    D["_audit"](engine, "cloud.send", args.name, {"from": sender, "chars": len(text), "backend": "floor", "seq": seq})
    print((receipt.get("id") or receipt.get("message_id") or "sent") if isinstance(receipt, dict) else "sent")
    sys.stderr.write("%s: delivered to %s (Floor seq %d)\n" % (H["PROG"], args.name, seq))
    return H["EXIT_OK"]

def _name_artifact(err, name):
    return D["H"]["LaunchError"]("%s: %s: the record is unchanged" % (name, scrub(err)))

def _observe(record):
    who = floor_who()
    return _call(transport().observe, who, record["work_id"])

def _line(message):
    if not isinstance(message, dict):
        return scrub(message, 400)
    return scrub("%s: %s" % (str(message.get("role") or message.get("type") or "?")[:20], message.get("text") or ""), 400)

def _read_floor(args, record):
    limit = max(1, min(int(args.limit), 200))
    who = floor_who()
    try:
        seen = _call(transport().observe, who, record["work_id"])
        done = _call(transport().result, who, record["work_id"])
    except UnavailableArtifactError as err:
        raise _name_artifact(err, args.name) from None
    for message in [m for m in (seen.get("messages") or []) if isinstance(m, dict) or isinstance(m, str)][-limit:]:
        print(_line(message))
    print(scrub("floor: state %s, landed_sha %s" % (done.get("state", seen.get("state")), done.get("landed_sha", seen.get("landed_sha")))))
    manifest = done.get("manifest", seen.get("manifest"))
    if manifest is not None:
        print(scrub("manifest: " + json.dumps(manifest, sort_keys=True), 2000))
    tail = done.get("log_tail", seen.get("log_tail"))
    tail = tail.splitlines() if isinstance(tail, str) else tail if isinstance(tail, list) else []
    for line in tail[-40:]:
        print(scrub("log_tail: " + str(line), 400))
    return D["H"]["EXIT_OK"]

def _status_floor(records):
    lines, rows = [], []
    for record in records:
        row = {"worker": record["worker"], "backend": "floor", "runtime_id": record.get("runtime_id"), "session": record.get("session"),
               "idle_deadline": record.get("idle_deadline"), "hard_deadline": record.get("hard_deadline")}
        try:
            seen = _observe(record)
            row.update({k: seen.get(k) for k in ("state", "busy", "landed_sha", "rounds")})
            lines.append("      cloud %s: Floor session %s, state %s, busy %s, landed_sha %s, rounds %s" % (
                record["worker"], record.get("session"), *(scrub(seen.get(k, "?"), 40) for k in ("state", "busy", "landed_sha", "rounds"))))
        except UnavailableArtifactError as err:
            row["unavailable"] = err.artifact
            lines.append("      cloud %s: Floor session %s, artifact %s is unavailable (the record is unchanged)" % (record["worker"], record.get("session"), err.artifact))
        except (D["CloudError"], D["Refusal"]) as err:
            row["problem"] = scrub(err, 100)
            lines.append("      cloud %s: Floor session %s not read (%s)" % (record["worker"], record.get("session"), scrub(err, 100)))
        rows.append(row)
    return lines, rows

def _retire_floor(args, runner, now, record):
    H = D["H"]
    LaunchError = H["LaunchError"]
    engine, state, _ = H["build_engine"](runner, now, "cli")
    kind = H["gates_caller_kind"](runner, now)
    if not D["_may_retire"](kind, record):
        D["_audit"](engine, "cloud.retire_refused", args.name, {"why": scrub(kind[1], 120)})
        raise LaunchError("refused: only the operator, the Chief of Staff or %s's own boss may retire it" % args.name)
    if args.undo:
        raise LaunchError("a Floor worker's session is cancelled once retired: there is nothing to undo")
    reason = " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", args.reason or "").split())[:200] or "finished"
    seq = int(record.get("cancel_seq") or int(record.get("seq", 1)) + 1)   # a retire that waited is asked again with the same seq and key
    answer = _call(transport().cancel, floor_who(), record["work_id"], cancel_key(record["session"], seq), seq)
    if isinstance(answer, dict) and answer.get("reserved"):
        record["cancel_seq"], record["seq"] = seq, max(seq, int(record.get("seq", 1)))
        D["save_record"](state, record)
        D["_audit"](engine, "cloud.cancel_recorded", args.name, {"seq": seq, "backend": "floor"})
        print("%s: cancellation is recorded and waiting for heavy-job custody confirmation: record kept; `%s retire %s` again" % (args.name, H["PROG"], args.name))
        return H["EXIT_ERROR"]
    D["_audit"](engine, "cloud.stopped", args.name, {"exit": 0, "reason": reason, "runtime_id": record.get("runtime_id"), "backend": "floor"})
    D["_final_message"](engine, state, record, args.name, reason, None)
    D["_registry_note"](engine, "retire", state, args.name, reason)
    D["_keep_boss"](state, record)
    D["remove_record"](state, args.name)
    if switchboard_authority() == "cluster":
        try:
            call_switchboard_v1("/v1/cloud-record", {"worker": args.name, "delete": True, "reason": reason})
        except FloorError as err:
            _warn("cluster copy of the %s record was not deleted" % args.name, err)
    print("%s is retired (Floor session %s cancelled)" % (args.name, record.get("session")))
    return H["EXIT_OK"]

def rollback_to_pane(state, name, pane_id, cancel_fn=None):
    """Rollback of a cloud orchestrator to a local pane: cancel its Floor session, remove the record once retired, name the pane."""
    record = D["load_record"](state, name)
    if record is not None and is_floor(record):
        cancel = cancel_fn or (lambda rec: _call(transport().cancel, floor_who(), rec["work_id"], cancel_key(rec["session"], int(rec.get("seq", 1)) + 1),
                                                 int(rec.get("seq", 1)) + 1))
        answer = cancel(record)
        if isinstance(answer, dict) and answer.get("reserved"):
            return {"worker": name, "pane": pane_id, "rolled_back": False}
        D["remove_record"](state, name)
    return {"worker": name, "pane": pane_id, "rolled_back": True}
SESSION_RE = re.compile(r"ses_[0-9a-f]{16}")
CHARTER_RE = re.compile(r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[0-9]+")
REPORT_STATES = ("done", "failed", "blocked", "question", "working")

def verify_switchboard_auth(header_value, expected_secret):
    """The sender identity from the egress proxy's X-Switchboard-Auth header; anything else raises FloorError 401."""
    bad = FloorError("unauthenticated switchboard request", 401)
    if not isinstance(header_value, str) or not header_value or not expected_secret or len(header_value) > 512:
        raise bad
    fields = {}
    for part in header_value.split(";"):
        key, sep, value = part.partition("=")
        if not sep or key in fields:
            raise bad
        fields[key.strip()] = value
    agent, session, charter, secret = fields.get("agent"), fields.get("session"), fields.get("charter"), fields.get("secret")
    if set(fields) - {"agent", "session", "charter", "secret"} or secret is None:
        raise bad
    if not (agent and CALLER_RE.fullmatch(agent) and session and SESSION_RE.fullmatch(session)):
        raise bad
    if charter is not None and not CHARTER_RE.fullmatch(charter):
        raise bad
    if not hmac.compare_digest(secret.encode("utf-8"), str(expected_secret).encode("utf-8")):
        raise bad
    return {"agent": agent, "session": session, "charter": charter}

def ensure_switchboard_auth_file(env=None, fetch_secret=None):
    """The relay credential file: the configured 0600 file when present, else fetched from Key Vault and written atomically (0600). Returns its path, or None."""
    env = os.environ if env is None else env
    auth_file = (env.get("CW_SWITCHBOARD_AUTH_FILE") or "").strip()
    if auth_file and os.path.isfile(auth_file) and stat.S_IMODE(os.stat(auth_file).st_mode) == 0o600:
        return auth_file
    vault = (env.get("CW_SWITCHBOARD_KEYVAULT") or "").strip()
    secret_name = (env.get("CW_SWITCHBOARD_SECRET_NAME") or "switchboard-relay-auth").strip()
    if not vault:
        return None
    if fetch_secret is None:
        def fetch_secret(vault_name, name):
            import subprocess
            try:
                done = subprocess.run(["az", "keyvault", "secret", "show", "--vault-name", vault_name, "--name", name, "--query", "value", "-o", "tsv"],
                                      capture_output=True, text=True, timeout=15)
            except subprocess.TimeoutExpired:
                raise FloorError(504, "reading secret %s from vault %s timed out after 15s: retry" % (name, vault_name))
            if done.returncode != 0:
                raise FloorError(502, "could not read secret %s from vault %s (az exit %d): check az login and access" % (name, vault_name, done.returncode))
            return done.stdout.strip()
    value = fetch_secret(vault, secret_name)
    if not isinstance(value, str) or not value.strip():
        return None
    path = auth_file or os.path.join(os.path.expanduser("~"), ".config", "fleet-switchboard", "switchboard-relay.auth")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=".relay-auth-", dir=os.path.dirname(path) or ".")
    with os.fdopen(fd, "w") as handle:
        handle.write(value.strip() + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path

def handle_switchboard_v1(method, path, headers, body_bytes, expected_secret, state=None, artifact_put=None):
    """One request to the in-cluster switchboard: returns (status, body). The sender is the proxy-injected header's agent, never a body field."""
    try:
        auth = verify_switchboard_auth((headers or {}).get("X-Switchboard-Auth"), expected_secret)
    except FloorError as err:
        return err.status, {"error": str(err)}
    state = state if state is not None else {}
    for key in ("pane_outbox", "reports", "reminders", "audit"):
        state.setdefault(key, [])
    route = (method.upper(), path.split("?", 1)[0])
    if route == ("GET", "/v1/mode"):
        if "mode" not in state and state.get("state_dir"):
            state["mode"] = cluster_switchboard_mode(state["state_dir"])
        return 200, {"mode": state.get("mode") or "shadow"}
    if route == ("GET", "/v1/pane-outbox"):
        return 200, {"messages": [m for m in state["pane_outbox"] if m.get("to") == auth["agent"] or auth["agent"] in ("cos", "pane-relay")]}
    body = {}
    if route[0] == "POST":
        try:
            body = D["parse_bounded"](body_bytes.decode("utf-8")) if body_bytes else {}
        except ValueError:
            return 400, {"error": "bad request body"}
        if not isinstance(body, dict):
            return 400, {"error": "bad request body"}
    if route[0] == "POST" and route[1] in ("/v1/send", "/v1/report", "/v1/remind", "/v1/retire", "/v1/orders", "/v1/intent", "/v1/decisions", "/v1/pane-state", "/v1/pane-command") \
            and callable(state.get("on_v1_action")):
        state["on_v1_action"](route[1], auth, body, state)
    for spoof in ("sender", "from"):
        if spoof in body and body[spoof] != auth["agent"]:
            state["audit"].append({"event": "spoofed_sender", "agent": auth["agent"], "session": auth["session"], "claimed": scrub(body[spoof], 40)})
    text = scrub(body.get("text", ""), 4000) if isinstance(body.get("text", ""), str) else ""
    if route in (("POST", "/v1/intent"), ("GET", "/v1/intent")):
        if route[0] == "GET" or body.get("action") in ("list", "show"):
            return 200, {"intents": state.get("intents") or []}
        state.setdefault("intents", []).append({"action": body.get("action") or "set", "issue": body.get("issue"), "text": text, "from": auth["agent"]})
        return 200, {"recorded": True, "intents": state["intents"]}
    if route in (("POST", "/v1/decisions"), ("GET", "/v1/decisions")):
        if route[0] == "GET" or body.get("action") in ("list", "show"):
            out = {"decisions": state.get("decisions") or []}
            if isinstance(state.get("decisions_file"), dict):
                out["decisions_file"] = state["decisions_file"]
            return 200, out
        title = body.get("title", "")
        state.setdefault("decisions", []).append({"action": body.get("action") or "batch", "id": body.get("id"), "title": scrub(title, 200) if isinstance(title, str) else "",
                                                  "rows": body.get("rows") if isinstance(body.get("rows"), list) else [], "from": auth["agent"]})
        return 200, {"recorded": True, "decisions": state["decisions"]}
    if route == ("POST", "/v1/mode"):
        if body.get("mode") not in ("shadow", "active"):
            return 400, {"error": "mode must be shadow or active"}
        state["mode"] = body["mode"]
        if state.get("state_dir"):
            write_cluster_switchboard_mode(state["state_dir"], body["mode"])
        return 200, {"mode": state["mode"]}
    if route == ("POST", "/v1/send"):
        to = body.get("to")
        if not isinstance(to, str) or not CALLER_RE.fullmatch(to) or not text:
            return 400, {"error": "to and text are required"}
        message = {"from": auth["agent"], "session": auth["session"], "to": to, "text": text, "issue": body.get("issue"), "repo": body.get("repo")}
        if isinstance(body.get("oversize_files"), dict):
            message["oversize_files"] = {k: str(v) for k, v in body["oversize_files"].items() if isinstance(k, str) and re.fullmatch(r"send-[A-Za-z0-9_.-]+\.txt", k)}
        if is_canary(dict(message, canary=body.get("canary"))):
            state["pane_outbox"].append(dict(message, id=len(state["pane_outbox"]) + 1 + len(state.get("acked", []))))
            return 200, {"queued": "pane"}
        target = (state.get("floor_sessions") or {}).get(to)
        if target:
            seq = int(target.get("seq", 1)) + 1
            _call(transport().message, floor_who(), target["work_id"], send_key(target.get("session", to), seq, text), seq, text)
            target["seq"] = seq
            return 200, {"queued": "floor"}
        state["pane_outbox"].append(dict(message, id=len(state["pane_outbox"]) + 1 + len(state.get("acked", []))))
        return 200, {"queued": "pane"}
    if route == ("POST", "/v1/report"):
        status = body.get("state") or body.get("status")
        if status not in REPORT_STATES:
            return 400, {"error": "status must be one of " + ", ".join(REPORT_STATES)}
        state["reports"].append({"from": auth["agent"], "session": auth["session"], "status": status, "text": text, "repo": body.get("repo")})
        return 200, {"recorded": True}
    if route == ("POST", "/v1/remind"):
        when = body.get("when")
        action = body.get("action")
        manage = action in ("list", "cancel", "cancel_key")
        if not manage and (not isinstance(when, (str, int, float)) or not text):
            return 400, {"error": "when and text are required"}
        to = body.get("to") or body.get("name") or auth["agent"]
        if not isinstance(to, str) or not CALLER_RE.fullmatch(to):
            return 400, {"error": "bad recipient"}
        if action == "list":
            return 200, {"recorded": False, "reminders": [r for r in state["reminders"] if auth["agent"] in (r.get("to"), r.get("from"))]}
        if manage:
            target, field = (body.get("cancel"), "id") if action == "cancel" else (body.get("cancel_key"), "key")
            keep = [r for r in state["reminders"] if not (target is not None and r.get(field) == target and r.get("from") == auth["agent"])]
            removed = len(state["reminders"]) - len(keep)
            state["reminders"][:] = keep
            return 200, {"recorded": True, "cancelled": removed}
        keys = ("issue", "repo", "key", "ttl", "every", "until_closed", "until_merged", "keep_if_closed", "action", "cancel", "cancel_key")
        entry = {"to": to, "from": auth["agent"], "when": when, "text": text}
        entry.update({k: body.get(k) for k in keys})
        state["reminders"].append(entry)
        return 200, {"recorded": True, **(state.pop("last_remind", None) or {})}
    if route == ("POST", "/v1/artifact"):
        name = body.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name) or artifact_put is None:
            return 400, {"error": "a plain name is required"}
        try:
            data = base64.b64decode(body.get("data_b64") or "", validate=True)
        except ValueError:
            return 400, {"error": "data_b64 is not base64"}
        digest = hashlib.sha256(data).hexdigest()
        url = artifact_put(auth["session"], "%s-%s" % (digest, name), data)
        if not isinstance(url, str) or not url.startswith("https://"):
            return 502, {"error": "artifact store refused"}
        return 200, {"url": url, "sha256": digest, "sp": "r"}
    if route == ("POST", "/v1/retire"):
        name, reason = body.get("worker") or body.get("name"), scrub(body.get("reason", ""), 200) if isinstance(body.get("reason", ""), str) else ""
        if not isinstance(name, str) or not CALLER_RE.fullmatch(name):
            return 400, {"error": "worker is required"}
        target = (state.get("floor_sessions") or {}).get(name)
        if target is not None and not D["_may_retire"](("agent", auth["agent"], "chief-of-staff" if auth["agent"] == "cos" else None), target):   # the one caller rule of a removal
            return 403, {"error": "only the Chief of Staff or the worker's own boss may retire it"}
        state.setdefault("retires", []).append({"worker": name, "reason": reason, "undo": bool(body.get("undo")), "from": auth["agent"]})
        if target:
            seq = int(target.get("seq", 1)) + 1
            _call(transport().cancel, floor_who(), target["work_id"], cancel_key(target.get("session", name), seq), seq)
            target["seq"] = seq
        return 200, {"retired": True, "worker": name}
    if route == ("POST", "/v1/orders"):
        action = body.get("action")
        if action not in ("list", "add", "remove", "copy", "cos", None):
            return 400, {"error": "action must be list, add, remove or copy"}
        orders = state.setdefault("orders", [])
        if action != "list":
            orders.append({"action": action or "cos", "charter": body.get("charter"), "source": body.get("source"), "text": text, "number": body.get("number"), "from": auth["agent"]})
        return 200, {"recorded": True, "orders": orders}
    if route == ("POST", "/v1/pane-state"):
        if auth["agent"] not in ("pane-relay", "cos"):
            return 403, {"error": "pane-state is for the pane relay"}
        agents = body.get("agents")
        if not isinstance(agents, dict):
            return 400, {"error": "agents is required"}
        wanted = ("status", "errored", "turn_error", "turn_error_id", "idle_at", "permission_waiting", "form_waiting", "pane", "session_id", "fleet")
        state["pane_state"] = {k: {f: v[f] for f in wanted if f in v} for k, v in agents.items() if isinstance(v, dict)}
        state["pane_state_at"] = body.get("at")
        commands = list(state.setdefault("pane_commands", []))
        state["pane_commands"].clear()
        return 200, {"updated": len(state["pane_state"]), "commands": commands}
    if route == ("POST", "/v1/pane-command"):
        agent, action = body.get("agent"), body.get("action")
        if not isinstance(agent, str) or not CALLER_RE.fullmatch(agent) or action not in ("resume", "interrupt", "retire"):
            return 400, {"error": "agent and a known action are required"}
        command = {"action": action, "agent": agent, "reason": scrub(body.get("reason", ""), 200) if isinstance(body.get("reason", ""), str) else "", "from": auth["agent"]}
        state.setdefault("pane_commands", []).append(command)
        state["pane_outbox"].append({"from": auth["agent"], "session": auth["session"], "to": "pane-relay", "command": command, "text": "%s %s" % (action, agent),
                                     "id": len(state["pane_outbox"]) + 1 + len(state.get("acked", []))})
        return 200, {"queued": True}
    if route == ("POST", "/v1/pane-ack"):
        ids = body.get("ids")
        if not isinstance(ids, list):
            return 400, {"error": "ids is required"}
        mine = lambda m: m.get("id") in ids and (m.get("to") == auth["agent"] or auth["agent"] == "pane-relay")   # an agent acks only its own messages
        keep = [m for m in state["pane_outbox"] if not mine(m)]
        state.setdefault("acked", []).extend(m for m in state["pane_outbox"] if mine(m))
        state["pane_outbox"][:] = keep
        return 200, {"acked": len(state["acked"])}
    return 404, {"error": "no such route"}

OVERSIZE_RE = re.compile(r"(\[oversize send \(\d+ chars\) stored at )([^\]]+?/(oversize-sends/(send-[A-Za-z0-9_.-]+\.txt)))(\])")
SYNC_STATE_FILES = ("registry.json", "retired.json", "boss-overrides.json", "escalated-requests.json", "pr-notices.json", "order-notices.json")
_PASSTHROUGH_AUTH = []
PASSTHROUGH_COMMANDS = ("orders", "decisions", "intent", "retire")

def _write_private(path, content):
    tmp = "%s.tmp%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(tmp, path)

def switchboard_relay_config(env=None):
    """url, auth_file and authority from the environment, else from the state file switchboard-relay.json (never raises)."""
    if env is None and _PASSTHROUGH_AUTH:
        prev = _PASSTHROUGH_AUTH.pop()
        if prev is not None:
            os.environ["CW_SWITCHBOARD_AUTHORITY"] = prev
    env = os.environ if env is None else env
    got = {"url": (env.get("CW_SWITCHBOARD_URL") or "").strip(), "auth_file": (env.get("CW_SWITCHBOARD_AUTH_FILE") or "").strip(),
           "authority": (env.get("CW_SWITCHBOARD_AUTHORITY") or "").strip()}
    if all(got.values()):
        return got
    try:
        path = os.path.join(str(D["H"]["state_dir_path"]()), "switchboard-relay.json")
    except Exception:
        return got   # no state directory bound: no file to read
    try:
        with open(path, encoding="utf-8") as handle:
            data = D["parse_bounded"](handle.read(65536))
    except FileNotFoundError:
        return got
    except Exception as err:   # a present but unreadable or corrupt file must not silently turn into local writes
        raise FloorError("switchboard-relay.json cannot be read (%s); fix or remove it" % type(err).__name__) from None
    if not isinstance(data, dict):
        return got
    for key, names in (("url", ("url", "switchboard_url")), ("auth_file", ("auth_file", "switchboard_auth_file")), ("authority", ("authority", "switchboard_authority"))):
        for name in names:
            if not got[key] and isinstance(data.get(name), str):
                got[key] = data[name].strip()
    return got

def switchboard_authority(env=None):
    if env is None and _PASSTHROUGH_AUTH:
        prev = _PASSTHROUGH_AUTH.pop()
        if prev is not None:
            os.environ["CW_SWITCHBOARD_AUTHORITY"] = prev
        return "local"
    return switchboard_relay_config(env)["authority"]

def call_switchboard_v1(path, payload, env=None, sleep_fn=time.sleep):
    """POST `payload` to the in-cluster switchboard (CW_SWITCHBOARD_URL, header from the 0600 file CW_SWITCHBOARD_AUTH_FILE); None when no URL is set."""
    config = switchboard_relay_config(env)
    base = config["url"]
    if not base: return None
    caller_agent = None
    if isinstance(payload, dict):
        payload = dict(payload); caller_agent = payload.pop("_caller_agent", None)
    header = _auth_header(config)
    if isinstance(caller_agent, str) and CALLER_RE.fullmatch(caller_agent): header = re.sub(r"(^|;)agent=[^;]+", lambda m: m.group(1) + "agent=" + caller_agent, header)
    data = json.dumps(payload, sort_keys=True).encode()
    headers = [("Accept", "application/json"), ("Content-Type", "application/json"), ("X-Switchboard-Auth", header)]
    for delay in (1.5, 3.0, None):
        try:
            return _parse(_request("POST", base.rstrip("/") + path, data, headers)[1])
        except FloorError as err:
            if delay is None or (err.status or 0) in range(1, 500): raise
            _warn("switchboard relay (retrying in %ss)" % delay, err); sleep_fn(delay)
PANE_RELAY_PORT = 18080   # the local end of the port-forward to the in-cluster switchboard

def _warn(what, err):
    print("pane-relay: %s failed: %s" % (what, scrub(str(err))), file=sys.stderr)

def _auth_header(config):
    auth_file = config["auth_file"]
    if not auth_file:
        raise FloorError("CW_SWITCHBOARD_AUTH_FILE is not set")
    try:
        info = os.stat(auth_file)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise FloorError("CW_SWITCHBOARD_AUTH_FILE is not a regular 0600 file of the user's own")
        with open(auth_file, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        raise FloorError("CW_SWITCHBOARD_AUTH_FILE cannot be read") from None

def drain_pane_outbox(engine, sb_mod=None, discovery=None, env=None):
    """Fetch the cluster's pane outbox, deliver what the Mac owns (cluster authority only), and ack."""
    config = switchboard_relay_config(env)
    if not config["url"]:
        return {"delivered": 0, "acked": 0}
    header = _auth_header(config)
    ans = _parse(_request("GET", config["url"].rstrip("/") + "/v1/pane-outbox", None, [("Accept", "application/json"), ("X-Switchboard-Auth", header)])[1])
    messages = (ans.get("messages") if isinstance(ans, dict) else None) or []
    if not messages:
        return {"delivered": 0, "acked": 0}
    authority = config.get("authority") or "shadow"
    ack_ids, delivered, canaries = [], 0, []
    for m in messages:
        mid = m.get("id")
        if mid is None:
            continue
        if is_canary(m):
            canaries.append(m.get("text"))
            ack_ids.append(mid)
            continue
        if authority != "cluster":
            if m.get("from") != "switchboard":
                ack_ids.append(mid)
            continue
        if m.get("from") == "switchboard":
            recipient = m.get("to")
            session_id = m.get("session")
            text = m.get("text") or ""
            keys = m.get("keys") or []
            message_id = m.get("message_id") or ("msg_cluster_%s" % mid)
            delivery = m.get("delivery") or "queue"
            resume = bool(m.get("resume", True))
            description = m.get("description") or ("cluster switchboard -> %s" % recipient)
            if isinstance(m.get("oversize_files"), dict) and m["oversize_files"]:
                sdir = getattr(engine, "state_dir", None)
                if not isinstance(sdir, (str, os.PathLike)):
                    try:
                        sdir = D["H"]["state_dir_path"]()
                    except Exception:
                        sdir = None
                if sdir:
                    try:
                        ov_dir = os.path.join(str(sdir), "oversize-sends")
                        os.makedirs(ov_dir, exist_ok=True)
                        for fname, content in m["oversize_files"].items():
                            if isinstance(fname, str) and re.fullmatch(r"send-[A-Za-z0-9_.-]+\.txt", fname) and isinstance(content, str):
                                _write_private(os.path.join(ov_dir, fname), content)
                        text = OVERSIZE_RE.sub(lambda match: match.group(1) + os.path.join(str(sdir), match.group(3)) + match.group(5), text)
                    except OSError as err:
                        _warn("oversize files of outbox message %s" % mid, err)
            if discovery is not None and recipient in getattr(discovery, "agents", {}):
                session_id = discovery.agents[recipient].session_id
            try:
                engine.v2.synthetic(session_id, text, {"fleet": {"keys": keys, "from": "switchboard"}}, resume, delivery, message_id, description)
            except Exception as err:
                if "409" not in str(err):
                    _warn("delivery of outbox message %s" % mid, err)
                    continue
            else:
                try:
                    if getattr(engine, "audit", None) is not None:
                        engine.audit.append("daemon", "v2.synthetic", recipient, {"keys": list(keys), "delivery": delivery, "resume": bool(resume)})
                except Exception as err:
                    _warn("audit of outbox message %s" % mid, err)
            ack_ids.append(mid)
            delivered += 1
        elif isinstance(m.get("command"), dict):
            command = m["command"]
            if command.get("action") == "resume" and sb_mod is not None and discovery is not None:
                sb_mod.resume_ended_turn(engine, "switchboard", command["agent"], command.get("reason") or "cluster resume", discovery, window=0)
            ack_ids.append(mid)
        else:
            ack_ids.append(mid)
    if ack_ids:
        call_switchboard_v1("/v1/pane-ack", {"ids": ack_ids}, env=env)
    return {"delivered": delivered, "acked": len(ack_ids), **({"canaries": canaries} if canaries else {})}

def pane_relay_pass(engine, sb_mod, env=None):
    """One Mac pane-relay pass: discover, collect worker facts, push pane state, drain the outbox."""
    discovery = engine.discover(engine.now())
    facts = getattr(sb_mod, "WorkerFacts", None)
    for source in getattr(engine, "sources", ()):
        if facts is not None and isinstance(source, facts):
            try:
                source()   # WorkerFacts is callable and reads engine.discovery, the one discover() just took
            except Exception as err:
                _warn("worker fact collection", err)
    try:
        sb_mod.cloud_push_pane_state(engine, discovery)
    except Exception as err:
        _warn("pane state push", err)
    return drain_pane_outbox(engine, sb_mod=sb_mod, discovery=discovery, env=env)

def is_canary(m):
    return m.get("to") == "pane-relay" and (m.get("canary") is True or str(m.get("text") or "").startswith("canary:"))

def cluster_switchboard_mode(state_dir, env=None):
    env_mode = ((os.environ if env is None else env).get("SWITCHBOARD_MODE") or "").strip()
    file_mode, path = None, os.path.join(str(state_dir), "switchboard-mode.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                file_mode = D["parse_bounded"](handle.read()).get("mode")
            file_mode = file_mode if file_mode in ("active", "shadow") else None
        except (OSError, ValueError, AttributeError) as err:
            _warn("reading switchboard-mode.json (falling back to the environment)", err)
    if file_mode and env_mode in ("active", "shadow") and env_mode != file_mode:
        raise FloorError("refused: switchboard mode mismatch (SWITCHBOARD_MODE=%s != switchboard-mode.json=%s)" % (env_mode, file_mode), 500)
    return file_mode or (env_mode if env_mode in ("active", "shadow") else "shadow")

def write_cluster_switchboard_mode(state_dir, mode):
    if mode not in ("shadow", "active"):
        raise FloorError("mode must be shadow or active")
    path = os.path.join(str(state_dir), "switchboard-mode.json")
    os.makedirs(str(state_dir), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(state_dir), prefix=".switchboard-mode.")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"mode": mode}))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return {"mode": mode}

def notify_macos_alarm(title, text, runner=subprocess.run):
    if sys.platform != "darwin" and runner is subprocess.run:
        return None
    script = 'display notification %s with title %s sound name "Sosumi"' % (json.dumps(str(text)[:240]), json.dumps(str(title)[:80]))
    try:
        return runner(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=10)
    except Exception as err:
        _warn("macOS notification", err)

def mac_daemon_paused(state_dir, runner=subprocess.run):
    pid = None
    for name in ("daemon.json", "run.lock"):
        try:
            with open(os.path.join(str(state_dir), name), encoding="utf-8") as handle:
                pid = D["parse_bounded"](handle.read()).get("pid")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(pid, int) and pid > 0:
            break
        pid = None
    if pid is None:
        return True
    done = runner(["ps", "-o", "state=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    return done.returncode != 0 or (done.stdout or "").strip().startswith("T")

def check_single_authority(cluster_mode, mac_authority, mac_paused):
    """None when exactly one side holds authority; else a description of the mismatch."""
    if cluster_mode == "active" and mac_authority == "cluster" and mac_paused is True:
        return None
    if cluster_mode == "shadow" and mac_authority != "cluster" and mac_paused is False:
        return None
    if cluster_mode == "shadow" and mac_paused:
        return "no active switchboard authority: cluster is shadow and Mac daemon is paused"
    if cluster_mode == "active" and not mac_paused:
        return "split-brain authority: cluster is active and Mac daemon is running"
    return "authority mismatch: cluster=%s, mac_authority=%s, mac_paused=%s" % (cluster_mode, mac_authority, mac_paused)

class PaneRelayMonitor:
    """Relay alarm state: reachability, single authority, end-to-end canary."""
    def __init__(self, notify=notify_macos_alarm, down_after=120, canary_every=600, canary_wait=60):
        self.notify, self.down_after, self.canary_every, self.canary_wait = notify, down_after, canary_every, canary_wait
        self.unreachable_since = None
        self.reachability_alerted = self.authority_alerted = self.canary_alerted = False
        self.canary_token = self.canary_sent_at = self.last_canary = None; self.failed_canaries = 0
    def tick(self, now, reachable, cluster_mode=None, mac_authority=None, mac_paused=None, canaries=()):
        """Returns {"restart": bool, "send_canary": token or None}."""
        out = {"restart": False, "send_canary": None, "rollback": False}
        if not reachable:
            self.unreachable_since = self.unreachable_since if self.unreachable_since is not None else now
            if now - self.unreachable_since >= self.down_after and not self.reachability_alerted:
                self.notify("fleet-switchboard relay down", "Cannot reach in-cluster switchboard for 2 minutes")
                self.reachability_alerted = out["restart"] = True
            return out
        self.unreachable_since = None
        if self.reachability_alerted:
            self.notify("fleet-switchboard relay recovered", "In-cluster switchboard tunnel recovered")
            self.reachability_alerted = False
        if cluster_mode is not None:
            problem = check_single_authority(cluster_mode, mac_authority, mac_paused)
            if problem is None:
                self.authority_alerted = False
            elif not self.authority_alerted:
                self.notify("fleet-switchboard authority mismatch", problem)
                self.authority_alerted = True
        if self.canary_token and self.canary_token in [str(c) for c in canaries]:
            self.canary_token, self.canary_alerted, self.failed_canaries = None, False, 0
        if self.canary_token and now - self.canary_sent_at >= self.canary_wait:
            self.failed_canaries += 1
            if self.failed_canaries >= 2 and mac_authority == "cluster":
                out["rollback"], self.failed_canaries = True, 0
                self.notify("fleet-switchboard rolled back", "2 consecutive canaries failed; reverted switchboard authority to Mac")
            elif not self.canary_alerted:
                self.notify("fleet-switchboard canary failed", "End-to-end canary wake was not delivered and acked")
            self.canary_alerted, self.canary_token = True, None
        if mac_authority in ("cluster", "shadow") and self.canary_token is None and (self.last_canary is None or now - self.last_canary >= self.canary_every):
            self.canary_token, self.canary_sent_at, self.last_canary = "canary:%d" % now, now, now
            out["send_canary"] = self.canary_token
        return out

def fetch_cluster_mode(env=None):
    config = switchboard_relay_config(env)
    base, hdr = config["url"].rstrip("/"), [("Accept", "application/json"), ("X-Switchboard-Auth", _auth_header(config))]
    try:
        ans = _parse(_request("GET", base + "/v1/mode", None, hdr)[1])
    except FloorError as err:
        if err.status != 404:
            raise
        ans = _parse(_request("GET", base + "/healthz")[1])  # older pod
    return ans.get("mode") if isinstance(ans, dict) else None

def rollback_switchboard_authority(state_dir, env=None, kill_fn=os.kill):
    """Rehearsed rollback from cluster authority to the Mac daemon."""
    try:
        call_switchboard_v1("/v1/mode", {"mode": "shadow"}, env=env)
    except Exception as err:
        _warn("cluster mode switch to shadow (the cluster may still hold authority)", err)
    relay_path = os.path.join(str(state_dir), "switchboard-relay.json")
    if os.path.exists(relay_path):
        with open(relay_path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            data["authority"] = "shadow"
            fd, tmp = tempfile.mkstemp(dir=str(state_dir), prefix=".relay-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as out:
                    json.dump(data, out, sort_keys=True)
                os.chmod(tmp, 0o600)
                os.replace(tmp, relay_path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
    pid = None
    for name in ("daemon.json", "run.lock"):
        try:
            with open(os.path.join(str(state_dir), name), encoding="utf-8") as handle:
                got = json.load(handle)
            pid = got.get("pid") if isinstance(got, dict) else None
        except (OSError, ValueError):
            continue
        if pid is not None:
            break
    if isinstance(pid, int) and pid > 1:
        try:
            kill_fn(pid, signal.SIGCONT)
        except (ProcessLookupError, OSError):
            pass
    return {"authority": "shadow", "resumed_pid": pid}

def fleetkit_azure_dir(home_dir):
    """The fleet-kit Azure config directory under a home, or None when it does not exist."""
    path = os.path.join(str(home_dir), ".azure-fleetkit")
    return path if os.path.isdir(path) else None

def render_pane_relay_plist(python_bin, kit_root, home_dir, state_dir, path_env):
    """The LaunchAgent plist that keeps the pane-relay loop running."""
    e = lambda v: html.escape(str(v), quote=False)
    args = [python_bin, "-B", os.path.join(str(kit_root), "bin", "cloud_floor.py"), "pane-relay", "--kit-root", str(kit_root), "--state-dir", str(state_dir)]
    env = (("HOME", home_dir), ("PATH", path_env), ("CW_SWITCHBOARD_URL", "http://127.0.0.1:%d" % PANE_RELAY_PORT))
    azure_dir = fleetkit_azure_dir(home_dir)
    if azure_dir:
        env += (("AZURE_CONFIG_DIR", azure_dir),)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        "  <key>Label</key>\n  <string>com.fleet-kit.pane-relay</string>\n"
        "  <key>ProgramArguments</key>\n  <array>\n" + "".join("    <string>%s</string>\n" % e(a) for a in args) + "  </array>\n"
        "  <key>EnvironmentVariables</key>\n  <dict>\n" + "".join("    <key>%s</key>\n    <string>%s</string>\n" % (k, e(v)) for k, v in env) + "  </dict>\n"
        "  <key>RunAtLoad</key>\n  <true/>\n"
        "  <key>KeepAlive</key>\n  <true/>\n"
        "  <key>ThrottleInterval</key>\n  <integer>10</integer>\n"
        "  <key>StandardOutPath</key>\n  <string>" + e(os.path.join(str(state_dir), "pane-relay-stdout.log")) + "</string>\n"
        "  <key>StandardErrorPath</key>\n  <string>" + e(os.path.join(str(state_dir), "pane-relay-stderr.log")) + "</string>\n"
        "</dict>\n</plist>\n"
    )
def sync_cloud_records_to_cluster(state_dir, env=None):
    """Push the Mac's cloud records (and, while not cluster authority, reminders) to the cluster."""
    def read(path):
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as err:
            raise FloorError("%s cannot be read (%s); the sync is skipped so the cluster keeps its copy" % (os.path.basename(path), type(err).__name__)) from None
    if switchboard_authority(env) == "cluster":
        ans = call_switchboard_v1("/v1/cloud-sync", {"mirror_only": True}, env=env)
        if isinstance(ans, dict) and isinstance(ans.get("decisions_file"), dict):
            try:
                _write_private(os.path.join(str(state_dir), "decisions.json"), json.dumps(ans["decisions_file"], indent=2))
            except OSError as err:
                _warn("decisions.json sync", err)
        return {"synced": 0}
    records, reminders = {}, None
    cloud = os.path.join(str(state_dir), "cloud")
    if os.path.isdir(cloud):
        for name in sorted(os.listdir(cloud)):
            if name.endswith(".json"):
                got = read(os.path.join(cloud, name))
                if got is not None:
                    records[name[:-5]] = got
    if switchboard_authority(env) != "cluster":
        reminders = read(os.path.join(str(state_dir), "reminders.json"))
    payload = {"records": records, "reminders": reminders}
    state_files = {}
    for fname in SYNC_STATE_FILES:
        got = read(os.path.join(str(state_dir), fname))
        if got is not None:
            state_files[fname] = json.dumps(got)
    if state_files:
        payload["state_files"] = state_files
    ans = call_switchboard_v1("/v1/cloud-sync", payload, env=env)
    if isinstance(ans, dict) and isinstance(ans.get("decisions_file"), dict) and switchboard_authority(env) == "cluster":
        try:
            _write_private(os.path.join(str(state_dir), "decisions.json"), json.dumps(ans["decisions_file"], indent=2))
        except OSError as err:
            _warn("decisions.json sync", err)
    return {"synced": len(records)}

def _healthy(url="http://127.0.0.1:%d/healthz" % PANE_RELAY_PORT):
    try:
        return urllib.request.urlopen(url, timeout=3).status == 200
    except Exception:
        return False

def _start_port_forward(lib):
    namespace = os.environ.get("CW_CLOUD_NAMESPACE") or "cw"
    base = ["kubectl"]
    for flag, name in (("--server", "CW_CLOUD_SERVER"), ("--certificate-authority", "CW_CLOUD_CA_FILE")):
        if os.environ.get(name):
            base += [flag, os.environ[name]]
    base += ["--token", lib.cluster_token(os.environ, duration="30m"), "-n", namespace]
    pods = json.loads(subprocess.run(base + ["get", "pods", "-o", "json"], capture_output=True, text=True, timeout=60, check=True).stdout)
    for item in pods.get("items", []):
        if item["metadata"]["name"].startswith("fleet-switchboard") and item.get("status", {}).get("phase") == "Running":
            return subprocess.Popen(base + ["port-forward", "pod/" + item["metadata"]["name"], "%d:8080" % PANE_RELAY_PORT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    raise FloorError("no running fleet-switchboard pod")

def run_pane_relay_loop(poll_seconds=5.0, max_passes=None, env=None):
    H = D["H"]
    azure_dir = fleetkit_azure_dir(os.path.expanduser("~"))
    if not os.environ.get("AZURE_CONFIG_DIR") and azure_dir:
        os.environ["AZURE_CONFIG_DIR"] = azure_dir
    lib = D["_lib"]()
    D["use_settings"]()
    engine, state_dir, _ = H["build_engine"](subprocess.run, time.time, "pane-relay")
    sb_mod = types.SimpleNamespace(**H)
    child, started, passes = None, 0.0, 0
    monitor, canaries = PaneRelayMonitor(), []
    try:
        while max_passes is None or passes < max_passes:
            passes += 1
            if max_passes is None and (child is None or child.poll() is not None or time.time() - started > 900 or not _healthy()):
                if child is not None and child.poll() is None:
                    child.terminate()
                try:
                    child, started = _start_port_forward(lib), time.time()
                    time.sleep(2)
                except Exception as err:
                    child = None
                    _warn("port-forward start (check kubectl and the cluster settings)", err)
            try:
                sync_cloud_records_to_cluster(state_dir, env=env)
            except Exception as err:
                _warn("cloud record sync", err)
            try:
                canaries = (pane_relay_pass(engine, sb_mod, env=env) or {}).get("canaries") or []
            except Exception as err:
                _warn("pane relay pass", err)
            if max_passes is None:
                try:
                    reachable = _healthy()
                    mode = fetch_cluster_mode(env) if reachable else None
                    act = monitor.tick(time.time(), reachable, mode, switchboard_authority(env), mac_daemon_paused(state_dir), canaries)
                    if act["send_canary"]:
                        call_switchboard_v1("/v1/send", {"to": "pane-relay", "text": act["send_canary"], "canary": True}, env=env)
                    if act.get("rollback"):
                        try:
                            rc = subprocess.run([sys.executable, os.path.join(D["kit_root"](), "tools", "sync-in-cluster-switchboard"), "--rollback"], timeout=120, check=False).returncode
                            rc and _warn("rollback sync", rc)
                        finally: rollback_switchboard_authority(state_dir, env=env)
                    if act["restart"]:
                        started = 0.0
                except Exception as err:
                    _warn("relay monitor", err)
            if max_passes is None or passes < max_passes:
                time.sleep(poll_seconds)
    finally:
        if child is not None and child.poll() is None:
            child.terminate()

def relay_cli_if_configured(command, payload, env=None):
    """Forward a Mac CLI action to the in-cluster switchboard; None when CW_SWITCHBOARD_URL is not set."""
    if not switchboard_relay_config(env)["url"]:
        return None
    orig_payload = payload
    if command in ("send", "report", "remind", "retire", "orders", "decisions", "intent") and isinstance(payload, dict):
        payload = _resolve_local_caller_and_repo(payload, env=env)
    if command == "send" and isinstance(payload, dict) and isinstance(payload.get("text"), str):
        found = {}
        for match in OVERSIZE_RE.finditer(payload["text"]):
            full = match.group(2)
            try:
                if os.path.isfile(full):
                    with open(full, encoding="utf-8", errors="replace") as handle:
                        found[match.group(4)] = handle.read()
            except OSError:
                pass
        if found:
            payload = dict(payload, oversize_files=found)
    ans = call_switchboard_v1("/v1/" + command, payload, env=env)
    if command == "decisions" and isinstance(ans, dict) and isinstance(ans.get("decisions_file"), dict):
        try:
            _write_private(os.path.join(str(D["H"]["state_dir_path"]()), "decisions.json"), json.dumps(ans["decisions_file"], indent=2))
        except Exception as err:
            _warn("decisions.json update", err)
    if env is None and command in PASSTHROUGH_COMMANDS and isinstance(ans, dict):
        _PASSTHROUGH_AUTH[:] = [os.environ.pop("CW_SWITCHBOARD_AUTHORITY", None)]
    if command == "remind" and isinstance(ans, dict):
        if isinstance(orig_payload, dict):
            orig_payload["_relay_ans"] = ans
        if switchboard_authority(env) != "cluster" and payload.get("action") in (None, "add") and ans.get("id") and ans.get("at") is not None:
            try:
                H = D["H"]
                if "build_engine" in H:
                    engine = H["build_engine"](subprocess.run, time.time, "cli")[0]
                    engine.audit.append("cli", "reminder.add", payload.get("to") or payload.get("name"), {
                        "id": ans["id"], "at": ans.get("at_iso") or H["iso"](float(ans["at"])),
                        "from": payload.get("_caller_agent"), "issue": payload.get("issue"), "repo": payload.get("repo"),
                        **({"key": str(payload["key"])[:60]} if payload.get("key") else {})})
            except Exception as err:
                _warn("local audit of reminder %s" % ans.get("id"), err)
    return ans

def _resolve_local_caller_and_repo(payload, env=None):
    """Best effort: name the calling lane (`_caller_agent`) and fill `repo` + numeric `issue` from its default repo."""
    try:
        H = D["H"]
        if "build_engine" not in H:
            return payload
    except Exception:   # no engine in this process (a bare unit-test load): nothing to resolve
        return payload
    try:
        engine, _, config = H["build_engine"](subprocess.run, time.time, "cli")
        caller = payload.get("sender") or payload.get("from") or H["identify_caller"](engine.herdr, engine.v2, env or os.environ)
        if isinstance(caller, str) and CALLER_RE.fullmatch(caller):
            payload = dict(payload, _caller_agent=caller)
        if payload.get("issue") is not None and not payload.get("repo") and "resolve_for_sender" in H:
            repo, number = H["resolve_for_sender"](engine, config, payload["issue"], caller)
            if repo and number is not None:
                payload = dict(payload, repo=repo, issue=int(number))
    except Exception as err:   # the action still goes, without the caller or repo: say so
        _warn("resolving the caller and repo (sent without them)", err)
    return payload

def push_pane_state(observations, env=None, now=None):
    """Push the Mac's pane observations; returns the commands the cluster answers with."""
    ans = call_switchboard_v1("/v1/pane-state", {"agents": observations, "at": now}, env=env)
    return (ans.get("commands") or []) if isinstance(ans, dict) else []

def _stayed_name(name):
    """D[name], or an unmatchable class when the name is not there (`D` has no `get` and no membership test)."""
    try:
        return D[name]
    except KeyError:
        return ()

def _one_line_text(err):
    return " ".join(str(err).split())[:160]
def floor_watch_sync(state_dir, records, engine=None):
    """Read the Floor controller's sessions (the source of truth for backend=floor): audit `cloud.stopped` once for a finished session and add a virtual record for a session no local file holds. Never raises: an offline pass is not broken by it."""
    try:
        held = transport()
        sessions = held._state()["sessions"] if hasattr(held, "_state") else getattr(held, "works", None)
        if sessions is None and not hasattr(held, "_state"):
            sessions = {}   # a transport with no session map (HTTP): each record is observed one by one below
        if not isinstance(sessions, dict):
            return records
        sessions = {k: v for k, v in sessions.items() if isinstance(v, dict)}
    except Exception as err:
        if isinstance(err, _stayed_name("Refusal")):
            env = env_of()
            if not any((env.get(k) or "").strip() for k in ("CW_FLOOR_URL", "CW_FLOOR_QUEUE_URL", "CW_FLOOR_BLOB_URL", "CW_FLOOR_TRANSPORT")):
                return records   # no Floor is configured: nothing to read, and nothing to say
            print("floor_watch_sync: the Floor state could not be read (%s)" % _one_line_text(err), file=sys.stderr)
            return records
        print("floor_watch_sync: the Floor state could not be read (%s)" % type(err).__name__, file=sys.stderr)
        return records
    wakes_path = os.path.join(str(state_dir), "floor-terminal-wakes.json")
    woken = {}
    try:
        with open(wakes_path, encoding="utf-8") as handle:
            got = D["parse_bounded"](handle.read())
        woken = got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        woken = {}
    known = set()
    for record in list(records):
        if not isinstance(record, dict) or record.get("backend") != "floor":
            continue
        known.add(record.get("work_id") or record.get("session"))
        found = sessions.get(record.get("work_id")) or sessions.get(record.get("session"))
        if found is None and not hasattr(held, "_state"):
            try:
                found = _observe(record)
            except Exception as err:
                print("floor_watch_sync: the Floor session of %s was not read (%s)" % (scrub(record.get("worker"), 40), type(err).__name__), file=sys.stderr)
                found = None
        found = found if isinstance(found, dict) else {}
        s_state = found.get("state")
        if s_state is None:
            continue
        record["floor_state"] = s_state
        record["landed_sha"] = found.get("landed_sha")
        record["floor_error"] = found.get("error")
        if s_state not in FINISHED_STATES:
            continue
        if getattr(engine, "audit", None) and not record.get("stopped_audited"):
            try:
                engine.audit.append(engine.actor, "cloud.stopped", record["worker"], {"exit": 0 if s_state != "failed" else 1, "reason": s_state, "runtime_id": record.get("runtime_id"), "backend": "floor"})
                record["stopped_audited"] = True
                D["save_record"](state_dir, record)
            except Exception as err:   # not marked audited: the next pass tries again, and says so
                record.pop("stopped_audited", None)
                print("floor_watch_sync: cloud.stopped for %s was not recorded (%s)" % (scrub(record.get("worker"), 40), type(err).__name__), file=sys.stderr)
        if s_state in ("done", "failed") and engine is not None and hasattr(engine, "send") and not record.get("terminal_woken") and not woken.get("%s:%s" % (record["worker"], s_state)):
            boss = record.get("reports_to")
            if isinstance(boss, str) and boss.strip():
                if s_state == "done":
                    msg = "Floor worker %s finished (done): landed_sha=%s" % (record["worker"], scrub(found.get("landed_sha") or "none", 60))
                else:
                    msg = "Floor worker %s finished (failed): %s" % (record["worker"], scrub(found.get("error") or "unknown error", 200))
                try:
                    wkey = "%s:%s" % (record["worker"], s_state)
                    woken[wkey] = True
                    _write_private(wakes_path, json.dumps(woken, sort_keys=True))
                    record["terminal_woken"] = True   # saved BEFORE the send: a failed save never leads to a second wake
                    D["save_record"](state_dir, record)
                    engine.send("switchboard", boss.strip(), msg, issue=record.get("issue"), repo=record.get("repo"))
                except Exception as err:
                    record.pop("terminal_woken", None)
                    woken.pop("%s:%s" % (record["worker"], s_state), None)
                    try:
                        _write_private(wakes_path, json.dumps(woken, sort_keys=True))
                    except OSError:
                        pass
                    print("floor_watch_sync: terminal wake for %s failed (%s)" % (scrub(record.get("worker"), 40), type(err).__name__), file=sys.stderr)
    for work_id, found in sessions.items():
        name, repo = found.get("worker") or found.get("title"), found.get("repo")
        if work_id in known or not name or not repo or any(isinstance(r, dict) and r.get("worker") == name for r in records):
            continue
        records.append({"worker": name, "backend": "floor", "work_id": work_id, "session": found.get("session", work_id), "repo": repo, "floor_state": found.get("state"), "virtual": True})
    return records

def drain_check(state, expected_workers):
    """{"lost": N, "twice": M} over the records of `state`: lost is an expected worker with no usable record; twice is a work that two records claim, or a Floor record that still carries what only a pod holds (its password file, its pod uid): two authorities for one worker."""
    records = D["list_records"](state)
    by_name = {r.get("worker"): r for r in records if "problem" not in r}
    lost = sum(1 for name in set(expected_workers) if name not in by_name)
    claims, twice = {}, 0
    for record in by_name.values():
        if is_floor(record):
            claims.setdefault(record.get("work_id"), []).append(record["worker"])
            if record.get("pod_uid") or record.get("auth_ref") or D["password_path"](state, record["worker"]).exists():
                twice += 1
    twice += sum(len(names) - 1 for names in claims.values() if len(names) > 1)
    return {"lost": lost, "twice": twice}
def retire_pod_launcher(state, env=None):
    """Whether the pod launcher may be retired: only when the backend is `floor`, the rollback window is `closed`, no pod worker is live and no record is unreadable. It decides and changes nothing: a refusal says why (exit 2 through the command that asks)."""
    env = env_of() if env is None else env
    Refusal = D["Refusal"]
    if (env.get("CW_REMOTE_BACKEND") or "").strip().lower() != "floor":
        raise Refusal("the pod launcher stays: CW_REMOTE_BACKEND is not floor; nothing was changed")
    if (env.get("CW_POD_ROLLBACK_WINDOW") or "open").strip().lower() != "closed":
        raise Refusal("the pod launcher stays: the rollback window is still open (CW_POD_ROLLBACK_WINDOW is not closed); nothing was changed")
    records = D["list_records"](state)
    pods = [r.get("worker") for r in records if "problem" not in r and not is_floor(r)]
    unknown = [r.get("worker") for r in records if "problem" in r]
    if pods:
        raise Refusal("the pod launcher stays: %d pod worker(s) are still live (%s); nothing was changed" % (len(pods), ", ".join(str(p) for p in pods[:5])))
    if unknown:
        raise Refusal("the pod launcher stays: %d record(s) have an unknown outcome (%s); nothing was changed" % (len(unknown), ", ".join(str(p) for p in unknown[:5])))
    drained = drain_check(state, [r["worker"] for r in records])
    if drained["lost"] or drained["twice"]:
        raise Refusal("the pod launcher stays: the drain found %d lost and %d run twice; nothing was changed" % (drained["lost"], drained["twice"]))
    return dict(drained, ready=True)

def bind(namespace):
    """Wrap `_launch`, `cmd_send`, `cmd_read`, `status_lines` and `cmd_retire` of bin/cloud_worker.py. Each keeps `__wrapped__` and takes a pod record to the old code."""
    refusals = namespace["refusals"]
    inner_launch, inner_send, inner_read = namespace["_launch"], namespace["cmd_send"], namespace["cmd_read"]
    inner_status, inner_retire = namespace["status_lines"], namespace["cmd_retire"]
    real_list = namespace["list_records"]
    _launch = _launch_wrapper(inner_launch)
    @refusals
    def cmd_send(args, runner, now, record):
        if not is_floor(record):
            return inner_send(args, runner, now, record)
        return _send_floor(args, runner, now, record)
    @refusals
    def cmd_read(args, runner, now):
        record = D["route"](args.name) if D["valid_name"](args.name) else None
        if not is_floor(record):
            return inner_read(args, runner, now)
        return _read_floor(args, record)
    def status_lines(runner, state, *args, **kwargs):
        records = D["list_records"](state)
        floors = [r for r in records if is_floor(r)]
        if not floors:
            return inner_status(runner, state, *args, **kwargs)
        pods = [r for r in records if not is_floor(r)]
        lines, rows = [], []
        if pods:   # the pod read sees the pod records only: a Floor record is never given to the launcher
            namespace["list_records"] = lambda _state: pods
            try:
                lines, rows = inner_status(runner, state, *args, **kwargs)
            finally:
                namespace["list_records"] = real_list
        else:
            lines = ["      cloud: %d Floor session(s), no pod worker" % len(floors)]
        more, extra = _status_floor(floors)
        return lines + more, rows + extra
    @refusals
    def cmd_retire(args, runner, now, record):
        if not is_floor(record):
            return inner_retire(args, runner, now, record)
        return _retire_floor(args, runner, now, record)
    for wrapper, inner in ((_launch, inner_launch), (cmd_send, inner_send), (cmd_read, inner_read), (status_lines, inner_status), (cmd_retire, inner_retire)):
        wrapper.__wrapped__ = inner
        wrapper.__name__, wrapper.__doc__ = inner.__name__, inner.__doc__
    namespace["_launch"], namespace["cmd_send"], namespace["cmd_read"] = _launch, cmd_send, cmd_read
    namespace["status_lines"], namespace["cmd_retire"] = status_lines, cmd_retire

def apply_pane_state_to_discovery(pane_state, discovery):
    for name, data in (pane_state or {}).items() if isinstance(pane_state, dict) else ():
        if not isinstance(data, dict):
            continue
        obs = discovery.agents.get(name)
        if obs is None and "Observation" in D["H"]:
            obs = D["H"]["Observation"](name, data.get("session_id") or ("ses_" + name), data.get("pane"), data.get("status") or "idle", data.get("fleet") if isinstance(data.get("fleet"), dict) else {})
            discovery.agents[name] = obs
        if obs is not None:
            obs.status = data.get("status") or obs.status
            obs.errored = bool(data.get("errored"))
            obs.turn_error = data.get("turn_error") or ""
            obs.turn_error_id = data.get("turn_error_id")
            obs.idle_at = data.get("idle_at")
            obs.outcome = data["outcome"] if "outcome" in data else getattr(obs, "outcome", None)
            obs.outcome = obs.outcome or ("succeeded" if obs.status == "idle" and not obs.errored else None)
            obs.permission_waiting = bool(data.get("permission_waiting"))
            obs.form_waiting = bool(data.get("form_waiting"))
    return discovery
def queue_pane_resume_command(state, agent, reason):
    cmd = {"action": "resume", "agent": agent, "reason": scrub(reason or "", 200), "from": "switchboard"}
    state.setdefault("pane_commands", []).append(cmd)
    state.setdefault("pane_outbox", []).append(cmd)
    return cmd
if __name__ == "__main__":
    import sys as _sys
    _args = _sys.argv[1:]
    if _args[:1] != ["pane-relay"]:
        raise SystemExit("usage: cloud_floor.py pane-relay --kit-root DIR --state-dir DIR")
    _kit = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for _flag in ("--kit-root", "--state-dir"):
        if _flag in _args:
            if _args.index(_flag) + 1 >= len(_args):
                raise SystemExit("%s needs a value" % _flag)
    if "--kit-root" in _args:
        _kit = _args[_args.index("--kit-root") + 1]
    if "--state-dir" in _args:
        os.environ.setdefault("FLEET_STATE_DIR", _args[_args.index("--state-dir") + 1])
    # The switchboard entry runs the loop itself: no code is loaded by name here.
    os.execv(_sys.executable, [_sys.executable, "-B", os.path.join(_kit, "bin", "fleet-switchboard"), "--run-pane-relay-loop"])
