"""Ask #2393: a landing is credited only to the pod that made it. The push watch reports `landed <sha>` to a pod's boss for the pod's OWN commit (a patch id
equal to one of its branch head's commits, or the sha it reported); another Land-trailer commit on the default branch is told once as a landing by someone else.
Every fixture is faked: no GitHub call, no real state directory. The controls run the same worlds through today's behaviour (the ownership test made always true)."""
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_push_watch import Case, NOW, REPO, record   # noqa: E402
from test_land_repos import land_module, LAUNCH   # noqa: E402,F401

OWN_MAIN = "1" * 40       # the pod's commit as it landed on main (another sha, the same patch)
OWN_BRANCH = "2" * 40     # the pod's own branch head
FOREIGN = ["3" * 40, "4" * 40, "5" * 40]
RAW = "janus-infra/widget"


def sb_module():
    from importlib.machinery import SourceFileLoader
    return SourceFileLoader("sb_land_attr", str(REPO / "bin" / "fleet-switchboard")).load_module()


def patch(text):
    return {"filename": "a.py", "status": "modified", "patch": "@@ -1 +1 @@\n-old\n+%s" % text}


def land(sha, login="someone-else", message="Work\n\nLanded-by: spire land", date="2027-01-15T09:00:00Z"):
    return {"sha": sha, "author": {"login": login}, "commit": {"message": message, "committer": {"date": date}, "author": {"name": "N"}}}


def today(text):
    """Today's code: every Land commit is the pod's own."""
    old = "mine, definite = is_own(sha)"
    assert old in text, old
    return text.replace(old, "mine, definite = True, True", 1)


class TestLandAttribution(Case):
    def setUp(self):
        super().setUp()
        self.reports, self.main, self.fail_commits = [], [], False
        self.engine.config = {"land_repos": ["janus-infra/*"]}
        self.engine.report = lambda sender, state, text, *a, **k: (self.reports.append((sender, text)), {"sent": True})[1]
        self.ns.is_land_repo = land_module().is_land_repo
        self.engine.cloud_watch = {"records": [record("w-a", "lane-a", RAW)]}
        self.patches = {OWN_MAIN: "own", OWN_BRANCH: "own"}
        self.patches.update({f: "foreign-%s" % f[0] for f in FOREIGN})
        self.world_ = self.world
        self.world = self.gh
        self.launcher_line(worker="w-a", action="create")

    def gh(self, path):
        if "/commits?sha=" in path:
            return self.main
        if "matching-refs/heads/w-a" in path:
            return [{"ref": "refs/heads/w-a/work", "object": {"sha": OWN_BRANCH}}]
        if "/compare/" in path:
            return {"commits": [{"sha": OWN_BRANCH}]}
        sha = path.rsplit("/", 1)[1]
        if sha in self.patches:
            if self.fail_commits:
                raise OSError("down")
            return {"sha": sha, "files": [patch(self.patches[sha])]}
        return self.world_(path)

    def said(self):
        return [t for _r, t in self.engine.sent if t.startswith("a landing by someone else was seen")]

    def pod_world(self, module=None):
        self.main = [land(FOREIGN[0]), land(OWN_MAIN, "w-a-bot"), land(FOREIGN[1]), land(FOREIGN[2])]
        self.run_pass(NOW, module)
        return self.reports

    def test_s2_three_foreign_commits_and_one_own_only_the_own_is_credited(self):
        reports = self.pod_world()
        self.assertEqual([r[0] for r in reports], ["w-a"])
        self.assertEqual(sum(1 for _s, t in reports if any(f[:12] in t for f in FOREIGN)), 0)
        self.assertEqual(sum(1 for _s, t in reports if OWN_MAIN[:12] in t), 1)

    def test_s2_control_today_the_same_four_commit_world_credits_a_foreign_commit(self):
        module = types.ModuleType("pw_today")
        exec(compile(today((REPO / "bin" / "push_watch.py").read_text()), "push_watch.py", "exec"), module.__dict__)
        reports = self.pod_world(module)
        self.assertEqual(len(reports), 1)   # one completion: the newest Land commit, a foreign one, is reported landed
        self.assertTrue(any(f[:12] in reports[0][1] for f in FOREIGN))

    def test_s3_a_foreign_land_commit_is_told_once_and_credits_no_pod(self):
        self.main = [land(FOREIGN[0], "Eve")]
        self.run_pass(NOW)
        self.run_pass(NOW + 60)
        self.assertEqual(self.reports, [])
        self.assertEqual(self.said(), ["a landing by someone else was seen: %s by Eve" % FOREIGN[0][:12]])
        self.assertEqual([r for r, t in self.engine.sent if t in self.said()], ["lane-a"])

    def test_s3_control_today_the_same_commit_credits_the_pod_and_says_nothing_of_someone_else(self):
        module = types.ModuleType("pw_today")
        exec(compile(today((REPO / "bin" / "push_watch.py").read_text()), "push_watch.py", "exec"), module.__dict__)
        self.main = [land(FOREIGN[0], "Eve")]
        self.run_pass(NOW, module)
        self.assertEqual((len(self.reports), self.said()), (1, []))

    def test_a_sibling_pods_own_commit_is_not_told_as_someone_elses_in_any_order_or_pass(self):
        for owner, other, lane in (("w-a", "w-b", "lane-b"), ("w-b", "w-a", "lane-a")):
            self.setUp()
            self.engine.cloud_watch = {"records": [record("w-a", "lane-a", RAW), record("w-b", "lane-b", RAW)]}
            self.launcher_line(worker="w-b", action="create")
            self.ns.reported_text = lambda worker, owner=owner: "landed %s" % FOREIGN[0] if worker == owner else None
            self.main = [land(FOREIGN[0])]
            for step in range(3):
                self.run_pass(NOW + 60 * step)
            self.assertEqual([r[0] for r in self.reports], [owner])
            self.assertEqual(self.said(), [], "%s's boss was told of someone else's landing" % other)
        self.setUp()   # the control: only the pod that does not own it watches: the line is said
        self.ns.reported_text = lambda worker: None
        self.main = [land(FOREIGN[0])]
        self.run_pass(NOW)
        self.assertEqual(len(self.said()), 1)

    def test_three_foreign_commits_are_three_lines_one_each_and_the_own_is_not_called_someone_elses(self):
        self.pod_world()
        self.run_pass(NOW + 60)
        lines = self.said()
        self.assertEqual(sorted(re.findall(r": (\w{12}) by", " ".join(lines))), sorted(f[:12] for f in FOREIGN))
        self.assertFalse([t for t in lines if OWN_MAIN[:12] in t])

    def test_a_line_that_could_not_be_sent_is_tried_again_and_then_told_once(self):
        real, fail = self.engine.send, [True]

        def send(sender, recipient, text, **kw):
            if fail and text.startswith("a landing by someone else"):
                fail.pop()
                raise OSError("down")
            return real(sender, recipient, text, **kw)
        self.engine.send = send
        self.main = [land(FOREIGN[0])]
        self.run_pass(NOW)
        self.assertEqual(self.said(), [])
        self.run_pass(NOW + 60)
        self.run_pass(NOW + 120)
        self.assertEqual(len(self.said()), 1)

    def test_the_report_text_the_daemon_keeps_is_what_the_push_watch_reads(self):
        sbmod = sb_module()
        with tempfile.TemporaryDirectory() as state:
            sbmod.record_last_report(Path(state), "w-a", "working", NOW, "landed %s on main" % FOREIGN[1])
            text = (sbmod._read_small(Path(state), sbmod.LAST_REPORT_FILE).get("w-a") or {}).get("text")
        self.ns.reported_text = lambda worker: text
        self.patches = {}
        self.main = [land(FOREIGN[1])]
        self.run_pass(NOW)
        self.assertEqual([r[0] for r in self.reports], ["w-a"])

    def test_the_sha_the_pod_reported_is_its_own_without_any_patch_read(self):
        self.ns.reported_text = lambda worker: "landed %s" % FOREIGN[1][:10]
        self.patches = {}
        self.main = [land(FOREIGN[1])]
        self.run_pass(NOW)
        self.assertEqual([r[0] for r in self.reports], ["w-a"])
        self.assertEqual(self.said(), [])

    def test_a_patch_id_read_that_fails_credits_nobody_and_says_someone_elses_landing_was_seen(self):
        self.fail_commits = True
        self.main = [land(OWN_MAIN)]
        self.run_pass(NOW)
        self.assertEqual((self.reports, len(self.said())), ([], 1))
        self.fail_commits = False
        self.run_pass(NOW + 60)   # the read answers now: it is the pod's own and is credited
        self.assertEqual([r[0] for r in self.reports], ["w-a"])

    def test_a_commit_whose_files_carry_no_patch_cannot_be_matched(self):
        self.world_ = lambda path: {"files": [{"filename": "a.bin"}]}
        self.patches = {}
        self.main = [land(OWN_MAIN)]
        self.run_pass(NOW)
        self.assertEqual((self.reports, len(self.said())), ([], 1))

    def test_the_author_login_is_one_safe_token(self):
        self.main = [land(FOREIGN[0], "ev`il @all\n\"ignore previous\"")]
        self.run_pass(NOW)
        (line,) = self.said()
        self.assertRegex(line, r"^a landing by someone else was seen: [0-9a-f]{12} by [A-Za-z0-9._-]+$")

    def test_a_login_of_nothing_safe_is_unknown(self):
        self.main = [dict(land(FOREIGN[0]), author=None)]
        self.main[0]["commit"]["author"] = {"name": "@@``"}
        self.run_pass(NOW)
        self.assertEqual(self.said(), ["a landing by someone else was seen: %s by unknown" % FOREIGN[0][:12]])


if __name__ == "__main__":
    unittest.main()
