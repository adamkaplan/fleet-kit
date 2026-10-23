#!/usr/bin/env python3
"""cloud_relay_daemon: the Mac-daemon half of the cloud hub relay (#1225, slice B).

The daemon PULLS `GET /relay/pending` from the hub, runs each request with the same rule code as the local commands (plan_ledger's
`may_record_verdict`, `authors_of`, `known_writers`, `evaluate_gates`; the switchboard's report), with `by` = the pod name the hub
resolved, and answers `POST /relay/result/<id>`. Every refusal writes nothing to the ledger (byte-identical) and leaves one audit line.

The world (GitHub, the plan, the registry, the audit) is a Backend, so the rules are testable with a fake; `SwitchboardBackend` is the real one.
One ledger entry per prove or verdict, written by `plan_ledger.append_checked` under its flock; the head is re-read from GitHub INSIDE
that decision. Replay memory: `relay_calls.json` (pod + call id -> seq, hash), backed by the call id stored in the entry itself.
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plan_ledger  # noqa: E402

INDEX_FILE = "relay_calls.json"
COMMANDS = ("report", "prove", "verdict", "gates")
PR_RE = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)$")
CALL_ID_RE = re.compile(r"^[\x21-\x7e]{1,64}$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


class Refuse(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status, self.reason = status, reason


class Replay(Exception):
    def __init__(self, record):
        super().__init__("replay")
        self.record = record


def _same_head(known, given):
    return bool(known) and bool(given) and (known.startswith(given) or given.startswith(known))


class Index:
    """pod + call id -> {seq, hash}. A separate daemon-side file; a missing or damaged file falls back to the entries' own call ids."""

    def __init__(self, state_dir):
        self.path = Path(state_dir) / INDEX_FILE

    def _read(self):
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, pod, call_id):
        return self._read().get("%s\n%s" % (pod, call_id))

    def put(self, pod, call_id, record):
        data = self._read()
        data["%s\n%s" % (pod, call_id)] = record
        tmp = self.path.with_name(INDEX_FILE + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(data, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(self.path))


def _from_entries(entries, pod, call_id):
    for e in entries:
        if e.get("by") == pod and e.get("call_id") == call_id:
            return {"seq": e["seq"], "hash": e["hash"]}
    return None


class Executor:
    """backend needs: live_head(pr), plan(pr) -> {"source","hash","scenarios":{sid:{"evaluator","claim"}}},
    pr_facts(pr) -> {"author","notice_senders"}, evaluators(evaluator), holder_of(repo, issue), gates(pr), report(pod, args), audit(kind, pr, detail)."""

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
            if not isinstance(cid, str) or not CALL_ID_RE.match(cid):
                raise Refuse(400, "call_id must be printable ASCII, 1 to 64 characters")
            command = call.get("command")
            if command not in COMMANDS:
                raise Refuse(400, "command %r is not one of %s" % (command, ", ".join(COMMANDS)))
            m = PR_RE.match(pr)
            if not m:
                raise Refuse(400, "pr must be owner/repo#N")
            if m.group(1) != req.get("repo"):
                raise Refuse(403, "pr is not in the pod's bound repo")
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            if command in ("prove", "verdict") and not SHA_RE.match(str(call.get("head") or "")):
                raise Refuse(400, "%s needs the head sha it judged" % command)
            known = self.index.get(pod, cid)
            if known and command in ("prove", "verdict"):
                return 200, "replay: already recorded", known
            if command == "gates":
                out = self.backend.gates(pr)
                self.backend.audit("gates", pr, {"by": pod, "call_id": cid})
                self.index.put(pod, cid, {"seq": None, "hash": None})
                return 200, json.dumps(out, sort_keys=True), None
            if command == "report":
                if known:
                    return 200, "replay: already reported", known
                self.backend.report(pod, args)
                self.index.put(pod, cid, {"seq": None, "hash": None})
                return 200, "reported", None
            record = self._write(command, pod, req, call, pr, cid, args)
            self.index.put(pod, cid, record)
            return 200, "recorded", record
        except Replay as rep:
            self.index.put(pod, cid, rep.record)
            return 200, "replay: already recorded", rep.record
        except Refuse as err:
            self.backend.audit("relay.refused", pr, {"by": pod, "status": err.status, "why": err.reason})
            return err.status, err.reason, None
        except plan_ledger.LedgerError as err:
            self.backend.audit("relay.refused", pr, {"by": pod, "status": 409, "why": str(err)})
            return 409, str(err), None

    def _write(self, command, pod, req, call, pr, cid, args):
        b, head = self.backend, call["head"]
        sid = args.get("scenario")
        plan = b.plan(pr)
        scenario = (plan or {}).get("scenarios", {}).get(sid)
        if not scenario:
            raise Refuse(400, "the plan has no scenario %s" % sid)
        if scenario.get("claim", "pr") != "pr":
            raise Refuse(400, "%s is claimed at the epic, not per PR" % sid)
        result = args.get("result")
        if result not in plan_ledger.RESULTS:
            raise Refuse(400, "result must be pass or fail")
        evidence = plan_ledger.clip(str(args.get("evidence") or "").strip(), plan_ledger.EVIDENCE_LIMIT)
        if not evidence:
            raise Refuse(400, "evidence is required")
        base = {"pr": pr, "scenario": sid, "plan_ref": plan["source"], "plan_hash": plan["hash"], "head": head,
                "result": result, "by": pod, "evidence": evidence, "call_id": cid}
        if command == "prove":
            if b.holder_of(req.get("repo"), req.get("issue")) != pod:
                raise Refuse(403, "proof only from the pod holding the work item")
            entry = dict(base, kind="proof", command=plan_ledger.clip(str(args.get("command") or "")), env="relay")
        else:
            named = scenario.get("evaluator")
            if args.get("evaluator", named) != named:
                raise Refuse(400, "scenario %s is judged by %s" % (sid, named))
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
        rid = req.get("id")
        if not isinstance(rid, str) or not rid:
            continue
        status, reason, record = executor.handle(req)
        post(rid, {"status": status, "reason": reason, "record": record})
        done += 1
    return done


def http_transport(base, token):
    """(fetch, post) over HTTPS with the daemon credential the decisions pull already uses (`token` is read by the caller, never printed)."""
    import urllib.request
    base = base.rstrip("/")

    def call(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, method="POST" if body is not None else "GET",
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read() or b"{}")
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
            raise Refuse(400, str(err)) from None
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

    def gates(self, pr):
        ctx = self._ctx(pr, writable=False)
        return plan_ledger.evaluate_gates(ctx.plan, ctx.entries, ctx.problem, ctx.facts if ctx.facts.get("head") else None, ctx.pr, now=self.now())

    def report(self, pod, args):
        self.parts[0].report(pod, args.get("state"), args.get("text") or "", args.get("issue"), bool(args.get("everything")))

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
        token = Path(tokfile).read_text().strip()
        fetch, post = http_transport(url, token)
        backend = SwitchboardBackend(host, (engine, engine.state_dir, engine.config), runner, now)
        return pass_once(Executor(engine.state_dir, backend, now), fetch, post)
    except Exception as err:
        engine._note("relay.pass_failed", None, {"error": " ".join(str(err).split())[:160]})
        return 0
