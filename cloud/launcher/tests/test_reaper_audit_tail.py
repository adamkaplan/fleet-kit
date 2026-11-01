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


class TestPins(unittest.TestCase):
    OLD_LINES = ["            return self._renew_locked(pods, now)", '        return last_audit_line(self.audit_path, "renew-git-token", rid)']

    def test_s7_two_removed_lines(self):
        p = stub_support.start(self, ["git", "diff", "-U0", TIP, "--", "cloud/launcher/cloud_worker.py", "cloud/launcher/tests"], cwd=ROOT,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        out = p.communicate()[0].decode(errors="replace")
        if p.returncode != 0:
            self.skipTest("the integration tip is not in this checkout")
        removed = [x[1:] for x in out.splitlines() if x.startswith("-") and not x.startswith("---")]
        print("S7: %d removed lines" % len(removed))
        self.assertEqual(sorted(removed), sorted(self.OLD_LINES))


if __name__ == "__main__":
    unittest.main()
