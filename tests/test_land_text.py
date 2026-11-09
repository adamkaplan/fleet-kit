"""Ask #2320 (S2): the launcher's setup text for a land repository ends with `spire land`, not a pull request; any other repository's text is what it was.
Run: python3 -m unittest tests.test_land_text"""
import argparse
import contextlib
import io
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

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
FORBIDDEN = re.compile(r"PLAN|CLAIMS|review|\bPR\b|pull request|explainer", re.I)


class TestLandText(unittest.TestCase):
    def lines(self, repo, config=None):
        return pod.payload(BRIEF, repo=repo, config=config).splitlines()

    def test_a_land_repository_brief_has_one_spire_land_line_and_no_plan_claims_or_reviewer_line(self):
        lines = self.lines("janus-infra/widget")
        self.assertEqual(len([l for l in lines if "spire land" in l]), 1)
        added = [l for l in lines if l.strip() and l not in pod.payload(BRIEF, repo="janus-infra/widget", config={"land_repos": []}).splitlines()]   # what a land repository gets beyond today's text
        self.assertTrue([l for l in added if "spire land" in l])
        self.assertEqual([l for l in added if FORBIDDEN.search(l)], [])
        self.assertEqual(lines[-1], BRIEF.strip())   # the brief is still last, byte for byte

    def test_the_text_up_to_task_is_the_same_for_other_repositories_control(self):
        other = pod.payload(BRIEF, repo="acme/other")
        self.assertNotIn("spire land", other)
        self.assertEqual(other, cloud_worker.SETUP_PREAMBLE % {"repo": "acme/other", "branch": "b"} + BRIEF)

    def test_the_switch_decides(self):
        self.assertIn("spire land", pod.payload(BRIEF, repo="acme/other", config={"land_repos": ["acme/*"]}))
        self.assertNotIn("spire land", pod.payload(BRIEF, repo="janus-infra/widget", config={"land_repos": []}))

    def test_the_helper(self):
        uses = fpn.sb.uses_spire_land
        self.assertTrue(uses({}, "janus-infra/x"))
        self.assertTrue(uses({}, "JANUS-infra/Widget"))
        self.assertFalse(uses({}, "janus-infra/"))
        self.assertFalse(uses({}, "janus-infra-x/y"))
        self.assertFalse(uses({}, "acme/other"))
        self.assertFalse(uses({"land_repos": []}, "janus-infra/x"))
        self.assertTrue(uses({"land_repos": ["a/b"]}, "a/b"))
        self.assertFalse(uses({"land_repos": ["a/b"]}, "a/c"))
        self.assertFalse(uses({"land_repos": ["acme/*"]}, "janus-infra/widget"))
        self.assertFalse(uses({}, ""))
        for bad in ("janus-infra/*", 5, [1], None):   # a malformed value fails to the default, never to nobody
            self.assertTrue(uses({"land_repos": bad}, "janus-infra/x"), bad)

    def test_setup_text_without_the_marker_is_returned_unchanged(self):
        self.assertEqual(fpn.sb.setup_with_land("no marker", {}, "janus-infra/x"), "no marker")

    def test_a_launch_again_with_a_brief_sends_the_land_line_for_a_land_repo_only(self):
        for repo, expect in (("janus-infra/widget", 1), ("acme/other", 0)):
            module = fpn.CW()
            launch_again = module.cloud_launch_again
            sent = []

            class Pod:
                def prompt(self, session, text, *a, **k):
                    sent.append(text)

            class Cluster:
                def __init__(self, *a, **k):
                    pass
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False

            record = {"worker": "coder5", "state": "starting", "runtime_id": "r1", "url": "https://pod.example", "session": "ses1"}
            args = argparse.Namespace(name="coder5", agent="worker", role="coder", reports_to="lead", issue=None, charter=None, dir=None)
            stubs = dict(VaultRecovery=lambda *a: None, Cluster=Cluster, _pod_known=lambda r: True, _pod_of=lambda r, s: {"ready": True, "phase": "Running"},
                         _cluster_snapshot=lambda *a: None, tunnel=lambda cluster, record: contextlib.nullcontext(object()), remote_v2=lambda *a: Pod(),
                         _read_private=lambda *a: BRIEF, save_record=lambda *a: None, _registry_note=lambda *a, **k: None)
            with tempfile.TemporaryDirectory() as state, contextlib.ExitStack() as stack:
                for key, value in stubs.items():
                    stack.enter_context(mock.patch.object(module, key, value, create=True))
                stack.enter_context(mock.patch.dict(module.H, {"fleet_record": lambda *a: {}}))
                stack.enter_context(mock.patch.object(launch_again, "brief_path", lambda st, n: Path(state) / (n + ".brief")))
                (Path(state) / "coder5.brief").write_text(BRIEF)
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                self.assertEqual(launch_again._finish_starting(args, None, types.SimpleNamespace(config={"call_timeout_seconds": 5}), state, record, repo, "b"), 0)
            self.assertTrue(sent, repo)
            self.assertEqual(len([l for l in "\n".join(sent).splitlines() if "spire land" in l]), expect, repo)

    def test_the_skills_and_the_brief_header_carry_the_rule(self):
        line = fpn.sb.LAND_SETUP_LINE
        self.assertIn("This repository lands with `spire land`", line)
        for path in ("skills/fleet-coordination/SKILL.md", "skills/fleet-review/SKILL.md", "skills/fleet-explainer/SKILL.md", "skills/fleet-coordination/operator-brief-header.txt"):
            text = (ROOT / path).read_text()
            self.assertIn(".spire/tool/bin/spire land", text, path)
            self.assertIn("exit 22 = held for a person: stop and say so", text, path)
            self.assertIn("This repository lands with `spire land`", text, path)   # tied to the launcher's own marker
        self.assertIn(".spire/tool/bin/spire land", line)


if __name__ == "__main__":
    unittest.main()
