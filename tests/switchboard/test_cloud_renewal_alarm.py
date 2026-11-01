"""#1579, part three: the alarm on a failed git-token renewal of a cloud pod (scenarios S16 to S19 of the plan), with a fake audit file, a fake send and a fake clock.

The scenario functions take the module to test, so the same functions run on a scratch copy with one mutation in a child process (the four mutants)."""
import calendar
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
BIN = TESTS_DIR.parent.parent / "bin"
sys.path.insert(0, str(TESTS_DIR))
from support.core import Base   # noqa: E402
from support.loader import sb   # noqa: E402

T0 = float(calendar.timegm((2026, 10, 30, 12, 0, 0)))
PLANT = "ghs_PLANTEDsecretVALUE0123456789abcdefABCDEF"


def load(path=None):
    spec = importlib.util.spec_from_file_location("cloud_renewal_alarm_under_test", str(path or BIN / "cloud_renewal_alarm.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stamp(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


class AlarmWorld:
    """A state directory, the launcher's audit file, a fake send and the clock."""

    def __init__(self, module, bosses=None, lane=None):
        self.module = module
        self.dir = tempfile.mkdtemp(prefix="alarm-world-")
        self.audit_file = os.path.join(self.dir, "audit.jsonl")
        self.env = {"CW_CLOUD_AUDIT": self.audit_file}
        self.now = T0
        self.messages = []
        self.entries = []
        mk = lambda n, role: types.SimpleNamespace(name=n, session_id=n, status="idle", fleet={"role": role})
        self.agents = {"cos-1": mk("cos-1", "chief-of-staff")}
        for boss in set((bosses or {}).values()):
            self.agents[boss] = mk(boss, "orchestrator")
        self.bosses = bosses or {}
        self.engine = types.SimpleNamespace(state_dir=self.dir, now=lambda: self.now, actor="daemon", config={"cloud_alarm_to": lane} if lane else {},
                                            audit=types.SimpleNamespace(append=lambda actor, event, subject, detail: self.entries.append((event, subject, detail))))

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def line(self, at, worker, ok, reason="", **more):
        rec = {"ts": stamp(at), "worker": worker, "runtime_id": "pod/" + worker, "action": "renew-git-token", "ok": ok, "actor": "operator", "reason": reason}
        rec.update(more)
        with open(self.audit_file, "a") as handle:
            handle.write(json.dumps(rec, sort_keys=True) + "\n")

    def send(self, state_dir, to, at, created, text, kind, subject, state, first=False, **more):
        self.messages.append((to, text, kind, subject))
        return {"to": to}

    def tick(self, at=None):
        if at is not None:
            self.now = at
        return self.module.tick(self.engine, types.SimpleNamespace(agents=self.agents), self.send, lambda seen: None, lambda worker: self.bosses.get(worker), "cos-1", env=self.env)

    def to(self, name):
        return [m for m in self.messages if m[0] == name]


def s16(module):
    w = AlarmWorld(module, {"worker-1": "lane-a"})
    try:
        w.line(T0 - 5, "worker-1", False, "kubectl get: You must be logged in to the server (Unauthorized)")
        first = len(w.tick())
        second = len(w.tick(T0 + 30))
        assert (first, second) == (2, 0), (first, second)
        assert all("pod worker-1" in m[1] and "Unauthorized" in m[1] for m in w.messages)
        assert {m[0] for m in w.messages} == {"lane-a", "cos-1"}
        assert [e[0] for e in w.entries] == ["cloud.renewal.alarm"]
        print("S16: messages after tick 1 %d, after tick 2 %d" % (first, second))
    finally:
        w.close()


def s17(module):
    w = AlarmWorld(module, {"worker-1": "lane-a"})
    try:
        for minute in (0, 10, 20):
            w.line(T0 + minute * 60, "worker-1", False, "kubectl get: Unauthorized")
            w.tick(T0 + minute * 60 + 5)
        within = len(w.to("lane-a")), len(w.to("cos-1"))
        assert within == (1, 1), within
        w.line(T0 + 31 * 60, "worker-1", False, "kubectl get: Unauthorized")
        w.tick(T0 + 31 * 60 + 5)
        after = len(w.to("lane-a")), len(w.to("cos-1"))
        assert after == (2, 2), after
        w.line(T0 + 32 * 60, "worker-1", True)   # a success ends the series: the next failure alarms at once
        w.line(T0 + 33 * 60, "worker-1", False, "kubectl get: Unauthorized")
        w.tick(T0 + 33 * 60 + 5)
        assert len(w.to("lane-a")) == 3, len(w.to("lane-a"))
        print("S17: messages per recipient in 3 failures within 30 minutes %d, after 31 minutes %d" % (within[0], after[0]))
    finally:
        w.close()


def s18(module):
    workers = {"worker-%d" % i: "coder-%d" % i for i in range(1, 10)}
    w = AlarmWorld(module, workers)
    try:
        for i, name in enumerate(sorted(workers)):
            w.line(T0 + i, name, False, "kubectl get: You must be logged in to the server (Unauthorized)")
        w.tick(T0 + 60)
        lane = w.to("cos-1")
        bosses = [m for m in w.messages if m[0].startswith("coder-")]
        assert len(lane) == 1 and "9 pods" in lane[0][1], lane
        assert len(bosses) == 9 and len({m[0] for m in bosses}) == 9, len(bosses)
        assert lane[0][1].count("worker-") <= 10
        print("S18: lane messages %d (names the count 9), boss messages %d" % (len(lane), len(bosses)))
    finally:
        w.close()


def s19(module):
    w = AlarmWorld(module, {"worker-1": "lane-a"})
    try:
        w.line(T0, "worker-1", False, "Unauthorized " + PLANT, detail=PLANT, note="token " + PLANT)
        saw = []
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            w.tick(T0 + 5)
        assert len(w.messages) == 2
        blob = json.dumps(w.messages) + out.getvalue() + json.dumps(w.entries) + "".join(Path(w.dir, n).read_text() for n in os.listdir(w.dir) if n.endswith(".json"))
        count = blob.count(PLANT) + blob.count(PLANT[:12])
        assert count == 0, count
        assert "[redacted]" in w.messages[0][1]
        print("S19: occurrences of the planted text in messages, log and audit %d" % count)
    finally:
        w.close()


def s16b(module):
    w = AlarmWorld(module, {"worker-1": "lane-a"})
    try:
        original = module._write_state

        def failing(path, state):
            raise OSError("disk full")
        module._write_state = failing
        w.line(T0 - 60, "worker-1", False, "no network")
        for n in range(5):
            w.tick(T0 + n)
        counts = [len(w.to(r)) for r in ("lane-a", "cos-1")]
        print("S16b: passes 5, alarms per recipient with a failing state write %d" % max(counts))
        assert counts == [1, 1], counts
        # a send that raises for the second recipient: the first is not repeated on the next pass
        w2 = AlarmWorld(module, {"worker-1": "lane-a"})
        module._write_state = original
        try:
            w2.line(T0 - 60, "worker-1", False, "other")
            real = w2.send

            def flaky(state_dir, to, *rest, **more):
                if to == "cos-1":
                    raise RuntimeError("send failed")
                return real(state_dir, to, *rest, **more)
            w2.tick_with = flaky
            try:
                module.tick(w2.engine, types.SimpleNamespace(agents=w2.agents), flaky, lambda seen: None, lambda worker: w2.bosses.get(worker), "cos-1", env=w2.env)
            except RuntimeError:
                pass
            w2.tick(T0 + 5)
            assert len(w2.to("lane-a")) == 1, w2.messages
        finally:
            w2.close()
        # a lane recipient on hold is skipped like the boss
        w3 = AlarmWorld(module, {"worker-1": "lane-a"})
        try:
            w3.line(T0 - 60, "worker-1", False, "held")
            module.tick(w3.engine, types.SimpleNamespace(agents=w3.agents), w3.send, lambda seen: "hold" if seen.name == "cos-1" else None, lambda worker: w3.bosses.get(worker), "cos-1", env=w3.env)
            assert w3.to("cos-1") == [] and len(w3.to("lane-a")) == 1, w3.messages
            print("S16c: lane recipient on hold skipped, messages %d" % len(w3.messages))
        finally:
            w3.close()
    finally:
        w.close()


def s16d(module):
    w = AlarmWorld(module, {"worker-9": "lane-a", "worker-8": "lane-a"})
    w2 = AlarmWorld(module, {"worker-7": "lane-a"})
    try:
        w.line(T0 + 7200, "worker-9", False, "clock jump")
        w.line(T0 - 10, "worker-8", False, "real failure")
        w.tick()
        alarmed = 1 if not any("worker-9" in m[1] for m in w.messages) else 0
        with open(os.path.join(w.dir, module.STATE_FILE)) as handle:
            cursor = json.load(handle)["cursor"]
        assert cursor == T0 - 10 and len(w.to("lane-a")) == 1 and len(w.to("cos-1")) == 1, (cursor, w.messages)
        w2.line(T0 + 240, "worker-7", False, "slightly ahead")
        w2.tick()
        within = len(w2.to("cos-1"))
        assert within == 0, w2.messages
        print("S16d: future-dated lines ignored 1, real failures alarmed %d of 1, a line ahead of the clock handled before its time %d" % (alarmed, within))
    finally:
        w.close()
        w2.close()


def s16e(module):
    w = AlarmWorld(module, {"worker-1": "lane-a"})
    try:
        def failing(path, state):
            raise TypeError("not serialisable")
        module._write_state = failing
        w.line(T0 - 60, "worker-1", False, "no network")
        for n in range(5):
            w.tick(T0 + n)
        counts = [len(w.to(r)) for r in ("lane-a", "cos-1")]
        print("S16e: passes 5, alarms per recipient with a state write raising TypeError %d" % max(counts))
        assert counts == [1, 1], counts
    finally:
        w.close()


def s21(module):
    w = AlarmWorld(module, {"worker-1": "lane-a", "worker-2": "lane-a", "worker-3": "lane-a"})
    try:
        w.line(T0 - 5, "worker-1", False, "before the clock")
        w.line(T0 + 1, "worker-2", False, "one second ahead")
        w.line(T0 + 240, "worker-3", False, "four minutes ahead")

        def cursor():
            with open(os.path.join(w.dir, module.STATE_FILE)) as handle:
                return json.load(handle)["cursor"]

        def told(pod):
            return len([m for m in w.messages if "pod %s " % pod in m[1]])
        w.tick(T0)
        assert cursor() <= T0, "a line ahead of the clock moved the cursor"
        assert (told("worker-1"), told("worker-2"), told("worker-3")) == (2, 0, 0), w.messages
        w.tick(T0 + 2)
        assert cursor() <= T0 + 2, "a line ahead of the clock moved the cursor"
        assert (told("worker-1"), told("worker-2"), told("worker-3")) == (2, 2, 0), w.messages
        print("S21: alarms at tick 1 %d, at tick 2 %d for the 1-second line, for the 4-minute line %d" % (2, 2, told("worker-3")))
    finally:
        w.close()


def s22(module):
    w = AlarmWorld(module, {"worker-1": "lane-a"})
    try:
        original = module._write_state

        def after_send(path, state):
            if w.messages:
                raise OSError(5, "the process ended after the send")
            return original(path, state)
        module._write_state = after_send
        w.line(T0 - 5, "worker-1", False, "no network")
        first = len(w.tick())
        module._WARNED[:] = [1]
        fresh = load()   # a new process: no memory of the first
        fresh._write_state = original
        fresh._WARNED[:] = [1]
        w.module = fresh
        before = len(w.messages)
        w.tick(T0 + 5)
        again = len(w.messages) - before
        assert again == 2, "an alarm was lost: %d messages in the new process" % again
        before = len(w.messages)
        w.tick(T0 + 10)
        assert len(w.messages) == before, w.messages
        print("S22: messages in the first process %d, in the new process %d, at the next tick 0" % (first, again))
    finally:
        w.close()


def s23(module):
    import contextlib
    import io
    w = AlarmWorld(module, {"worker-1": "lane-a"})
    try:
        module._WARNED[:] = []
        w.engine.state_dir = os.path.join(w.dir, "PLANTEDdir-" + PLANT, "state")   # does not exist: the state cannot be written
        w.line(T0 - 5, "worker-1", False, "no network", detail=PLANT)
        err = io.StringIO()
        counts = []
        with contextlib.redirect_stderr(err):
            for n in range(3):
                before = len(w.messages)
                w.tick(T0 + n)
                counts.append(len(w.messages) - before)
        assert counts == [2, 0, 0], counts
        lines = [l for l in err.getvalue().splitlines() if l]
        assert len(lines) == 1 and "OSError 2" in lines[0], lines
        assert "PLANTEDdir" not in err.getvalue() and PLANT not in err.getvalue() and w.dir not in err.getvalue(), err.getvalue()
        fresh = load()
        w.module = fresh
        before = len(w.messages)
        with contextlib.redirect_stderr(io.StringIO()):
            w.tick(T0 + 5)
        assert len(w.messages) - before == 2, w.messages
        print("S23: messages per tick %s, standard error lines %d (%s), a new process %d" % (counts, len(lines), lines[0].split("(")[1].split(")")[0], 2))
    finally:
        w.close()


SCENARIOS = {"s16e": s16e, "s16d": s16d, "s16b": s16b, "s16": s16, "s17": s17, "s18": s18, "s19": s19}
SCENARIOS["s21"] = s21
SCENARIOS["s22"] = s22
SCENARIOS["s23"] = s23


def run(name, path=None):
    """The child's entry: one scenario on the module at `path` (a scratch copy)."""
    SCENARIOS[name](load(path))


MUTANTS = {
    "s22": ("writes the state before the send", "    sent = []\n    try:\n", "    _persist(state_path, state)\n    sent = []\n    try:\n"),
    "s21": ("the slack of 300 seconds again", "FUTURE_SLACK_SECONDS = 0 ", "FUTURE_SLACK_SECONDS = 300 "),
    "s16": ("sends nothing", "item = send(engine.state_dir, to, now, now, text, KIND, subject, reason, first=True)", "item = None"),
    "s16d": ("no future guard", "if l[0] <= now + FUTURE_SLACK_SECONDS]", "if True]"),
    "s16b": ("keeps no record of a failed write", "_MEMORY[state_path] = copy.deepcopy(state)", "pass"),
    "s16e": ("catches only OSError on the state write", "_MEMORY.pop(state_path, None)\n    except Exception:", "_MEMORY.pop(state_path, None)\n    except OSError:"),
    "s17": ("alarms every failure line", "if last is not None and 0 <= at - last < SERIES_SECONDS:", "if False:"),
    "s18": ("one lane message per pod", "if len(by_reason[reason]) == 1:", "if True:"),
    "s19": ("copies the whole audit line", 'scrub(rec.get("reason"))', "json.dumps(rec)"),
}


class TestCloudRenewalAlarm(unittest.TestCase):
    def setUp(self):
        self.module = load()

    def test_s16_one_message_to_the_boss_and_one_to_the_lane_and_none_on_the_second_tick(self):
        s16(self.module)

    def test_s16d_a_future_dated_line_is_ignored_and_never_moves_the_cursor(self):
        s16d(self.module)

    def test_s21_a_line_dated_after_the_clock_is_not_read_and_the_cursor_never_passes_the_clock(self):
        s21(self.module)

    def test_s22_the_state_is_written_after_the_send_and_a_lost_write_repeats_one_round(self):
        s22(self.module)

    def test_s23_an_unwritable_state_directory_keeps_alarming_with_one_line_on_standard_error(self):
        s23(self.module)

    def test_s16b_a_failing_state_write_and_a_failing_send_never_repeat_an_alarm_and_a_held_lane_is_skipped(self):
        s16b(self.module)

    def test_s16e_a_state_write_raising_any_exception_never_repeats_an_alarm(self):
        s16e(self.module)

    def test_s17_the_same_pod_within_thirty_minutes_sends_nothing_more_and_a_success_ends_the_series(self):
        s17(self.module)

    def test_s18_nine_pods_with_one_reason_are_one_lane_message_naming_the_count_and_nine_boss_messages(self):
        s18(self.module)

    def test_s19_a_planted_token_shaped_text_is_in_no_message_and_no_audit_entry(self):
        s19(self.module)

    def test_the_scrub_flattens_cuts_and_redacts(self):
        m = self.module
        self.assertEqual(m.scrub("a\nb\x00c \u00e9"), "a b?c ?")
        self.assertEqual(m.scrub("x " + PLANT + " y"), "x [redacted] y")
        self.assertEqual(m.scrub("github_pat_11AAAA_bbbb"), "[redacted]")
        self.assertEqual(m.scrub("A" * 36), "[redacted]")
        self.assertLessEqual(len(m.scrub("word " * 100)), 160)

    def test_a_missing_audit_file_sends_nothing_and_raises_nothing(self):
        w = AlarmWorld(self.module)
        try:
            self.assertEqual(w.tick(), [])
            self.assertFalse(os.path.exists(os.path.join(w.dir, self.module.STATE_FILE)))
        finally:
            w.close()

    def test_a_bad_line_and_an_old_failure_are_skipped(self):
        w = AlarmWorld(self.module)
        try:
            with open(w.audit_file, "w") as handle:
                handle.write("not json\n[1]\n")
            w.line(T0 - 7200, "worker-1", False, "old")
            self.assertEqual(w.tick(), [])
        finally:
            w.close()

    def test_the_audit_path_follows_the_launchers_rule(self):
        m = self.module
        self.assertEqual(m.audit_path({"CW_CLOUD_AUDIT": "/x/a.jsonl"}), "/x/a.jsonl")
        self.assertEqual(m.audit_path({"XDG_STATE_HOME": "/s"}), "/s/cw-cloud/audit.jsonl")

    def test_the_lane_key_defaults_to_the_chief_of_staff_and_is_validated(self):
        m = self.module
        self.assertEqual((m.CONFIG_KEY, m.CONFIG_DEFAULT), ("cloud_alarm_to", []))
        m.validate(["lane-a", "coder-1"], "x")
        m.validate([], "x")
        for bad in ("lane-a", [1], ["a b"], ["a"] * 21, None):
            with self.assertRaises(ValueError):
                m.validate(bad, "x")
        w = AlarmWorld(self.module, {"worker-1": "lane-a"}, lane=["coder-1", "lane-a"])
        try:
            w.agents["coder-1"] = types.SimpleNamespace(name="coder-1", session_id="c", status="idle", fleet={"role": "coder"})
            w.line(T0, "worker-1", False, "r")
            w.tick(T0 + 5)
            self.assertEqual(sorted(m_[0] for m_ in w.messages), ["coder-1", "lane-a"])   # the boss is told once; the lane's other name once; no chief of staff
        finally:
            w.close()


class TestCloudRenewalAlarmWired(Base):
    """The entry's tick over the real send and the real hold rule."""

    def test_the_wired_tick_reads_the_launchers_file_and_sends_a_reminder_to_the_boss(self):
        if True:
            state = self.state_dir
            state.mkdir(parents=True, exist_ok=True, mode=0o700)
            (state / "cloud").mkdir(exist_ok=True)
            (state / "cloud" / "worker-1.json").write_text(json.dumps({"worker": "worker-1", "reports_to": "lane-a"}))
            audit = state / "audit.jsonl"
            audit.write_text(json.dumps({"ts": stamp(T0), "worker": "worker-1", "runtime_id": "pod/worker-1", "action": "renew-git-token", "ok": False, "reason": "boom"}) + "\n")
            mk = lambda n, role: types.SimpleNamespace(name=n, session_id=n, status="idle", fleet={"role": role}, conflict=[], problem=None, engaged=lambda *a: False)
            engine = types.SimpleNamespace(state_dir=str(state), now=lambda: T0 + 5, audit=None, actor="daemon", config={})
            old = os.environ.get("CW_CLOUD_AUDIT")
            os.environ["CW_CLOUD_AUDIT"] = str(audit)
            try:
                sent = sb.cloud_renewal_alarm_tick(engine, types.SimpleNamespace(agents={"lane-a": mk("lane-a", "orchestrator"), "cos-1": mk("cos-1", "chief-of-staff")}))
            finally:
                if old is None:
                    os.environ.pop("CW_CLOUD_AUDIT", None)
                else:
                    os.environ["CW_CLOUD_AUDIT"] = old
            self.assertEqual(sorted(to for to, _ in sent), ["cos-1", "lane-a"])
            self.assertEqual({r["to"] for r in sb.read_reminders(str(state))}, {"cos-1", "lane-a"})


MUTANT_TEXT = {   # the failure text of the check each mutant breaks, anchored on what its scenario prints
    "s16": "AssertionError: (0, 0)",
    "s16b": "AssertionError: [5, 5]",
    "s16d": "AssertionError: (1793368800.0",
    "s16e": "TypeError: not serialisable",
    "s17": "AssertionError: (3, 3)",
    "s18": 'assert len(lane) == 1 and "9 pods" in lane[0][1]',
    "s19": "AssertionError: 24",
    "s21": "a line ahead of the clock moved the cursor",
    "s22": "an alarm was lost: 0 messages in the new process",
}


class TestCloudRenewalAlarmMutants(unittest.TestCase):
    """Each mutant is a scratch copy OUTSIDE the repository with one applied change, run as a child process: its scenario exits 1; the real module exits 0."""

    def child(self, scenario, module_path):
        sys.path.insert(0, str(TESTS_DIR.parent.parent / "cloud" / "worker" / "tests"))
        import stub_support   # the one starter of a child process (its cleanup is registered before anything waits)
        code = "import sys; sys.path.insert(0, %r); import test_cloud_renewal_alarm as t; t.run(%r, %r)" % (str(TESTS_DIR), scenario, str(module_path))
        proc = stub_support.start(self, [sys.executable, "-c", code], stdout=-1, stderr=-1)
        proc.communicate(timeout=60)
        return proc.returncode

    def child_text(self, scenario, module_path):
        code = "import sys; sys.path.insert(0, %r); import test_cloud_renewal_alarm as t; t.run(%r, %r)" % (str(TESTS_DIR), scenario, str(module_path))
        import subprocess
        proc = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=60)
        return proc.returncode, proc.stdout + proc.stderr

    def test_the_real_module_passes_and_each_of_the_four_mutants_fails_its_scenario(self):
        scratch = Path(tempfile.mkdtemp(prefix="alarm-mutant-"))
        self.addCleanup(shutil.rmtree, str(scratch), True)
        source = (BIN / "cloud_renewal_alarm.py").read_text()
        for scenario, (label, old, new) in sorted(MUTANTS.items()):
            self.assertEqual(self.child(scenario, BIN / "cloud_renewal_alarm.py"), 0, scenario)
            self.assertIn(old, source, "the mutation of %s is not applied: the text is gone" % label)
            copy = scratch / ("mutant_%s.py" % scenario)
            copy.write_text(source.replace(old, new, 1))
            self.assertNotEqual(copy.read_text(), source)
            self.assertEqual(self.child(scenario, copy), 1, "the mutant '%s' did not fail %s" % (label, scenario))
            self.assertEqual(self.child_text(scenario, BIN / "cloud_renewal_alarm.py")[0], 0, scenario)
            code_, text_ = self.child_text(scenario, copy)
            self.assertEqual(code_, 1, scenario)
            self.assertIn(MUTANT_TEXT[scenario], text_, "the mutant '%s' did not fail with the text of its own check: %s" % (label, text_[-300:]))


# ---- #1579, PC-6 continuation: scenarios S24, S25 and S26 (inserted after the earlier tests; nothing above changes) ----
def s24(module):
    msg = "a cursor ahead of the clock hid a line"
    w = AlarmWorld(module, {"worker-0": "lane-a", "worker-1": "lane-a", "worker-2": "lane-a"})
    try:
        with open(os.path.join(w.dir, module.STATE_FILE), "w") as handle:   # the clock stepped back: the stored cursor is 10 minutes ahead
            json.dump({"cursor": T0 + 600, "edge": [], "alarmed": [["worker-0", T0]], "pods": {}}, handle)
        w.line(T0, "worker-0", False, "alarmed before the step back")
        w.line(T0 + 300, "worker-1", False, "five minutes ahead")
        w.line(T0 + 720, "worker-2", False, "twelve minutes ahead")

        def cursor():
            with open(os.path.join(w.dir, module.STATE_FILE)) as handle:
                return json.load(handle)["cursor"]

        def told(pod):
            return len([m for m in w.messages if "pod %s " % pod in m[1]])
        after = 0
        counts = []
        for step in (0, 360, 780):
            w.tick(T0 + step)
            if cursor() > T0 + step:
                after += 1
            counts.append((told("worker-0"), told("worker-1"), told("worker-2")))
        assert after == 0, msg
        assert counts == [(0, 0, 0), (0, 2, 0), (0, 2, 2)], "%s: %s" % (msg, counts)
        old = []
        for n, content in enumerate((None, "{corrupt")):
            v = AlarmWorld(module, {"old-%d" % n: "lane-a", "new-%d" % n: "lane-a"})
            try:
                if content is not None:
                    with open(os.path.join(v.dir, module.STATE_FILE), "w") as handle:
                        handle.write(content)
                v.line(T0 - 7200, "old-%d" % n, False, "two hours old")
                v.line(T0 - 600, "new-%d" % n, False, "ten minutes old")
                v.tick(T0)
                old.append(len([m for m in v.messages if "pod old-" in m[1]]))
                assert len([m for m in v.messages if "pod new-" in m[1]]) == 2, v.messages
            finally:
                v.close()
        assert old == [0, 0], old
        print("S24: the cursor after the clock in %d of 3 ticks, alarms per tick %s, repeats of the earlier line %d, old failures alarmed after a missing or corrupt state %s" % (after, counts, counts[2][0], old))
    finally:
        w.close()


def s25(module):
    msg = "a raising send left no audit entry"
    w = AlarmWorld(module, {"worker-1": "lane-a"})
    try:
        def boom(*args, **more):
            raise RuntimeError("the send raised")
        real = w.send
        w.send = boom
        w.line(T0 - 5, "worker-1", False, "no network")
        raised = 0
        try:
            w.tick()
        except Exception:
            raised = 1
        assert len(w.entries) == 1, "%s: %d audit entries" % (msg, len(w.entries))
        assert raised == 0, "a raising send was raised into the pass"
        assert os.path.exists(os.path.join(w.dir, module.STATE_FILE)), "the state was not written"
        w.send = real
        w.tick(T0 + 5)
        assert w.messages == [], w.messages
        print("S25: audit entries %d, state written 1, exceptions raised %d, further sends %d" % (len(w.entries), raised, len(w.messages)))
    finally:
        w.close()


def s26(text):
    start = text.index("**The alarm on a failed token renewal (#1579).**")
    section = text[start:text.index("\n", start)]
    assert "written by a temporary file and a rename" in section, "the document does not say the state is written by a temporary file and a rename"
    assert "AT LEAST ONCE" in section and "a crash between the send and the write repeats one round" in section, "the document does not say the alarm is at least once"
    assert "keeps its state in memory" in section and "one line on standard error" in section, "the document does not say an unwritable state is kept in memory"
    print("S26: sentences found 3 of 3")


S24_S25_MUTANTS = {
    "s24": ("does not clamp the cursor", "clamped = cursor is not None and cursor > now", "clamped = False"),
}


class TestCloudRenewalAlarmContinuation(unittest.TestCase):
    """The control (the unmutated module) runs FIRST, then the copy with the mutation asserted applied, each in a child process."""

    DOC = TESTS_DIR.parent.parent / "docs" / "switchboard.md"

    def child(self, scenario, path):
        code = "import sys; sys.path.insert(0, %r); import test_cloud_renewal_alarm as t; t.%s(t.load(%r))" % (str(TESTS_DIR), scenario, str(path))
        import subprocess
        proc = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=90)
        return proc.returncode, proc.stdout + proc.stderr

    def mutant(self, scenario, old, new, text):
        scratch = Path(tempfile.mkdtemp(prefix="alarm-mutant-"))
        self.addCleanup(shutil.rmtree, str(scratch), True)
        source = (BIN / "cloud_renewal_alarm.py").read_text()
        self.assertEqual(self.child(scenario, BIN / "cloud_renewal_alarm.py")[0], 0, "the control failed")
        self.assertEqual(source.count(old), 1, "the mutation is not applied: the text is not found once")
        copy = scratch / "mutant.py"
        copy.write_text(source.replace(old, new, 1))
        code, out = self.child(scenario, copy)
        self.assertEqual(code, 1, out[-300:])
        self.assertIn(text, out)

    def test_s24_the_cursor_is_clamped_to_the_clock_and_a_line_is_alarmed_once(self):
        s24(load())
        self.mutant("s24", *S24_S25_MUTANTS["s24"][1:], "a cursor ahead of the clock hid a line")

    def test_s25_a_raising_send_leaves_its_audit_entry_and_its_state(self):
        s25(load())
        source = (BIN / "cloud_renewal_alarm.py").read_text()
        head = source.index("    except Exception:\n        pass   # nothing raises into the daemon pass")
        tail = source.index("    return sent\n", head)
        old = source[head:tail]
        new = ("    finally:\n        _persist(state_path, state)\n    if engine.audit is not None:\n"
               "        for pod, (worker, reason) in sorted(alarms.items()):\n"
               "            engine.audit.append(engine.actor, EVENT, pod, {\"told\": sorted(set(told.get(pod, []))), \"reason\": reason, \"group\": len(by_reason.get(reason, [pod]))})\n")
        self.mutant("s25", old, new, "a raising send left no audit entry")

    def test_s26_the_document_says_what_the_alarm_writes_and_that_it_is_at_least_once(self):
        text = self.DOC.read_text()
        s26(text)
        sentence_start = text.index("The alarm is AT LEAST ONCE:")
        sentence_end = text.index("never loses an alarm.", sentence_start) + len("never loses an alarm.")
        copy = text[:sentence_start] + text[sentence_end:]
        self.assertNotEqual(copy, text)
        with self.assertRaises(AssertionError) as raised:
            s26(copy)
        self.assertIn("the document does not say the alarm is at least once", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
