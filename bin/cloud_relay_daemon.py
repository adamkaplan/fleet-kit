#!/usr/bin/env python3
"""cloud_relay_daemon: the Mac-daemon half of the cloud hub relay (#1225, slice B).

The daemon PULLS `GET /relay/pending` from the hub, runs each request with the same rule code as the local commands (plan_ledger's
`may_record_verdict`, `authors_of`, `known_writers`, `evaluate_gates`; the switchboard's report), with `by` = the pod name the hub
resolved, and answers `POST /relay/result/<id>`. Every refusal writes nothing to the ledger (byte-identical) and leaves one audit line.

The world (GitHub, the plan, the registry, the audit) is a Backend, so the rules are testable with a fake; `SwitchboardBackend` is the real one.
One ledger entry per prove or verdict, written by `plan_ledger.append_checked` under its flock; the head is re-read from GitHub INSIDE
that decision. Replay memory: `relay_calls.json` (pod + call id -> seq, hash), backed by the call id stored in the entry itself.

Report delivery is AT-LEAST-ONCE: the report is delivered, then the index record is written; a crash between the two delivers it again on
replay. A report replay with a different pr or different args under the same call id is 409.

The PR-to-issue link comes from the PR body's closing keywords (and the plan's issue). It is NOT a security boundary: anyone who can edit
the PR body can set it. The boundaries are the prove holder check (`relay_holders`) and the verdict evaluator registry (`plan_evaluators`).

Pod evidence text (prove/verdict args) is stored in the ledger UNSCRUBBED: the ledger is not secret-safe, so pods must never put secrets in it.
"""
import contextlib
import hashlib
import fcntl
import json
import os
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plan_ledger  # noqa: E402
import cloud_relay  # noqa: E402  (the one definition of the wire shapes, shared with the hub)

INDEX_FILE = "relay_calls.json"
INDEX_MAX = 5000   # entries kept; the oldest are pruned (the ledger's own call ids still answer a pruned replay)
COMMANDS = cloud_relay.COMMANDS
NEEDS_HEAD = cloud_relay.NEEDS_HEAD
NEEDS_LINK = cloud_relay.NEEDS_LINK
PENDING_CAP = 4 * 1024 * 1024   # bytes of the hub's pending list; larger is refused
TOKEN_CAP = 4096                # bytes of the token file
REASON_FIXED = "relay failed"
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
    with a unique temp name and pruned to INDEX_MAX (oldest first); a missing or damaged file falls back to the entries' own call ids."""

    def __init__(self, state_dir):
        self.path = Path(state_dir) / INDEX_FILE

    def _read(self):
        try:
            with open(str(self.path), "rb") as handle:
                raw = handle.read(INDEX_READ_CAP + 1)
            if len(raw) > INDEX_READ_CAP:
                return {}
            data = json.loads(raw.decode())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, pod, call_id):
        return self._read().get("%s\n%s" % (pod, call_id))

    def put(self, pod, call_id, record):
        lock = self.path.with_name(INDEX_FILE + ".lock")
        with os.fdopen(os.open(str(lock), os.O_WRONLY | os.O_CREAT, 0o600), "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            data = self._read()
            key = "%s\n%s" % (pod, call_id)
            data.pop(key, None)
            data[key] = record     # json keeps insertion order: the newest is last
            while len(data) > INDEX_MAX:
                data.pop(next(iter(data)))
            tmp = self.path.with_name("%s.%d.%s.tmp" % (INDEX_FILE, os.getpid(), uuid.uuid4().hex))
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


def _answer(known):
    return {"seq": known["seq"], "hash": known["hash"]}


class Executor:
    """backend needs: live_head(pr), plan(pr) -> {"source","hash","scenarios":{sid:{"evaluator","claim"}}},
    pr_issues(pr) -> {issue numbers the PR is linked to}, pr_facts(pr) -> {"author","notice_senders"}, evaluators(evaluator), holder_of(repo, issue), gates(pr), report(pod, args), audit(kind, pr, detail)."""

    def __init__(self, state_dir, backend, now):
        self.state, self.backend, self.now = state_dir, backend, now
        self.index = Index(state_dir)

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
                self.backend.audit("gates", pr, {"by": pod, "call_id": cid})
                text = json.dumps(out, sort_keys=True)
                if len(text) > REASON_MAX:   # a clipped JSON would be unreadable: send a short valid one
                    text = json.dumps({"ready": out.get("ready") if isinstance(out, dict) else None, "truncated": True})
                return 200, text, None
            if command == "report":   # delivered once per call id; kept under its own key ("report\n...", no seq or hash), so it never answers a prove or verdict
                seen = self.index.get("report\n" + pod, cid)
                digest = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()
                if seen:
                    if seen.get("pr") != pr or seen.get("args") != digest:
                        raise Refuse(409, "call_id reused for a different call")
                    return 200, "replay: already reported", None
                # only the state and the text come from the pod; the issue is the hub's, and `everything` is dropped
                self.backend.report(pod, {"state": args.get("state"), "text": clean_text(args.get("text")), "issue": req.get("issue")})
                self.index.put("report\n" + pod, cid, {"command": "report", "pr": pr, "args": digest})
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
                self.backend.audit("relay.refused", pr, {"by": pod, "status": 409, "why": "call_id reused for a different call"})
                return 409, "call_id reused for a different call", None
            self.index.put(pod, cid, dict(_answer(first), command=command, pr=pr, head=call.get("head"), args=args_digest(args)))
            return 200, "replay: already recorded", _answer(first)
        except Refuse as err:
            self.backend.audit("relay.refused", pr, {"by": pod, "status": err.status, "why": clean_text(err.detail or err.reason)})
            return err.status, err.reason, None
        except plan_ledger.LedgerError as err:
            self.backend.audit("relay.refused", pr, {"by": pod, "status": 409, "why": clean_text(err)})
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
            live = b.live_head(pr)   # re-read from GitHub inside the append decision
            if not _same_head(live, head):
                raise Refuse(409, "stale head: the PR head is %s, not %s" % ((live or "unknown")[:12], head[:12]))
            return entry

        stored = plan_ledger.append_checked(self.state, self.now(), build)
        b.audit("plan.%s" % entry["kind"], pr, {"scenario": sid, "result": result, "seq": stored["seq"], "by": pod,
                                               "chain": stored["hash"], "via": "relay"})
        return {"seq": stored["seq"], "hash": stored["hash"]}


def pass_once(executor, fetch, post):
    """One pull pass. fetch() -> {"requests": [...]} ; post(id, body). Returns the number answered."""
    pending = fetch() or {}
    done = 0
    for req in (pending.get("requests") or [])[:10]:
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
    import urllib.error
    import urllib.parse
    import urllib.request
    import cloud_cli
    base = base.rstrip("/")
    parts = urllib.parse.urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc or "@" in parts.netloc:
        raise ValueError("hub url must be http(s) without user information")
    if parts.scheme == "http" and not cloud_cli._http_allowed(parts, {}):
        raise ValueError("plain http to a host that is not loopback or cluster-local is refused")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):   # the bearer is never forwarded
            return None
    opener = urllib.request.build_opener(NoRedirect)

    def call(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, method="POST" if body is not None else "GET",
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with opener.open(req, timeout=20) as resp:
            raw = resp.read(PENDING_CAP + 1)
            if len(raw) > PENDING_CAP:
                raise ValueError("hub answer over %d bytes" % PENDING_CAP)
            return json.loads(raw or b"{}")
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
            with open(tokfile, "rb") as handle:
                raw = handle.read(TOKEN_CAP + 1)
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
