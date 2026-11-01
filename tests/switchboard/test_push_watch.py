"""Tests of the push watch (ask #1740, bin/push_watch.py) on a fake world of two lanes. Nothing is read from a real state directory or from GitHub."""
import datetime
import importlib.util
import json
import re
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NOW = 1_800_000_000.0
SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40


def load(mutant=False):
    text = (REPO / "bin" / "push_watch.py").read_text()
    if mutant:   # sends every item to every lane
        text = text.replace("tell(lane, text + (", "[tell(l, text, name) for l in ('lane-a', 'lane-b')]; tell(lane, text + (", 1)
        assert "lane-b" in text
    module = types.ModuleType("push_watch_under_test")
    exec(compile(text, "push_watch.py", "exec"), module.__dict__)
    return module


def parse_iso(text):
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


class Audit:
    def __init__(self, path):
        self.path, self.lines = Path(path), []

    def append(self, actor, event, subject=None, detail=None, **extra):
        self.lines.append((event, subject, detail))


class Engine:
    actor = "switchboard"

    def __init__(self, directory, records):
        self.audit = Audit(Path(directory) / "audit.jsonl")
        self.sent, self.cloud_watch = [], {"records": records}

    def send(self, sender, recipient, text, **kw):
        self.sent.append((recipient, text))
        return {"sent": True}


def record(worker, lane, repo="o/r"):
    r = {"worker": worker, "repo": repo}
    if lane:
        r["reports_to"] = lane
    return r


class PushWorld:
    """A fake gh: refs per worker prefix, commits per sha, and a request log."""
    def __init__(self):
        self.refs, self.commits, self.requests, self.fail, self.chain = {}, {}, [], False, [SHA_A, SHA_B, SHA_C]

    def __call__(self, path):
        self.requests.append(path)
        if self.fail:
            raise OSError("down")
        match = re.search(r"matching-refs/heads/(.+)$", path)
        if match:
            return [{"ref": "refs/heads/" + b, "object": {"sha": s}} for b, s in self.refs.items() if b.startswith(match.group(1))]
        if "/compare/" in path:
            prev, tip = path.rsplit("/", 1)[1].split("...")
            chain = self.chain[self.chain.index(prev) + 1:self.chain.index(tip) + 1]
            return {"commits": [{"sha": c, "commit": self.commits[c]["commit"]} for c in chain],
                    "files": [{"filename": "n%d.py" % i, "additions": 1, "deletions": 1} for i in range(3)]}
        return self.commits[path.rsplit("/", 1)[1]]


def commit(subject="fix the thing", body="BODY-SECRET", files=("a.py", "b.py", "c.py", "d.py")):
    return {"commit": {"message": subject + "\n\n" + body, "author": {"email": "x@y.z"}}, "stats": {"additions": 5, "deletions": 2},
            "files": [{"filename": f, "patch": "+token=SECRET-PATCH"} for f in files]}


class Case(unittest.TestCase):
    config = {"interval": 0, "quiet_minutes": 10}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.launcher = str(Path(self.tmp.name) / "launcher.jsonl")
        Path(self.launcher).touch()
        self.pw = load()
        self.world = PushWorld()
        self.world.commits = {SHA_A: commit(), SHA_B: commit("second"), SHA_C: commit("third")}
        self.engine = Engine(self.tmp.name, [record("w-a", "lane-a"), record("w-b", "lane-b")])
        self.ns = types.SimpleNamespace(parse_iso=parse_iso, _one_line=lambda t, n=200: str(t)[:n], parse_bounded=json.loads)

    def run_pass(self, now, module=None):
        return (module or self.pw).run_pass(self.ns, self.engine, now, self.world, self.config, self.launcher)

    def to(self, lane):
        return [t for r, t in self.engine.sent if r == lane]

    def launcher_line(self, **item):
        with open(self.launcher, "a") as handle:
            handle.write(json.dumps(dict(item, ok=True, ts="2027-01-15T08:01:00Z")) + "\n")


class TestPushWatch(Case):
    def test_s2_the_launching_lane_gets_one_item_and_the_other_lane_none(self):
        self.world.refs = {"w-a/wip": SHA_A}
        self.run_pass(NOW)
        self.run_pass(NOW + 1)
        self.assertEqual((len(self.to("lane-a")), len(self.to("lane-b"))), (1, 0))
        text = self.to("lane-a")[0]
        for part in ("w-a", "w-a/wip", SHA_A[:8], "fix the thing", "4 file(s)", "7 line(s)"):
            self.assertIn(part, text)

    def test_s3_a_quiet_item_once_and_each_cancel(self):
        self.world.refs = {"w-a/wip": SHA_A}
        self.run_pass(NOW)
        self.assertEqual(len(self.run_pass(NOW + 599)), 0)
        self.assertEqual(len(self.run_pass(NOW + 600)), 1)
        self.assertEqual(len(self.run_pass(NOW + 1200)), 0)
        self.assertIn("w-a pushed %s at" % SHA_A[:8], self.to("lane-a")[1])
        self.assertIn("has been quiet for 10 minutes", self.to("lane-a")[1])

    def _cancelled(self, **cancel):
        self.setUp()
        self.world.refs = {"w-a/wip": SHA_A}
        self.run_pass(NOW)
        self.launcher_line(worker="w-a", **cancel)
        self.run_pass(NOW + 700)
        return len(self.to("lane-a"))

    def test_s3_a_message_a_stop_and_a_retire_cancel_the_quiet_item(self):
        self.assertEqual([self._cancelled(action="send"), self._cancelled(action="report"), self._cancelled(action="stop")], [1, 1, 1])
        self.setUp()
        self.world.refs = {"w-a/wip": SHA_A}
        self.run_pass(NOW)
        self.engine.cloud_watch = {"records": [record("w-b", "lane-b")]}   # the record is gone: retired
        self.run_pass(NOW + 700)
        self.assertEqual(len(self.to("lane-a")), 1)

    def test_s5_a_later_push_cancels_the_quiet_item(self):
        self.world.refs = {"w-a/wip": SHA_A}
        self.run_pass(NOW)
        self.run_pass(NOW + 500)
        self.world.refs = {"w-a/wip": SHA_B}
        self.run_pass(NOW + 650)   # 650 s after the first push, 150 s after the second
        self.assertEqual([("second" in t) for t in self.to("lane-a")], [False, True])
        self.assertEqual(len(self.run_pass(NOW + 1300)), 1)   # the quiet item of the second sha only
        self.assertIn(SHA_B[:8], self.to("lane-a")[-1])

    def test_s2_two_commits_in_one_pass_are_two_items_for_the_launching_lane_and_none_for_the_other(self):
        self.world.refs = {"w-a/wip": SHA_A, "w-b/wip": SHA_A}
        self.run_pass(NOW)
        self.world.refs = {"w-a/wip": SHA_C}
        self.run_pass(NOW + 60)
        got = self.to("lane-a")
        self.assertEqual(len(got), 3)   # the first sight, then the two new commits
        self.assertIn("second", got[1])
        self.assertIn("third", got[2])
        self.assertEqual(len(self.to("lane-b")), 1)   # its own first sight only
        self.run_pass(NOW + 120)
        self.assertEqual(len(self.to("lane-a")), 3)

    def test_the_task_branch_of_the_record_is_read_by_its_exact_name(self):
        self.engine.cloud_watch = {"records": [dict(record("w-a", "lane-a"), branch="upkeep/1-x")]}
        self.world.refs = {"upkeep/1-x": SHA_A, "upkeep/1-xy": SHA_B}
        self.run_pass(NOW)
        self.assertEqual(len(self.to("lane-a")), 1)
        self.assertIn("upkeep/1-x:", self.to("lane-a")[0])
        self.assertEqual(len([p for p in self.world.requests if "matching-refs" in p]), 2)

    def test_s5_the_mutant_that_tells_every_lane_fails_with_the_named_check_and_the_control_passes(self):
        self.world.refs = {"w-a/wip": SHA_A}
        self.run_pass(NOW)
        self.assertEqual((len(self.to("lane-a")), len(self.to("lane-b"))), (1, 0))   # the control
        self.setUp()
        self.world.refs = {"w-a/wip": SHA_A}
        self.run_pass(NOW, load(mutant=True))
        with self.assertRaises(AssertionError) as caught:
            self.assertEqual(len(self.to("lane-b")), 0, "the other lane got an item for a worker it did not launch")
        self.assertIn("the other lane got an item", str(caught.exception))

    def test_s4_the_item_holds_the_subject_counts_and_three_paths_only(self):
        self.world.commits[SHA_A] = commit("s" * 150, files=["f%d.py" % i for i in range(12)])
        self.world.refs = {"w-a/wip": SHA_A}
        self.run_pass(NOW)
        text = self.to("lane-a")[0]
        self.assertIn("12 file(s)", text)
        self.assertEqual(len(re.findall(r"f\d+\.py", text)), 3)
        self.assertIn("s" * 100 + "\"", text)
        for leak in ("BODY-SECRET", "SECRET-PATCH", "x@y.z"):
            self.assertNotIn(leak, text)

    def test_u1_a_push_delivery_makes_no_item_there_is_no_push_handler(self):
        text = (REPO / "bin" / "fleet-switchboard").read_text()
        block = text[text.index("WEBHOOK_HANDLERS"):][:1500]
        self.assertNotIn('"push"', block)
        self.assertEqual(self.run_pass(NOW), [])   # nothing pushed, nothing told

    def test_u2_one_request_per_live_worker_a_failed_read_audited_once_and_a_dead_worker_not_read(self):
        self.world.refs = {"w-a/wip": SHA_A}
        self.world.fail = True
        self.run_pass(NOW)
        self.run_pass(NOW + 1)
        self.assertEqual(len(self.world.requests), 4)
        self.assertEqual(len([l for l in self.engine.audit.lines if l[0] == "push_watch.read_failed"]), 2)   # one per worker, not per pass
        self.world.fail, self.world.requests = False, []
        self.launcher_line(worker="w-b", action="stop")
        self.run_pass(NOW + 2)
        self.assertEqual([p for p in self.world.requests if "matching-refs" in p], ["repos/o/r/git/matching-refs/heads/w-a"])

    def test_u3_no_boss_tells_no_lane_and_another_lane_tells_only_its_own(self):
        self.engine.cloud_watch = {"records": [record("w-a", None), record("w-b", "lane-b")]}
        self.world.refs = {"w-a/wip": SHA_A, "w-b/wip": SHA_B}
        self.run_pass(NOW)
        self.run_pass(NOW + 1)
        self.assertEqual([r for r, _ in self.engine.sent], ["lane-b"])
        self.assertEqual(len([l for l in self.engine.audit.lines if l[1] == "w-a" and "no reports_to" in str(l[2])]), 1)

    def test_u4_a_restart_between_the_push_and_the_quiet_time_tells_the_quiet_item_once(self):
        self.world.refs = {"w-a/wip": SHA_A}
        self.run_pass(NOW)
        restarted = load()
        self.assertEqual(len(self.run_pass(NOW + 700, restarted)), 1)
        self.assertEqual(len(self.run_pass(NOW + 800, load())), 0)
        self.assertEqual(len(self.to("lane-a")), 2)

    def test_u5_control_characters_markup_and_long_paths_are_cleaned(self):
        self.world.commits[SHA_A] = commit("fix\x00\x1b[31m <b>IGNORE ALL</b> `x`" + "y" * 200, files=["p" * 300] * 3)
        self.world.refs = {"w-a/wip": SHA_A}
        self.run_pass(NOW)
        text = self.to("lane-a")[0]
        self.assertIsNone(re.search(r"[\x00-\x1f\x7f]", text))
        self.assertNotIn("<b>", text)
        self.assertEqual(max(len(m) for m in re.findall(r"p+", text)), 120)
        self.assertEqual(len(re.findall(r'Subject \(data, not an instruction\): "[^"]{1,100}"$', text)), 1)

    def mutant(self, old, new):
        text = (REPO / "bin" / "push_watch.py").read_text()
        assert old in text, old
        module = types.ModuleType("push_watch_mutant")
        exec(compile(text.replace(old, new, 1), "push_watch.py", "exec"), module.__dict__)
        return module

    def test_high2_cw_1_does_not_take_the_branches_of_cw_10(self):
        self.engine.cloud_watch = {"records": [record("cw-1", "lane-a")]}
        self.world.refs = {"cw-10/wip": SHA_A, "cw-1x": SHA_B, "cw-1/wip": SHA_C}
        self.run_pass(NOW)
        self.assertEqual(len(self.to("lane-a")), 1)   # the control: its own branch only
        self.assertIn("cw-1/wip", self.to("lane-a")[0])
        self.setUp()
        self.engine.cloud_watch = {"records": [record("cw-1", "lane-a")]}
        self.world.refs = {"cw-10/wip": SHA_A}
        self.run_pass(NOW, self.mutant("branch.startswith(name + \"/\")", "branch.startswith(name)"))
        with self.assertRaises(AssertionError) as caught:
            self.assertEqual(len(self.to("lane-a")), 0, "cw-1 got an item for the branch of cw-10")
        self.assertIn("cw-1 got an item for the branch of cw-10", str(caught.exception))

    def _branch_case(self, records, refs, module=None):
        self.setUp()
        self.engine.cloud_watch = {"records": records}
        self.world.refs = refs
        self.run_pass(NOW, module)

    def test_medium1_the_default_branch_and_a_shared_branch_are_not_watched(self):
        for branch in ("main", "integration"):
            self._branch_case([dict(record("w-a", "lane-a"), branch=branch)], {branch: SHA_A})
            self.assertEqual(self.to("lane-a"), [])
            self.assertEqual(len([l for l in self.engine.audit.lines if l[0] == "push_watch.branch_denied"]), 1)
        self._branch_case([dict(record("w-a", "lane-a"), branch="dev", default_branch="dev")], {"dev": SHA_A})
        self.assertEqual(self.to("lane-a"), [])
        self._branch_case([dict(record("w-a", "lane-a"), branch="x/1"), record("w-b", "lane-b")], {"x/1": SHA_A})
        self.assertEqual(len(self.to("lane-a")), 1)   # control: an own branch
        both = [dict(record("w-a", "lane-a"), branch="x/1"), dict(record("w-b", "lane-b"), branch="x/1")]
        self._branch_case(both, {"x/1": SHA_A})
        self.assertEqual(self.engine.sent, [])   # neither gets it
        self._branch_case(both, {"x/1": SHA_A}, self.mutant("> 1)", "> 5)"))
        with self.assertRaises(AssertionError) as caught:
            self.assertEqual(self.engine.sent, [], "a branch two workers name was watched")
        self.assertIn("a branch two workers name was watched", str(caught.exception))
        self._branch_case([dict(record("w-a", "lane-a"), branch="main")], {"main": SHA_A}, self.mutant("task_ in DENIED_BRANCHES or", "False and"))
        with self.assertRaises(AssertionError) as caught:
            self.assertEqual(self.engine.sent, [], "the default branch was watched")
        self.assertIn("the default branch was watched", str(caught.exception))

    def _failing_compare(self, module=None):
        self.setUp()
        self.engine.cloud_watch = {"records": [record("w-a", "lane-a")]}
        self.world.refs = {"w-a/wip": SHA_A}
        self.run_pass(NOW)
        real = self.world.__call__
        self.world.refs = {"w-a/wip": SHA_C}
        calls = []

        def broken(path):
            if "/compare/" in path:
                calls.append(path)
                raise OSError("422 no merge base")
            return real(path)
        self.world = broken
        for i in (1, 2, 3):
            self.run_pass(NOW + 60 * i, module)
        return calls

    def test_medium2_a_failed_compare_falls_back_to_the_tip_once(self):
        calls = self._failing_compare()
        self.assertEqual(len(self.to("lane-a")), 2)   # the first sight, then the tip alone
        self.assertEqual(len(calls), 1)
        self.assertEqual(len([l for l in self.engine.audit.lines if l[0] == "push_watch.compare_failed"]), 1)
        self._failing_compare(self.mutant('return raw_gh("repos/%s/commits/%s"', 'raise RuntimeError("x"); return raw_gh("repos/%s/commits/%s"'))
        with self.assertRaises(AssertionError) as caught:
            self.assertEqual(len(self.to("lane-a")), 2, "no fallback: the failed compare is retried each pass")
        self.assertIn("no fallback", str(caught.exception))

    def test_medium3_the_cap_rotates_and_a_rate_limit_stops_the_pass(self):
        recs = [record("w%02d" % i, "lane-a") for i in range(30)]
        self.engine.cloud_watch = {"records": recs}
        self.world.refs = {"w%02d/wip" % i: SHA_A for i in range(30)}
        self.run_pass(NOW)
        first = [p for p in self.world.requests if "matching-refs" in p]
        self.assertLessEqual(len(first), self.pw.PASS_WORKERS_MAX)
        self.run_pass(NOW + 1)
        self.assertEqual(len({p for p in self.world.requests if "matching-refs" in p}), 30)
        self.setUp()
        self.engine.cloud_watch = {"records": recs}
        self.world.refs = {"w%02d/wip" % i: SHA_A for i in range(30)}
        self.run_pass(NOW, self.mutant("PASS_WORKERS_MAX = 20", "PASS_WORKERS_MAX = 99"))
        with self.assertRaises(AssertionError) as caught:
            self.assertLessEqual(len([p for p in self.world.requests if "matching-refs" in p]), 20, "more than the cap was read")
        self.assertIn("more than the cap was read", str(caught.exception))

    def _limited(self, module=None):
        self.setUp()
        self.requests = []

        def limited(path):
            self.requests.append(path)
            raise OSError("HTTP 403 rate limit exceeded")
        self.world = limited
        self.run_pass(NOW, module)

    def test_medium3_a_rate_limit_stops_the_pass_after_the_first_request(self):
        self._limited()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len([l for l in self.engine.audit.lines if l[0] == "push_watch.rate_limited"]), 1)
        self._limited(self.mutant("limited.append(1)", "pass"))
        with self.assertRaises(AssertionError) as caught:
            self.assertEqual(len(self.requests), 1, "the pass went on after the rate limit")
        self.assertIn("went on after the rate limit", str(caught.exception))

    def test_low_no_mention_no_quote_and_a_failed_send_is_not_told(self):
        self.world.commits[SHA_A] = commit('hi @everyone "x" end https://e.com/@u')
        self.world.refs = {"w-a/wip": SHA_A}
        self.run_pass(NOW)
        text = self.to("lane-a")[0]
        self.assertNotIn("@", text)
        self.assertEqual(text.count('"'), 2)
        self.assertIn("https://e.com/u", text)
        self.setUp()
        self.world.refs = {"w-a/wip": SHA_A}
        ok = self.engine.send
        self.engine.send = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
        self.run_pass(NOW)
        self.engine.send = ok
        self.run_pass(NOW + 1)
        self.assertEqual(len(self.to("lane-a")), 1)   # retried and told once
        self.setUp()
        self.world.refs = {"w-a/wip": SHA_A}
        self.engine.send = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
        self.run_pass(NOW, self.mutant("                    unsent = True\n", "                    unsent = False\n"))
        self.engine.send = ok
        self.run_pass(NOW + 1)
        with self.assertRaises(AssertionError) as caught:
            self.assertEqual(len(self.to("lane-a")), 1, "a failed send was marked told")
        self.assertIn("a failed send was marked told", str(caught.exception))

    def _fails(self, check, text):
        with self.assertRaises(AssertionError) as caught:
            check()
        self.assertIn(text, str(caught.exception))

    def test_fix3_a_corrupt_or_missing_state_starts_empty_and_audits_once(self):
        self.world.refs = {"w-a/wip": SHA_A}
        state = Path(self.tmp.name) / "push-watch.json"
        state.write_text("{not json")
        self.run_pass(NOW)
        self.assertEqual(len(self.to("lane-a")), 1)
        self.assertEqual(json.loads(state.read_text())["workers"]["w-a"]["branches"]["w-a/wip"]["sha"], SHA_A)
        state.write_text("[" * 5000)   # too deep: refused, an empty state, no raise
        self.run_pass(NOW + 1)
        self.assertEqual(len(self.to("lane-a")), 2)

    def test_fix3_pc8_a_branch_of_another_recorded_worker_is_denied_and_audited_once(self):
        recs = [dict(record("cw-1", "lane-a"), branch="cw-2/wip"), record("cw-2", "lane-b")]
        self._branch_case(recs, {"cw-2/wip": SHA_A})
        self.assertEqual(self.to("lane-a"), [])
        self.assertEqual(len(self.to("lane-b")), 1)   # cw-2 still gets its own push
        self.run_pass(NOW + 1)
        self.assertEqual(len([l for l in self.engine.audit.lines if l[0] == "push_watch.branch_denied"]), 1)
        self._branch_case([dict(record("cw-1", "lane-a"), branch="cw-2"), record("cw-2", "lane-b")], {"cw-2": SHA_A})
        self.assertEqual(self.to("lane-a"), [])
        self._branch_case(recs, {"cw-2/wip": SHA_A}, self.mutant('any(o != name and', 'False and any(o != name and'))
        self._fails(lambda: self.assertEqual(self.to("lane-a"), [], "cw-1 received the pushes of cw-2"), "cw-1 received the pushes of cw-2")
        self._branch_case([dict(record("cw-1", "lane-a"), branch="own/x"), record("cw-2", "lane-b")], {"own/x": SHA_A}, self.mutant('any(o != name and', 'False and any(o != name and'))
        self.assertEqual(len(self.to("lane-a")), 1)   # control: the unmutated rule allows an own branch, as does the mutant

    def _quiet_send(self, module=None):
        self.setUp()
        self.world.refs = {"w-a/wip": SHA_A}
        self.run_pass(NOW, module)
        ok = self.engine.send
        self.engine.send = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
        self.run_pass(NOW + 700, module)
        self.engine.send = ok
        self.run_pass(NOW + 800, module)
        self.run_pass(NOW + 900, module)   # told exactly once after the success: nothing more
        self.run_pass(NOW + 1000, module)
        return [t for t in self.to("lane-a") if "quiet" in t]

    def test_fix3_pc4_a_failed_quiet_send_is_retried_at_the_next_pass(self):
        self.assertEqual(len(self._quiet_send()), 1)
        mutant = self.mutant('                n_before = len(sent)\n                tell(lane, item_text(name, branch, b["sha"], {}, "quiet"', '                b["quiet"] = True\n                n_before = len(sent)\n                tell(lane, item_text(name, branch, b["sha"], {}, "quiet"')
        self._fails(lambda: self.assertEqual(len(self._quiet_send(mutant)), 1, "the quiet item was marked before a failed send"), "the quiet item was marked before a failed send")

    def _both_fail(self, module=None):
        self.setUp()
        self.engine.cloud_watch = {"records": [record("w-a", "lane-a")]}
        self.world.refs = {"w-a/wip": SHA_A}
        self.run_pass(NOW)
        real = self.world.__call__
        self.world.refs = {"w-a/wip": SHA_C}
        log = []

        def broken(path):
            if "/compare/" in path or path.endswith("/commits/" + SHA_C):
                log.append(path)
                raise OSError("422 gone")
            return real(path)
        self.world = broken
        for i in (1, 2, 3):
            self.run_pass(NOW + 60 * i, module)
        return log

    def test_fix3_pc2_both_reads_failing_advances_the_sha_and_audits_once(self):
        log = self._both_fail()
        self.assertEqual(len(log), 2)   # the compare and the tip, in the first pass only
        self.assertEqual(len(self.to("lane-a")), 1)   # the first sight only: no item for the failed sha
        self.assertEqual(len([l for l in self.engine.audit.lines if l[0] == "push_watch.compare_failed"]), 1)
        self.assertEqual(len([l for l in self.engine.audit.lines if l[0] == "push_watch.read_failed"]), 0)
        mutant = self.mutant('if cmpfail and not limited:', 'if False:')
        self._fails(lambda: self.assertEqual(len(self._both_fail(mutant)), 2, "the failed sha was read again at the next pass"), "the failed sha was read again at the next pass")

    def _rotation(self, module=None):
        self.setUp()
        recs = [record("w%d" % i, "lane-a") for i in range(5)]
        self.engine.cloud_watch = {"records": recs}
        self.world.refs = {"w%d/wip" % i: SHA_A for i in range(5)}
        real = self.world.__call__
        self.seen, self.hit = [], False

        def limited(path):
            self.seen.append(path)
            if path.endswith("heads/w2") and not self.hit:
                self.hit = True
                raise OSError("HTTP 403 rate limit exceeded")
            return real(path)
        self.world = limited
        self.run_pass(NOW, module)
        del self.seen[:]
        self.run_pass(NOW + 1, module)
        return [p.split("heads/")[1] for p in self.seen if "matching-refs" in p]

    def test_fix3_the_rate_limit_stop_keeps_the_cursor_at_the_first_unreached_worker(self):
        self.assertEqual(self._rotation()[:3], ["w2", "w3", "w4"])
        mutant = self.mutant('state["cursor"] = live.index(current)', 'pass')
        self._fails(lambda: self.assertEqual(self._rotation(mutant)[:3], ["w2", "w3", "w4"], "the workers after the stop were not read first"), "the workers after the stop were not read first")


    def _load_case(self, text, module=None, reader=None):
        self.setUp()
        if reader:
            self.ns.parse_bounded = reader
        self.world.refs = {"w-a/wip": SHA_A}
        state = Path(self.tmp.name) / "push-watch.json"
        state.write_text(text)
        self.run_pass(NOW, module)
        return [l for l in self.engine.audit.lines if l[0] == "push_watch.load_failed"]

    def test_fix3_load_corrupt_and_too_deep_give_one_audit_line_and_a_programming_error_propagates(self):
        deep = lambda t: (_ for _ in ()).throw(ValueError("nested too deep")) if t.startswith("[[") else json.loads(t)
        self.assertEqual(len(self._load_case("{not json")), 1)
        self.assertEqual(len(self.to("lane-a")), 1)   # an empty state: audited once, the pass went on
        self.assertEqual(len(self._load_case("[[[[", reader=deep)), 1)
        self.assertEqual(len(self.to("lane-a")), 1)
        boom = lambda t: (_ for _ in ()).throw(RuntimeError("bug"))
        with self.assertRaises(RuntimeError):
            self._load_case("{}", reader=boom)
        broad = self.mutant("except (OSError, ValueError):   # a corrupt", "except Exception:   # a corrupt")
        self._fails(lambda: self._load_case("{}", broad, boom) and None or (_ for _ in ()).throw(AssertionError("a RuntimeError of the reader was swallowed as an empty state")), "a RuntimeError of the reader was swallowed as an empty state")

    def _launcher_read(self, lines, reader=None, module=None):
        self.setUp()
        with open(self.launcher, "w") as handle:
            handle.write("".join(l + "\n" for l in lines))
        if reader:
            self.ns.parse_bounded = reader
        state = {"workers": {}, "offset": None, "stopped": {}, "reported": {}}
        (module or self.pw)._read_launcher(self.ns, self.launcher, state, parse_iso)
        return state

    def _good(self, worker, action):
        return json.dumps({"worker": worker, "action": action, "ok": True, "ts": "2027-01-15T08:01:00Z"})

    def test_launcher_corrupt_line_between_good_lines_is_skipped_and_the_pass_goes_on(self):
        state = self._launcher_read([self._good("w-a", "stop"), "{not json", self._good("w-b", "report")])
        self.assertEqual(sorted(state["stopped"]), ["w-a"])
        self.assertEqual(sorted(state["reported"]), ["w-b"])
        self.run_pass(NOW)   # the whole pass over the same file raises nothing

    def test_launcher_too_deep_line_is_skipped_the_same_way(self):
        deep = lambda t: (_ for _ in ()).throw(ValueError("nested too deep")) if t.startswith("[[") else json.loads(t)
        state = self._launcher_read([self._good("w-a", "stop"), "[[[[", self._good("w-b", "report")], reader=deep)
        self.assertEqual(sorted(state["stopped"]), ["w-a"])
        self.assertEqual(sorted(state["reported"]), ["w-b"])

    def test_launcher_programming_error_propagates_and_the_broad_except_mutant_fails(self):
        boom = lambda t: (_ for _ in ()).throw(RuntimeError("bug"))
        with self.assertRaises(RuntimeError):
            self._launcher_read([self._good("w-a", "stop")], reader=boom)
        text = (REPO / "bin" / "push_watch.py").read_text()
        self.assertEqual(text.count("        except ValueError:\n            continue"), 1)
        mutant = types.ModuleType("push_watch_mutant")
        exec(compile(text.replace("        except ValueError:\n            continue", "        except Exception:\n            continue"), "push_watch.py", "exec"), mutant.__dict__)
        self._fails(lambda: self._launcher_read([self._good("w-a", "stop")], reader=boom, module=mutant) and self.fail(
            "a RuntimeError of the launcher reader was swallowed as a corrupt line"),
            "a RuntimeError of the launcher reader was swallowed as a corrupt line")

class TestWriteOnce(Case):
    def _replaces(self, module):
        self.world.refs = {"w-a/wip": SHA_A}
        self.engine.cloud_watch = {"records": [record("w-a", "lane-a")]}
        real, calls = module.os.replace, []
        def counted(src, dst):
            calls.append(str(dst))
            return real(src, dst)
        module.os.replace = counted
        try:
            for i in range(40):
                self.run_pass(NOW + i, module)
        finally:
            module.os.replace = real
        return len([c for c in calls if c.endswith("push-watch.json")])

    def test_fix2_idle_passes_write_the_state_file_at_most_twice(self):
        self.assertLessEqual(self._replaces(self.pw), 2)

    def test_fix2_the_unconditional_save_mutant_fails(self):
        text = (REPO / "bin" / "push_watch.py").read_text()
        guard = "            if handle.read(len(text) + 1) == text:"
        self.assertIn(guard, text)
        mutant = types.ModuleType("push_watch_mutant")
        exec(compile(text.replace(guard, "            if False:"), "push_watch.py", "exec"), mutant.__dict__)
        with self.assertRaises(AssertionError) as caught:
            self.assertLessEqual(self._replaces(mutant), 2, "the state file was rewritten on idle passes")
        self.assertIn("the state file was rewritten on idle passes", str(caught.exception))


class TestTickWithoutConfig(unittest.TestCase):
    """#1740: push_watch_tick reads the configuration defensively."""

    def _tick(self, old=False):
        import importlib.machinery
        path = str(REPO / "bin" / "fleet-switchboard")
        text = Path(path).read_text()
        if old:
            text = text.replace('(getattr(engine, "config", None) or {}).get("push_watch"))', 'engine.config.get("push_watch"))')
            assert 'engine.config.get("push_watch"))' in text
        module = types.ModuleType("sb_push_tick")
        module.__file__ = path
        exec(compile(text, path, "exec"), module.__dict__)
        with tempfile.TemporaryDirectory() as tmp:
            audit = types.SimpleNamespace(append=lambda *a: lines.append(a))
            lines = []
            engine = types.SimpleNamespace(audit=audit, actor="daemon", now=lambda: NOW)
            result = module.push_watch_tick(engine, None)
            self.assertEqual((result, lines, list(Path(tmp).iterdir())), ([], [], []))

    def test_fix1_an_engine_without_config_runs_the_pass_with_no_audit_line(self):
        self._tick()

    def test_fix1_the_old_expression_fails_the_check(self):
        with self.assertRaises(AttributeError) as caught:
            self._tick(old=True)
        self.assertIn("has no attribute 'config'", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
