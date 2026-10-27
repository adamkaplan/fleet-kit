"""Cloud relay (issue 1225, slice A): the hub half of pod -> hub -> daemon calls for report, prove, verdict, gates.

    POST /relay               pod: {"call_id", "command", "pr": "owner/repo#N", "head", "args"}; the pod's own service-account token as Bearer.
    GET  /relay/pending       daemon only: {"requests": [{"id", "pod", "repo", "issue", "call"}]}, oldest first, at most 10.
    POST /relay/result/<id>   daemon only: {"status", "reason", "record": {"seq", "hash"} | null}; answers the waiting pod call.

The hub resolves pod, repo and issue from the secret and the agents map (record keys "repo", "issue"), never from the body.
Refusals (answered before anything is queued, one audit line each, never the secret): 401 no or bad secret, 404 pod not in the
map, 400 unlisted command / missing or malformed call id / bad fields, 413 oversized, 403 PR outside the pod's bound repo.
Stale head and the independence rules are the daemon's: it answers them through /relay/result.
The queue is in memory and bounded; the hub keeps no ledger and no GitHub token. The daemon is the source of truth for call ids
(pod + call_id -> record); a replay here joins the entry already queued or answered (200 only: a refused or failed answer is forgotten, so the same call_id is
evaluated afresh; a replay whose command, pr or head differs is 409), else queues again and the daemon dedupes.
A call not answered within WAIT seconds gets 504 and its entry stays queued, so the same call id is replay-safe.
"""
import collections
import hashlib
import json
import re
import threading
import time
import uuid

import cloud_channel

# The one definition of the wire shapes; the daemon (cloud_relay_daemon) imports these.
COMMANDS = ("report", "prove", "verdict", "gates")
NEEDS_HEAD = ("prove", "verdict")
NEEDS_LINK = ("gates", "prove", "verdict", "report")   # the PR must be linked to the pod's issue
CALL_ID = re.compile(r"[\x21-\x7e]{1,64}")
PR = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]{1,9})")
HEAD = re.compile(r"[0-9a-f]{40}")   # the full live sha, never a prefix
WAIT = 30.0
BATCH = 10
MAX_PENDING = 200
MAX_PER_POD = 20
MAX_WAIT_PER_POD = 4   # held POST /relay waits per pod (each holds a hub handler slot for up to WAIT seconds)
MAX_WAIT_TOTAL = 16    # upper bound on held waits; Relay.configure(max_handlers) lowers it to max_handlers//2 when the server is smaller
BODY_DEADLINE = 5.0    # seconds in total for a pod's relay request body to arrive (the socket timeout alone is per read)
PENDING_TTL = 120.0   # seconds an unanswered entry may wait for the daemon before it is dropped
MAX_ARGS = 16
MAX_ARG_KEY = 64
MAX_ARG_VALUE = 8192
MAX_DONE = 1000


def _no_constant(name):
    raise ValueError("%s is not JSON" % name)


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def loads_strict(raw):
    """json.loads for a relay body: NaN/Infinity and duplicate keys are ValueError. Very deep nesting raises RecursionError (callers catch both)."""
    return json.loads(raw, parse_constant=_no_constant, object_pairs_hook=_no_duplicates)


class Refusal(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status, self.reason = status, reason


def fingerprint(call):
    """What makes two calls with one call_id the same call: command, pr, head and a digest of the whole args (evidence text included)."""
    args = json.dumps(call.get("args"), sort_keys=True, default=str)
    return (call.get("command"), call.get("pr"), call.get("head"), hashlib.sha256(args.encode()).hexdigest())


class Entry:
    def __init__(self, pod, repo, issue, call):
        self.id = uuid.uuid4().hex
        self.pod, self.repo, self.issue, self.call = pod, repo, issue, call
        self.done = threading.Event()
        self.answer = None
        self.born = None
        self.fingerprint = fingerprint(call)


class Relay:
    def __init__(self, agents_read, wait=WAIT, ttl=PENDING_TTL, max_pending=MAX_PENDING, max_per_pod=MAX_PER_POD, clock=time.monotonic):
        self.agents_read = agents_read   # () -> {agent: {"repo", "issue", ...}}
        self.wait = wait
        self.deny = set()   # names that are not pods (the daemon credential): refused 403 once the pod is known to the map
        self.ttl, self.max_pending, self.max_per_pod, self.clock = ttl, max_pending, max_per_pod, clock
        self.lock = threading.Lock()
        self.waiting = {}   # pod -> waits held right now
        self.max_wait_pod, self.max_wait_total = MAX_WAIT_PER_POD, MAX_WAIT_TOTAL
        self.max_inflight, self.inflight = None, 0   # POST /relay handlers in the hub right now (any stage); None until configure()

        self.pending = collections.OrderedDict()   # id -> Entry, unanswered
        self.by_call = {}                          # (pod, call_id) -> Entry (unanswered or recently answered)
        self.finished = collections.OrderedDict()  # (pod, call_id) -> Entry, answered, bounded

    def configure(self, max_handlers):
        """Derive the caps from the server's real handler count. Pod POST /relay handlers (every stage: token review,
        body read, held wait) are admitted at most max_handlers//2 at once, so at least half the slots stay free for the
        daemon's GET /relay/pending and POST /relay/result AND for channel/decisions requests (those share that other
        half; the daemon routes are not reserved against them). ValueError when max_handlers < 2 (no half to keep)."""
        if not isinstance(max_handlers, int) or isinstance(max_handlers, bool) or max_handlers < 2:
            raise ValueError("the relay needs max_handlers >= 2 to keep half the slots for the daemon, got %r" % (max_handlers,))
        self.max_inflight = max_handlers // 2
        self.max_wait_total = min(MAX_WAIT_TOTAL, self.max_inflight)

    def admit(self):
        """Count one pod relay handler in; False (answer 503) when max_inflight are already in. Pair with release()."""
        with self.lock:
            if self.max_inflight is not None and self.inflight >= self.max_inflight:
                return False
            self.inflight += 1
            return True

    def release(self):
        with self.lock:
            self.inflight -= 1

    def validate(self, pod, body):
        """The call dict for a pod's body, or Refusal. Touches nothing."""
        try:
            agents = self.agents_read()
        except (OSError, ValueError):
            # fail closed: with the agents map unavailable no pod can be bound to a repo, so every call is refused 503 (never guessed, never queued)
            raise Refusal(503, "agents map unreadable")
        record = agents.get(pod) if isinstance(agents, dict) else None
        if not isinstance(record, dict) or not isinstance(record.get("repo"), str):
            raise Refusal(404, "unknown pod: not bound to a repo")
        if pod in self.deny:
            raise Refusal(403, "the daemon credential cannot relay as a pod")
        if not isinstance(body, dict):
            raise Refusal(400, "body must be a JSON object")
        command = body.get("command")
        if command not in COMMANDS:
            raise Refusal(400, "command not allowed: only %s" % "|".join(COMMANDS))
        call_id = body.get("call_id")
        if not isinstance(call_id, str) or not CALL_ID.fullmatch(call_id):
            raise Refusal(400, "call_id missing or malformed: printable ASCII, at most 64")
        pr = body.get("pr")
        match = PR.fullmatch(pr) if isinstance(pr, str) else None
        if not match:
            raise Refusal(400, "pr missing or malformed: owner/repo#N")
        if match.group(1) != record["repo"]:
            raise Refusal(403, "PR repo is not the pod's bound repo")
        head = body.get("head")
        if command in NEEDS_HEAD and not (isinstance(head, str) and HEAD.fullmatch(head)):
            raise Refusal(400, "head required for %s: a hex sha" % command)
        if head is not None and not (isinstance(head, str) and HEAD.fullmatch(head)):
            raise Refusal(400, "head malformed")
        args = body.get("args", {})
        if not isinstance(args, dict):
            raise Refusal(400, "args must be an object")
        for key, value in args.items():
            if len(args) > MAX_ARGS or not isinstance(key, str) or len(key) > MAX_ARG_KEY:
                raise Refusal(400, "args: at most %d fields, names up to %d characters" % (MAX_ARGS, MAX_ARG_KEY))
            if not (value is None or isinstance(value, (bool, int)) or (isinstance(value, str) and len(value) <= MAX_ARG_VALUE)):
                raise Refusal(400, "args: a value is a string up to %d characters, a number, a boolean or null" % MAX_ARG_VALUE)
        call = {"call_id": call_id, "command": command, "pr": pr, "args": args}
        if head is not None:
            call["head"] = head
        return record, call

    def submit(self, pod, body):
        """Validate, queue (or join), wait; returns (status, payload). Raises Refusal before queueing."""
        record, call = self.validate(pod, body)
        key = (pod, call["call_id"])
        with self.lock:
            self._expire()
            entry = self.by_call.get(key)
            # NOTE: the wait caps below are checked BEFORE joining an existing entry: a replay of an already queued,
            # still unanswered call from a pod at its per-pod cap gets 429 (it would hold a slot like any other wait).
            # A replay of an already answered call is served without any cap.
            if entry is not None and entry.fingerprint != fingerprint(call):
                return 409, {"status": 409, "error": "call_id reused for a different call"}
            if not (entry is not None and entry.done.is_set()):
                if self.waiting.get(pod, 0) >= self.max_wait_pod:
                    raise Refusal(429, "in-flight wait cap reached: at most %d held calls per pod" % self.max_wait_pod)
                if sum(self.waiting.values()) >= self.max_wait_total:
                    raise Refusal(503, "in-flight wait cap reached: at most %d held calls on the hub" % self.max_wait_total)
            if entry is None:
                if sum(1 for e in self.pending.values() if e.pod == pod) >= self.max_per_pod:
                    raise Refusal(503, "per-pod cap reached: %d unanswered calls from this pod" % self.max_per_pod)
                if len(self.pending) >= self.max_pending:
                    raise Refusal(503, "global cap reached: relay queue full (%d)" % self.max_pending)
                entry = Entry(pod, record["repo"], record.get("issue"), call)
                entry.born = self.clock()
                self.pending[entry.id] = entry
                self.by_call[key] = entry
            if entry.done.is_set():
                return entry.answer
            self.waiting[pod] = self.waiting.get(pod, 0) + 1
        try:
            if not entry.done.wait(self.wait):
                return 504, {"status": 504, "error": "daemon did not answer in time; replay with the same call_id"}
            return entry.answer
        finally:
            with self.lock:
                self.waiting[pod] -= 1
                if not self.waiting[pod]:
                    del self.waiting[pod]

    def _expire(self):
        """Drop unanswered entries older than the TTL (caller holds the lock); their waiters are answered 504."""
        now = self.clock()
        for eid in [i for i, e in self.pending.items() if now - e.born > self.ttl]:
            entry = self.pending.pop(eid)
            self.by_call.pop((entry.pod, entry.call["call_id"]), None)
            entry.answer = (504, {"status": 504, "error": "daemon did not answer within %ds; entry dropped, replay with the same call_id" % self.ttl})
            entry.done.set()

    def list_pending(self):
        with self.lock:
            self._expire()
            return [{"id": e.id, "pod": e.pod, "repo": e.repo, "issue": e.issue, "call": e.call}
                    for e in list(self.pending.values())[:BATCH]]

    def result(self, entry_id, body):
        """Record the daemon's answer; returns None or raises Refusal."""
        if not isinstance(body, dict):
            raise Refusal(400, "body must be a JSON object")
        status, reason, record = body.get("status"), body.get("reason", ""), body.get("record")
        if not isinstance(status, int) or isinstance(status, bool) or not 200 <= status <= 599:
            raise Refusal(400, "bad status")
        if not isinstance(reason, str) or len(reason) > 2000:
            raise Refusal(400, "bad reason")
        if record is not None and not (isinstance(record, dict) and isinstance(record.get("seq"), int)
                                       and isinstance(record.get("hash"), str)):
            raise Refusal(400, "bad record")
        payload = {"status": status, "reason": reason, "record": record}
        if status != 200:
            payload["error"] = reason
        with self.lock:
            self._expire()
            entry = self.pending.pop(entry_id, None)
            if entry is None:
                raise Refusal(404, "no such pending request")
            entry.answer = (status, payload)
            key = (entry.pod, entry.call["call_id"])
            if status != 200:   # a refusal is not replayable from here: the same id is evaluated afresh once the cause is fixed
                if self.by_call.get(key) is entry:
                    del self.by_call[key]
            else:
                self.finished[key] = entry
            while len(self.finished) > MAX_DONE:
                old, _ = self.finished.popitem(last=False)
                self.by_call.pop(old, None)
        entry.done.set()


def pod_of(reviewer, token, namespace):
    """Agent name proved by the token, or None."""
    try:
        review = reviewer(token, cloud_channel.AUDIENCE)
    except Exception:
        raise Refusal(503, "token review unavailable")
    if not isinstance(review, dict) or review.get("authenticated") is not True:
        return None
    audiences = review.get("audiences")
    if not isinstance(audiences, list) or cloud_channel.AUDIENCE not in audiences:
        return None
    return cloud_channel.agent_of(review.get("user"), namespace)
