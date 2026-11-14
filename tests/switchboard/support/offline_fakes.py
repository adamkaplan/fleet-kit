"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): offline_fakes."""
import contextlib
import fcntl
import fnmatch
import json
import os
import re
import shutil
import subprocess
from unittest import mock
from support.baseline_adapters import with_baseline
from support.bodies_and_comments import _utc, intent_body
from support.herdr_world import World
from support.installer_tables import WORKER_STEPS
from support.loader import sb, sc, with_http_head
from support.policy_and_import import FAKE_V1
from support.process_and_ci import apply_edit_intent, apply_handoff_step, classified_in_audit
from support.scenario_and_delivery import JEV_SCENARIO_CONFIG
from support.scenario_roles import FAKE_GH, FAKE_REPO
from support.source_scans import _matches
from support.wire_fakes import EPOCH, FAKE_HERDR, FAKE_OPENCODE, HERDR_ALLOWED, SOCKET_ALLOWED, _record_keys


class OfflineRun:
    """One scenario on the fakes: a pass every POLL seconds while the daemon
    runs, steps applied at their times, a simulated clock in 1 s ticks.
    Expectations are judged on the fakes' state, never on the engine's report."""

    POLL = 5

    def __init__(self, test, scenario, label, faults=(), baseline=None):
        self.scenario = scenario
        self.faults = sorted(faults)
        self.baseline = baseline
        self.work = test.home / "scenario-repo"
        self.work.mkdir(exist_ok=True)
        self.cos_orders_path = str(test.home / "scenario-runs" / scenario["id"] / label / "standing-orders.md")  # PR 18
        self.world = World(test, agents=(), state=test.home / "scenario-runs" / scenario["id"] / label,
                           config=dict(JEV_SCENARIO_CONFIG, cos_orders_file=self.cos_orders_path,
                                       **(scenario.get("config") or {})))
        launched = {step["agent"] for step in scenario["steps"] if step["do"] in ("launch", "import-v1")}
        if any(step["do"] == "bootstrap" for step in scenario["steps"]):
            launched.add(sb.COS_NAME)  # PR 16: `bootstrap` makes the Chief of Staff
        for name, member in sorted(scenario["cast"].items()):
            if name not in launched:  # a launched agent does not exist until `launch` makes it
                self.world.add_agent(name, fleet=self.fleet_of(member))
        attach_intent_lab(self)  # PR 6: a fake GitHub when the scenario has issues
        self.world.engine = self.world.new_engine()
        self.daemon = True
        self.lock = None
        self.take_lock()        # PR 16: a live daemon holds run.lock, so ensure-on-use can tell
        test.addCleanup(self.drop_lock)
        self.said = {}          # PR 16: step index -> what a command step printed
        self.crashed_writes = set()
        self.launched = {}
        self.snapshots = {}  # PR 14: second -> what `fleet-switchboard decisions` showed then, for the decisions oracles

    @staticmethod
    def fleet_of(member):
        return {key: member[key] for key in ("role", "reports_to", "issue", "charter", "repo") if key in member}

    def run(self):
        # #2108: load_module() resolves the real path of bin/ files twice on every pass (about 30% of a play);
        # those paths never change during a play, so answer them from a cache. Every other path is resolved as before.
        bin_dir, real = os.path.dirname(os.path.realpath(sb.__file__)), os.path.realpath
        cache = {}

        def realpath(path, *args, **kwargs):
            if args or kwargs or not isinstance(path, str) or not path.startswith(bin_dir):
                return real(path, *args, **kwargs)
            if path not in cache:
                cache[path] = real(path)
            return cache[path]
        # and the client stamps its call record with the real clock while the play runs on the fake one, so nothing
        # was ever forgotten and each pass counted the whole record again (quadratic); stamp with the play's clock.
        world, original_init = self.world, sb.V2Client.__init__

        def client_init(client, *args, **kwargs):
            original_init(client, *args, **kwargs)
            client.clock = lambda: float(world.clock.t)
        with mock.patch.object(os.path, "realpath", realpath), mock.patch.object(sb.V2Client, "__init__", client_init):
            if getattr(world.engine, "v2", None) is not None and hasattr(world.engine.v2, "calls"):
                world.engine.v2.clock = lambda: float(world.clock.t)   # the engine made in __init__
            return self._play()

    def _play(self):
        steps = list(self.scenario["steps"])
        end = max([sc.duration_seconds(step["at"]) for step in steps] or [0])
        for second in range(end + 1):
            self.world.clock.t = EPOCH + second
            self.world.v2.advance()
            while steps and sc.duration_seconds(steps[0]["at"]) == second:
                self.apply(steps.pop(0))
            if self.daemon and second % self.POLL == 0:
                self.one_pass(())
            if second in self.snapshot_times():
                self.snapshot(second)
        return self

    def snapshot_times(self):
        return {sc.duration_seconds(e["at"]) for e in self.scenario["expect"] if e["that"] in ("decisions", "decisions_say")}

    def snapshot(self, second):
        """PR 14: the file the daemon keeps, as `fleet-switchboard decisions` would read it at this second."""
        os.environ[sb.FAULT_ENV] = ",".join(self.faults)
        now = self.world.clock.now()
        report = sb.decisions_report(self.world.state, now)
        self.snapshots[second] = {"report": report, "text": sb.format_decisions(report, now)}

    def apply(self, step):
        do, world = step["do"], self.world
        if do == "fact" and self.baseline == "fleet-heartbeat":
            self.type_wake(step)
        elif do == "fact":
            world.fact(step["to"], step["key"], step["summary"], kind=step.get("kind", "lab.fact"),
                       issue=step.get("issue"), batch=step.get("batch", True), **{"from": step.get("from", "world")})
        elif do == "turn":
            world.v2.start_turn(world.sessions[step["actor"]], "fleet task", fleet=True,
                                lasts=sc.duration_seconds(step.get("lasts", "0s")), steps=step.get("steps", 1))
        elif do == "message":
            if step["actor"] != "you":
                raise NotImplementedError("offline: a message comes from you")
            world.v2.start_turn(world.sessions[step["to"]], step["text"], fleet=False,
                                lasts=sc.duration_seconds(step.get("lasts", "0s")))
        elif do == "draft":
            world.herdr.input_box[world.panes[step["to"]]] = step["text"]  # you, typing; nothing is sent
        elif do == "launch":
            self.launch(step)
        elif do == "report":  # PR 13: `fleet-switchboard report`, as the cast member's model would run it
            os.environ[sb.FAULT_ENV] = ",".join(self.faults)
            world.new_engine("cli").report(step["actor"], step["state"], step["text"], step.get("issue"))
        elif do == "send":  # PR 14: `fleet-switchboard send`, as the cast member's model would run it
            os.environ[sb.FAULT_ENV] = ",".join(self.faults)
            world.new_engine("cli").send(step["actor"], step["to"], step["text"], step.get("issue"))
        elif do == "delete-decisions":  # PR 14: the projection is disposable; the daemon makes it again
            if (world.state / sb.DECISIONS_FILE).exists():  # under `no-decisions-file` there is none to delete
                (world.state / sb.DECISIONS_FILE).unlink()
        elif do == "pass":
            self.one_pass(step.get("faults", ()))
        elif do == "kill-daemon":
            self.daemon = False
            self.drop_lock()
        elif do == "restart-daemon":
            self.start_daemon()
        elif do == "use":  # PR 16
            self.use(step)
        elif do == "bootstrap":
            self.bootstrap(step)
        elif do == "close-agent":
            world.herdr([FAKE_HERDR, "pane", "close", world.panes[step["agent"]]])
        elif do in WORKER_STEPS:
            apply_worker_step(self, step)
        elif do == "edit-intent":
            apply_edit_intent(self, step)
        elif do == "handoff":  # PR 8
            apply_handoff_step(self, step)
        elif do != "wait":
            raise NotImplementedError("the offline runner does not implement step %r" % do)

    # -- PR 16: the daemon's lock, the commands that start it, and `bootstrap`
    def take_lock(self):
        if self.lock is None:
            fd = os.open(str(self.world.state / "run.lock"), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:  # another run in this test already holds this state directory's lock
                os.close(fd)
                return
            self.lock = fd

    def drop_lock(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    def start_daemon(self):
        self.daemon = True
        self.world.engine = self.world.new_engine()  # a new process: nothing in memory survives
        self.take_lock()

    def use(self, step):
        """A command that needs the daemon, as `main` runs it: ensure-on-use first, with a spawn that is this runner's daemon."""
        if step["command"] not in sb.ENSURE_ON_USE_COMMANDS:
            raise NotImplementedError("%r does not ensure the daemon" % step["command"])
        with mock.patch.dict(os.environ, {sb.FAULT_ENV: ",".join(self.faults)}), mock.patch.object(sb, "ENSURE_ON_USE", True):
            sb.ensure_on_use(lambda argv, **kwargs: self.start_daemon(), state_dir=self.world.state, sleep=lambda s: None)

    def bootstrap(self, step):
        world = self.world
        index = [i for i, s in enumerate(self.scenario["steps"]) if s is step][0]
        with mock.patch.dict(os.environ, {sb.FAULT_ENV: ",".join(self.faults)}):
            try:
                done = sb.bootstrap_cos(world.new_engine("cli"), directory=str(self.work), patience=5,
                                        sleep=lambda seconds: setattr(world.clock, "t", world.clock.t + seconds))
            except sb.BootstrapRefused as err:
                self.said[index] = "bootstrap refused: %s" % err
                return
        world.sessions[sb.COS_NAME], world.panes[sb.COS_NAME] = done["session"], done["pane"]
        self.said[index] = "%s %s in workspace %s, pane %s" % (done["action"], sb.COS_NAME, done["workspace"], done["pane"])

    def oracle_status_first_line(self, e):
        """The first line of `fleet-switchboard status` for a machine whose config asks for the decision model, with no key."""
        root = self.world.state.parent / ("status-" + self.world.state.name)
        config = root / "config" / "fleet-switchboard" / "config.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "jev": {}}))
        env = {"XDG_CONFIG_HOME": str(root / "config"), "XDG_STATE_HOME": str(root / "state"),
               sb.FAULT_ENV: ",".join(self.faults)}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("OPENROUTER_API_KEY", None)
            text = sb.format_status(sb.collect_status(self.world.route, self.world.clock.now))
        first = text.splitlines()[0]
        return e["text"] in first, "the first line is %r" % first

    def oracle_bootstrap_says(self, e):
        said = self.said.get(e["step"])
        return said is not None and e["text"] in said, "step %d printed %r" % (e["step"], said)

    def oracle_fleet_sessions(self, e):
        found = [sid for sid, session in self.world.v2.sessions.items()
                 if ((session.get("metadata") or {}).get("fleet") or {}).get("name") == e["agent"]]
        return len(found) == e["count"], "%d v2 session(s) carry the fleet name %s" % (len(found), e["agent"])

    def type_wake(self, step):
        """The baseline: today's heartbeat types the wake with `herdr agent prompt`.
        This runner is the only code allowed to; the engine never does."""
        herdr = self.world.herdr
        herdr.allow_typing = True
        herdr([FAKE_HERDR, "agent", "prompt", self.world.panes[step["to"]], "[heartbeat] " + step["summary"]])
        herdr.allow_typing = False

    def launch(self, step):
        """fleet-switchboard launch, as the CLI runs it, on the fakes."""
        name, world = step["agent"], self.world
        member = self.scenario["cast"][name]
        done = sb.launch_agent(
            world.new_engine("cli"), name, "coder" if member["role"] == "coder" else member["role"], str(self.work),
            "w1", member["role"], member.get("reports_to"), member.get("issue"), step.get("brief"),
            patience=5, sleep=lambda seconds: setattr(world.clock, "t", world.clock.t + seconds),
            repo=member.get("repo"))
        world.sessions[name], world.panes[name] = done["session"], done["pane"]
        self.launched[name] = done

    def one_pass(self, step_faults):
        os.environ[sb.FAULT_ENV] = ",".join(sorted(set(self.faults) | set(step_faults)))
        first = len(self.world.v2.writes)
        try:
            if (self.world.state / "cloud").exists():
                self.world.engine.run_pass()
            else:
                # no cloud pod in the world: the cloud ticks (each re-reads a helper module from disk) have nothing to find on any of the thousands of passes
                quiet = ("lost_worker_tick", "push_watch_tick", "pod_silence_tick", "cloud_watch_tick", "cloud_end_tick", "cloud_stall_tick", "cloud_turn_tick")
                with contextlib.ExitStack() as stack:
                    for name in quiet:
                        stack.enter_context(mock.patch.object(sb, name, lambda *a, **k: None))
                    self.world.engine.run_pass()
        except sb.SimulatedCrash:
            self.crashed_writes.update(range(first, len(self.world.v2.writes)))
            self.world.engine = self.world.new_engine()
        finally:
            os.environ[sb.FAULT_ENV] = ",".join(self.faults)

    # -- oracles: each returns (ok, what was observed)
    def mode_of(self, name, message_id):
        """How a delivered message reached the model, from the model calls:
        `wake` when it was the input that called the model (it started a turn
        from idle, or ran after the final reply); `note` when it rode along
        with another input, or was injected between steps. The same rule as
        the Lab's (scenarios/switchboard/README.md)."""
        for call in self.world.model_calls(name):
            ids = [i["id"] for i in call["inputs"]]
            if message_id in ids:
                woke = ids[-1] == message_id and call["phase"] in ("turn_start", "after_reply")
                return "wake" if woke else "note"
        return None

    def oracle_delivered(self, e):
        found = [a for a in self.world.admissions(e["to"])
                 if a["delivered_at"] is not None and _matches(a["keys"], e["key"])]
        if "mode" in e:
            found = [a for a in found if self.mode_of(e["to"], a["id"]) == e["mode"]]
        if "text" in e:
            found = [a for a in found if e["text"] in a["text"]]
        keys = {key for a in found for key in a["keys"] if fnmatch.fnmatchcase(key, e["key"])}
        ok = len(found) == e["count"] if "count" in e else bool(found)
        if "keys" in e:
            ok = ok and len(keys) == e["keys"]
        return ok, "%d message(s) in the transcript carry %d matching key(s)" % (len(found), len(keys))

    def oracle_message_count(self, e):
        found = [a for a in self.world.admissions(e["to"]) if a["from"] == "switchboard"]
        return len(found) == e["count"], "%d switchboard message(s) admitted" % len(found)

    def oracle_model_saw(self, e):
        when = e.get("when", "any")
        calls = [c for c in self.world.model_calls(e["agent"], None if when == "any" else when)
                 if any(_matches(i["keys"], e["key"]) for i in c["inputs"])]
        ok = len(calls) == e["count"] if "count" in e else bool(calls)
        return ok, "%d model call(s) (%s) carried it, at %s s" % (
            len(calls), when, [int(c["at"] - EPOCH) for c in calls])

    def oracle_draft_intact(self, e):
        """The fake herdr's input box still holds the draft, and no message sent it."""
        draft = " ".join(e["text"].split())
        box = " ".join(self.world.herdr.input_box.get(self.world.panes[e["agent"]], "").split())
        session = self.world.v2.sessions[self.world.sessions[e["agent"]]]
        sent = [m["id"] for m in session["messages"] if m["type"] == "user" and draft in " ".join(m["text"].split())]
        return draft in box and not sent, "input box %r; sent in: %s" % (box, ", ".join(sent) or "no message")

    def oracle_metadata_roundtrip(self, e):
        name = e["agent"]
        session = self.world.v2.sessions[self.world.sessions[name]]
        if e.get("of", "session") == "session":
            want = e["fleet"] if "fleet" in e else dict(
                {"name": name}, **self.fleet_of(self.scenario["cast"][name]))
            got = (session["metadata"] or {}).get("fleet")
            return got == want, "session.get metadata.fleet = %s; expected %s" % (got, want)
        if e.get("key"):
            found = [m for m in session["messages"] if _matches(_record_keys(m), e["key"])]
        else:
            found = [m for m in session["messages"] if m["type"] in ("user", "synthetic") and e["text"] in m["text"]]
        if not found:
            return False, "no message with %r in the transcript" % (e.get("key") or e.get("text"))
        default = {"keys": [e["key"]]} if e.get("key") else None
        want, got = e.get("fleet", default), (found[0].get("metadata") or {}).get("fleet")
        return got == want, "%s metadata.fleet = %s; expected %s" % (found[0]["type"], got, want)

    def launched_pane(self, name):
        """The pane herdr says shows this agent's session, found through herdr's agent list."""
        session = self.world.sessions[name]
        for pane in sorted(self.world.herdr.panes):
            if sb.agent_session(self.world.herdr.agent(pane)) == session:
                return pane, self.world.herdr.panes[pane]
        return None, None

    def oracle_pane_runs_v2(self, e):
        pane, entry = self.launched_pane(e["agent"])
        if pane is None:
            return False, "no herdr pane shows session %s" % self.world.sessions[e["agent"]]
        program = entry["program"] or ""
        ok = sb.runs_program(program, FAKE_OPENCODE) and program.endswith("-s " + self.world.sessions[e["agent"]])
        return ok, "pane %s runs %r (agent opencode on %s)" % (pane, program, self.world.sessions[e["agent"]])

    def oracle_no_typing(self, e):
        """The pane was started by argv, so it has no shell, and herdr was asked to type nothing."""
        pane, entry = self.launched_pane(e["agent"])
        typed = self.world.herdr.typing_attempts
        ok = entry is not None and entry.get("argv", [None])[0] == FAKE_OPENCODE and not typed
        return ok, "pane %s opened by argv %s; typing attempts: %s" % (pane, (entry or {}).get("argv"), typed or "none")
    def oracle_classified(self, e):  # PR 7: see "PR 7" below
        return classified_in_audit(self.world.audit(), e["agent"], self.scenario["steps"][e["step"]]["text"], e["as"])

    def evaluate(self):
        results = []
        for expectation in self.scenario["expect"]:
            oracle = getattr(self, "oracle_" + expectation["that"], None)
            if oracle is None:
                raise NotImplementedError("the offline runner has no oracle %r" % expectation["that"])
            ok, seen = oracle(expectation)
            results.append({"label": expectation.get("name", expectation["that"]),
                            "that": expectation["that"], "ok": ok, "seen": seen})
        return results

    def implicit(self):
        """Checked in every run: each write audited before and after (unless
        the process crashed right after it), and herdr never asked to type."""
        problems = []
        entries = self.world.audit()
        for index, write in enumerate(self.world.v2.writes):
            before = entries[:write["audit_lines"]]
            intents = [e for e in before if e.get("phase") == "intent"
                       and write["id"] in ((e.get("detail") or {}).get("message_id"),
                                           (e.get("detail") or {}).get("inbox_id"))]
            if not intents:
                problems.append("%s %s had no audit intent before it" % (write["op"], write["id"]))
                continue
            call = intents[-1]["call"]
            if index not in self.crashed_writes and not any(
                    e.get("phase") == "result" and e.get("call") == call for e in entries):
                problems.append("%s %s has no audit result" % (write["op"], write["id"]))
        for argv in self.world.herdr.calls:
            if argv[1:3] not in HERDR_ALLOWED:
                problems.append("herdr asked for %r" % argv[1:])
        problems.extend("herdr was asked to type: %r" % attempt for attempt in self.world.herdr.typing_attempts)
        problems.extend("herdr socket method %r" % method for method, _ in self.world.herdr.socket_calls
                        if method not in SOCKET_ALLOWED)
        return problems


def control_play(test, scenario):
    """The control's play and a line saying what it switched off or swapped."""
    control = scenario["control"]
    if "fault" in control:
        return OfflineRun(test, scenario, "control", [control["fault"]]).run(), "fault %s" % control["fault"]
    if control.get("baseline") == "timer-heartbeat":  # the old timer, added to the play (PR 4)
        return OfflineRun(test, with_baseline(scenario, "timer-heartbeat"), "control").run(), \
            "baseline timer-heartbeat"
    if "baseline" in control:
        return OfflineRun(test, scenario, "control", baseline=control["baseline"]).run(), \
            "baseline %s" % control["baseline"]
    return OfflineRun(test, sc.apply_variant(scenario, control["variant"]), "control").run(), "variant"


def run_scenario(test, scenario):
    """The run, then the control. Returns a report; a control that passes is a failure."""
    control = scenario["control"]
    main = OfflineRun(test, scenario, "run").run()
    checked, how = control_play(test, scenario)
    run_results, control_results = main.evaluate(), checked.evaluate()
    proves_nothing = [entry for entry in control["fails"]
                      if all(r["ok"] for r in control_results if entry in (r["label"], r["that"]))]
    return {"id": scenario["id"], "control_kind": how, "run": run_results, "control": control_results,
            "implicit": main.implicit(), "failures": [r for r in run_results if not r["ok"]],
            "proves_nothing": proves_nothing}


def apply_worker_step(run, step):
    world, do = run.world, step["do"]
    if do == "away":
        return  # you simply prompt nothing more
    if do == "permission" and step["actor"] != "you":
        name = step["actor"]  # a turn that cannot finish until you answer
        if not world.v2.busy(world.sessions[name]):  # PR 18: an agent already blocked asks again, in the same turn
            world.v2.start_turn(world.sessions[name], "fleet task", fleet=True, lasts=86400)
        world.v2.ask_permission(world.sessions[name])
        world.herdr.panes[world.panes[name]]["status"] = "blocked"
    elif do == "permission":
        name = step["to"]
        world.herdr.panes[world.panes[name]]["status"] = None
        world.v2.answer_permissions(world.sessions[name])
    elif do == "close-pane":
        world.engine.herdr.runner = ClosingRunner(world.herdr, world.panes[step["agent"]])
    elif do == "delete-state":
        shutil.rmtree(str(world.state))
        sb.ensure_state_dir(world.state)  # what the next start of the daemon does first
    else:  # timer-heartbeat: the old fleet-heartbeat wakes each orchestrator whether or not anything happened
        for name, member in sorted(run.scenario["cast"].items()):
            if member["role"] in ("orchestrator", "chief-of-staff"):
                ses = world.sessions[name]
                keys = ["heartbeat:%s:%d" % (name, world.clock.now())]
                world.engine.v2.synthetic(ses, "heartbeat: anything new?", {"fleet": {"keys": keys, "from": "fleet-heartbeat"}},
                                          True, "queue", sb.derive_message_id(ses, keys))


class FakeGH:
    """`gh api` over a scenario's issues. A runner, like the other fakes.

    Serves GET only, by path: an issue, its parent (404 without one) and a
    charter's sub-issues. Any other shape of call raises, so a write cannot
    pass for a read. `issues` is a scenario's table: number -> intent,
    done_when, parent. A charter's body is its yaml block.
    """

    def __init__(self, issues, charters, clock, repo=FAKE_REPO, token=None):
        self.issues = {int(n): dict(v) for n, v in issues.items()}
        self.charters, self.clock = set(charters), clock
        self.revision, self.updated = {}, {}
        self.calls, self.down = [], False
        self.repo, self.token = repo, token  # PR 15: which repo this is, and the one token that may read it
        self.api = "repos/%s/issues/" % repo

    def body(self, number):
        if number in getattr(self, "bodies", {}):  # PR 17: a body written by PATCH, or set by a test
            return self.bodies[number]
        if number in self.charters:
            line = self.issues[number].get("authority")  # PR 9: a charter's Standing authority line
            workspace = self.issues[number].get("workspace")  # PR 15: a charter's `workspace:` line
            return "```yaml\norchestrator: platform\n%sstatus: active\n```\n\nThe charter.\n" % (
                "workspace: %s\n" % workspace if workspace else "") + (
                "\nStanding authority: %s\n" % line if line else "")
        issue = self.issues[number]
        return intent_body(issue.get("intent"), issue.get("done_when"), self.revision.get(number, 0))

    def record(self, number):
        return {"number": number, "state": self.issues[number].get("state", "open"),
                "title": "Issue %d" % number, "body": self.body(number),
                "updated_at": self.updated.get(number, "2027-01-01T00:00:00Z")}

    def edit(self, number, **fields):
        """Edit the intent fields (None removes a line); the body always changes. (old, new) bodies."""
        old = self.body(number)
        self.issues[number].update(fields)
        self.revision[number] = self.revision.get(number, 0) + 1
        self.updated[number] = _utc(self.clock.now())
        return old, self.body(number)

    def __call__(self, argv, **kwargs):
        return with_http_head(self.serve, argv, kwargs, self.__dict__.setdefault("http", {}))

    def serve(self, argv, kwargs):
        self.calls.append(argv)
        if "--method" in argv and argv[argv.index("--method") + 1] == "PATCH":  # PR 17: a charter's body
            return self.patch_body(argv, kwargs)
        if "--method" in argv:  # PR 8: the handoff comment, the one write
            return self.post_comment(argv, kwargs)
        if len(argv) != 3 or argv[:2] != [FAKE_GH, "api"]:
            raise AssertionError("fake gh serves `gh api <path>` only, a GET; got %r" % argv)
        if self.down:
            return subprocess.CompletedProcess(argv, 1, "", "gh: network unreachable\n")
        if self.token is not None and (kwargs.get("env") or {}).get("GH_TOKEN") != self.token:
            if getattr(self, "leak", False):  # a hostile gh that echoes the credential into its error
                return subprocess.CompletedProcess(argv, 1, "", "gh: HTTP 401 bad credentials %s\n"
                                                   % (kwargs.get("env") or {}).get("GH_TOKEN"))
            return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)\n")
        match = re.match(r"^%s(\d+)(/parent|/sub_issues\?per_page=100)?$" % re.escape(self.api), argv[2])
        if not match or int(match.group(1)) not in self.issues:
            return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)\n")
        number, tail = int(match.group(1)), match.group(2)
        if tail == "/parent":
            parent = self.issues[number].get("parent")
            body = self.record(parent) if parent in self.issues else None
        elif tail:
            body = [self.record(n) for n in sorted(self.issues) if self.issues[n].get("parent") == number]
        else:
            body = self.record(number)
        if body is None:
            return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)\n")
        return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")


def attach_intent_lab(run):
    """The offline runner's seam: a scenario with `issues` gets a fake GitHub and a reader over it."""
    run.ask_gh = None
    if "issues" not in run.scenario or "repos" in run.scenario:  # PR 15: a play with repos has its own GitHub
        return
    charters = {m["charter"] for m in run.scenario["cast"].values() if "charter" in m}
    run.ask_gh = FakeGH(run.scenario["issues"], charters, run.world.clock)
    run.world.intents = sb.IntentReader(FAKE_GH, FAKE_REPO, run.ask_gh, run.world.clock.now, 5)


class FakeV1:
    """The OpenCode v1 binary as the import sees it: it answers `export <id>` from
    a table of exports and refuses to be asked anything else, so a test fails if
    the command ever runs v1 for another purpose."""

    def __init__(self, exports=None):
        self.exports = {i: json.dumps(e) for i, e in (exports or {}).items()}
        self.calls = []

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if len(argv) != 3 or argv[:2] != [FAKE_V1, "export"]:
            raise AssertionError("v1 may only be asked `export <id>`; got %r" % argv)
        if argv[2] not in self.exports:
            return subprocess.CompletedProcess(argv, 1, "", "Session not found: %s\n" % argv[2])
        return subprocess.CompletedProcess(argv, 0, self.exports[argv[2]], "")


class ClosingRunner:
    """herdr's runner that closes a pane the moment the engine reads it with
    `agent get`: the pane closes between the decision and the delivery."""

    def __init__(self, herdr, pane):
        self.herdr, self.pane, self.fired = herdr, pane, False

    def __call__(self, argv, **kwargs):
        if list(argv[1:3]) == ["agent", "get"] and argv[3:4] == [self.pane] and not self.fired:
            self.fired = True
            del self.herdr.panes[self.pane]
        return self.herdr(argv, **kwargs)
