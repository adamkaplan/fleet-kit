#!/usr/bin/env python3
"""Offline tests: an orchestrator launch always gives its hub entry the session (#1298); a worker launch is unchanged.

Cause: `hub` in bin/cloud_worker.py followed only the hub setting, while the launcher registers a hub entry for every orchestrator launch.
Without the setting the session step was skipped, the entry's session stayed empty, the hub refused the record and every message answered 503.
Run directly: python3 cloud/launcher/tests/test_orchestrator_hub_session.py
"""
import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("FL" + "EET_SWITCHBOARD_UNDER_TEST", "1")
_loader = importlib.machinery.SourceFileLoader("cw_test_cloud_worker_mod", str(ROOT / "bin" / "test-cloud-worker"))
_spec = importlib.util.spec_from_loader("cw_test_cloud_worker_mod", _loader)
tcw = importlib.util.module_from_spec(_spec)
sys.modules["cw_test_cloud_worker_mod"] = tcw
_loader.exec_module(tcw)

# Captured from the code before the change: what a worker launch without the hub setting hands the launcher (per-run values shaped).
BASELINE_WORKER_ARGV = ["create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main", "--run-id", "<run-id>", "--json",
                        "--password-fd", "<fd>", "--run-secret-fd", "model-auth=<fd>", "--run-secret-fd", "git-token=<fd>"]


class Launch(tcw.Base):
    def setUp(self):
        super().setUp()
        text = self.stub.read_text().replace('"--json")', '"--json", "--agent", "--boss")', 1)   # the launcher's create knows these two (the stub predates them)
        self.stub.write_text(text)
        for name in ("CW_HUB_CHANNEL_URL", "CW_HUB_REGISTRY_SECRET"):
            os.environ.pop(name, None)

    def orchestrator(self):
        brief = self.home / "brief.txt"
        brief.write_text("Build the thing.\n")
        return self.main("launch", "orch1", "--cloud", "--agent", "orchestrator", "--repo", "acme-org/widget-service", "--branch", "main",
                         "--role", "orchestrator", "--reports-to", "boss1", "--brief-file", str(brief))

    def hub_calls(self):
        return [c for c in self.stub_calls() if c[0] == "hub-session"]


class TestHubSession(Launch):
    def test_s2_s3_orchestrator_without_the_setting_gives_the_entry_the_session_once(self):
        code, out, err = self.orchestrator()
        self.assertEqual(code, 0, err)
        calls = self.hub_calls()
        print("%d of 1 hub-session calls with the new session" % len([c for c in calls if c[2:4] == ["--session", "ses_remote1"]]))
        self.assertEqual(len(calls), 1, "the session step ran %d times, not once" % len(calls))
        self.assertEqual(calls[0][1], "orch1")
        self.assertEqual(calls[0][2:4], ["--session", "ses_remote1"])

    def test_s5_worker_without_the_setting_does_not_call_the_step(self):
        code, out, err = self.launch()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.hub_calls(), [])
        self.assertNotIn("hub-session", "\n".join(" ".join(c) for c in self.stub_calls()))

    def test_u1_worker_argv_is_identical_to_the_baseline(self):
        code, out, err = self.launch()
        self.assertEqual(code, 0, err)
        argv = [self.shaped(c) for c in self.stub_calls() if c[0] == "create"]
        self.assertEqual(len(argv), 1)
        self.assertEqual(argv[0], BASELINE_WORKER_ARGV)


class TestMutant(unittest.TestCase):
    def test_s4_a_mutant_that_reverts_the_line_fails_the_orchestrator_test(self):
        src = (ROOT / "bin" / "cloud_worker.py").read_text()
        self.assertEqual(src.count(' or args.agent == "orchestrator")'), 1, "the changed line is not as expected")
        mutant = src.replace(' or args.agent == "orchestrator")', ")", 1)
        with tempfile.TemporaryDirectory() as d:   # a copy of the tree parts the test reads, with the reverted line
            shutil.copytree(ROOT / "bin", os.path.join(d, "bin"), ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(ROOT / "cloud" / "worker" / "tests", os.path.join(d, "cloud", "worker", "tests"), ignore=shutil.ignore_patterns("__pycache__"))
            os.makedirs(os.path.join(d, "cloud", "launcher", "tests"))
            shutil.copy(__file__, os.path.join(d, "cloud", "launcher", "tests"))
            Path(d, "bin", "cloud_worker.py").write_text(mutant)
            res = subprocess.run([sys.executable, os.path.join(d, "cloud", "launcher", "tests", Path(__file__).name),
                                  "TestHubSession.test_s2_s3_orchestrator_without_the_setting_gives_the_entry_the_session_once"],
                                 env=dict(os.environ, TMPDIR=d), capture_output=True, text=True, cwd=d)
        print("%d of 1 mutants refused" % (res.returncode != 0))
        self.assertNotEqual(res.returncode, 0, "the mutant passed the orchestrator test")


class TestDocs(unittest.TestCase):
    def test_docs_orchestrator_hub_session(self):
        text = (ROOT / "cloud" / "README.md").read_text()
        self.assertIn("needs no hub setting", text)
        self.assertIn("answers 503", text)


if __name__ == "__main__":
    unittest.main()
