#!/usr/bin/env python3
"""Ask #1397: a lane sees a cloud worker's state in ONE line (`status NAME`, and the launch).

Offline: the cluster is the fake kubectl beside this file (read by the real launcher), the agent service is a fake client, time is a fake clock.
S2 one line, error line cut and free of planted secrets; S3 the four states and a mutant; S4 the launch line, once and bounded; S5 another lane's
worker refused and nothing written; S1/S6 the documents."""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_status_summary as S  # noqa: E402  (the fake cluster, the records and the switchboard module)

sb, CLOUD, ALIVE = S.sb, S.CLOUD, S.ALIVE
ROOT = S.KIT
WRITE_VERBS = ("create", "patch", "delete", "label", "exec", "apply", "port-forward", "replace", "scale")
TOKEN = "ghp_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0dF3gH6"
PASSWORD = "Zq7-planted-passphrase"
ENV_VALUE = "INTERNAL_HOST=vault-7.corp.example"
ENV_SECRET = "plantedenvvalue4242"


class FakeV2:
    """The agent service of one pod: it answers the reads the state line uses, and records any other call as a write."""

    def __init__(self, permissions=(), forms=(), messages=()):
        self.permissions, self.forms, self.messages = list(permissions), list(forms), list(messages)
        self.reads, self.writes = [], []

    def permission_list(self, session):
        self.reads.append("permission_list")
        return list(self.permissions)

    def form_list(self, session):
        self.reads.append("form_list")
        return list(self.forms)

    def message_list(self, session, limit=None, order=None, type=None, cursor=None):
        self.reads.append("message_list")
        return list(self.messages)

    def __getattr__(self, name):   # prompt, session_create, form_reply ...: anything else is a write
        def call(*args, **kwargs):
            self.writes.append(name)
        return call


def message(ms, text="", error=None):
    found = {"type": "assistant", "text": text, "time": {"created": ms}}
    if error is not None:
        found["error"] = error
    return found


class Base(S.Base):
    def setUp(self):
        super().setUp()
        self.v2 = FakeV2(messages=[message(1798365852000, "working")])
        for target, name, value in ((CLOUD, "remote_v2", lambda *a, **k: self.v2), (CLOUD, "tunnel", self.fake_tunnel)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.files_before = {}

    @contextlib.contextmanager
    def fake_tunnel(self, cluster, record):
        yield record

    def tree(self):
        found = {}
        for path in sorted(self.state_dir.rglob("*")):
            if path.is_file():
                found[str(path)] = (path.stat().st_size, path.read_bytes())
        return found

    def worker(self, phase="Running", session=True):
        self.put_pods(self.pod("cw-coder7", "coder7", phase=phase, hard=ALIVE))
        path = self.record("coder7", hard=ALIVE)
        if not session:
            body = json.loads(path.read_text())
            del body["session"]
            path.write_text(json.dumps(body))
            os.chmod(str(path), 0o600)

    def status_line(self, name="coder7"):
        self.files_before = self.tree()   # after the arrangement: the records are the fixture, the command must leave them as they are
        code, out, err = self.run_command("status", name)
        return code, out, err

    def assert_read_only(self):
        """0 messages sent, 0 files written (the state directory is byte for byte as it was), 0 cluster changes."""
        self.assertEqual(self.v2.writes, [])
        self.assertEqual(self.tree(), self.files_before)
        writes = [c for c in self.kube_calls() if c and c[0] in WRITE_VERBS]
        self.assertEqual(writes, [], writes)


class TestErrorLine(Base):
    """S2: one line; the error line at most 160 characters and 0 of 3 planted secrets (a token, a password, an environment value)."""

    def planted(self):
        pad = "x " * 40
        return "Error: request failed " + pad + " token %s password: %s %s " % (TOKEN, PASSWORD, ENV_VALUE)

    def line_with_error(self, text, error_field=True):
        self.worker()
        self.v2.messages = [message(1798365852000, "" if error_field else text, error=text if error_field else None)]
        code, out, err = self.status_line()
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1, out)
        return lines[0]

    def error_part(self, line):
        self.assertIn("; last error ", line)
        return line.split("; last error ", 1)[1]

    def test_s2_one_line_with_a_cut_error_and_no_planted_secret(self):
        line = self.line_with_error(self.planted())
        self.assertLessEqual(len(self.error_part(line)), 160)
        shown = [secret for secret in (TOKEN, PASSWORD, ENV_VALUE.split("=")[1]) if secret in line]
        self.assertEqual(shown, [], line)
        self.assertTrue(line.startswith("cloud coder7: running; last tool call or message 2026-"), line)
        self.assert_read_only()

    def test_s2_each_secret_alone_and_from_a_text_line_never_shows(self):
        for kind, text in (("token", "Error: push refused for %s" % TOKEN), ("password", "Error: login failed, password=%s" % PASSWORD),
                           ("password colon", 'Error: {"password": "%s"}' % PASSWORD), ("environment", "Error: bad config %s" % ENV_VALUE),
                           ("pair inside a kept pair", "Error: failed: DB_PASSWORD=%s" % PASSWORD), ("bearer", "Error: Authorization: Bearer %s" % TOKEN), ("process environment", "Error: connecting with %s" % ENV_SECRET)):
            for field in (True, False):
                with self.subTest(kind=kind, error_field=field), mock.patch.dict(os.environ, {"SERVICE_API_KEY": ENV_SECRET}):
                    line = self.line_with_error(text, error_field=field)
                    for secret in (TOKEN, PASSWORD, ENV_VALUE.split("=")[1], ENV_SECRET):
                        self.assertNotIn(secret, line)
                    self.assertIn("Error", line)   # the useful words stay

    def test_s2_a_secret_at_the_cut_leaves_no_piece(self):
        text = "Error: " + "a " * 70 + TOKEN + " " + PASSWORD
        line = self.line_with_error(text)
        self.assertLessEqual(len(self.error_part(line)), 160)
        for secret in (TOKEN, PASSWORD):
            for i in range(0, len(secret) - 7):
                self.assertNotIn(secret[i:i + 8], line)

    def test_s2_the_worker_password_never_shows_in_any_form(self):
        line = self.line_with_error("Error: server said synthetic-password is wrong")
        self.assertNotIn("synthetic-password", line)

    def test_s2_a_plain_error_is_kept(self):
        line = self.line_with_error("ERROR: connection refused by the proxy")
        self.assertIn("ERROR: connection refused by the proxy", line)

    def test_s2_no_error_says_none_and_no_message_says_none_yet(self):
        self.worker()
        self.v2.messages = []
        code, out, _ = self.status_line()
        self.assertIn("last tool call or message none yet; last error none", out)

    def test_s2_a_build_that_prints_the_raw_error_shows_a_planted_secret(self):
        """The discriminator: the redaction replaced by a bare cut shows 1 of 3 planted secrets, and the replacement was applied."""
        applied = []

        def raw(text, known=()):
            applied.append(True)
            return str(text)[:160]
        with mock.patch.object(CLOUD, "redact_error", raw):
            line = self.line_with_error("Error: token %s" % TOKEN)
        self.assertTrue(applied)
        self.assertIn(TOKEN, line)


class TestStates(Base):
    """S3: the four states, and a build that always prints running fails the three others."""

    def case(self, kind):
        """Arrange one situation; the state it must print."""
        if kind == "starting (pod pending)":
            self.worker(phase="Pending")
        elif kind == "starting (no session yet)":
            self.worker(session=False)
        elif kind == "running":
            self.worker()
        elif kind == "waiting (permission request)":
            self.worker()
            self.v2.permissions = [{"id": "per_1"}]
        elif kind == "waiting (question)":
            self.worker()
            self.v2.forms = [{"id": "frm_1", "title": "Which branch?"}]
        elif kind == "exited (succeeded)":
            self.worker(phase="Succeeded")
        elif kind == "exited (failed)":
            self.worker(phase="Failed")
        elif kind == "exited (gone)":
            self.record("coder7", hard=ALIVE)
        return {"starting": "starting", "running": "running", "waiting": "waiting on a prompt", "exited": "exited"}[kind.split(" ")[0]]

    CASES = ("starting (pod pending)", "starting (no session yet)", "running", "waiting (permission request)", "waiting (question)",
             "exited (succeeded)", "exited (failed)", "exited (gone)")

    def printed(self, kind):
        want = self.case(kind)
        code, out, err = self.status_line()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.splitlines()), 1, out)
        return want, out

    def test_s3_each_situation_prints_its_state(self):
        for kind in self.CASES:
            with self.subTest(kind):
                self.setUp()   # a clean cluster, records and fake service for each situation
                want, out = self.printed(kind)
                self.assertTrue(out.startswith("cloud coder7: %s;" % want), out)
                self.assert_read_only()

    def test_s3_an_unreadable_service_is_never_called_running(self):
        self.worker()

        def down(*a, **k):
            raise sb.V2Unreachable("no answer")
        self.v2.permission_list = down
        code, out, _ = self.status_line()
        self.assertEqual(code, 1)
        self.assertIn("state not read", out)
        self.assertNotIn("running", out)

    def test_s3_a_mutant_that_always_prints_running_fails_the_three_other_states(self):
        applied = []

        def always_running(runner, state, cluster, record, timeout, snapshot=None, v2_for=None):
            applied.append(True)
            return {"state": "running", "last_ms": None, "error": None}
        passed = {}
        with mock.patch.object(CLOUD, "read_state", always_running):
            for kind in ("starting (pod pending)", "running", "waiting (permission request)", "exited (failed)"):
                self.setUp()
                want, out = self.printed(kind)
                passed[kind.split(" ")[0]] = out.startswith("cloud coder7: %s;" % want)
        self.assertEqual(len(applied), 4, "the mutation was applied to every run")
        self.assertEqual(passed, {"starting": False, "running": True, "waiting": False, "exited": False})


class Clock:
    """A fake clock: a pause moves it, nothing waits for real."""

    def __init__(self):
        self.now, self.pauses = 1000.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.pauses.append(seconds)
        self.now += max(seconds, 0.001)


class TestLaunchLine(Base):
    """S4: the launch prints the line once after the first message is accepted; a silent pod ends within 60 s plus 5 s, with no failure."""

    def run_line(self, v2=None):
        self.worker()
        record = CLOUD.load_record(self.state_dir, "coder7")
        self.files_before = self.tree()
        clock = Clock()
        with mock.patch.object(CLOUD, "_clock", clock), mock.patch.object(CLOUD, "_sleep", clock.sleep), CLOUD.Cluster() as cluster:
            line = CLOUD.launch_state_line(subprocess.run, self.state_dir, cluster, record, "coder7", v2 or self.v2, 1798365852000)
        return line, clock

    def test_s4_the_pod_took_its_brief_the_line_is_the_state_line(self):
        line, clock = self.run_line()
        self.assertTrue(line.startswith("cloud coder7: running; last tool call or message 2026-"), line)
        self.assertEqual(clock.pauses, [])   # the service answered: no wait
        self.assertNotIn("message_list", self.v2.reads)   # the message log is not read by the launch
        self.assert_read_only()

    def test_s4_a_silent_pod_ends_within_the_bound_with_the_plain_words(self):
        def silent(*a, **k):
            raise sb.V2Unreachable("no answer")
        self.v2.permission_list = silent
        line, clock = self.run_line()
        self.assertEqual(line, "cloud coder7: no state read within 60 s")
        self.assertGreaterEqual(clock.now - 1000.0, 60)
        self.assertLessEqual(clock.now - 1000.0, 65)
        self.assert_read_only()

    def test_s4_a_service_that_refuses_the_read_is_said_at_once(self):
        class Refusing(FakeV2):
            def permission_list(self, *a, **k):
                raise sb.V2Error("http", 500, "boom")
        line, clock = self.run_line(Refusing())
        self.assertIn("state not read", line)   # an answer that is a refusal is said at once, not waited for
        self.assertEqual(clock.pauses, [])

    def test_s4_a_service_that_times_out_is_silence_and_ends_within_the_bound(self):
        class Slow(FakeV2):
            def permission_list(self, *a, **k):
                raise sb.V2Error("timeout", None, "slow")
        line, clock = self.run_line(Slow())
        self.assertEqual(line, "cloud coder7: no state read within 60 s")
        self.assertLessEqual(clock.now - 1000.0, 65)

    def test_s4_the_launch_calls_it_once_after_the_brief_and_prints_it_once_on_stderr(self):
        import inspect
        source = inspect.getsource(CLOUD._launch)
        self.assertEqual(source.count("launch_state_line("), 1)
        self.assertEqual(source.count("print(stated, file=sys.stderr)"), 1)
        self.assertLess(source.index("v2.prompt("), source.index("launch_state_line("))
        self.assertLess(source.index("launch_state_line("), source.index('print(json.dumps({"name": name'))
        self.assertIn("raise stated", source)   # an interrupt is raised after the record is done, never swallowed
        self.assertEqual(CLOUD.STATE_WAIT_SECONDS, 60)


class TestRefusal(Base):
    """S5: a worker of another lane is refused as the other cloud commands refuse it; nothing is written."""

    def test_s5_another_lanes_worker_is_refused_and_nothing_was_read_or_written(self):
        self.worker()
        with mock.patch.object(sb, "gates_caller_kind", lambda runner, now: ("agent", "other-lane", "orchestrator")):
            code, out, err = self.status_line()
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("refused", err)
        self.assertEqual(self.v2.reads, [])
        self.assert_read_only()

    def test_s5_the_workers_own_boss_and_the_chief_of_staff_may_read_it(self):
        for kind in (("agent", "orch", "orchestrator"), ("agent", "cos", "chief-of-staff")):
            with self.subTest(kind[1]):
                self.setUp()
                self.worker()
                with mock.patch.object(sb, "gates_caller_kind", lambda runner, now, kind=kind: kind):
                    code, out, err = self.status_line()
                self.assertEqual(code, 0, err)

    def test_s5_a_name_that_is_not_a_cloud_worker_is_one_line_of_trouble(self):
        code, out, err = self.status_line("nobody")
        self.assertEqual(code, 1)
        self.assertIn("not a cloud worker", err)

    def test_s5_the_plain_status_command_is_unchanged(self):
        self.worker()
        code, out, err = self.run_command("status")
        self.assertIn("cloud: nodes", out + "\n".join(CLOUD.status_extra(subprocess.run)[0]))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
        return " ".join(handle.read().split())


DOCS = ("cloud/launcher/README.md", "docs/cloud-worker-interface.md")
STATES = ("starting", "running", "waiting on a prompt", "exited")
PROBLEM = ("used to list a cloud worker only by name, its stop time and `launcher status unavailable`", "looked like a running one")
SENTENCE = "The pod phase, the agent service's session state and its last message time"


def statements(text):
    """How many of the 3 statements (what it listed before, why a stuck pod looked running, what the launcher can read) the text makes."""
    return sum((PROBLEM[0] in text, PROBLEM[1] in text, SENTENCE.lower() in text.lower()))


def states_with_action(text):
    """The states that have an action: `<state>` followed, before the next state's mention in its row or bullet, by an imperative."""
    found = 0
    for state in STATES:
        row = re.search(r"`%s`[:|][^`]*?(?:wait|leave|read|retire|answer|launch)" % re.escape(state), text, re.I) or \
            re.search(r"\| `%s` \|[^|]*\|[^|]*(?:wait|leave|read|retire|launch)" % re.escape(state), text, re.I)
        found += bool(row)
    return found


class TestDocuments(unittest.TestCase):
    def test_s1_each_document_states_the_three_things(self):
        for rel in DOCS:
            with self.subTest(rel):
                self.assertEqual(statements(read(rel)), 3)

    def test_s1_a_document_without_the_stuck_pod_problem_shows_2_of_3(self):
        mutant = read(DOCS[0]).replace(PROBLEM[1], "looked fine")
        self.assertEqual(statements(mutant), 2)

    def test_s6_each_document_gives_each_of_the_four_states_an_action(self):
        for rel in DOCS:
            with self.subTest(rel):
                self.assertEqual(states_with_action(read(rel)), 4)

    def test_s6_a_document_without_the_waiting_action_shows_3_of_4(self):
        text = read(DOCS[0])
        start = text.index("`waiting on a prompt`: the agent service")
        end = text.index("- `exited`", start)
        self.assertEqual(states_with_action(text[:start] + text[end:]), 3)

    def test_the_line_and_its_bounds_are_in_the_readme(self):
        text = read(DOCS[0])
        for phrase in ("160 characters", "no state read within 60 s", "read only", "another lane's worker is refused"):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
