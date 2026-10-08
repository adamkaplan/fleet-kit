"""decision_sync: the sync engine between the Switchboard's Decision list and an external decision system.

OFF BY DEFAULT. Nothing here runs, and no adapter is called, unless `external_decisions.enabled` is true and the
repo is on the per-repo list. The list stays the Switchboard's own: this engine only mirrors it outward and takes
answers back, and a down adapter never hides a local decision or blocks local answering.

  - the link table (external-links.json): decision key <-> outside id, revision, what was last said, the pull
    cursor and the applied event ids. Disposable: reconcile() rebuilds it by asking the adapter to look each
    decision up by its key.
  - the outbox (external-outbox.json): durable outbound operations (raise, revise, status, withdraw), at-least-once
    with idempotency keys, retried with backoff, in order per decision. Sized for growth: a depth alarm, never a drop.
  - inbound: answers, clarifications and reversals pulled by cursor, applied once (event ids), by the authority of
    the role that answered.

The rules (keys, mapping, planning, authority) are decision_core's, pure. This module holds the files and the calls,
and reaches the rest of the Switchboard through the `host` it is given: audit, answer delivery, clock.

Python 3.9+, standard library only. Loaded by path from beside bin/fleet-switchboard.
"""

import json
import os
import time

import decision_core as core

LINKS_FILE = "external-links.json"
OUTBOX_FILE = "external-outbox.json"
STATUS_FILE = "external-status.json"
APPLIED_LIMIT = 5000            # event ids remembered; the cursor already bounds what is pulled again
WITHDRAWN_KEEP_SECONDS = 7 * 86400   # a withdrawn link is kept this long so a late event is known as closed
PASS_OP_LIMIT = 100             # outbound operations tried in one cycle: the rest wait for the next
JUDGE_MAX_TRIES = 5             # an answer the judge could not be asked about is retried this often, then not applied


class Host:
    """What the sync engine needs from the Switchboard. The script gives the real one; tests give a fake."""

    def audit(self, event, subject, detail):
        """Record one event in the audit log (no secrets)."""

    def judge_answer(self, entry, intent):
        """The owner's standing orders, consulted for an answer by the owner's agent: {"verdict": "allow" | "deny" |
        "unavailable", "reason": one line}. The default allows: the script's host judges. Only ever called for an
        answer that role authority already accepts: a judge can tighten and never loosen."""
        return {"verdict": "allow", "reason": "no judge"}

    def deliver_answer(self, entry, intent):
        """Deliver an applied answer for the list entry to the asker by its own path. Raises when it cannot."""

    def deliver_note(self, target, text):
        """Tell the raising agent of `target` (a list entry or a link) something, as a note. Raises when it cannot."""


def _read_json(path):
    try:
        with open(path) as handle:
            body = json.load(handle)
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _write_json(path, body):
    temp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(body, handle, indent=1, sort_keys=True)
            handle.write("\n")
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def backoff_seconds(attempts, base, ceiling):
    return min(ceiling, base * (2 ** max(0, attempts - 1)))


class SyncEngine:
    def __init__(self, settings, state_dir, adapter, host, now=time.time):
        """`settings` is the validated `external_decisions` section: repos, tier_map, kinds, poll_seconds,
        outbox_alarm_depth, retry_base_seconds, retry_max_seconds."""
        self.settings, self.state_dir, self.adapter, self.host, self.now = settings, str(state_dir), adapter, host, now
        self.links_path = os.path.join(self.state_dir, LINKS_FILE)
        self.outbox_path = os.path.join(self.state_dir, OUTBOX_FILE)
        self.status_path = os.path.join(self.state_dir, STATUS_FILE)
        self.links, self.cursor, self.applied, self.last_pull = {}, None, [], None
        self.parked = {}   # event id -> {"event", "tries", "next_at"}: answers waiting for a judge that could not be asked
        self.ops, self.seq = [], 0
        self.rebuild = False
        self._load()

    # -- the files

    def _load(self):
        body = _read_json(self.links_path)
        if body is None or not isinstance(body.get("links"), dict):
            self.rebuild = True   # missing or unreadable: rebuilt from the adapter, never trusted
        else:
            self.links = body["links"]
            self.cursor, self.last_pull = body.get("cursor"), body.get("last_pull")
            self.applied = [e for e in body.get("applied", []) if isinstance(e, str)]
            parked = body.get("parked")
            self.parked = parked if isinstance(parked, dict) else {}
        box = _read_json(self.outbox_path)
        if box is not None and isinstance(box.get("ops"), list):
            self.ops, self.seq = box["ops"], box.get("seq", 0)

    def _save(self):
        now = self.now()
        self.links = {k: l for k, l in self.links.items()
                      if not (l.get("withdrawn") and now - l.get("withdrawn_at", now) > WITHDRAWN_KEEP_SECONDS)
                      and not (l.get("closed") and now - l.get("closed_at", now) > WITHDRAWN_KEEP_SECONDS)}
        _write_json(self.links_path, {"version": 1, "links": self.links, "cursor": self.cursor,
                                      "last_pull": self.last_pull, "applied": self.applied[-APPLIED_LIMIT:],
                                      "parked": self.parked})
        _write_json(self.outbox_path, {"version": 1, "seq": self.seq, "ops": self.ops})

    # -- what is wanted outside

    def desired(self, decisions, seeds):
        """{key: want} for the listed decisions that go outward. `seeds` maps a display id to its source seed."""
        out, repos, kinds = {}, self.settings["repos"], self.settings["kinds"]
        for entry in decisions:
            seed = seeds.get(entry.get("id"))
            if seed is None or not core.goes_outward(entry, repos, kinds):
                continue
            floor = self.settings["tier_map"].get(entry.get("tier"))
            if floor is None:
                continue
            payload = core.outward_payload(entry, floor)
            thread = (entry.get("repo"), entry.get("ask"), entry.get("agent")) if entry.get("ask") else None
            out[core.decision_key(seed)] = {"payload": payload, "digest": core.payload_digest(payload),
                                            "tier": entry.get("tier"), "floor": floor, "kind": entry.get("kind"),
                                            "thread": list(thread) if thread else None, "entry": entry}
        return out

    def _projected(self):
        """The links as they will be once every queued operation has been applied."""
        projected = {k: dict(v) for k, v in self.links.items()}
        for op in self.ops:
            self._apply_to(projected, op, None)
        return projected

    def _apply_to(self, links, op, external_id):
        key, kind = op["key"], op["op"]
        link = links.get(op.get("rebind_from")) if op.get("rebind_from") in links else links.get(key)
        if kind == "raise":
            old = links.get(key) or {}
            links[key] = dict(old, external_id=external_id or old.get("external_id"), revision=op["revision"],
                              digest=op["digest"], tier=op["tier"], floor=op["floor"], kind=op["kind"],
                              thread=op["thread"], repo=op["decision"].get("repo"), ask=op["decision"].get("ask"),
                              agent=op["decision"].get("agent"), display_id=op["decision"].get("display_id"),
                              withdrawn=False, closed=False)
        elif kind == "revise":
            if op.get("rebind_from") in links:
                links[key] = links.pop(op["rebind_from"])
            link = links.setdefault(key, {})
            link.update(revision=op["revision"], digest=op["digest"], tier=op["tier"], floor=op["floor"],
                        thread=op["thread"], agent=op["decision"].get("agent"),
                        display_id=op["decision"].get("display_id"))
            if external_id:
                link["external_id"] = external_id
        elif kind == "status" and link is not None:
            link.update(tier=op["tier"], floor=op["floor"], status_seq=op.get("seq", link.get("status_seq", 0)))
        elif kind == "withdraw" and link is not None:
            link.update(withdrawn=True, withdrawn_at=self.now(), revision=op["revision"])

    def _enqueue(self, planned, desired):
        for item in planned:
            key, kind = item["key"], item["op"]
            want = desired.get(key)
            link = self._projected().get(item.get("rebind_from") or key) or {}
            label = "status:%s:%d" % (want["floor"], item["seq"]) if kind == "status" else kind
            op = dict(item, id=core.idempotency_key(key, label, item["revision"]), attempts=0, next_at=0,
                      last_error=None, created=self.now())
            if want is not None:
                op.update(decision=want["payload"], digest=want["digest"], tier=want["tier"], floor=want["floor"],
                          kind=want["kind"], thread=want["thread"])
            else:
                op.update(decision={"display_id": link.get("display_id")}, floor=link.get("floor"),
                          tier=link.get("tier"))
            if any(o["id"] == op["id"] for o in self.ops):
                continue
            self.seq += 1
            op["seq_no"] = self.seq
            self.ops.append(op)

    def note_back(self, key, text, tag):
        """Queue a status note about a decision (a refused answer), once per `tag`."""
        link = self.links.get(key)
        if link is None or link.get("withdrawn"):
            return
        op = {"op": "status", "key": key, "revision": link["revision"], "seq": link.get("status_seq", 0),
              "id": core.idempotency_key(key, "note:" + tag, link["revision"]), "attempts": 0, "next_at": 0,
              "last_error": None, "created": self.now(), "note": text, "floor": link.get("floor"),
              "tier": link.get("tier"), "decision": {"display_id": link.get("display_id")}, "note_only": True}
        if not any(o["id"] == op["id"] for o in self.ops):
            self.seq += 1
            op["seq_no"] = self.seq
            self.ops.append(op)

    # -- the cycle

    def cycle(self, decisions, errors, seeds):
        """One pass: plan what must go outward, send what can go, pull what came back. Never raises: a failure is
        in the returned status ({state: ok | down, reason, outbox, alarm, last_pull}), and the list is untouched."""
        now = self.now()
        status = {"state": "ok", "reason": None, "outbox": 0, "alarm": False, "last_pull": self.last_pull,
                  "written_at": now}
        try:
            reason = self.adapter.health()
        except Exception as err:
            reason = "health check failed: %s" % core.one_line("%s: %s" % (type(err).__name__, err), 120)
        try:
            desired = self.desired(decisions, seeds)
            errored = {"github": "issue", "reports": "report", "v2": "question"}
            kinds = {errored[e["source"]] for e in errors if e.get("source") in errored}
            if not reason and self.rebuild:
                self.reconcile(desired)
            self._enqueue(core.plan_operations(desired, self._projected(), kinds), desired)
            if reason:
                status.update(state="down", reason=core.one_line(reason, 160))
            else:
                self._flush(now)
                self._retry_parked(desired, now)
                if self.last_pull is None or now - self.last_pull >= self.settings["poll_seconds"]:
                    self._pull(desired, decisions, now)
        except Exception as err:   # the engine's own fault: visible, never raised into the pass
            status.update(state="down", reason=core.one_line("sync engine: %s: %s" % (type(err).__name__, err), 160))
        try:
            self._save()
        except OSError as err:
            status.update(state="down", reason="cannot write the link table: %s" % (err.strerror or err))
        status.update(outbox=len(self.ops), last_pull=self.last_pull)
        status["alarm"] = len(self.ops) > self.settings["outbox_alarm_depth"]
        failing = [o for o in self.ops if o.get("last_error")]
        if status["state"] == "ok" and failing:
            status.update(state="down", reason=core.one_line(failing[0]["last_error"], 160))
        try:
            _write_json(self.status_path, status)
        except OSError:
            pass
        return status

    def reconcile(self, desired=None, decisions=None, seeds=None):
        """Rebuild the link table: for each decision that should be outside and has no link, ask the adapter whether
        it already holds it (by key). A found item is linked (its digest unknown, so a revise follows, harmlessly)."""
        if desired is None:
            desired = self.desired(decisions or [], seeds or {})
        for key, want in desired.items():
            if key in self.links:
                continue
            found = self.adapter.lookup(key)
            if found and found.get("external_id"):
                self.links[key] = {"external_id": found["external_id"], "revision": int(found.get("revision") or 1),
                                   "digest": None, "tier": want["tier"], "floor": want["floor"],
                                   "kind": want["kind"], "thread": want["thread"],
                                   "repo": want["payload"].get("repo"), "ask": want["payload"].get("ask"),
                                   "agent": want["payload"].get("agent"), "display_id": want["payload"].get("display_id"),
                                   "withdrawn": False, "closed": False}
                self.host.audit("external.linked", key, {"external_id": found["external_id"], "how": "reconcile"})
        self.rebuild = False

    def _flush(self, now):
        blocked, tried = set(), 0
        for op in sorted(list(self.ops), key=lambda o: o["seq_no"]):
            if op["key"] in blocked or tried >= PASS_OP_LIMIT:
                blocked.add(op["key"])
                continue
            if op["next_at"] > now:
                blocked.add(op["key"])
                continue
            tried += 1
            link = self.links.get(op.get("rebind_from") or op["key"]) or self.links.get(op["key"]) or {}
            operation = {"op": op["op"], "key": op["key"], "external_id": link.get("external_id"),
                         "revision": op["revision"], "decision": op.get("decision"), "floor": op.get("floor")}
            if op.get("note"):
                operation["note"] = op["note"]
            try:
                if op["op"] != "raise" and not operation["external_id"]:
                    raise RuntimeError("no outside id is linked yet")
                answer = self.adapter.push(operation, op["id"]) or {}
            except Exception as err:
                op["attempts"] += 1
                op["last_error"] = core.one_line("%s: %s" % (type(err).__name__, err), 200)
                op["next_at"] = now + backoff_seconds(op["attempts"], self.settings["retry_base_seconds"],
                                                       self.settings["retry_max_seconds"])
                self.host.audit("external.push_failed", op["key"], {"op": op["op"], "attempts": op["attempts"],
                                                                     "error": op["last_error"]})
                blocked.add(op["key"])
                continue
            if not op.get("note_only"):
                self._apply_to(self.links, op, answer.get("external_id"))
            self.ops.remove(op)
            self.host.audit("external.pushed", op["key"], {"op": op["op"], "revision": op["revision"],
                                                           "idempotency": op["id"],
                                                           "external_id": answer.get("external_id")})

    # -- inbound

    def _pull(self, desired, decisions, now):
        current = {k: w["entry"] for k, w in desired.items()}
        events, nxt = self.adapter.pull_changes(self.cursor)
        for event in events or []:
            if not self._apply_event(event, current):
                return   # a transient failure: the cursor stays, the event comes again
        self.cursor = nxt if nxt is not None else self.cursor
        self.last_pull = now

    def _retry_parked(self, desired, now):
        current = {k: w["entry"] for k, w in desired.items()}
        for eid, held in sorted(self.parked.items(), key=lambda kv: kv[1].get("next_at", 0)):
            if held.get("next_at", 0) > now:
                continue
            self._apply_event(held["event"], current, retry=True)

    def _by_external_id(self, external_id):
        for key, link in self.links.items():
            if external_id and link.get("external_id") == external_id:
                return key, link
        return None, None

    def _apply_event(self, event, current, retry=False):
        """True when the event is settled (applied, refused or ignored: never seen again); False to try it again."""
        eid = event.get("event_id") if isinstance(event, dict) else None
        if not isinstance(eid, str) or not eid:
            self.host.audit("external.ignored", "event", {"reason": "an event with no event_id"})
            return True
        if eid in self.applied or (eid in self.parked and not retry):
            return True
        key, link = self._by_external_id(event.get("external_id"))
        kind = event.get("type")
        detail = {"event_id": eid, "type": kind, "external_id": event.get("external_id"),
                  "answered_by": event.get("answered_by")}

        def settle(audit_event, extra=None):
            self.parked.pop(eid, None)
            self.applied.append(eid)
            self.host.audit(audit_event, key or str(event.get("external_id")), dict(detail, **(extra or {})))
            return True

        if link is None:
            return settle("external.orphan", {"reason": "no decision is linked to this outside id: not delivered"})
        role = core.normalize_role(event.get("answered_by"))
        if kind in ("answered", "reversed") and link.get("answered"):
            return self._reversal(key, link, event, role, settle, detail)
        if kind == "reversed":
            return settle("external.ignored", {"reason": "there is no delivered answer to change"})
        if link.get("withdrawn") or key not in current:
            return settle("external.closed", {"reason": "that decision is no longer open here: nothing delivered"})
        entry = current[key]
        if kind == "withdrawn":
            link.update(closed=True, closed_at=self.now())
            return settle("external.withdrawn", {"reason": "withdrawn outside; the local decision stays until it is answered here"})
        if kind in ("answered", "clarify"):
            ok, why = core.answer_authority(role, entry["tier"])
            if not ok:
                self.note_back(key, "not applied: %s" % why, eid)
                return settle("external.refused", {"reason": why, "role": role, "tier": entry["tier"]})
        if kind == "clarify":
            try:
                self.host.deliver_note(entry, "A question about your decision: %s" % core.one_line(event.get("text") or "", 300))
            except Exception as err:
                self.host.audit("external.deliver_failed", key, dict(detail, error=core.one_line(str(err), 200)))
                return False
            link.setdefault("clarified", []).append(eid)
            return settle("external.clarify", {"role": role})
        if kind == "answered":
            intent = {"option_id": event.get("option_id"), "text": event.get("text"), "rows": event.get("rows") or {},
                      "answered_by": role, "event_id": eid}
            if role == "owner_agent":   # the owner's standing orders bind it, as they bind the Chief of Staff's own answers
                judged = self._judged(eid, event, entry, intent, key)
                if judged is not None:
                    why = judged["reason"]
                    if judged["verdict"] == "unavailable":
                        return True   # parked: asked again later, the cursor moves on
                    self.note_back(key, "not applied: %s" % why, eid)
                    return settle("external.judged", {"verdict": "deny", "reason": why, "role": role,
                                                       "tier": entry["tier"]})
            try:
                self.host.deliver_answer(entry, intent)
            except Exception as err:
                self.host.audit("external.deliver_failed", key, dict(detail, error=core.one_line(str(err), 200)))
                return False
            link["answered"] = {"option_id": event.get("option_id"), "by": role, "event_id": eid,
                                "at": event.get("at"), "tier": entry["tier"]}
            link.update(closed=True, closed_at=self.now())   # it leaves the list because it was answered: nothing to withdraw outside
            return settle("external.answered", {"role": role, "option_id": event.get("option_id")})
        return settle("external.ignored", {"reason": "an event type this fleet does not act on"})

    def _judged(self, eid, event, entry, intent, key):
        """None when the judge allows the answer. Else {"verdict": "deny" | "unavailable", "reason"}: a deny is final;
        an unavailable judge parks the event for another try, up to JUDGE_MAX_TRIES, and then it is a deny."""
        try:
            found = self.host.judge_answer(entry, intent) or {}
        except Exception as err:
            found = {"verdict": "unavailable", "reason": "the judge failed: %s" % core.one_line(str(err), 120)}
        verdict = found.get("verdict")
        reason = core.one_line(found.get("reason") or verdict or "no verdict", 300)
        if verdict == "allow":
            return None
        if verdict != "unavailable":
            return {"verdict": "deny", "reason": reason}
        held = self.parked.setdefault(eid, {"event": event, "tries": 0, "next_at": 0})
        held["tries"] += 1
        self.host.audit("external.judge_unavailable", key, {"event_id": eid, "tries": held["tries"], "reason": reason})
        if held["tries"] >= JUDGE_MAX_TRIES:
            return {"verdict": "deny", "reason": "the judge could not be asked after %d tries (%s): not applied, "
                                                 "the owner decides" % (held["tries"], reason)}
        held["next_at"] = self.now() + backoff_seconds(held["tries"], self.settings["retry_base_seconds"],
                                                        self.settings["retry_max_seconds"])
        return {"verdict": "unavailable", "reason": reason}

    def _reversal(self, key, link, event, role, settle, detail):
        """The answer was changed outside after it was delivered: the asker is told, and nothing is undone."""
        ok, why = core.answer_authority(role, (link.get("answered") or {}).get("tier") or link.get("tier"))
        if not ok:
            return settle("external.refused", {"reason": why, "role": role})
        try:
            self.host.deliver_note(link, "answer changed: %s" % core.one_line(event.get("text") or event.get("option_id") or "", 300))
        except Exception as err:
            self.host.audit("external.deliver_failed", key, dict(detail, error=core.one_line(str(err), 200)))
            return False
        link["answered"] = dict(link.get("answered") or {}, option_id=event.get("option_id"), by=role)
        return settle("external.reversed", {"role": role, "option_id": event.get("option_id")})
