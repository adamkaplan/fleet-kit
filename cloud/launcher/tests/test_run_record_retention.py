"""Ask #1743, pull request 1: the retention of the run records, its dry run, its bound and its README line (S2, S3, S4, S5, S8).

Offline: fake directories under a temporary directory only; the real Launcher.daily_maintenance and Launcher.reap. Every mutant is a scratch copy of the
real source: its text asserted to occur once, the edit asserted applied, the control (the unmutated source) run first, the failure text anchored.
"""
import contextlib
import datetime as dt
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, LAUNCHER)
import cloud_worker as real  # noqa: E402
import run_record as real_rr  # noqa: E402

NOW = dt.datetime(2026, 11, 2, 0, 5, 0, tzinfo=dt.timezone.utc)
DAY = 86400


def say(line):
    print(line, file=sys.__stdout__)


class FakeKube:
    def __init__(self, pods=()):
        self.pods = list(pods)

    def list_pods(self):
        return list(self.pods)

    def scrub(self, text, limit=300):
        return str(text)[:limit]

    def add_secrets(self, *a):
        pass

    def close(self):
        pass


def pod(worker):
    return {"metadata": {"name": "pod-" + worker, "annotations": {"cw/worker": worker, "cw/started": real.iso(NOW)}}, "status": {"phase": "Running"}}


def run_id(worker, n=0):
    return "%s-20260301T000000Z-%06x" % (worker, n)


def make_record(root, name, age_days, size=10):
    d = os.path.join(root, name)
    os.makedirs(d)
    for f in ("run.json", "tally.json", "transcript.jsonl"):
        with open(os.path.join(d, f), "w") as h:
            h.write("x" * size)
    t = NOW.timestamp() - age_days * DAY
    os.utime(d, (t, t))
    return d


def tree(root):
    out = {}
    for base, dirs, files in os.walk(root):
        for n in dirs + files:
            p = os.path.join(base, n)
            s = os.lstat(p)
            body = None
            if os.path.isfile(p) and not os.path.islink(p):
                with open(p, "rb") as h:
                    body = h.read()
            out[os.path.relpath(p, root)] = (s.st_size, s.st_mtime, body)
    return out


def launcher(mod, tmp, pods=()):
    env = {"CW_CLOUD_AUDIT": os.path.join(tmp, "audit.jsonl"), "XDG_STATE_HOME": tmp, "CW_RUN_RECORD_DIR": os.path.join(tmp, "runs")}
    return mod.Launcher(FakeKube(pods), env, lambda: NOW, lambda s: None), env["CW_RUN_RECORD_DIR"]


def maintain(mod, rr_mod, tmp, pods, dry_run=False):
    lau, root = launcher(mod, tmp, pods)
    err = io.StringIO()
    with contextlib.redirect_stderr(err), mock.patch.object(mod, "run_record", rr_mod):
        res = lau.daily_maintenance(lau.kube.list_pods(), NOW, dry_run=dry_run)
    return res, err.getvalue().splitlines(), root


def mutant(path, old, new, name):
    with open(path) as f:
        src = f.read()
    assert src.count(old) == 1, "the text to mutate must occur once: %r" % old
    out = src.replace(old, new)
    assert out != src, "the mutation was not applied"
    d = tempfile.mkdtemp()
    p = os.path.join(d, name + ".py")
    with open(p, "w") as f:
        f.write(out)
    spec = importlib.util.spec_from_file_location(name + "_scratch", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fixture(tmp):
    root = os.path.join(tmp, "runs")
    os.makedirs(root)
    live = make_record(root, run_id("alive"), 90)
    inside = make_record(root, run_id("ended", 1), 5)
    outside = make_record(root, run_id("gone", 2), 45)
    return live, inside, outside


def check_live_kept(rr_mod, mod):
    with tempfile.TemporaryDirectory() as tmp:
        live, inside, outside = fixture(tmp)
        maintain(mod, rr_mod, tmp, [pod("alive")])
        if not os.path.isdir(live):
            raise AssertionError("a live worker's record was removed")
        if os.path.isdir(outside):
            raise AssertionError("the old ended worker's record stays")
        if not os.path.isdir(inside):
            raise AssertionError("a record inside the window was removed")


def check_readme(text):
    i = text.find("Retention of the run records")
    if i < 0:
        raise AssertionError("the README has no retention paragraph")
    para = text[i:].split("\n\n")[0]
    if "30 days" not in para:
        raise AssertionError("the retention paragraph gives no number of days")
    return para


class S2Readme(unittest.TestCase):
    def test_the_retention_paragraph_states_the_run_record_line_and_a_copy_without_it_fails(self):
        with open(os.path.join(ROOT, "cloud", "README.md")) as f:
            text = f.read()
        para = check_readme(text)   # the control, first
        self.assertIn("kept while its worker has a pod", para)
        i = text.find("**Retention of the run records")
        self.assertGreater(i, -1)
        with self.assertRaises(AssertionError) as cm:
            check_readme(text[:i])
        self.assertIn("retention", str(cm.exception))
        say("S2 1 of 1 run-record line with its 30 days; the copy without the paragraph fails 1 of 1 with `retention`; control passes 1 of 1")


class S3DryAndApply(unittest.TestCase):
    def test_dry_run_lists_and_changes_nothing_and_apply_removes_the_outside_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, inside, outside = fixture(tmp)
            before = tree(tmp)
            res, lines, root = maintain(real, real_rr, tmp, [pod("alive")], dry_run=True)
            diffs = sum(1 for k in set(before) | set(tree(tmp)) if before.get(k) != tree(tmp).get(k))
            self.assertEqual(diffs, 0)
            self.assertEqual((len(res["removed"]), res["bytes"], res["kept"]), (1, 30, 1))
            self.assertEqual(len([x for x in lines if "would remove run record" in x]), 1)
            self.assertTrue(any("(dry run): 1 run records to remove (30 bytes)" in x for x in lines))
        with tempfile.TemporaryDirectory() as tmp:
            live, inside, outside = fixture(tmp)
            res, lines, root = maintain(real, real_rr, tmp, [pod("alive")])
            gone = [x for x in lines if ": removed run record" in x]
            self.assertEqual(len(gone), 1)
            present = [os.path.isdir(p) for p in (live, inside, outside)]
            self.assertEqual(present, [True, True, False])
        say("S3 dry run lists 1 record (30 bytes) and leaves 0 differences; apply removes 1 of 3 with 1 line per removal, 2 kept present")


class S4Mutant(unittest.TestCase):
    def test_a_live_workers_record_stays_and_the_mutant_fails_with_the_anchored_text(self):
        check_live_kept(real_rr, real)   # the control, first
        mut = mutant(os.path.join(LAUNCHER, "run_record.py"), "            if worker_of(name) in keep_workers:\n", "            if False:\n", "rr_mut4")
        with self.assertRaises(AssertionError) as cm:
            check_live_kept(mut, real)
        self.assertIn("a live worker's rec", str(cm.exception))
        say("S4 2 of 2 expectations hold; control passes 1 of 1; the copy fails 1 of 1 with `a live worker's rec...`; mutation applied")


def foreigners(root):
    os.symlink(os.path.join(root, ".."), os.path.join(root, run_id("link", 7)))
    with open(os.path.join(root, "stray.txt"), "w") as f:
        f.write("s")
    d = make_record(root, run_id("foreign", 8), 90)
    with open(os.path.join(d, "notes.txt"), "w") as f:
        f.write("not ours")
    return [os.path.join(root, run_id("link", 7)), os.path.join(root, "stray.txt"), d]


def check_bound(rr_mod, mod):
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "runs")
        os.makedirs(root)
        for i in range(600):
            make_record(root, run_id("w%d" % i, i), 45, 1)
        keep = foreigners(root)
        res, lines, _ = maintain(mod, rr_mod, tmp, [])
        left = sum(1 for n in os.listdir(root) if os.path.isdir(os.path.join(root, n)) and not os.path.islink(os.path.join(root, n)) and n.startswith("w"))
        if len(res["removed"]) != 500 or left != 100:
            raise AssertionError("the bound: %d removed and %d left" % (len(res["removed"]), left))
        for p in keep:
            if not os.path.lexists(p):
                raise AssertionError("a foreign entry was removed: " + os.path.basename(p))
        return len(res["removed"]), left, len(keep)


class S8Bound(unittest.TestCase):
    def test_500_go_100_stay_and_foreign_entries_are_untouched(self):
        removed, left, foreign = check_bound(real_rr, real)   # the control, first
        self.assertEqual((removed, left, foreign), (500, 100, 3))
        mut = mutant(os.path.join(LAUNCHER, "run_record.py"), "        if len(removed) >= limit:\n", "        if False:\n", "rr_mut8")
        with self.assertRaises(AssertionError) as cm:
            check_bound(mut, real)
        self.assertIn("the bound: 600 removed", str(cm.exception))
        say("S8 500 removed and 100 left, 3 of 3 foreign entries untouched; the unbounded copy removes 600 of 600")


class Counting:
    """Counts the opens and listings that touch the record directory."""
    def __init__(self, root):
        self.root, self.opened, self.listed = root, [], []

    def __enter__(self):
        r, me = self.root, self
        orig_open, orig_list, orig_scan = open, os.listdir, os.scandir

        def under(p):
            try:
                return os.path.abspath(os.fspath(p)).startswith(os.path.abspath(r))
            except TypeError:
                return False

        def o(p, *a, **k):
            if under(p):
                me.opened.append(p)
            return orig_open(p, *a, **k)

        def ls(p="."):
            out = orig_list(p)
            if under(p):
                me.listed.append(len(out))
            return out

        def sc(p="."):
            if under(p):
                me.listed.append(len(orig_list(p)))
            return orig_scan(p)
        self.ps = [mock.patch("builtins.open", o), mock.patch("os.listdir", ls), mock.patch("os.scandir", sc)]
        for p in self.ps:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self.ps:
            p.stop()


def pass_counts(mod, n):
    with tempfile.TemporaryDirectory() as tmp:
        lau, root = launcher(mod, tmp)
        os.makedirs(root)
        for i in range(n):
            make_record(root, run_id("w%d" % i, i), 1, 1)
        lau.maintenance_day = NOW.strftime("%Y-%m-%d")   # the maintenance is not due: the pass alone
        with Counting(root) as c, contextlib.redirect_stderr(io.StringIO()):
            lau.reap()
        return len(c.opened), sum(c.listed)


def check_pass(mod):
    a, b = pass_counts(mod, 50), pass_counts(mod, 5000)
    if a != b or a != (0, 0):
        raise AssertionError("the pass read the records: %s with 50 and %s with 5000 (opened, listed)" % (a, b))
    return a, b


class S5Pass(unittest.TestCase):
    def test_the_pass_reads_no_record_at_50_or_at_5000_and_the_listing_copy_fails(self):
        a, b = check_pass(real)   # the control, first
        self.assertEqual((a, b), ((0, 0), (0, 0)))
        mut = mutant(os.path.join(LAUNCHER, "cloud_worker.py"), "        pods = self.kube.list_pods()\n        # The renewal is the FIRST step",
                     "        pods = self.kube.list_pods()\n        os.listdir(self.recorder.root(None))\n        # The renewal is the FIRST step", "cw_mut5")
        with self.assertRaises(AssertionError) as cm:
            check_pass(mut)
        self.assertIn("the pass read the records", str(cm.exception))
        self.assertIn("(0, 50) with 50", str(cm.exception).replace("%s", ""))
        say("S5 0 other record files opened and 0 directories listed in the 2 passes (50 and 5000 records), equal 2 of 2; control passes 1 of 1; "
            "the listing copy fails 1 of 1 with `the pass read the records` (50 listed with 50, 5000 with 5000)")


def passes_1200(mod):
    """Three passes of the reaper in one UTC day over 1,200 expired records and no running worker, then a fourth."""
    with tempfile.TemporaryDirectory() as tmp:
        lau, root = launcher(mod, tmp)
        os.makedirs(root)
        for i in range(1200):
            make_record(root, run_id("w%d" % i, i), 45, 1)
        removed, left = [], 1200
        for _ in range(3):
            with contextlib.redirect_stderr(io.StringIO()):
                lau.reap()
            now_left = len(os.listdir(root))
            removed.append(left - now_left)
            left = now_left
        if left:
            raise AssertionError("an expired record was left after three passes: %d left" % left)
        with Counting(root) as c, contextlib.redirect_stderr(io.StringIO()):
            lau.reap()
        return removed, left, sum(c.listed), len(os.listdir(root))


class S23Passes(unittest.TestCase):
    def test_three_passes_remove_500_500_200_and_the_fourth_lists_nothing(self):
        removed, left, listed, after = passes_1200(real)   # the control, first
        self.assertEqual((removed, left, listed, after), ([500, 500, 200], 0, 0, 0))
        mut = mutant(os.path.join(LAUNCHER, "cloud_worker.py"), "        if not dry_run and len(names) < run_record.MAINTENANCE_MAX_REMOVALS:\n",
                     "        if not dry_run:\n", "cw_mut23")
        with self.assertRaises(AssertionError) as cm:
            passes_1200(mut)
        self.assertIn("an expired record was left after three passes: 700 left", str(cm.exception))
        say("S23 passes removed %s, %d left, pass 4 listed %d and removed 0; control passes 1 of 1; the day-done copy fails 1 of 1 with `%s`; mutation applied"
            % (removed, left, listed, str(cm.exception)))


def keep_days_run(mod, value):
    with tempfile.TemporaryDirectory() as tmp:
        lau, root = launcher(mod, tmp)
        lau.env["CW_RECORD_KEEP_DAYS"] = value
        os.makedirs(root)
        d = make_record(root, run_id("ended", 3), 10)
        with contextlib.redirect_stderr(io.StringIO()):
            lau.daily_maintenance([], NOW)
        return os.path.isdir(d)


def check_keep_days(mod):
    kept = {v: keep_days_run(mod, v) for v in ("0", "-5", "1")}
    if not (kept["0"] and kept["-5"]):
        raise AssertionError("a value below 1 drained the history: %s" % kept)
    return kept


class S24KeepDays(unittest.TestCase):
    def test_a_value_below_1_falls_back_to_30_days(self):
        kept = check_keep_days(real)   # the control, first
        self.assertEqual(kept, {"0": True, "-5": True, "1": False})
        mut = mutant(os.path.join(LAUNCHER, "cloud_worker.py"), "                days = run_record.KEEP_DAYS   #", "                days = 1   #", "cw_mut24")
        with self.assertRaises(AssertionError) as cm:
            check_keep_days(mut)
        self.assertIn("a value below 1 drained the history", str(cm.exception))
        say("S24 the 10-day record kept with 0 (%s) and -5 (%s), removed with 1 (kept %s); control passes 1 of 1; the max(days, 1) copy fails 1 of 1 with `a value below 1 drained the history`; mutation applied"
            % (kept["0"], kept["-5"], kept["1"]))


def pass_order(mod):
    """One pass of the fake world of S5 with the maintenance due; the names of the calls, in order."""
    with tempfile.TemporaryDirectory() as tmp:
        lau, root = launcher(mod, tmp)
        os.makedirs(root)
        make_record(root, run_id("w0", 0), 45, 1)
        calls = []
        r, m = lau.renew_git_tokens, lau.daily_maintenance
        lau.renew_git_tokens = lambda *a, **k: (calls.append("renewal"), r(*a, **k))[1]
        lau.daily_maintenance = lambda *a, **k: (calls.append("maintenance"), m(*a, **k))[1]
        with contextlib.redirect_stderr(io.StringIO()):
            lau.reap()
    if calls != ["renewal", "maintenance"]:
        raise AssertionError("the maintenance ran before the renewal: %s" % calls)
    return calls


class S27Order(unittest.TestCase):
    def test_the_renewal_is_recorded_before_the_maintenance_and_the_copy_fails(self):
        calls = pass_order(real)   # the control, first
        self.assertEqual(calls, ["renewal", "maintenance"])
        src = os.path.join(LAUNCHER, "cloud_worker.py")
        mut = mutant(src, "        renewed = self.renew_git_tokens(self._before_hard_limit(pods, now), now)\n        self.daily_maintenance(pods, now)\n",
                     "        self.daily_maintenance(pods, now)\n        renewed = self.renew_git_tokens(self._before_hard_limit(pods, now), now)\n", "cw_mut27")
        with self.assertRaises(AssertionError) as cm:
            pass_order(mut)
        self.assertIn("the maintenance ran before the renewal", str(cm.exception))
        say("S27 recorded %s in 1 of 1 passes; control passes 1 of 1; the copy fails 1 of 1 with `%s`; mutation applied" % (calls, str(cm.exception)))


if __name__ == "__main__":
    unittest.main()
