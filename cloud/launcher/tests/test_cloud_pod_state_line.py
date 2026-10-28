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
import shutil
import subprocess
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_status_summary as S  # noqa: E402  (the fake cluster, the records and the switchboard module)

sb, CLOUD, ALIVE = S.sb, S.CLOUD, S.ALIVE
ROOT = S.KIT
CLOUD.bind(sb.__dict__)   # the module is bound to the switchboard once here, so any class runs alone (#1397 root of the order dependence)
READ_VERBS = ("get", "describe", "logs", "top", "version", "api-resources", "cluster-info", "config", "auth")   # what a read-only command may ask the cluster
WRITE_VERBS = ("create", "patch", "delete", "label", "annotate", "exec", "apply", "port-forward", "replace", "scale", "cp", "run", "edit", "set",
               "rollout", "drain", "cordon", "uncordon", "taint", "expose", "autoscale")
GLOBAL_FLAGS_WITH_VALUE = ("--kubeconfig", "--context", "-n", "--namespace", "--cluster", "--user", "--server", "-s", "--token", "--request-timeout")


def kube_verb(call):
    """The verb of one recorded kubectl call: the first word that is not a global flag or its value (the call starts with `--kubeconfig PATH`)."""
    i = 0
    while i < len(call):
        word = call[i]
        if word in GLOBAL_FLAGS_WITH_VALUE:
            i += 2
        elif word.startswith("-"):
            i += 1
        else:
            return word
    return None
TOKEN = "ghp_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0dF3gH6"
PASSWORD = "Zq7-planted-phrasing"
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
        calls = self.kube_calls()
        verbs = [kube_verb(c) for c in calls]
        self.assertTrue(all(verbs), "no real verb read from %r" % (calls,))   # a check that cannot see a verb is not a check
        writes = [c for c, verb in zip(calls, verbs) if verb not in READ_VERBS]
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
        line = self.line_with_error("ERROR: connection refused by the server")
        self.assertIn("ERROR: connection refused by the server", line)

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

    def test_s5_the_status_command_issues_get_calls_only_and_the_check_sees_them(self):
        self.worker()
        code, out, err = self.status_line()
        self.assertEqual(code, 0, err)
        verbs = [kube_verb(c) for c in self.kube_calls()]
        self.assertGreaterEqual(len(verbs), 1)   # the check below has something to look at
        self.assertEqual(set(verbs), {"get"}, verbs)
        self.assertEqual(kube_verb(["--kubeconfig", "/x/config", "--context", "cw", "-n", "cw", "patch", "pod", "p"]), "patch")

    def test_s5_a_status_path_that_issues_one_write_call_fails_the_check(self):
        """The mutant: the read of the pod is followed by one `patch` (then one `delete`) of the pod; the read-only check must fail, mutation applied."""
        for verb in ("patch", "delete"):
            with self.subTest(verb):
                self.setUp()
                self.worker()
                applied = []
                real = CLOUD._cluster_snapshot

                def snapshot_then_write(runner, cluster, timeout, verb=verb, real=real):
                    found = real(runner, cluster, timeout)
                    applied.append(verb)
                    subprocess.run([os.environ["CW_KUBECTL"], "--kubeconfig", cluster.kubeconfig, "--context", "cw", "-n", "cw", verb, "pod", "cw-coder7"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=cluster.env())
                    return found
                with mock.patch.object(CLOUD, "_cluster_snapshot", snapshot_then_write):
                    self.status_line()
                self.assertEqual(applied, [verb])
                with self.assertRaises(AssertionError):
                    self.assert_read_only()

    def test_s5_the_transient_login_is_gone_afterwards_and_the_state_directory_stays_private(self):
        """The command logs in with a token file and a kubeconfig in a private directory under the temporary directory (removed on every way out), and
        sets the cloud directory of the state to mode 0700 (the same shared code as the other cloud commands)."""
        self.worker()
        root = Path(tempfile.gettempdir()) / ("cwtok-%d" % os.getuid())
        before = sorted(root.glob("cmd-*")) if root.exists() else []
        os.chmod(str(self.state_dir / "cloud"), 0o755)
        code, out, err = self.status_line()
        self.assertEqual(code, 0, err)
        after = sorted(root.glob("cmd-*")) if root.exists() else []
        self.assertEqual(after, before, "a transient login directory is left behind")
        self.assertEqual(os.stat(str(self.state_dir / "cloud")).st_mode & 0o777, 0o700)

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

    def test_the_inherent_limit_and_the_local_files_are_in_both_documents(self):
        for rel in DOCS:
            text = read(rel)
            for phrase in ("closed vocabulary", "ERROR_VOCABULARY", "bin/pod_state_line.py", "a secret that is itself one of the vocabulary", "transient", "removed when the command ends", "mode `0700`"):
                with self.subTest(rel=rel, phrase=phrase):
                    self.assertIn(phrase, text)


def leaked(secret, line, width=4):
    """The pieces of `secret` of `width` or more characters that the printed line still holds."""
    return [secret[i:i + width] for i in range(0, len(secret) - width + 1) if secret[i:i + width] in line]


def joined(*parts):
    return "".join(parts)


# every value below is synthetic and built from plain parts: no complete token shape sits in this file
SEVEN = "hunter" + "2"
TAIL = "q7planted" + "tail"
HALF_A, HALF_B = "k3Zx9Qw2Lm8Vb5N", "c1Rt7Yu4Ih6Gf0X"
BLOB = __import__("base64").b64encode(b"planted-synthetic-secret-value-0001").decode()
QUERY = "planted" + "Query" + "9x7Zk2"
HOSTILE = (
    ("marker on an earlier line", "Error: password:\n" + SEVEN, SEVEN),
    ("marker on an earlier line, longer gap", "Error: login failed\npassword:\n\n" + SEVEN, SEVEN),
    ("carriage return inside the value", "Error: password=" + SEVEN + "Z\r" + TAIL, TAIL),
    ("vertical tab inside the value", "Error: password=" + SEVEN + "Z\x0b" + TAIL, TAIL),
    ("line separator inside the value", "Error: password=" + SEVEN + "Z\u2028" + TAIL, TAIL),
    ("unmarked long secret cut by a zero-width space", "Error: bad " + HALF_A + "\u200b" + HALF_B, HALF_A + HALF_B),
    ("unmarked long secret cut by an escape character", "Error: bad " + HALF_A + "\x1b" + HALF_B, HALF_A + HALF_B),
    ("base64 blob split by a line break", "Error: bad " + BLOB[:20] + "\n" + BLOB[20:], BLOB),
    ("token in a URL query", "Error: GET https://host.example/path?token=" + QUERY + "&x=1 failed", QUERY),
    ("value after Authorization on the previous line", "Error: Authorization:\nBearer " + QUERY, QUERY),
    ("doubled keyword", "Error: password password: " + QUERY, QUERY),
)


class TestHostile(Base):
    """S2 again, through the real `status NAME` path: 0 planted pieces of 4 or more characters, whatever separates the value from its marker."""

    def printed(self, text):
        self.worker()
        self.v2.messages = [message(1798365852000, "", error=text)]
        code, out, err = self.status_line()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.splitlines()), 1, out)
        return out

    def test_s2_every_hostile_case_leaves_no_planted_piece(self):
        for kind, text, secret in HOSTILE:
            with self.subTest(kind):
                self.setUp()
                line = self.printed(text)
                self.assertEqual(leaked(secret, line), [], line)
                self.assertLessEqual(len(line.split("; last error ", 1)[1]), 160)

    def test_redact_error_directly_each_case(self):
        for kind, text, secret in HOSTILE:
            with self.subTest(kind):
                out = CLOUD.redact_error(text)
                self.assertEqual(leaked(secret, out), [], out)
                self.assertNotIn("\n", out)


# ---- the second round (ask #1397): a table of hostile texts, every one through the real `status NAME` path. Every secret is built from plain parts here.
V40 = "".join(("aEhWzjRci8hI", "pQ3mN7vXk2Ld", "Yt5sBw9ZcF1gHe4u", "Xo6Jr0Kq"))        # 44 characters, mixed letters and digits
V34 = "".join(("Mv4Hq8Zs1Wd", "Lc7Tn2Bx9Rf", "Pj5Ge3Ya6K"))                            # 31 characters
V30 = "".join(("Qe9Rt3Yu7Io", "Pa1Sd5Fg8Hj", "Kl2Zx6Cv"))                              # 29 characters
TOK = "gh" + "p_" + "yvok5" + "QwErTy" + "UiOp3Zx9Cv" + "BnM1aS"                         # a token shape, assembled at run time only
READABLE = "Error: could not open /x/y: File exists"
BREAKS = (("LF", "\n"), ("CR", "\r"), ("CRLF", "\r\n"), ("NEL", "\u0085"), ("LS", "\u2028"), ("PS", "\u2029"), ("VT", "\x0b"), ("FF", "\x0c"))
PAD = "ab " * 2000


def table():
    """(label, text, planted secrets) for each hostile case: the 8 shapes found by the independent proof, and variations of each."""
    cases = []
    for name, brk in BREAKS:   # (1) a line break of any kind between the marker and its value
        for kind, value in (("short", SEVEN), ("40", V40), ("token", TOK)):
            cases.append(("marker %s value, %s" % (name, kind), "Error: login failed" + brk + "password:" + brk + value, [value]))
    for chain in ("password: password: ", "password: token: ", "token: secret=", "auth: password: ", "Authorization: Bearer ", "password password: ",
                  "password: \r\npassword: \r\n", "secret: key: auth: "):   # (2) a doubled keyword, or a chain of them
        for kind, value in (("short", SEVEN), ("40", V40)):
            cases.append(("chain %r, %s" % (chain, kind), "Error: bad " + chain + value, [value]))
    for offset in (3000, 3985, 3990, 3994, 3999, 4000, 4005, 4100):   # (3) the secret starts at this character, on the last line, around the cut at 4000
        for kind, value in (("token", TOK), ("40", V40)):
            cases.append(("last line at %d, %s" % (offset, kind), "Error: boom\n" + PAD[:offset - 13] + "\n" + value, [value]))
    for secret in (V30, V34, V40):   # (4) an unmarked secret wrapped into two halves
        for name, brk in BREAKS[:3]:
            half = len(secret) // 2
            cases.append(("wrapped %d by %s" % (len(secret), name), "Error: bad " + secret[:half] + brk + secret[half:], [secret]))
    for marker in ("pass\u200bword: ", "tok\u200ben: ", "pa\u200bss\u200bword=", "sec\u2060ret: ", "pass\u00adword: ", "tok\ufeffen: ", "pass\u200dword = "):   # (5)
        for kind, value in (("short", SEVEN), ("40", V40)):
            cases.append(("split keyword %r, %s" % (marker, kind), "Error: " + marker + value, [value]))
    for kind, text, secret in HOSTILE:   # the earlier round, kept
        cases.append(("earlier: " + kind, text, [secret]))
    cases.append(("a 100000 character line then a secret", "x" * 100000 + " password: " + SEVEN, [SEVEN]))
    cases.append(("value after a keyword-colon-keyword on the next line", "token:\nsecret:\n" + SEVEN, [SEVEN]))
    cases.append(("all of it at once", PAD + "\r\nAuthorization:\r\nBearer\r\n" + TOK, [TOK]))
    return cases


class TestDenyByDefault(Base):
    """S2, second round: 0 planted pieces of 6 or more characters in the printed line, every case a plain readable error still prints readable."""

    def printed(self, text):
        self.worker()
        self.v2.messages = [message(1798365852000, "", error=text)]
        code, out, err = self.status_line()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.splitlines()), 1, out)
        return out

    def test_the_table_has_at_least_sixty_cases(self):
        self.assertGreaterEqual(len(table()), 60)

    def test_s2_no_planted_piece_of_six_characters_in_any_case_through_status(self):
        failures = []
        for label, text, secrets in table():
            self.setUp()
            line = self.printed(text)
            shown = [piece for secret in secrets for piece in leaked(secret, line)]
            if shown or len(line.split("; last error ", 1)[1]) > 160:
                failures.append((label, shown[:2]))
        self.assertEqual(failures, [], "%d of %d cases leak" % (len(failures), len(table())))

    def test_a_100000_character_line_is_handled_in_linear_time(self):
        for text in ("a" * 100000, "ab " * 40000, "password: " * 10000, "a1" * 50000 + " token", "=" * 100000, "x://" + "u:" * 50000, "A" * 99990 + "\r\n" * 5):
            began = time.monotonic()
            out = CLOUD.redact_error(text)
            self.assertLess(time.monotonic() - began, 5, text[:20])
            self.assertLessEqual(len(out), 160)

JWT_PARTS = ("ey" + "Jhb", "GciOiJIUzI1NiJ9.", "eyJzdWIiOiIxMjM0NTY3ODkw.", "SflKxwRJSMeKKF2QT4fwpM")
FORMS = {"short": SEVEN, "44": V40, "31": V34, "token": TOK, "jwt": "".join(JWT_PARTS), "sk": "s" + "k-" + "proj" + "AbCdEfGhIjKlMnOpQrSt", "passphrase": PASSWORD}
CONTEXTS = ("Error: password={}", 'Error: {{"password": "{}"}}', "export API_KEY={}", "Error: https://bob:{}@host.example/x failed", "Error: Authorization: Bearer {}",
            "Error: Authorization: Basic {}", "Error: --password {} failed", "Error: failed with passwd:{}", "Error: x\npassword\n{}", "Error: PASSWORD\r\n:\r\n{}",
            "Error: pass:\u200b{}", "Error: token\t{}", 'Error: {{"api_key":"{}","x":1}}', "Error: cookie: session={}; path=/", "Error: pwd = '{}'",
            'Error: secret: "{}" more', "Error: secret:\n\n\n{}", "Error: key {}", "Error: private: {}", "Error: credential: user {}", "Error: Bearer\n{}",
            "Error: session\u2028{}", "Error: tok\u200ben:\r\n{}", "Error: password: password: {}")


class TestContexts(unittest.TestCase):
    """S2: every form of a secret (7 forms) in every hostile context (24 contexts), alone and after 40 plain words: 336 texts, 0 pieces of 6 characters."""

    def test_s2_every_form_in_every_context_leaves_no_piece(self):
        failures = []
        for (kind, value), context in [(a, b) for a in FORMS.items() for b in CONTEXTS]:
            for before in ("", "x " * 40):
                shown = leaked(value, CLOUD.redact_error(before + context.format(value)))
                if shown:
                    failures.append((kind, context, shown[:1]))
        self.assertEqual(failures, [])

    def test_s2_the_known_secrets_of_the_run_never_show_in_any_context(self):
        known = {ENV_SECRET, PASSWORD}
        for context in CONTEXTS:
            for value in known:
                out = CLOUD.redact_error("Error: " + context.format(value[:5] + "\u200b" + value[5:]), known)
                self.assertEqual(leaked(value, out), [], out)


# ---- the allow-list round (ask #1397, plan revision 3): the error line prints token by token through ONE closed vocabulary (ERROR_VOCABULARY in
# bin/pod_state_line.py). Every secret below is synthetic and built from plain parts.
BASE_VOCAB = frozenset(CLOUD.ERROR_VOCABULARY) if hasattr(CLOUD, "ERROR_VOCABULARY") else frozenset()   # taken at import: a mutant that edits the live list is still judged by this copy
SEPARATORS = frozenset(" :,.()\"'/=-_[]")
CLASS_NAME = re.compile(r"[A-Z][A-Za-z]{0,39}")
CLASS_END = ("Error", "Exception", "Warning", "Timeout")
PLACEHOLDER = "[redacted]"
SENTINEL = "zq" + "uill" + "feather"   # a planted word: the mutant adds it to the list
CYR_PASSWORD = "\u043f\u0430\u0440\u043e\u043b\u044c"
CJK_PASSWORD = "\u5bc6\u7801"
KEYWORDS = ("password", "Passwort", "mot de passe", "contrasena", "senha", "clave", "wachtwoord", CYR_PASSWORD, CJK_PASSWORD, "pw", "p4ssw0rd", "otp code",
            "token", "secret", "api key", "the api key for the service is", "pass word", "pass phrase is", "Bearer", "Authorization", "kennwort", "losenord",
            "p\u0430ssword", "pa55phrase", "credential", "sesame", "mdp", "passe", "geheim", "tajne", "\u30d1\u30b9\u30ef\u30fc\u30c9", "\ube44\ubc00\ubc88\ud638")
ZW = ("\u200b", "\u200d", "\u2060", "\ufeff", "\u202e", "\u00ad")


def camel_words(name):
    """The CamelCase words of a name, split at lower-to-upper boundaries; a run of capitals is one word (HTTPError: HTTP, Error)."""
    return re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+", name)


def printed_tokens_ok(error_part, vocabulary=BASE_VOCAB):
    """The tokens of a printed error part that are NOT allowed: not the placeholder, not a vocabulary word, a number of at most 5 digits, a separator
    or an error-class name."""
    bad = []
    rest = error_part.replace(PLACEHOLDER, " ")
    for tok in re.findall(r"[^\W_]+|.", rest):
        if tok in SEPARATORS or tok.isspace():
            continue
        if tok.lower() in vocabulary or (tok.isascii() and tok.isdigit() and len(tok) <= 5):
            continue
        if tok.isascii() and tok.isalpha() and CLASS_NAME.fullmatch(tok) and tok.endswith(CLASS_END) and all(w.lower() in vocabulary for w in camel_words(tok)):
            continue
        bad.append(tok)
    return bad


def make_secret(rng, vocabulary):
    """A synthetic secret of 1 to 64 characters: letters and digits (a digit run of at most 3), holding no vocabulary word of 4 or more letters."""
    while True:
        out = []
        for _ in range(rng.randint(1, 64)):
            digits = len(out) >= 1 and out[-1].isdigit() and (len(out) < 2 or out[-2].isdigit())
            out.append(rng.choice("0123456789") if (not digits and rng.random() < 0.25) else rng.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"))
        secret = "".join(out)
        low = secret.lower()
        if not any(len(w) >= 4 and w in low for w in vocabulary) and not any(w in low for w in ("redact", "error")):
            return secret


def hostile_texts(count=1000, seed=1397):
    """(text, planted secret) pairs from a seeded generator: any keyword, any shape of splitting, wrapping, URL, path, quote and encoding."""
    import random
    rng = random.Random(seed)
    texts = []
    for n in range(count):
        secret = SENTINEL if n % 50 == 7 else make_secret(rng, BASE_VOCAB)
        kw = rng.choice(KEYWORDS)
        shape = rng.randrange(15)
        half = max(1, len(secret) // 2)
        if shape == 0:
            text = "%s: %s" % (kw, secret)
        elif shape == 1:
            text = "%s %s" % (kw, secret)
        elif shape == 2:
            text = "%s=%s" % (kw, secret)
        elif shape == 3:   # split by hyphens
            text = "%s %s" % (kw, "-".join(secret[i:i + rng.randint(1, 4)] for i in range(0, len(secret), 3)))
        elif shape == 4:   # split by spaces
            text = "%s: %s" % (kw, " ".join(secret[i:i + rng.randint(1, 5)] for i in range(0, len(secret), 4)))
        elif shape == 5:   # zero-width characters inside the secret and the keyword
            z = rng.choice(ZW)
            text = "%s%s%s: %s" % (kw[:2], z, kw[2:], z.join(secret))
        elif shape == 6:   # keyword on one line, secret on the next
            text = "failed\n%s:\n%s" % (kw, secret)
        elif shape == 7:   # wrapped over two lines
            text = "Error: bad\n%s: %s\n%s" % (kw, secret[:half], secret[half:])
        elif shape == 8:   # inside a URL
            text = "Error: GET https://host.example/v1/%s/%s/x?%s=%s failed" % (kw.replace(" ", "-"), secret, kw.replace(" ", "_"), secret)
        elif shape == 9:   # inside a path
            text = "Error: open /home/user/.%s/%s/%s: no such file" % (kw.replace(" ", "-"), secret, secret[:half])
        elif shape == 10:   # quotes
            text = 'Error: {"%s": "%s"} \'%s\'' % (kw, secret, secret)
        elif shape == 11:   # percent encoding
            text = "Error: %s=%s" % (kw.replace(" ", "+"), "".join("%%%02x" % ord(c) for c in secret))
        elif shape == 12:   # decimal and hex HTML encodings
            text = "Error: %s %s &#x%s;" % (kw, "".join("&#%d;" % ord(c) for c in secret), secret.encode().hex())
        elif shape == 13:   # hidden inside a CamelCase string that ends in Error or Exception
            secret = ("".join(c for c in secret if c.isalpha()) or "Zqxv").capitalize()
            text = "%s%s: failed" % (secret, rng.choice(("Error", "Exception")))
        else:   # after plain filler words
            text = "Error: %s is the one for the %s that was %s" % (kw, rng.choice(("service", "server", "account")), secret)
        if rng.random() < 0.2:
            text = text + "\n" + text.split("\n")[-1]
        texts.append((text, secret))
    return texts


class TestProperty(Base):
    """S9: 1000 seeded hostile texts (every fifth through `status NAME`, the rest through the same function): every printed token is allowed and 0 planted pieces of 4+ characters appear."""

    def run_texts(self, count=1000, vocabulary=BASE_VOCAB):
        """The failures: (text, what) for each text whose printed line holds a token that is not allowed or a planted piece."""
        failures = []
        for n, (text, secret) in enumerate(hostile_texts(count)):
            if n % 5 and n % 50 != 7:   # four texts of five go straight through the redaction (the time of the whole file), the others through the command
                part = CLOUD.redact_error(text)
            else:
                self.worker()
                self.v2.messages = [message(1798365852000, "", error=text)]
                code, out, err = self.status_line()
                self.assertEqual(code, 0, err)
                self.assertEqual(len(out.splitlines()), 1, out)
                part = out.split("; last error ", 1)[1]
            bad = printed_tokens_ok(part, vocabulary)
            pieces = leaked(secret, part)
            if bad or pieces or len(part) > 160:
                failures.append((text[:60], bad[:2], pieces[:2]))
        return failures

    def test_s9_the_generator_is_seeded_and_makes_a_thousand_varied_texts(self):
        texts = hostile_texts()
        self.assertEqual(len(texts), 1000)
        self.assertEqual(texts, hostile_texts())
        self.assertGreaterEqual(len({len(secret) for _, secret in texts}), 40)

    def test_s9_1000_of_1000_texts_print_only_allowed_tokens_and_no_planted_piece(self):
        self.assertTrue(BASE_VOCAB, "the vocabulary is not there")
        failures = self.run_texts()
        self.assertEqual(failures, [], "%d of 1000 texts print something that is not allowed" % len(failures))

    def test_s9_a_printed_token_outside_the_vocabulary_is_seen_by_the_check(self):
        self.assertEqual(printed_tokens_ok("Error: " + "hunter2", frozenset(("error",))), ["hunter2"])
        self.assertEqual(printed_tokens_ok("RuntimeError: [redacted] 404 and", frozenset(("and", "runtime", "error"))), [])


class TestVocabularyScan(Base):
    """S12: ONE list in ONE place, no second copy, no keyword table; a mutant that adds a planted word is caught by the property test."""

    def sources(self):
        found = []
        for base in ("bin", "cloud/launcher"):
            for path in sorted((Path(ROOT) / base).rglob("*")):
                if path.suffix in (".py", ".sh", "") and path.is_file() and "tests" not in path.parts and "__pycache__" not in path.parts:
                    try:
                        found.append((str(path.relative_to(ROOT)), path.read_text(encoding="utf-8")))
                    except (UnicodeDecodeError, OSError):
                        pass
        return found

    def test_s12_the_vocabulary_is_one_frozenset_of_about_two_hundred_words_in_the_worker_module(self):
        vocabulary = CLOUD.ERROR_VOCABULARY
        self.assertIs(type(vocabulary), frozenset)
        self.assertTrue(150 <= len(vocabulary) <= 260, len(vocabulary))
        self.assertTrue(all(w == w.lower() and w.isascii() and w.isalpha() for w in vocabulary))
        for everyday in ("horse", "battery", "staple", "apple", "hunter", "correct"):
            self.assertNotIn(everyday, vocabulary)

    def test_s12_no_second_copy_of_the_list_and_no_old_keyword_table_remains(self):
        signature = re.compile(r"refused\W+denied\W+forbidden\W+unauthorized\W+timeout")
        old = re.compile("_STRONG" + "_WORDS|_WEAK" + "_WORDS|_SKIP" + "_WORDS|def _key" + "word\\(|def _value" + "_like\\(")
        copies, tables = [], []
        for rel, text in self.sources():
            copies += [rel] * len(signature.findall(text))
            tables += [rel] * len(old.findall(text))
        self.assertEqual(copies, ["bin/pod_state_line.py"], copies)
        self.assertEqual(tables, [], tables)

    def test_s12_a_mutant_that_adds_a_planted_word_is_caught_by_the_property_test(self):
        mutant = CLOUD.ERROR_VOCABULARY | {SENTINEL}
        with mock.patch.object(CLOUD._state_module(), "ERROR_VOCABULARY", mutant):   # the list lives in the module bin/pod_state_line.py
            self.assertIn(SENTINEL, CLOUD.ERROR_VOCABULARY)   # the mutation is applied
            failures = TestProperty.run_texts(self, count=100)   # the planted word comes at the 8th text and every 50th after
        self.assertGreaterEqual(len(failures), 1)


def survivors():
    """The nine survivor shapes: (label, text, planted values)."""
    value = SEVEN
    cases = [("a: %s" % kw, "Error: login failed: %s: %s" % (kw, value), [value])
             for kw in ("Passwort", "mot de passe", "contrasena", "senha", "clave", "wachtwoord", CYR_PASSWORD, CJK_PASSWORD)]
    cases += [("b: lookalike", "Error: p\u0430ssword: %s" % value, [value])]
    cases += [("c: %s" % kw, "Error: bad %s: %s" % (kw, value), [value]) for kw in ("p4ssw0rd", "pw")]
    cases += [("c: otp code", "Error: bad otp code: 482913", ["482913"])]
    cases += [("d: api key", "Error: the api key for the service is %s" % value, [value])]
    cases += [("e: pass word", "Error: pass word: %s" % value, [value]), ("e: pass phrase", "Error: pass phrase is %s" % value, [value])]
    cases += [("f: four words", "Error: correct horse battery staple", ["correct", "horse", "battery", "staple"])]
    twelve = "qzxwvb" + "nmklpj"
    cases += [("g: wrapped 12 letters", "Error: bad %s\n%s" % (twelve[:6], twelve[6:]), [twelve, twelve[:6], twelve[6:]])]
    cases += [("h: url path", "Error: GET /v1/users/%s/.." % value, [value])]
    twenty = "qzjxkvwpmb" + "hgtfdlnsyr"
    cases += [("i: twenty letters", "Error: bad %s" % twenty, [twenty])]
    return cases


class TestSurvivors(Base):
    """S10: the nine survivor shapes; none of the planted values prints."""

    def test_s10_none_of_the_planted_values_prints_and_nine_of_nine_shapes_are_there(self):
        shapes = {label[0] for label, _, _ in survivors()}
        self.assertEqual(shapes, set("abcdefghi"))
        failures = []
        for label, text, values in survivors():
            self.setUp()
            self.worker()
            self.v2.messages = [message(1798365852000, "", error=text)]
            code, out, err = self.status_line()
            self.assertEqual(code, 0, err)
            part = out.split("; last error ", 1)[1]
            shown = [v for v in values if v in part]
            if shown or printed_tokens_ok(part):
                failures.append((label, shown, printed_tokens_ok(part)))
        self.assertEqual(failures, [])


class TestCamelSurvivors(Base):
    """S10 (class names): a secret written as a CamelCase string ending in Error or Exception does not print."""

    def test_s10_a_secret_inside_a_camel_case_error_name_does_not_print(self):
        for name, planted in (("MySecretPasswordHunterTwoError", "Hunter"), ("QzjxkvwpmbhgtfdlnsyrError", "Qzjxkvwpmb"), ("OtpCodeFourEightTwoException", "Four")):
            with self.subTest(name):
                self.setUp()
                self.worker()
                self.v2.messages = [message(1798365852000, "", error=name + ": failed")]
                code, out, err = self.status_line()
                self.assertNotIn(planted, out)
                self.assertIn(PLACEHOLDER, out)


# S11: (text, the cause words that must stay, whether a NAME is lost to the placeholder)
POD_ERRORS = (
    ("failed to pull image: pull access denied", ("failed to pull image: pull access denied",), False),
    ("push refused: protected branch", ("push refused: protected branch",), False),
    ("RuntimeError: agent crashed", ("RuntimeError: agent crashed",), False),
    ("exceeded quota: pods", ("exceeded quota: pods",), False),
    ("ErrImagePull", ("ErrImagePull",), False),
    ("ModuleNotFoundError: No module named 'widgetlib'", ("ModuleNotFoundError", "No module named"), True),
    ("pods \"coder7\" is forbidden: User cannot list", ("is forbidden: User cannot list",), True),
    ("could not open /x/y: File exists", ("could not open", "File exists"), True),
    ("code = NotFound desc = failed to find", ("code = NotFound desc = failed to find",), False),
)


class TestRealErrors(Base):
    """S11: nine real-shaped pod errors keep their cause word or error class. Six keep the whole line (pull refused, push refused by the guard, agent
    crash, quota, ErrImagePull, code = NotFound); the three that name something (a module, a pod, a directory) lose that NAME to the placeholder and
    keep the cause word and the error class. Nothing more is claimed than the vocabulary holds."""

    def test_s11_each_keeps_its_cause_and_at_most_three_lose_a_name(self):
        lost = []
        for text, causes, loses in POD_ERRORS:
            with self.subTest(text):
                self.setUp()
                self.worker()
                self.v2.messages = [message(1798365852000, "", error=text)]
                code, out, err = self.status_line()
                part = out.split("; last error ", 1)[1]
                for cause in causes:
                    self.assertIn(cause, part)
                self.assertEqual(PLACEHOLDER in part, loses, part)
                lost += [text] * (PLACEHOLDER in part)
        self.assertEqual(len(POD_ERRORS), 9)
        self.assertLessEqual(len(lost), 3)


class TestClassNames(Base):
    """The fix of the fourth proof: a CamelCase name prints only if EVERY CamelCase word of it is a word of the vocabulary."""
    PRINT = ("ModuleNotFoundError", "TimeoutError", "RuntimeError", "ConnectionResetError", "PermissionError", "FileExistsError", "HTTPError",
             "CalledProcessError")
    HIDDEN = ("MySecretPasswordHunterTwoError", "QzxwvbnmklpjhgtfdlnsyrabcdefghijkError", "QzxwvbnmklpjhgtfdlnsyrabcdefghiError",
              "HunterTwoException", "PasswordHunterError", "ZZZError", "SecretKeyXqzvException")

    def test_the_readable_error_classes_print(self):
        for name in self.PRINT:
            with self.subTest(name):
                self.assertEqual(CLOUD.redact_error(name + ": failed"), name + ": failed")

    def test_a_name_with_a_word_outside_the_vocabulary_is_the_placeholder(self):
        for name in self.HIDDEN:
            with self.subTest(name):
                out = CLOUD.redact_error(name + ": failed")
                self.assertEqual(out, "[redacted]: failed")

    def test_the_arbitrary_35_letters_ending_in_error_do_not_print(self):
        name = "Qzxwvbnmklpjhgtfdlnsyrabcdefghijk"[:30] + "Error"
        self.assertEqual(len(name), 35)
        self.assertNotIn("Qzxw", CLOUD.redact_error("failed " + name))

    def test_the_words_are_split_at_lower_to_upper_and_an_acronym_is_one_word(self):
        self.assertEqual(camel_words("HTTPError"), ["HTTP", "Error"])
        self.assertEqual(camel_words("CalledProcessError"), ["Called", "Process", "Error"])

    def test_the_vocabulary_stays_short(self):
        self.assertLessEqual(len(CLOUD.ERROR_VOCABULARY), 260)


class TestPlaceholderAndNormalising(unittest.TestCase):
    def test_a_run_of_placeholders_and_separators_is_one_placeholder(self):
        self.assertEqual(CLOUD.redact_error("failed: /aa1/bb2/cc3 a1b2"), "failed: /[redacted]")

    def test_a_token_is_allowed_only_if_allowed_in_both_readings(self):
        out = CLOUD.redact_error("Error: tok\u200ben pw\u200b9 and failed")
        self.assertNotIn("pw", out)
        self.assertIn("failed", out)

    def test_an_error_class_name_and_a_short_number_print_and_a_long_number_does_not(self):
        out = CLOUD.redact_error("ConnectionResetError 404 123456")
        self.assertIn("ConnectionResetError", out)
        self.assertIn("404", out)
        self.assertNotIn("123456", out)

    def test_every_other_token_is_the_placeholder_in_any_language(self):
        self.assertEqual(CLOUD.redact_error("error: " + CYR_PASSWORD + " " + CJK_PASSWORD), "error: [redacted]")

    def test_a_two_million_character_line_prints_within_ten_seconds_and_a_longer_one_prints_a_phrase(self):
        began = time.monotonic()
        out = CLOUD.redact_error("ab1 " * 500000)
        self.assertLess(time.monotonic() - began, 10)
        self.assertLessEqual(len(out), 160)
        self.assertEqual(CLOUD.redact_error("a" * 2000001), "[error text too long to show]")


class TestTransientLogin(Base):
    """S5 (extended): the token file and kubeconfig sit in a private 0700 directory and are GONE before the command exits, on success and on 2 error paths."""

    def watch(self):
        seen = []
        real = CLOUD.Cluster.close

        def close(cluster):
            if cluster.dir:
                seen.append((cluster.dir, os.stat(cluster.dir).st_mode & 0o777, sorted(os.listdir(cluster.dir))))
            return real(cluster)
        patch = mock.patch.object(CLOUD.Cluster, "close", close)
        patch.start()
        self.addCleanup(patch.stop)
        return seen

    def check(self, seen, files):
        self.assertEqual(len(seen), 1, seen)
        directory, mode, names = seen[0]
        self.assertEqual(mode, 0o700)
        for name in files:
            self.assertIn(name, names)
        self.assertFalse(os.path.exists(directory), "the login directory is left behind")

    def test_s5_success_the_login_files_are_private_and_gone(self):
        self.worker()
        seen = self.watch()
        code, out, err = self.status_line()
        self.assertEqual(code, 0, err)
        self.check(seen, ("token", "config"))

    def test_s5_error_path_one_a_launcher_that_cannot_be_reached(self):
        self.worker()
        seen = self.watch()
        state = self.cluster()
        state["fail_cmd"] = "get"
        self.write_cluster(state)
        os.environ["FAKE_KUBE_ECHO"] = "the cluster is down"
        self.addCleanup(os.environ.pop, "FAKE_KUBE_ECHO", None)
        code, out, err = self.status_line()
        self.assertEqual(code, 1, (out, err))
        self.assertIn("state not read", out)
        self.check(seen, ("token", "config"))

    def test_s5_error_path_two_a_login_that_fails_after_the_token_file_is_written(self):
        self.worker()
        seen = self.watch()
        with mock.patch.object(CLOUD.json, "dump", side_effect=OSError(28, "No space left on device")):
            code, out, err = self.status_line()
        self.assertNotEqual(code, 0)
        self.check(seen, ("token",))

    def test_s5_a_build_that_leaves_the_token_file_behind_is_seen(self):
        self.worker()
        seen = self.watch()
        with mock.patch.object(CLOUD.shutil, "rmtree", lambda *a, **k: None):
            self.status_line()
        self.assertTrue(seen and os.path.exists(seen[0][0]))
        shutil.rmtree(seen[0][0], ignore_errors=True)


class Clock:
    """A fake clock: a pause moves it, nothing waits for real; a pause past the allowed bound is recorded as an overrun and refused."""

    def __init__(self, limit=70):
        self.start = self.now = 1000.0
        self.limit, self.overrun, self.pauses = limit, False, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.pauses.append(seconds)
        self.now += max(seconds, 0.001)
        if self.now - self.start > self.limit:
            self.overrun = True
            raise RuntimeError("the fake clock ran past the bound")


_loader = __import__("importlib.machinery").machinery.SourceFileLoader("cw_test_cloud_worker_mod_1397", os.path.join(ROOT, "bin", "test-cloud-worker"))
_spec = __import__("importlib.util").util.spec_from_loader("cw_test_cloud_worker_mod_1397", _loader)
tcw = __import__("importlib.util").util.module_from_spec(_spec)
sys.modules["cw_test_cloud_worker_mod_1397"] = tcw
_loader.exec_module(tcw)


class TestLaunchCommand(tcw.Base):
    """S4 through the real `launch --cloud` path (the real cmd_launch with the existing fake cluster tools and fake agent service of the cloud tests)."""

    def setUp(self):
        self.addCleanup(CLOUD.bind, sb.__dict__)   # the cloud module is shared by both switchboard copies: this file's own is bound again afterwards
        super().setUp()
        self.cw = tcw.sb.load_module("cloud_worker")
        self.clock = Clock()
        self.calls, self.v2 = [], FakeV2(messages=[])
        pods = {"pods": [{"name": "coder7-7f9c", "worker": "coder7", "phase": "Running"}]}
        real = self.cw.launch_state_line

        def watched(runner, state, cluster, record, name, v2, accepted=None, seconds=60):
            self.calls.append(len(self.remote.messages))   # the brief had reached the pod when the line was asked for
            return real(runner, state, cluster, record, name, self.v2, accepted, seconds if self.seconds is None else self.seconds)
        self.seconds = None
        for target, name, value in ((self.cw, "_cluster_snapshot", lambda *a, **k: pods), (self.cw, "launch_state_line", watched),
                                    (self.cw, "_clock", self.clock), (self.cw, "_sleep", self.clock.sleep)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def go(self):
        began = time.monotonic()
        code, out, err = self.launch()
        return code, out, err, time.monotonic() - began

    def check_bounded(self):
        self.assertFalse(self.clock.overrun, "the wait ran past its bound")
        self.assertLessEqual(self.clock.now - self.clock.start, 65)

    def test_s4_the_answering_pod_prints_its_line_once_after_the_brief(self):
        code, out, err, _ = self.go()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertEqual(json.loads(out)["name"], "coder7")
        self.assertEqual(err.count("cloud coder7: running; last tool call or message "), 1, err)
        self.assertEqual(len(self.calls), 1)
        self.assertGreaterEqual(self.calls[0], 1)   # after the brief was accepted
        self.assertEqual(self.clock.pauses, [])

    def test_s4_a_silent_pod_says_so_ends_normally_and_waits_only_on_the_fake_clock(self):
        def silent(*a, **k):
            raise tcw.sb.V2Unreachable("no answer")
        self.v2.permission_list = silent
        code, out, err, real = self.go()
        self.assertEqual(code, 0, err)   # the launch's normal exit status
        self.assertEqual(err.count("cloud coder7: no state read within 60 s"), 1, err)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertGreaterEqual(self.clock.now - self.clock.start, 60)
        self.check_bounded()
        self.assertLess(real, 5, "the test took real seconds")

    def test_s4_a_launch_that_waits_without_a_bound_fails_the_same_checks(self):
        def silent(*a, **k):
            raise tcw.sb.V2Unreachable("no answer")
        self.v2.permission_list = silent
        self.seconds = 10 ** 6   # the mutant: no bound that matters
        self.go()
        with self.assertRaises(AssertionError):
            self.check_bounded()


if __name__ == "__main__":
    unittest.main()
