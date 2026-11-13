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
ON = {"land_repos": ["acme-org/*"]}   # the path is off until the key names a repository (#2327)
FORBIDDEN = re.compile(r"PLAN|CLAIMS|review|\bPR\b|pull request|explainer", re.I)


class TestLandText(unittest.TestCase):
    def lines(self, repo, config=None):
        return pod.payload(BRIEF, repo=repo, config=config).splitlines()

    def test_a_land_repository_brief_has_one_spire_land_line_and_no_plan_claims_or_reviewer_line(self):
        lines = self.lines("acme-org/widget", ON)
        self.assertEqual(len([l for l in lines if "spire land" in l]), 1)
        added = [l for l in lines if l.strip() and l not in pod.payload(BRIEF, repo="acme-org/widget", config={"land_repos": []}).splitlines()]   # what a land repository gets beyond today's text
        self.assertTrue([l for l in added if "spire land" in l])
        self.assertEqual([l for l in added if FORBIDDEN.search(l)], [])
        self.assertEqual(lines[-1], BRIEF.strip())   # the brief is still last, byte for byte

    def test_the_text_up_to_task_is_the_same_for_other_repositories_control(self):
        other = pod.payload(BRIEF, repo="acme/other")
        self.assertNotIn("spire land", other)
        self.assertEqual(other, cloud_worker.SETUP_PREAMBLE % {"repo": "acme/other", "branch": "b"} + BRIEF)

    def test_the_switch_decides(self):
        self.assertNotIn("spire land", pod.payload(BRIEF, repo="acme-org/widget"), "no key: off")
        self.assertIn("spire land", pod.payload(BRIEF, repo="acme/other", config={"land_repos": ["acme/*"]}))
        self.assertNotIn("spire land", pod.payload(BRIEF, repo="acme-org/widget", config={"land_repos": []}))

    def test_the_helper(self):
        uses = fpn.sb.uses_spire_land
        self.assertFalse(uses({}, "acme-org/x"), "absent key: off")
        self.assertTrue(uses(ON, "acme-org/x"))
        self.assertTrue(uses(ON, "ACME-ORG/Widget"))
        self.assertFalse(uses(ON, "acme-org/"))
        self.assertFalse(uses(ON, "acme-org-x/y"))
        self.assertFalse(uses(ON, "acme/other"))
        self.assertFalse(uses({"land_repos": []}, "acme-org/x"))
        self.assertTrue(uses({"land_repos": ["a/b"]}, "a/b"))
        self.assertFalse(uses({"land_repos": ["a/b"]}, "a/c"))
        self.assertFalse(uses({"land_repos": ["acme/*"]}, "acme-org/widget"))
        self.assertFalse(uses({}, ""))
        for bad in ("acme-org/*", 5, [1], None, ["nowhere"], ["acme-org/x"] * 201):   # a malformed value fails to the default (off)
            self.assertFalse(uses({"land_repos": bad}, "acme-org/x"), bad)

    def test_setup_text_without_the_marker_is_returned_unchanged(self):
        self.assertEqual(fpn.sb.setup_with_land("no marker", {}, "acme-org/x"), "no marker")

    def test_a_launch_again_with_a_brief_sends_the_land_line_for_a_land_repo_only(self):
        for repo, expect in (("acme-org/widget", 1), ("acme/other", 0)):
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
                self.assertEqual(launch_again._finish_starting(args, None, types.SimpleNamespace(config=dict(ON, call_timeout_seconds=5)), state, record, repo, "b"), 0)
            self.assertTrue(sent, repo)
            self.assertEqual(len([l for l in "\n".join(sent).splitlines() if "spire land" in l]), expect, repo)

    OLD_LINE = "- This repository lands with `spire land`: when done, commit, run `.spire/tool/bin/spire land` and report `landed <sha>`, or the exit code and what stopped it; exit 22 = held for a person: stop and say so.\n"   # the launch line before this fix

    def detached_problems(self, text):
        """What is wrong with a launch text's land step: a `spire land` run outside the detached `nohup sh -c '...' &` command, or no detached command, poll or progress report."""
        detached = r"nohup sh -c '\.spire/tool/bin/spire land > /work/land\.log 2>&1;[^']*' > /dev/null 2>&1 &"
        bad = []
        rest = re.sub(detached, "", text)
        bad += [("foreground run", m) for m in re.findall(r"\.spire/tool/bin/spire land", rest)]
        for need in ("sleep 120; tail -n 20 /work/land.log", "land step"):
            if need not in text:
                bad.append(("missing", need))
        if not re.search(detached, text):
            bad.append(("missing", "the detached command"))
        return bad

    def test_the_launch_text_runs_land_detached_and_never_in_the_foreground(self):
        self.assertEqual(self.detached_problems("\n".join(self.lines("acme-org/widget", ON))), [])

    def test_control_the_old_launch_line_through_the_real_path_is_caught(self):
        with mock.patch.object(fpn.sb, "LAND_SETUP_LINE", self.OLD_LINE):
            text = "\n".join(self.lines("acme-org/widget", ON))
        self.assertIn(("foreground run", ".spire/tool/bin/spire land"), self.detached_problems(text))
        self.assertIn(("missing", "the detached command"), self.detached_problems(text))
        self.assertIn(("foreground run", ".spire/tool/bin/spire land"), self.detached_problems(".spire/tool/bin/spire land > /work/land.log 2>&1 &"))   # redirected but not under nohup

    def test_no_text_claims_an_acme_org_default_for_land_repos(self):
        for path in ("docs/switchboard.md", "skills/fleet-coordination/SKILL.md", "skills/fleet-review/SKILL.md", "skills/fleet-explainer/SKILL.md"):
            for line in (ROOT / path).read_text().splitlines():
                if "land_repos" in line or "spire land" in line:
                    self.assertNotRegex(line, r"acme-org/\*`?\s*by default|default `\[\"acme-org", path)
        self.assertEqual(fpn.sb.uses_spire_land({}, "acme-org/widget"), False)   # the code's default: no repository

    def test_a_land_launch_carries_the_rebut_rule_once_and_another_launch_none(self):
        land = "\n".join(self.lines("acme-org/widget", ON))
        self.assertEqual(land.count("--rebut 'ROLE=Pn: why'"), 1)
        self.assertIn("list every rebuttal (finding, principle, accepted or not) in your final report", land)
        self.assertEqual(land.count("--rebut"), 1)
        self.assertIn("the same finding rejected twice: stop and report it to the orchestrator", land)
        self.assertNotIn("--rebut", pod.payload(BRIEF, repo="acme/other"))
        self.assertNotIn("--rebut", pod.payload(BRIEF, repo="acme-org/widget", config={"land_repos": []}))

    def test_the_skills_and_the_brief_header_carry_the_rule(self):
        line = fpn.sb.LAND_SETUP_LINE
        self.assertIn("This repository lands with `spire land`", line)
        for path in ("skills/fleet-coordination/SKILL.md", "skills/fleet-review/SKILL.md", "skills/fleet-explainer/SKILL.md", "skills/fleet-coordination/operator-brief-header.txt"):
            text = (ROOT / path).read_text()
            self.assertIn(".spire/tool/bin/spire land", text, path)
            self.assertIn("exit 22 = held for a person: stop and say so", text, path)
            self.assertIn("nohup sh -c '.spire/tool/bin/spire land > /work/land.log 2>&1;", text, path)
            self.assertIn(fpn.sb.LAND_STEP, text, path)   # the step is the launcher's own, word for word (no drift)
            self.assertEqual(self.detached_problems(text), [], path)
            self.assertIn("This repository lands with `spire land`", text, path)   # tied to the launcher's own marker
        self.assertIn(".spire/tool/bin/spire land", line)


if __name__ == "__main__":
    unittest.main()
