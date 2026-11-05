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
    print(json.dumps({"title": "t", "state": "OPEN", "additions": 3, "deletions": 1, "changedFiles": 1, "statusCheckRollup": json.loads(__import__("os").environ.get("GH_STUB_ROLLUP", "[]")),
        "reviewDecision": "", "mergeable": "MERGEABLE", "headRefOid": "a" * 40, "baseRefOid": "b" * 40, "body": "no plan here\\n",
        "author": {"login": "x"}, "reviews": [], "comments": [], "labels": [], "isDraft": False, "baseRefName": "main",
        "files": [{"path": "README.md", "additions": 1, "deletions": 0, "changeType": "MODIFIED"}]}))
    sys.exit(0)
if a[:2] == ["api", "repos/acme/app/actions/runs/777"]:
    print(json.dumps({"w": 5, "e": "pull_request", "p": ".github/workflows/ci.yml"}))
    sys.exit(0)
print("[]" if a[:1] == ["api"] else "{}")
"""


def run_check(mode, enabled=True, touch_part=False, root=None, rollup=None):
    """(exit code, parsed answer, the recorded gh argv lines) of one `gates-check acme/app#12 --for merge --json`."""
    root = root or Path(tempfile.mkdtemp(prefix="gates-reads-"))
    log = root / "calls.log"
    gh = root / "gh"
    gh.write_text(GH_STUB_TEXT % {"log": str(log)})
    gh.chmod(0o755)
    oc = root / "opencode"
    oc.write_text("#!/bin/sh\nexit 0\n")
    oc.chmod(0o755)
    (root / "cfg" / "fleet-switchboard").mkdir(parents=True, exist_ok=True)
    (root / "state" / "fleet-switchboard").mkdir(parents=True, mode=0o700, exist_ok=True)
    (root / "cfg" / "fleet-switchboard" / "config.json").write_text(json.dumps(
        {"gh": str(gh), "opencode": str(oc), "plan_gates": {"repos": {"acme/app": {"mode": mode, "enabled": enabled, "budget_ms": 20000}}}}))
    env = dict(os.environ, XDG_CONFIG_HOME=str(root / "cfg"), XDG_STATE_HOME=str(root / "state"), HOME=str(root))
    env.pop("FLEET_SWITCHBOARD_UNDER_TEST", None)
    if rollup is not None:
        env["GH_STUB_ROLLUP"] = json.dumps(rollup)
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

    def test_s2_a_run_identity_read_once_is_kept_for_the_next_check(self):
        rollup = [{"__typename": "CheckRun", "name": "test", "workflowName": "ci", "status": "COMPLETED", "conclusion": "SUCCESS",
                   "detailsUrl": "https://github.com/acme/app/actions/runs/777/job/1", "startedAt": "2026-11-05T10:00:00Z", "completedAt": "2026-11-05T10:05:00Z"}]
        root = Path(tempfile.mkdtemp(prefix="gates-reads-"))
        first = run_check("shadow", root=root, rollup=rollup)
        (root / "calls.log").unlink()
        second = run_check("shadow", root=root, rollup=rollup)
        reads = lambda calls: [c for c in calls if c.startswith("api repos/acme/app/actions/runs/")]
        self.assertEqual(len(reads(first[2])), 1, first[2])
        self.assertEqual(reads(second[2]), [], second[2])
        self.assertEqual(first[1], second[1])

    def test_s3_not_opted_in_and_disabled_still_allow(self):
        code, answer, calls = run_check("shadow", enabled=False)
        self.assertEqual((code, answer["verdict"]), (0, "allow"))
        self.assertEqual([c for c in calls if c.startswith("pr view")], [])


class TestKeptLedgerChain(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(REPO / "bin"))
        import plan_ledger
        self.pl = plan_ledger
        self.dir = tempfile.mkdtemp(prefix="chain-")
        for n in range(3):
            self.pl.append_entry(self.dir, {"pr": "acme/app#%d" % n, "kind": "proof", "result": "pass"}, 1000 + n)

    def test_s3_a_second_read_gives_the_same_answer_from_the_kept_entries(self):
        first = self.pl.read_ledger(self.dir)
        self.assertTrue((Path(self.dir) / self.pl.KEPT_CHAIN_FILE).exists())
        self.assertEqual(self.pl.read_ledger(self.dir), first)
        self.assertEqual(first[1], None)

    def test_s3_an_edited_ledger_is_still_found_with_a_kept_copy_beside_it(self):
        self.pl.read_ledger(self.dir)
        path = Path(self.dir) / self.pl.LEDGER_FILE
        path.write_text(path.read_text().replace("acme/app#1", "acme/app#9"))
        entries, problem = self.pl.read_ledger(self.dir)
        self.assertIn("edited", problem or "")


if __name__ == "__main__":
    unittest.main()
