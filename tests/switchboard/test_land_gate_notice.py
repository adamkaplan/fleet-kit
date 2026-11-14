"""Ask #2517 clause 4 and 5 (S10, S7): when a landing's push of the default branch reaches the repository, the orchestrator of the pod that made it is told once with the short sha
(the push watch's attribution of #2396: a patch id or the sha the pod reported) and the orchestrator of another pod is told nothing; the one sentence about the land token is in the land
text. The notice is the push watch's existing completion report (`done` to the pod's boss): this file proves it for the case of two pods in one repository."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_repos import Case, NOW, SHA, OLD_SHA, land_module, record   # noqa: E402

REPO = Path(__file__).resolve().parents[2]
TEXT_FILES = ("bin/switchboard/land.py", "skills/fleet-coordination/SKILL.md", "skills/fleet-coordination/operator-brief-header.txt", "skills/fleet-review/SKILL.md", "skills/fleet-explainer/SKILL.md")


class TestLandedPushNotice(Case):
    def setUp(self):
        super().setUp()
        self.reports = []
        self.engine.config = {"land_repos": ["acme-org/*"]}
        self.engine.report = lambda sender, state, text, *a, **k: (self.reports.append((sender, state, text)), {"sent": True})[1]
        self.ns.is_land_repo = land_module().is_land_repo
        self.ns.reported_text = lambda worker: "landed %s" % SHA if worker == "w-a" else None   # only lane-a's pod reported this sha
        self.engine.cloud_watch = {"records": [record("w-a", "lane-a", "acme-org/widget"), record("w-b", "lane-b", "acme-org/widget")]}
        gh = self.world.__call__
        self.main = []
        self.world = lambda path: self.main if "/commits?sha=" in path else gh(path)
        self.launcher_line(worker="w-a", action="create")
        self.launcher_line(worker="w-b", action="create")

    @staticmethod
    def land(sha, message="Add it\n\nLanded-by: spire land", date="2027-01-15T09:00:00Z"):
        return {"sha": sha, "commit": {"message": message, "committer": {"date": date}}}

    # The S10 tests below are regression guards of the existing push-watch behaviour (who is told, once, with the short sha); they are not new behaviour of the gate.
    def test_s10_the_pod_that_made_the_landed_commit_is_told_once_with_the_short_sha_and_nobody_else(self):
        self.main = [self.land(SHA)]
        self.run_pass(NOW)
        self.run_pass(NOW + 60)
        self.assertEqual([(r[0], r[1]) for r in self.reports], [("w-a", "done")])   # the count of wrong or repeated notices is 0
        self.assertIn(SHA[:12], self.reports[0][2])

    def test_s10_a_commit_of_neither_pod_tells_no_pod_it_landed(self):
        self.main = [self.land(OLD_SHA)]
        self.run_pass(NOW)
        self.assertEqual(self.reports, [])   # no `done` to anyone
        told = [text for _, text in self.engine.sent]
        self.assertTrue(all("someone else" in t or "landed %s" % OLD_SHA[:12] not in t for t in told), told)   # at most the one line that says another landed, never a credit

    def test_s10_control_when_the_other_pod_reported_that_sha_it_is_the_one_told(self):
        self.ns.reported_text = lambda worker: "landed %s" % OLD_SHA if worker == "w-b" else None
        self.main = [self.land(OLD_SHA)]
        self.run_pass(NOW)
        self.assertEqual([(r[0], r[1]) for r in self.reports], [("w-b", "done")])   # the attribution decides: the check above can see a wrong notice

    def test_s10_the_pod_told_of_its_landing_was_told_at_launch_to_run_spire_land_without_a_land_gate_command(self):
        text = land_module().LAND_SETUP_LINE
        self.assertIn(land_module().LAND_STEP, text)
        self.assertIn("run it DETACHED: `nohup sh -c '.spire/tool/bin/spire land >", text)
        self.assertNotIn("land-gate", text)   # a cloud pod has no fleet-switchboard: the git proxy gates the push
        self.main = [self.land(SHA)]
        self.run_pass(NOW)
        self.assertEqual([(r[0], r[1]) for r in self.reports], [("w-a", "done")])   # and the one told of the landing is the pod that took it


class TestLandTokenSentence(unittest.TestCase):
    def test_s7_every_land_text_carries_the_land_step_and_no_land_gate_command(self):
        for name in TEXT_FILES:
            text = (REPO / name).read_text()
            with self.subTest(file=name):
                self.assertIn("run it DETACHED: `nohup sh -c \\'.spire/tool/bin/spire land >" if name.endswith(".py") else "run it DETACHED: `nohup sh -c '.spire/tool/bin/spire land >", text)
                self.assertNotIn("land-gate take", text)
