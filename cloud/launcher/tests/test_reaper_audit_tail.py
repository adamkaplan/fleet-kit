"""Ask #1742: the renewal reads the audit tail ONCE per tick; one CPU/lines log line per reaper pass and per renewal tick.

The "tip" copy is the real module with pin (b)'s lookup switched off (the tip's code: last_audit_line per failing pod). Mutants are built from
the real source by an ANCHORED text replacement whose count is asserted to be 1 (the mutation is asserted applied)."""
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
import datetime
import io
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "worker", "tests"))
import test_cloud_worker as base  # noqa: E402
import stub_support  # noqa: E402
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
SOURCE_PATH = os.path.join(LAUNCHER, "cloud_worker.py")
TIP = "a2bdee42814e91dccc0af87820b8f27e633db12d"
T0 = base.T0
WINDOW = 262144
PODS = 100
PIN_B_NEW = '        tail = getattr(self, "_tail", None)\n'
PLANTS = ("planted-pod-7", "10.9.8.7:6443", "t" + "0123456789abcdef0123456789abcdef0123")


def source():
    with open(SOURCE_PATH) as f:
        return f.read()


class CountingJson:
    """The module's json, counting the loads calls (the lines parsed)."""
    def __init__(self):
        self.n = 0

    def loads(self, s, *a, **k):
        self.n += 1
        return json.loads(s, *a, **k)

    def __getattr__(self, name):
        return getattr(json, name)


class FakeTime:
    """time with a thread_time that advances 0.25 on each call (the fake CPU counter)."""
    def __init__(self):
        self.t = 0.0

    def thread_time(self):
        self.t += 0.25
        return self.t

    def __getattr__(self, name):
        return getattr(__import__("time"), name)


def build_module(replace=None, name="cloud_worker_tail_copy"):
    """A copy of the real module (optionally with one anchored replacement, asserted applied exactly once); its json counts."""
    src = source()
    if replace:
        old, new = replace
        assert src.count(old) == 1, "the mutation did not apply: %r" % old
        src = src.replace(old, new)
    mod = types.ModuleType(name)
    mod.__file__ = SOURCE_PATH
    sys.modules[name] = mod
    try:
        exec(compile(src, SOURCE_PATH, "exec"), mod.__dict__)
    finally:
        del sys.modules[name]
    mod.json = CountingJson()   # the module's own `import json` ran first: replace it with the counter
    return mod


def tip_module():
    return build_module((PIN_B_NEW, "        tail = None\n"), "cloud_worker_tip_copy")


class FakeKube:
    def __init__(self, pods):
        self.pods = pods

    def scrub(self, s, *a, **k):
        return str(s)

    def add_secrets(self, *a):
        pass

    def close(self):
        pass

    def list_pods(self):
        return self.pods


def make_pods(n=PODS):
    return [{"metadata": {"name": "pod-%d" % i, "annotations": {"cw/worker": "w%d" % i}}} for i in range(n)]


class World:
    """A temp audit file with a tail of padding lines (and two lines of pod 0: an older 'lapsed' and a newer 'stale'), failing pods."""
    def __init__(self, test, mod, pods=None, reason="lapsed", seed=True, padding=1600):
        self.dir = tempfile.mkdtemp(prefix="tail-")
        test.addCleanup(self._rm)
        self.path = os.path.join(self.dir, "audit.jsonl")
        self.mod, self.reason = mod, reason
        self.pods = pods or make_pods()
        lines = [{"action": "create", "runtime_id": "pod/x%d" % i, "ok": True, "reason": "", "worker": "w", "ts": "t"} for i in range(padding)]
        if seed:
            lines.append({"action": "renew-git-token", "runtime_id": "pod/pod-0", "ok": False, "reason": "lapsed", "worker": "w0", "ts": "t"})
            lines.append({"action": "renew-git-token", "runtime_id": "pod/pod-0", "ok": False, "reason": "stale", "worker": "w0", "ts": "t"})
        with open(self.path, "w") as f:
            for r in lines:
                f.write(json.dumps(r, sort_keys=True) + "\n")
        self.L = mod.Launcher(FakeKube(self.pods), {"CW_CLOUD_AUDIT": self.path}, lambda: T0, lambda s: None)
        self.reasons = {}

        def fail(pod, now):
            raise mod._NotRenewed(self.reasons.get(pod["metadata"]["name"], self.reason))
        self.L._renew_git_token = fail

    def _rm(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def tail_line_count(self):
        with open(self.path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - WINDOW))
            return len(f.read().decode(errors="replace").splitlines())

    def audit_lines(self):
        with open(self.path) as f:
            return [json.loads(x) for x in f if x.strip()]

    def tick(self):
        """One renewal pass; (report, lines parsed by this tick, tail's line count before it)."""
        before = self.tail_line_count()
        self.mod.json.n = 0
        report = self.L.renew_git_tokens(self.pods, T0)
        return report, self.mod.json.n, before


def strip(lines):
    return [{k: v for k, v in r.items() if k not in ("ts", "actor")} for r in lines]


def check_once_per_tick(test, mod):
    w = World(test, mod)
    for i in range(2):
        _, parsed, own = w.tick()
        if parsed > own:
            raise AssertionError("the tail was read per pod: tick %d parsed %d lines, the tail holds %d" % (i + 1, parsed, own))


def run_four_ticks(test, mod):
    """Four ticks (the third changes the reason of one pod): ([(lines written, report)], all audit lines)."""
    w = World(test, mod)
    runs = []
    for i in range(4):
        if i == 2:
            w.reasons["pod-5"] = "changed"
        n0 = len(w.audit_lines())
        report, _, _ = w.tick()
        runs.append((len(w.audit_lines()) - n0, report))
    return runs, strip(w.audit_lines())


class TestOnePerTick(unittest.TestCase):
    def test_control_one_read_per_tick(self):
        check_once_per_tick(self, build_module())

    def test_tip_reads_per_pod(self):
        w = World(self, tip_module())
        _, parsed, own = w.tick()
        self.assertGreaterEqual(parsed, 50 * own)   # the tip's count for 100 pods (S1's number)
        print("S1: tip parsed %d lines for %d pods, tail %d lines (%.0f times)" % (parsed, PODS, own, parsed / own))

    def test_s2_per_pod_copy_fails(self):
        mut = build_module((PIN_B_NEW, '        tail = AuditTail(self.audit_path) if getattr(self, "_tail", None) is not None else None\n'))
        with self.assertRaisesRegex(AssertionError, "the tail was read per pod"):
            check_once_per_tick(self, mut)

    def test_s2_counts(self):
        w = World(self, build_module())
        for i in range(2):
            _, parsed, own = w.tick()
            self.assertLessEqual(parsed, own)
            print("S2: tick %d parsed %d lines, tail %d lines: within" % (i + 1, parsed, own))


class TestSameAsTip(unittest.TestCase):
    def test_s3_equal_to_tip(self):
        tip_runs, tip_lines = run_four_ticks(self, tip_module())
        new_runs, new_lines = run_four_ticks(self, build_module())
        self.assertEqual([r[0] for r in tip_runs], [100, 0, 1, 0])   # the values recorded from the tip's code (a copy of the module without the lookup)
        self.assertEqual(new_runs, tip_runs)
        self.assertEqual(new_lines, tip_lines)
        print("S3: lines written per tick %s, report entries equal in %d of %d ticks" % ([r[0] for r in new_runs], sum(a[1] == b[1] for a, b in zip(new_runs, tip_runs)), len(tip_runs)))

    def test_s3_oldest_wins_copy_fails(self):
        tip_runs, tip_lines = run_four_ticks(self, tip_module())
        key = '(rec.get("action"), rec.get("runtime_id"))'
        val = '(rec.get("ok"), rec.get("reason"))'
        mut = build_module(("self.newest.setdefault(%s, %s)" % (key, val), "self.newest[%s] = %s" % (key, val)))
        runs, lines = run_four_ticks(self, mut)

        def check():
            if [r[0] for r in runs] != [r[0] for r in tip_runs] or lines != tip_lines:
                raise AssertionError("the newest line must win: lines per tick %s, the tip wrote %s" % ([r[0] for r in runs], [r[0] for r in tip_runs]))
        with self.assertRaisesRegex(AssertionError, "the newest line must win"):
            check()


class LineFixture:
    """The pass and the tick on a fake world with a fake CPU counter."""
    def __init__(self, test, mod):
        mod.time = FakeTime()
        mod.reaper_lock = lambda *a, **k: None
        pods = [{"metadata": {"name": n, "annotations": {"cw/worker": "w%d" % i}}} for i, n in enumerate((PLANTS[0], "pod-b", "pod-c"))]
        self.w = World(test, mod, pods=pods, reason=PLANTS[1] + " " + PLANTS[2], seed=False, padding=5)   # a tail of 5 lines
        L = self.w.L
        L._before_hard_limit = lambda pods, now: pods

        class Refresher:
            notices = []

            def run(self, force=False):
                return types.SimpleNamespace(state="fresh", reason="", token="")
        self.mod, self.refresher = mod, Refresher()
        L.reap = lambda: (L.renew_git_tokens(self.w.pods, T0), [])[1]
        L.reap_results = {}
        L.note_slow_pass = lambda took: False
        real = mod.Launcher
        mod.Launcher = lambda kube, env, clock, sleep: L
        mod.Launcher._catch_signals, mod.Launcher._restore_signals = real._catch_signals, real._restore_signals
        mod.RenewalTicker.start = lambda self: None

    def pass_text(self):
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.mod._reap_main({"CW_CLOUD_NAMESPACE": "cw"}, types.SimpleNamespace(loop=0), lambda s: None, clock=lambda: T0,
                                refresher=self.refresher, kube=self.w.L.kube)
        return err.getvalue()

    def tick_text(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.mod.RenewalTicker(self.w.L, self.refresher, lambda: T0, lambda: None).tick()
        return err.getvalue()


def check_lines(test, mod):
    f = LineFixture(test, mod)
    ptext = f.pass_text()
    if "reaper pass line" not in ptext:
        raise AssertionError("no pass line")
    ttext = f.tick_text()
    if "renewal tick line" not in ttext:
        raise AssertionError("no tick line")
    return ptext, ttext


class TestLogLines(unittest.TestCase):
    def test_s4_lines_hold_fake_values(self):
        ptext, ttext = check_lines(self, build_module())
        pl = [x for x in ptext.splitlines() if "reaper pass line" in x]
        tl = [x for x in ttext.splitlines() if "renewal tick line" in x]
        self.assertEqual((len(pl), len(tl)), (1, 1))
        # the pass parses the 5 seeded lines; it writes 3 failure lines, so the tick then parses 5 + 3
        self.assertRegex(pl[0], r"^cloud-worker: reaper pass line: cpu_seconds=0\.250 audit_lines_parsed=5$")
        self.assertRegex(tl[0], r"^cloud-worker: renewal tick line: cpu_seconds=0\.250 audit_lines_parsed=8$")
        print("S4: %s | %s" % (pl[0], tl[0]))

    def test_s4_copy_without_line_fails(self):
        mut = build_module(('print("cloud-worker: reaper pass line:', '(lambda *a, **k: None)("cloud-worker: reaper pass line:'))
        with self.assertRaisesRegex(AssertionError, "no pass line"):
            check_lines(self, mut)

    def test_s5_planted_strings_absent(self):
        ptext, ttext = check_lines(self, build_module())
        text = ptext + ttext
        found = [p for p in PLANTS if p in text] + [n for n in ("pod-b", "pod-c", "w0", "w1", "w2") if n in text]
        self.assertEqual(found, [])
        print("S5: 0 of %d planted strings and 0 pod or worker names in the 2 lines" % len(PLANTS))


class TestDocs(unittest.TestCase):
    PHRASES = ("reaper pass line", "renewal tick line", "cpu_seconds", "audit_lines_parsed", "counts only", "standard error")

    @staticmethod
    def paragraph(text):
        for block in text.split("\n\n"):
            if block.lstrip().startswith("**The reaper pass line"):
                return block
        return ""

    def check(self, text):
        para = self.paragraph(text)
        missing = [p for p in self.PHRASES if p not in para]
        if missing:
            raise AssertionError("no reaper pass line paragraph, missing: %s" % missing)

    def test_s6_docs(self):
        with open(os.path.join(ROOT, "cloud", "README.md")) as f:
            text = f.read()
        self.check(text)
        print("S6: %d of %d phrases present" % (len(self.PHRASES), len(self.PHRASES)))
        cut = text.replace(self.paragraph(text), "")
        self.assertNotEqual(cut, text)
        with self.assertRaisesRegex(AssertionError, "reaper pass line"):
            self.check(cut)


class StepClock:
    """A fake monotonic clock: a fake step moves it by its own seconds."""
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def thread_time(self):
        return 0.0

    def __getattr__(self, name):
        return getattr(__import__("time"), name)


STEP_NAMES = ("list", "renew", "vanished", "activity", "stops", "orphans", "notes")
STEP_LINE = "steps list=1.0 renew=2.0 vanished=0.5 activity=0.6 (3) stops=4.0 (1) orphans=0.5 notes=1.5 total=10.1"


def step_pass_text(test, mod):
    clock = StepClock()
    cls = mod.Launcher
    f = LineFixture(test, mod)
    mod.time = clock
    L = f.w.L
    L.reap = types.MethodType(cls.reap, L)
    for i, pod in enumerate(f.w.pods):
        pod["metadata"]["annotations"]["cw/note"] = PLANTS[1] + " " + PLANTS[2]

    def step(seconds, answer=None):
        def call(*a, **k):
            clock.t += seconds
            return answer
        return call
    L.kube.list_pods = step(1.0, f.w.pods)
    L.renew_git_tokens = step(2.0, [])
    L._note_vanished_pods = step(0.5)
    L.hard_deadline = lambda pod: T0 + datetime.timedelta(days=1)
    idle = f.w.pods[2]["metadata"]["name"]

    def activity(name):
        clock.t += 0.2
        return (False, T0 - datetime.timedelta(days=1)) if name == idle else (True, T0)
    L.activity = activity
    L.stop = lambda ns, reason: (step(4.0)(), {})[1]
    L.retire_row = lambda worker, reason: None
    L.remove_orphan_secrets = step(0.5)
    L.send_wip_notes = step(1.5)
    text = f.pass_text()
    if " steps list=" not in text:
        raise AssertionError("no step seconds")
    return text


class TestStepSeconds(unittest.TestCase):
    def test_s8_step_seconds(self):
        ctext = step_pass_text(self, build_module())   # the control first
        line = [x for x in ctext.splitlines() if "reaper pass line" in x]
        self.assertEqual(len(line), 1)
        self.assertTrue(line[0].endswith(STEP_LINE), line[0])
        self.assertEqual([p for p in (PLANTS[0], "pod-b", "pod-c") + PLANTS[1:] if p in ctext], [])
        print("S8: control passes: %s" % line[0].split("audit_lines_parsed=", 1)[1].split(" ", 1)[1])
        old = 'getattr(L, "step_seconds", None)'
        mut = build_module((old, "None"))
        with self.assertRaisesRegex(AssertionError, "^no step seconds$"):
            step_pass_text(self, mut)
        with open(os.path.join(ROOT, "cloud", "README.md")) as f:
            para = TestDocs.paragraph(f.read())
        found = [n for n in STEP_NAMES if "`%s`" % n in para]
        self.assertEqual(len(found), 7)
        print("S8: 7 of 7 step seconds + 2 counts + total equal; 0 of 3 pod names, 0 planted; mutant fails 'no step seconds'; %d of 7 names in README" % len(found))


S10_STEPS = (("list", "list=1.5"), ("renew", "renew=2.5"), ("vanished", "vanished=1.0"), ("activity", "activity=0.9 (2)"),
             ("orphans", "orphans=1.0"), ("notes", "notes=2.0"))

# the total of the steps done, from the fake clock: list 1.5; 1.0+2.5; 1.0+2.0+1.0; 1.0+2.0+0.5+0.9; 3.5+0.6+4.0+1.0; 3.5+0.6+4.0+0.5+2.0
S10_TOTALS = {"list": 1.5, "renew": 3.5, "vanished": 4.0, "activity": 4.4, "orphans": 9.1, "notes": 10.6}


def raising_pass_text(test, mod, raising):
    """The fake world of S8 with ONE step raising after its fake seconds plus 0.5; returns the pass line (or fails with the anchored text)."""
    clock = StepClock()
    cls = mod.Launcher
    f = LineFixture(test, mod)
    mod.time = clock
    L = f.w.L
    L.reap = types.MethodType(cls.reap, L)

    def step(name, seconds, answer=None):
        def call(*a, **k):
            clock.t += seconds
            if name == raising:
                clock.t += 0.5
                raise RuntimeError("fake step failure")
            return answer
        return call
    L.kube.list_pods = step("list", 1.0, f.w.pods)
    L.renew_git_tokens = step("renew", 2.0, [])
    L._note_vanished_pods = step("vanished", 0.5)
    L.hard_deadline = lambda pod: T0 + datetime.timedelta(days=1)
    idle = f.w.pods[2]["metadata"]["name"]
    reads = []

    def activity(name):
        reads.append(name)
        clock.t += 0.2
        if raising == "activity" and len(reads) == 2:
            clock.t += 0.5
            raise RuntimeError("fake step failure")
        return (False, T0 - datetime.timedelta(days=1)) if name == idle else (True, T0)
    L.activity = activity
    L.stop = lambda ns, reason: (step("stops", 4.0)(), {})[1]
    L.retire_row = lambda worker, reason: None
    L.remove_orphan_secrets = step("orphans", 0.5)
    L.send_wip_notes = step("notes", 1.5)
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        try:
            mod._reap_main({"CW_CLOUD_NAMESPACE": "cw"}, types.SimpleNamespace(loop=0), lambda s: None, clock=lambda: T0,
                           refresher=f.refresher, kube=L.kube)
        except RuntimeError:
            pass
    lines = [x for x in err.getvalue().splitlines() if "reaper pass line" in x]
    want = dict(S10_STEPS)[raising]
    if len(lines) != 1 or (" " + want) not in lines[0] or (" total=%.1f" % S10_TOTALS[raising]) not in lines[0]:
        raise AssertionError("a raising step lost its seconds: %s" % raising)
    return lines[0]


class TestRaisingSteps(unittest.TestCase):
    def test_s10_each_of_six_steps_raises(self):
        raising_pass_text(self, build_module(), "list")   # the control first
        for name, want in S10_STEPS:
            line = raising_pass_text(self, build_module(), name)
            print("S10: %s raises: %s" % (name, line.split("audit_lines_parsed=", 1)[1]))
            self.assertIn(" " + want, line)
            self.assertIn(" total=%.1f" % S10_TOTALS[name], line)
            print("S10: %s run: total expected %.1f" % (name, S10_TOTALS[name]))
        for name, _ in S10_STEPS:
            old = 'steps["%s"] %s mono() - t0' % (name, "+=" if name == "activity" else "=")
            src = source()
            self.assertEqual(len([x for x in src.split("\n") if x.strip() == old]), 1, old)
            # the timer is read after the call, outside the finally: the finally line is moved out one level, after the try block
            lines = src.split("\n")
            idx = [k for k, l in enumerate(lines) if l.strip() == old]
            k = idx[0]
            self.assertEqual(lines[k - 1].strip(), "finally:")
            self.assertEqual(lines[k - 3].strip(), "try:")   # try / the call / finally / the timer
            tail = 2 if name == "activity" else 1
            body = [x[4:] for x in lines[k - 2:k - 1]] + [x[4:] for x in lines[k:k + tail]]
            lines[k - 3:k + tail] = body   # the call, then the timer after it: no try, no finally
            mutated = "\n".join(lines)
            self.assertNotEqual(mutated, src)   # the mutation applied
            tmp = tempfile.mkdtemp(prefix="s10-")   # the scratch copy lives under /tmp, never in the repository
            with open(os.path.join(tmp, "cloud_worker.py"), "w") as f:
                f.write(mutated)
            mod = types.ModuleType("cloud_worker_s10_" + name)
            mod.__file__ = SOURCE_PATH
            exec(compile(mutated, SOURCE_PATH, "exec"), mod.__dict__)
            mod.json = CountingJson()
            with self.assertRaisesRegex(AssertionError, "^a raising step lost its seconds: %s$" % name):
                raising_pass_text(self, mod, name)
            print("S10: copy with the %s timer after the call fails: a raising step lost its seconds" % name)


class GapClock(StepClock):
    """The fake monotonic clock that adds `gap` seconds at each boundary: the read that starts a step right after the read that ended the step before."""
    def __init__(self, gap):
        StepClock.__init__(self)
        self.gap, self.reads_since_exit, self.gaps = gap, 0, 0

    def exited(self):
        self.reads_since_exit = 1

    def monotonic(self):
        if self.reads_since_exit == 1:
            self.reads_since_exit = 2   # the read that ends the step
        elif self.reads_since_exit == 2:
            self.reads_since_exit = 0   # the read that starts the next step: a boundary
            self.t += self.gap
            self.gaps += 1
        return self.t


def s11_world(test, mod, gap=0.0, raise_second_read=False):
    """The fake world of S8; the fake clock adds `gap` seconds at each boundary of the steps; the 2nd activity read may raise."""
    clock = GapClock(gap)
    cls = mod.Launcher
    f = LineFixture(test, mod)
    mod.time = clock
    L = f.w.L
    L.reap = types.MethodType(cls.reap, L)
    state = {"steps": 0.0}

    def step(seconds, answer=None):
        def call(*a, **k):
            try:
                clock.t += seconds
                state["steps"] += seconds
                return answer
            finally:
                clock.exited()
        return call
    L.kube.list_pods = step(1.0, f.w.pods)
    L.renew_git_tokens = step(2.0, [])
    L._note_vanished_pods = step(0.5)
    L.hard_deadline = lambda pod: T0 + datetime.timedelta(days=1)
    idle = f.w.pods[2]["metadata"]["name"]
    reads = []

    def activity(name):
        reads.append(name)
        try:
            clock.t += 0.2
            state["steps"] += 0.2
            if raise_second_read and len(reads) == 2:
                clock.t += 0.5
                state["steps"] += 0.5
                raise RuntimeError("fake step failure")
            return (False, T0 - datetime.timedelta(days=1)) if name == idle else (True, T0)
        finally:
            clock.exited()
    L.activity = activity
    L.stop = lambda ns, reason: (step(4.0)(), {})[1]
    L.retire_row = lambda worker, reason: None
    L.remove_orphan_secrets = step(0.5)
    L.send_wip_notes = step(1.5)
    state["clock"] = clock
    return f, L, clock, state


def s11_pass(test, f):
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        try:
            f.mod._reap_main({"CW_CLOUD_NAMESPACE": "cw"}, types.SimpleNamespace(loop=0), lambda s: None, clock=lambda: T0,
                             refresher=f.refresher, kube=f.w.L.kube)
        except RuntimeError:
            pass
    return [x for x in err.getvalue().splitlines() if "reaper pass line" in x]


def s11_total(test, mod):
    f, L, clock, state = s11_world(test, mod, gap=0.25, raise_second_read=True)
    lines = s11_pass(test, f)
    want = state["steps"] + 0.25 * state["clock"].gaps
    if len(lines) != 1 or (" total=%.1f" % want) not in lines[0]:
        raise AssertionError("the total left out the seconds between steps")
    return lines[0], want, state["clock"].gaps


def s11_early(test, mod):
    f, L, clock, state = s11_world(test, mod)
    first = s11_pass(test, f)
    assert len(first) == 1 and STEP_LINE in first[0], first   # a pass that completed: its numbers are on the launcher
    def boom():
        raise RuntimeError("fake clock failure")
    L.clock = boom
    lines = s11_pass(test, f)
    if len(lines) != 1 or " steps " in lines[0] or any(n in lines[0] for n in ("list=1.0", "renew=2.0", "total=10.1")):
        raise AssertionError("the steps of the pass before were shown")
    return lines[0]


class TestTotalWithGaps(unittest.TestCase):
    def test_s11_total_and_early_raise(self):
        line, want, nb = s11_total(self, build_module())   # the controls first
        early = s11_early(self, build_module())
        self.assertIn(" total=%.1f" % want, line)
        self.assertNotIn(" steps ", early)
        print("S11: control: raise in the 2nd activity read, %d boundaries of 0.25: expected total %.2f; line: %s" % (nb, want, line.split("audit_lines_parsed=", 1)[1]))
        print("S11: control: early raise line: %s" % early.split("audit_lines_parsed=", 1)[1].strip() + " (no step part)")
        old = 'st["total"] if st["total"] is not None else time.monotonic() - st["start"]'
        with self.assertRaisesRegex(AssertionError, "^the total left out the seconds between steps$"):
            s11_total(self, build_module((old, "done")))
        print("S11: copy with the total as the sum of the steps fails: the total left out the seconds between steps")
        old = "                L.step_seconds = None   # the pass line shows the steps of THIS pass only\n"
        with self.assertRaisesRegex(AssertionError, "^the steps of the pass before were shown$"):
            s11_early(self, build_module((old, "                pass\n")))
        print("S11: copy that does not clear the dictionary fails: the steps of the pass before were shown")


class DipClock(StepClock):
    """The fake monotonic clock of S8 that returns a value `back` seconds SMALLER at its `at`-th read only (the 2nd read ends the pod list)."""
    def __init__(self, at=2, back=5.0):
        StepClock.__init__(self)
        self.n, self.at, self.back = 0, at, back

    def monotonic(self):
        self.n += 1
        return self.t - self.back if self.n == self.at else self.t


def s12_numbers(test, mod):
    clock = DipClock()
    real = StepClock
    try:
        globals()["StepClock"] = lambda: clock   # the S8 world builds its clock through StepClock()
        text = step_pass_text(test, mod)
    finally:
        globals()["StepClock"] = real
    line = [x for x in text.splitlines() if "reaper pass line" in x]
    assert len(line) == 1, line
    import re
    nums = [float(x) for x in re.findall(r"=(-?\d+\.\d)", line[0].split(" steps ", 1)[1])]
    if any(n < 0 for n in nums):
        raise AssertionError("a negative number was printed")
    return line[0], nums


class TestClockStepsBack(unittest.TestCase):
    def test_s12_clock_steps_back(self):
        line, nums = s12_numbers(self, build_module())   # the control first
        self.assertEqual(len(nums), 8)
        print("S12: control: a read 5 seconds smaller at the end of the pod list; %d seconds in the line, min %.1f: %s" % (len(nums), min(nums), line.split(" steps ", 1)[1]))
        with self.assertRaisesRegex(AssertionError, "^a negative number was printed$"):
            s12_numbers(self, build_module(("return max(0.0, float(v))", "return float(v)")))
        print("S12: copy without the clamp fails: a negative number was printed")


BAD_DICTS = (None, {"list": 0.0, "renew": 0.0, "vanished": 0.0, "activity": 0.0, "activity_n": 0, "stops": 0.0, "stops_n": 0, "orphans": 0.0, "notes": 0.0, "total": None},
             {"list": 0.0, "renew": 0.0, "vanished": 0.0, "activity": 0.0, "activity_n": "x", "stops": 0.0, "stops_n": 0, "orphans": 0.0, "notes": 0.0, "total": 1.0, "start": 0.0})


def s13_check(test, mod):
    for d in BAD_DICTS:
        try:
            got = mod.step_text(d)
        except Exception:
            raise AssertionError("step_text raised inside the finally")
        assert got == "", got
    f, L, clock, state = s11_world(test, mod)

    def reap():
        L.step_seconds = BAD_DICTS[2]
        raise RuntimeError("the pass's own error")
    L.reap = reap
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        try:
            mod._reap_main({"CW_CLOUD_NAMESPACE": "cw"}, types.SimpleNamespace(loop=0), lambda s: None, clock=lambda: T0,
                           refresher=f.refresher, kube=L.kube)
        except RuntimeError as e:
            own = str(e)
        except Exception:
            raise AssertionError("step_text raised inside the finally")
        else:
            raise AssertionError("the pass did not end with its own error")
    lines = [x for x in err.getvalue().splitlines() if "reaper pass line" in x]
    assert len(lines) == 1 and " steps " not in lines[0], lines
    return own, lines[0]


class TestBadDictionaries(unittest.TestCase):
    def test_s13_step_text_never_raises(self):
        own, line = s13_check(self, build_module())   # the control first
        self.assertEqual(own, "the pass's own error")
        print("S13: control: 3 of 3 dictionaries (missing, no start with total None, activity_n 'x') give no step part, 0 raised; the pass ended with its own error %r; line: %s" % (own, line.split("audit_lines_parsed=", 1)[1].strip() or "(no step part)"))
        with self.assertRaisesRegex(AssertionError, "^step_text raised inside the finally$"):
            s13_check(self, build_module(("    except Exception:\n        return \"\"\n", "    except ZeroDivisionError:\n        return \"\"\n")))
        print("S13: copy without the guard fails: step_text raised inside the finally")


if __name__ == "__main__":
    unittest.main()
