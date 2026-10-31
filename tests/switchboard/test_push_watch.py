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


class World:
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
        self.world = World()
        self.world.commits = {SHA_A: commit(), SHA_B: commit("second"), SHA_C: commit("third")}
        self.engine = Engine(self.tmp.name, [record("w-a", "lane-a"), record("w-b", "lane-b")])
        self.ns = types.SimpleNamespace(parse_iso=parse_iso, _one_line=lambda t, n=200: str(t)[:n])

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


if __name__ == "__main__":
    unittest.main()
