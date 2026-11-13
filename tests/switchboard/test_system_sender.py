"""Ask #2518: the cloud reaper sends as a registered system sender, proved by the calling process (bin/system_sender.py).

Scenarios S2, S3, S4, S6, S7 and U1 of the plan. The "process that is the sender" is a real child of a real fake reaper (a scratch kit with
cloud/launcher/cloud_worker.py, started as the installed service starts it); the send itself runs over the fake herdr and v2 of the test world.
Controls: the same call with no claim is what the current code does for the reaper (it cannot tell who it is)."""
import contextlib
import importlib.util
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest import mock

from support.core import Base
from support.herdr_world import World
from support.loader import captured, sb
from support.paths import REPO
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE

ENV = "FLEET_SWITCHBOARD_SYSTEM_SENDER"
NAME = "cloud-reaper"
SYSTEM = sb.load_module("system_sender")


def load_land_report():
    spec = importlib.util.spec_from_file_location("land_report_for_2518", str(REPO / "cloud" / "launcher" / "land_report.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Kit:
    """A scratch kit holding a stand-in for the launcher's script, and the processes started from it."""

    def __init__(self, case):
        self.root = case.home / "kit"
        self.script = self.root / "cloud" / "launcher" / "cloud_worker.py"
        self.script.parent.mkdir(parents=True)
        self.script.write_text("import time\ntime.sleep(120)\n")
        os.chmod(str(self.script), 0o644)
        self.case = case

    def start(self, *argv, program=None, script=None):
        proc = subprocess.Popen([program or sys.executable] + list(argv or ("-B", str(script or self.script), "reap", "--loop", "60")),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.case.addCleanup(proc.wait)
        self.case.addCleanup(proc.terminate)
        mine = Path("/proc/%d/cmdline" % os.getpid()).read_bytes() if sys.platform.startswith("linux") else None
        deadline = time.time() + 10
        while mine is not None and time.time() < deadline and Path("/proc/%d/cmdline" % proc.pid).read_bytes() == mine:   # the child has exec'd
            time.sleep(0.01)
        return proc.pid


class SendRig(Base):
    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("cos", "coder"))
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        self.kit = Kit(self)
        patch = mock.patch.object(sb, "KIT_ROOT", self.kit.root)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop(ENV, None)
        os.environ.pop("HERDR_PANE_ID", None)
        os.environ.pop(sb.SESSION_ENV, None)

    def send(self, *argv, parent=None, lab=False, **env):
        """`send cos ...` as one process whose parent is `parent` (a pid), with the claim variable and others set as given."""
        if not lab:
            os.environ.pop(sb.LAB_ENV, None)
        os.environ.update(env)
        patcher = mock.patch.object(os, "getppid", lambda: parent) if parent is not None else contextlib.nullcontext()
        with patcher, captured() as (out, err):
            code = sb.main(list(argv) or ["send", "cos", "--issue", "2", "daily report"], runner=self.world.route,
                           now=self.world.clock.now, wait=lambda sock, seconds: False, socket_call=self.world.herdr.socket)
        return code, out.getvalue(), err.getvalue()

    def delivered(self):
        return [m for m in self.world.admissions("cos")]

    def senders(self):
        return [m["keys"][0].split(":")[1] for m in self.delivered()]


class TestSystemSenderSend(SendRig):
    def test_s2_the_reaper_process_is_delivered_labelled_with_its_name(self):
        reaper = self.kit.start()
        code, out, err = self.send(parent=reaper, **{ENV: NAME})
        self.assertEqual(code, 0, err)
        self.assertEqual(self.senders(), [NAME])

    def test_s2_control_the_same_call_with_no_claim_is_what_the_current_code_does_to_the_reaper(self):
        reaper = self.kit.start()
        code, out, err = self.send(parent=reaper)
        self.assertNotEqual(code, 0)
        self.assertIn("cannot tell who you are", err)
        self.assertEqual(self.delivered(), [])

    def impostor(self, **kw):
        before = len(self.delivered())
        code, out, err = self.send(**kw)
        self.assertEqual(code, 1, err)
        self.assertEqual(len([line for line in err.splitlines() if line.strip()]), 1, err)
        self.assertEqual(len(self.delivered()), before)
        return err

    def test_s3_no_proof_a_wrong_proof_and_an_unregistered_name_are_refused_with_one_line(self):
        err = self.impostor(parent=os.getpid(), **{ENV: NAME})                       # a process that is not the reaper (the test runner)
        self.assertIn("not the system sender %s" % NAME, err)
        wrong = self.kit.start("-B", str(self.kit.script), "stop")                  # the right script, the wrong word
        self.assertIn("not the system sender", self.impostor(parent=wrong, **{ENV: NAME}))
        reaper = self.kit.start()
        self.assertIn("not a registered system sender", self.impostor(parent=reaper, **{ENV: "cloud-reaper-2"}))
        self.assertIn("not a registered system sender", self.impostor(parent=reaper, **{ENV: "cos"}))

    def test_s3_forged_senders_are_all_refused(self):
        coder = self.world.panes["coder"]
        session = self.world.sessions["coder"]
        self.assertIn("not the system sender", self.impostor(HERDR_PANE_ID=coder, **{ENV: NAME}))              # a pane
        self.assertIn("not the system sender", self.impostor(**{sb.SESSION_ENV: session, ENV: NAME}))           # a session id
        self.assertIn("not the system sender", self.impostor(**{ENV: NAME}))                                    # only the variable set to the name
        self.assertIn("not the system sender", self.impostor(lab=True, **{ENV: NAME}))                          # the variable in the lab
        for lab in (False, True):                                                                               # --from naming the sender
            with self.subTest(lab=lab):
                if lab:
                    os.environ[sb.LAB_ENV] = "1"
                else:
                    os.environ.pop(sb.LAB_ENV, None)
                before = len(self.delivered())
                with captured() as (out, err):
                    code = sb.main(["send", "--from", NAME, "cos", "--issue", "2", "x"], runner=self.world.route, now=self.world.clock.now,
                                   wait=lambda sock, seconds: False, socket_call=self.world.herdr.socket)
                self.assertEqual(code, 1, err.getvalue())
                self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
                self.assertEqual(len(self.delivered()), before)

    def test_s3_the_reaper_process_with_a_from_naming_another_is_refused_and_so_is_another_recipient(self):
        reaper = self.kit.start()
        with mock.patch.object(os, "getppid", lambda: reaper), captured() as (out, err):
            os.environ[ENV] = NAME
            code = sb.main(["send", "coder", "--issue", "2", "x"], runner=self.world.route, now=self.world.clock.now,
                           wait=lambda sock, seconds: False, socket_call=self.world.herdr.socket)
        self.assertEqual(code, 1)
        self.assertIn("may write only to cos", err.getvalue())
        self.assertEqual(self.world.admissions("coder"), [])
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)

    def test_s6_a_persons_pane_and_the_lab_from_are_delivered_with_the_labels_they_had(self):
        os.environ["HERDR_PANE_ID"] = self.world.panes["coder"]
        code, _, err = self.send(lab=True)
        self.assertEqual(code, 0, err)
        os.environ.pop("HERDR_PANE_ID")
        with captured() as (out, err):
            code = sb.main(["send", "--from", "coder", "cos", "--issue", "2", "again"], runner=self.world.route, now=self.world.clock.now,
                           wait=lambda sock, seconds: False, socket_call=self.world.herdr.socket)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(self.senders(), ["coder", "coder"])

    def test_s6_a_persons_pane_outside_the_lab_is_delivered_as_before_even_in_the_reapers_process_tree(self):
        reaper = self.kit.start()
        os.environ["HERDR_PANE_ID"] = self.world.panes["coder"]
        code, _, err = self.send(parent=reaper)                                      # no claim: the pane identifies, as before
        self.assertEqual(code, 0, err)
        self.assertEqual(self.senders(), ["coder"])


class TestParentProof(Base):
    """The proof itself, against real processes."""

    def setUp(self):
        super().setUp()
        self.kit = Kit(self)

    def proof(self, pid, **kw):
        return SYSTEM.parent_is(NAME, self.kit.root, parent=pid, **kw)

    def test_the_registered_program_is_the_sender(self):
        self.assertTrue(self.proof(self.kit.start()))
        self.assertTrue(self.proof(self.kit.start("-B", str(self.kit.script), "reap")))

    def test_everything_else_is_not(self):
        other = self.home / "other"
        (other / "cloud" / "launcher").mkdir(parents=True)
        (other / "cloud" / "launcher" / "cloud_worker.py").write_text("import time\ntime.sleep(120)\n")
        cases = {
            "this test process": os.getpid(),
            "no such process": 2 ** 22 + 5,
            "another script": self.kit.start("-B", str(other / "cloud" / "launcher" / "cloud_worker.py"), "reap"),
            "another verb": self.kit.start("-B", str(self.kit.script), "create"),
            "no verb": self.kit.start("-B", str(self.kit.script)),
            "python -c": self.kit.start("-c", "import time; time.sleep(120)", str(self.kit.script), "reap"),
            "a relative path": self.kit.start("-B", "cloud/launcher/cloud_worker.py", "reap"),
        }
        for label, pid in cases.items():
            with self.subTest(label):
                self.assertFalse(self.proof(pid))

    def test_another_user_and_a_writable_program_are_not(self):
        pid = self.kit.start()
        self.assertTrue(self.proof(pid))
        self.assertFalse(self.proof(pid, uid=os.getuid() + 1))
        os.chmod(str(self.kit.script), 0o666)
        self.assertFalse(self.proof(pid))

    def test_a_reader_that_fails_is_no_proof(self):
        self.assertFalse(SYSTEM.parent_is(NAME, self.kit.root, parent=1, reader=lambda pid: None))
        self.assertFalse(SYSTEM.parent_is("nobody", self.kit.root, parent=1))

    def test_a_real_child_of_the_reaper_is_proved_by_its_own_parent_and_a_shell_child_is_not(self):
        """No patched getppid: the registered program starts a child that asks, and so does a plain shell of the test."""
        out = self.home / "answer.txt"
        probe = ("import sys; sys.path.insert(0, %r); import system_sender as s; "
                 "print(s.refusal('cloud-reaper', 'cos', %r))" % (str(REPO / "bin"), str(self.kit.root)))
        self.kit.script.write_text("import subprocess, sys\nopen(%r, 'w').write(subprocess.run([sys.executable, '-c', %r], capture_output=True, text=True).stdout)\n"
                                   % (str(out), probe))
        subprocess.run([sys.executable, "-B", str(self.kit.script), "reap"], check=True, timeout=60)
        self.assertEqual(out.read_text().strip(), "None")
        shell = subprocess.run(["sh", "-c", "%s -c \"$0\"" % sys.executable, probe], capture_output=True, text=True, timeout=60)
        self.assertEqual(shell.stdout.strip(), "not sent: this process is not the system sender cloud-reaper")

    def test_the_registry_names_the_reaper_and_only_cos(self):
        self.assertEqual(sorted(SYSTEM.SYSTEM_SENDERS), [NAME])
        self.assertEqual(SYSTEM.SYSTEM_SENDERS[NAME]["recipients"], ("cos",))
        self.assertEqual(SYSTEM.SYSTEM_SENDER_ENV, ENV)


class TestLandReportUsesTheSender(Base):
    """S4 and S7: the land report's message part names the registered sender and is sent on its first try; a refusal is not a send."""

    def setUp(self):
        super().setUp()
        self.land = load_land_report()

    def runner(self, accept, calls):
        def run(argv, **kw):
            env = kw.get("env") or {}
            calls.append((argv, env))
            ok = accept(argv, env) and "HERDR_PANE_ID" not in env and sb.SESSION_ENV not in env
            return type("R", (), {"returncode": 0 if ok else 1})()
        return run

    def test_s4_the_message_part_is_sent_once_with_the_system_sender_and_no_pane_variable(self):
        calls = []
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("HERDR_PANE_ID", None)
            os.environ.pop(sb.SESSION_ENV, None)
            ok = self.land.send_message("report", "acme/api#7", run=self.runner(lambda a, e: e.get(ENV) == NAME, calls))
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][:4], ["fl" + "eet-switchboard", "send", "cos", "--issue"])
        self.assertEqual(calls[0][1][ENV], NAME)
        self.assertEqual(self.land.SYSTEM_SENDER, NAME)

    def pass_with(self, accept, state_path, calls):
        import datetime as dt
        now = dt.datetime(2026, 11, 12, 13, 5, tzinfo=dt.timezone.utc)
        root = self.home / "records"
        root.mkdir(exist_ok=True)
        run = self.runner(accept, calls)
        return self.land.daily_pass({"CW_LAND_REPORT_ISSUE": "acme/api#7"}, now, str(root), str(state_path),
                                    send=lambda text, issue: self.land.send_message(text, issue, run=run),
                                    comment=lambda text, issue: True, git=lambda *a: [])

    def test_s4_the_state_file_records_the_message_sent_on_the_first_try(self):
        import json
        calls, state = [], self.home / "state.json"
        self.assertEqual(self.pass_with(lambda a, e: e.get(ENV) == NAME, state, calls), "sent")
        recorded = json.loads(state.read_text())
        self.assertEqual((len(calls), recorded["message"], recorded["message_tries"]), (1, True, 1))

    def test_s4_control_a_send_with_no_sender_is_refused_three_times_in_three_passes(self):
        import json
        calls, state = [], self.home / "state.json"
        for _ in range(3):
            self.pass_with(lambda a, e: False, state, calls)
        recorded = json.loads(state.read_text())
        self.assertEqual((recorded["message"], recorded["message_tries"]), (False, 3))

    def test_s7_a_refusal_is_not_recorded_as_sent_and_is_tried_again_at_the_next_pass(self):
        import json
        calls, state = [], self.home / "state.json"
        self.assertEqual(self.pass_with(lambda a, e: False, state, calls), "partial")
        recorded = json.loads(state.read_text())
        self.assertEqual((recorded["message"], recorded["message_tries"]), (False, 1))
        self.assertEqual(self.pass_with(lambda a, e: True, state, calls), "sent")
        recorded = json.loads(state.read_text())
        self.assertEqual((recorded["message"], recorded["message_tries"], len(calls)), (True, 2, 2))
