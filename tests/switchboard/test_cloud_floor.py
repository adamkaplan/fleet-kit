"""Floor as the remote-worker backend (ask #2564, bin/cloud_floor.py): the transport, the admission rules, the cutover and the guards on what is printed.

CLAIMS: S1, S2, S3, S5, U1. Every test drives the real `cmd_launch`, `cmd_send`, `cmd_read`, `status_lines` and `cmd_retire` of bin/cloud_worker.py (as
bin/cloud_inspect.py wrapped them) against an in-memory FakeFloor that keeps the rules of the caller API: a command key is applied once, a message or a
cancel must carry the next `seq`, a cancel with a heavy job in flight answers `reserved` until custody is confirmed, `down` is an HTTP 503 and `art-9`
is an unreadable artifact. The pod side is a counter: `run_launcher`, `Cluster` and `tunnel` of bin/cloud_worker.py are stand-ins that count their calls.

Every input and external call:
  input / call                                   test                                                                  claim
  launch --cloud routed to Floor (submit)        test_a_launch_makes_one_work_and_a_repeat_returns_it                  S1
  a launch that lost its record write            test_a_launch_that_lost_its_record_finds_the_same_work                S1
  brief text (push, PR, prove, land lines)       test_the_brief_loses_every_delivery_step_and_gains_the_contract       S1
  send (message, seq, command key)               test_sends_are_ordered_and_a_retry_is_the_same_command                S1
  read, status (observe, result)                 test_read_and_status_show_the_work                                    S1
  retire (cancel, reserved custody)              test_retire_waits_for_custody_then_removes_the_record                 S1
  retire by a stranger                           test_retire_by_a_stranger_is_refused_and_asks_nothing                 S1
  CW_FLOOR_VERIFY_ENABLED off / on (tester, rehearse)  TestCloudFloorRolesAndBriefs.test_verify_*                                     S3
  local pane and --headless launch, local read   test_a_local_launch_and_read_make_no_floor_call                       S1
  CW_REMOTE_BACKEND flips, pod records           test_a_worker_stays_on_the_side_that_admitted_it                      S2
  the route table (backend, CW_FLOOR_REPOS)      test_the_route_rule                                                   S2
  status over pod and Floor records              test_status_gives_the_launcher_only_the_pod_records                  S2
  role and workflow matrix                       test_the_matrix_refuses_what_floor_cannot_work                       S3
  100% of eligible admissions                    test_every_eligible_admission_goes_to_floor_and_none_to_the_launcher S3
  retire_pod_launcher, drain_check               test_the_pod_launcher_retires_only_when_closed_and_drained           S3
  Floor 503 on submit                            test_an_outage_on_submit_writes_no_record                            S5
  unreadable artifact on observe and result      test_an_unreadable_artifact_is_named_and_the_record_is_untouched     S5
  the HTTP client (503, art-9, redirect)         test_the_http_client_over_loopback                                   S5
  Queue + Blob wire format over loopback         TestCloudFloorQueueBlob (five tests: life, payload blob, state_seq, outage, choice)   S1 S5
  private names in settings and errors           test_planted_private_values_never_reach_output_or_files              U1
  the two guards of a new module and test file   test_the_guard_tools_pass                                            U1

Mutants (each is applied to bin/cloud_floor.py in memory; the test named must FAIL under it, and the unmutated control passes first):
  M1  translate_brief returns the brief unchanged, or the send key drops the seq   -> the brief test, the send test
  M2  send and retire pick the side by the CW_REMOTE_BACKEND of the day            -> test_a_worker_stays_on_the_side_that_admitted_it
  M3  the admission matrix lets every repository and role through                  -> test_the_matrix_refuses_what_floor_cannot_work
  M6  the verify setting is ignored (verify launches are admitted while it is off) -> test_verify_modes_are_refused_while_the_setting_is_off
  M5  a failed submit is swallowed and the record is written anyway                -> test_an_outage_on_submit_writes_no_record
"""
import argparse
import contextlib
import hashlib
import http.server
import io
import json
import os
import re
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from support.core import Base
from support.loader import sb
from support.paths import REPO

REPO_NAME, OTHER_REPO = "acme-org/widget-service", "acme-org2/plain-site"
CONFIG = {"land_repos": ["acme-org/*"], "call_timeout_seconds": 5}
FUTURE = "2099-01-01T00:00:00Z"
BRIEF = ("Add a retry to the fetch loop.\nWhen it works: git push origin HEAD\nthen run: gh pr create --fill\n"
         "and `fleet-switchboard prove --pr acme-org/widget-service@" + "a" * 40 + "`\nkeep the change small\n")


class FakeFloor:
    """The caller API of Floor in memory: Submit, Message, Cancel, Observe and Result with idempotency keys, seq checks and custody."""

    def __init__(self, floor):
        self.floor, self.calls = floor, []
        self.works, self.keys = {}, {}
        self.down = self.reserved = self.bad_artifact = False
        self.landed = self.manifest = self.log_tail = None

    def _fail(self, verb):
        self.calls.append(verb)
        if self.down:
            raise self.floor.FloorError("Floor answered HTTP 503", 503)

    def _once(self, key, make):
        if key not in self.keys:
            self.keys[key] = make()
        return self.keys[key]

    def submit(self, caller, cmd_id, **fields):
        self._fail("submit")
        self.submitted = dict(fields, cmd_id=cmd_id, caller=caller)

        def make():
            work_id = "w-" + hashlib.sha256((caller + fields["session_id"]).encode()).hexdigest()[:10]
            self.works[work_id] = {"session": fields["session_id"], "seq": 1, "messages": [], "cancelled": False}
            return {"work_id": work_id, "session_id": fields["session_id"], "seq": 1}
        return self._once(cmd_id, make)

    def _work(self, work_id):
        if work_id not in self.works:
            raise self.floor.FloorError("no such work", 404)
        return self.works[work_id]

    def message(self, caller, work_id, cmd_id, seq, text):
        self._fail("message")
        work = self._work(work_id)

        def make():
            if seq != work["seq"] + 1:
                raise self.floor.FloorError("seq conflict", 409)
            work["seq"] = seq
            work["messages"].append({"role": "user", "seq": seq, "text": text})
            return {"id": "m-%d" % seq, "seq": seq}
        return self._once(cmd_id, make)

    def cancel(self, caller, work_id, cmd_id, seq):
        self._fail("cancel")
        work = self._work(work_id)

        def make():
            if seq != work["seq"] + 1:
                raise self.floor.FloorError("seq conflict", 409)
            work["seq"], work["cancelled"] = seq, True
            return {"cancelled": True}
        return dict(self._once(cmd_id, make), reserved=self.reserved)

    def _artifact(self):
        if self.bad_artifact:
            raise self.floor.UnavailableArtifactError("art-9")

    def observe(self, caller, work_id):
        self._fail("observe")
        self._artifact()
        work = self._work(work_id)
        return {"state": "cancelled" if work["cancelled"] else "running", "busy": True, "landed_sha": self.landed, "rounds": 2, "messages": list(work["messages"]),
                "manifest": self.manifest, "log_tail": self.log_tail}

    def result(self, caller, work_id):
        self._fail("result")
        self._artifact()
        self._work(work_id)
        return {"state": "landed" if self.landed else "running", "landed_sha": self.landed, "manifest": self.manifest, "log_tail": self.log_tail}


class Quiet:
    """A stand-in for Cluster() and tunnel() of the pod side: it counts, it holds no credential."""
    def __init__(self, counter, label):
        self.counter, self.label = counter, label

    def __call__(self, *args, **kwargs):
        self.counter.append(self.label)
        return self

    def env(self):
        return {}

    def clean(self, text, limit=300):
        return str(text)[:limit]

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class PodWorld(Exception):
    """Raised by a stand-in the pod path reaches first: proof that a launch went to the pod code."""


class FloorCase(Base):
    def setUp(self):
        super().setUp()
        self.m = sb.load_module("cloud_worker").bind(vars(sb))
        self.floor = self.m.cloud_floor
        self.fake = FakeFloor(self.floor)
        self.state = self.home / "state"
        self.state.mkdir()
        self.audits, self.pod_calls, self.notes, self.finals = [], [], [], []
        self.brief, self.kind = BRIEF, ("person", "operator")
        self.engine = mock.Mock()
        env = mock.patch.dict(os.environ, {"CW_ALLOWED_REPOS": "%s,%s" % (REPO_NAME, OTHER_REPO), "CW_REMOTE_BACKEND": "floor", "CW_FLOOR_CALLER": "fleet"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(setattr, self.floor, "TRANSPORT", self.floor.TRANSPORT)
        self.floor.TRANSPORT = self.fake
        launcher = lambda runner, verb, *a, **k: self.pod_calls.append(verb) or (0, {"pushed": True, "phase": "Running"}, "")
        patches = [mock.patch.dict(self.m.H, {"build_engine": lambda *a, **k: (self.engine, str(self.state), CONFIG), "intent_refusal": lambda *a: None,
                                              "state_dir_path": lambda: self.state, "ensure_state_dir": lambda: str(self.state),
                                              "gates_caller_kind": lambda *a: self.kind, "read_brief": lambda path, stdin: self.brief}),
                   mock.patch.object(self.m, "_audit", lambda engine, what, name, body=None: self.audits.append((what, name))),
                   mock.patch.object(self.m, "_registry_note", lambda engine, what, *a, **k: self.notes.append(what)),
                   mock.patch.object(self.m, "_final_message", lambda *a, **k: self.finals.append(a[3])),
                   mock.patch.object(self.m, "run_launcher", launcher), mock.patch.object(self.m, "Cluster", Quiet(self.pod_calls, "cluster")),
                   mock.patch.object(self.m, "tunnel", Quiet(self.pod_calls, "tunnel")),
                   mock.patch.object(self.m, "remote_v2", lambda *a: self._pod_v2()),
                   mock.patch.object(self.m, "_cluster_snapshot", lambda *a: self.pod_calls.append("snapshot") or {"pods": []}),
                   mock.patch.object(self.m, "_first_push", lambda: self._first_push())]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    # the pod side: counted, never real
    def _pod_v2(self):
        self.pod_calls.append("v2")
        v2 = mock.Mock()
        v2.message_list.return_value = []
        v2.prompt.return_value = {"id": "pod-msg"}
        return v2

    def _first_push(self):
        pod = self

        class First:
            def send_state(self, *a, **k):
                pod.pod_calls.append("state")
                return "state of pod worker", None
        return First()

    def args(self, name="f1", **over):
        base = dict(name=name, repo=REPO_NAME, branch="work/" + name, agent="coder", role=None, reports_to="lead", shape=None, issue=None, idle=None, hard=None,
                    brief_file="-", resume=False, headless=False, persistent=False, stack=False, no_commit=False, base=None, pr_label=None, charter=None, dir=None,
                    cloud=True)
        base.update(over)
        return argparse.Namespace(**base)

    def run_cmd(self, function, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = function(*args)
        return code, out.getvalue(), err.getvalue()

    def run_cmd_catching(self, function, *args):
        try:
            return self.run_cmd(function, *args)
        except (sb.LaunchError, self.m.CloudError) as err:
            return 1, "", str(err)

    def launch(self, name="f1", **over):
        return self.run_cmd(self.m.cmd_launch, self.args(name, **over), None, lambda: 1000.0)

    def send(self, name, text):
        record = self.m.route(name)
        return self.run_cmd(self.m.cmd_send, argparse.Namespace(name=name, issue=None, interrupt=False, text=text.split()), None, lambda: 1000.0, record)

    def read(self, name):
        return self.run_cmd(self.m.cmd_read, argparse.Namespace(name=name, limit=50, cloud_only=False), None, lambda: 1000.0)

    def retire(self, name):
        record = self.m.route(name)
        return self.run_cmd(self.m.cmd_retire, argparse.Namespace(name=name, undo=False, reason="done"), None, lambda: 1000.0, record)

    def status(self):
        return self.m.status_lines(None, str(self.state))

    def record(self, name):
        return json.loads(self.m.record_path(str(self.state), name).read_text())

    def pod_record(self, name, **over):
        record = {"worker": name, "url": "https://pod.example.internal", "runtime_id": "pod/" + name, "session": "ses_" + name, "hard_deadline": FUTURE,
                  "idle_deadline": FUTURE, "repo": REPO_NAME, "branch": "work/" + name, "agent": "coder"}
        record.update(over)
        self.m.save_record(str(self.state), record)
        return record

    def floor_calls(self):
        return len(self.fake.calls)


class TestCloudFloor(FloorCase):

    def test_a_launch_makes_one_work_and_a_repeat_returns_it(self):
        code, out, err = self.launch()
        self.assertEqual(code, 0, err)
        line = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((line["name"], line["cloud"], line["runtime_id"].startswith("floor/")), ("f1", True, True))
        record = self.record("f1")
        self.assertEqual((record["backend"], record["seq"], len(self.fake.works)), ("floor", 1, 1))
        self.assertEqual(record["work_id"], next(iter(self.fake.works)))
        first_key, before = self.fake.submitted["cmd_id"], self.m.record_path(str(self.state), "f1").read_bytes()
        code, out, err = self.launch()
        self.assertEqual(code, 0, err)
        self.assertEqual((self.fake.submitted["cmd_id"], len(self.fake.works), self.fake.calls.count("submit")), (first_key, 1, 2))
        self.assertEqual(self.m.record_path(str(self.state), "f1").read_bytes(), before)
        self.assertEqual(self.pod_calls, [])

    def test_a_launch_that_lost_its_record_finds_the_same_work(self):
        with mock.patch.object(self.m, "save_record", side_effect=OSError("disk")):
            with self.assertRaises(sb.LaunchError):
                self.launch()
        self.assertEqual(len(self.fake.works), 1)
        self.assertEqual(self.launch()[0], 0)
        self.assertEqual((len(self.fake.works), self.record("f1")["work_id"]), (1, next(iter(self.fake.works))))

    def test_sends_are_ordered_and_a_retry_is_the_same_command(self):
        self.launch()
        work = next(iter(self.fake.works.values()))
        self.assertEqual(self.send("f1", "first note")[0], 0)
        with mock.patch.object(self.m, "save_record", side_effect=OSError("disk")):   # accepted by Floor, record not moved
            with self.assertRaises(OSError):
                self.send("f1", "second note")
        self.assertEqual(self.record("f1")["seq"], 2)
        self.assertEqual(self.send("f1", "second note")[0], 0)   # the same seq, the same key: not run twice
        self.assertEqual([(m["seq"], m["text"]) for m in work["messages"]], [(2, "first note"), (3, "second note")])
        self.assertEqual(self.record("f1")["seq"], 3)
        self.assertEqual(self.pod_calls, [])
        self.assertEqual(self.audits.count(("cloud.send", "f1")), 2)

    def test_read_and_status_show_the_work(self):
        self.launch()
        self.send("f1", "hello there")
        self.fake.landed = "c" * 40
        code, out, err = self.read("f1")
        self.assertEqual(code, 0, err)
        self.assertIn("user: hello there", out)
        self.assertIn("landed_sha " + "c" * 40, out)
        lines, rows = self.status()
        self.assertTrue(any("landed_sha " + "c" * 40 in l and "busy True" in l and "rounds 2" in l for l in lines), lines)
        self.assertEqual([r["landed_sha"] for r in rows if r["worker"] == "f1"], ["c" * 40])
        self.assertEqual(self.pod_calls, [])

    def test_retire_waits_for_custody_then_removes_the_record(self):
        self.launch()
        self.fake.reserved = True
        code, out, err = self.retire("f1")
        self.assertEqual(code, self.m.H["EXIT_ERROR"])
        self.assertIn("waiting for heavy-job custody", out)
        self.assertTrue(self.m.record_path(str(self.state), "f1").exists())
        self.assertEqual(self.finals, [])
        self.fake.reserved = False
        code, out, err = self.retire("f1")
        self.assertEqual(code, 0, err)
        self.assertIn("f1 is retired (Floor session ses_", out)
        self.assertFalse(self.m.record_path(str(self.state), "f1").exists())
        self.assertEqual((self.finals, self.notes.count("retire"), self.pod_calls), (["f1"], 1, []))
        self.assertEqual(len(self.fake.keys), 2)   # one launch, one cancel: the wait asked the same command again

    def test_retire_by_a_stranger_is_refused_and_asks_nothing(self):
        self.launch(reports_to="lead")
        self.kind = ("agent", "other")
        with self.assertRaises(sb.LaunchError):
            self.retire("f1")
        self.assertNotIn("cancel", self.fake.calls)
        self.kind = ("agent", "lead")
        self.assertEqual(self.retire("f1")[0], 0)

    def test_a_local_launch_and_read_make_no_floor_call(self):
        with self.assertRaises(sb.LaunchError):   # --cloud with --headless is the old refusal, in its words
            self.launch("h1", headless=True)
        self.engine.discover.return_value.agents = {}
        with self.assertRaises(sb.LaunchError):   # a name with no record is the local read
            self.read("nobody")
        self.assertEqual((self.floor_calls(), self.pod_calls), (0, []))

    def test_an_outage_on_submit_writes_no_record(self):
        self.fake.down = True
        code, out, err = self.run_cmd_catching(self.m.cmd_launch, self.args("o1"), None, lambda: 1000.0)
        self.assertNotEqual(code, 0)
        self.assertEqual(list(Path(self.state).glob("cloud/*")), [])
        self.fake.down = False
        self.assertEqual(self.launch("o1")[0], 0)
        self.assertEqual(len(self.fake.works), 1)

    def test_an_unreadable_artifact_is_named_and_the_record_is_untouched(self):
        self.launch("a1")
        path = self.m.record_path(str(self.state), "a1")
        before = path.read_bytes()
        self.fake.bad_artifact = True
        with self.assertRaises(sb.LaunchError) as caught:
            self.read("a1")
        self.assertIn("art-9", str(caught.exception))
        lines, rows = self.status()
        self.assertTrue(any("art-9" in line for line in lines), lines)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.pod_calls, [])

    def test_the_http_client_over_loopback(self):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                return None

            def _answer(self, code, body, headers=()):
                data = json.dumps(body).encode()
                self.send_response(code)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                seen.append((self.path, self.headers.get("Authorization")))
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path.endswith("/submissions"):
                    self._answer(503, {"error": "persistence down"})
                else:
                    self._answer(302, {}, [("Location", "http://127.0.0.1:1/x")])

            def do_GET(self):
                self._answer(503, {"error": "cannot read art-9"})
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        client = self.floor.HttpTransport("http://127.0.0.1:%d" % server.server_port, "tok-PLANTED-0123456789")
        with self.assertRaises(self.floor.FloorError) as caught:
            client.submit("fleet", "c-1", session_id="s")
        self.assertEqual(caught.exception.status, 503)
        with self.assertRaises(self.floor.UnavailableArtifactError) as caught:
            client.observe("fleet", "w-1")
        self.assertEqual(caught.exception.artifact, "art-9")
        with self.assertRaises(self.floor.FloorError) as caught:
            client.cancel("fleet", "w-1", "c-2", 2)
        self.assertEqual(caught.exception.status, 302)   # a redirect is never followed
        self.assertEqual([path for path, _ in seen], ["/v1/callers/fleet/submissions", "/v1/callers/fleet/works/w-1/cancel"])
        self.assertTrue(all(auth == "Bearer tok-PLANTED-0123456789" for _, auth in seen))




class TestCloudFloorCutover(FloorCase):

    def test_the_route_rule(self):
        route = self.floor.route_repo
        cases = [({}, "pod"), ({"CW_REMOTE_BACKEND": "floor"}, "floor"), ({"CW_REMOTE_BACKEND": " Pod "}, "pod"),
                 ({"CW_REMOTE_BACKEND": "pod", "CW_FLOOR_REPOS": "*"}, "pod"), ({"CW_FLOOR_REPOS": "acme-org/*"}, "floor"),
                 ({"CW_FLOOR_REPOS": "acme-org/other,ACME-ORG/Widget-Service"}, "floor"), ({"CW_FLOOR_REPOS": "acme-org/other"}, "pod"),
                 ({"CW_FLOOR_REPOS": "*"}, "floor"), ({"CW_FLOOR_REPOS": ""}, "pod")]
        for env, want in cases:
            self.assertEqual(route(REPO_NAME, CONFIG, env), want, env)

    def test_a_worker_stays_on_the_side_that_admitted_it(self):
        os.environ["CW_REMOTE_BACKEND"] = "pod"
        self.pod_record("p1")
        os.environ["CW_REMOTE_BACKEND"] = "floor"
        for name in ("f1", "f2"):
            self.assertEqual(self.launch(name)[0], 0)
        os.environ["CW_REMOTE_BACKEND"] = "pod"   # the rollback switch: the Floor workers are still Floor's
        with mock.patch.dict(self.m.H, {"launch_model_cloud": mock.Mock(side_effect=PodWorld)}):
            with self.assertRaises(PodWorld):   # a new admission goes to the pod code
                self.launch("p2")
        self.pod_record("p2")
        self.assertEqual(len(self.fake.works), 2)
        self.fake.calls.clear()
        self.pod_calls.clear()
        for name in ("f1", "f2"):
            self.assertEqual(self.send(name, "go on")[0], 0)
            self.assertEqual(self.read(name)[0], 0)
        self.assertEqual(self.retire("f1")[0], 0)
        self.assertEqual(self.pod_calls, [], "a Floor worker never reaches the pod launcher")
        self.fake.calls.clear()
        for name in ("p1", "p2"):
            self.assertEqual(self.send(name, "go on")[0], 0)
            self.assertEqual(self.read(name)[0], 0)
            self.assertEqual(self.retire(name)[0], 0)
        self.assertEqual(self.fake.calls, [], "a pod worker never reaches Floor")
        self.assertIn("stop", self.pod_calls)
        os.environ["CW_REMOTE_BACKEND"] = "floor"   # flipping again changes nothing for the one that is left
        self.assertEqual(self.send("f2", "still here")[0], 0)
        self.assertEqual(self.record("f2")["seq"], 3)

    def test_status_gives_the_launcher_only_the_pod_records(self):
        self.pod_record("p1")
        self.launch("f1")
        lines, rows = self.status()
        self.assertEqual(sorted(r["worker"] for r in rows), ["f1", "p1"])
        self.assertEqual(self.pod_calls.count("snapshot"), 1)
        self.assertEqual(self.pod_calls.count("status"), 1)   # one pod record, one launcher status
        self.pod_calls.clear()
        self.m.remove_record(str(self.state), "p1")
        self.status()
        self.assertEqual(self.pod_calls, [])

    def test_every_eligible_admission_goes_to_floor_and_none_to_the_launcher(self):
        for index, agent in enumerate(["coder", "worker", "coder", "worker"]):
            self.assertEqual(self.launch("e%d" % index, agent=agent)[0], 0)
        self.assertEqual((len(self.fake.works), self.pod_calls.count("create"), self.pod_calls), (4, 0, []))
        self.assertEqual({self.record("e%d" % i)["backend"] for i in range(4)}, {"floor"})

    def test_the_pod_launcher_retires_only_when_closed_and_drained(self):
        state, retire = str(self.state), self.floor.retire_pod_launcher
        self.pod_record("p1")
        self.launch("f1")
        env = {"CW_REMOTE_BACKEND": "floor", "CW_POD_ROLLBACK_WINDOW": "open"}
        for change, word in (({}, "rollback window"), ({"CW_POD_ROLLBACK_WINDOW": "closed"}, "pod worker"),
                             ({"CW_REMOTE_BACKEND": "pod", "CW_POD_ROLLBACK_WINDOW": "closed"}, "not floor")):
            with self.assertRaises(self.m.Refusal) as caught:
                retire(state, dict(env, **change))
            self.assertIn(word, str(caught.exception))
        self.assertTrue(self.m.record_path(state, "p1").exists())   # nothing was changed
        self.assertEqual(self.retire("p1")[0], 0)
        self.assertEqual(self.floor.drain_check(state, ["f1"]), {"lost": 0, "twice": 0})
        self.assertEqual(retire(state, dict(env, CW_POD_ROLLBACK_WINDOW="closed"))["ready"], True)
        self.assertEqual(self.floor.drain_check(state, ["f1", "gone"]), {"lost": 1, "twice": 0})
        self.m.save_record(state, dict(self.record("f1"), worker="f9"))   # a second record claiming the same work
        self.assertEqual(self.floor.drain_check(state, ["f1"])["twice"], 1)




class TestCloudFloorRolesAndBriefs(FloorCase):

    def test_the_brief_loses_every_delivery_step_and_gains_the_contract(self):
        self.launch()
        sent = self.fake.submitted["brief"]
        self.assertTrue(sent.startswith("Floor Author Contract"))
        body = sent.split("Task:\n", 1)[1]
        self.assertNotRegex(body, r"git push|gh pr create|prove")
        self.assertIn("Add a retry to the fetch loop.", body)
        self.assertIn("keep the change small", body)
        self.assertEqual({k: self.fake.submitted[k] for k in ("repo", "branch", "title", "ref")}, {"repo": REPO_NAME, "branch": "work/f1", "title": "f1", "ref": "main"})
        pod_text = self.m.POD_HEADER + self.m.H["setup_with_land"](self.m.SETUP_PREAMBLE, CONFIG, REPO_NAME) % {"repo": REPO_NAME, "branch": "b"} + "Do the task.\n"
        translated = self.floor.translate_brief(pod_text, REPO_NAME, "b")
        self.assertNotIn("Setup (do this first", translated)
        self.assertNotIn("/work/land.log", translated)
        self.assertNotIn("nohup", translated)
        self.assertIn("Do the task.", translated)
        self.assertEqual(translated.count("Floor Author Contract"), 1)

    def test_the_matrix_refuses_what_floor_cannot_work(self):
        cases = [("reviewer", dict(role="reviewer"), BRIEF), ("tester", dict(role="tester"), BRIEF), ("no-commit", dict(no_commit=True), BRIEF),
                 ("rehearsal", {}, "REHEARSAL BRIEF\nmeasure it"), ("stack", dict(stack=True), BRIEF), ("docker", {}, "Do it\nNEEDS: docker\n"),
                 ("orchestrator", dict(agent="orchestrator"), BRIEF), ("persistent", dict(persistent=True), BRIEF),
                 ("other agent", dict(agent="scribe"), BRIEF), ("merger", {}, "Run: gh pr merge 12 --squash\n"),
                 ("not a land repo", dict(repo=OTHER_REPO), BRIEF)]
        for label, over, brief in cases:
            self.brief = brief
            code, out, err = self.run_cmd(self.m.cmd_launch, self.args("r1", **over), None, lambda: 1000.0)
            self.assertEqual(code, 2, label)
            self.assertIn("nothing was created", err, label)
            self.assertEqual((self.floor_calls(), self.pod_calls, list(Path(self.state).glob("cloud/*.json"))), (0, [], []), label)
        self.assertIn("pull requests and gates", err)

    def verify_launch(self, name, **over):
        return self.run_cmd(self.m.cmd_launch, self.args(name, **over), None, lambda: 1000.0)

    def test_verify_modes_are_refused_while_the_setting_is_off(self):
        for off in (None, "", "0", "false"):
            os.environ.pop("CW_FLOOR_VERIFY_ENABLED", None)
            if off is not None:
                os.environ["CW_FLOOR_VERIFY_ENABLED"] = off
            for label, over, brief in (("tester", dict(role="tester"), BRIEF), ("no-commit", dict(no_commit=True), BRIEF), ("rehearsal", {}, "REHEARSAL BRIEF\nmeasure it")):
                self.brief = brief
                code, out, err = self.verify_launch("v1", **over)
                self.assertEqual(code, 2, (off, label))
                self.assertIn("nothing was created", err)
                self.assertEqual((self.floor_calls(), self.pod_calls, list(Path(self.state).glob("cloud/*.json"))), (0, [], []), label)

    def test_verify_modes_are_routed_to_floor_when_the_setting_is_on_and_the_manifest_is_readable(self):
        os.environ["CW_FLOOR_VERIFY_ENABLED"] = "1"
        raw = "REHEARSAL BRIEF\nrun the checks: git push origin HEAD would be here"
        for name, over, brief, mode in (("v1", dict(role="tester", agent="scribe"), BRIEF, "verify-only"), ("v2", dict(no_commit=True), BRIEF, "rehearse"),
                                        ("v3", {}, raw, "rehearse")):
            self.brief = brief
            code, out, err = self.verify_launch(name, **over)
            self.assertEqual(code, 0, (name, err))
            sent = self.fake.submitted
            self.assertEqual((sent["mode"], sent["env"]), (mode, {"FLOOR_WORKFLOW": mode}), name)
            self.assertEqual(sent["brief"], brief, "a verify launch is not translated to author commits")
            self.assertNotIn("Floor Author Contract", sent["brief"])
            self.assertEqual(self.record(name)["mode"], mode)
            self.fake.manifest = {"mode": mode, "checks": [{"name": "unit", "ok": True}]}
            self.fake.log_tail = "step one\nstep two ok"
            code, out, err = self.read(name)
            self.assertEqual(code, 0, err)
            self.assertIn('manifest: {"checks": [{"name": "unit", "ok": true}], "mode": "%s"}' % mode, out)
            self.assertIn("log_tail: step two ok", out)
            self.assertIn("landed_sha None", out)
        self.assertEqual(self.pod_calls, [])
        os.environ["CW_FLOOR_VERIFY_ENABLED"] = "true"   # the other spelling
        self.brief = BRIEF
        self.assertEqual(self.verify_launch("v4", role="tester")[0], 0)
        for label, over, brief in (("reviewer", dict(role="reviewer"), BRIEF), ("stack", dict(role="tester", stack=True), BRIEF), ("not land", dict(role="tester", repo=OTHER_REPO), BRIEF)):
            self.brief = brief
            self.assertEqual(self.verify_launch("v9", **over)[0], 2, label)   # the rest of the matrix still holds

    def test_a_verify_launch_over_the_queue_names_its_mode(self):
        os.environ["CW_FLOOR_VERIFY_ENABLED"] = "1"
        mini = MiniFloor()
        self.addCleanup(mini.close)
        self.floor.TRANSPORT = None
        os.environ.update({"CW_FLOOR_QUEUE_URL": mini.base + "/q", "CW_FLOOR_QUEUE_SAS": MiniFloor.QUEUE_SAS, "CW_FLOOR_BLOB_URL": mini.base + "/b", "CW_FLOOR_BLOB_SAS": MiniFloor.BLOB_SAS})
        self.assertEqual(self.verify_launch("v5", role="tester")[0], 0)
        command = mini.commands[0]
        self.assertEqual((command["mode"], command["env"], command["brief"]), ("verify-only", {"FLOOR_WORKFLOW": "verify-only"}, BRIEF))
        sid = self.record("v5")["session"]
        mini.texts[sid] = {"text": "", "manifest": {"ok": True}, "log_tail": ["a", "b"]}
        code, out, err = self.read("v5")
        self.assertEqual(code, 0, err)
        self.assertIn('manifest: {"ok": true}', out)
        self.assertIn("log_tail: b", out)




class TestCloudFloorPublicSafe(FloorCase):

    def test_planted_private_values_never_reach_output_or_files(self):
        host, token = "floor-planted.corp.example", "tok-PLANTED-" + "z9" * 12
        token_file = self.home / "floor.token"
        token_file.write_text(token + "\n")
        os.chmod(str(token_file), 0o600)
        planted = ["CW_FLOOR_URL=https://%s/api" % host, "CW_FLOOR_TOKEN_FILE=%s" % token_file, "SAS=sv=2024&sig=Zm9vYmFy&se=2030"]
        settings = self.home / "cloud.env"
        settings.write_text("\n".join(planted) + "\n")
        os.chmod(str(settings), 0o600)
        os.environ["CW_FLOOR_URL"], os.environ["CW_FLOOR_TOKEN_FILE"] = "https://%s/api" % host, str(token_file)
        said = self.floor.scrub("POST https://%s/api/x failed: %s Bearer %s sig=Zm9vYmFy host %s" % (host, token, token, host))
        for secret in (host, token, "Zm9vYmFy", "https://"):
            self.assertNotIn(secret, said)
        os.environ.update({"CW_FLOOR_QUEUE_URL": "https://queue.example.internal/cmds", "CW_FLOOR_QUEUE_SAS": "?sv=2024&sig=QPLANTEDsig9",
                           "CW_FLOOR_BLOB_URL": "https://blob.example.internal/state", "CW_FLOOR_BLOB_SAS": "sv=2024&sig=BPLANTEDsig9"})
        said = self.floor.scrub("PUT https://queue.example.internal/cmds/messages?sv=2024&sig=QPLANTEDsig9 and blob.example.internal/state QPLANTEDsig9 BPLANTEDsig9")
        for secret in ("queue.example.internal", "blob.example.internal", "PLANTEDsig9", "https://"):
            self.assertNotIn(secret, said)
        self.fake.down = True
        _, _, err = self.run_cmd_catching(self.m.cmd_launch, self.args("u1"), None, lambda: 1000.0)
        self.assertNotIn(host, err)
        self.assertNotIn(token, err)
        shapes = r"azurecontainerapps\.io|postgres\.database\.azure\.com|core\.windows\.net|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
        for path in ("bin/cloud_floor.py", "docs/architecture/units/cloud_floor.py.row", "tests/switchboard/test_cloud_floor.py", "bin/cloud_inspect.py"):
            self.assertEqual(re.findall(shapes, (REPO / path).read_text()), [], path)

    def test_the_guard_tools_pass(self):
        for argv in (["tools/new-module-demands", "bin/cloud_floor.py"], ["tools/new-test-checks", "tests/switchboard/test_cloud_floor.py"]):
            done = subprocess.run([sys.executable] + argv, cwd=str(REPO), capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)





class MiniFloor:
    """Floor's side of the Queue + Blob v1 wire format on a loopback port: the queue takes base64 commands in a QueueMessage, applies each cmd_id once
    (a repeat is acked `duplicate`) and publishes state.json and msg/<session>.json; a payload blob is read back when a command names one."""
    QUEUE_SAS, BLOB_SAS = "sv=2024&sig=QUEUEsigPLANTED", "sv=2024&sig=BLOBsigPLANTED"

    def __init__(self):
        self.commands, self.sessions, self.acks, self.blobs, self.texts, self.paths = [], {}, {}, {}, {}, []
        self.down, self.hold, self.state_key = False, False, "seq"
        mini = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                return None

            def _send(self, code, body=b"", headers=()):
                self.send_response(code)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _split(self):
                path, _, query = self.path.partition("?")
                mini.paths.append(self.path)
                return path, query

            def do_POST(self):
                path, query = self._split()
                data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if query != mini.QUEUE_SAS or path != "/q/messages":
                    return self._send(403)
                if mini.down:
                    return self._send(503, b"queue down")
                text = re.search(rb"<MessageText>([A-Za-z0-9+/=]+)</MessageText>", data).group(1)
                mini.apply(json.loads(__import__("base64").b64decode(text)))
                self._send(201)

            def do_PUT(self):
                path, query = self._split()
                data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if query != mini.BLOB_SAS or self.headers.get("x-ms-blob-type") != "BlockBlob":
                    return self._send(403)
                mini.blobs[path[len("/b/"):]] = json.loads(data)
                self._send(201)

            def do_GET(self):
                path, query = self._split()
                if query != mini.BLOB_SAS:
                    return self._send(403)
                if path == "/b/state.json":
                    body = {"v": 1, "floor": "f1", mini.state_key: 7, "written_at": "2030-01-01T00:00:00Z", "sessions": mini.sessions, "acks": mini.acks}
                    return self._send(200, json.dumps(body).encode())
                name = path[len("/b/msg/"):-len(".json")]
                if path.startswith("/b/msg/") and name in mini.texts:
                    held = mini.texts[name]
                    body = {"v": 1, "session_id": name, "state": "idle", "text": held, "at": "2030-01-01T00:00:00Z", "round": 1}
                    body.update(held if isinstance(held, dict) else {})
                    return self._send(200, json.dumps(body).encode())
                self._send(404)
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.thread.join(5)

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.server.server_port

    def apply(self, command):
        self.commands.append(command)
        key, sid = command["cmd_id"], command["session_id"]
        if key in self.acks:
            self.acks[key] = "duplicate"
            return
        self.acks[key] = "applied"
        if command["op"] == "launch":
            brief = command.get("brief") if "brief" in command else self.blobs[command["payload"]]["brief"]
            self.sessions.setdefault(sid, {"state": "running", "busy": True, "last_update": "2030-01-01T00:00:00Z", "rounds": 2, "landed_sha": None, "error": None, "brief": brief})
        elif command["op"] == "retire" and not self.hold:
            self.sessions[sid]["state"] = "retired"


class TestCloudFloorQueueBlob(FloorCase):
    def setUp(self):
        super().setUp()
        self.mini = MiniFloor()
        self.addCleanup(self.mini.close)
        self.floor.TRANSPORT = None
        os.environ.update({"CW_FLOOR_QUEUE_URL": self.mini.base + "/q", "CW_FLOOR_QUEUE_SAS": "?" + MiniFloor.QUEUE_SAS,
                           "CW_FLOOR_BLOB_URL": self.mini.base + "/b", "CW_FLOOR_BLOB_SAS": MiniFloor.BLOB_SAS})
        for name in ("CW_FLOOR_TRANSPORT", "CW_FLOOR_URL"):
            os.environ.pop(name, None)

    def test_the_whole_life_of_a_worker_over_the_wire(self):
        self.assertEqual(self.launch()[0], 0)
        sid = self.record("f1")["session"]
        launch = self.mini.commands[0]
        self.assertEqual({k: launch[k] for k in ("v", "op", "session_id", "seq", "ref", "repo")},
                         {"v": 1, "op": "launch", "session_id": sid, "seq": 1, "ref": "main", "repo": "https://github.com/%s.git" % REPO_NAME})
        self.assertRegex(launch["cmd_id"], r"^c-[0-9a-f]{16}$")
        self.assertRegex(launch["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertTrue(launch["brief"].startswith("Floor Author Contract"))
        self.assertNotRegex(launch["brief"].split("Task:\n", 1)[1], r"git push|gh pr create")
        self.assertEqual(self.record("f1")["work_id"], sid)
        self.assertEqual(self.launch()[0], 0)   # the same command again: Floor acks it `duplicate`, there is one session
        self.assertEqual((len(self.mini.sessions), list(self.mini.acks.values())), (1, ["duplicate"]))
        self.assertEqual(self.send("f1", "please continue")[0], 0)
        send = self.mini.commands[-1]
        self.assertEqual((send["op"], send["seq"], send["text"], send["session_id"]), ("send", 2, "please continue", sid))
        self.mini.texts[sid], self.mini.sessions[sid]["landed_sha"] = "all done here", "d" * 40
        code, out, err = self.read("f1")
        self.assertEqual(code, 0, err)
        self.assertIn("assistant: all done here", out)
        self.assertIn("landed_sha " + "d" * 40, out)
        lines, rows = self.status()
        self.assertTrue(any("state running" in l and "d" * 40 in l and "rounds 2" in l for l in lines), lines)
        self.mini.hold = True   # the retire is sent but the session is not retired yet: custody is not confirmed
        code, out, err = self.retire("f1")
        self.assertEqual(code, self.m.H["EXIT_ERROR"])
        self.assertIn("waiting for heavy-job custody", out)
        retire = self.mini.commands[-1]
        self.assertEqual((retire["op"], retire["seq"]), ("retire", 3))
        self.assertNotIn("text", retire)
        self.mini.sessions[sid]["state"] = "retired"
        self.assertEqual(self.retire("f1")[0], 0)
        self.assertEqual(self.mini.commands[-1]["cmd_id"], retire["cmd_id"])   # the wait asked the same command again
        self.assertFalse(self.m.record_path(str(self.state), "f1").exists())
        for path in self.mini.paths:
            self.assertTrue(path.endswith(MiniFloor.QUEUE_SAS) or path.endswith(MiniFloor.BLOB_SAS), path)

    def test_a_large_brief_goes_in_a_payload_blob(self):
        self.brief = "Add the table.\n" + "x" * 70000
        self.assertEqual(self.launch()[0], 0)
        command = self.mini.commands[0]
        self.assertEqual(command["payload"], "payload/%s.json" % command["cmd_id"])
        self.assertNotIn("brief", command)
        blob = self.mini.blobs[command["payload"]]
        self.assertEqual(blob["env"], {})
        self.assertTrue(blob["brief"].startswith("Floor Author Contract") and blob["brief"].endswith("x\n"))
        self.assertEqual(next(iter(self.mini.sessions.values()))["brief"], blob["brief"])

    def test_state_may_name_its_counter_state_seq_and_a_missing_message_blob_is_empty(self):
        self.launch()
        self.mini.state_key = "state_seq"
        code, out, err = self.read("f1")
        self.assertEqual(code, 0, err)
        self.assertNotIn("assistant:", out)
        self.assertIn("state running", self.status()[0][-1])

    def test_a_queue_outage_writes_no_record_and_says_nothing_private(self):
        self.mini.down = True
        code, out, err = self.run_cmd_catching(self.m.cmd_launch, self.args("q1"), None, lambda: 1000.0)
        self.assertNotEqual(code, 0)
        self.assertEqual(list(Path(self.state).glob("cloud/*")), [])
        for secret in ("127.0.0.1", "sigPLANTED", MiniFloor.QUEUE_SAS):
            self.assertNotIn(secret, err)

    def test_the_transport_is_chosen_by_the_settings(self):
        pick = self.floor.transport
        self.assertIsInstance(pick(), self.floor.QueueBlobTransport)
        os.environ["CW_FLOOR_URL"] = "https://floor.example.internal"
        self.assertIsInstance(pick(), self.floor.QueueBlobTransport)   # both URLs set: the queue is the default
        os.environ["CW_FLOOR_TRANSPORT"] = "http"
        self.assertIsInstance(pick(), self.floor.HttpTransport)
        del os.environ["CW_FLOOR_TRANSPORT"], os.environ["CW_FLOOR_QUEUE_URL"]
        self.assertIsInstance(pick(), self.floor.HttpTransport)   # no queue URL: http
        os.environ["CW_FLOOR_TRANSPORT"] = "queue"
        with self.assertRaises(self.m.Refusal):
            pick()
        os.environ["CW_FLOOR_TRANSPORT"] = "pigeon"
        with self.assertRaises(self.m.Refusal):
            pick()
        self.floor.TRANSPORT = self.fake   # the injectable object wins over every setting
        self.assertIs(pick(), self.fake)


class TestCloudFloorMutants(Base):
    """Each mutant is a change of bin/cloud_floor.py made in memory; the test that should catch it runs under it and must fail, and runs first without it."""
    def failed(self, test, **patches):
        floor = sb.load_module("cloud_worker").bind(vars(sb)).cloud_floor
        result = unittest.TestResult()
        with contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(floor, name, value))
            owner = next(c for c in (TestCloudFloor, TestCloudFloorCutover, TestCloudFloorRolesAndBriefs, TestCloudFloorPublicSafe) if test in vars(c))
            owner(test).run(result)
        return bool(result.failures or result.errors)

    def check(self, test, **patches):
        self.assertFalse(self.failed(test), "the control passes: " + test)
        self.assertTrue(self.failed(test, **patches), "the mutant is caught by " + test)

    def test_m1_the_brief_is_not_translated(self):
        self.check("test_the_brief_loses_every_delivery_step_and_gains_the_contract", translate_brief=lambda brief, repo, branch, base=None: brief)

    def test_m1_the_send_key_drops_the_seq(self):
        self.check("test_sends_are_ordered_and_a_retry_is_the_same_command", send_key=lambda session, seq, text: "c-fixed")

    def test_m2_the_side_is_picked_by_the_setting_of_the_day(self):
        self.check("test_a_worker_stays_on_the_side_that_admitted_it",
                   is_floor=lambda record: isinstance(record, dict) and os.environ.get("CW_REMOTE_BACKEND") == "floor")

    def test_m3_the_matrix_lets_everything_through(self):
        self.check("test_the_matrix_refuses_what_floor_cannot_work", check_floor_admission=lambda *a, **k: None)

    def test_m6_the_verify_setting_is_ignored(self):
        self.check("test_verify_modes_are_refused_while_the_setting_is_off", verify_enabled=lambda env=None: True)

    def test_m5_a_failed_submit_is_swallowed(self):
        self.check("test_an_outage_on_submit_writes_no_record", submit_work=lambda who, cmd_id, **fields: {"work_id": "w-lost"})


if __name__ == "__main__":
    unittest.main()
