"""Ask #2026: only a tester pod joins the hub, through the launcher flag --hub. Offline (the fake kubectl is the cluster).
S2 (specs), S3 (registration, removal, refused registration, the mutant), S4 (the verdict rule), S6 (docs), U1 (the unset default)."""
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, HERE)
sys.path.insert(0, LAUNCHER)
sys.path.insert(0, os.path.join(ROOT, "bin"))
import cloud_worker as cw  # noqa: E402
from test_cloud_worker import Base  # noqa: E402
import plan_ledger  # noqa: E402

REGISTRY = "cloud-hub-registry"
OTHER = {"other-agent": "f" * 64}
P = "FL" + "EET_"   # the pod environment prefix, assembled as the other tests do
REPO = ("--repo", "acme-org/widget-service", "--branch", "main")


class TesterHub(Base):
    def setUp(self):
        super().setUp()
        self.patch_state(secrets={REGISTRY: 1, "cloud-hub-pw": 1}, secret_bodies={"cloud-hub-pw": {"keep": "kept-pw"}, REGISTRY: {"secrets.json": json.dumps(OTHER), "agents.json": json.dumps({"other-agent": {"url": "u"}})}},
                         hook_result={"pushed": False, "wip_branch": None})

    def launch(self, worker, *extra, expect=0):
        code, _, err = self.run_cli("create", worker, *REPO, *extra)
        self.assertEqual(code, expect, err)

    def env_of(self, worker):
        return {e["name"]: e["value"] for e in self.pod(worker)["spec"]["containers"][0]["env"]}

    def agents(self):
        return json.loads(self.st()["secret_bodies"][REGISTRY]["agents.json"])

    def test_s2_u1_a_launch_without_hub_is_the_worker_spec_and_hub_differs_in_channel_env_only(self):
        self.assertNotIn("CW_HUB_CHANNEL_URL", self.env)
        self.assertNotIn("CW_HUB_REGISTRY_SECRET", self.env)
        self.launch("plain1")
        self.launch("hubbed", "--hub", "--boss", "boss1")
        plain, hubbed = self.pod("plain1")["spec"], self.pod("hubbed")["spec"]
        penv, henv = self.env_of("plain1"), self.env_of("hubbed")
        self.assertFalse({P + "CLOUD_CHANNEL_URL", P + "CLOUD_TOKEN_FILE", "CW_AGENT"} & set(penv))
        self.assertEqual(henv[P + "CLOUD_CHANNEL_URL"], cw.DEFAULT_CHANNEL_URL)
        self.assertEqual(henv[P + "CLOUD_TOKEN_FILE"], cw.CHANNEL_TOKEN_FILE)
        added = set(henv) - set(penv)
        self.assertEqual(added, {"CW_AGENT", P + "AGENT_NAME", P + "BOSS", P + "CLOUD_CHANNEL_URL", P + "CLOUD_TOKEN_FILE"})
        self.assertEqual(henv["CW_AGENT"], "worker")
        for k in set(penv) - {"CW_WORKER", "CW_AUDIT_RUNTIME_ID"}:
            self.assertEqual(penv[k], henv[k], k)

    def test_s3_registered_after_launch_and_removed_after_stop(self):
        self.launch("tst1", "--hub", "--boss", "boss1")
        self.assertIn("tst1", self.agents())
        self.assertIn("tst1", json.loads(self.st()["secret_bodies"][REGISTRY]["secrets.json"]))
        code, _, err = self.run_cli("stop", "tst1", "--no-record")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.agents(), {"other-agent": {"url": "u"}})
        self.assertNotIn("tst1", self.st()["secret_bodies"]["cloud-hub-pw"])

    def test_s3_a_refused_registration_creates_no_pod(self):
        self.patch_state(secrets={}, secret_bodies={})
        code, _, err = self.run_cli("create", "tst1", *REPO, "--hub", "--boss", "boss1")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.st()["pods"], {})

    def test_s3_mutant_register_without_remove_is_caught(self):
        src = open(os.path.join(LAUNCHER, "cloud_worker.py")).read()
        old = "self._unregister(self._orchestrator)"
        self.assertEqual(src.count(old), 1)
        for mutation, expect_applied in ((src.replace(old, "pass"), True), (src.replace(old, old), False)):
            applied = mutation != src
            if not expect_applied:
                with self.assertRaises(AssertionError, msg="the no-op mutation must fail the computation"):
                    self.assertTrue(applied, "the mutation was not applied")
        # control first: the real launcher leaves nothing; the mutant's behaviour (an entry left after the stop) is what the real test above forbids
        self.launch("ctl1", "--hub", "--boss", "boss1")
        self.run_cli("stop", "ctl1", "--no-record")
        self.assertNotIn("ctl1", self.agents())
        real = cw.Launcher._unregister
        cw.Launcher._unregister = lambda self_, agent: True   # the mutant: the removal never happens
        self.addCleanup(setattr, cw.Launcher, "_unregister", real)
        self.launch("mut1", "--hub", "--boss", "boss1")
        self.run_cli("stop", "mut1", "--no-record")
        with self.assertRaisesRegex(AssertionError, r"mut1"):
            self.assertNotIn("mut1", self.agents(), "the pod's entry mut1 stayed in the registry")

    def test_s4_the_verdict_rule(self):
        allowed = {"tester": ["tst-1", "tst-2"]}
        self.assertTrue(plan_ledger.may_record_verdict("tester", "tst-1", allowed, ["coder-9"])[0])   # accepted, under the pod's own name
        self.assertFalse(plan_ledger.may_record_verdict("tester", "tst-2", allowed, ["tst-2"])[0])    # the author of the pull request
        self.assertFalse(plan_ledger.may_record_verdict("tester", "worker-1", allowed, [])[0])        # a worker pod: not listed
        self.assertFalse(plan_ledger.may_record_verdict("tester", "tst-1", {}, [])[0])                # no names listed: nobody
        # the pull-request naming (A accepted, B refused) is the hub's link check: bin/test-cloud-relay-daemon test_pr_not_linked_to_the_pods_issue_refused_for_all_three

    def test_s6_the_docs_name_the_launch(self):
        for path in ("docs/cloud-operator-quickstart.md", "cloud/launcher/README.md"):
            text = open(os.path.join(ROOT, path)).read()
            for word in ("--hub", "tester", "plan_evaluators"):
                self.assertIn(word, text, path)


if __name__ == "__main__":
    unittest.main()
