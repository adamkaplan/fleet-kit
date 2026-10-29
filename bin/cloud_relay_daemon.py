#!/usr/bin/env python3
"""cloud_relay_daemon: the Mac-daemon half of the cloud hub relay (#1225, slice B).

The daemon PULLS `GET /relay/pending` from the hub, runs each request with the same rule code as the local commands (plan_ledger's
`may_record_verdict`, `authors_of`, `known_writers`, `evaluate_gates`; the switchboard's report), with `by` = the pod name the hub
resolved, and answers `POST /relay/result/<id>`. Every refusal writes nothing to the ledger (byte-identical) and leaves one audit line.

The world (GitHub, the plan, the registry, the audit) is a Backend, so the rules are testable with a fake; `SwitchboardBackend` is the real one.
One ledger entry per prove or verdict, written by `plan_ledger.append_checked` under its flock; the head is re-read from GitHub just BEFORE
that decision (a fresh read, never the request's memo, under HEAD_TIMEOUT; one more call in the request's allowance). Replay memory: `relay_calls.json` (pod + call id -> seq, hash), backed by the call id stored in the entry itself.

Report delivery is AT-MOST-ONCE per call id: a report can be LOST (never duplicated); for `report` the ask's "exactly one" reads "at most one". A lost report is
told to the pod by name (409 "lost or still pending: resend with a NEW call_id"). Report delivery, in detail: the call id is CLAIMED first (a PENDING record, get-or-claim in one
step under the index flock), then delivered, then marked DONE. A replay of a done id answers 200 "replay: already reported"; a replay of a
PENDING id (a crash, or a delivery that raised, between claim and done) is resolved deliberately: it answers that named 409 and
NEVER delivers a second time (the first delivery may have happened; the owner sees the audit line `report.claimed` and may resend under a new
call id). Claims live in their own file (`relay_reports.json`, REPORT_MAX entries, so a flood of prove/verdict entries cannot evict them; a
damaged claim file refuses with 500 rather than forgetting). A report replay with a different pr or different args under the same call id is 409.
Refusals (daemon-only credential, repo, allow-list, rules) write an AUDIT line `relay.refused` on purpose and leave the ledger and files
byte-identical. The hub's 1000-entry finished cache can be flooded to evict another pod's hub-side replay record; the daemon-side dedupe
above still holds it. Every hub request (connect, headers, body, response; GET and POST) has ONE wall-clock deadline (READ_DEADLINE 30 s), and a relay pass a budget (PASS_BUDGET 60 s: later requests wait for the next pass); each hub request runs in a worker thread joined at READ_DEADLINE (DNS and every connect attempt included; a late thread is abandoned and the pass records relay pass cut), so the transport of a pass is at most PASS_BUDGET + READ_DEADLINE = 90 s. Only ONE hub worker thread lives at a time (a new request while an abandoned one runs is refused as a cut: "previous request still running"), and a cut sets a cancel flag checked after connect and before the send, so a late thread never POSTs; a late GET result is discarded silently. The up to 40 GitHub context reads of a pass carry NO deadline inside the pass budget: they are unbounded. The hub read has a TOTAL deadline (READ_DEADLINE) and the live head is read FRESH, OUTSIDE the ledger flock (bounded by HEAD_TIMEOUT, refused 504 with nothing written when late); inside the flock the build only compares it with the head the request judged: no network under the lock.
GitHub reads per pass, worst case: at most 10 requests, each making _ctx reads (a ledger context = PR facts and plan from GitHub): report 1 (link), gates 2, prove 3 (link, plan, live head), verdict 4 (link, plan, PR facts, live head); so at most 10 x 4 = 40 context reads per pass.
Memory limit, exactly: report claims survive restarts and a missing or pruned relay_calls.json (own file, REPORT_MAX 50000 entries; beyond that the oldest claim is forgotten, and a deleted relay_reports.json is an empty memory, so a report with that call id would then be delivered again). A damaged file refuses instead.

The PR-to-issue link comes from the PR body's closing keywords (and the plan's issue). It is NOT a security boundary: anyone who can edit
the PR body can set it. The boundaries are the prove holder check (`relay_holders`) and the verdict evaluator registry (`plan_evaluators`).

Pod evidence text (prove/verdict args) is stored in the ledger UNSCRUBBED: the ledger is not secret-safe, so pods must never put secrets in it.
"""
import contextlib
import errno
import hashlib
import fcntl
import json
import os
import re
import stat
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plan_ledger  # noqa: E402
import cloud_relay  # noqa: E402  (the one definition of the wire shapes, shared with the hub)

INDEX_FILE = "relay_calls.json"
REPORT_FILE = "relay_reports.json"
REPORT_MAX = 50000   # report claims kept (their own file); the oldest are pruned beyond this
READ_DEADLINE = 30.0   # seconds in TOTAL for one hub answer (the socket timeout alone is per read)
HEAD_TIMEOUT = 10.0    # seconds the in-lock head re-check may take before the write is refused
INDEX_MAX = 5000   # entries kept; the oldest are pruned (the ledger's own call ids still answer a pruned replay)
COMMANDS = cloud_relay.COMMANDS
NEEDS_HEAD = cloud_relay.NEEDS_HEAD
NEEDS_LINK = cloud_relay.NEEDS_LINK
PENDING_CAP = 4 * 1024 * 1024   # bytes of the hub's pending list; larger is refused
TOKEN_CAP = 4096                # bytes of the token file
REASON_FIXED = "relay failed"
RELAY_GH_PER_HOUR = 600         # GitHub calls (not requests) all relay requests may make in a rolling hour (PC-6)
RELAY_GH_WINDOW = 3600.0        # seconds of that rolling hour
RELAY_GH_PER_POD = 120          # of those, the most ONE pod may spend in the rolling hour (a share: one pod cannot starve the others)
RELAY_GH_PER_REQUEST = 3        # most calls one request makes: 1 pull request read + at most 2 plan reads
BUDGET_REASON = "relay GitHub budget"
POD_BUDGET_REASON = "relay GitHub budget: pod share"
PENDING_REASON = "already claimed, never delivered twice: lost or still pending: resend with a NEW call_id"
PASS_BUDGET = 60.0              # wall-clock seconds of one relay pass; requests not started by then wait for the next pass
REASON_MAX = 1900               # the hub refuses a reason over 2000 characters; every answer is clipped below that
INDEX_READ_CAP = 8 * 1024 * 1024   # bytes of relay_calls.json read; the file is written only by this daemon (INDEX_MAX entries), a larger one is treated as damaged
RID_RE = re.compile(r"[0-9a-f]{32}")
PR_RE, CALL_ID_RE, SHA_RE = cloud_relay.PR, cloud_relay.CALL_ID, cloud_relay.HEAD


class Refuse(Exception):
    """`reason` goes to the pod (fixed or allow-listed text only); `detail`, when given, goes to the audit instead."""

    def __init__(self, status, reason, detail=None):
        super().__init__(reason)
        self.status, self.reason, self.detail = status, reason, detail


class Replay(Exception):
    def __init__(self, record):
        super().__init__("replay")
        self.record = record


def _same_head(known, given):
    return bool(known) and bool(given) and known == given


class Index:
    """pod + call id -> {seq, hash, command, pr, head}, for prove and verdict only. A separate daemon-side file, written under a flock
    with a unique temp name and pruned to `cap` (oldest first); a missing or damaged file falls back to the entries' own call ids.
    With strict=True (the report claims) a DAMAGED or oversized file is an error (OSError/ValueError), never an empty memory: a report
    must not be delivered again because its memory was lost. A file that does not exist yet is an empty memory."""

    def __init__(self, state_dir, name=INDEX_FILE, cap=None, strict=False):
        self.name, self._cap, self.strict = name, cap, strict
        self.path = Path(state_dir) / name

    @property
    def cap(self):
        return self._cap or INDEX_MAX

    def _read(self):
        try:
            with open(str(self.path), "rb") as handle:
                raw = handle.read(INDEX_READ_CAP + 1)
            if len(raw) > INDEX_READ_CAP:
                raise ValueError("index over %d bytes" % INDEX_READ_CAP)
            data = json.loads(raw.decode())
            if not isinstance(data, dict):
                raise ValueError("index is not an object")
            return data
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            if self.strict:
                raise
            return {}

    def get(self, pod, call_id):
        return self._read().get("%s\n%s" % (pod, call_id))

    @contextlib.contextmanager
    def _locked(self):
        lock = self.path.with_name(self.name + ".lock")
        with os.fdopen(os.open(str(lock), os.O_WRONLY | os.O_CREAT, 0o600), "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            yield

    def _store(self, data):
        while len(data) > self.cap:
            data.pop(next(iter(data)))
        tmp = self.path.with_name("%s.%d.%s.tmp" % (self.name, os.getpid(), uuid.uuid4().hex))
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps(data))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(tmp), str(self.path))
        finally:
            with contextlib.suppress(OSError):
                os.unlink(str(tmp))

    def put(self, pod, call_id, record):
        with self._locked():
            data = self._read()
            key = "%s\n%s" % (pod, call_id)
            data.pop(key, None)
            data[key] = record     # json keeps insertion order: the newest is last
            self._store(data)

    def claim(self, pod, call_id, record):
        """Get-or-claim in ONE locked step: the existing record when there is one (nothing written), else None after `record` is
        stored. Two concurrent passes cannot both get None."""
        with self._locked():
            data = self._read()
            key = "%s\n%s" % (pod, call_id)
            if key in data:
                return data[key]
            data[key] = record
            self._store(data)
            return None


def clean_text(value, limit=2000):
    """Control characters (all of Cc, newlines and tabs included) become spaces; whitespace is collapsed; clipped. engine.report then
    enforces its own one-line limit."""
    text = "".join(" " if (ord(c) < 32 or 0x7f <= ord(c) < 0xa0) else c for c in str(value or ""))
    return " ".join(text.split())[:limit]


def _from_entries(entries, pod, call_id):
    for e in entries:
        if e.get("by") == pod and e.get("call_id") == call_id:
            return {"seq": e["seq"], "hash": e["hash"], "command": {"proof": "prove", "verdict": "verdict"}.get(e.get("kind")),
                    "pr": e.get("pr"), "head": e.get("head"), "scenario": e.get("scenario"), "result": e.get("result"),
                    "evidence": e.get("evidence")}
    return None


def args_digest(args):
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()


def _same_call(known, command, pr, head, digest=None):
    """Same command, pr, head and (when the record carries one) the same args digest: a replay must be the call that was recorded."""
    if "args" in known and known.get("args") != digest:
        return False
    return known.get("command") == command and known.get("pr") == pr and known.get("head") == head


_TIMEOUT = object()


def _bounded(fn, timeout):
    """fn() with a deadline: _TIMEOUT when it does not answer in time, the Refuse itself when it raises one (429 budget/pause: the pod sees the named
    cause), None for any other failure (an unknown head, refused as stale)."""
    box = []

    def run():
        try:
            box.append(fn())
        except Refuse as r:
            box.append(r)
        except Exception:
            box.append(None)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return box[0] if box else _TIMEOUT


def _answer(known):
    return {"seq": known["seq"], "hash": known["hash"]}


class RelayBudget:
    """The hourly caps on the GitHub calls of relay requests (PC-6): a rolling window of call times, safe for two threads. TWO caps: a share per pod
    (`pod_cap`, keyed on the hub-resolved pod name: one hostile pod cannot spend the hour) and the global `cap` as a backstop. A refusal names which.
    `room` (asked before a request starts: the reserve of RELAY_GH_PER_REQUEST calls) and `take` (one call) are separate steps; `take` is the hard stop,
    so a request that outgrows its reserve is refused 429 in the middle (fail closed: nothing is written then either)."""

    def __init__(self, now, cap=None, window=None, pod_cap=None):
        self.now, self.lock, self.times, self.pods = now, threading.Lock(), [], {}
        self.cap = RELAY_GH_PER_HOUR if cap is None else cap
        self.pod_cap = RELAY_GH_PER_POD if pod_cap is None else pod_cap
        self.window = RELAY_GH_WINDOW if window is None else window

    def _trim(self):
        edge = self.now() - self.window
        while self.times and self.times[0] <= edge:
            self.times.pop(0)
        for pod in list(self.pods):
            mine = self.pods[pod]
            while mine and mine[0] <= edge:
                mine.pop(0)
            if not mine:
                del self.pods[pod]

    def _refusal(self, pod, need):
        if len(self.pods.get(pod, ())) + need > self.pod_cap:
            return POD_BUDGET_REASON
        if len(self.times) + need > self.cap:
            return BUDGET_REASON
        return None

    def room(self, pod=None, need=RELAY_GH_PER_REQUEST):
        """None when `need` more calls fit under both caps (asked BEFORE a request makes any call), else the refusal's reason."""
        with self.lock:
            self._trim()
            return self._refusal(pod, need)

    def take(self, pod=None):
        """Counts one call for `pod`: None when taken, else the reason (and nothing counted)."""
        with self.lock:
            self._trim()
            why = self._refusal(pod, 1)
            if why is None:
                at = self.now()
                self.times.append(at)
                self.pods.setdefault(pod, []).append(at)
            return why


_READ_FLAGS = frozenset({"-i", "--paginate"})                       # known read flags without a value
_READ_VALUE_FLAGS = frozenset({"-H", "--jq", "-q", "--cache"})      # known read flags that take one value


def _is_read(argv):
    """An ALLOW-list: only `gh pr view ...` and `gh api <path>` whose other args are all known read flags are memoized within a request.
    Anything else (any spelling of a method, field or input flag, `-XPOST`, `--field=x`, an unknown flag, a second positional) counts as a write."""
    if not isinstance(argv, (list, tuple)) or len(argv) < 3:
        return False
    if argv[1] == "pr" and argv[2] == "view":
        return True
    if argv[1] != "api":
        return False
    paths, i, rest = 0, 0, list(argv[2:])
    while i < len(rest):
        x = rest[i]
        if not isinstance(x, str):
            return False
        if x in _READ_FLAGS:
            i += 1
        elif x in _READ_VALUE_FLAGS:
            if i + 1 >= len(rest):
                return False
            i += 2
        elif x.startswith("-"):
            return False
        else:
            paths += 1
            i += 1
    return paths == 1


class MeteredRunner:
    """The runner every relay gh call goes through. The metering rules live with the gate (GhAccounts.check_pause and .record, which do nothing for a call
    made inside GhAccounts.read: `read` checks and counts that one itself, so a call is counted exactly once); this class adds only the relay's own
    hourly budget (global and per pod) and the per-request memo of READ argv shapes. One MeteredRunner belongs to ONE request (`for_request`)."""

    def __init__(self, runner, accounts, budget, now, pod=None):
        self.runner, self.accounts, self.budget, self.now, self.pod = runner, accounts, budget, now, pod
        self.memo = {}

    def for_request(self, pod):
        return MeteredRunner(self.runner, self.accounts, self.budget, self.now, pod)

    def __call__(self, argv, *a, **k):
        read = _is_read(argv)
        key = tuple(argv) if read else None
        if key is not None and key in self.memo:   # the same read twice in ONE request (the pull request picture, the plan): answered from the first
            return self.memo[key]
        if not read:
            self.memo.clear()   # a write: a later read must not be answered from before it
        done = self._call(argv, *a, **k)
        if key is not None and getattr(done, "returncode", 1) == 0:
            self.memo[key] = done
        return done

    def _call(self, argv, *a, **k):
        accounts = self.accounts
        if accounts is not None:
            try:
                accounts.check_pause()   # ANY paused account stops the call (fail closed)
            except Exception as err:
                if type(err).__name__ != "GhRateLimited":
                    raise
                raise Refuse(429, "GitHub rate limited: the daemon's gate is paused", "relay stopped by the shared GitHub pause") from None
        if self.budget is not None:
            why = self.budget.take(self.pod)
            if why:
                raise Refuse(429, why)
        if accounts is not None:
            accounts.record("relay")
        return self.runner(argv, *a, **k)


class Executor:
    """backend needs: live_head(pr), plan(pr) -> {"source","hash","scenarios":{sid:{"evaluator","claim"}}},
    pr_issues(pr) -> {issue numbers the PR is linked to}, pr_facts(pr) -> {"author","notice_senders"}, evaluators(evaluator), holder_of(repo, issue), gates(pr), report(pod, args), audit(kind, pr, detail)."""

    def __init__(self, state_dir, backend, now):
        self.state, self.backend, self.now = state_dir, backend, now
        self.index = Index(state_dir)
        self.reports = Index(state_dir, REPORT_FILE, REPORT_MAX, strict=True)

    # one request -> (status, reason, record)
    def handle(self, req):
        pod = req.get("pod")
        call = req.get("call") if isinstance(req.get("call"), dict) else {}
        pr = call.get("pr") if isinstance(call.get("pr"), str) else ""
        begin = getattr(self.backend, "begin_request", None)
        backend = begin(req) if begin else self.backend   # ONE context per request, held by the request itself: a late thread of an earlier request cannot reach it
        try:
            if not isinstance(pod, str) or not pod:
                raise Refuse(400, "no pod named")
            cid = call.get("call_id")
            if not isinstance(cid, str) or not CALL_ID_RE.fullmatch(cid):
                raise Refuse(400, "call_id must be printable ASCII, 1 to 64 characters")
            command = call.get("command")
            if command not in COMMANDS:
                raise Refuse(400, "command is not one of %s" % ", ".join(COMMANDS))
            m = PR_RE.fullmatch(pr)
            if not m:
                raise Refuse(400, "pr must be owner/repo#N")
            if m.group(1) != req.get("repo"):
                raise Refuse(403, "pr is not in the pod's bound repo")
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            if command in NEEDS_HEAD and not SHA_RE.fullmatch(str(call.get("head") or "")):
                raise Refuse(400, "%s needs the head sha it judged" % command)
            room = getattr(backend, "gh_room", None)
            why = room() if room is not None else None
            if why:   # BEFORE any gh call; audited as a refusal, nothing written
                raise Refuse(429, why)
            if command in NEEDS_LINK:   # the PR must be the one linked to the issue this pod is bound to (this read is at most 1 call, charged to the pod)
                self._check_linked(pr, req, backend)
            if command == "gates":   # read-only and not indexed: nothing to replay
                out = backend.gates(pr)
                backend.audit("gates", pr, {"by": clean_text(pod, 80), "call_id": clean_text(cid, 64)})
                text = json.dumps(out, sort_keys=True)
                if len(text) > REASON_MAX:   # a clipped JSON would be unreadable: send a short valid one
                    text = json.dumps({"ready": out.get("ready") if isinstance(out, dict) else None, "truncated": True})
                return 200, text, None
            if command == "report":   # claimed, then delivered, then marked done; kept apart from prove/verdict (no seq or hash)
                digest = args_digest(args)
                mine = {"command": "report", "pr": pr, "args": digest}
                seen = self.reports.claim(pod, cid, dict(mine, state="pending"))
                if seen:
                    if seen.get("pr") != pr or seen.get("args") != digest:
                        raise Refuse(409, "call_id reused for a different call")
                    if seen.get("state") == "done":
                        return 200, "replay: already reported", None
                    backend.audit("report.claimed", pr, {"by": clean_text(pod, 80), "call_id": clean_text(cid, 64)})
                    raise Refuse(409, PENDING_REASON, "report replay of a pending claim")
                # only the state and the text come from the pod; the issue is the hub's, and `everything` is dropped
                backend.report(pod, {"state": args.get("state"), "text": clean_text(args.get("text")), "issue": req.get("issue")})
                self.reports.put(pod, cid, dict(mine, state="done"))
                return 200, "reported", None
            known = self.index.get(pod, cid)
            if known:
                if not _same_call(known, command, pr, call["head"], args_digest(args)):
                    raise Refuse(409, "call_id reused for a different call")
                return 200, "replay: already recorded", _answer(known)
            record = self._write(command, pod, req, call, pr, cid, args, backend)
            self.index.put(pod, cid, dict(record, command=command, pr=pr, head=call["head"], args=args_digest(args)))
            return 200, "recorded", record
        except Replay as rep:
            first = rep.record   # rebuilt from the ledger entry: no digest, so compare what the entry holds
            clipped = plan_ledger.clip(str(args.get("evidence") or "").strip(), plan_ledger.EVIDENCE_LIMIT)
            if (not _same_call(first, command, pr, call.get("head")) or first.get("scenario") != args.get("scenario")
                    or first.get("result") != args.get("result") or first.get("evidence") != clipped):
                backend.audit("relay.refused", clean_text(pr, 80), {"by": clean_text(pod, 80), "status": 409, "why": "call_id reused for a different call"})
                return 409, "call_id reused for a different call", None
            self.index.put(pod, cid, dict(_answer(first), command=command, pr=pr, head=call.get("head"), args=args_digest(args)))
            return 200, "replay: already recorded", _answer(first)
        except Refuse as err:
            backend.audit("relay.refused", clean_text(pr, 80), {"by": clean_text(pod, 80), "status": err.status, "why": clean_text(err.detail or err.reason)})
            return err.status, err.reason, None
        except plan_ledger.LedgerError as err:
            backend.audit("relay.refused", clean_text(pr, 80), {"by": clean_text(pod, 80), "status": 409, "why": clean_text(err)})
            return 409, "the ledger refused the write", None

    def _check_linked(self, pr, req, backend):
        linked = {str(i) for i in (backend.pr_issues(pr) or ())}
        if str(req.get("issue")) not in linked:
            raise Refuse(403, "pr not linked to the pod's issue")

    def _check_holder(self, pod, req, backend):
        if backend.holder_of(req.get("repo"), req.get("issue")) != pod:
            raise Refuse(403, "proof only from the pod holding the work item")

    def _write(self, command, pod, req, call, pr, cid, args, backend):
        b, head = backend, call["head"]
        sid = args.get("scenario")
        plan = b.plan(pr)
        scenario = (plan or {}).get("scenarios", {}).get(sid)
        if not scenario:
            raise Refuse(400, "the plan has no such scenario", "the plan has no scenario %s" % clean_text(sid, 80))
        if scenario.get("claim", "pr") != "pr":
            raise Refuse(400, "that scenario is claimed at the epic, not per PR")
        result = args.get("result")
        if result not in plan_ledger.RESULTS:
            raise Refuse(400, "result must be pass or fail")
        evidence = plan_ledger.clip(str(args.get("evidence") or "").strip(), plan_ledger.EVIDENCE_LIMIT)
        if not evidence:
            raise Refuse(400, "evidence is required")
        base = {"pr": pr, "scenario": sid, "plan_ref": plan["source"], "plan_hash": plan["hash"], "head": head,
                "result": result, "by": pod, "evidence": evidence, "call_id": cid}
        if command == "prove":
            self._check_holder(pod, req, b)
            entry = dict(base, kind="proof", command=plan_ledger.clip(str(args.get("command") or "")), env="relay")
        else:
            named = scenario.get("evaluator")
            if args.get("evaluator", named) != named:
                raise Refuse(400, "the scenario is judged by %s" % clean_text(named, 80))
            facts = b.pr_facts(pr)
            entries, _ = plan_ledger.read_ledger(self.state)
            senders = facts.get("notice_senders") or ()
            if not plan_ledger.known_writers(entries, pr, senders):
                raise Refuse(403, "refused: no writer of %s is known: its author must record a proof or notice first" % pr)
            authors = plan_ledger.authors_of(entries, pr, senders, facts.get("author"))
            ok, why = plan_ledger.may_record_verdict(named, pod, {named: b.evaluators(named)}, authors)
            if not ok:
                raise Refuse(403, "refused: %s" % why)
            entry = dict(base, kind="verdict", evaluator=named)

        def build(entries, seq):
            again = _from_entries(entries, pod, cid)
            if again:
                raise Replay(again)
            if command == "verdict":   # the chain may have gained a proof by this pod since the first read
                a2 = plan_ledger.authors_of(entries, pr, facts.get("notice_senders") or (), facts.get("author"))
                ok2, why2 = plan_ledger.may_record_verdict(named, pod, {named: b.evaluators(named)}, a2)
                if not ok2:
                    raise Refuse(403, "refused: %s" % why2)
            if not _same_head(first, head):   # the ONLY compare: with the head read BEFORE the flock (no network under the lock)
                raise Refuse(409, "stale head: the PR head is %s, not %s" % ((first or "unknown")[:12], head[:12]))
            return entry

        first = _bounded(lambda: b.live_head(pr), HEAD_TIMEOUT)   # the slow read is made BEFORE the flock, under a short timeout
        if isinstance(first, Refuse):
            raise first
        if first is _TIMEOUT:
            raise Refuse(504, "the PR head could not be read in time: nothing was written", "live_head timed out after %ds" % HEAD_TIMEOUT)
        stored = plan_ledger.append_checked(self.state, self.now(), build)
        b.audit("plan.%s" % entry["kind"], pr, {"scenario": sid, "result": result, "seq": stored["seq"], "by": pod,
                                               "chain": stored["hash"], "via": "relay"})
        return {"seq": stored["seq"], "hash": stored["hash"]}


class PassCut(TimeoutError):
    """A hub request outran the wall-clock deadline (DNS, connect or anything else): its thread is abandoned."""


def pass_once(executor, fetch, post, budget=None, clock=time.monotonic):
    """One pull pass. fetch() -> {"requests": [...]} ; post(id, body). Returns the number answered. A request is started only while the
    pass has used less than `budget` seconds (PASS_BUDGET); the rest stay pending at the hub for the next pass."""
    start = clock()
    budget = PASS_BUDGET if budget is None else budget
    try:
        pending = fetch() or {}
    except PassCut:
        with contextlib.suppress(Exception):
            executor.backend.audit("relay.pass_cut", None, {"error": "relay pass cut"})
        return 0
    done = 0
    for req in (pending.get("requests") or [])[:10]:
        if clock() - start >= budget:
            break
        if not isinstance(req, dict):
            continue
        rid = req.get("id")
        if not isinstance(rid, str) or not RID_RE.fullmatch(rid):   # the id goes into a URL path: only what the hub generates
            continue
        try:
            status, reason, record = executor.handle(req)
            reason = str(reason)[:REASON_MAX]
        except Exception as err:   # one bad request must not abort the batch or block later passes: answer it 500 with a fixed reason
            status, reason, record = 500, REASON_FIXED, None   # the detail stays daemon-side, never sent to the pod
            with contextlib.suppress(Exception):
                executor.backend.audit("relay.failed", None, {"id": rid, "error": clean_text(err, 160)})
        try:
            post(rid, {"status": status, "reason": reason, "record": record})
        except PassCut:   # abandoned at the deadline: record it, the daemon goes on (the request stays at the hub)
            with contextlib.suppress(Exception):
                executor.backend.audit("relay.pass_cut", None, {"id": rid, "error": "relay pass cut"})
            break
        except Exception:   # the hub could not take this answer: leave it to expire there, go on with the rest
            continue
        done += 1
    return done


def http_transport(base, token):
    """(fetch, post) over HTTPS with the daemon credential the decisions pull already uses (`token` is read by the caller, never printed)."""
    import urllib.parse
    import cloud_cli
    base = base.rstrip("/")
    parts = urllib.parse.urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc or "@" in parts.netloc:
        raise ValueError("hub url must be http(s) without user information")
    if parts.scheme == "http" and not cloud_cli._http_allowed(parts, {}):
        raise ValueError("plain http to a host that is not loopback or cluster-local is refused")

    import http.client

    inflight, inflight_lock = [], threading.Lock()

    def call(path, body=None):
        """The request in a WORKER THREAD joined at READ_DEADLINE: DNS, every connect attempt and everything else share ONE wall clock. When
        the join times out the thread is abandoned (daemon) and PassCut is raised; the request stays at the hub."""
        box = []
        with inflight_lock:
            if inflight and inflight[0].is_alive():   # ONE in-flight worker: no new thread while an abandoned one still runs
                raise PassCut("previous request still running")
            cancel = threading.Event()

            def run():
                try:
                    box.append((True, _call(path, body, cancel)))
                except BaseException as err:
                    box.append((False, err))
                finally:
                    with inflight_lock:
                        if inflight and inflight[0] is threading.current_thread():
                            inflight.clear()
            worker = threading.Thread(target=run, daemon=True)
            inflight[:] = [worker]
            worker.start()
        worker.join(READ_DEADLINE)
        if not box:
            cancel.set()   # the pass is cut: a late thread must not POST
            raise PassCut("relay pass cut")
        ok, value = box[0]
        if ok:
            return value
        raise value

    def _call(path, body=None, cancel=None):
        """One request under ONE wall-clock deadline (READ_DEADLINE) covering connect, headers, body and response: the socket timeout is the
        REMAINING time at each step, and a watchdog shuts the socket at the deadline (a hub trickling headers one byte at a time is cut)."""
        data = None if body is None else json.dumps(body).encode()
        end = time.monotonic() + READ_DEADLINE
        conn_class = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = conn_class(parts.netloc, timeout=READ_DEADLINE)

        def cut():
            sock = conn.sock
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.shutdown(2)
                with contextlib.suppress(OSError):
                    sock.close()
        watchdog = threading.Timer(READ_DEADLINE, cut)
        watchdog.daemon = True
        watchdog.start()
        try:
            conn.connect()   # DNS + connect: bounded by the timeout above
            if cancel is not None and cancel.is_set():   # the pass was cut while we waited: nothing is sent (a GET result is discarded, a POST is not made)
                raise PassCut("relay pass cut")
            conn.request("POST" if data is not None else "GET", (parts.path or "") + path, body=data,
                         headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            if conn.sock is not None:
                conn.sock.settimeout(max(0.05, end - time.monotonic()))
            resp = conn.getresponse()
            if resp.status != 200:   # a redirect is not followed (the bearer is never forwarded)
                raise OSError("hub answered %d" % resp.status)
            raw = b""
            while len(raw) <= PENDING_CAP:   # read in pieces, each under the REMAINING time
                left = end - time.monotonic()
                if left <= 0:
                    raise TimeoutError("hub answer not complete within %ds" % READ_DEADLINE)
                if conn.sock is not None:
                    conn.sock.settimeout(left)
                chunk = resp.read1(min(8192, PENDING_CAP + 1 - len(raw)))
                if not chunk:
                    break
                raw += chunk
            declared = resp.getheader("Content-Length")
            if time.monotonic() >= end and (not declared or not declared.isdigit() or len(raw) != int(declared)):
                raise TimeoutError("hub answer not complete within %ds" % READ_DEADLINE)
            if declared and declared.isdigit() and len(raw) != int(declared) and len(raw) <= PENDING_CAP:
                raise ValueError("hub answer shorter than its Content-Length")
            if len(raw) > PENDING_CAP:
                raise ValueError("hub answer over %d bytes" % PENDING_CAP)
            return cloud_relay.loads_strict(raw or b"{}")
        except TimeoutError:
            raise
        except (OSError, http.client.HTTPException):
            if time.monotonic() >= end - 0.01:
                raise TimeoutError("hub answer not complete within %ds" % READ_DEADLINE) from None
            raise
        finally:
            watchdog.cancel()
            conn.close()
    return (lambda: call("/relay/pending")), (lambda rid, body: call("/relay/result/" + rid, body))


class SwitchboardBackend:
    """The real world, through the switchboard's own functions (`host` is the fleet-switchboard module; `parts` = (engine, state, config))."""

    def __init__(self, host, parts, runner, now, budget=None):
        self.host, self.now, self.budget, self.pod = host, now, budget, None
        accounts = getattr(parts[0], "accounts", None)
        accounts = accounts if all(hasattr(accounts, n) for n in ("calls", "paused", "check_pause", "record")) else None
        self.runner = MeteredRunner(runner, accounts, budget, now) if runner is not None and (accounts is not None or budget is not None) else runner
        self.base_engine, self._rest = parts[0], tuple(parts[1:])
        self.parts = self._parts(self.runner)
        self.memo = {}

    def _parts(self, runner):
        """(engine, state, config) whose gh runner is `runner`. A shallow copy of the engine with ONE attribute replaced (the shared accounts stay shared);
        the Engine has no constructor argument for its gh runner."""
        import copy
        engine = self.base_engine
        if engine is not None and isinstance(runner, MeteredRunner):
            engine = copy.copy(engine)
            engine.gh_runner = runner
        return (engine,) + self._rest

    def begin_request(self, req=None):
        """A NEW backend for ONE request: its own memo, its own metered runner (own memo) and engine copy, held by the request's calls. A thread of an
        earlier request that finishes late writes only into its own, abandoned context."""
        import copy
        view = copy.copy(self)
        view.memo = {}
        view.pod = req.get("pod") if isinstance(req, dict) else None
        if isinstance(self.runner, MeteredRunner):
            view.runner = self.runner.for_request(view.pod)
            view.parts = view._parts(view.runner)
        return view

    def gh_room(self):
        """None when the request may start, else the 429 reason (global cap or this pod's share)."""
        return None if self.budget is None else self.budget.room(self.pod)

    def _ctx(self, pr, writable=True, plan_read=True):
        """The ledger context, built ONCE per request and flag set (also a refusal it raised): plan, pr_facts and pr_issues share it."""
        import types
        key = (pr, bool(writable), bool(plan_read))
        if key not in self.memo:
            args = types.SimpleNamespace(pr=pr, plan=None, ask=None)
            try:
                self.memo[key] = (self.host._ledger_context(args, self.runner, self.now, writable, parts=self.parts, floor=not writable, plan_read=plan_read), None)
            except Exception as err:
                self.memo[key] = (None, err)
        ctx, err = self.memo[key]
        if err is not None:
            raise err
        return ctx

    def live_head(self, pr):
        """A FRESH read of the PR head (never the request's memo: the head compared under the lock is the latest one); one call in the request's allowance."""
        import types
        runner = self.runner.for_request(self.pod) if isinstance(self.runner, MeteredRunner) else self.runner
        args = types.SimpleNamespace(pr=pr, plan=None, ask=None)
        ctx = self.host._ledger_context(args, runner, self.now, True, parts=self._parts(runner), floor=False, plan_read=False)
        return (ctx.facts or {}).get("head")

    def plan(self, pr):
        ctx = self._ctx(pr)
        try:
            self.host._plan_usable(ctx)
        except self.host.LedgerCommandError as err:
            raise Refuse(400, "the plan is not usable", clean_text(err)) from None
        return {"source": ctx.source, "hash": ctx.plan["plan_hash"],
                "scenarios": {x["id"]: {"evaluator": plan_ledger.evaluator_of(x), "claim": plan_ledger.claim_of(x)}
                              for x in ctx.plan["scenarios_parsed"] if x.get("id")}}

    def pr_facts(self, pr):
        facts = self._ctx(pr).facts
        return {"author": facts.get("author"), "notice_senders": facts.get("notice_senders") or ()}

    def evaluators(self, evaluator):
        return (self.parts[2].get("plan_evaluators") or {}).get(evaluator) or []

    def holder_of(self, repo, issue):   # fail closed: only a holder the daemon's config names
        return (self.parts[2].get("relay_holders") or {}).get("%s#%s" % (repo, issue))

    def pr_issues(self, pr):
        """Issue numbers the PR is linked to: its closing keywords (Closes/Fixes/Resolves [owner/repo]#N) in the body, and its plan's issue."""
        import re
        ctx = self._ctx(pr, plan_read=False)   # the link check needs the body and the plan's SOURCE, not the plan: no plan read
        repo = ctx.repo
        found = set()
        for m in re.finditer(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+(?:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#([0-9]+)", str((ctx.facts or {}).get("body") or ""), re.I):
            if m.group(1) in (None, repo):
                found.add(int(m.group(2)))
        m = re.fullmatch(r"([^#]+)#([0-9]+)", str(ctx.source or ""))
        if m and m.group(1) == repo:
            found.add(int(m.group(2)))
        return found

    def gates(self, pr):
        ctx = self._ctx(pr, writable=False)
        return plan_ledger.evaluate_gates(ctx.plan, ctx.entries, ctx.problem, ctx.facts if ctx.facts.get("head") else None, ctx.pr, now=self.now())

    def report(self, pod, args):
        self.parts[0].report(pod, args.get("state"), args.get("text") or "", args.get("issue"), False)

    def audit(self, kind, pr, detail):
        engine = self.parts[0]
        if engine.audit is not None:
            engine.audit.append(engine.actor, kind, pr, detail)


def pass_if_configured(engine, host, runner, now, environ=os.environ, budget=None):
    """One relay pull and answer, run by the relay loop (RelayLoop.run) on its own engine. Does nothing unless FLEET_RELAY_HUB_URL and FLEET_RELAY_HUB_TOKEN_FILE are set; never raises."""
    url, tokfile = environ.get("FLEET_RELAY_HUB_URL"), environ.get("FLEET_RELAY_HUB_TOKEN_FILE")
    if not url or not tokfile:
        return 0
    try:
        try:
            try:   # NONBLOCK: a FIFO cannot hang the daemon; NOFOLLOW: a symlink is refused; fstat BEFORE any read
                fd = os.open(tokfile, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            except OSError as err:
                if err.errno != errno.ELOOP:
                    raise   # the outer handler records only the class name
                engine._note("relay.pass_failed", None, {"error": "the token file is a symbolic link: it is not used"})
                return 0
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):   # before fdopen: a directory would raise IsADirectoryError there
                    engine._note("relay.pass_failed", None, {"error": "the token file is a directory / not a regular file: it is not used"})
                    return 0
            except BaseException:
                os.close(fd)
                raise
            with os.fdopen(fd, "rb") as handle:
                loose = info.st_mode & 0o077 or info.st_uid != os.geteuid()
                raw = handle.read(TOKEN_CAP + 1) if not loose else b""
            if loose:   # not a regular file, or readable by group or other: refused, never read or used
                engine._note("relay.pass_failed", None, {"error": "the token file is not private to this user (regular file, owned by the user, mode 0600): it is not used"})
                return 0
            if len(raw) > TOKEN_CAP:
                engine._note("relay.pass_failed", None, {"error": "token file over %d bytes" % TOKEN_CAP})
                return 0
            token = raw.decode().strip()
        except Exception as err:   # only the class name: the message may carry the file's path or content
            engine._note("relay.pass_failed", None, {"error": type(err).__name__})
            return 0
        fetch, post = http_transport(url, token)
        backend = SwitchboardBackend(host, (engine, engine.state_dir, engine.config), runner, now, budget)
        return pass_once(Executor(engine.state_dir, backend, now), fetch, post)
    except Exception as err:
        engine._note("relay.pass_failed", None, {"error": " ".join(str(err).split())[:160]})
        return 0


CADENCE = 5.0   # seconds between relay checks, on the relay's own loop (not tied to the engine pass)


class RelayLoop:
    """Pulls and answers relay requests on their own short cadence, in one daemon thread, so a long engine pass
    cannot make the hub's wait run out. One thread means one in-flight pass; each pass keeps its own total
    deadline and PASS_BUDGET (pass_once). The ledger flock is taken by the Executor as before.

    CONCURRENCY RULE: the Engine is not thread-safe (discovery, the V2Reads caches, per-pass counters, _last). This thread
    never touches the pass thread's engine: `engine_factory` builds the loop's OWN Engine (same config and state dir, own
    caches), in the thread that starts the loop. What the two engines share is only files, each under its own lock (the
    ledger flock, the audit and report-rider file locks). The factory is REQUIRED (ValueError at start); a test that wants sharing says so: engine_factory=lambda: engine."""

    def __init__(self, engine, host, runner, now, environ=os.environ, cadence=None, sleep=None, engine_factory=None):
        if engine_factory is None:
            raise ValueError("the relay loop needs an engine factory (its own Engine); sharing the pass engine must be explicit: engine_factory=lambda: engine")
        self.engine = engine_factory()
        self.budget = RelayBudget(now)   # one hourly cap for every pass of this loop
        self.args = (self.engine, host, runner, now, environ, self.budget)
        shared = getattr(engine, "accounts", None)   # ONE gate: the relay's reads are counted and paused in the pass engine's GhAccounts
        if self.engine is not engine and self.engine is not None and shared is not None and hasattr(shared, "calls"):
            self.engine.accounts = shared
            if getattr(self.engine, "intents", None) is not None:
                self.engine.intents.accounts = shared
        self.cadence = CADENCE if cadence is None else cadence
        self.stop_flag = threading.Event()
        self.sleep = sleep or self.stop_flag.wait
        self.thread = None
        self.failures = 0

    def _record_failure(self, err):
        """Counts the failure, and audits it if it can; never raises (the audit append itself may be what failed)."""
        self.failures += 1
        try:
            self.engine._note("relay.loop_failed", None, {"error": type(err).__name__})
        except BaseException:
            pass

    def run(self):
        while not self.stop_flag.is_set():
            try:
                pass_if_configured(*self.args)   # never raises, but its own handler calls _note, which can
            except Exception as err:
                self._record_failure(err)
            if self.stop_flag.is_set():
                break
            self.sleep(self.cadence)

    def stop(self, join=2.0):
        """Stops the loop and waits `join` s. A pull can last about 90 s: if the thread is still alive an audit line says so, and
        the late thread can still write (a ledger append is flock-atomic; a pass cut by its deadline posts nothing).
        Nothing needs closing: the second engine holds no open file or socket (AuditLog opens, appends and closes per call; V2Client and
        GhAccounts only run subprocesses per call), so stopping the thread is the whole teardown."""
        self.stop_flag.set()
        if self.thread is not None:
            self.thread.join(join)
            if self.thread.is_alive():
                try:
                    self.engine._note("relay.stop_timeout", None, {"error": "the relay thread was still running %g s after stop" % join})
                except BaseException:
                    pass


def start_loop(engine, host, runner, now, environ=os.environ, **kw):
    """Start the relay loop; None (nothing runs) unless FLEET_RELAY_HUB_URL and FLEET_RELAY_HUB_TOKEN_FILE are set.
    Raises ValueError when there is a hub but no host module (a None host would fail silently in every pass)."""
    if not environ.get("FLEET_RELAY_HUB_URL") or not environ.get("FLEET_RELAY_HUB_TOKEN_FILE"):
        return None
    if host is None:
        raise ValueError("the relay loop needs the switchboard module as its host")
    if kw.get("engine_factory") is None:
        raise ValueError("the relay loop needs an engine factory (its own Engine) when a hub is configured")
    loop = RelayLoop(engine, host, runner, now, environ, **kw)
    loop.thread = threading.Thread(target=loop.run, name="relay-loop", daemon=True)
    loop.thread.start()
    return loop
