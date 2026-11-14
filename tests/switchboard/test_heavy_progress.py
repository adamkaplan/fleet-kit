"""#1762: the heavy wrapper prints one progress line a minute while its command runs. A fake clock, a fake slot lock and a fake child: nothing waits for a real minute."""
import faulthandler
import importlib.util
import os
import signal
import subprocess
import sys
import re
import tempfile
import threading
import time
import unicodedata
import unittest
from unittest import mock



class _LazySb:
    """support.loader loads the whole switchboard (about 4.5 s); the scratch-copy child processes never need it, so it is imported on first use."""
    def __getattr__(self, name):
        from support.loader import sb as real
        return getattr(real, name)


_sb = _LazySb()


START_WAIT = float(os.environ.get("FLEET_TEST_START_WAIT", "5"))   # the inner run of S10's mutant shortens it: only the mutant waits it out


def _made_up_pid():
    if os.environ.get("FLEET_TEST_INNER"):   # a scratch child: a number nobody holds, without loading the switchboard just for the helper
        pid = 4000000 + os.getpid() % 100000
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return pid
            except OSError:
                pass
            pid += 1
    from support.herdr_world import made_up_pid as real
    return real()


def load_heavy():
    """bin/heavy_slots.py as loaded by the suite; FLEET_TEST_HEAVY_SLOTS names a scratch copy to load instead (S10's mutant)."""
    path = os.environ.get("FLEET_TEST_HEAVY_SLOTS")
    if not path:
        return _sb.load_heavy_slots()
    spec = importlib.util.spec_from_file_location("heavy_slots_scratch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Out:
    """The wrapper's error stream: every line with the fake second it was written at."""

    def __init__(self, now):
        self.now, self.lines, self.text = now, [], ""

    def write(self, text):
        self.text += text
        for line in text.splitlines():
            self.lines.append((self.now[0], line))

    def flush(self):
        pass


LAST = [None]


class BlockingOut(Out):
    """An error stream whose write of a progress line blocks on an Event the test releases; the blocked line is dropped when released (the stream is gone)."""

    def __init__(self, now, entered, release, done):
        Out.__init__(self, now)
        self.entered, self.release, self.done, self.returned = entered, release, done, threading.Event()

    def write(self, text):
        if "still running" not in text:
            return Out.write(self, text)
        self.done.set()   # the fake child ends while the write is blocked
        self.entered.set()
        self.release.wait()   # event only: no timer; the test releases it after run returned
        self.returned.set()


class Rig:
    """run() with a fake slot (or a busy one), a fake child alive `alive` fake seconds and a fake pause for the progress thread."""

    def __init__(self, case, alive=0, status=0, busy=False, wait_max=1800, command=None, env=None, table_raises=False, slow_exit=False, slow_read=False, block_write=False):
        self.hs = load_heavy()
        self.zero = False
        LAST[0] = self   # the kill handler of S14 reads the events of the rig that is running
        self.started = threading.Event()
        self.now = [1000.0]
        self.out = Out(self.now)
        self.done = threading.Event()
        self.pauses, self.signals, self.starts, self.began = [], [], [], []
        rig = self

        class FakeChild:
            pid = _made_up_pid()   # no such process: the CPU read fails and the line has no CPU part

            def __init__(child, argv, **kwargs):
                rig.starts.append(argv)
                rig.began.append(rig.now[0])

            def wait(child):
                if alive and os.environ.get("FLEET_TEST_SELFCLOCK") and not rig.started.wait(2):   # S12's copy has no progress thread: the fake child runs on its own clock
                    rig.now[0] = rig.began[0] + alive
                    return status
                if alive and os.environ.get("FLEET_TEST_INNER") and not rig.started.wait(START_WAIT):   # a guard against a hang, not a measure
                    raise AssertionError("the progress thread did not start within %g s" % START_WAIT)
                if alive and not rig.done.wait(30):
                    raise AssertionError("the fake child was never ended")
                return status

            def send_signal(child, number):
                rig.signals.append(number)

        def pause(stop, seconds):
            rig.pauses.append(seconds)
            if seconds < 0.01:   # a tight loop of lines: end the fake child so the run returns and the test can name it
                rig.zero = True
                rig.done.set()
            rig.now[0] += seconds
            if rig.now[0] - rig.began[0] > alive:
                rig.done.set()
                stop.wait(30)   # the child has ended: the wrapper stops this thread
                if slow_exit:
                    time.sleep(0.3)   # the thread is slow to end: only a join makes the wrapper wait for it

        def take(state, slots):
            if busy:
                return None, None
            return os.open(os.devnull, os.O_RDWR), 0

        def sleep(seconds):
            rig.now[0] += 10

        environ = {"PATH": os.environ["PATH"], "FLEET_HEAVY_NAME": "lane-x", "FLEET_HEAVY_REPORT_SECONDS": "30"}
        environ.update(env or {})
        patches = [mock.patch.object(self.hs, "_take", take), mock.patch.object(self.hs, "_take_express", lambda state: None), mock.patch.object(self.hs, "holders", lambda s, n=3: []),
                   mock.patch.object(self.hs.subprocess, "Popen", FakeChild), mock.patch.object(self.hs, "_own_start", lambda: ""), mock.patch.object(self.hs, "_start_of", lambda pid: ""),
                   mock.patch.object(self.hs, "_progress_pause", pause), mock.patch.object(self.hs, "_progress_started", self.started, create=True),
                   mock.patch.object(self.hs, "_resolve", lambda command, environ: "/bin/true")]
        self.entered, self.release = threading.Event(), threading.Event()
        if block_write:
            self.out = BlockingOut(self.now, self.entered, self.release, self.done)
        if slow_read:
            def slow(pid):
                self.entered.set()
                self.done.set()   # the fake child ends while the read is blocked
                self.release.wait(30)   # blocks until the test releases it after the return
                return None
            patches.append(mock.patch.object(self.hs, "_child_cpu_seconds", slow))
        if table_raises:
            def broken(pid):
                self.done.set()   # the thread ends on this read: the fake child ends with it
                raise OSError("the process table cannot be read")
            patches.append(mock.patch.object(self.hs, "_child_cpu_seconds", broken))
        dog = {"read_table": lambda: [], "pause": lambda seconds: self.done.is_set(), "killpg": lambda *a: self.signals.append(a), "kill": lambda *a: self.signals.append(a),
               "every": 0.0, "lsof": lambda path: set(), "grace": 0.001, "sweep_pause": lambda s: None}
        for patcher in patches:
            patcher.start()
            case.addCleanup(patcher.stop)
        try:
            self.status = self.hs.run(command or ["python3", "/nowhere/tool"], "/state", 3, wait_max, "holder-x", environ=environ, out=self.out, clock=lambda: self.now[0], sleep=sleep,
                                      watchdog=dog, short=False, ceiling="100000", worktree=lambda cwd: "/work/acme-tree")
        finally:
            for patcher in patches:
                patcher.stop()

    def progress(self):
        return [line for _t, line in self.out.lines if line.startswith("heavy: ") and "still running" in line]

    def longest_gap(self, end):
        """The longest silence of the wrapper from the command's start to its end, in fake seconds."""
        times = [self.began[0]] + [t for t, _l in self.out.lines if t >= self.began[0]] + [end]
        return max(b - a for a, b in zip(times, times[1:]))


class TestHeavyProgress(unittest.TestCase):

    def test_s2_a_child_alive_200_seconds_gets_one_line_at_each_full_minute(self):
        rig = Rig(self, alive=200)
        lines = rig.progress()
        self.assertEqual((rig.status, len(lines)), (0, 3), lines)
        for line, seconds in zip(lines, (60, 120, 180)):
            self.assertTrue(line.startswith("heavy: python3 still running: %d s since the command started" % seconds), line)

    def test_s3_a_wait_stops_at_the_bound_with_the_refusal_line_and_75(self):
        rig = Rig(self, busy=True, wait_max=120)
        text = [line for _t, line in rig.out.lines]
        self.assertEqual(rig.status, 75)
        self.assertEqual(len([l for l in text if l.startswith("heavy: no slot after 120 s")]), 1, text)
        self.assertEqual(len(rig.starts), 0)
        self.assertEqual(len([l for l in text if l.startswith("heavy: waiting for a slot")]), 3, text)
        self.assertEqual(rig.progress(), [])

    def _s4(self):
        rig = Rig(self, alive=900, status=7)
        gap = rig.longest_gap(rig.began[0] + 900)
        self.assertEqual(rig.status, 7)
        self.assertLessEqual(gap, 60, "the wrapper was silent for %d s" % gap)
        return rig, gap

    def test_s4_a_child_silent_for_900_seconds_never_leaves_the_wrapper_silent_for_more_than_60(self):
        rig, gap = self._s4()
        self.assertEqual((len(rig.progress()), gap), (15, 60))

    def _scratch(self, replacements, inner, extra_env=None):
        """Run the inner test against scratch copies of bin/heavy_slots.py outside the repository: the control (unchanged) and the mutant (replacements applied)."""
        source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin", "heavy_slots.py")
        text = open(source).read()
        mutant = text
        for old, new in replacements:
            self.assertEqual(text.count(old), 1, old)
            mutant = mutant.replace(old, new)
        here = os.path.dirname(os.path.abspath(__file__))
        results = {}
        with tempfile.TemporaryDirectory() as scratch:
            for kind, body in (("control", text), ("mutant", mutant)):
                path = os.path.join(scratch, kind + "_heavy_slots.py")
                open(os.path.join(scratch, "heavy_slots_watch.py"), "w").write(open(os.path.join(here, "..", "..", "bin", "heavy_slots_watch.py")).read())
                self.assertFalse(os.path.abspath(path).startswith(os.path.abspath(os.path.join(here, "..", ".."))))
                with open(path, "w") as handle:
                    handle.write(body)
                env = dict(os.environ, FLEET_TEST_HEAVY_SLOTS=path, FLEET_TEST_INNER="1", **(extra_env or {}))
                results[kind] = subprocess.run([sys.executable, __file__, "TestHeavyProgress." + inner], cwd=here, env=env,
                                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=300)
        return results

    def test_s12_inner_s4_on_a_copy_without_the_progress_thread(self):
        if not os.environ.get("FLEET_TEST_INNER"):
            self.skipTest("run by test_s12 in a child process")
        self._s4()

    def test_s12_a_copy_without_the_progress_thread_fails_with_the_anchored_text_and_the_control_passes_first(self):
        results = self._scratch([("progress_thread.start()", "pass")], "test_s12_inner_s4_on_a_copy_without_the_progress_thread", {"FLEET_TEST_SELFCLOCK": "1"})
        self.assertEqual(results["control"].returncode, 0, results["control"].stdout)
        self.assertEqual(results["mutant"].returncode, 1, results["mutant"].stdout)
        self.assertIn("the wrapper was silent for 900 s", results["mutant"].stdout)

    def test_s11_inner_a_slow_read_writes_nothing_after_the_return(self):
        if not os.environ.get("FLEET_TEST_INNER"):
            self.skipTest("run by test_s11 in a child process")
        rig = Rig(self, alive=100, slow_read=True)
        self.assertTrue(rig.entered.is_set())
        lines = len(rig.out.lines)
        rig.release.set()   # the read ends after the wrapper returned
        time.sleep(0.15)
        self.assertEqual(len(rig.out.lines) - lines, 0, "a line was written after the wrapper returned")

    def test_s11_a_read_that_outlasts_the_join_writes_no_line_after_the_return(self):
        results = self._scratch([("if stop.is_set():   # stopped after the read, before the write", "if False:")], "test_s11_inner_a_slow_read_writes_nothing_after_the_return")
        self.assertEqual(results["control"].returncode, 0, results["control"].stdout)
        self.assertEqual(results["mutant"].returncode, 1, results["mutant"].stdout)
        self.assertIn("a line was written after the wrapper returned", results["mutant"].stdout)

    def test_s9_no_line_and_no_thread_is_left_after_the_wrapper_returned(self):
        before = threading.active_count()
        rig = Rig(self, alive=130, slow_exit=True)
        after = threading.active_count()
        self.assertEqual(len(rig.progress()), 2)
        lines = len(rig.out.lines)
        rig.now[0] += 180
        time.sleep(0.15)
        self.assertEqual(len(rig.out.lines) - lines, 0, "a line was written after the wrapper returned")
        self.assertEqual(after - before, 0, "the progress thread is still alive after the wrapper returned")

    def test_s10_inner_s4_with_the_thread_start_awaited(self):
        if not os.environ.get("FLEET_TEST_INNER"):
            self.skipTest("run by test_s10 in a child process")
        self._s4()

    def test_s10_a_thread_that_never_starts_fails_by_a_named_text_and_the_control_passes_first(self):
        source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin", "heavy_slots.py")
        text = open(source).read()
        start = "progress_thread.start()"
        self.assertEqual(text.count(start), 1)
        here = os.path.dirname(os.path.abspath(__file__))
        with tempfile.TemporaryDirectory() as scratch:
            copies = {"control": text, "mutant": text.replace(start, "pass")}
            results = {}
            for kind in ("control", "mutant"):
                path = os.path.join(scratch, kind + "_heavy_slots.py")
                open(os.path.join(scratch, "heavy_slots_watch.py"), "w").write(open(os.path.join(here, "..", "..", "bin", "heavy_slots_watch.py")).read())
                with open(path, "w") as handle:
                    handle.write(copies[kind])
                env = dict(os.environ, FLEET_TEST_HEAVY_SLOTS=path, FLEET_TEST_INNER="1", FLEET_TEST_START_WAIT="1.5")
                results[kind] = subprocess.run([sys.executable, __file__, "TestHeavyProgress.test_s10_inner_s4_with_the_thread_start_awaited"], cwd=here, env=env,
                                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=300)
        self.assertEqual(results["control"].returncode, 0, results["control"].stdout)
        self.assertEqual(results["mutant"].returncode, 1, results["mutant"].stdout)
        self.assertIn("the progress thread did not start within 1.5 s", results["mutant"].stdout)

    def test_u1_a_run_that_ends_before_the_interval_prints_nothing_of_its_own(self):
        rig = Rig(self, alive=0, status=5)
        self.assertEqual((rig.status, rig.progress(), rig.out.text), (5, [], ""))
        rig = Rig(self, alive=10, status=5)
        self.assertEqual((rig.status, rig.progress(), rig.out.text), (5, [], ""))

    def test_u2_a_failure_of_the_progress_read_is_quiet_and_never_changes_the_status(self):
        for raises in (False, True):
            rig = Rig(self, alive=130, status=3, table_raises=raises)
            self.assertEqual((rig.status, rig.signals, rig.out.text.count("Traceback")), (3, [], 0))
            self.assertEqual(len(rig.progress()), 0 if raises else 2)   # unreadable: the line without the CPU part (or, when the read itself raises, the thread ends quietly)
            for line in rig.progress():
                self.assertNotIn("CPU", line)

    def test_u3_the_line_names_the_program_by_its_basename_only(self):
        rig = Rig(self, alive=130, command=["python3", "/secret/dir/tool", "--token", "ghp_aaaaaaaaaaaaaaaaaaaa"])
        self.assertEqual(len(rig.progress()), 2)
        for line in rig.progress():
            self.assertTrue(line.startswith("heavy: python3 still running"), line)
        for word in ("/secret/dir", "--token", "ghp_"):
            self.assertEqual(rig.out.text.count(word), 0, word)

    def test_u4_the_niceness_is_as_at_the_base(self):
        hs = _sb.load_heavy_slots()
        self.assertEqual(hs.command_prefix(lambda name: "/usr/bin/" + name if name == "taskpolicy" else None, environ={}), ["/usr/bin/taskpolicy", "-b"])
        self.assertEqual(hs.command_prefix(lambda name: "/usr/bin/nice" if name == "nice" else None, environ={}), ["/usr/bin/nice", "-n", "10"])
        self.assertEqual(hs.command_prefix(lambda name: None, environ={}), [])

    def test_u6_the_interval_is_60_and_an_environment_value_shortens_it(self):
        hs = _sb.load_heavy_slots()
        self.assertEqual((hs.PROGRESS_EVERY, hs.WAIT_REPORT_SECONDS, hs.DEFAULT_WAIT_MAX), (60, 30, 1800))
        rig = Rig(self, alive=200)
        self.assertEqual(set(rig.pauses), {60})
        rig = Rig(self, alive=1, env={"FLEET_HEAVY_PROGRESS_SECONDS": "0.3"})
        self.assertEqual((set(rig.pauses), len(rig.progress())), ({0.3}, 3))

    def test_s13_inner_the_interval_variable_falls_back_to_60(self):
        if not os.environ.get("FLEET_TEST_INNER"):
            self.skipTest("run by test_s13 in a child process")
        for value in ("0", "-5", "abc", "", "nan", "inf"):
            rig = Rig(self, alive=1, env={"FLEET_HEAVY_PROGRESS_SECONDS": value})
            read = set(rig.pauses)
            self.assertFalse(rig.zero, "a zero interval was accepted (value %r)" % value)
            self.assertNotIn(0.0, read, "a zero interval was accepted (value %r)" % value)
            self.assertEqual(read, {60}, "value %r read as %r" % (value, read))
        rig = Rig(self, alive=1, env={"FLEET_HEAVY_PROGRESS_SECONDS": "0.05"})
        self.assertEqual(set(rig.pauses), {0.05})

    def test_s13_a_zero_or_bad_interval_falls_back_to_60_and_the_old_reading_fails(self):
        old = 'progress_every = float(environ.get("FLEET_HEAVY_PROGRESS_SECONDS") or PROGRESS_EVERY)'
        results = self._scratch([("progress_every = _progress_interval(environ, out)", old)], "test_s13_inner_the_interval_variable_falls_back_to_60")
        self.assertEqual(results["control"].returncode, 0, results["control"].stdout)
        self.assertEqual(results["mutant"].returncode, 1, results["mutant"].stdout)
        self.assertIn("a zero interval was accepted", results["mutant"].stdout)

    def test_s15_inner_a_bad_interval_setting_falls_back_to_60_with_one_line(self):
        if not os.environ.get("FLEET_TEST_INNER"):
            self.skipTest("run by test_s15 in a child process")
        for value in ("1e308", "86401", "abc", "0", "-5", "nan", "inf"):
            rig = Rig(self, alive=1, env={"FLEET_HEAVY_PROGRESS_SECONDS": value})
            read = set(rig.pauses)
            self.assertEqual(read, {60}, "an out-of-range interval was accepted (value %r read as %r)" % (value, read))
            said = [line for _t, line in rig.out.lines if "out of range" in line]
            self.assertEqual(said, ["heavy: FLEET_HEAVY_PROGRESS_SECONDS=%s is out of range, using 60" % value], "a bad setting was silent (value %r: %r)" % (value, said))
        for value, expected in ((None, 60), ("", 60), ("86400", 86400), ("0.05", 0.05)):
            env = {} if value is None else {"FLEET_HEAVY_PROGRESS_SECONDS": value}
            rig = Rig(self, alive=1, env=env)
            self.assertEqual(set(rig.pauses), {expected}, "value %r" % (value,))
            self.assertEqual([l for _t, l in rig.out.lines if "out of range" in l], [], "a valid or empty setting printed a line (value %r)" % (value,))

    def test_s15_a_bad_setting_falls_back_with_one_line_and_each_mutant_fails_with_its_text(self):
        for old, new, text in (("<= 86400:", "<= float(\"inf\"):", "an out-of-range interval was accepted"),
                               ('out.write("heavy: FLEET_HEAVY_PROGRESS_SECONDS=%s is out of range, using 60\\n" % raw[:40])', "pass", "a bad setting was silent")):
            results = self._scratch([(old, new)], "test_s15_inner_a_bad_interval_setting_falls_back_to_60_with_one_line")
            self.assertEqual(results["control"].returncode, 0, results["control"].stdout)
            self.assertEqual(results["mutant"].returncode, 1, results["mutant"].stdout)
            self.assertIn(text, results["mutant"].stdout)

    def test_s14_inner_a_blocked_write_does_not_hold_the_return(self):
        if not os.environ.get("FLEET_TEST_INNER"):
            self.skipTest("run by test_s14 in a child process")
        def killed(number, frame):   # the job's kill: name the hang and print every thread, the lock and the event
            sys.stderr.write("the stop waited for a blocked write\n")
            for thread in threading.enumerate():
                sys.stderr.write("thread %s alive=%s\n" % (thread.name, thread.is_alive()))
            faulthandler.dump_traceback(all_threads=True)
            rig = LAST[0]
            if rig is not None:
                sys.stderr.write("write entered: %s, released: %s, returned: %s; the lock is shown by the traces above\n" % (rig.entered.is_set(), rig.release.is_set(), getattr(rig.out, "returned", threading.Event()).is_set()))
            sys.stderr.flush()
            os._exit(1)
        signal.signal(signal.SIGUSR1, killed)   # run() takes over INT, TERM and HUP: the job's kill notice here is USR1
        before = set(threading.enumerate())
        rig = Rig(self, alive=100, block_write=True, status=4)
        self.assertEqual(rig.status, 4)   # run returned while the write is still blocked
        self.assertTrue(rig.entered.is_set() and not rig.out.returned.is_set())
        lines = len(rig.out.lines)
        rig.release.set()   # only after run returned
        self.assertTrue(rig.out.returned.wait(5))
        for thread in set(threading.enumerate()) - before:
            thread.join(5)
        self.assertEqual(len(rig.out.lines) - lines, 0, "a line was written after the wrapper returned")

    def test_s14_a_stop_that_waits_on_a_blocked_write_is_killed_by_the_outer_bound_and_named(self):
        text = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin", "heavy_slots.py")).read()
        anchor = "got_lock = progress_lock.acquire(timeout=2)"
        self.assertEqual(text.count(anchor), 1, anchor)
        here = os.path.dirname(os.path.abspath(__file__))
        outcome = {}
        with tempfile.TemporaryDirectory() as scratch:
            for kind, body in (("control", text), ("mutant", text.replace(anchor, "got_lock = progress_lock.acquire()"))):
                path = os.path.join(scratch, kind + "_heavy_slots.py")
                open(os.path.join(scratch, "heavy_slots_watch.py"), "w").write(open(os.path.join(here, "..", "..", "bin", "heavy_slots_watch.py")).read())
                with open(path, "w") as handle:
                    handle.write(body)
                env = dict(os.environ, FLEET_TEST_HEAVY_SLOTS=path, FLEET_TEST_INNER="1")
                child = subprocess.Popen([sys.executable, __file__, "TestHeavyProgress.test_s14_inner_a_blocked_write_does_not_hold_the_return"], cwd=here, env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
                try:
                    output, _ = child.communicate(timeout=float(os.environ.get("FLEET_TEST_S14_BOUND", "6")))   # the outer bound: only the mutant reaches it
                    outcome[kind] = (child.returncode, output, False)
                except subprocess.TimeoutExpired:
                    child.send_signal(signal.SIGUSR1)   # the job's kill notice: the child names the hang and ends
                    output, _ = child.communicate(timeout=30)
                    outcome[kind] = (child.returncode, output, True)
        self.assertEqual(outcome["control"][:1] + outcome["control"][2:], (0, False), outcome["control"][1])
        self.assertEqual(outcome["mutant"][2], True, "the mutant never reached the outer bound: " + outcome["mutant"][1])
        self.assertIn("the stop waited for a blocked write", outcome["mutant"][1])

    def test_s16_inner_a_control_character_is_not_echoed_and_a_broken_stream_does_not_stop_the_child(self):
        if not os.environ.get("FLEET_TEST_INNER"):
            self.skipTest("run by test_s16 in a child process")
        rig = Rig(self, alive=0, env={"FLEET_HEAVY_PROGRESS_SECONDS": "a\nheavy: forged"})
        said = [line for _t, line in rig.out.lines]
        self.assertEqual(said, ["heavy: FLEET_HEAVY_PROGRESS_SECONDS=a?heavy: forged is out of range, using 60"], "a control character reached the error stream: %r" % (said,))
        rig = Rig(self, alive=0, env={"FLEET_HEAVY_PROGRESS_SECONDS": "a\x1b[31mb"})
        self.assertNotIn("\x1b", rig.out.text, "a control character reached the error stream")
        for error in (OSError, ValueError):
            for status in (0, 7):
                class Broken(Out):
                    def write(self, text):
                        raise error("broken")
                with mock.patch.dict(globals(), {"Out": Broken}):
                    try:
                        rig = Rig(self, alive=0, status=status, env={"FLEET_HEAVY_PROGRESS_SECONDS": "abc"})
                    except (OSError, ValueError):
                        self.fail("a broken error stream stopped the child (%s)" % error.__name__)
                self.assertEqual((len(rig.starts), rig.status), (1, status), "a broken error stream stopped the child (%s, status %d)" % (error.__name__, status))

    def test_s16_a_control_character_and_a_broken_stream_each_mutant_fails_with_its_text(self):
        shown = '''raw = "".join("?" if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c for c in raw[:40])'''
        guard = "except (OSError, ValueError):\n            pass"
        for old, new, text in ((shown, "pass", "a control character reached the error stream"),
                               (guard, "except ZeroDivisionError:\n            pass", "a broken error stream stopped the child")):
            results = self._scratch([(old, new)], "test_s16_inner_a_control_character_is_not_echoed_and_a_broken_stream_does_not_stop_the_child")
            self.assertEqual(results["control"].returncode, 0, results["control"].stdout)
            self.assertEqual(results["mutant"].returncode, 1, results["mutant"].stdout)
            self.assertIn(text, results["mutant"].stdout)

    def test_s17_inner_a_line_separator_is_not_echoed(self):
        if not os.environ.get("FLEET_TEST_INNER"):
            self.skipTest("run by test_s17 in a child process")
        for sep in ("\u2028", "\u2029"):
            rig = Rig(self, alive=0, env={"FLEET_HEAVY_PROGRESS_SECONDS": "a" + sep + "heavy: x"})
            self.assertEqual(len(rig.out.text.splitlines()), 1, "a line separator reached the error stream: %r" % (rig.out.text,))
            self.assertNotIn(sep, rig.out.text, "a line separator reached the error stream")
        heavy = load_heavy()
        for code in list(range(0x200)) + list(range(0x2000, 0x2100)) + [0xFEFF, 0xFFFE, 0xFFFF, 0x1D173, 0xE0001, 0xE007F]:   # a representative set: controls, Latin, the general punctuation block (U+2028, U+2029), format characters, noncharacters
            char = chr(code)
            out = Out([0])
            heavy._progress_interval({"FLEET_HEAVY_PROGRESS_SECONDS": char}, out)
            if not out.text:
                continue   # a digit: a valid interval, no fallback line
            line = out.text[len("heavy: FLEET_HEAVY_PROGRESS_SECONDS="):-len(" is out of range, using 60\n")]
            want = "?" if unicodedata.category(char) in ("Cc", "Zl", "Zp") else char
            if line != want:
                self.fail("a line separator reached the error stream: U+%04X gave %r, want %r" % (code, line, want))
            if len(out.text.splitlines()) != 1:
                self.fail("a line separator reached the error stream: U+%04X splits" % code)

    def test_s17_a_line_separator_mutant_fails_with_its_text_and_the_control_passes_first(self):
        source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin", "heavy_slots.py")
        text = open(source).read()
        lines = re.findall(r'^ *raw = "".join\(.*$', text, re.M)
        self.assertEqual(len(lines), 1, lines)
        # mutant E: the code-range version, without U+2028 and U+2029
        range_line = '        raw = "".join("?" if ord(c) < 32 or 127 <= ord(c) <= 159 else c for c in raw[:40])'
        results = self._scratch([(lines[0], range_line)], "test_s17_inner_a_line_separator_is_not_echoed")
        self.assertEqual(results["control"].returncode, 0, results["control"].stdout)
        self.assertEqual(results["mutant"].returncode, 1, results["mutant"].stdout)
        self.assertIn("a line separator reached the error stream", results["mutant"].stdout)


if __name__ == "__main__":
    unittest.main()
