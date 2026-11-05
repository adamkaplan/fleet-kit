"""`gates-check` reads the pull request once and starts without work it does not need (ask #2230).

Each test runs the real program in a child process with a fake gh that records its argv, in a temp config and state directory.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROGRAM = REPO / "bin" / "fleet-switchboard"

GH_STUB_TEXT = """#!/usr/bin/env python3
import json, sys
open(%(log)r, "a").write(" ".join(sys.argv[1:]) + "\\n")
a = sys.argv[1:]
if a[:2] == ["pr", "view"]:
    print(json.dumps({"title": "t", "state": "OPEN", "additions": 3, "deletions": 1, "changedFiles": 1, "statusCheckRollup": [],
        "reviewDecision": "", "mergeable": "MERGEABLE", "headRefOid": "a" * 40, "baseRefOid": "b" * 40, "body": "no plan here\\n",
        "author": {"login": "x"}, "reviews": [], "comments": [], "labels": [], "isDraft": False, "baseRefName": "main",
        "files": [{"path": "README.md", "additions": 1, "deletions": 0, "changeType": "MODIFIED"}]}))
    sys.exit(0)
print("[]" if a[:1] == ["api"] else "{}")
"""


def run_check(mode, enabled=True, touch_part=False):
    """(exit code, parsed answer, the recorded gh argv lines) of one `gates-check acme/app#12 --for merge --json`."""
    root = Path(tempfile.mkdtemp(prefix="gates-reads-"))
    log = root / "calls.log"
    gh = root / "gh"
    gh.write_text(GH_STUB_TEXT % {"log": str(log)})
    gh.chmod(0o755)
    oc = root / "opencode"
    oc.write_text("#!/bin/sh\nexit 0\n")
    oc.chmod(0o755)
    (root / "cfg" / "fleet-switchboard").mkdir(parents=True)
    (root / "state" / "fleet-switchboard").mkdir(parents=True, mode=0o700)
    (root / "cfg" / "fleet-switchboard" / "config.json").write_text(json.dumps(
        {"gh": str(gh), "opencode": str(oc), "plan_gates": {"repos": {"acme/app": {"mode": mode, "enabled": enabled, "budget_ms": 20000}}}}))
    env = dict(os.environ, XDG_CONFIG_HOME=str(root / "cfg"), XDG_STATE_HOME=str(root / "state"), HOME=str(root))
    env.pop("FLEET_SWITCHBOARD_UNDER_TEST", None)
    if touch_part:   # a stale kept compile: the source is newer than the cache, so it is compiled again
        part = REPO / "bin" / "switchboard" / "plan_input.py"
        os.utime(str(part), None)
    done = subprocess.run([sys.executable, str(PROGRAM), "gates-check", "acme/app#12", "--for", "merge", "--json"],
                          capture_output=True, text=True, env=env, timeout=120)
    calls = log.read_text().splitlines() if log.exists() else []
    return done.returncode, json.loads(done.stdout), calls


class TestGatesCheckReads(unittest.TestCase):
    def test_s2_the_pull_request_is_read_once(self):
        code, answer, calls = run_check("shadow")
        views = [c for c in calls if c.startswith("pr view")]
        self.assertLessEqual(len(views), 1, "reads of the pull request: %s" % views)
        self.assertEqual(answer["verdict"], "allow", answer)

    def test_s3_the_decisions_are_the_same_with_a_stale_kept_compile(self):
        fresh = run_check("enforce")
        stale = run_check("enforce", touch_part=True)
        self.assertEqual(fresh[0], stale[0])
        self.assertEqual(fresh[1], stale[1])
        self.assertEqual(fresh[0], 0, fresh[1])   # enforce is downgraded to shadow in this bare fixture (no ci recorder, no chief-of-staff file): an allow
        self.assertEqual([f["gate"] for f in fresh[1]["failed"]][:2], ["G0a", "G1"])

    def test_s3_not_opted_in_and_disabled_still_allow(self):
        code, answer, calls = run_check("shadow", enabled=False)
        self.assertEqual((code, answer["verdict"]), (0, "allow"))
        self.assertEqual([c for c in calls if c.startswith("pr view")], [])


if __name__ == "__main__":
    unittest.main()
