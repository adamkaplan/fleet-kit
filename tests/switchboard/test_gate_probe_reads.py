"""`gates-check` answers inside the shim's probe limit (ask #2215): a fake gh delays each read and logs it; the reads are counted and their span timed."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROBE_LIMIT = 3.0   # the shim's limit, in seconds
DELAY = 0.6         # what the fake hub takes for each read
HEAD = "a" * 40

GH_SCRIPT = """#!/usr/bin/env python3
import json, sys, time
start = time.time()
time.sleep(%(delay)r)
a = sys.argv[1:]
if a[:2] == ["pr", "view"]:
    print(json.dumps({"number": 7, "state": "OPEN", "headRefOid": %(head)r, "baseRefName": "main", "body": "no plan here\\n", "title": "t",
                      "author": {"login": "w"}, "isDraft": False, "files": [{"path": "a.py"}], "changedFiles": 1, "statusCheckRollup": [],
                      "reviews": [], "labels": [], "mergeable": "MERGEABLE", "baseRefOid": "b" * 40, "comments": []}))
    code = 0
else:
    sys.stderr.write("not found\\n")
    code = 1
with open(%(log)r, "a") as handle:
    handle.write(json.dumps({"argv": a, "start": start, "end": time.time()}) + "\\n")
sys.exit(code)
"""


class TestGateProbeReads(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gate-probe-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(str(self.tmp), ignore_errors=True))
        self.log = self.tmp / "reads.jsonl"
        gh = self.tmp / "gh"
        gh.write_text(GH_SCRIPT % {"delay": DELAY, "head": HEAD, "log": str(self.log)})
        gh.chmod(0o755)
        self.cfg = self.tmp / "config"
        (self.cfg / "fleet-switchboard").mkdir(parents=True)
        (self.cfg / "fleet-switchboard" / "config.json").write_text(json.dumps(
            {"gh": str(gh), "opencode": "/bin/true", "plan_gates": {"repos": {"acme/app": {"mode": "shadow"}}}}))
        (self.tmp / "state").mkdir()

    def check(self, kind):
        env = dict(os.environ, HOME=str(self.tmp), XDG_CONFIG_HOME=str(self.cfg), XDG_DATA_HOME=str(self.tmp / "data"),
                   FLEET_SWITCHBOARD_STATE=str(self.tmp / "state"))
        if self.log.exists():
            self.log.unlink()
        done = subprocess.run([sys.executable, str(REPO / "bin" / "fleet-switchboard"), "gates-check", "acme/app#7", "--for", kind, "--json"],
                              capture_output=True, text=True, env=env, timeout=60)
        reads = [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []
        return done, reads

    def test_s2_the_reads_are_few_and_their_span_leaves_margin_under_the_probe_limit(self):
        for kind in ("ready", "merge"):
            done, reads = self.check(kind)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            views = [r for r in reads if r["argv"][:2] == ["pr", "view"]]
            self.assertEqual(len(views), 1, "the pull request is read once: %s" % [r["argv"][:3] for r in reads])
            self.assertLessEqual(len(reads), 2, "one read of the pull request and one of its plan, no more: %s" % [r["argv"][:3] for r in reads])
            span = max(r["end"] for r in reads) - min(r["start"] for r in reads)
            self.assertLess(span, PROBE_LIMIT / 2.0, "the reads took %.2f s of the probe's %.1f s" % (span, PROBE_LIMIT))

    def test_s3_the_decision_is_the_one_the_gates_made_before(self):
        no_plan = "G0a: no plan found: pass --plan, or put `PLAN: <link>` in the PR body; G1: needs a plan that passes G0a"
        before = {"ready": (["G0a", "G1"], no_plan),   # the decisions of origin/integration on this fixture, read before the change
                  "merge": (["G0a", "G1", "G4"], no_plan + "; G4: CI is none (needs green at the head, or a second agent's re-run) (reported, not enforced)")}
        for kind, (gates, reason) in before.items():
            done, _ = self.check(kind)
            payload = json.loads(done.stdout)
            self.assertEqual((payload["verdict"], payload["mode"], payload["head"]), ("allow", "shadow", HEAD))
            self.assertEqual([f["gate"] for f in payload["failed"]], gates)
            self.assertEqual(payload["reason"], reason)


if __name__ == "__main__":
    unittest.main()
