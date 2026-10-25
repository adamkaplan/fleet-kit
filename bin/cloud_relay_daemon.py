#!/usr/bin/env python3
"""cloud_relay_daemon: the Mac-daemon half of the cloud hub relay (#1225, slice B).

The daemon PULLS `GET /relay/pending` from the hub, runs each request with the same rule code as the local commands (plan_ledger's
`may_record_verdict`, `authors_of`, `known_writers`, `evaluate_gates`; the switchboard's report), with `by` = the pod name the hub
resolved, and answers `POST /relay/result/<id>`. Every refusal writes nothing to the ledger (byte-identical) and leaves one audit line.

The world (GitHub, the plan, the registry, the audit) is a Backend, so the rules are testable with a fake; `SwitchboardBackend` is the real one.
One ledger entry per prove or verdict, written by `plan_ledger.append_checked` under its flock; the head is re-read from GitHub INSIDE
that decision. Replay memory: `relay_calls.json` (pod + call id -> seq, hash), backed by the call id stored in the entry itself.

Report delivery is AT-MOST-ONCE per call id: a report can be LOST (never duplicated); for `report` the ask's "exactly one" reads "at most one". A lost report is
told to the pod by name (409 "lost or still pending: resend with a NEW call_id"). Report delivery, in detail: the call id is CLAIMED first (a PENDING record, get-or-claim in one
step under the index flock), then delivered, then marked DONE. A replay of a done id answers 200 "replay: already reported"; a replay of a
PENDING id (a crash, or a delivery that raised, between claim and done) is resolved deliberately: it answers that named 409 and
NEVER delivers a second time (the first delivery may have happened; the owner sees the audit line `report.claimed` and may resend under a new
call id). Claims live in their own file (`relay_reports.json`, REPORT_MAX entries, so a flood of prove/verdict entries cannot evict them; a
damaged claim file refuses with 500 rather than forgetting). A report replay with a different pr or different args under the same call id is 409.
Refusals (daemon-only credential, repo, allow-list, rules) write an AUDIT line `relay.refused` on purpose and leave the ledger and files
byte-identical. The hub's 1000-entry finished cache can be flooded to evict another pod's hub-side replay record; the daemon-side dedupe
above still holds it. Every hub request (connect, headers, body, response; GET and POST) has ONE wall-clock deadline (READ_DEADLINE 30 s), and a relay pass a budget (PASS_BUDGET 60 s: later requests wait for the next pass); worst case for the transport of a pass is PASS_BUDGET + READ_DEADLINE = 90 s, plus the GitHub reads of the request being handled. The hub read has a TOTAL deadline (READ_DEADLINE) and the live head is read OUTSIDE the ledger flock (bounded by HEAD_TIMEOUT, refused 504 with nothing written when late); inside the flock the build only compares it with the head the request judged: no network under the lock.
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
    """fn() with a deadline: _TIMEOUT when it does not answer in time, None when it raises (an unknown head, refused as stale)."""
    box = []

    def run():
        try:
            box.append(fn())
        except Exception:
            box.append(None)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return box[0] if box else _TIMEOUT


def _answer(known):
    return {"seq": known["seq"], "hash": known["hash"]}


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
            if command in NEEDS_LINK:   # the PR must be the one linked to the issue this pod is bound to
                self._check_linked(pr, req)
            if command == "gates":   # read-only and not indexed: nothing to replay
                out = self.backend.gates(pr)
                self.backend.audit("gates", pr, {"by": clean_text(pod, 80), "call_id": clean_text(cid, 64)})
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
                    self.backend.audit("report.claimed", pr, {"by": clean_text(pod, 80), "call_id": clean_text(cid, 64)})
                    raise Refuse(409, PENDING_REASON, "report replay of a pending claim")
                # only the state and the text come from the pod; the issue is the hub's, and `everything` is dropped
                self.backend.report(pod, {"state": args.get("state"), "text": clean_text(args.get("text")), "issue": req.get("issue")})
                self.reports.put(pod, cid, dict(mine, state="done"))
                return 200, "reported", None
            known = self.index.get(pod, cid)
            if known:
                if not _same_call(known, command, pr, call["head"], args_digest(args)):
                    raise Refuse(409, "call_id reused for a different call")
                return 200, "replay: already recorded", _answer(known)
            record = self._write(command, pod, req, call, pr, cid, args)
            self.index.put(pod, cid, dict(record, command=command, pr=pr, head=call["head"], args=args_digest(args)))
            return 200, "recorded", record
        except Replay as rep:
            first = rep.record   # rebuilt from the ledger entry: no digest, so compare what the entry holds
            clipped = plan_ledger.clip(str(args.get("evidence") or "").strip(), plan_ledger.EVIDENCE_LIMIT)
            if (not _same_call(first, command, pr, call.get("head")) or first.get("scenario") != args.get("scenario")
                    or first.get("result") != args.get("result") or first.get("evidence") != clipped):
                self.backend.audit("relay.refused", clean_text(pr, 80), {"by": clean_text(pod, 80), "status": 409, "why": "call_id reused for a different call"})
                return 409, "call_id reused for a different call", None
            self.index.put(pod, cid, dict(_answer(first), command=command, pr=pr, head=call.get("head"), args=args_digest(args)))
            return 200, "replay: already recorded", _answer(first)
        except Refuse as err:
            self.backend.audit("relay.refused", clean_text(pr, 80), {"by": clean_text(pod, 80), "status": err.status, "why": clean_text(err.detail or err.reason)})
            return err.status, err.reason, None
        except plan_ledger.LedgerError as err:
            self.backend.audit("relay.refused", clean_text(pr, 80), {"by": clean_text(pod, 80), "status": 409, "why": clean_text(err)})
            return 409, "the ledger refused the write", None

    def _check_linked(self, pr, req):
        linked = {str(i) for i in (self.backend.pr_issues(pr) or ())}
        if str(req.get("issue")) not in linked:
            raise Refuse(403, "pr not linked to the pod's issue")

    def _check_holder(self, pod, req):
        if self.backend.holder_of(req.get("repo"), req.get("issue")) != pod:
            raise Refuse(403, "proof only from the pod holding the work item")

    def _write(self, command, pod, req, call, pr, cid, args):
        b, head = self.backend, call["head"]
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
            self._check_holder(pod, req)
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
        if first is _TIMEOUT:
            raise Refuse(504, "the PR head could not be read in time: nothing was written", "live_head timed out after %ds" % HEAD_TIMEOUT)
        stored = plan_ledger.append_checked(self.state, self.now(), build)
        b.audit("plan.%s" % entry["kind"], pr, {"scenario": sid, "result": result, "seq": stored["seq"], "by": pod,
                                               "chain": stored["hash"], "via": "relay"})
        return {"seq": stored["seq"], "hash": stored["hash"]}


def pass_once(executor, fetch, post, budget=None, clock=time.monotonic):
    """One pull pass. fetch() -> {"requests": [...]} ; post(id, body). Returns the number answered. A request is started only while the
    pass has used less than `budget` seconds (PASS_BUDGET); the rest stay pending at the hub for the next pass."""
    start = clock()
    budget = PASS_BUDGET if budget is None else budget
    pending = fetch() or {}
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

    def call(path, body=None):
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
            conn.request("POST" if data is not None else "GET", (parts.path or "") + path, body=data,
                         headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})   # connect happens here: bounded by the timeout above
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

    def __init__(self, host, parts, runner, now):
        self.host, self.parts, self.runner, self.now = host, parts, runner, now

    def _ctx(self, pr, writable=True):
        import types
        args = types.SimpleNamespace(pr=pr, plan=None, ask=None)
        return self.host._ledger_context(args, self.runner, self.now, writable, parts=self.parts, floor=not writable)

    def live_head(self, pr):
        return (self._ctx(pr).facts or {}).get("head")

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
        ctx = self._ctx(pr)
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


def pass_if_configured(engine, host, runner, now, environ=os.environ):
    """Called by the daemon after each pass. Does nothing unless FLEET_RELAY_HUB_URL and FLEET_RELAY_HUB_TOKEN_FILE are set; never raises."""
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
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                loose = info.st_mode & 0o077 or info.st_uid != os.geteuid() or not stat.S_ISREG(info.st_mode)
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
        backend = SwitchboardBackend(host, (engine, engine.state_dir, engine.config), runner, now)
        return pass_once(Executor(engine.state_dir, backend, now), fetch, post)
    except Exception as err:
        engine._note("relay.pass_failed", None, {"error": " ".join(str(err).split())[:160]})
        return 0
