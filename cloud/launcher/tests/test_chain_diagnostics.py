"""Ask #1414: the chain script shows lane output live, waits for and replays the lanes after a serial failure, and stops every lane on a signal.
Each case runs a COPY of bin/test-cloud-launcher (or a mutant of it) against a fake tree with fake entries. Offline; its own temporary directory; no port."""
import os, shutil, signal, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "worker", "tests"))
import stub_support  # noqa: E402
import test_chain_list as base  # noqa: E402

TESTS = "cloud/launcher/tests/"
LANE = "test_lane_slow.py"
SERIAL_BAD = "test_serial_bad.py"
SERIAL_SLOW = "test_serial_slow.py"

# The fake entries. DIAG_DIR (an environment variable) is the run's own directory: pid files and the end time.
FAKES = {
    LANE: "import os, sys, time\nd = os.environ['DIAG_DIR']\nprint('lane-body-start', flush=True)\nopen(os.path.join(d, 'lane.pid'), 'w').write(str(os.getpid()))\n"
          "time.sleep(float(os.environ.get('LANE_SECONDS', '3')))\nprint('lane-body-end', flush=True)\nopen(os.path.join(d, 'lane.end'), 'w').write(repr(time.time()))\n",
    SERIAL_BAD: "import sys, time\ntime.sleep(0.5)\nsys.exit(1)\n",
    SERIAL_SLOW: "import os, time\nopen(os.path.join(os.environ['DIAG_DIR'], 'serial.pid'), 'w').write(str(os.getpid()))\ntime.sleep(30)\n",
    "test_ok.py": "pass\n",
    "test_ok2.py": "pass\n",
    "test_ok3.py": "pass\n",
    "test_lane_second.py": "import os\nopen(os.path.join(os.environ['DIAG_DIR'], 'second.started'), 'w').write('x')\n",
}


def is_alive(pid):
    """Running and not a zombie (a pod without an init never reaps the dead). A pid that does not exist is dead."""
    if os.path.exists("/proc/self/stat"):
        try:
            with open("/proc/%d/stat" % pid) as f:
                # the state follows the LAST ")" : the command name may hold spaces and parentheses
                return f.read().rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            return False
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    except OSError:
        out = None
    if out is not None:
        return bool(out.strip()) and "Z" not in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def real_script():
    return base.slurp(base.SCRIPT)


def mutate(text, old, new):
    out = text.replace(old, new, 1)
    assert out != text, "the mutation did not apply: %r" % old
    return out


class Diag(unittest.TestCase):
    def tree(self, script, serial, lane):
        """A fake tree: the script text, the serial entries (in order) and the lane entries (the parallel list)."""
        d = tempfile.mkdtemp(prefix="chain-diag-")
        self.addCleanup(shutil.rmtree, d, True)
        self.diag = os.path.join(d, "diag")
        os.makedirs(self.diag)
        for sub in ("bin", "cloud/launcher/tests", "cloud/worker/tests"):
            os.makedirs(os.path.join(d, sub))
        path = os.path.join(d, "bin", "test-cloud-launcher")
        with open(path, "w") as f:
            f.write(script)
        os.chmod(path, 0o755)
        for n in set(serial) | set(lane):
            with open(os.path.join(d, TESTS + n), "w") as f:
                f.write(FAKES[n])
        with open(os.path.join(d, base.FIXED_LAST), "w") as f:
            f.write("pass\n")
        names = sorted(set(serial) | set(lane))
        with open(os.path.join(d, "bin", "test-cloud-launcher.chain"), "w") as f:
            f.write("".join(TESTS + n + "\n" for n in names) + base.FIXED_LAST + "\n")
        with open(os.path.join(d, "bin", "test-cloud-launcher.parallel"), "w") as f:
            f.write("".join(TESTS + n + "\n" for n in lane))
        return d

    shell = None   # None: the script runs through its own shebang line; else the path of the shell that runs it

    def start(self, d, extra_env=None):
        env = dict(os.environ, DIAG_DIR=self.diag, **(extra_env or {}))
        cmd = [os.path.join(d, "bin", "test-cloud-launcher")]
        if self.shell:
            cmd = [self.shell] + cmd
        return stub_support.start(self, cmd, cwd=d, env=env,
                                  stdin=subprocess.DEVNULL, preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),   # a shell cannot trap a signal ignored at its start
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def wait_file(self, name, seconds=15):
        path = os.path.join(self.diag, name)
        end = time.time() + seconds
        while not self.content(path):
            self.assertLess(time.time(), end, "%s never appeared" % name)
            time.sleep(0.05)
        return self.content(path)

    def content(self, path):
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            return ""

    def kill_later(self, name):
        """A fake that outlives a mutant script must not outlive the test."""
        def kill():
            path = os.path.join(self.diag, name)
            if self.content(path):
                try:
                    os.kill(int(self.content(path)), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass
        self.addCleanup(kill)

    # S2: the RUN line is printed before the entry ends; every live line names the entry; the replay is whole.
    def s2(self, script):
        d = self.tree(script, ["test_ok.py"], [LANE])
        entry = TESTS + LANE
        p = self.start(d)
        live = []
        while True:
            line = p.stdout.readline()
            self.assertTrue(line, "the output ended with no RUN line for the lane entry")
            live.append(line)
            if line.startswith("RUN " + entry):
                break
        seen = time.time()
        rest = p.stdout.read()
        p.wait(timeout=60)
        self.assertEqual(p.returncode, 0, "".join(live) + rest)
        end = float(self.wait_file("lane.end"))
        self.assertLess(seen, end, "the RUN line came after the entry's end: lane output shown only at the end")
        out = "".join(live) + rest
        for l in out.split("\n"):
            if l.startswith(("RUN ", "TIME ", "FAILED")):
                self.assertIn("cloud/", l, "a live line without an entry name: %r" % l)
        self.assertIn("RUN %s\nlane-body-start\nlane-body-end\nTIME %s " % (entry, entry), out, "the replay of the lane's output is not whole")

    # S3: a serial failure while a lane runs: the lane's output is replayed before RESULT: FAIL, status 1.
    def s3(self, script):
        d = self.tree(script, [SERIAL_BAD], [LANE])
        self.kill_later("lane.pid")
        p = self.start(d)
        out, _ = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 1, out)
        self.assertIn("FAILED: %s%s" % (TESTS, SERIAL_BAD), out)
        self.assertIn("lane-body-end", out, "the lane's output was not replayed")
        self.assertLess(out.index("lane-body-end"), out.index("RESULT: FAIL"))
        self.assertEqual(out.rstrip().split("\n")[-1], "RESULT: FAIL")

    # S4: a signal while a lane and a serial entry sleep: both are gone and the status is not 0.
    def s4(self, script, sig):
        d = self.tree(script, [SERIAL_SLOW], [LANE])
        self.kill_later("lane.pid")
        self.kill_later("serial.pid")
        env_seconds = os.environ.get("LANE_SECONDS")
        os.environ["LANE_SECONDS"] = "30"
        self.addCleanup(lambda: os.environ.__setitem__("LANE_SECONDS", env_seconds) if env_seconds else os.environ.pop("LANE_SECONDS", None))
        p = self.start(d)
        pids = [int(self.wait_file("lane.pid")), int(self.wait_file("serial.pid"))]
        p.send_signal(sig)
        p.wait(timeout=60)   # not communicate(): a lane left running holds the output pipe open
        self.assertNotEqual(p.returncode, 0)
        end = time.time() + 10
        while any(is_alive(x) for x in pids) and time.time() < end:
            time.sleep(0.1)
        self.assertEqual([x for x in pids if is_alive(x)], [], "a lane or serial process was left running after signal %s" % sig)

    # S6: after a serial failure no lane starts a new entry; an entry in flight that outlives the bound is stopped; nothing is left running.
    def s6_no_new_entry(self, script):
        # lane 0 holds the slow entry (1st) and the second entry (4th of the list); the two fillers go to lanes 1 and 2
        d = self.tree(script, [SERIAL_BAD], [LANE, "test_ok.py", "test_ok2.py", "test_lane_second.py"])
        self.kill_later("lane.pid")
        p = self.start(d)
        out, _ = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 1, out)
        self.assertIn("lane-body-end", out, "the entry in flight was not let finish")
        time.sleep(1)
        self.assertFalse(os.path.exists(os.path.join(self.diag, "second.started")), "a lane started a new entry after the serial failure")
        self.assertEqual(out.rstrip().split("\n")[-1], "RESULT: FAIL")

    def s6_bound(self, script):
        d = self.tree(script, [SERIAL_BAD], [LANE])
        self.kill_later("lane.pid")
        p = self.start(d, {"LANE_SECONDS": "30", "CHAIN_LANE_WAIT": "2"})
        try:
            out, _ = p.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            self.fail("the script did not stop the entry in flight at the bound")
        self.assertEqual(p.returncode, 1, out)
        self.assertEqual(out.rstrip().split("\n")[-1], "RESULT: FAIL")
        pid = int(self.wait_file("lane.pid"))
        end = time.time() + 5
        while is_alive(pid) and time.time() < end:
            time.sleep(0.1)
        self.assertFalse(is_alive(pid), "the entry in flight was left running")

    def test_s6_a_serial_failure_starts_no_new_lane_entry(self):
        self.s6_no_new_entry(real_script())

    def test_s6_an_entry_in_flight_is_stopped_at_the_bound(self):
        self.s6_bound(real_script())

    def test_s6_mutant_a_lane_keeps_starting_entries_fails_the_first(self):
        m = mutate(real_script(), '    [ ! -e "$OUT/stop" ] || break   # a serial entry failed: start no new entry\n', "")
        with self.assertRaises(AssertionError):
            self.s6_no_new_entry(m)

    def test_s6_mutant_the_bound_is_ignored_fails_the_second(self):
        m = mutate(real_script(), "LANE_WAIT=${CHAIN_LANE_WAIT:-300}", "LANE_WAIT=300")
        with self.assertRaises(AssertionError):
            self.s6_bound(m)

    def test_s2_the_run_line_is_live_and_the_replay_is_whole(self):
        self.s2(real_script())

    def test_s3_a_serial_failure_waits_for_the_lane_and_replays_it_before_result(self):
        self.s3(real_script())

    def test_s4_an_interrupt_and_a_terminate_leave_no_process(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(sig=sig):
                self.s4(real_script(), sig)

    # S5: a mutant for each of S2, S3, S4 fails its test.
    def test_s5_mutant_lane_output_only_at_the_end_fails_s2(self):
        m = mutate(real_script(), 'note() { echo "$1"; [ -z "$LOGF" ]', 'note() { [ -n "$LOGF" ] || echo "$1"; [ -z "$LOGF" ]')
        with self.assertRaises(AssertionError):
            self.s2(m)

    def test_s5_mutant_no_replay_after_a_serial_failure_fails_s3(self):
        m = mutate(real_script(), "  stop_groups\n  replay\n  echo \"RESULT: FAIL\"", "  stop_groups\n  echo \"RESULT: FAIL\"")
        with self.assertRaises(AssertionError):
            self.s3(m)

    def test_s5_mutant_no_trap_fails_s4(self):
        text = real_script()
        m = mutate(mutate(text, "trap 'stop_groups; exit 130' INT", ":"), "trap 'stop_groups; exit 143' TERM", ":")
        with self.assertRaises(AssertionError):
            self.s4(m, signal.SIGTERM)


# Ask #1414, KILL after a short grace: a lane entry whose child ignores TERM. The child writes "<shell pid> <sleep pid>" to its file.
STUBBORN = "test_lane_stubborn.py"
FAKES[STUBBORN] = (
    "import os, subprocess, sys\nd = os.environ['DIAG_DIR']\n"
    "p = subprocess.Popen(['sh', '-c', \"trap '' TERM; sleep 120 & echo \\\"$$ $!\\\" > \\\"$1.tmp\\\"; mv \\\"$1.tmp\\\" \\\"$1\\\"; wait\", 'sh', os.path.join(d, 'stub.pid')])\n"
    "p.wait()\n")


class Kill(unittest.TestCase):
    """The cases of the KILL after the grace, run under the default shell and under dash (classes below)."""
    GRACE = "2"
    shell = None
    tree, start, wait_file, content, kill_later = Diag.tree, Diag.start, Diag.wait_file, Diag.content, Diag.kill_later

    def stubborn_tree(self, script, serial):
        d = self.tree(script, serial, [STUBBORN])
        def cleanup():
            for w in self.content(os.path.join(self.diag, "stub.pid")).split():
                try:
                    os.kill(int(w), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass
        self.addCleanup(cleanup)
        self.kill_later("lane.pid")
        return d

    def stub_pids(self):
        return [int(w) for w in self.wait_file("stub.pid").split()]

    def gone(self, pids, what):
        end = time.time() + int(self.GRACE) + 6   # the grace plus a margin
        while any(is_alive(x) for x in pids) and time.time() < end:
            time.sleep(0.1)
        self.assertEqual([x for x in pids if is_alive(x)], [], "a TERM-ignoring descendant was left running after %s" % what)

    def k_signal(self, script, sig):
        d = self.stubborn_tree(script, [SERIAL_SLOW])
        self.kill_later("serial.pid")
        p = self.start(d, {"CHAIN_KILL_GRACE": self.GRACE})
        pids = self.stub_pids()
        self.wait_file("serial.pid")
        p.send_signal(sig)
        p.wait(timeout=60)
        self.assertEqual(p.returncode, 130 if sig == signal.SIGINT else 143)
        self.gone(pids, "signal %s" % sig)

    def k_serial_failure(self, script):
        d = self.stubborn_tree(script, [SERIAL_BAD])
        p = self.start(d, {"CHAIN_KILL_GRACE": self.GRACE, "CHAIN_LANE_WAIT": "2"})
        pids = self.stub_pids()
        p.wait(timeout=60)
        self.assertEqual(p.returncode, 1)
        self.gone(pids, "a serial failure")

    def test_k_a_term_ignoring_descendant_is_gone_after_an_interrupt(self):
        self.k_signal(real_script(), signal.SIGINT)

    def test_k_a_term_ignoring_descendant_is_gone_after_a_terminate(self):
        self.k_signal(real_script(), signal.SIGTERM)

    def test_k_a_term_ignoring_descendant_is_gone_after_a_serial_failure(self):
        self.k_serial_failure(real_script())

    def test_k_mutant_without_the_kill_fails_each_case(self):
        text = real_script()
        m = mutate(text, '    [ -z "$surv" ] || kill -KILL $surv 2>/dev/null\n', "")
        for case in (lambda: self.k_signal(m, signal.SIGINT), lambda: self.k_signal(m, signal.SIGTERM), lambda: self.k_serial_failure(m)):
            with self.assertRaises(AssertionError):
                case()


class IsAlive(unittest.TestCase):
    """The liveness helper itself, and a mutant of it."""
    def test_a_zombie_is_dead(self):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        try:
            end = time.time() + 5
            while is_alive(pid) and time.time() < end:
                time.sleep(0.05)
            self.assertFalse(is_alive(pid), "an unreaped exited child was counted alive")
        finally:
            os.waitpid(pid, 0)

    def test_a_live_sleeper_is_alive(self):
        p = subprocess.Popen(["sleep", "30"])
        try:
            time.sleep(0.2)
            self.assertTrue(is_alive(p.pid))
        finally:
            p.kill()
            p.wait()

    def test_a_pid_that_does_not_exist_is_dead(self):
        p = subprocess.Popen(["true"])
        p.wait()   # reaped: the pid is free
        self.assertFalse(is_alive(p.pid))

    def test_s4_mutant_a_helper_that_is_always_alive_fails_the_s4_tests(self):
        from unittest import mock
        me = sys.modules[__name__]
        for cls in [c for c in (Diag, globals().get("DashDiag")) if c is not None]:
            with mock.patch.object(me, "is_alive", lambda pid: True):
                self.assertTrue(me.is_alive(0), "the mutation was not applied")
                res = unittest.TestResult()
                cls("test_s4_an_interrupt_and_a_terminate_leave_no_process").run(res)
            self.assertFalse(res.wasSuccessful(), "the S4 test of %s passed with a helper that is always alive" % cls.__name__)


class Docs(unittest.TestCase):
    def section(self):
        doc = base.slurp(os.path.join(base.ROOT, "cloud", "launcher", "README.md"))
        self.assertIn("## Chain diagnostics (ask #1414)", doc)
        return doc.split("## Chain diagnostics (ask #1414)")[1].split("\n## ")[0]

    def test_s7_the_readme_says_what_a_hung_lane_looks_like(self):
        self.assertIn("a RUN line with no TIME line", self.section())

    def test_s1_the_readme_names_the_three_causes(self):
        sec = self.section()
        for words in ("only at the end", "kills only the lane subshells", "no trap for an interrupt or a terminate signal"):
            self.assertIn(words, sec)

    def test_s7_the_readme_states_the_wait_bound(self):
        self.assertIn("300 seconds", self.section())


DASH = next((c for c in ("/bin/dash", "/usr/bin/dash") if os.path.exists(c)), None)


class DashDiag(Diag):
    """The same cases, the script run by dash (the /bin/sh of the Ubuntu CI leg)."""
    shell = DASH


if DASH is None:
    print("SKIPPED: no dash on this machine (/bin/dash, /usr/bin/dash): the dash cases do not run", flush=True)
    del DashDiag


class DashKill(Kill):
    shell = DASH


if DASH is None:
    del DashKill


if __name__ == "__main__":
    unittest.main()
