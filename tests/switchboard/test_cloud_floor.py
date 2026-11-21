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
import plistlib
import re
import signal
import subprocess
import sys
import threading
import types
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


def _sp_run(cmd):
    import subprocess
    return subprocess.run(cmd, capture_output=True, text=True).stdout


class TestCloudFloor(FloorCase):

    def test_the_relay_is_off_without_a_url_and_push_pane_state_reads_commands(self):
        self.assertIsNone(self.floor.relay_cli_if_configured("send", {"to": "a"}, env={}))
        with mock.patch.object(self.floor, "call_switchboard_v1", return_value={"commands": [{"action": "resume", "agent": "w"}]}):
            self.assertEqual(self.floor.push_pane_state({}, env={}, now=1), [{"action": "resume", "agent": "w"}])
        with mock.patch.object(self.floor, "call_switchboard_v1", return_value=None):
            self.assertEqual(self.floor.push_pane_state({}, env={}, now=1), [])

    def test_a_stayed_name_that_is_missing_matches_nothing(self):
        self.assertIs(self.floor._stayed_name("Refusal"), self.floor.D["Refusal"])
        self.assertEqual(self.floor._stayed_name("NoSuchName"), ())

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

    def test_class_defaults_to_production_and_background_is_recorded(self):
        self.assertEqual(self.launch("c0")[0], 0)
        self.assertNotIn("priority_class", self.fake.submitted)
        self.assertEqual(self.record("c0")["priority_class"], "production")
        self.assertEqual(self.launch("c1", priority_class="background")[0], 0)
        self.assertEqual(self.fake.submitted["priority_class"], "background")
        self.assertEqual(self.record("c1")["priority_class"], "background")

    def test_non_descriptive_worker_name_prints_a_warning(self):
        code, out, err = self.launch("fks1011")
        self.assertIn("<repo-short>-<issue>-<slug>", err)
        code, out, err = self.launch("fk-1011-orders-in-context")
        self.assertNotIn("<repo-short>-<issue>-<slug>", err)

    def test_outage_is_for_cos_or_a_person_only(self):
        self.kind = ("agent", "widget-lead", "orchestrator")
        self.assertEqual(self.launch("o1", priority_class="outage")[0], 2)
        self.assertIn(("cloud.outage_refused", "o1"), self.audits)
        self.assertEqual(self.fake.works, {})
        for index, kind in enumerate((("agent", "cos", "chief-of-staff"), ("person", "a person at a shell"))):
            self.kind = kind
            name = "o%d" % (index + 2)
            self.assertEqual(self.launch(name, priority_class="outage")[0], 0)
            self.assertIn(("cloud.outage_allowed", name), self.audits)
            self.assertEqual(self.fake.submitted["priority_class"], "outage")

    def test_preview_is_only_for_the_preview_repository(self):
        with mock.patch.dict(os.environ, {"CW_FLOOR_PREVIEW_REPO": REPO_NAME}):
            self.assertEqual(self.launch("v1", preview=True)[0], 0)
            self.assertTrue(self.fake.submitted["preview"])
            self.assertTrue(self.record("v1")["preview"])
            self.assertEqual(self.launch("v2", preview=True, repo=OTHER_REPO)[0], 2)
        self.assertEqual(len(self.fake.works), 1)

    def test_a_closed_rollback_window_refuses_a_pod_launch(self):
        with mock.patch.dict(os.environ, {"CW_REMOTE_BACKEND": "pod", "CW_POD_ROLLBACK_WINDOW": "closed"}):
            code, out, err = self.launch("r1")
        self.assertEqual(code, 2)
        self.assertIn("Floor", err)
        self.assertEqual((self.pod_calls, self.fake.works), ([], {}))

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
        for tool, target in (("new-module-" + "demands", "bin/cloud_floor.py"), ("new-test-" + "checks", "tests/switchboard/test_cloud_floor.py")):
            done = subprocess.run([sys.executable, "tools/" + tool, target], cwd=str(REPO), capture_output=True, text=True, timeout=120)
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
        self.assertEqual(launch["project_id"], "fleetkit")
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
        self.assertEqual(blob["project_id"], "fleetkit")
        self.assertEqual(command["project_id"], "fleetkit")
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


class TestCloudAgents(FloorCase):
    SECRET = "s3cr3t"
    HEADER = "agent=service-ops;session=ses_0123456789abcdef;charter=acme-org/widget-service#10;secret=s3cr3t"

    def call(self, method, path, body=None, header=None, **kw):
        headers = {"X-Switchboard-Auth": self.HEADER if header is None else header}
        raw = json.dumps(body).encode() if body is not None else b""
        return self.floor.handle_switchboard_v1(method, path, headers, raw, self.SECRET, **kw)

    def sb_refusal(self):
        return self.m.D["Refusal"] if "D" in vars(self.m) else self.floor.D["Refusal"]

    def test_persisted_mode_authority(self):
        import tempfile
        cf = self.floor
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "switchboard-mode.json").write_text('{"mode": "active"}')
            self.assertEqual(cf.cluster_switchboard_mode(tmp, env={}), "active")
            self.assertEqual(cf.cluster_switchboard_mode(tmp, env={"SWITCHBOARD_MODE": "active"}), "active")
            with self.assertRaises(cf.FloorError):
                cf.cluster_switchboard_mode(tmp, env={"SWITCHBOARD_MODE": "shadow"})

    def test_switchboard_manifest_and_pane_rollback(self):
        import ast
        text = (REPO / "cloud/k8s/switchboard.yaml").read_text()
        body = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
        decoder, pos, docs = json.JSONDecoder(), 0, []
        while pos < len(body):
            if body[pos].isspace() or body[pos:pos + 3] == "---":
                pos += 3 if body[pos:pos + 3] == "---" else 1
                continue
            obj, pos = decoder.raw_decode(body, pos)
            docs.append(obj)
        deployment = next(d for d in docs if d.get("kind") == "Deployment")
        container = next(c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "switchboard")
        self.assertEqual(container.get("command"), ["python3", "/run/home/app/tools/in-cluster-switchboard-server"])
        env_map = {e["name"]: e.get("value") for e in container.get("env", [])}
        self.assertEqual(env_map.get("SWITCHBOARD_MODE"), "active")
        server = REPO / "tools/in-cluster-switchboard-server"
        self.assertTrue(server.is_file())
        ast.parse(server.read_text())
        import importlib.machinery, importlib.util, tempfile
        loader = importlib.machinery.SourceFileLoader("ics_pin_test", str(server))
        spec = importlib.util.spec_from_loader("ics_pin_test", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        import re as _re
        pinned_val = _re.search(r'"name":\s*"SWITCHBOARD_PINNED_SHA",\s*"value":\s*"([^"]*)"', text).group(1)
        self.assertEqual(mod.compute_code_tree_hash(REPO), pinned_val)
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "CODE_SHA").write_text("a" * 40 + "\n")
            mod.verify_pinned_code_sha(td, {"SWITCHBOARD_PINNED_SHA": "a" * 40})
            with self.assertRaises(SystemExit) as cm:
                mod.verify_pinned_code_sha(td, {"SWITCHBOARD_PINNED_SHA": "UNPINNED"})
            self.assertEqual(cm.exception.code, 2)
            (Path(td) / "switchboard-mode.json").write_text('{"mode": "active"}')
            with self.assertRaises(SystemExit) as cm:
                mod.verify_mode_agreement(td, {"SWITCHBOARD_MODE": "shadow"})
            self.assertEqual(cm.exception.code, 2)
            self.assertEqual(mod.verify_mode_agreement(td, {"SWITCHBOARD_MODE": "active"}), "active")
            import re as _re
            self.assertRegex(_re.search(r'"SWITCHBOARD_PINNED_SHA",\s*"value": "([^"]*)"', (REPO / "cloud/k8s/switchboard.yaml").read_text()).group(1), r"^[0-9a-f]{40}$")
            with self.assertRaises(SystemExit) as cm:
                mod.verify_pinned_code_sha(td, {"SWITCHBOARD_PINNED_SHA": "b" * 40})
            self.assertEqual(cm.exception.code, 2)
        sync = REPO / "tools/sync-in-cluster-switchboard"
        self.assertTrue(sync.is_file())
        ast.parse(sync.read_text())
        sl = importlib.machinery.SourceFileLoader("sync_pin_test", str(sync))
        sm = importlib.util.module_from_spec(importlib.util.spec_from_loader("sync_pin_test", sl))
        sl.exec_module(sm)
        for path in ("docs", "agents", "plugins", "skills", "templates"):
            self.assertIn(path, sm.PATHS)
        self.assertIn("cloud/launcher", sm.PATHS)
        snap = {"agents": [{"agent_session": "ses_example"}, {"agent_session": {"value": "ses_live2", "kind": "id"}}],
                "sessions": {"ses_live2": {"metadata": {"fleet": {"repo": "acme-org/live-service"}}},
                             "ses_example": {"metadata": {"fleet": {"repo": "acme-org/widget-service"}}},
                             "ses_other": {"metadata": {"fleet": {"repo": "acme-org/floor-service"}}},
                             "ses_example2": {"metadata": {"fleet": {"repo": "other-org/widget-api"}}}}}
        self.assertEqual(sm.watched_repos_from_snapshot(snap, {"gh_users": {"acme-org/*": "acct"}}), ["acme-org/live-service", "acme-org/widget-service"])
        self.assertEqual(sm.watched_repos_from_snapshot({"agents": [{"agent_session": 123}], "sessions": snap["sessions"]}, {"gh_users": {"acme-org/*": "acct"}}), [])
        import urllib.error as _ue
        from unittest import mock as _mock
        err404 = _ue.HTTPError("u", 404, "Not Found", {}, None)
        with _mock.patch.object(mod.urllib.request, "urlopen", side_effect=err404):
            cp = mod.inprocess_gh_api(["gh", "api", "repos/acme-org/floor-service/pulls?state=closed&per_page=100"], {"GH_TOKEN": "x"})
        self.assertEqual((cp.returncode, cp.stdout), (0, "[]\n"))
        import tempfile as _tf, shutil as _sh, json as _js
        captured = []
        sm.kubectl_out = lambda args: captured.append(args[-1]) or ""
        sm.check_watched_repos_readable("pod")
        with _tf.TemporaryDirectory() as home:
            (Path(home) / "app/tools").mkdir(parents=True)
            _sh.copy(server, Path(home) / "app/tools/in-cluster-switchboard-server")
            (Path(home) / ".config/fleet-switchboard").mkdir(parents=True)
            (Path(home) / ".config/fleet-switchboard/config.json").write_text(_js.dumps(
                {"gh_users": {"acme-org/*": "acct"}, "github_repos": []}))
            (Path(home) / "fleet_agents_snapshot.json").write_text(_js.dumps(snap))
            import subprocess as _s2, sys as _sys
            out = _s2.run([_sys.executable, "-c", captured[0].replace("/run/home", home)], capture_output=True, text=True).stdout
        self.assertIn("acme-org/widget-service watched by a live agent but not in github_repos", out)
        self.assertIn("has no token for account acct", out)
        sync_text = sync.read_text()
        self.assertIn('APP + ".stage"', sync_text)
        self.assertNotIn("rm -rf bin tools shims docs", sync_text)
        import subprocess as _sp
        with tempfile.TemporaryDirectory() as td:
            app = Path(td) / "app"
            (app / "docs").mkdir(parents=True)
            (app / "docs" / "old.txt").write_text("old")
            (Path(td) / "app.stage" / "tools").mkdir(parents=True)
            (Path(td) / "app.stage" / "tools" / "in-cluster-switchboard-server").write_text("new")
            script = sm.swap_script("t" * 40, "shadow", str(app), str(Path(td) / "st" / "mode.json"), str(Path(td) / "copy.py"))
            _sp.run(["sh", "-c", script], check=True)
            self.assertTrue((app / "tools" / "in-cluster-switchboard-server").is_file())
            self.assertEqual((app / "CODE_SHA").read_text(), "t" * 40)
            self.assertFalse((Path(td) / "app.old").exists())
            self.assertFalse((Path(td) / "app.stage").exists())
        with tempfile.TemporaryDirectory() as cfgd:
            cfgp = Path(cfgd) / "config.json"
            self.assertEqual(sm.cluster_config_bytes(cfgp), b"")
            self.assertEqual(sm.compute_config_hash(cfgp), "")
            cfgp.write_text(json.dumps({"herdr": "/opt/herdr", "opencode": "/usr/bin/oc", "external_decisions": ["x"], "k": 1}))
            cfg_out = json.loads(sm.cluster_config_bytes(cfgp))
            self.assertEqual(cfg_out["herdr"], "/bin/true")
            self.assertEqual(cfg_out["opencode"], "/bin/true")
            self.assertNotIn("external_decisions", cfg_out)
            self.assertEqual(cfg_out["k"], 1)
            import hashlib as _hl
            self.assertEqual(sm.compute_config_hash(cfgp), _hl.sha256(sm.cluster_config_bytes(cfgp)).hexdigest()[:40])
            cfgp.write_text(json.dumps({"k": 1, "opencode": "/x", "herdr": "/y"}))
            self.assertEqual(sm.compute_config_hash(cfgp), _hl.sha256(sm.cluster_config_bytes(cfgp)).hexdigest()[:40])
            self.assertRegex(sm.compute_config_hash(cfgp), r"^[0-9a-f]{40}$")
        h = "a" * 40
        self.assertEqual(sm.check_shas(h, h, ""), "")
        self.assertNotEqual(sm.check_shas(h, h, "UNPINNED"), "")
        self.assertNotEqual(sm.check_shas(h, "b" * 40, ""), "")
        self.assertNotEqual(sm.check_shas(h, h, "c" * 40), "")
        self.assertNotEqual(sm.check_shas("abc", "abc", ""), "")

    def test_pane_state_is_applied_to_discovery(self):
        obs = types.SimpleNamespace(status="idle", errored=False, turn_error="", turn_error_id=None, idle_at=None, permission_waiting=False, form_waiting=False)
        discovery = types.SimpleNamespace(agents={"widget-api": obs})
        state = {"widget-api": {"status": "blocked", "errored": True, "turn_error": "stream error", "turn_error_id": "m1", "permission_waiting": True}, "bad": 3}
        self.floor.apply_pane_state_to_discovery(state, discovery)
        self.assertEqual((obs.status, obs.errored, obs.turn_error_id, obs.permission_waiting, obs.form_waiting), ("blocked", True, "m1", True, False))
        self.assertIs(self.floor.apply_pane_state_to_discovery(None, discovery), discovery)

    def test_pane_resume_command_is_queued(self):
        state = {}
        cmd = self.floor.queue_pane_resume_command(state, "widget-api", "turn ended")
        self.assertEqual(cmd["action"], "resume")
        self.assertEqual(state["pane_commands"], [cmd])
        self.assertEqual(state["pane_outbox"], [cmd])

    def test_blocked_kind_reads_a_relayed_permission(self):
        engine = types.SimpleNamespace(sources=[], pane_command_sink=None)
        obs = types.SimpleNamespace(name="widget-api", session_id="ses_example", permission_waiting=True, form_waiting=False, errored=False)
        self.assertIn("permission request", sb.blocked_kind(engine, obs))

    def test_blocked_kind_with_a_sink_reads_the_ended_turn(self):
        engine = types.SimpleNamespace(sources=[], pane_command_sink=lambda *a: None)
        obs = types.SimpleNamespace(name="widget-api", session_id="ses_example", permission_waiting=False, form_waiting=False, errored=False)
        self.assertNotIn("not read", sb.blocked_kind(engine, obs))

    def test_auth_binds_the_header_identity(self):
        auth = self.floor.verify_switchboard_auth(self.HEADER, self.SECRET)
        self.assertEqual((auth["agent"], auth["session"], auth["charter"]), ("service-ops", "ses_0123456789abcdef", "acme-org/widget-service#10"))
        for bad in (None, "", self.HEADER.replace("s3cr3t", "nope"), self.HEADER.replace("ses_0123456789abcdef", "forged"), "agent=cos;session=forged;secret=forged"):
            with self.assertRaises(self.floor.FloorError) as ctx:
                self.floor.verify_switchboard_auth(bad, self.SECRET)
            self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(self.call("GET", "/v1/pane-outbox", header="x")[0], 401)

    def test_intent_and_decisions_are_recorded_and_listed(self):
        state = {}
        code, out = self.call("POST", "/v1/intent", {"action": "set", "issue": "acme-org/widget-service#2", "text": "ship it", "from": "service-ops"}, state=state)
        self.assertEqual(code, 200)
        self.assertEqual(out["intents"][0]["from"], "service-ops")
        self.assertEqual(self.call("GET", "/v1/intent", state=state)[1]["intents"], state["intents"])
        code, out = self.call("POST", "/v1/decisions", {"title": "Pick one", "rows": [{"a": 1}]}, state=state)
        self.assertEqual((code, out["recorded"]), (200, True))
        self.assertEqual(self.call("POST", "/v1/decisions", {"action": "list"}, state=state)[1]["decisions"][0]["title"], "Pick one")
        self.assertEqual(self.call("GET", "/v1/decisions", state={})[1], {"decisions": []})

    def test_ensure_switchboard_auth_file_fetches_into_a_private_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "relay.auth")
            env = {"CW_SWITCHBOARD_AUTH_FILE": path, "CW_SWITCHBOARD_KEYVAULT": "widget-vault"}
            seen = []
            def fetch(vault, name):
                seen.append((vault, name))
                return "agent=x;secret=y"
            self.assertEqual(self.floor.ensure_switchboard_auth_file(env, fetch), path)
            self.assertEqual(seen, [("widget-vault", "switchboard-relay-auth")])
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(self.floor.ensure_switchboard_auth_file(env, lambda *a: self.fail("refetched")), path)
        self.assertIsNone(self.floor.ensure_switchboard_auth_file({}, lambda *a: "x"))

    def test_a_body_sender_is_ignored_and_the_pane_outbox_drains(self):
        state = {}
        code, _ = self.call("POST", "/v1/send", {"to": "cos", "text": "hi", "sender": "cos", "from": "cos"}, state=state)
        self.assertEqual(code, 200)
        self.assertEqual([m["from"] for m in state["pane_outbox"]], ["service-ops"])
        code, out = self.call("GET", "/v1/pane-outbox", header=self.HEADER.replace("service-ops", "cos"), state=state)
        self.assertEqual([m["text"] for m in out["messages"]], ["hi"])
        self.call("POST", "/v1/pane-ack", {"ids": [m["id"] for m in out["messages"]]}, state=state, header=self.HEADER)
        self.assertEqual(len(state["pane_outbox"]), 1)   # another agent cannot ack it
        self.call("POST", "/v1/pane-ack", {"ids": [m["id"] for m in out["messages"]]}, state=state, header=self.HEADER.replace("service-ops", "cos"))
        self.assertEqual(state["pane_outbox"], [])

    def test_report_and_remind_are_attributed_to_the_header(self):
        state = {}
        self.assertEqual(self.call("POST", "/v1/report", {"status": "done", "text": "ok", "sender": "cos"}, state=state)[0], 200)
        self.assertEqual(self.call("POST", "/v1/remind", {"when": "2099-01-01T00:00:00Z", "text": "x"}, state=state)[0], 200)
        self.assertEqual((state["reports"][0]["from"], state["reminders"][0]["to"]), ("service-ops", "service-ops"))

    def test_report_accepts_state_and_rejects_an_unknown_one(self):
        state = {}
        self.assertEqual(self.call("POST", "/v1/report", {"state": "done", "text": "ok"}, state=state)[0], 200)
        self.assertEqual(state["reports"][0]["status"], "done")
        self.assertEqual(self.call("POST", "/v1/report", {"state": "bogus", "text": "ok"}, state=state)[0], 400)

    def test_an_artifact_gets_a_read_only_url(self):
        import base64
        got = []
        put = lambda session, name, data: got.append((session, name)) or "https://store.example.net/floor-artifacts/%s/%s?sp=r&sig=x" % (session, name)
        code, out = self.call("POST", "/v1/artifact", {"name": "a.txt", "data_b64": base64.b64encode(b"data").decode()}, artifact_put=put)
        self.assertEqual(code, 200)
        self.assertEqual(out["sha256"], hashlib.sha256(b"data").hexdigest())
        self.assertTrue(out["url"].startswith("https://") and "sp=r" in out["url"])
        self.assertEqual(got[0][0], "ses_0123456789abcdef")

    def test_agent_profiles_need_the_setting_and_roll_back(self):
        for over, mode in ((dict(role="research-assistant"), "research-assistant"), (dict(agent="orchestrator"), "orchestrator")):
            with mock.patch.dict(os.environ, {"CW_FLOOR_AGENTS_ENABLED": ""}):
                self.assertIsNone(self.floor.verify_mode(self.args("r1", **over), BRIEF))
            with mock.patch.dict(os.environ, {"CW_FLOOR_AGENTS_ENABLED": "1"}):
                self.assertEqual(self.floor.verify_mode(self.args("r1", **over), BRIEF), mode)
                self.floor.check_floor_admission(self.args("r1", **over), REPO_NAME, BRIEF, CONFIG)
        with mock.patch.dict(os.environ, {"CW_FLOOR_AGENTS_ENABLED": "1"}):
            for over in (dict(agent="orchestrator", stack=True), dict(role="research-assistant")):
                with self.assertRaises(self.sb_refusal()):
                    self.floor.check_floor_admission(self.args("r1", **over), REPO_NAME, "NEEDS: docker\n" if "role" in over else BRIEF, CONFIG)
            self.launch(name="o1", agent="orchestrator", role="orchestrator")
        self.assertEqual(self.fake.submitted["mode"], "orchestrator")
        self.assertEqual(self.fake.submitted["env"]["FLOOR_PROFILE"], "orchestrator")
        cancelled = []
        out = self.floor.rollback_to_pane(str(self.state), "o1", "1-2", cancel_fn=lambda rec: cancelled.append(rec["session"]) or {"cancelled": True})
        self.assertEqual(out, {"worker": "o1", "pane": "1-2", "rolled_back": True})
        self.assertEqual((len(cancelled), list(Path(self.state).glob("cloud/o1.json"))), (1, []))


class TestFloorAuthority(FloorCase):
    SECRET = "s3cr3t"

    def header(self, agent):
        return "agent=%s;session=ses_0123456789abcdef;secret=s3cr3t" % agent

    def call(self, path, body, agent="cos", **kw):
        return self.floor.handle_switchboard_v1("POST", path, {"X-Switchboard-Auth": self.header(agent)}, json.dumps(body).encode(), self.SECRET, **kw)

    def test_pane_state_observation_with_stayed_and_orchestrator_retire(self):
        worker = sb.load_module("cloud_worker").bind(vars(sb))
        with mock.patch.object(self.floor, "D", worker._Stayed()):
            disc = types.SimpleNamespace(agents={})
            self.floor.apply_pane_state_to_discovery({"widget-api": {"status": "idle", "pane": "%1"}}, disc)
            self.assertIn("widget-api", disc.agents)
        state = {}
        self.assertEqual(self.call("/v1/retire", {"worker": "widget-api"}, agent="service-ops", state=state)[0], 200)

    def test_cluster_mode_persists_across_restart_and_delivers_canary(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.call("/v1/mode", {"mode": "active"}, state={"state_dir": tmp}), (200, {"mode": "active"}))
            state2 = {"state_dir": tmp}
            got = self.floor.handle_switchboard_v1("GET", "/v1/mode", {"X-Switchboard-Auth": self.header("cos")}, b"", self.SECRET, state=state2)
            self.assertEqual(got, (200, {"mode": "active"}))
            self.assertEqual(self.call("/v1/send", {"to": "pane-relay", "text": "canary:t1", "canary": True}, agent="pane-relay", state=state2)[1], {"queued": "pane"})
            self.assertEqual(len(state2["pane_outbox"]), 1)
            state2["mode"] = "shadow"
            self.assertEqual(self.call("/v1/send", {"to": "pane-relay", "text": "canary:t2", "canary": True}, agent="pane-relay", state=state2)[1], {"queued": "pane"})
            self.assertEqual(len(state2["pane_outbox"]), 2)  # shadow queues the canary too, so it can be acked
            acked, synthetic = [], mock.Mock()
            engine = types.SimpleNamespace(v2=types.SimpleNamespace(synthetic=synthetic))
            msgs = list(state2["pane_outbox"])
            def fake_request(method, url, *a, **k):
                return 200, json.dumps({"messages": msgs}).encode()
            with mock.patch.object(self.floor, "switchboard_relay_config", return_value={"url": "http://x", "authority": "cluster"}), \
                    mock.patch.object(self.floor, "_auth_header", return_value="h"), mock.patch.object(self.floor, "_request", fake_request), \
                    mock.patch.object(self.floor, "call_switchboard_v1", lambda path, payload, env=None: acked.append(payload["ids"])):
                result = self.floor.drain_pane_outbox(engine)
            self.assertEqual(result["canaries"], ["canary:t1", "canary:t2"])
            self.assertEqual(acked, [[m["id"] for m in msgs]])
            synthetic.assert_not_called()

    def _single_owner_checks(self):
        import tempfile, importlib.machinery, importlib.util
        sent = []
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CW_SWITCHBOARD_URL": "http://x", "CW_SWITCHBOARD_AUTHORITY": "cluster"}
            with mock.patch.object(self.floor, "call_switchboard_v1", lambda path, payload, env=None: sent.append((path, payload)) or {"decisions_file": {"decisions": []}}):
                self.floor.sync_cloud_records_to_cluster(tmp, env=env)
            self.assertEqual(sent, [("/v1/cloud-sync", {"mirror_only": True})])
            self.assertEqual(sorted(os.listdir(tmp)), ["decisions.json"])
            wakes = []
            engine = types.SimpleNamespace(send=lambda *a, **k: wakes.append(a), audit=None, actor="x")
            rec = {"worker": "widget-api", "backend": "floor", "work_id": "w1", "session": "ses_example", "reports_to": "cos", "repo": "acme-org/widget-service"}
            held = types.SimpleNamespace(works={"w1": {"state": "done", "landed_sha": "abc"}})
            with mock.patch.object(self.floor, "transport", lambda: held), \
                    mock.patch.dict(self.floor.D, {"save_record": lambda *a: None}):
                self.floor.floor_watch_sync(tmp, [dict(rec)], engine)
                self.floor.floor_watch_sync(tmp, [dict(rec)], engine)
            self.assertEqual(len(wakes), 1)
            def boom(*a, **k):
                raise OSError("pane unreachable")
            with tempfile.TemporaryDirectory() as tmp2, mock.patch.object(self.floor, "transport", lambda: held), \
                    mock.patch.dict(self.floor.D, {"save_record": lambda *a: None}):
                self.floor.floor_watch_sync(tmp2, [dict(rec)], types.SimpleNamespace(send=boom, audit=None, actor="x"))
                self.floor.floor_watch_sync(tmp2, [dict(rec)], engine)
            self.assertEqual(len(wakes), 2)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "floor-terminal-wakes.json")))
            so = os.path.join(tmp, "so.md")
            Path(so).write_bytes(b"orders\n")
            loader = importlib.machinery.SourceFileLoader("sync_so_test", str(REPO / "tools/sync-in-cluster-switchboard"))
            sm = importlib.util.module_from_spec(importlib.util.spec_from_loader("sync_so_test", loader))
            loader.exec_module(sm)
            self.assertEqual(sm.standing_orders_bytes(so), b"orders\n")
            self.assertEqual(len(sm.compute_standing_orders_hash(so)), 40)
            self.assertEqual(sm.compute_standing_orders_hash(os.path.join(tmp, "none.md")), "")

    def test_every_state_writing_cli_command_from_parser_works_under_cluster_authority(self):
        import tempfile
        parser = sb.build_parser()
        subs = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices
        nested = {n: next((a for a in subs[n]._actions if isinstance(a, argparse._SubParsersAction)), None) for n in ("decisions", "orders")}
        self.assertTrue(all(nested.values()))
        self.assertTrue({"escalate", "resolve", "answer", "supersede", "batch"} <= set(nested["decisions"].choices))
        self.assertTrue({"add", "remove", "copy"} <= set(nested["orders"].choices))
        writers = {"send", "report", "remind", "retire", "orders", "decisions", "move", "reassign", "resume", "notice", "intent"}
        self.assertTrue(writers <= set(parser.commands))
        for name in ("orders", "decisions", "intent"):
            self.assertIn(name, self.floor.PASSTHROUGH_COMMANDS)
        self.assertNotIn("retire", self.floor.PASSTHROUGH_COMMANDS)
        self._single_owner_checks()
        pod_state = {"pane_outbox": [], "reports": [], "reminders": [], "acked": []}
        floor, case = self.floor, self

        class Pod(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                code, ans = floor.handle_switchboard_v1("POST", self.path, dict(self.headers), raw, case.SECRET, state=pod_state)
                data = json.dumps(ans).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        server = http.server.HTTPServer(("127.0.0.1", 0), Pod)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as tmp:
            auth = os.path.join(tmp, "auth")
            with open(auth, "w") as handle:
                handle.write(self.header("cos"))
            os.chmod(auth, 0o600)
            env = {"CW_SWITCHBOARD_URL": "http://127.0.0.1:%d" % server.server_port, "CW_SWITCHBOARD_AUTH_FILE": auth, "CW_SWITCHBOARD_AUTHORITY": "cluster"}
            ov_dir = os.path.join(tmp, "oversize-sends")
            os.makedirs(ov_dir)
            with open(os.path.join(ov_dir, "send-abc.txt"), "w") as handle:
                handle.write("long body")
            text = "[oversize send (9 chars) stored at %s/oversize-sends/send-abc.txt]" % tmp
            relay = lambda cmd, payload: self.floor.relay_cli_if_configured(cmd, payload, env=env)
            self.assertEqual(relay("send", {"sender": "cos", "to": "widget-api", "text": text})["queued"], "pane")
            self.assertEqual(pod_state["pane_outbox"][-1]["oversize_files"], {"send-abc.txt": "long body"})
            self.assertEqual(relay("report", {"sender": "cos", "state": "working", "text": "x"}), {"recorded": True})
            self.assertTrue(relay("remind", {"to": "cos", "when": 60, "text": "x"})["recorded"])
            self.assertTrue(relay("retire", {"name": "local-pane", "worker": "local-pane", "reason": "done", "undo": False})["retired"])
            self.assertEqual(pod_state["retires"][-1]["worker"], "local-pane")
            for action in ("add", "remove", "copy", None):
                self.assertTrue(relay("orders", {"action": action, "name": "widget-api"})["recorded"])
            self.assertEqual(len(pod_state["orders"]), 4)
            self.assertTrue(relay("decisions", {"action": "escalate", "id": "d1", "title": "t"})["recorded"])
            self.assertEqual(relay("decisions", {"action": "list"})["decisions"][0]["id"], "d1")
            # the one-shot passthrough lets the CLI's own local writer run once, then cluster authority returns
            saved = dict(os.environ)
            try:
                os.environ.update(env)
                self.floor._PASSTHROUGH_AUTH[:] = []
                self.assertTrue(isinstance(self.floor.relay_cli_if_configured("orders", {"action": "list"}), dict))
                self.assertEqual(self.floor.switchboard_authority(), "local")
                self.assertEqual(self.floor.switchboard_authority(), "cluster")
                for cmd, payload in (("decisions", {"action": "resolve", "id": "d1"}), ("intent", {"action": "add"})):
                    self.floor._PASSTHROUGH_AUTH[:] = []
                    os.environ.update(env)
                    self.assertTrue(isinstance(self.floor.relay_cli_if_configured(cmd, payload), dict))
                    self.assertEqual(self.floor.switchboard_authority(), "local")
            finally:
                os.environ.clear()
                os.environ.update(saved)
            # the CLI itself: the local GitHub/v2 writer runs after the cluster refresh, not just a 200
            pod_state["decisions_file"] = {"decisions": []}
            labels, comments, replies = [], [], []
            def fake_engine(runner, now, writable=False):
                eng = types.SimpleNamespace(
                    config={}, state_dir=tmp, actor="cos", audit=types.SimpleNamespace(append=lambda *a, **k: None),
                    now=lambda: 0, _note=lambda *a, **k: None,
                    v2=types.SimpleNamespace(permission_reply=lambda *a: replies.append(a)))
                import contextlib
                eng._external = lambda *a, **k: contextlib.nullcontext()
                rows = [{"id": "acme-org/widget-service#10", "kind": "issue", "tier": "cos", "repo": "acme-org/widget-service", "number": 10},
                        {"id": "acme-org/widget-service#11", "kind": "issue", "tier": "cos", "repo": "acme-org/widget-service", "number": 11},
                        {"id": "perm:ses_1:req_1", "kind": "permission", "tier": "cos", "session": "ses_1", "request": "req_1"}]
                return eng, rows, None
            saved = dict(os.environ)
            try:
                os.environ.update(env)
                with mock.patch.object(sb, "fresh_engine_decisions", fake_engine), \
                        mock.patch.object(sb, "decision_actor", lambda e, a: (None, None)), \
                        mock.patch.object(sb, "decision_or_persisted", lambda e, rows, i, a, n: next((r for r in rows if r["id"] == i), None)), \
                        mock.patch.object(sb, "_issue_target", lambda e, d: (d["repo"], d["number"])), \
                        mock.patch.object(sb, "refuse_human_tier", lambda *a: None), \
                        mock.patch.object(sb, "change_issue_labels", lambda e, r, repo, n, add, rem: labels.append((n, list(add), list(rem)))), \
                        mock.patch.object(sb, "comment_as", lambda e, r, repo, n, text, who: comments.append((n, text))), \
                        mock.patch.object(sb, "finish_decision_write", lambda *a: None), \
                        contextlib.redirect_stdout(io.StringIO()):
                    parser = sb.build_parser()
                    for argv in (["decisions", "escalate", "acme-org/widget-service#10", "--reason", "needs owner"],
                                 ["decisions", "resolve", "acme-org/widget-service#11", "--note", "covered by order"],
                                 ["decisions", "answer", "perm:ses_1:req_1", "allow", "--note", "safe"]):
                        self.floor._PASSTHROUGH_AUTH[:] = []
                        os.environ.update(env)
                        self.assertEqual(sb.cmd_decisions(parser.parse_args(argv), None, lambda: 0), 0)
            finally:
                os.environ.clear()
                os.environ.update(saved)
            self.assertEqual(labels[0][0], 10)
            self.assertTrue(any(l.endswith("awaiting-user") for l in labels[0][1]))
            self.assertTrue(any(l.endswith("awaiting-cos") for l in labels[0][2]))
            self.assertIn("Escalated to the owner: needs owner", comments[0][1])
            self.assertEqual(labels[1][0], 11)
            self.assertEqual(sorted(l.split(":")[-1] for l in labels[1][2]), ["awaiting-cos", "awaiting-user"])
            self.assertIn("covered by order", comments[1][1])
            self.assertEqual(replies, [("ses_1", "req_1", "once", "safe")])
            # the oversize file lands where the recipient's drain wrote it, with the path rewritten
            home = os.path.join(tmp, "mac")
            sent = []
            engine = types.SimpleNamespace(state_dir=home, v2=types.SimpleNamespace(synthetic=lambda sid, t, *a: sent.append(t)))
            msg = {"id": 1, "from": "switchboard", "to": "widget-api", "session": "ses_example", "text": text.replace(tmp, "/pod"),
                   "oversize_files": {"send-abc.txt": "long body"}}
            with mock.patch.object(self.floor, "switchboard_relay_config", return_value={"url": "http://x", "authority": "cluster"}), \
                    mock.patch.object(self.floor, "_auth_header", return_value="h"), \
                    mock.patch.object(self.floor, "_request", lambda *a, **k: (200, json.dumps({"messages": [msg]}).encode())), \
                    mock.patch.object(self.floor, "call_switchboard_v1", lambda *a, **k: None):
                self.assertEqual(self.floor.drain_pane_outbox(engine)["delivered"], 1)
            self.assertIn(os.path.join(home, "oversize-sends", "send-abc.txt"), sent[0])
            self.assertEqual(Path(home, "oversize-sends", "send-abc.txt").read_text(), "long body")

    def test_single_authority_and_tunnel_drop_fire_macos_notifications(self):
        f = self.floor
        self.assertIsNone(f.check_single_authority("active", "cluster", True))
        self.assertIsNone(f.check_single_authority("shadow", "mac", False))
        sent = []
        monitor = f.PaneRelayMonitor(notify=lambda title, text: sent.append(title))
        monitor.tick(0, True, "shadow", "cluster", True)
        monitor.tick(1, True, "active", "mac", False)
        self.assertEqual(sent, ["fleet-switchboard authority mismatch"])
        sent.clear()
        for now in (10, 60, 129):
            monitor.tick(now, False)
        self.assertEqual(sent, [])
        self.assertTrue(monitor.tick(130, False)["restart"])
        monitor.tick(200, False)
        self.assertEqual(sent, ["fleet-switchboard relay down"])
        monitor.tick(210, True)
        self.assertIn("fleet-switchboard relay recovered", sent)
        sent.clear()
        monitor = f.PaneRelayMonitor(notify=lambda title, text: sent.append(title))
        self.assertIsNotNone(monitor.tick(0, True, "active", "cluster", True)["send_canary"])
        self.assertFalse(monitor.tick(61, True, "active", "cluster", True)["rollback"])
        self.assertEqual(sent, ["fleet-switchboard canary failed"])
        self.assertIsNotNone(monitor.tick(600, True, "active", "cluster", True)["send_canary"])
        act = monitor.tick(661, True, "active", "cluster", True)
        self.assertIs(act["rollback"], True)
        self.assertEqual(sent[-1], "fleet-switchboard rolled back")
        calls = []
        f.notify_macos_alarm("t", "x", runner=lambda argv, **k: calls.append(argv))
        self.assertEqual(calls[0][0], "/usr/bin/osascript")

    def test_pane_state_preserves_outcome_and_defaults_clean_idle(self):
        class Obs:
            def __init__(self, name, session_id, pane, status, fleet):
                self.name, self.session_id, self.pane, self.status, self.fleet = name, session_id, pane, status, fleet
                self.errored = False

        names = ("widget-api", "widget-web", "widget-bad", "widget-fail")
        disc = argparse.Namespace(agents={n: Obs(n, "ses_" + n, None, "working", {}) for n in names})
        self.floor.apply_pane_state_to_discovery({
            "widget-api": {"status": "idle", "idle_at": 1},
            "widget-web": {"status": "idle", "outcome": "done"},
            "widget-bad": {"status": "idle", "errored": True},
            "widget-fail": {"status": "idle", "outcome": "failed"},
        }, disc)
        self.assertEqual(disc.agents["widget-fail"].outcome, "failed")
        self.assertEqual(disc.agents["widget-api"].outcome, "succeeded")
        self.assertEqual(disc.agents["widget-web"].outcome, "done")
        self.assertIsNone(disc.agents["widget-bad"].outcome)

    def test_cmd_remind_reads_text_file_before_relay(self):
        import tempfile
        calls = []
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "note.txt")
            with open(path, "w") as fh:
                fh.write("check the widget-api rollout")
            args = argparse.Namespace(name="widget-api", when="10m", text=[], text_file=path, issue=None, key=None, ttl=None, every=None,
                                      until_closed=None, until_merged=None, keep_if_closed=False, list_mine=False, cancel=None, cancel_key=None, sender=None)
            done = lambda command, payload: calls.append((command, payload)) or True
            with mock.patch.dict(os.environ, {"CW_SWITCHBOARD_AUTHORITY": "cluster"}), mock.patch.object(sb, "relay_cli_done", done), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                code = sb.cmd_remind(args, None, lambda: 0)
        self.assertEqual(code, 0)
        self.assertIn("relayed to cluster", err.getvalue())
        self.assertEqual(calls[0][0], "remind")
        self.assertEqual(calls[0][1]["text"], "check the widget-api rollout")

    def test_a_finished_session_retires_on_the_first_call(self):
        for finished in ("done", "failed", "retired"):
            transport = self.floor.QueueBlobTransport("https://q.example/q", "sig=1", "https://b.example/b", "sig=2")
            transport._enqueue = lambda *a, **k: None
            transport._state = lambda: {"sessions": {"w-1": {"state": finished}}, "acks": {}}
            self.assertEqual(transport.cancel("fleet", "w-1", "c1", 2), {"cancelled": True, "reserved": False})

    def test_watch_sync_audits_stop_once_and_adds_virtual_records(self):
        class T:
            works = {"w-1": {"state": "done"}, "w-2": {"state": "failed"}, "w-3": {"state": "working", "worker": "widget-api", "repo": "acme-org/widget-service"}}
        self.floor.TRANSPORT = T()
        audit = mock.Mock()
        engine = mock.Mock(audit=audit, actor="sw")
        records = [{"worker": "a", "backend": "floor", "work_id": "w-1"}, {"worker": "b", "backend": "floor", "work_id": "w-2"}]
        with mock.patch.object(self.m, "save_record", lambda *a: None):
            self.floor.floor_watch_sync(self.state, records, engine)
            self.floor.floor_watch_sync(self.state, records, engine)
        reasons = sorted((c.args[3]["reason"], c.args[3]["exit"]) for c in audit.append.call_args_list)
        self.assertEqual(reasons, [("done", 0), ("failed", 1)])
        self.assertIn("widget-api", [r["worker"] for r in records])

    def test_watch_sync_sends_one_terminal_wake_to_the_boss(self):
        class T:
            works = {"w-1": {"state": "done", "landed_sha": "abc123"}, "w-2": {"state": "failed", "error": "build broke"}}
        self.floor.TRANSPORT = T()
        audit = mock.Mock()
        engine = mock.Mock(audit=audit, actor="sw")
        records = [{"worker": "a", "backend": "floor", "work_id": "w-1", "reports_to": "cos"},
                   {"worker": "b", "backend": "floor", "work_id": "w-2", "reports_to": "cos"}]
        with mock.patch.object(self.m, "save_record", lambda *a: None):
            self.floor.floor_watch_sync(self.state, records, engine)
            self.floor.floor_watch_sync(self.state, records, engine)
        texts = sorted(c.args[2] for c in engine.send.call_args_list)
        self.assertEqual(len(texts), 2)
        self.assertIn("landed_sha=abc123", texts[0])
        self.assertIn("build broke", texts[1])
        self.assertEqual(audit.append.call_count, 2)

    def test_watch_sync_observes_a_transport_with_no_session_map(self):
        class T:
            pass
        self.floor.TRANSPORT = T()
        engine = mock.Mock(audit=mock.Mock(), actor="sw")
        records = [{"worker": "a", "backend": "floor", "work_id": "w-1", "reports_to": "cos"}]
        with mock.patch.object(self.m, "save_record", lambda *a: None), \
                mock.patch.object(self.floor, "_observe", return_value={"state": "done", "landed_sha": "def456"}):
            self.floor.floor_watch_sync(self.state, records, engine)
        self.assertEqual(records[0]["floor_state"], "done")
        self.assertIn("landed_sha=def456", engine.send.call_args.args[2])

    def test_pod_silence_summary_reads_a_floor_session_without_the_launcher(self):
        cloud = mock.Mock()
        cloud.route.return_value = {"worker": "a", "backend": "floor", "session": "ses_example"}
        cloud.cloud_floor.is_floor.return_value = True
        cloud.cloud_floor._observe.return_value = {"state": "working", "busy": True, "rounds": 2, "landed_sha": None}
        with mock.patch.object(self.sb.load_module("cloud_worker") if hasattr(self, "sb") else sb.load_module("cloud_worker"), "bind", return_value=cloud):
            line = sb.pod_silence_summary("a")
        self.assertIn("state=working", line)
        cloud.run_launcher.assert_not_called()

    def test_lost_worker_skips_a_finished_floor_session(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("lost_worker", str(REPO / "bin" / "lost_worker.py"))
        lw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lw)
        created = {"w": {"at": 1000, "deadline": 2000, "line": "l"}}
        for record, expect in (({"backend": "floor", "floor_state": "done"}, 0), ({"backend": "floor"}, 1)):
            state = {"created": dict(created)}
            out = lw._losses(state, mock.Mock(floor_session_finished=lambda n, r: r.get("floor_state") in self.floor.FINISHED_STATES), mock.Mock(), {}, 10 ** 7, {"w": record})
            self.assertEqual(len(out), expect)

    def test_lost_worker_never_flags_a_floor_worker_as_local(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("lost_worker", str(REPO / "bin" / "lost_worker.py"))
        lw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lw)
        sbm = mock.Mock(retired=lambda e, n: False)
        records = {"widget-api": {"backend": "floor"}}
        state = {"seen": {"widget-api": {"at": 5, "lane": None, "ask": "x"}}}
        out = lw._losses(state, sbm, mock.Mock(), {}, 100, records)
        self.assertEqual([x for x in out if x["kind"] == "local_worker"], [])
        state = {}
        lw._losses(state, sbm, mock.Mock(), {"widget-api": {"lane": None}}, 100, records)
        self.assertNotIn("widget-api", state.get("seen", {}))
        state = {"seen": {"other": {"at": 5, "lane": None, "ask": "x"}}}
        out = lw._losses(state, sbm, mock.Mock(), {}, 100, records)
        self.assertEqual([x["name"] for x in out if x["kind"] == "local_worker"], ["other"])

    def test_pane_state_persists_across_restart(self):
        import importlib.machinery, importlib.util
        import tempfile
        loader = importlib.machinery.SourceFileLoader("ics_server", str(REPO / "tools/in-cluster-switchboard-server"))
        srv = importlib.util.module_from_spec(importlib.util.spec_from_loader("ics_server", loader))
        loader.exec_module(srv)
        with tempfile.TemporaryDirectory() as td:
            srv.STATE_DIR = Path(td)
            srv.PANE_STATE_FILE = Path(td) / "pane-state.json"
            srv.save_pane_state({"widget-api": {"status": "idle"}})
            self.assertEqual(srv.PANE_STATE_FILE.stat().st_mode & 0o777, 0o600)
            srv.V1_STATE.pop("pane_state", None)
            srv.load_pane_state()
            self.assertEqual(srv.V1_STATE["pane_state"], {"widget-api": {"status": "idle"}})
            srv.V1_STATE.pop("pane_state", None)

    def test_synthetic_messages_persist_and_list(self):
        import importlib.machinery, importlib.util
        import tempfile
        loader = importlib.machinery.SourceFileLoader("ics_server", str(REPO / "tools/in-cluster-switchboard-server"))
        srv = importlib.util.module_from_spec(importlib.util.spec_from_loader("ics_server", loader))
        loader.exec_module(srv)
        with tempfile.TemporaryDirectory() as td:
            srv.STATE_DIR = Path(td)
            srv.SYNTHETIC_MESSAGES_FILE = Path(td) / "synthetic-messages.json"
            srv.V1_STATE["synthetic_messages"] = {}
            v2 = object.__new__(srv.ShadowV2)
            v2.session_get = lambda sid: {"metadata": {"fleet": {"name": "cos"}}}
            try:
                for i, text in enumerate(["first", "question: widget-api needs a call"]):
                    try:
                        v2.synthetic("ses_example", text, {}, False, "immediate", "msg_%d" % i, "d")
                    except Exception:
                        pass  # shadow bookkeeping after recording is not under test
                self.assertEqual(srv.SYNTHETIC_MESSAGES_FILE.stat().st_mode & 0o777, 0o600)
                srv.V1_STATE["synthetic_messages"] = {}
                srv.load_synthetic_messages()
                listed = v2.message_list("ses_example", limit=10, type="synthetic")
                self.assertEqual([m["id"] for m in listed], ["msg_1", "msg_0"])
                self.assertEqual(v2.message_list("ses_example", type="other"), [])
                pages = list(v2.message_pages("ses_example", 5, "synthetic", 2))
                self.assertEqual([m["id"] for m in pages[0][0]], ["msg_1", "msg_0"])
                self.assertIn("question:", listed[0]["text"])
            finally:
                srv.V1_STATE["synthetic_messages"] = {}

    def test_report_send_and_decisions_refresh_decisions(self):
        import importlib.machinery, importlib.util
        loader = importlib.machinery.SourceFileLoader("ics_server", str(REPO / "tools/in-cluster-switchboard-server"))
        srv = importlib.util.module_from_spec(importlib.util.spec_from_loader("ics_server", loader))
        loader.exec_module(srv)
        published = []

        class Store:
            def publish(self, decisions, errors, now_ts, provisional=False):
                published.append(provisional)

        class Eng:
            discovery = object()
            now = staticmethod(lambda: 1.0)
            def report(self, *a, **k): return {}
            def send(self, *a, **k): return {}

        class SB:
            derive_decisions = staticmethod(lambda eng, now: ([], []))
            decision_store = staticmethod(lambda eng: Store())

        saved = dict(srv.ENGINE_REF)
        srv.ENGINE_REF.update(engine=Eng(), sb_mod=SB(), cf_mod=None)
        try:
            auth = {"agent": "widget-api"}
            run = lambda route, body: srv.handle_on_v1_action(route, auth, body, {})
            run("/v1/report", {"state": "question", "text": "q"})
            self.assertEqual(published, [True])
            run("/v1/report", {"state": "working", "text": "x"})
            self.assertEqual(published, [True])
            run("/v1/send", {"to": "cos", "text": "answer"})
            run("/v1/decisions", {"action": "escalate"})
            self.assertEqual(published, [True] * 3)
            run("/v1/decisions", {"action": "list"})
            self.assertEqual(len(published), 3)
            SB.derive_decisions = staticmethod(lambda eng, now: 1 / 0)
            run("/v1/send", {"to": "cos", "text": "answer"})  # a failing refresh never breaks the route
        finally:
            srv.ENGINE_REF.update(saved)

    def test_retire_orders_and_pane_commands(self):
        state = {}
        self.assertEqual(self.call("/v1/retire", {"worker": "widget-api", "reason": "done"}, state=state), (200, {"retired": True, "worker": "widget-api"}))
        self.assertEqual(state["retires"][0]["worker"], "widget-api")
        code, out = self.call("/v1/orders", {"action": "add", "charter": "acme-org/widget-service#10", "text": "ship"}, state=state)
        self.assertEqual((code, out["recorded"], len(out["orders"])), (200, True, 1))
        self.assertEqual(self.call("/v1/pane-command", {"action": "resume", "agent": "widget-api", "reason": "x"}, state=state)[0], 200)
        self.assertEqual(self.call("/v1/pane-state", {"agents": {"widget-api": {"status": "idle", "junk": 1}}}, agent="service-ops", state=state)[0], 403)
        code, out = self.call("/v1/pane-state", {"agents": {"widget-api": {"status": "idle", "junk": 1}}, "at": 5}, agent="pane-relay", state=state)
        self.assertEqual((code, out["updated"], [c["action"] for c in out["commands"]]), (200, 1, ["resume"]))
        self.assertEqual(state["pane_state"], {"widget-api": {"status": "idle"}})

    def test_retire_is_for_the_chief_of_staff_or_the_boss_and_cancels_the_session(self):
        class T:
            calls = []
            def cancel(self, who, work_id, key, seq):
                self.calls.append((work_id, seq))
                return {"cancelled": True, "reserved": False}
        self.floor.TRANSPORT = T()
        state = {"floor_sessions": {"widget-api": {"work_id": "w-1", "seq": 1, "reports_to": "service-ops"}}}
        self.assertEqual(self.call("/v1/retire", {"worker": "widget-api"}, agent="stranger", state=state)[0], 403)
        self.assertEqual(T.calls, [])
        self.assertEqual(self.call("/v1/retire", {"worker": "widget-api"}, agent="service-ops", state=state)[0], 200)
        self.assertEqual(T.calls, [("w-1", 2)])

    def test_an_audit_failure_leaves_the_stop_unrecorded_and_says_so(self):
        class T:
            works = {"w-1": {"state": "done"}}
        self.floor.TRANSPORT = T()
        engine = mock.Mock(actor="sw")
        engine.audit.append.side_effect = OSError("disk")
        records = [{"worker": "a", "backend": "floor", "work_id": "w-1"}]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.floor.floor_watch_sync(self.state, records, engine)
        self.assertNotIn("stopped_audited", records[0])
        self.assertIn("OSError", err.getvalue())

    def test_call_switchboard_v1(self):
        self.assertIsNone(self.floor.call_switchboard_v1("/v1/orders", {}, env={}))
        auth = self.home / "auth"
        auth.write_text("agent=cos;session=ses_example;secret=x")
        auth.chmod(0o600)
        seen = {}

        def fake(method, url, data, headers, **k):
            seen.update(url=url, headers=dict(headers))
            return 200, b'{"ok": true}'
        with mock.patch.object(self.floor, "_request", fake):
            out = self.floor.call_switchboard_v1("/v1/orders", {"action": "list"}, env={"CW_SWITCHBOARD_URL": "https://sw.example", "CW_SWITCHBOARD_AUTH_FILE": str(auth)})
        self.assertEqual(out, {"ok": True})
        self.assertEqual(seen["url"], "https://sw.example/v1/orders")
        self.assertIn("agent=cos", seen["headers"]["X-Switchboard-Auth"])
        env = {"CW_SWITCHBOARD_URL": "https://sw.example", "CW_SWITCHBOARD_AUTH_FILE": str(auth)}
        attempts, sleeps = [], []

        def flaky(method, url, data, headers, **k):
            attempts.append(url)
            if len(attempts) == 1:
                raise self.floor.FloorError("Floor could not be reached (URLError)")
            if len(attempts) == 2:
                raise self.floor.FloorError("busy", status=503)
            return 200, b'{"ok": true}'
        with mock.patch.object(self.floor, "_request", flaky):
            out = self.floor.call_switchboard_v1("/v1/remind", {"text": "x"}, env=env, sleep_fn=sleeps.append)
        self.assertEqual((out, len(attempts), sleeps), ({"ok": True}, 3, [1.5, 3.0]))
        attempts.clear()

        def denied(method, url, data, headers, **k):
            attempts.append(url)
            raise self.floor.FloorError("no", status=401)
        with mock.patch.object(self.floor, "_request", denied):
            with self.assertRaises(self.floor.FloorError):
                self.floor.call_switchboard_v1("/v1/remind", {"text": "x"}, env=env, sleep_fn=sleeps.append)
        self.assertEqual(len(attempts), 1)

    def test_relay_cli_preserves_caller_agent_and_repo(self):
        auth = self.home / "auth"
        auth.write_text("agent=pane-relay;session=ses_example;secret=x")
        auth.chmod(0o600)
        seen = {}

        def fake(method, url, data, headers, **k):
            seen.update(url=url, headers=dict(headers), body=json.loads(data))
            return 200, b'{"ok": true}'
        H = {"build_engine": lambda *a: (mock.Mock(), None, {}), "identify_caller": lambda *a: "fleetkit",
             "resolve_for_sender": lambda engine, config, issue, caller: ("acme-org/widget-service", 34)}
        env = {"CW_SWITCHBOARD_URL": "https://sw.example", "CW_SWITCHBOARD_AUTH_FILE": str(auth)}
        with mock.patch.object(self.floor, "D", {"H": H, "parse_bounded": json.loads}), mock.patch.object(self.floor, "_request", fake):
            self.floor.relay_cli_if_configured("send", {"to": "cos", "text": "hi", "issue": 34}, env=env)
        self.assertEqual(seen["url"], "https://sw.example/v1/send")
        self.assertIn("agent=fleetkit", seen["headers"]["X-Switchboard-Auth"])
        self.assertEqual(seen["body"]["repo"], "acme-org/widget-service")
        self.assertEqual(seen["body"]["issue"], 34)
        self.assertNotIn("_caller_agent", seen["body"])

    def test_relay_config_from_state_file(self):
        (self.home / "switchboard-relay.json").write_text('{"url": "https://sw.example", "auth_file": "/x/auth", "authority": "cluster"}')
        worker = sb.load_module("cloud_worker").bind(vars(sb))
        with mock.patch.dict(worker.H, {"state_dir_path": lambda: self.home}), mock.patch.object(self.floor, "D", worker._Stayed()):
            got = self.floor.switchboard_relay_config(env={})
            self.assertEqual((got["url"], got["authority"]), ("https://sw.example", "cluster"))
            self.assertEqual(self.floor.switchboard_authority(env={"CW_SWITCHBOARD_AUTHORITY": "local"}), "local")

    def test_corrupt_relay_file_is_loud(self):
        (self.home / "switchboard-relay.json").write_text("{not json")
        worker = sb.load_module("cloud_worker").bind(vars(sb))
        with mock.patch.dict(worker.H, {"state_dir_path": lambda: self.home}), mock.patch.object(self.floor, "D", worker._Stayed()):
            with self.assertRaises(Exception):
                self.floor.switchboard_relay_config(env={})

    def test_cluster_authority_relay_failure_is_loud(self):
        def boom(*a, **k):
            raise RuntimeError("down")
        with mock.patch.dict(os.environ, {"CW_SWITCHBOARD_AUTHORITY": "cluster"}), \
                mock.patch.object(sb, "run_cloud", boom):
            with self.assertRaises(sb.LaunchError):
                sb.relay_cli("remind", {})


    def test_drain_pane_outbox_delivers_switchboard_messages_and_acks(self):
        auth = self.home / "auth"
        auth.write_text("agent=cos;session=ses_example;secret=x")
        auth.chmod(0o600)
        msgs = [{"id": 1, "from": "switchboard", "to": "w", "session": "ses_a", "text": "hi"}, {"id": 2, "from": "mac"}]
        for authority, delivered, acked in (("cluster", 1, [1, 2]), ("shadow", 0, [2])):
            env = {"CW_SWITCHBOARD_URL": "https://sw.example", "CW_SWITCHBOARD_AUTH_FILE": str(auth), "CW_SWITCHBOARD_AUTHORITY": authority}
            engine = mock.Mock()
            with mock.patch.object(self.floor, "_request", return_value=(200, json.dumps({"messages": msgs}).encode())), \
                    mock.patch.object(self.floor, "call_switchboard_v1") as post:
                out = self.floor.drain_pane_outbox(engine, env=env)
            self.assertEqual(out, {"delivered": delivered, "acked": len(acked)})
            post.assert_called_once_with("/v1/pane-ack", {"ids": acked}, env=env)
            self.assertEqual(engine.v2.synthetic.call_count, delivered)

    def test_relayed_remind_prints_the_stored_receipt(self):
        state = {"on_v1_action": lambda route, auth, body, st: st.update(last_remind={"id": "rem_abc12345", "at": 1700000900.0, "at_iso": "2023-11-14T22:28:20Z"})}
        status, got = self.call("/v1/remind", {"to": "widget-api", "when": "15m", "text": "check"}, state=state)
        self.assertEqual(status, 200)
        self.assertEqual((got["id"], got["at"], got["at_iso"]), ("rem_abc12345", 1700000900.0, "2023-11-14T22:28:20Z"))
        self.assertNotIn("last_remind", state)
        args = argparse.Namespace(name="widget-api", when="15m", text=["check"], text_file=None, issue=None, key=None, ttl=None, every=None,
                                  until_closed=None, until_merged=None, keep_if_closed=False, list_mine=False, cancel=None, cancel_key=None, sender=None)
        def done(command, payload):
            payload["_relay_ans"] = got
            return True
        with mock.patch.object(sb, "relay_cli_done", done), contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()) as out:
            code = sb.cmd_remind(args, None, lambda: 1700000000.0)
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), "rem_abc12345")
        self.assertIn(sb._print_when(1700000900.0, 1700000000.0), err.getvalue())
        self.assertIn("relayed to cluster", err.getvalue())

    def test_drain_pane_outbox_audits_synthetic_and_lateness_sees_the_pair(self):
        import tempfile
        from datetime import datetime, timezone
        auth = self.home / "auth"
        auth.write_text("agent=cos;session=ses_example;secret=x")
        auth.chmod(0o600)
        env = {"CW_SWITCHBOARD_URL": "https://sw.example", "CW_SWITCHBOARD_AUTH_FILE": str(auth), "CW_SWITCHBOARD_AUTHORITY": "cluster"}
        msgs = [{"id": 1, "from": "switchboard", "to": "w", "session": "ses_a", "text": "hi", "keys": ["reminder:rem_abc12345"]}]
        engine = mock.Mock()
        with mock.patch.object(self.floor, "_request", return_value=(200, json.dumps({"messages": msgs}).encode())), \
                mock.patch.object(self.floor, "call_switchboard_v1"):
            self.floor.drain_pane_outbox(engine, env=env)
        calls = engine.audit.append.call_args_list
        self.assertEqual(len(calls), 1)
        who, event, subject, detail = calls[0].args
        self.assertEqual((who, event), ("daemon", "v2.synthetic"))
        self.assertEqual(set(detail), {"keys", "delivery", "resume"})
        self.assertIn("reminder:rem_abc12345", detail["keys"])
        def ts(t):
            return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        base = 1700000000
        lines = [{"ts": ts(base), "actor": "cli", "event": "reminder.add", "subject": "w", "detail": {"id": "rem_abc12345", "at": ts(base)}},
                 {"ts": ts(base + 3600), "actor": who, "event": event, "subject": subject, "detail": detail}]
        with tempfile.TemporaryDirectory() as d:
            Path(d, "audit.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
            now = base + 3700
            self.assertEqual(sb.late_reminder_counts(d, {}, now), {"w": 1})
            self.assertEqual(sb.late_reminder_worst(d, now), {"w": 3600})

    def test_relay_remind_audits_locally_and_warns_when_the_audit_fails(self):
        ans = {"recorded": True, "id": "rem_abc12345", "at": 1700000900.0, "at_iso": "2023-11-14T22:28:20Z"}
        engine = mock.Mock()
        H = {"build_engine": lambda *a: (engine, None, {}), "iso": lambda t: "x"}
        payload = {"to": "widget-api", "when": "15m", "text": "t", "action": "add"}
        with mock.patch.object(self.floor, "switchboard_relay_config", return_value={"url": "https://sw.example"}), \
                mock.patch.object(self.floor, "call_switchboard_v1", return_value=ans), \
                mock.patch.object(self.floor, "_resolve_local_caller_and_repo", side_effect=lambda p, env=None: p), \
                mock.patch.object(self.floor, "D", {"H": H}):
            self.floor.relay_cli_if_configured("remind", payload)
            self.assertEqual(payload["_relay_ans"], ans)
            args = engine.audit.append.call_args.args
            self.assertEqual(args[:3], ("cli", "reminder.add", "widget-api"))
            self.assertEqual(args[3]["id"], "rem_abc12345")
            engine.audit.append.side_effect = RuntimeError("disk full")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.floor.relay_cli_if_configured("remind", dict(payload))
        self.assertIn("disk full", err.getvalue())

    def test_rollback_switchboard_authority_sets_shadow_and_resumes_pid(self):
        (self.home / "switchboard-relay.json").write_text('{"url": "https://sw.example", "authority": "cluster"}')
        pid = os.getpid()
        (self.home / "daemon.json").write_text(json.dumps({"pid": pid}))
        kills = []
        with mock.patch.object(self.floor, "call_switchboard_v1") as post:
            out = self.floor.rollback_switchboard_authority(self.home, env={}, kill_fn=lambda *a: kills.append(a))
        post.assert_called_once_with("/v1/mode", {"mode": "shadow"}, env={})
        self.assertEqual(json.loads((self.home / "switchboard-relay.json").read_text())["authority"], "shadow")
        self.assertEqual(kills, [(pid, signal.SIGCONT)])
        self.assertEqual(out, {"authority": "shadow", "resumed_pid": pid})

    def test_fetch_cluster_mode_falls_back_to_healthz_on_404_only(self):
        cfg = {"url": "https://sw.example/"}
        def req(status, mode):
            def fake(method, url, *a, **k):
                if url.endswith("/v1/mode"):
                    raise self.floor.FloorError("gone", status=status)
                return 200, json.dumps({"mode": mode}).encode()
            return fake
        with mock.patch.object(self.floor, "switchboard_relay_config", return_value=cfg), \
                mock.patch.object(self.floor, "_auth_header", return_value="x"), \
                mock.patch.object(self.floor, "_parse", side_effect=lambda b: json.loads(b)):
            with mock.patch.object(self.floor, "_request", req(404, "shadow")):
                self.assertEqual(self.floor.fetch_cluster_mode({}), "shadow")
            with mock.patch.object(self.floor, "_request", req(500, "shadow")):
                with self.assertRaises(self.floor.FloorError):
                    self.floor.fetch_cluster_mode({})

    def test_render_pane_relay_plist_is_valid_xml(self):
        out = self.floor.render_pane_relay_plist("/usr/bin/python3", "/home/user/kit", "/home/user", "/home/user/state", "/usr/bin:/a&b")
        data = plistlib.loads(out.encode())
        self.assertEqual(data["Label"], "com.fleet-kit.pane-relay")
        self.assertIs(data["KeepAlive"], True)
        self.assertEqual(data["EnvironmentVariables"]["PATH"], "/usr/bin:/a&b")
        self.assertEqual(data["StandardOutPath"], "/home/user/state/pane-relay-stdout.log")
        self.assertEqual(data["StandardErrorPath"], "/home/user/state/pane-relay-stderr.log")
        odd = plistlib.loads(self.floor.render_pane_relay_plist("/usr/bin/python3", "/k", "/Users/a%sb", "/Users/a%sb/state", "/usr/bin").encode())
        self.assertEqual(odd["StandardOutPath"], "/Users/a%sb/state/pane-relay-stdout.log")


    def test_pane_relay_pass_reports_a_failed_push_and_still_drains(self):
        engine = mock.Mock(sources=[])
        sb_mod = types.SimpleNamespace(cloud_push_pane_state=mock.Mock(side_effect=RuntimeError("down")))
        err = io.StringIO()
        with mock.patch.object(self.floor, "drain_pane_outbox", return_value={"delivered": 0, "acked": 0}) as drain, contextlib.redirect_stderr(err):
            out = self.floor.pane_relay_pass(engine, sb_mod, env={})
        self.assertEqual(out, {"delivered": 0, "acked": 0})
        self.assertIn("pane state push failed", err.getvalue())
        drain.assert_called_once()

    def test_sync_cloud_records_sends_reminders_only_outside_cluster_authority(self):
        (self.home / "cloud").mkdir()
        (self.home / "cloud" / "w-1.json").write_text('{"a": 1}')
        (self.home / "reminders.json").write_text('[1]')
        for authority, reminders in (("shadow", [1]), ("cluster", None)):
            with mock.patch.object(self.floor, "switchboard_authority", return_value=authority), \
                    mock.patch.object(self.floor, "call_switchboard_v1") as post:
                out = self.floor.sync_cloud_records_to_cluster(self.home, env={})
            self.assertEqual(out, {"synced": 1})
            post.assert_called_once_with("/v1/cloud-sync", {"records": {"w-1": {"a": 1}}, "reminders": reminders}, env={})


class TestFinalMessageRelay(Base):
    def test_final_message_relays_over_send(self):
        worker = sb.load_module("cloud_worker").bind(vars(sb))
        calls = []
        engine = mock.Mock()
        done = lambda command, payload: calls.append((command, payload)) or True
        retired = mock.Mock()
        retired.last_message.return_value = ("last text", None)
        with mock.patch.dict(worker.H, {"relay_cli_done": done, "post_ask_comment": None, "_one_line": lambda t, n=300: str(t)[:n]}), mock.patch.object(worker, "_retired", lambda: retired), \
                mock.patch.object(worker, "_scrub_lib", lambda: (lambda t, *a: t)):
            with mock.patch.object(worker, "_audit", lambda e, what, name, body=None: calls.append(("audit", body))):
                worker._final_message(engine, {}, {"reports_to": "cos"}, "widget-api", "done")
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0][0], calls[0][1]["to"]), ("send", "cos"))
        engine.send.assert_not_called()


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
