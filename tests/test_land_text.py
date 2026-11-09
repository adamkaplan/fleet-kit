"""Ask #2320 (S2): the launcher's setup text for a land repository ends with `spire land`, not a pull request; any other repository's text is what it was.
Run: python3 -m unittest tests.test_land_text"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "switchboard"))
import cloud_worker  # noqa: E402
import test_first_push_notice as fpn  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_pod_header as pod  # noqa: E402

# the pod stub of those helpers lacks the model list the launch reads once (today's launch needs it): serve the launcher's default model
fpn.PodSessionStub.model_list = lambda self: [dict(zip(("providerID", "id"), fpn.sb.CLOUD_DEFAULT_MODEL.split("/", 1)))]

ROOT = Path(__file__).resolve().parent.parent
BRIEF = "Build the thing.\n"
FORBIDDEN = re.compile(r"PLAN|CLAIMS|reviewer", re.I)


def land():
    return fpn.sb.load_module("cloud_worker") and fpn.sb.setup_with_land, fpn.sb.uses_spire_land


class TestLandText(unittest.TestCase):
    def lines(self, repo, config=None):
        return pod.payload(BRIEF, repo=repo, config=config).splitlines()

    def test_a_land_repository_brief_has_one_spire_land_line_and_no_plan_claims_or_reviewer_line(self):
        lines = self.lines("janus-infra/widget")
        self.assertEqual(len([l for l in lines if "spire land" in l]), 1)
        self.assertEqual([l for l in lines if FORBIDDEN.search(l)], [])
        self.assertEqual(lines[-1], BRIEF.strip())   # the brief is still last, byte for byte

    def test_the_text_up_to_task_is_the_same_for_other_repositories_control(self):
        other = pod.payload(BRIEF, repo="acme/other")
        self.assertNotIn("spire land", other)
        self.assertEqual(other, cloud_worker.SETUP_PREAMBLE % {"repo": "acme/other", "branch": "b"} + BRIEF)

    def test_the_switch_decides(self):
        self.assertIn("spire land", pod.payload(BRIEF, repo="acme/other", config={"land_repos": ["acme/*"]}))
        self.assertNotIn("spire land", pod.payload(BRIEF, repo="janus-infra/widget", config={"land_repos": []}))

    def test_the_helper(self):
        _, uses = land()
        self.assertTrue(uses({}, "janus-infra/x"))
        self.assertFalse(uses({"land_repos": []}, "janus-infra/x"))
        self.assertTrue(uses({"land_repos": ["a/b"]}, "a/b"))
        self.assertFalse(uses({"land_repos": ["a/b"]}, "a/c"))
        self.assertFalse(uses({}, ""))

    def test_the_skills_and_the_brief_header_carry_the_rule_once(self):
        for path in ("skills/fleet-coordination/SKILL.md", "skills/fleet-review/SKILL.md", "skills/fleet-explainer/SKILL.md", "skills/fleet-coordination/operator-brief-header.txt"):
            text = (ROOT / path).read_text()
            self.assertEqual(text.count(".spire/tool/bin/spire land"), 1, path)
            self.assertIn("exit 22", text, path)


if __name__ == "__main__":
    unittest.main()
