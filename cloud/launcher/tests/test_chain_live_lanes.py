"""Ask #1414: a lane prints its RUN and TIME lines live (entry name on each), keeps its own output whole for the replay,
and a signal stops the lane children. Offline; scratch trees and scratch mutants under the temp dir. The fake entries wait
on named pipes (events), never on the clock, to decide anything."""
import os, shutil, signal, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_chain_list as base

T = "cloud/launcher/tests/"
FIFO_PY = ("import os\n"
           "def fifo(n): return os.open(os.path.join(os.environ['CHAIN_LOG'], n), os.O_RDWR)\n"
           "def put(n): os.write(fifo(n), b'x\\n')\n"
           "def get(n): os.read(fifo(n), 2)\n")
SLOW = FIFO_PY + "print('slow-body', flush=True)\nput('started')\nget('release')\n"
SERIAL = FIFO_PY + "get('started')\nprint('SERIAL-SAW-START', flush=True)\nput('release')\n"
PING_A = FIFO_PY + "print('a1', flush=True)\nput('ab')\nget('ba')\nprint('a2', flush=True)\nput('ab')\n"
PING_B = FIFO_PY + "get('ab')\nprint('b1', flush=True)\nput('ba')\nget('ab')\nprint('b2', flush=True)\n"
HANG = "#!/bin/sh\nsleep 300 &\necho \"$$ $!\" > \"$CHAIN_LOG/started\"\nwait\n"


class Lanes(base.Scratch):
    def build(self, names, par, files):
        d = self.tree(sorted(names), tests=())
        for n, body in files.items():
            with open(os.path.join(d, T + n), "w") as f:
                f.write(body)
        with open(os.path.join(d, "bin", "test-cloud-launcher.parallel"), "w") as f:
            f.write("".join(T + n + "\n" for n in par))
        return d

    def log(self, fifos):
        log = tempfile.mkdtemp(prefix="chain-log-"); self.addCleanup(shutil.rmtree, log, True)
        self.fds = {}
        for n in fifos:
            os.mkfifo(os.path.join(log, n))
            fd = os.open(os.path.join(log, n), os.O_RDWR)   # held open here so a written token is not lost between two entries
            self.addCleanup(os.close, fd)
            self.fds[n] = fd
        return log

    def put_script(self, d, script):
        if script is not None:
            with open(os.path.join(d, "bin", "test-cloud-launcher"), "w") as f:
                f.write(script)

    def chain(self, d, script, fifos=()):
        self.put_script(d, script)
        return subprocess.run([os.path.join(d, "bin", "test-cloud-launcher")], cwd=d, capture_output=True, text=True,
                              env=dict(os.environ, CHAIN_LOG=self.log(fifos)))

    def mutate(self, old, new):
        src = base.slurp(base.SCRIPT)
        self.assertIn(old, src)
        return src.replace(old, new)

    # ---- the RUN line is on the output while the lane entry still waits
    def live_problems(self, script):
        d = self.build(["test_a_serial.py", "test_b_slow.py"], ["test_b_slow.py"],
                       {"test_a_serial.py": SERIAL, "test_b_slow.py": SLOW})
        p = self.chain(d, script, fifos=("started", "release"))
        out = p.stdout.split("\n")
        run = "RUN " + T + "test_b_slow.py"
        bad = []
        if p.returncode != 0: bad.append("exit %d" % p.returncode)
        if run not in out or "SERIAL-SAW-START" not in out: return bad + ["lines missing"]
        if out.index(run) > out.index("SERIAL-SAW-START"): bad.append("RUN came after the entry had run")
        if out.count(run) != 1: bad.append("RUN lines %d" % out.count(run))
        if not [l for l in out if l.startswith("TIME " + T + "test_b_slow.py ")]: bad.append("no TIME")
        return bad

    def test_run_line_is_live_before_the_lane_entry_ends(self):
        self.assertEqual(self.live_problems(None), [])

    def test_mutant_printing_run_only_in_the_replay_is_caught(self):
        self.assertIn("RUN came after the entry had run", self.live_problems(self.mutate('echo "$1" >&3', 'echo "$1"')))

    # ---- each lane's output is replayed whole, blocks in list order
    def blocks(self, script):
        d = self.build(["test_a.py", "test_b.py"], ["test_a.py", "test_b.py"], {"test_a.py": PING_A, "test_b.py": PING_B})
        return self.chain(d, script, fifos=("ab", "ba"))

    def test_each_lane_output_is_replayed_whole_and_in_list_order(self):
        p = self.blocks(None)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("a1\na2\n", p.stdout)
        self.assertIn("b1\nb2\n", p.stdout)
        self.assertLess(p.stdout.index("a1\n"), p.stdout.index("b1\n"))

    def test_mutant_streaming_lane_output_is_caught(self):
        p = self.blocks(self.mutate('3>&1 > "$OUT/$f.out" 2>&1', ''))
        self.assertNotIn("a1\na2\n", p.stdout)

    # ---- a signal stops the lane children
    def alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        try:
            with open("/proc/%d/stat" % pid) as f:
                return f.read().rsplit(")", 1)[1].split()[0] != "Z"
        except OSError:
            return True

    def signal_problems(self, sig, script):
        d = self.tree(["test_x.py"], tests=())
        with open(os.path.join(d, "bin", "test-hang"), "w") as f:
            f.write(HANG)
        os.chmod(os.path.join(d, "bin", "test-hang"), 0o755)
        with open(os.path.join(d, "bin", "test-cloud-launcher.chain"), "w") as f:
            f.write("bin/test-hang\n" + base.FIXED_LAST + "\n")
        with open(os.path.join(d, "bin", "test-cloud-launcher.parallel"), "w") as f:
            f.write("bin/test-hang\n")
        self.put_script(d, script)
        log = self.log(["started"])
        outf = tempfile.TemporaryFile("w+")   # a file, not a pipe: a surviving child must not hold the reader open
        self.addCleanup(outf.close)
        p = subprocess.Popen([os.path.join(d, "bin", "test-cloud-launcher")], cwd=d, stdout=outf,
                             stderr=subprocess.STDOUT, text=True, env=dict(os.environ, CHAIN_LOG=log))
        self.addCleanup(p.kill)
        pids = [int(x) for x in os.read(self.fds["started"], 100).split()]   # blocks until the lane entry has started its child
        for x in pids:
            self.addCleanup(lambda x=x: os.kill(x, signal.SIGKILL) if self.alive(x) else None)
        p.send_signal(sig)
        p.wait()
        outf.seek(0); out = outf.read()
        bad = []
        if p.returncode == 0: bad.append("exit 0")
        if "RESULT: PASS" in out: bad.append("PASS printed")
        for _ in range(200):   # the signalled children are reaped by the system a moment later
            if not any(self.alive(x) for x in pids): break
            time.sleep(0.05)
        left = [x for x in pids if self.alive(x)]
        if left: bad.append("lane children left %d" % len(left))
        return bad

    def test_interrupt_and_terminate_stop_the_lane_children(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.assertEqual(self.signal_problems(sig, None), [], sig)

    def test_mutant_without_the_trap_is_caught(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            got = self.signal_problems(sig, self.mutate("' TERM INT\n", "' USR2\n"))
            self.assertTrue([b for b in got if b.startswith("lane children left")], (sig, got))


class Docs(unittest.TestCase):
    def test_readme_says_what_the_diagnostics_show(self):
        t = base.slurp(os.path.join(base.ROOT, "cloud", "launcher", "README.md"))
        for phrase in ("printed live", "hung lane", "line with no `TIME"):
            self.assertIn(phrase, t)


if __name__ == "__main__":
    unittest.main()
